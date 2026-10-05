# kakao-mute

카카오톡 채널(광고) 메시지가 만드는 **앱 아이콘 배지**와 **채팅 목록 안읽음 숫자**를 자동으로 읽음 처리하는 도구. 친구 채팅은 건드리지 않고, 폰 화면에는 아무것도 띄우지 않는다.

> **비공식 도구다.** 카카오와 무관하며, 카카오톡 UI를 자동으로 조작한다. 카카오 운영정책이 이런 자동화를 금지하는지는 확인하지 못했다. 사용에 따른 책임(계정 제재 등)은 사용자에게 있다.
>
> **검증 범위: Galaxy S24 (One UI 8.5, Android 16) 한 대.** 다른 기기와 버전은 확인하지 않았다. 삼성 구현에 대한 우회가 코드에 들어 있다.

## 동작 원리

1. 호스트(PC/서버)가 adb로 폰에 접속한다.
2. shell 권한 헬퍼(`app_process`)가 화면 출력이 없는 **가상 디스플레이**를 만든다. 더미 `ImageReader` Surface를 붙여서 물리 화면에는 아무것도 나타나지 않는다.
3. 그 가상 디스플레이에서 카톡을 띄워 `광고` 탭 맨 위 채널에 안읽음이 있으면 열었다 닫는다. 이게 읽음 처리다.
4. 안읽음이 없을 때까지 반복한 뒤 디스플레이를 해제한다.
5. 사용자가 물리 화면에서 카톡을 쓰는 중이면 그 회차는 건너뛴다 (채널을 열기 직전에도 다시 확인한다).

알림이 꺼진 채널은 알림 기반 트리거가 동작하지 않아서 **폴링**으로 확인한다. 폰이 **잠겨 있으면 가상 디스플레이로 보낸 입력이 무시되므로**, 잠금 상태를 짧은 간격(60초)으로 확인하고 **잠금이 풀려 있는 동안에만** 사이클을 돌린다 (풀린 직후 한 번, 이후 10분마다. 직전 사이클 후 5분이 안 지났으면 지날 때까지 미룬다). 사이클 1회는 폰에 CPU 약 18 코어-초(벽시계 약 15초)를 더하므로 자주 돌리지 않는다. 조사 과정과 시행착오는 [docs/design-notes.md](docs/design-notes.md).

## 요구사항

- 폰: 개발자 옵션의 USB 디버깅, `adb tcpip 5555`로 열어둔 adb. 집 밖에서도 쓰려면 Tailscale 같은 VPN 권장.
- 호스트: Python 3.8+ (표준 라이브러리만 사용), adb(platform-tools). 헬퍼 빌드에는 JDK 17+와 Android SDK(build-tools, platforms)가 필요하다.

## 카톡 쪽 선행 조건 (수동)

1. 채팅 목록에 **`광고` 폴더(탭)** 를 만들고 광고 채널을 직접 넣는다. 새 광고 채널이 생기면 직접 추가해야 한다.
2. **안읽음순 정렬**을 적용한다. 안읽음 채널이 맨 위에 와야 "맨 위만 확인하고 반복"이 성립한다.

> **친구 채팅을 `광고` 탭에 넣지 말 것.** 넣으면 읽음 처리된다.

## 설치와 실행

```bash
# 1) 헬퍼 빌드
bash helper/build.sh

# 2) 폰을 TCP 모드로 (USB 또는 무선 디버깅으로 연결된 상태에서 1회, 재부팅하면 다시 필요)
adb tcpip 5555
adb connect <폰 IP>:5555        # 처음 한 번 폰에서 "항상 허용" 승인

# 3) 설정
cp daemon/config.example.json daemon/config.json   # serial/connect 를 폰 주소로 수정

# 4) 확인
python daemon/kakao_mute.py check-lock    # 폰이 잠겨 있는지 (잠김/풀림/판별 불가)
python daemon/kakao_mute.py check-guard   # 물리 화면에서 카톡이 켜져 있는지
python daemon/kakao_mute.py discover      # 폴더 탭까지만 열고 목록 노드 출력 (채널은 안 연다)
python daemon/kakao_mute.py once          # 사이클 1회
python daemon/kakao_mute.py run           # 폴링 루프 (잠금 해제 중에만 사이클)
```

## 설정 (`daemon/config.json`)

| 키 | 기본값 | 설명 |
|---|---|---|
| `serial`, `connect` | - | adb 대상 (`ip:5555`). `connect`는 목록에 없을 때 `adb connect`에 쓴다 |
| `folder_tab` | `광고` | 폴더 탭 이름 |
| `chat_name_id` | `com.kakao.talk:id/name` | 채널 이름 노드의 resource-id |
| `unread_badge_id` | `com.kakao.talk:id/unread_count` | 안읽음 배지 노드의 resource-id |
| `lock_check_interval_sec` | 60 | 잠금 상태 확인 간격 |
| `unlocked_cycle_interval_sec` | 600 | 잠금이 풀려 있는 동안 사이클 간격 |
| `min_cycle_gap_sec` | 300 | 잠금 해제 직후 사이클이라도 직전 사이클 후 이 시간이 안 지났으면 미룸 |
| `unlock_settle_sec` | 5 | 잠금이 풀린 직후 사이클 전에 기다리는 시간 |
| `dwell_sec` | 3 | 채널을 열어두는 시간 |
| `max_open_per_cycle` | 20 | 한 회차에 열 최대 채널 수 |
| `skip_if_foreground` | `["com.kakao.talk"]` | 물리 화면에서 이 앱이 켜져 있으면 회차를 건너뜀 |
| `discord_webhook_url` | `""` | 실패 알림 (환경변수 `KMUTE_DISCORD_WEBHOOK`도 가능) |
| `alert_after_sec` / `alert_repeat_sec` | 1800 / 10800 | 실패가 이 시간 이상 계속되면 알림 / 알림 반복 간격 |
| `alert_min_interval_sec` | 21600 | 같은 채널의 멈춤 알림 최소 간격 |

resource-id는 카톡을 업데이트하면 바뀔 수 있다. 동작이 깨지면 `discover`로 다시 확인한다.

## 디스코드 알림

`discord_webhook_url`이 있으면 데몬 시작, 연속 실패, 멈춤 감지(열었는데 안읽음이 안 풀림), 복구를 알린다. **웹훅 URL은 비밀값**이니 `config.json`(gitignore)이나 환경변수로만 넣고 커밋하지 않는다. 미설정이면 로그만 남긴다.

## 서버 운영

[deploy/kakao-mute.service](deploy/kakao-mute.service)에 systemd 유닛 예시가 있다 (검증하지 않았다). 호스트는 **하나만** 돌린다. 두 호스트가 같은 폰에서 동시에 사이클을 돌리는 것을 막는 락은 없다.

## 알려진 제약

- **폰이 잠겨 있으면 동작하지 않는다 (관찰).** 같은 코드와 같은 좌표로, 화면이 켜져 있어도 잠금 상태에서는 가상 디스플레이로 보낸 탭이 무시됐고, 잠금을 풀자 정상 동작했다. 원인은 키가드(잠금 화면)로 추정하지만 다른 요인과 완전히 분리해 확인하지는 않았다.
- 그래서 데몬은 잠금 상태(`dumpsys trust`의 `deviceLocked`, `dumpsys window`의 `isKeyguardShowing`)를 확인해, 잠겨 있으면 사이클을 돌리지 않는다. 이건 실패로 세지 않아서 알림도 가지 않는다.
- 만약 잠금 판별을 놓쳐 사이클이 돌아도, 탭 선택 검증이 실패해 중단되고 채널을 열지 않는다 (다른 탭의 친구 채팅을 건드리지 않는다).
- 즉 읽음 처리는 **폰이 잠금 해제된 동안에만** 된다. 광고 배지는 폰을 켜서 잠금을 푼 뒤 보통 1분 안에 지워지지만, 직전 사이클이 5분 안이었으면 최대 5분 남을 수 있다.

## 보안 주의

- `adb tcpip 5555`는 인증된 키만 접속할 수 있지만, 같은 네트워크(VPN 포함)의 다른 기기가 접속을 시도할 수 있다. Tailscale을 쓴다면 ACL로 호스트만 허용하는 것을 권장한다.
- 호스트는 폰의 화면과 알림을 읽을 수 있는 shell 권한을 가진다. 신뢰하는 개인 장비에서만 돌릴 것.

## 검증된 것과 아닌 것

확인함 (Galaxy S24 / One UI 8.5 / Android 16):
- 더미 Surface 가상 디스플레이에서 카톡이 렌더링되고, 채널을 열었다 닫으면 폰의 안읽음 배지와 숫자가 사라진다.
- `adb tcpip 5555`가 Wi-Fi 전환 후에도 유지되고, Tailscale 경유로 모바일 데이터에서도 접속된다.
- 물리 화면에서 카톡 사용 중임을 감지하는 가드 파서.
- 폴링 로직(잠금 판별 파싱, 잠금 중 사이클 생략, 해제 직후/간격 사이클, 멈춤 감지, 가드 재확인, 시간 기준 알림과 복구)은 가짜 폰/웹훅으로 `tests/test_daemon.py`에서 검증. 실기기에서는 `check-lock`이 잠김/풀림을 각각 맞게 판별하고, 풀린 상태에서 `Poller.step()` 1회가 사이클을 정상 실행하는 것을 확인했다. 잠금 → 해제 전환을 실기기에서 `run`으로 지켜보거나 며칠 돌려본 것은 아직 아니다.

확인하지 못함:
- 수일 단위 장시간 폴링 안정성, 며칠 뒤에도 adb 인증이 유지되는지 (무선 디버깅 페어링이 8일 만에 풀린 사례가 있었다).
- 상단 고정이 아닌 광고 채널에서의 안읽음순 정렬 동작과, 실제 광고 채널을 통한 읽음 처리 (친구 채팅으로만 확인).
- 읽음 처리가 카톡 서버 쪽(다른 기기 세션)에 반영되는지: scrcpy 가상 디스플레이에서는 확인했지만 더미 Surface 경로에서는 직접 보지 않았다.
- 실제 디스코드 웹훅 전송, systemd 유닛.

## 개발

```bash
python tests/test_daemon.py   # 폰 없이 로직 검증
```

## 출처

가상 디스플레이를 만드는 방식은 [scrcpy](https://github.com/Genymobile/scrcpy)(Apache-2.0)의 접근을 참고해 구현했다.

## 라이선스

[MIT](LICENSE)
