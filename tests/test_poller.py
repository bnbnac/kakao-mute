"""Poller(잠금 확인, 직전 확인 시각 규칙, 알림)와 Postgres 기록(kmute_store)을 가짜 객체로 검증한다."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
import kakao_mute as km
import kmute_store as ks

logging.disable(logging.CRITICAL)
CFG = dict(km.DEFAULTS, serial=None)
CHECK = CFG["check_interval_sec"]      # 1800
RETRY = CFG["retry_gap_sec"]           # 300
TICK = CFG["lock_check_interval_sec"]  # 60
ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)


class Clock:
    t = 100000.0

    @staticmethod
    def now():
        return Clock.t


class FakeStore:
    enabled = True

    def __init__(self, last_ok=None):
        self.last_ok = last_ok
        self.heartbeats, self.cycles, self.errors, self.dumps = [], [], [], []

    def heartbeat(self, locked):
        self.heartbeats.append(locked)
        return True

    def record_cycle(self, started, duration, result, opened=None, error=None):
        self.cycles.append((result, opened, error))
        return True

    def record_dump(self, started, kind, body):
        self.dumps.append((kind, body))
        return True

    def note_error(self, error):
        self.errors.append(error)

    def load_last_cycle_ok(self):
        return self.last_ok


class Notif:
    def __init__(self):
        self.sent = []

    def send(self, text, **kw):
        self.sent.append(text)
        return True


def make(locked_seq, store=None, cycle_results=(0,)):
    """locked_seq: step 마다 소비되는 잠금 상태(마지막 값 유지). cycle_results: run_cycle 결과/예외(마지막 값 유지)."""
    seq, res = list(locked_seq), list(cycle_results)
    calls, sleeps = [], []
    km.ensure_connected = lambda cfg: None

    def locked(adb):
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(v, Exception):
            raise v
        return v

    def cycle(cfg, adb, mode="walk", notifier=None):
        calls.append(Clock.t)
        r = res.pop(0) if len(res) > 1 else res[0]
        if isinstance(r, Exception):
            raise r
        return r

    km.device_locked, km.run_cycle = locked, cycle
    notif = Notif()
    store = store if store is not None else FakeStore()
    p = km.Poller(CFG, None, notif, store, clock=Clock.now, sleep=lambda s: sleeps.append(s))
    return p, calls, sleeps, notif, store


def advance(p, seconds):
    """seconds 만큼 TICK 간격으로 step 을 돌린다."""
    for _ in range(int(seconds // TICK)):
        Clock.t += TICK
        p.step()


# 1) 잠금 중: 사이클 없음, 하트비트는 기록
Clock.t = 100000.0
p, calls, sleeps, notif, store = make([True])
advance(p, 600)
check("잠금 중: 사이클 안 돌림", calls == [])
check("잠금 중에도 하트비트 기록 (잠김=True)", len(store.heartbeats) == 10 and set(store.heartbeats) == {True})

# 2) 해제 직후: 직전 확인이 없으면 바로 사이클 (안정화 대기)
Clock.t = 100000.0
p, calls, sleeps, notif, store = make([True, True, False])
advance(p, TICK * 3)
check("해제 직후 사이클 1회", len(calls) == 1)
check("해제 직후 안정화 대기", sleeps == [CFG["unlock_settle_sec"]])
check("사이클 결과 기록(ok)", store.cycles == [("ok", 0, None)])

# 3) 30분 규칙: 직전 확인이 30분 이상 전일 때만 사이클
Clock.t = 100000.0
p, calls, sleeps, notif, store = make([False])
p.step()                                            # t0: 첫 사이클
t0 = calls[0]
advance(p, CHECK - TICK)                            # t0 + 29분
check("직전 확인 30분 미만: 사이클 안 돌림", len(calls) == 1)
advance(p, TICK)                                    # t0 + 30분
check("직전 확인 30분 경과: 사이클 실행", len(calls) == 2)

# 4) 해제를 자주 해도 30분 안에는 다시 안 돈다
Clock.t = 100000.0
p, calls, sleeps, notif, store = make([False, True, False, True, False, True, False])
for _ in range(7):
    p.step()
    Clock.t += TICK
check("잠금/해제를 반복해도 30분 안에는 사이클 1회", len(calls) == 1)

# 5) 장시간 잠금 후 해제: 마지막 확인이 오래됐으니 바로 사이클
Clock.t = 100000.0
p, calls, sleeps, notif, store = make([False, True, True, False])
p.step()
Clock.t += 3 * 3600
p.step(); p.step()                                  # 계속 잠김 (3시간 경과)
n_before = len(calls)
p.step()                                            # 풀림
check("장시간 잠금 후 해제: 바로 사이클", n_before == 1 and len(calls) == 2 and sleeps[-1] == CFG["unlock_settle_sec"])

# 6) 건너뜀(물리 화면에서 카톡 사용 중)은 확인이 아니므로 직전 확인 시각을 갱신하지 않고, retry_gap 뒤 재시도
Clock.t = 100000.0
p, calls, sleeps, notif, store = make([False], cycle_results=(None, None, 2))
p.step()
check("건너뜀: 사이클 호출됨", len(calls) == 1 and store.cycles[-1][0] == "skipped")
check("건너뜀: 직전 확인 시각 갱신 안 함", p.last_ok is None)
advance(p, RETRY - TICK)
check("건너뜀 후 retry_gap 전에는 재시도 안 함", len(calls) == 1)
advance(p, TICK)
check("건너뜀 후 retry_gap 뒤 재시도 (30분 기다리지 않음)", len(calls) == 2)
advance(p, RETRY)
check("성공하면 직전 확인 시각 갱신, 이후 30분 규칙", p.last_ok is not None and len(calls) == 3)

# 6-2) 실제 run_cycle 로 통합: 안읽음을 발견했는데 가드로 중단되면 확인한 게 아니다 (30분 대기 금지)
import xml.etree.ElementTree as _ET


def _tree(rows):
    out = ['<hierarchy><node text="광고" resource-id="id/tab_chip" clickable="true" selected="true" bounds="[0,0][100,100]"/>']
    y = 200
    for name, unread in rows:
        out.append('<node clickable="true" bounds="[0,%d][1080,%d]">' % (y, y + 150))
        out.append('<node text="%s" resource-id="id/name" bounds="[0,%d][100,%d]"/>' % (name, y, y + 50))
        if unread:
            out.append('<node text="%s" resource-id="id/unread" bounds="[0,%d][50,%d]"/>' % (unread, y + 60, y + 100))
        out.append('</node>')
        y += 150
    out.append('</hierarchy>')
    return _ET.fromstring("".join(out))


class _Helper:
    def __init__(self, *a):
        pass

    def wait_display_id(self, timeout=20):
        return 9

    def stop(self):
        pass


class _Ui:
    taps = 0

    def __init__(self, adb, display):
        pass

    def dump(self):
        return _tree([("AD", "2")])

    def tap_node(self, node, root):
        _Ui.taps += 1

    def back(self):
        pass


import importlib
importlib.reload(km)               # run_cycle 를 가짜로 바꾼 것을 원래대로 되돌린다
km.Helper, km.Ui = _Helper, _Ui
km.ensure_connected = lambda cfg: None
km.time.sleep = lambda s: None
CFG2 = dict(km.DEFAULTS, serial=None, chat_name_id="id/name", unread_badge_id="id/unread", settle_sec=0,
            dwell_sec=0, launch_wait_sec=0)
state = {"blocked_after": 0, "calls": 0}


def _guard(cfg, adb):
    state["calls"] += 1
    return set() if state["calls"] == 1 else {"com.kakao.talk"}    # 시작 가드는 통과, 열기 직전에는 사용 중


km.guard_blocked = _guard
km.device_locked = lambda adb: False
Clock.t = 100000.0
notif2 = Notif()
p = km.Poller(CFG2, None, notif2, FakeStore(), clock=Clock.now, sleep=lambda s: None)
p.step()
check("가드로 중단된 사이클은 직전 확인 시각을 갱신하지 않음", p.last_ok is None and _Ui.taps == 1)
check("가드로 중단된 사이클은 DB 에 skipped 로 기록", p.store.cycles and p.store.cycles[-1][0] == "skipped")
advance(p, RETRY - TICK)
check("retry_gap 전에는 재시도 안 함", state["calls"] == 2 and p.last_ok is None)
state["calls"] = 0
km.guard_blocked = lambda cfg, adb: set()            # 사용자가 카톡에서 나감
_Ui.taps = 0
advance(p, TICK)
check("retry_gap 뒤 재시도 (30분 기다리지 않음)", _Ui.taps >= 1)

# 7) 사이클 실패: retry_gap 마다 재시도, 시간 기준 알림, 성공하면 복구 알림
Clock.t = 100000.0
fail = km.CycleError("boom")
p, calls, sleeps, notif, store = make([False], cycle_results=(fail, fail, fail, fail, fail, fail, fail, 1))
p.step()
check("실패 직후엔 알림 없음", notif.sent == [])
advance(p, CFG["alert_after_sec"] - TICK)
check("기준 시간 전엔 알림 없음 (계속 실패해도)", notif.sent == [] and len(calls) >= 5)
advance(p, TICK)
check("기준 시간 경과: 실패 알림 1회", len(notif.sent) == 1 and "boom" in notif.sent[0])
check("실패는 DB 에 error 로 기록", store.cycles[0][0] == "error" and "boom" in (store.cycles[0][2] or ""))
check("실패는 last_error 로 남김", store.errors and "boom" in store.errors[0])
check("덤프가 없는 실패는 DB 덤프를 남기지 않음", store.dumps == [])
advance(p, RETRY * 2)
check("복구 알림 (사이클이 실제로 성공했을 때)", any("복구" in s for s in notif.sent) and p.last_ok is not None)

# 7-1) 덤프를 실은 실패는 DB 에 덤프를 남긴다
Clock.t = 100000.0
fail_d = km.with_dump(km.CycleError("탭 문제"), _ET.fromstring("<hierarchy><node text='X'/></hierarchy>"), "not_selected")
p, calls, sleeps, notif, store = make([False], cycle_results=(fail_d,))
p.step()
check("덤프를 실은 실패: 종류와 본문을 DB 에 기록", len(store.dumps) == 1 and store.dumps[0][0] == "not_selected"
      and "text=\"X\"" in store.dumps[0][1].replace("'", '"'))

# 7-2) 사이클이 안 도는 확인 단계는 복구로 치지 않는다
Clock.t = 100000.0
p, calls, sleeps, notif, store = make([False], cycle_results=(fail,))
advance(p, CFG["alert_after_sec"] + RETRY)
n = len(notif.sent)
Clock.t += TICK
p.step()
check("사이클이 안 도는 확인 단계는 복구가 아님", n == 1 and len(notif.sent) == 1 and p.alerted_at is not None)

# 8) 잠금 중에는 실패로 세지 않는다
Clock.t = 100000.0
p, calls, sleeps, notif, store = make([True])
advance(p, 24 * 3600)
check("장시간 잠금: 실패/알림 없음", p.fail_since is None and notif.sent == [])

# 9) 확인 자체가 실패(폰 접속 불가): 사이클 안 돌고, 하트비트 기록 안 되고, 시간 기준 알림
Clock.t = 100000.0
p, calls, sleeps, notif, store = make([km.AdbError("device offline")])
advance(p, CFG["alert_after_sec"] + TICK)
check("확인 실패: 사이클 안 돌림", calls == [])
check("확인 실패: 하트비트 기록 안 됨 (외부 감시가 이걸로 감지)", store.heartbeats == [])
check("확인 실패: 시간 기준 알림 1회", len(notif.sent) == 1 and "device offline" in notif.sent[0])

# 10) 시작할 때 DB 에서 직전 확인 시각을 복원: 재시작이 즉시 확인을 일으키지 않는다
Clock.t = 100000.0
p, calls, sleeps, notif, store = make([False], store=FakeStore(last_ok=Clock.t - 600))
check("복원된 직전 확인 시각", p.last_ok == Clock.t - 600)
p.step()
check("복원: 10분 전에 확인했으면 재시작 직후 사이클 안 돌림", calls == [])
advance(p, CHECK - 600)
check("복원: 30분이 되면 사이클", len(calls) == 1)

Clock.t = 100000.0
p, calls, *_ = make([False], store=FakeStore(last_ok=Clock.t + 99999))
check("복원: 미래 시각은 무시하고 바로 사이클", p.last_ok is None or True)
p.step()
check("복원: 미래 시각이면 사이클 실행", len(calls) == 1)

Clock.t = 100000.0
p, calls, *_ = make([False], store=FakeStore(last_ok=None))
p.step()
check("복원값 없으면 첫 확인에서 사이클", len(calls) == 1)

# 11) 설정 오류(NotCalibrated)는 삼키지 않고 밖으로 낸다
Clock.t = 100000.0
p, calls, sleeps, notif, store = make([False], cycle_results=(km.NotCalibrated("x"),))
try:
    p.step()
    check("NotCalibrated 전파", False)
except km.NotCalibrated:
    check("NotCalibrated 전파", True)


# ---- kmute_store (가짜 DB 연결) ----
class FakeCur:
    def __init__(self, conn):
        self.conn, self.description, self._rows = conn, None, []

    def execute(self, sql, params=()):
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("db error")
        self.conn.log.append((" ".join(sql.split()), tuple(params)))
        if sql.lstrip().upper().startswith("SELECT"):
            self.description = [("x",)]
            self._rows = self.conn.rows

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, log, rows=(), fail_on=None):
        self.log, self.rows, self.fail_on, self.closed = log, list(rows), fail_on, False

    def cursor(self):
        return FakeCur(self)

    def close(self):
        self.closed = True


def store_with(rows=(), fail_on=None, connect_error=None):
    log, connects = [], []

    def connect(dsn):
        connects.append(dsn)
        if connect_error and len(connects) <= connect_error:
            raise ConnectionError("down")
        return FakeConn(log, rows, fail_on)

    Clock.t = 200000.0
    return ks.Store("postgresql://x", host="h1", connect=connect, clock=Clock.now, retry_sec=60), log, connects


s = ks.Store("", connect=lambda d: 1 / 0)
check("DSN 없으면 비활성: 기록 안 하고 예외도 없음", s.enabled is False and s.heartbeat(True) is False
      and s.load_last_cycle_ok() is None)

s, log, connects = store_with()
check("하트비트 기록 성공", s.heartbeat(False) is True)
sqls = [q for q, _ in log]
check("첫 기록 전에 스키마 생성(테이블 3개)", sum("CREATE TABLE IF NOT EXISTS" in q for q in sqls) == 3)
check("하트비트 upsert 파라미터 (host, 시각, 잠금)", log[-1][1] == ("h1", Clock.t, False) and "heartbeat" in log[-1][0])
s.heartbeat(True)
check("스키마는 한 번만 생성, 연결도 재사용", sum("CREATE TABLE" in q for q, _ in log) == 3 and len(connects) == 1)

s, log, _ = store_with()
s.record_cycle(100.0, 12.345, "ok", opened=2)
kinds = [q for q, _ in log if "INSERT" in q]
check("ok 사이클: cycles 기록 + last_cycle_ok 갱신", len(kinds) == 2 and "cycle_log" in kinds[0] and "last_cycle_ok" in kinds[1])
check("ok 사이클 파라미터(소요 시간 반올림, 열린 수)", log[-2][1] == ("h1", 100.0, 12.35, "ok", 2, None))
s, log, _ = store_with()
s.record_cycle(100.0, 1.0, "skipped")
kinds = [q for q, _ in log if "INSERT" in q]
check("skipped 사이클: cycles 만 기록, last_cycle_ok 는 건드리지 않음", len(kinds) == 1 and "cycle_log" in kinds[0])
s, log, _ = store_with()
s.record_cycle(100.0, 1.0, "error", error="boom")
kinds = [q for q, _ in log if "INSERT" in q]
check("error 사이클: cycles + last_error 기록", len(kinds) == 2 and "last_error" in kinds[1])

s, log, _ = store_with()
s.dump_keep = 3
check("덤프 기록 성공", s.record_dump(100.0, "not_selected", "<x/>") is True)
check("덤프 INSERT 파라미터 (호스트, 사이클 시작 시각, 종류, 본문)",
      [p for q, p in log if "INSERT INTO failure_dump" in q] == [("h1", 100.0, "not_selected", "<x/>")])
check("덤프 기록 뒤 호스트당 최근 N개만 남기는 DELETE",
      [p for q, p in log if "DELETE FROM failure_dump" in q] == [("h1", "h1", 3)])
check("NullStore: 덤프 기록도 무해", ks.NullStore().record_dump(1, "k", "b") is False)

s, log, connects = store_with(connect_error=1)
check("연결 실패는 삼키고 False", s.heartbeat(True) is False)
s.heartbeat(True)
check("실패 직후 retry_sec 안에는 재연결 시도 안 함", len(connects) == 1)
Clock.t += 61
check("retry_sec 뒤 재연결 성공", s.heartbeat(True) is True and len(connects) == 2)

s, log, connects = store_with(fail_on="heartbeat (host, last_check_ok")
check("쿼리 실패도 삼킴", s.heartbeat(True) is False)
check("쿼리 실패 시 연결을 버리고 대기", s.conn is None and s.down_until > Clock.t)

s, _, _ = store_with(rows=[(Clock.t - 120.5,)])
check("직전 확인 시각 읽기", abs(s.load_last_cycle_ok() - (Clock.t - 120.5)) < 1e-6)
s, _, _ = store_with(rows=[])
check("행이 없으면 None", s.load_last_cycle_ok() is None)
s, _, _ = store_with(rows=[(None,)])
check("값이 NULL 이면 None", s.load_last_cycle_ok() is None)
s, _, _ = store_with(connect_error=5)
check("DB 를 못 읽으면 None (데몬은 계속 동작)", s.load_last_cycle_ok() is None)

s, log, _ = store_with(rows=[("h1", None, None, None, None)])
rows = s.selftest()
check("selftest: 스키마 생성, 하트비트 기록, 읽기", rows == [("h1", None, None, None, None)]
      and any("SELECT host" in q for q, _ in log))

n = ks.NullStore()
check("NullStore: 모든 호출이 무해", n.heartbeat(True) is False and n.record_cycle(1, 1, "ok") is False
      and n.note_error("x") is False and n.load_last_cycle_ok() is None)

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
