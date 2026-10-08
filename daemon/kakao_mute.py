#!/usr/bin/env python3
"""kakao-mute 호스트 데몬: 광고 폴더의 안읽음을 가상 디스플레이에서 읽음 처리한다.

사이클 1회:
  가드(물리 화면에서 카톡 사용 중이면 건너뜀) -> 더미 Surface 가상 디스플레이 생성(VdTest)
  -> 카톡 실행 -> 폴더 탭 -> 맨 위 행에 안읽음이 있으면 열고 닫기를 반복 -> 디스플레이 해제

서브커맨드:
  run        폴링 루프: 잠금 상태를 짧은 간격으로 확인하고, 풀려 있고 직전 확인이 오래됐을 때만 사이클을 돌린다
  check-db   Postgres 연결, 스키마 생성, 하트비트 기록/읽기 확인 (KMUTE_DB_DSN 필요)
  once       사이클 1회 (잠금 확인 없이)
  check-lock 폰의 잠금 상태 출력
  discover   폴더 탭까지만 열고 목록의 노드(resource-id/bounds)를 출력. 채널 행은 열지 않는다.
  check-guard  물리 화면의 포커스 앱을 출력

표준 라이브러리만 사용한다. 폰은 adb 로 접속 가능해야 한다 (README 참고).
"""
import argparse
import collections
import json
import logging
import logging.handlers
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import kmute_store

HERE = Path(__file__).resolve().parent
REMOTE_JAR = "/data/local/tmp/vdtest.jar"

DEFAULTS = {
    "serial": None,
    "connect": None,
    "jar": "../helper/build/vdtest.jar",
    "app": "com.kakao.talk/.activity.SplashActivity",
    "folder_tab": "광고",
    "chat_name_id": None,
    "unread_badge_id": None,
    "display_size": "1080x2340",
    "dpi": 420,
    "lock_check_interval_sec": 60,
    "check_interval_sec": 1800,
    "retry_gap_sec": 300,
    "unlock_settle_sec": 5,
    "late_wait_sec": 8,
    "tab_attempts": 2,
    "failure_dump_keep": 5,
    "db_dsn": "",
    "launch_wait_sec": 4,
    "settle_sec": 1.2,
    "dwell_sec": 3,
    "max_open_per_cycle": 20,
    "helper_max_sec": 180,
    "skip_if_foreground": ["com.kakao.talk"],
    "discord_webhook_url": "",
    "alert_after_sec": 1800,
    "alert_repeat_sec": 10800,
    "alert_min_interval_sec": 21600,
}

log = logging.getLogger("kmute")


class AdbError(Exception):
    pass


class CycleError(Exception):
    pass


class NotCalibrated(Exception):
    pass


class ForeignScreen(Exception):
    """가상 디스플레이 대신 다른 앱(물리 화면에서 사용자가 쓰는 앱)의 UI 가 덤프로 돌아왔다."""


class Adb:
    def __init__(self, serial):
        self.serial = serial
        self.base = ["adb"] + (["-s", serial] if serial else [])

    def run(self, *args, timeout=30, check=True):
        r = subprocess.run(self.base + list(args), capture_output=True, timeout=timeout)
        out = r.stdout.decode("utf-8", "replace")
        if check and r.returncode != 0:
            raise AdbError("adb %s -> %s %s" % (" ".join(args), r.returncode, r.stderr.decode("utf-8", "replace")))
        return out

    def shell(self, cmd, **kw):
        return self.run("shell", cmd, **kw)

    def popen_shell(self, cmd):
        return subprocess.Popen(self.base + ["shell", cmd], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def ensure_connected(cfg):
    serial = cfg["serial"]
    if not serial:
        return

    def listed():
        out = subprocess.run(["adb", "devices"], capture_output=True).stdout.decode("utf-8", "replace")
        return re.search(r"^%s\s+device\b" % re.escape(serial), out, re.M) is not None

    if listed():
        return
    if cfg["connect"]:
        subprocess.run(["adb", "connect", cfg["connect"]], capture_output=True, timeout=20)
        if listed():
            return
    raise AdbError("폰에 접속할 수 없음: %s" % serial)


def parse_display0_resumed(text):
    """`dumpsys activity activities` 출력에서 기본(물리) 디스플레이에서 Resumed 상태인 패키지 집합.

    디스플레이별 값은 `topResumedActivity=` 와 `Resumed:` 만 쓴다. 마지막 디스플레이 구역 끝에는 전역
    `ResumedActivity:` 줄(포커스를 가진 디스플레이의 앱)이 붙어 있어서, 포커스가 가상 디스플레이에 있으면
    그 줄을 물리 화면의 것으로 오인하게 된다. 그래서 이 패턴은 일부러 쓰지 않는다.
    """
    pkgs, cur = set(), None
    for line in text.splitlines():
        m = re.match(r"Display #(\d+) \(", line)
        if m:
            cur = int(m.group(1))
            continue
        if line and not line[0].isspace():
            cur = None
            continue
        if cur != 0:
            continue
        m = re.search(r"(?:\bResumed:|topResumedActivity=)\s*ActivityRecord\{\S+ u\d+ ([\w.]+)/", line)
        if m:
            pkgs.add(m.group(1))
    return pkgs


def foreground_packages_display0(adb):
    return parse_display0_resumed(adb.shell("dumpsys activity activities", timeout=30))


def device_locked(adb):
    """폰이 잠겨 있으면 True, 풀려 있으면 False, 판별 불가면 None.

    잠금 상태에서는 가상 디스플레이로 보낸 탭이 무시된다. 값이 둘 중 하나라도 잠금이면 잠금으로 본다.
    """
    out = adb.shell("dumpsys trust | grep deviceLocked")
    m = re.search(r"\(current\).*?deviceLocked=(\d)", out, re.S) or re.search(r"deviceLocked=(\d)", out)
    trust = (m.group(1) == "1") if m else None
    if trust:
        return True
    out = adb.shell("dumpsys window | grep isKeyguardShowing")
    m = re.search(r"isKeyguardShowing=(true|false)", out)
    keyguard = (m.group(1) == "true") if m else None
    if keyguard:
        return True
    if trust is None and keyguard is None:
        return None
    return False


def guard_blocked(cfg, adb):
    return foreground_packages_display0(adb) & set(cfg["skip_if_foreground"])


class Notifier:
    """디스코드 웹훅 알림. URL 은 config.json 또는 환경변수 KMUTE_DISCORD_WEBHOOK (비밀값, 로그에 남기지 않는다)."""

    def __init__(self, cfg):
        self.url = os.environ.get("KMUTE_DISCORD_WEBHOOK") or cfg.get("discord_webhook_url") or ""
        self.host = socket.gethostname()
        self.last = {}

    def send(self, text, key=None, min_interval=0):
        if not self.url:
            log.info("(알림 미설정) %s", text)
            return False
        now = time.time()
        if key and now - self.last.get(key, 0) < min_interval:
            return False
        content = ("[kakao-mute@%s] %s" % (self.host, text))[:1900]
        req = urllib.request.Request(
            self.url, data=json.dumps({"content": content}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "kakao-mute/1.0"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            log.warning("알림 전송 실패: %s", type(e).__name__)
            return False
        if key:
            self.last[key] = now
        return True


class Helper:
    """가상 디스플레이를 만드는 shell 권한 프로세스(VdTest)를 관리한다."""

    def __init__(self, adb, cfg, name):
        self.adb = adb
        cmd = ("CLASSPATH=%s app_process /system/bin VdTest name=%s size=%s dpi=%s app=%s hold=%s"
               % (REMOTE_JAR, name, cfg["display_size"], cfg["dpi"], cfg["app"], cfg["helper_max_sec"]))
        self.p = adb.popen_shell(cmd)
        self.q = queue.Queue()
        self.tail = collections.deque(maxlen=15)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.p.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            self.tail.append(line)
            self.q.put(line)

    def wait_display_id(self, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self.q.get(timeout=1)
            except queue.Empty:
                if self.p.poll() is not None:
                    break
                continue
            log.debug("helper: %s", line)
            m = re.search(r"DISPLAY_ID=(\d+)", line)
            if m:
                return int(m.group(1))
        raise CycleError("가상 디스플레이 생성 실패. helper 출력:\n  " + "\n  ".join(self.tail))

    def stop(self):
        try:
            self.p.stdin.write(b"quit\n")
            self.p.stdin.flush()
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.adb.shell("pkill -f VdTest", check=False)


def parse_bounds(s):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", s or "")
    return tuple(int(x) for x in m.groups()) if m else None


def center(b):
    return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2


def parent_map(root):
    return {c: p for p in root.iter() for c in p}


def label(n):
    return n.attrib.get("text") or n.attrib.get("content-desc") or ""


def clickable_ancestor(n, parents):
    cur = n
    while cur is not None:
        if cur.attrib.get("clickable") == "true":
            return cur
        cur = parents.get(cur)
    return n


KAKAO_PACKAGE = "com.kakao.talk"
# 가상 디스플레이의 덤프에는 카톡 노드와 함께 시스템 UI(내비게이션 바)가 항상 섞여 있고, 카톡이 뜨기 전에는
# 시스템 UI 나 런처만 보일 수 있다. 이들은 "다른 앱"으로 치지 않는다 (로딩 중과 구분하기 위해).
NEUTRAL_PACKAGES = {"com.android.systemui", "com.sec.android.app.launcher", "android"}


def foreign_packages(root):
    """카톡 노드가 없고 중립이 아닌 다른 앱 노드가 있으면 그 패키지 목록을, 아니면 None 을 돌려준다.

    실패 사례: 사용자가 물리 화면에서 배터리(com.samsung.android.lool)나 Tailscale 을 보고 있을 때
    `uiautomator dump --display N` 이 N 번이 아니라 그 앱의 트리를 돌려줬다.
    """
    packages = {n.attrib.get("package") for n in root.iter("node") if n.attrib.get("package")}
    if KAKAO_PACKAGE in packages:
        return None
    others = sorted(packages - NEUTRAL_PACKAGES)
    return others or None


class Ui:
    def __init__(self, adb, display):
        self.adb = adb
        self.display = display

    def dump(self):
        path = "/sdcard/kmute_dump.xml"
        self.adb.shell("uiautomator dump --display %d %s" % (self.display, path))
        xml = self.adb.run("exec-out", "cat", path).strip()
        root = ET.fromstring(xml)
        others = foreign_packages(root)
        if others:
            raise ForeignScreen(",".join(others))
        return root

    def tap_node(self, node, root):
        target = clickable_ancestor(node, parent_map(root))
        x, y = center(parse_bounds(target.attrib["bounds"]))
        self.adb.shell("input -d %d tap %d %d" % (self.display, x, y))

    def back(self):
        self.adb.shell("input -d %d keyevent 4" % self.display)


def folder_selected(root, folder_tab):
    """폴더 탭 칩이 실제로 선택된 상태인가. 다른 탭(전체 등)의 목록을 광고로 오인하지 않기 위한 검사."""
    return any(n.attrib.get("text") == folder_tab and
               (n.attrib.get("selected") == "true" or n.attrib.get("checked") == "true")
               for n in root.iter("node"))


def chat_rows(root, name_id):
    names = [n for n in root.iter("node") if n.attrib.get("resource-id") == name_id and n.attrib.get("text")]
    names.sort(key=lambda n: parse_bounds(n.attrib["bounds"])[1])
    return names


def has_unread(name_node, root, cfg):
    """채널 행에 안읽음 배지가 있는가. 배지 노드 식별은 안읽음이 생긴 뒤 discover 로 확인해 채운다."""
    badge_id = cfg["unread_badge_id"]
    if not badge_id:
        raise NotCalibrated("unread_badge_id 미설정. 안읽음이 있을 때 `discover` 로 배지 노드의 resource-id 확인 후 config 에 기입")
    row = clickable_ancestor(name_node, parent_map(root))
    return any(d.attrib.get("resource-id") == badge_id and d.attrib.get("text", "").strip()
               for d in row.iter("node"))


def print_nodes(root):
    parents = parent_map(root)
    for n in root.iter("node"):
        if not label(n):
            continue
        cb = clickable_ancestor(n, parents).attrib.get("bounds")
        print("%-30s id=%-45s bounds=%s click=%s" % (repr(label(n))[:30], n.attrib.get("resource-id", "-"),
                                                    n.attrib.get("bounds"), cb))


def find_folder_tab(root, cfg):
    return next((n for n in root.iter("node") if label(n) == cfg["folder_tab"]), None)


def find_chat_tab(root):
    """카톡 하단의 '채팅 탭' (접근성 라벨이 '채팅 탭' 또는 '채팅 탭 N개의 새로운 업데이트')."""
    return next((n for n in root.iter("node") if n.attrib.get("content-desc", "").startswith("채팅 탭")), None)


def with_dump(error, root, kind):
    """실패 시점의 UI 덤프를 예외에 실어 올린다. 저장은 DB 를 가진 호출자(Poller)가 한다.

    채팅 이름이 들어 있다. 연속 실패에서 처음 실패한 화면이 남도록 DB 에는 최근 failure_dump_keep 개를 둔다.
    """
    error.dump = (kind, ET.tostring(root, encoding="unicode"))
    return error


def reach_folder_tab(ui, cfg):
    """카톡이 뜬 뒤 폴더 탭이 보이는 화면까지 간다. (루트, 탭 노드)를 돌려준다.

    덤프는 폰의 비용이 크므로 폴링하지 않는다. 평소에는 launch_wait_sec 뒤 덤프 1번으로 끝나고,
    탭이 없을 때만 최대 tab_attempts 번 더 덤프한다. 그때마다
    - 하단 '채팅 탭'이 보이면 다른 하단 탭(친구 등)에 있는 것이니 채팅 탭을 눌러 이동하고,
    - 하단 탭도 안 보이면 아직 로딩 중(콜드 스타트)으로 보고 late_wait_sec 동안 기다린다.
    끝내 못 찾으면 마지막 덤프를 저장하고 실패한다.
    """
    root = ui.dump()
    for attempt in range(cfg["tab_attempts"] + 1):
        tab = find_folder_tab(root, cfg)
        if tab is not None:
            return root, tab
        if attempt == cfg["tab_attempts"]:
            break
        chat = find_chat_tab(root)
        if chat is not None:
            log.info("폴더 탭이 안 보이고 하단 채팅 탭이 보임 -> 채팅 탭으로 이동")
            ui.tap_node(chat, root)
            time.sleep(cfg["settle_sec"])
        else:
            log.info("폴더 탭도 하단 탭도 안 보임 -> 로딩 중으로 보고 %d초 대기", cfg["late_wait_sec"])
            time.sleep(cfg["late_wait_sec"])
        root = ui.dump()
    raise with_dump(CycleError("폴더 탭 '%s' 을 못 찾음 (채팅 화면이 아니거나 로딩/로그인/업데이트 화면)" % cfg["folder_tab"]),
                    root, "no_tab")


def run_cycle(cfg, adb, mode="walk", notifier=None):
    ensure_connected(cfg)

    blocked = guard_blocked(cfg, adb)
    if blocked:
        log.info("물리 화면에서 사용 중(%s) -> 이번 회차 건너뜀", ",".join(sorted(blocked)))
        return None

    helper = Helper(adb, cfg, "kmute-%d" % int(time.time()))
    try:
        display = helper.wait_display_id()
        log.info("가상 디스플레이 생성 id=%d", display)
        ui = Ui(adb, display)
        time.sleep(cfg["launch_wait_sec"])

        root, tab = reach_folder_tab(ui, cfg)
        for _ in range(2):
            ui.tap_node(tab, root)
            time.sleep(cfg["settle_sec"])
            root = ui.dump()
            if folder_selected(root, cfg["folder_tab"]):
                break
            tab = next((n for n in root.iter("node") if label(n) == cfg["folder_tab"]), None)
            if tab is None:
                raise with_dump(CycleError("폴더 탭 '%s' 이 사라짐" % cfg["folder_tab"]), root, "tab_vanished")
        else:
            raise with_dump(CycleError("폴더 탭 '%s' 을 눌렀지만 선택되지 않음. 다른 탭 목록을 처리하지 않도록 중단"
                                       % cfg["folder_tab"]), root, "not_selected")

        if mode == "discover":
            print_nodes(root)
            return 0

        if not cfg["chat_name_id"]:
            raise NotCalibrated("chat_name_id 미설정. `discover` 출력에서 채널 이름 노드의 resource-id 를 config 에 기입")

        opened = 0
        prev = None
        interrupted = False
        while opened < cfg["max_open_per_cycle"]:
            root = ui.dump()
            rows = chat_rows(root, cfg["chat_name_id"])
            if not rows:
                log.warning("채널 행을 못 찾음")
                break
            top = rows[0]
            if not has_unread(top, root, cfg):
                break
            name = top.attrib["text"]
            if name == prev:
                log.warning("'%s' 을 열었는데 안읽음이 안 풀림 -> 이번 회차 중단", name)
                if notifier:
                    notifier.send("'%s' 을 열었는데 안읽음이 안 풀립니다. 이번 회차를 중단했습니다." % name,
                                  key="stuck:" + name, min_interval=cfg["alert_min_interval_sec"])
                break
            blocked = guard_blocked(cfg, adb)
            if blocked:
                log.info("열기 직전 가드: 물리 화면에서 사용 중(%s) -> 중단", ",".join(sorted(blocked)))
                interrupted = True
                break
            log.info("열기: %s", name)
            ui.tap_node(top, root)
            time.sleep(cfg["dwell_sec"])
            ui.back()
            time.sleep(cfg["settle_sec"])
            opened += 1
            prev = name
            check = ui.dump()
            if not any(label(n) == cfg["folder_tab"] for n in check.iter("node")):
                log.info("목록 복귀 안 됨 -> back 한 번 더")
                ui.back()
                time.sleep(cfg["settle_sec"])
        else:
            log.warning("회차 상한(%d) 도달, 안읽음이 남아있을 수 있음", cfg["max_open_per_cycle"])
        # 안읽음을 발견하고도 가드로 중단됐으면 확인을 끝내지 못한 것이다. None(건너뜀)으로 돌려줘
        # 직전 확인 시각이 갱신되지 않고 retry_gap_sec 뒤에 다시 시도하게 한다.
        return None if interrupted else opened
    except ForeignScreen as e:
        log.info("가상 디스플레이 대신 다른 앱 화면(%s)이 덤프됨 -> 사용자가 폰을 쓰는 중으로 보고 이번 회차 건너뜀", e)
        return None
    finally:
        helper.stop()
        log.info("가상 디스플레이 해제")


def first_line(e):
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:300]


class Poller:
    """잠금 상태를 짧은 간격으로 확인하고, 풀려 있고 직전 확인이 충분히 오래됐을 때만 사이클을 돌린다.

    - 잠겨 있으면 사이클을 돌리지 않는다 (입력이 무시되므로). 이건 실패가 아니다.
    - **직전 확인(성공한 사이클)이 check_interval_sec 이상 전일 때만** 사이클을 돌린다. 몇 시간 잠겨 있다가
      풀리면 마지막 확인이 오래됐으니 자연스럽게 바로 돌고, 해제를 자주 해도 그 시간 안에는 다시 안 돈다.
    - 직전 확인 시각은 DB 에 저장해 두었다가 시작할 때 복원한다 (재시작이 즉시 확인을 일으키지 않게).
    - 사이클이 실패했거나 건너뛰어졌으면(물리 화면에서 카톡 사용 중) 확인한 것이 아니므로 직전 확인 시각을
      갱신하지 않고, retry_gap_sec 뒤에 다시 시도한다.
    - 확인 자체나 사이클이 연속으로 alert_after_sec 이상 실패하면 알린다.
    """

    def __init__(self, cfg, adb, notifier, store=None, clock=time.time, sleep=time.sleep):
        self.cfg, self.adb, self.notifier = cfg, adb, notifier
        self.store = store or kmute_store.NullStore()
        self.clock, self.sleep = clock, sleep
        self.last_locked = None
        self.last_attempt = None
        self.fail_since = None
        self.alerted_at = None
        self.last_ok = self._restore_last_ok()

    def _restore_last_ok(self):
        ts = self.store.load_last_cycle_ok()
        now = self.clock()
        if ts is None or ts > now + 60:
            return None
        log.info("DB 에서 직전 확인 시각 복원: %d분 전", (now - ts) // 60)
        return ts

    def step(self):
        """확인 1회. 다음 확인까지 대기할 초를 돌려준다."""
        cfg = self.cfg
        try:
            ensure_connected(cfg)
            locked = device_locked(self.adb)
        except Exception as e:
            self._fail(e)
            return cfg["lock_check_interval_sec"]

        if locked is None:
            log.warning("잠금 상태를 판별하지 못함 -> 풀려 있는 것으로 보고 진행 (탭 선택 검증이 보호)")
        self.store.heartbeat(locked)
        if locked:
            if self.last_locked is not True:
                log.info("잠금 상태 -> 사이클 대기")
            self.fail_since = None
        else:
            now = self.clock()
            ok_age = None if self.last_ok is None else now - self.last_ok
            try_age = None if self.last_attempt is None else now - self.last_attempt
            due = ((ok_age is None or ok_age >= cfg["check_interval_sec"])
                   and (try_age is None or try_age >= cfg["retry_gap_sec"]))
            if due:
                if self.last_locked is True:
                    log.info("잠금 해제 감지 -> %d초 뒤 사이클", cfg["unlock_settle_sec"])
                    self.sleep(cfg["unlock_settle_sec"])
                self._cycle()
        self.last_locked = locked
        return cfg["lock_check_interval_sec"]

    def _cycle(self):
        started = self.clock()
        self.last_attempt = started
        try:
            opened = run_cycle(self.cfg, self.adb, "walk", self.notifier)
        except NotCalibrated:
            raise
        except Exception as e:
            self.store.record_cycle(started, self.clock() - started, "error", error=first_line(e))
            dump = getattr(e, "dump", None)
            if dump:
                self.store.record_dump(started, *dump)
            self._fail(e)
            return
        duration = self.clock() - started
        if opened is None:
            self.store.record_cycle(started, duration, "skipped")
            return
        self.last_ok = self.clock()
        self.store.record_cycle(started, duration, "ok", opened=opened)
        self._ok()

    def _ok(self):
        if self.alerted_at is not None:
            self.notifier.send("복구됨: 다시 정상 동작합니다.")
            self.alerted_at = None
        self.fail_since = None

    def _fail(self, e):
        log.exception("확인/사이클 실패")
        self.store.note_error(first_line(e))
        now = self.clock()
        if self.fail_since is None:
            self.fail_since = now
        if (now - self.fail_since >= self.cfg["alert_after_sec"]
                and (self.alerted_at is None or now - self.alerted_at >= self.cfg["alert_repeat_sec"])):
            self.notifier.send("%d분째 실패 중: %s" % ((now - self.fail_since) // 60, first_line(e)))
            self.alerted_at = now


def prepare(cfg, adb):
    jar = (HERE / cfg["jar"]).resolve()
    if not jar.exists():
        sys.exit("헬퍼 jar 없음: %s\n  먼저 실행: bash helper/build.sh" % jar)
    ensure_connected(cfg)
    adb.run("push", str(jar), REMOTE_JAR)
    adb.shell("pkill -f VdTest", check=False)


def load_config(path):
    cfg = dict(DEFAULTS)
    p = Path(path)
    if p.exists():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "once", "discover", "check-guard", "check-lock", "check-db"])
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.handlers.RotatingFileHandler(HERE / "kakao_mute.log", maxBytes=1_000_000,
                                                                       backupCount=3, encoding="utf-8")])
    cfg = load_config(args.config)
    adb = Adb(cfg["serial"])
    notifier = Notifier(cfg)
    dsn = os.environ.get("KMUTE_DB_DSN") or cfg.get("db_dsn") or ""
    store = kmute_store.Store(dsn, dump_keep=cfg["failure_dump_keep"]) if dsn else kmute_store.NullStore()

    if args.cmd == "check-db":
        if not dsn:
            sys.exit("DSN 이 없음. 환경변수 KMUTE_DB_DSN 또는 config 의 db_dsn 을 설정하세요.")
        rows = store.selftest()
        print("DB 연결/스키마/하트비트 기록 확인 OK:", rows)
        return

    if args.cmd == "check-guard":
        ensure_connected(cfg)
        print("물리 화면 Resumed 패키지:", sorted(foreground_packages_display0(adb)) or "(없음)")
        return

    if args.cmd == "check-lock":
        ensure_connected(cfg)
        state = device_locked(adb)
        print("폰 잠금 상태:", {True: "잠김", False: "풀림", None: "판별 불가"}[state])
        return

    prepare(cfg, adb)
    try:
        if args.cmd in ("once", "discover"):
            try:
                r = run_cycle(cfg, adb, "discover" if args.cmd == "discover" else "walk", notifier)
            except CycleError as e:
                if getattr(e, "dump", None):
                    store.record_dump(time.time(), *e.dump)
                raise
            log.info("결과: %s", "건너뜀" if r is None else "%d개 열음" % r)
            return
        notifier.send("데몬 시작 (잠금 확인 %d초, 직전 확인이 %d분 이상 전일 때만 확인, DB %s)"
                      % (cfg["lock_check_interval_sec"], cfg["check_interval_sec"] // 60,
                         "사용" if store.enabled else "미사용"))
        poller = Poller(cfg, adb, notifier, store)
        while True:
            try:
                wait = poller.step()
            except NotCalibrated as e:
                notifier.send("설정 필요로 종료: %s" % e)
                sys.exit("설정 필요: %s" % e)
            time.sleep(wait)
    except KeyboardInterrupt:
        log.info("종료")


if __name__ == "__main__":
    main()
