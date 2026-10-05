# 설계 노트

이 도구를 만들면서 확인한 것과 막혔던 것을 적는다. 기기는 전부 Galaxy S24 (One UI 8.5, Android 16) 한 대이고, 일반화할 수 있는지는 모른다.

## 목표와 제약

- 카톡 채널(광고) 메시지의 앱 배지와 채팅 목록 안읽음을 없앤다. 친구 채팅의 알림과 배지는 유지한다.
- 폰이든 PC든 사용 중인 화면에 카톡이 뜨거나 포커스가 넘어가면 안 된다.
- 알림 자체를 끄는 방식은 쓸 수 없다고 봤다 (카톡을 쓰려면 알림 수신 동의가 필요한 구조라서).

## 해보고 버린 것

- **전체 읽음 기능**: 사용성이 나빠서 제외.
- **카톡 채팅방 폴더**: 폴더에 넣어도 전체 목록의 안읽음 숫자가 사라지지 않는다. 대신 폴더를 "광고 채널을 모아둔 고정된 탐색 대상"으로만 쓴다.
- **카톡 PC 클라이언트를 다른 곳에 새로 로그인**: 동시 로그인이 1세션이라 기존 세션이 끊긴다.
- **알림 지우기**: 삼성에서 알림창의 알림을 지우면 앱 아이콘 배지는 사라지지만 채팅 목록의 안읽음은 그대로다.

## 핵심 아이디어

카톡 채팅방을 "열어서 활성 상태로 만들면" 읽음 처리된다. 그 동작을 물리 화면과 분리된 가상 디스플레이에서 하면 화면을 점유하지 않는다. 가상 디스플레이에서 연 채팅방이 읽음 처리되어 폰의 안읽음이 사라지는 것을 확인했다.

## 가상 디스플레이

- 다른 앱(카톡)의 액티비티를 올리려면 **TRUSTED 가상 디스플레이**여야 하고, 이건 일반 앱이 갖지 못하는 권한이 필요하다 (shell은 가짐). 그래서 shell 권한 프로세스가 필요하다 (제 이해 기준이며 일반 앱으로 직접 시도해보지는 않았다).
- scrcpy `--new-display`로 먼저 확인했다. 다만 scrcpy는 비디오 싱크(재생 또는 녹화)가 하나는 있어야 가상 디스플레이를 유지한다 (`--no-window`는 `--new-display`와 함께 쓸 수 없다). PC에서는 창을 화면 밖 좌표에 두는 식으로 우회했지만, 폰 단독으로는 그런 우회가 불가능하다.
- 그래서 `app_process`로 도는 작은 Java 헬퍼가 `DisplayManager.createVirtualDisplay`에 더미 `ImageReader` Surface를 붙여 만든다. 소비자가 프레임을 비우지 않으면 앱 렌더링이 멈출 수 있다고 보고 프레임을 계속 비운다 (`drain=0` 변형으로 그 가설을 확인하는 옵션이 있지만 결과는 기록하지 않았다).
- 헬퍼는 표준입력의 개행/EOF를 종료 신호로 받아 즉시 디스플레이를 해제한다. `hold` 시간은 안전 상한이다.

### 삼성에서 막혔던 것

shell 프로세스에서 `ActivityThread`를 리플렉션으로 만들면 `mConfigurationController`가 비어 있어서, 삼성의 `CompatSandbox.applyDisplaySandboxingIfNeeded`가 `ActivityThread.getConfiguration()`을 호출할 때 NPE가 나고 프로세스가 `Killed`된다. `ConfigurationController`를 만들어 채워 넣으면 해결된다 (`helper/VdTest.java`의 `systemContext()`).

### 카톡이 이미 실행 중일 때

`am start --display`가 `Activity not started, intent has been delivered to currently running top-most instance`를 출력한다. 관찰한 경우에는 태스크가 가상 디스플레이로 올라가고 물리 화면 포커스는 그대로였다. 하지만 사용자가 물리 화면에서 카톡을 쓰는 중에도 안전한지는 확인하지 못해서, 물리 화면에서 카톡이 Resumed면 그 회차를 건너뛰는 가드를 넣었다.

## 접속 경로: 왜 `adb tcpip 5555`인가

처음에는 폰 안에서 권한을 얻는 Shizuku 앱 방식을 검토했다. 그러나 무인 운영이 필수라서, 폰이 이동 중에도 닿는 접속 경로를 먼저 확인했다.

- **무선 디버깅(TLS) 페어링은 오래 못 갔다.** 페어링한 PC가 8일 뒤에 접속 거부됐다 (TLS 단계에서 실패, 포트와 ping은 정상). 재페어링 후 접속됐다. 자동 해제됐다는 가설과 맞지만 폰의 페어링 목록은 확인하지 못했다.
- 무선 디버깅은 켤 때마다 포트가 바뀌고, 네트워크 전환에서 꺼질 수 있다 (제 이해 기준).
- **`adb tcpip 5555`** 는 포트가 고정이고, Wi-Fi를 껐다 켜도 유지됐다. 재부팅하면 풀려서 다시 실행해야 한다.
- **Tailscale**과 함께 쓰면 모바일 데이터에서도 `tailscale ip:5555`로 접속된다 (직접 연결, 지연 약 46ms). 호스트를 집의 한 대로 둘 수 있다.

Shizuku의 "무선 디버깅으로 시작"이 `SSLV3_ALERT_CERTIFICATE_UNKNOWN`으로 실패한 것은, PC에서 `adb pair`를 해서 PC만 페어링되고 Shizuku 자체 키가 페어링되지 않았기 때문일 가능성이 있다 (검증하지 않았다). 호스트 방식으로 가서 더 파지 않았다.

## 폴링과 트리거

- 광고 채널은 알림이 꺼져 있는 경우가 많아서 알림 기반 트리거가 안 울릴 수 있다. 그래서 주기적 폴링으로 한다. (폴링이 부하가 낮아서 선택한 것이 아니다. 이벤트 기반이 대기 비용은 더 낮다.)
- UI를 띄우지 않고 안읽음 수를 읽는 방법은 못 찾았다. 삼성 배지 provider(`content://com.sec.badge/apps`)는 shell 권한으로 접근이 거부된다 (`com.sec.android.provider.badge.permission.READ` 필요).

## UI 식별

- `uiautomator dump --display <논리 id>`로 가상 디스플레이의 UI를 얻는다. 고정 좌표는 배너와 스크롤 위치에 취약해서 쓰지 않고, 노드의 resource-id와 접근성 라벨로 찾는다.
- 채널 이름은 `com.kakao.talk:id/name`, 안읽음 배지는 `com.kakao.talk:id/unread_count`로 관찰했다 (멤버 수와는 별개 노드).
- 탭의 접근성 라벨에 폴더 단위 안읽음 수가 있다 (예: `광고 새로운 메시지 3개`). 아직 활용하지 않는다.
- `screencap -d`에는 SurfaceFlinger 내부 id가 필요하고, scrcpy나 헬퍼가 알려주는 논리 id와 다르다 (`dumpsys SurfaceFlinger --display-id`).

## Windows(Git Bash) 함정

`/sdcard/...` 같은 유닛스 스타일 절대경로를 Git Bash가 `C:/Program Files/Git/sdcard/...`로 바꾼다. `MSYS_NO_PATHCONV=1`을 지정하고, 이 경우 로컬 경로는 `C:/...`처럼 드라이브 문자를 포함해야 한다.
