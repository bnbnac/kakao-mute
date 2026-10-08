"""실제 Postgres 에 대한 통합 테스트. KMUTE_TEST_DSN 이 없으면 건너뛴다.

    KMUTE_TEST_DSN='postgresql://USER:PASSWORD@HOST:PORT/DB' python tests/test_postgres_integration.py

테스트용 호스트 이름(it-<시각>)으로만 기록하고 끝나면 그 행만 지운다. 일회용 DB 에서 돌리는 것을 권한다.
"""
import logging
import os
import sys
import time
from pathlib import Path

DSN = os.environ.get("KMUTE_TEST_DSN")
if not DSN:
    print("SKIP: KMUTE_TEST_DSN 미설정 (실제 Postgres 필요)")
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
import kakao_mute as km
import kmute_store as ks

logging.disable(logging.CRITICAL)
HOST = "it-%d" % int(time.time())
HOST3 = HOST + "-dump"
ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)


def query(sql, params=()):
    conn = ks.default_connect(DSN)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
        cur.close()
        return rows
    finally:
        conn.close()


def hb():
    return query("SELECT last_check_ok, last_cycle_ok, locked, last_error FROM heartbeat WHERE host=%s", (HOST,))


def cycles():
    return query("SELECT result, opened, error, duration_sec FROM cycle_log WHERE host=%s ORDER BY id", (HOST,))


try:
    s = ks.Store(DSN, host=HOST)
    check("스키마 생성 + 하트비트 기록", s.heartbeat(True) is True)
    row = hb()[0]
    check("하트비트 행: last_check_ok 있음, 잠김=True", row[0] is not None and row[2] is True and row[1] is None)
    s.heartbeat(False)
    check("하트비트 갱신: 잠김=False 로 upsert (행은 1개)", len(hb()) == 1 and hb()[0][2] is False)

    s.record_cycle(time.time(), 12.345, "ok", opened=2)
    row = hb()[0]
    check("ok 사이클: last_cycle_ok 갱신", row[1] is not None and row[3] is None)
    check("ok 사이클 로그", cycles()[-1][:2] == ("ok", 2) and abs(cycles()[-1][3] - 12.35) < 0.01)

    s.record_cycle(time.time(), 1.0, "error", error="boom")
    row = hb()[0]
    check("error 사이클: last_error 기록, last_cycle_ok 는 유지", row[3] == "boom" and row[1] is not None)
    before = hb()[0][1]
    s.record_cycle(time.time(), 1.0, "skipped")
    check("skipped 사이클: last_cycle_ok/last_error 를 건드리지 않음", hb()[0][1] == before and hb()[0][3] == "boom")
    s.record_cycle(time.time(), 1.0, "ok", opened=0)
    check("다시 ok: last_error 가 지워짐", hb()[0][3] is None)

    s.note_error("device offline")
    check("note_error: last_error 기록", hb()[0][3] == "device offline")

    s2 = ks.Store(DSN, host=HOST)
    ts = s2.load_last_cycle_ok()
    check("새 Store(=재시작 후)에서 직전 확인 시각 복원", ts is not None and abs(ts - time.time()) < 30)
    check("없는 호스트는 None", ks.Store(DSN, host=HOST + "-none").load_last_cycle_ok() is None)

    # Poller: 재시작해도 30분 규칙이 유지된다 (실제 DB 사용, 폰은 가짜)
    cfg = dict(km.DEFAULTS, serial=None)
    cycle_calls = []
    km.ensure_connected = lambda c: None
    km.device_locked = lambda adb: False
    km.run_cycle = lambda c, adb, mode="walk", notifier=None: (cycle_calls.append(1), 0)[1]

    class Notif:
        def send(self, *a, **kw):
            return True

    HOST2 = HOST + "-poller"
    p1 = km.Poller(cfg, None, Notif(), ks.Store(DSN, host=HOST2), sleep=lambda s_: None)
    p1.step()
    check("Poller#1: 첫 단계에서 사이클 실행", len(cycle_calls) == 1 and p1.last_ok is not None)
    p2 = km.Poller(cfg, None, Notif(), ks.Store(DSN, host=HOST2), sleep=lambda s_: None)
    check("Poller#2(재시작): DB 에서 직전 확인 시각 복원", p2.last_ok is not None and abs(p2.last_ok - p1.last_ok) < 5)
    p2.step()
    check("재시작 직후에도 30분 규칙 유지: 사이클 안 돌림", len(cycle_calls) == 1)
    check("Poller 하트비트가 DB 에 기록됨", query("SELECT locked FROM heartbeat WHERE host=%s", (HOST2,))[0][0] is False)
    check("Poller 사이클 로그가 DB 에 기록됨", query("SELECT count(*) FROM cycle_log WHERE host=%s", (HOST2,))[0][0] == 1)

    # 실패 덤프: 호스트당 최근 N개만 남고, 같은 사이클의 cycle_log 와 started_at 으로 짝지어진다
    sd = ks.Store(DSN, host=HOST3, dump_keep=3)
    for i, kind in enumerate(["no_tab", "not_selected", "tab_vanished", "not_selected", "no_tab"]):
        sd.record_cycle(1000.0 + i, 1.0, "error", error="e%d" % i)
        sd.record_dump(1000.0 + i, kind, "<hierarchy n='%d' 한글='채팅'/>" % i)
    rows = query("SELECT kind, body FROM failure_dump WHERE host=%s ORDER BY id", (HOST3,))
    check("덤프 보관: 최근 3개만 남음", [r[0] for r in rows] == ["tab_vanished", "not_selected", "no_tab"])
    check("덤프 본문(한글 포함)이 그대로 저장됨", rows[0][1] == "<hierarchy n='2' 한글='채팅'/>")
    other = ks.Store(DSN, host=HOST3 + "-b", dump_keep=3)
    other.record_dump(2000.0, "no_tab", "<x/>")
    check("덤프 보관은 호스트별로 독립", query("SELECT count(*) FROM failure_dump WHERE host=%s", (HOST3,))[0][0] == 3)
    joined = query("SELECT c.error, d.kind FROM failure_dump d JOIN cycle_log c ON c.host = d.host AND c.started_at = d.started_at "
                   "WHERE d.host=%s ORDER BY d.id", (HOST3,))
    check("덤프와 cycle_log 가 started_at 으로 짝지어짐", joined == [("e2", "tab_vanished"), ("e3", "not_selected"), ("e4", "no_tab")])
finally:
    for h in (HOST, HOST + "-poller", HOST3, HOST3 + "-b"):
        query("DELETE FROM failure_dump WHERE host=%s", (h,))
        query("DELETE FROM cycle_log WHERE host=%s", (h,))
        query("DELETE FROM heartbeat WHERE host=%s", (h,))

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
