import json
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
import kakao_mute as km

km.time.sleep = lambda s: None

# ---- 가짜 웹훅 서버 ----
received = []


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        received.append((self.headers.get("User-Agent"), json.loads(body)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d/hook" % srv.server_port

CFG = dict(km.DEFAULTS, serial=None, chat_name_id="id/name", unread_badge_id="id/unread",
           discord_webhook_url=URL, launch_wait_sec=0, settle_sec=0, dwell_sec=0, late_wait_sec=0,
           failure_dump=str(Path(__file__).resolve().parent / "_failure_dump_test.xml"))


def xml(rows, tab=True, bottom=False):
    out = ['<hierarchy>']
    if bottom:
        out.append('<node content-desc="채팅 탭 3개의 새로운 업데이트" clickable="true" bounds="[241,2148][409,2273]"/>')
    if tab:
        sel = "false" if tab == "off" else "true"
        out.append('<node text="광고" resource-id="id/tab_chip" clickable="true" selected="%s" bounds="[0,0][100,100]"/>' % sel)
    y = 200
    for name, unread in rows:
        out.append('<node clickable="true" bounds="[0,%d][1080,%d]">' % (y, y + 150))
        out.append('<node text="%s" resource-id="id/name" bounds="[0,%d][100,%d]"/>' % (name, y, y + 50))
        if unread:
            out.append('<node text="%s" resource-id="id/unread" bounds="[0,%d][50,%d]"/>' % (unread, y + 60, y + 100))
        out.append('</node>')
        y += 150
    out.append('</hierarchy>')
    return ET.fromstring("".join(out))


class FakeHelper:
    stopped = 0

    def __init__(self, *a):
        pass

    def wait_display_id(self, timeout=20):
        return 99

    def stop(self):
        FakeHelper.stopped += 1


class FakeUi:
    """script: dump() 호출마다 소비되는 (rows, tab[, bottom]) 목록"""
    script = []
    taps = 0
    dumps = 0

    def __init__(self, adb, display):
        pass

    def dump(self):
        FakeUi.dumps += 1
        item = FakeUi.script.pop(0) if len(FakeUi.script) > 1 else FakeUi.script[0]
        if item == "FOREIGN":
            raise km.ForeignScreen("com.samsung.android.lool")
        return xml(*item)

    def tap_node(self, node, root):
        FakeUi.taps += 1

    def back(self):
        pass


RealUi = km.Ui
km.Helper, km.Ui = FakeHelper, FakeUi
km.ensure_connected = lambda cfg: None
km.foreground_packages_display0 = lambda adb: set()
n = km.Notifier(CFG)
ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)


# 1) 정상: 안읽음 1개 -> 열고 -> 사라짐
FakeUi.script, FakeUi.taps = [([("A", "3")], True), ([("A", "3")], True), ([("A", "3")], True),
                              ([("A", None)], True), ([("A", None)], True)], 0
r = km.run_cycle(CFG, None, "walk", n)
check("정상 사이클: 1개 열고 종료", r == 1 and FakeUi.taps == 2)  # 탭: 폴더탭 + 채널

# 2) stuck: 열어도 안읽음이 안 풀림
received.clear()
FakeUi.script, FakeUi.taps = [([("A", "3")], True)], 0
r = km.run_cycle(CFG, None, "walk", n)
check("stuck: 1번만 열고 중단", r == 1 and FakeUi.taps == 2)
check("stuck: 알림 전송", len(received) == 1 and "A" in received[0][1]["content"] and received[0][0] == "kakao-mute/1.0")
FakeUi.script, FakeUi.taps = [([("A", "3")], True)], 0
km.run_cycle(CFG, None, "walk", n)
check("stuck: 같은 채널 재알림은 쓰로틀", len(received) == 1)

# 3) 열기 직전 가드
calls = {"n": 0}


def fg(adb):
    calls["n"] += 1
    return set() if calls["n"] == 1 else {"com.kakao.talk"}


km.foreground_packages_display0 = fg
FakeUi.script, FakeUi.taps = [([("A", "3")], True)], 0
r = km.run_cycle(CFG, None, "walk", n)
check("열기 직전 가드: 채널을 열지 않고 중단, 건너뜀(None)으로 처리", r is None and FakeUi.taps == 1)
km.foreground_packages_display0 = lambda adb: {"com.kakao.talk"}
check("시작 가드: 건너뜀(None)", km.run_cycle(CFG, None, "walk", n) is None)
km.foreground_packages_display0 = lambda adb: set()

# 3-2) 가드 파서: 실제 폰의 dumpsys activity activities 구조 (디스플레이별 구역 + 끝의 전역 ResumedActivity)
DUMP_KAKAO_ON_VIRTUAL = """ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #10 (activities from top to bottom):
  Display #10 info
    topResumedActivity=ActivityRecord{1 u0 com.kakao.talk/.activity.main.MainActivity t64527}
  Resumed activities in task display areas (from top to bottom):
    Resumed: ActivityRecord{1 u0 com.kakao.talk/.activity.main.MainActivity t64527}

Display #0 (activities from top to bottom):
  Display #0 info
      topResumedActivity=ActivityRecord{2 u0 com.sec.android.app.launcher/.activities.LauncherActivity t64507}
  Resumed activities in task display areas (from top to bottom):
    Resumed: ActivityRecord{2 u0 com.sec.android.app.launcher/.activities.LauncherActivity t64507}

  ResumedActivity: ActivityRecord{1 u0 com.kakao.talk/.activity.main.MainActivity t64527}

ActivityTaskSupervisor state:
  topResumedActivity=ActivityRecord{1 u0 com.kakao.talk/.activity.main.MainActivity t64527}
"""
DUMP_KAKAO_ON_PHYSICAL = """ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #0 (activities from top to bottom):
  Display #0 info
    topResumedActivity=ActivityRecord{3 u0 com.kakao.talk/.activity.main.MainActivity t64422}
  Resumed activities in task display areas (from top to bottom):
    Resumed: ActivityRecord{3 u0 com.kakao.talk/.activity.main.MainActivity t64422}

  ResumedActivity: ActivityRecord{3 u0 com.kakao.talk/.activity.main.MainActivity t64422}

ActivityTaskSupervisor state:
"""
check("가드 파서: 카톡이 가상 디스플레이에 있고 물리 화면은 런처면 카톡으로 보지 않음 (전역 ResumedActivity 무시)",
      km.parse_display0_resumed(DUMP_KAKAO_ON_VIRTUAL) == {"com.sec.android.app.launcher"})
check("가드 파서: 물리 화면에서 카톡을 쓰면 카톡으로 봄", "com.kakao.talk" in km.parse_display0_resumed(DUMP_KAKAO_ON_PHYSICAL))
check("가드 파서: 전역 구역(ActivityTaskSupervisor state)의 줄은 어느 디스플레이에도 귀속하지 않음",
      "com.kakao.talk" not in km.parse_display0_resumed(DUMP_KAKAO_ON_VIRTUAL))

# 3-3) 폴더 탭 도달: 평소엔 덤프 1번, 실패 경로에서만 제한된 횟수로 더 뜬다 (덤프는 폰의 비용이 큼)
# 사이클 전체 덤프 수 = 탭 도달 + 탭 선택 확인 1 + 안읽음 확인 1 (+ 열었을 때 복귀 확인)
FakeUi.script, FakeUi.taps, FakeUi.dumps = [([("A", None)], True)], 0, 0
r = km.run_cycle(CFG, None, "walk", n)
check("평소 경로: 탭이 처음부터 보이면 탭 도달에 덤프 1번만 (사이클 총 3번)", r == 0 and FakeUi.dumps == 3 and FakeUi.taps == 1)

FakeUi.script, FakeUi.taps, FakeUi.dumps = [([], False, False), ([("A", None)], True)], 0, 0
r = km.run_cycle(CFG, None, "walk", n)
check("로딩 중(하단 탭도 없음): 기다렸다가 덤프 1번 더 -> 진행 (사이클 총 4번)", r == 0 and FakeUi.dumps == 4)
check("로딩 중에는 채팅 탭을 누르지 않고 폴더 탭만 누름", FakeUi.taps == 1)

FakeUi.script, FakeUi.taps, FakeUi.dumps = [([], False, True), ([("A", None)], True)], 0, 0
r = km.run_cycle(CFG, None, "walk", n)
check("다른 하단 탭에 있으면 채팅 탭을 눌러 이동한 뒤 진행", r == 0 and FakeUi.taps == 2 and FakeUi.dumps == 4)

import os
_dump_path = CFG["failure_dump"]
if os.path.exists(_dump_path):
    os.remove(_dump_path)
FakeUi.script, FakeUi.taps, FakeUi.dumps = [([("FRIEND", "5")], False, True)], 0, 0
try:
    km.run_cycle(CFG, None, "walk", n)
    check("끝내 못 찾으면 실패", False)
except km.CycleError as e:
    check("끝내 못 찾으면 CycleError, 메시지에 덤프 경로 포함", "못 찾음" in str(e) and "_failure_dump_test.xml" in str(e))
check("실패 경로의 덤프 수는 상한(1 + tab_attempts)", FakeUi.dumps == 1 + CFG["tab_attempts"])
check("실패 경로: 채팅 탭 이동 시도는 tab_attempts 번, 친구 목록은 열지 않음", FakeUi.taps == CFG["tab_attempts"])
check("실패 시 마지막 UI 덤프를 파일로 저장", os.path.exists(_dump_path) and "FRIEND" in open(_dump_path, encoding="utf-8").read())
if os.path.exists(_dump_path):
    os.remove(_dump_path)

# 3-4) 물리 화면의 다른 앱이 덤프로 돌아오는 경우 (사용자가 배터리/Tailscale 앱을 보는 중): 실패가 아닌 건너뜀
def _tree(packages):
    nodes = "".join('<node package="%s" text="x" bounds="[0,0][10,10]"/>' % p for p in packages)
    return ET.fromstring("<hierarchy>%s</hierarchy>" % nodes)


check("외부 앱 감지: 카톡 + 시스템 UI(내비게이션 바)는 정상", km.foreign_packages(_tree(["com.kakao.talk", "com.android.systemui"])) is None)
check("외부 앱 감지: 시스템 UI/런처만 있으면 로딩 중으로 보고 정상 취급",
      km.foreign_packages(_tree(["com.android.systemui"])) is None
      and km.foreign_packages(_tree(["com.sec.android.app.launcher", "com.android.systemui"])) is None)
check("외부 앱 감지: 노드가 없으면(로딩 중) 정상 취급", km.foreign_packages(_tree([])) is None)
check("외부 앱 감지: 배터리 앱만 있으면 외부 앱 (시스템 UI 가 섞여도)",
      km.foreign_packages(_tree(["com.samsung.android.lool", "com.android.systemui"])) == ["com.samsung.android.lool"])
check("외부 앱 감지: Tailscale 앱", km.foreign_packages(_tree(["com.tailscale.ipn"])) == ["com.tailscale.ipn"])
check("외부 앱 감지: 카톡이 있으면 다른 앱이 섞여도 카톡 화면으로 봄",
      km.foreign_packages(_tree(["com.kakao.talk", "com.samsung.android.lool"])) is None)


class _DumpAdb:
    def __init__(self, xml_text):
        self.xml_text = xml_text

    def shell(self, cmd, **kw):
        return ""

    def run(self, *args, **kw):
        return self.xml_text


_battery = '<hierarchy><node package="com.samsung.android.lool" text="배터리" bounds="[0,0][1,1]"/></hierarchy>'
_kakao = '<hierarchy><node package="com.kakao.talk" text="채팅" bounds="[0,0][1,1]"/></hierarchy>'
try:
    RealUi(_DumpAdb(_battery), 7).dump()
    check("실제 Ui.dump: 외부 앱 화면이면 ForeignScreen", False)
except km.ForeignScreen as e:
    check("실제 Ui.dump: 외부 앱 화면이면 ForeignScreen (패키지 포함)", "com.samsung.android.lool" in str(e))
_ok_root = RealUi(_DumpAdb(_kakao), 7).dump()
check("실제 Ui.dump: 카톡 화면이면 정상 반환 (파싱된 트리)", _ok_root is not None and _ok_root.find(".//node").attrib["package"] == "com.kakao.talk")

# 첫 덤프부터 외부 앱 -> 건너뜀, 아무것도 누르지 않고 헬퍼 정리
before_stop = FakeHelper.stopped
FakeUi.script, FakeUi.taps, FakeUi.dumps = ["FOREIGN"], 0, 0
r = km.run_cycle(CFG, None, "walk", n)
check("첫 덤프가 외부 앱: 실패가 아니라 건너뜀(None)", r is None)
check("외부 앱: 아무것도 누르지 않음, 덤프도 1번뿐 (재시도/대기 없음)", FakeUi.taps == 0 and FakeUi.dumps == 1)
check("외부 앱: 헬퍼(가상 디스플레이)는 정리됨", FakeHelper.stopped == before_stop + 1)

# 사이클 도중(탭을 누른 뒤)에 외부 앱이 섞임 -> 건너뜀
FakeUi.script, FakeUi.taps, FakeUi.dumps = [([("A", "3")], True), "FOREIGN"], 0, 0
r = km.run_cycle(CFG, None, "walk", n)
check("탭을 누른 뒤 외부 앱이 섞이면 건너뜀(None), 채널은 열지 않음", r is None and FakeUi.taps == 1)

# 채널을 연 뒤(복귀 확인 덤프)에 외부 앱이 섞임 -> 건너뜀
FakeUi.script, FakeUi.taps, FakeUi.dumps = [([("A", "3")], True), ([("A", "3")], True), ([("A", "3")], True), "FOREIGN"], 0, 0
r = km.run_cycle(CFG, None, "walk", n)
check("채널을 연 뒤 외부 앱이 섞이면 건너뜀(None)", r is None and FakeUi.taps == 2)

# 4) 폴더 탭 없음 -> CycleError, 헬퍼는 항상 정리
before = FakeHelper.stopped
FakeUi.script = [([], False)]
try:
    km.run_cycle(CFG, None, "walk", n)
    check("탭 없음 -> 예외", False)
except km.CycleError:
    check("탭 없음 -> CycleError", True)
check("예외에도 헬퍼 정리", FakeHelper.stopped == before + 1)

# 4-2) 탭을 눌렀지만 선택되지 않음 -> 다른 탭 목록(친구 채팅 포함)을 처리하면 안 됨
FakeUi.script, FakeUi.taps = [([("FRIEND", "5")], "off")], 0
try:
    km.run_cycle(CFG, None, "walk", n)
    check("탭 선택 실패 -> 예외", False)
except km.CycleError as e:
    check("탭 선택 실패 -> CycleError", "선택되지 않음" in str(e))
check("탭 선택 실패: 탭만 2번 누르고 채널은 열지 않음", FakeUi.taps == 2)

# 5) 알림 미설정이면 조용히 로그만
os_env = km.os.environ.pop("KMUTE_DISCORD_WEBHOOK", None)
check("URL 없음 -> False", km.Notifier(dict(CFG, discord_webhook_url="")).send("x") is False)
# 6) 전송 실패해도 예외 안 남
check("전송 실패 -> False, 예외 없음", km.Notifier(dict(CFG, discord_webhook_url="http://127.0.0.1:1/x")).send("x") is False)

# 7) 잠금 판별 파싱
class FakeAdb:
    def __init__(self, trust="", window=""):
        self.trust, self.window = trust, window

    def shell(self, cmd, **kw):
        return self.trust if "trust" in cmd else self.window


TRUST_UNLOCKED = 'User "owner" (id=0, flags=0x4c13) (current): trustState=UNTRUSTED, deviceLocked=0, strongAuthRequired=0x0'
TRUST_LOCKED = 'User "owner" (id=0, flags=0x4c13) (current): trustState=UNTRUSTED, deviceLocked=1, strongAuthRequired=0x0'
check("잠금 판별: 풀림", km.device_locked(FakeAdb(TRUST_UNLOCKED, "    isKeyguardShowing=false")) is False)
check("잠금 판별: trust 잠김", km.device_locked(FakeAdb(TRUST_LOCKED, "    isKeyguardShowing=false")) is True)
check("잠금 판별: 키가드만 잠김", km.device_locked(FakeAdb(TRUST_UNLOCKED, "    isKeyguardShowing=true")) is True)
check("잠금 판별: 값을 못 읽으면 None", km.device_locked(FakeAdb("", "")) is None)
check("잠금 판별: 한쪽만 읽혀도 판단", km.device_locked(FakeAdb("", "    isKeyguardShowing=false")) is False)

if os.path.exists(CFG["failure_dump"]):
    os.remove(CFG["failure_dump"])
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
