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
           discord_webhook_url=URL, launch_wait_sec=0, settle_sec=0, dwell_sec=0)


def xml(rows, tab=True):
    out = ['<hierarchy>']
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
    """script: dump() 호출마다 소비되는 (rows, tab) 목록"""
    script = []
    taps = 0

    def __init__(self, adb, display):
        pass

    def dump(self):
        rows, tab = FakeUi.script.pop(0) if len(FakeUi.script) > 1 else FakeUi.script[0]
        return xml(rows, tab)

    def tap_node(self, node, root):
        FakeUi.taps += 1

    def back(self):
        pass


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
check("열기 직전 가드: 채널을 열지 않고 중단", r == 0 and FakeUi.taps == 1)
km.foreground_packages_display0 = lambda adb: {"com.kakao.talk"}
check("시작 가드: 건너뜀(None)", km.run_cycle(CFG, None, "walk", n) is None)
km.foreground_packages_display0 = lambda adb: set()

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

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
