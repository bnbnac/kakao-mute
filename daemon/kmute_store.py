"""Postgres 기록 (선택 기능).

목적 (자세한 이유는 docs/design-notes.md):
1. 데몬 밖에서 데몬 생존을 감시할 수 있게(dead man's switch) 하트비트를 남긴다. 알림을 데몬이 스스로
   보내기 때문에, 데몬이나 서버가 죽으면 알림이 오지 않는다.
2. "직전 확인이 충분히 오래됐을 때만 확인한다"는 규칙의 상태(직전 확인 시각)를 저장해, 데몬이 재시작돼도
   규칙이 유지되게 한다 (재시작이나 크래시 루프가 매번 즉시 확인을 일으키지 않게).

- 감시 기준은 `heartbeat.last_check_ok` (데몬 생존 + 폰 접속). `last_cycle_ok` 는 통계용이다.
  폰이 잠겨 있으면 사이클이 안 도는 것이 정상이라 last_cycle_ok 로는 이상을 판단할 수 없다.
- 이 기능은 부가 기능이다. DSN 이 없거나, 드라이버가 없거나, DB 가 죽어도 데몬은 계속 동작해야 한다.
  모든 DB 오류는 삼키고 경고만 남기며, 실패하면 retry_sec 동안 재시도하지 않는다.
- 드라이버(psycopg 또는 psycopg2)는 DSN 이 설정됐을 때만 불러온다.
- `failure_dump` 에는 사이클이 폴더 탭 단계에서 실패했을 때의 UI 덤프(XML)를 호스트당 최근 N개 둔다.
  `started_at` 이 `cycle_log.started_at` 과 같아 시각으로 짝지을 수 있다. 채팅 이름이 들어 있다.
"""
import logging
import socket
import time

log = logging.getLogger("kmute.store")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS heartbeat (
        host text PRIMARY KEY,
        last_check_ok timestamptz,
        last_cycle_ok timestamptz,
        locked boolean,
        last_error text,
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS cycle_log (
        id bigserial PRIMARY KEY,
        host text NOT NULL,
        started_at timestamptz NOT NULL,
        duration_sec real,
        result text NOT NULL,
        opened integer,
        error text
    )""",
    """CREATE TABLE IF NOT EXISTS failure_dump (
        id bigserial PRIMARY KEY,
        host text NOT NULL,
        started_at timestamptz NOT NULL,
        kind text NOT NULL,
        body text NOT NULL
    )""",
]

SQL_DUMP = """INSERT INTO failure_dump (host, started_at, kind, body)
VALUES (%s, to_timestamp(%s), %s, %s)"""

SQL_DUMP_PRUNE = """DELETE FROM failure_dump WHERE host = %s AND id NOT IN
(SELECT id FROM failure_dump WHERE host = %s ORDER BY id DESC LIMIT %s)"""

SQL_HEARTBEAT = """INSERT INTO heartbeat (host, last_check_ok, locked, updated_at)
VALUES (%s, to_timestamp(%s), %s, now())
ON CONFLICT (host) DO UPDATE SET last_check_ok = EXCLUDED.last_check_ok,
    locked = EXCLUDED.locked, updated_at = now()"""

SQL_CYCLE = """INSERT INTO cycle_log (host, started_at, duration_sec, result, opened, error)
VALUES (%s, to_timestamp(%s), %s, %s, %s, %s)"""

SQL_CYCLE_OK = """INSERT INTO heartbeat (host, last_cycle_ok, last_error, updated_at)
VALUES (%s, to_timestamp(%s), NULL, now())
ON CONFLICT (host) DO UPDATE SET last_cycle_ok = EXCLUDED.last_cycle_ok,
    last_error = NULL, updated_at = now()"""

SQL_ERROR = """INSERT INTO heartbeat (host, last_error, updated_at)
VALUES (%s, %s, now())
ON CONFLICT (host) DO UPDATE SET last_error = EXCLUDED.last_error, updated_at = now()"""

SQL_SELECT = """SELECT host, last_check_ok, last_cycle_ok, locked, last_error
FROM heartbeat WHERE host = %s"""

SQL_LAST_OK = "SELECT extract(epoch FROM last_cycle_ok) FROM heartbeat WHERE host = %s"


def default_connect(dsn):
    try:
        import psycopg
        return psycopg.connect(dsn, autocommit=True, connect_timeout=5)
    except ImportError:
        pass
    try:
        import psycopg2
    except ImportError:
        raise ImportError("DB 기록에는 psycopg 또는 psycopg2 가 필요합니다 (pip install 'psycopg[binary]')")
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = True
    return conn


class Store:
    def __init__(self, dsn, host=None, connect=None, clock=time.time, retry_sec=60, dump_keep=5):
        self.dsn = dsn or ""
        self.host = host or socket.gethostname()
        self._connect = connect or default_connect
        self.clock = clock
        self.retry_sec = retry_sec
        self.dump_keep = max(1, dump_keep)
        self.conn = None
        self.schema_ok = False
        self.down_until = 0.0

    @property
    def enabled(self):
        return bool(self.dsn)

    def _exec(self, sql, params=()):
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
        finally:
            cur.close()

    def _guarded(self, fn, failed):
        """연결과 스키마를 준비한 뒤 fn 을 실행한다. 어떤 DB 오류도 밖으로 내보내지 않고 failed 를 돌려준다."""
        if not self.enabled or self.clock() < self.down_until:
            return failed
        try:
            if self.conn is None:
                self.conn = self._connect(self.dsn)
                self.schema_ok = False
            if not self.schema_ok:
                for stmt in SCHEMA:
                    self._exec(stmt)
                self.schema_ok = True
            return fn()
        except Exception as e:
            log.warning("DB 접근 실패 (%s) -> %d초 동안 재시도 안 함", type(e).__name__, self.retry_sec)
            self._drop_connection()
            self.down_until = self.clock() + self.retry_sec
            return failed

    def _run(self, *statements):
        """(sql, params) 들을 실행한다. 실패하면 False."""
        def go():
            for sql, params in statements:
                self._exec(sql, params)
            return True
        return self._guarded(go, False)

    def load_last_cycle_ok(self):
        """직전 확인(성공한 사이클) 시각을 epoch 초로 돌려준다. 없거나 DB 를 못 읽으면 None.

        '직전 확인이 충분히 오래됐을 때만 확인한다'는 규칙이 데몬 재시작에도 유지되도록, 시작할 때 복원한다.
        """
        def go():
            rows = self._exec(SQL_LAST_OK, (self.host,))
            return float(rows[0][0]) if rows and rows[0][0] is not None else None
        return self._guarded(go, None)

    def _drop_connection(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.conn = None

    def heartbeat(self, locked):
        """확인 성공 (데몬 생존 + 폰 접속). 외부 감시가 보는 값이다."""
        return self._run((SQL_HEARTBEAT, (self.host, self.clock(), locked)))

    def record_cycle(self, started_at, duration, result, opened=None, error=None):
        """result: ok | skipped | error. ok 만 last_cycle_ok 를 갱신하고 오류를 지운다."""
        stmts = [(SQL_CYCLE, (self.host, started_at, round(duration, 2), result, opened, error))]
        if result == "ok":
            stmts.append((SQL_CYCLE_OK, (self.host, self.clock())))
        elif result == "error":
            stmts.append((SQL_ERROR, (self.host, (error or "")[:500])))
        return self._run(*stmts)

    def record_dump(self, started_at, kind, body):
        """실패한 사이클의 UI 덤프를 남기고 호스트당 최근 dump_keep 개만 둔다."""
        return self._run((SQL_DUMP, (self.host, started_at, kind, body)),
                         (SQL_DUMP_PRUNE, (self.host, self.host, self.dump_keep)))

    def note_error(self, error):
        """사이클 밖의 오류(폰 접속 실패 등)를 last_error 에 남긴다."""
        return self._run((SQL_ERROR, (self.host, (error or "")[:500])))

    def selftest(self):
        """연결, 스키마 생성, 하트비트 기록, 읽기까지 확인한다. 실패하면 예외를 그대로 낸다."""
        self.conn = self._connect(self.dsn)
        for stmt in SCHEMA:
            self._exec(stmt)
        self.schema_ok = True
        self._exec(SQL_HEARTBEAT, (self.host, self.clock(), None))
        return self._exec(SQL_SELECT, (self.host,))


class NullStore:
    enabled = False

    def heartbeat(self, locked):
        return False

    def record_cycle(self, *a, **kw):
        return False

    def record_dump(self, *a, **kw):
        return False

    def note_error(self, error):
        return False

    def load_last_cycle_ok(self):
        return None
