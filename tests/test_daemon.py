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


# 8) Poller: 잠금 중에는 사이클 없음, 해제 직후 1회, 해제 중 간격마다, 알림은 시간 기준
class Clock:
    t = 1000.0

    def now(self):
        return Clock.t


def make_poller(locked_seq, notif=None):
    clk = Clock()
    seq = list(locked_seq)
    cycles, sleeps = [], []
    km.device_locked = lambda adb: seq.pop(0) if len(seq) > 1 else seq[0]

    def fake_cycle(cfg, adb, mode="walk", notifier=None):
        cycles.append(Clock.t)
        if isinstance(cycles_fail[0], Exception):
            raise cycles_fail[0]

    cycles_fail = [None]
    km.run_cycle = fake_cycle
    notifier = notif or km.Notifier(dict(CFG, discord_webhook_url=""))
    p = km.Poller(CFG, None, notifier, clock=clk.now, sleep=lambda s: sleeps.append(s))
    return p, cycles, sleeps, cycles_fail


Clock.t = 1000.0
p, cycles, sleeps, _ = make_poller([True, True, False, False, False])
p.step(); Clock.t += 30
p.step(); Clock.t += 30
check("잠금 중: 사이클 안 돌림", cycles == [])
p.step()  # 풀림 직후
check("해제 직후: 안정화 대기 후 사이클 1회", len(cycles) == 1 and sleeps == [CFG["unlock_settle_sec"]])
Clock.t += 30
p.step()
check("해제 중 간격 전: 사이클 안 돌림", len(cycles) == 1)
Clock.t += CFG["unlocked_cycle_interval_sec"]
p.step()
check("해제 중 간격 경과: 사이클 1회 더", len(cycles) == 2)
check("step 반환값 = 잠금 확인 간격", p.step() == CFG["lock_check_interval_sec"])

# 8-1b) 해제를 자주 해도 최소 간격(min_cycle_gap_sec) 안에서는 사이클이 안 돈다
GAP = CFG["min_cycle_gap_sec"]
Clock.t = 3000.0
p, cycles, sleeps, _ = make_poller([False, True, False, False, False, False, False])
p.step()                                   # 시작 시 풀림 -> 사이클 1회
check("시작 시 사이클 1회", len(cycles) == 1)
Clock.t += 60
p.step()                                   # 잠김
Clock.t += 60
p.step()                                   # 다시 풀림 (직전 사이클 후 120초, 최소 간격 미만)
check("최소 간격 안의 재해제: 사이클 안 돎", len(cycles) == 1 and p.pending_unlock is True)
Clock.t += 60
p.step()
check("보류 중에도 간격 전에는 안 돎", len(cycles) == 1)
Clock.t += GAP
p.step()
check("최소 간격 경과: 보류된 해제 사이클 실행", len(cycles) == 2 and p.pending_unlock is False)
check("보류 사이클에도 안정화 대기", sleeps[-1] == CFG["unlock_settle_sec"])

# 8-1c) 보류 중 다시 잠기면 보류는 취소된다
Clock.t = 4000.0
p, cycles, sleeps, _ = make_poller([False, True, False, True, True])
p.step()
Clock.t += 60
p.step()                                   # 잠김
Clock.t += 60
p.step()                                   # 풀림 -> 보류
check("재해제로 보류 생김", p.pending_unlock is True)
Clock.t += 60
p.step()                                   # 다시 잠김
check("보류 중 잠기면 보류 취소", p.pending_unlock is False and len(cycles) == 1)

# 8-2) 처음부터 풀려 있으면 안정화 대기 없이 바로 사이클
Clock.t = 5000.0
p, cycles, sleeps, _ = make_poller([False])
p.step()
check("시작 시 풀림: 대기 없이 사이클", len(cycles) == 1 and sleeps == [])

# 8-3) 실패: 시간 기준 알림, 복구 알림
received.clear()
Clock.t = 9000.0
live = km.Notifier(dict(CFG, discord_webhook_url=URL))
p, cycles, sleeps, fail = make_poller([False], notif=live)
fail[0] = km.CycleError("boom")
CYC = CFG["unlocked_cycle_interval_sec"]
p.step()
check("실패 직후엔 알림 없음", len(received) == 0)
while Clock.t + CYC - 9000 < CFG["alert_after_sec"]:
    Clock.t += CYC
    p.step()
check("기준 시간 전엔 알림 없음 (사이클이 계속 실패해도)", len(received) == 0 and len(cycles) >= 3)
Clock.t += CYC
p.step()
check("기준 시간 경과: 실패 알림 1회", len(received) == 1 and "boom" in received[0][1]["content"])
Clock.t += CYC
p.step()
check("반복 간격 전엔 재알림 없음", len(received) == 1)
Clock.t += 30
p.step()
check("사이클이 안 도는 확인 단계는 복구로 치지 않음", len(received) == 1 and p.alerted_at is not None)
fail[0] = None
Clock.t += CYC
p.step()
check("사이클 성공 시 복구 알림", len(received) == 2 and "복구" in received[1][1]["content"])

# 8-4) 잠금 중에는 실패로 세지 않음
received.clear()
Clock.t = 20000.0
p, cycles, sleeps, fail = make_poller([True])
for _ in range(100):
    Clock.t += 60
    p.step()
check("장시간 잠금: 실패/알림 없음", p.fail_since is None and len(received) == 0)

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
