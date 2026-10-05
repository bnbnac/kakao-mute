#!/usr/bin/env python3
"""kakao-mute 호스트 데몬: 광고 폴더의 안읽음을 가상 디스플레이에서 읽음 처리한다.

사이클 1회:
  가드(물리 화면에서 카톡 사용 중이면 건너뜀) -> 더미 Surface 가상 디스플레이 생성(VdTest)
  -> 카톡 실행 -> 폴더 탭 -> 맨 위 행에 안읽음이 있으면 열고 닫기를 반복 -> 디스플레이 해제

서브커맨드:
  run        폴링 루프 (poll_interval_sec 간격)
  once       사이클 1회
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
    "poll_interval_sec": 1800,
    "launch_wait_sec": 4,
    "settle_sec": 1.2,
    "dwell_sec": 3,
    "max_open_per_cycle": 20,
    "helper_max_sec": 180,
    "skip_if_foreground": ["com.kakao.talk"],
    "discord_webhook_url": "",
    "alert_after_failures": 2,
    "alert_repeat_every": 6,
    "alert_min_interval_sec": 21600,
}

log = logging.getLogger("kmute")


class AdbError(Exception):
    pass


class CycleError(Exception):
    pass


class NotCalibrated(Exception):
    pass


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


def foreground_packages_display0(adb):
    """기본(물리) 디스플레이에서 Resumed 상태인 패키지 집합."""
    text = adb.shell("dumpsys activity activities", timeout=30)
    pkgs, cur = set(), None
    for line in text.splitlines():
        m = re.match(r"\s*Display #(\d+) \(", line)
        if m:
            cur = int(m.group(1))
            continue
        if cur != 0:
            continue
        m = re.search(r"(?:Resumed:|topResumedActivity=|ResumedActivity:)\s*ActivityRecord\{\S+ u\d+ ([\w.]+)/", line)
        if m:
            pkgs.add(m.group(1))
    return pkgs


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


class Ui:
    def __init__(self, adb, display):
        self.adb = adb
        self.display = display

    def dump(self):
        path = "/sdcard/kmute_dump.xml"
        self.adb.shell("uiautomator dump --display %d %s" % (self.display, path))
        xml = self.adb.run("exec-out", "cat", path).strip()
        return ET.fromstring(xml)

    def tap_node(self, node, root):
        target = clickable_ancestor(node, parent_map(root))
        x, y = center(parse_bounds(target.attrib["bounds"]))
        self.adb.shell("input -d %d tap %d %d" % (self.display, x, y))

    def back(self):
        self.adb.shell("input -d %d keyevent 4" % self.display)


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

        root = ui.dump()
        tab = next((n for n in root.iter("node") if label(n) == cfg["folder_tab"]), None)
        if tab is None:
            raise CycleError("폴더 탭 '%s' 을 못 찾음 (채팅 탭이 아니거나 로그인/업데이트 화면)" % cfg["folder_tab"])
        ui.tap_node(tab, root)
        time.sleep(cfg["settle_sec"])

        if mode == "discover":
            print_nodes(ui.dump())
            return 0

        if not cfg["chat_name_id"]:
            raise NotCalibrated("chat_name_id 미설정. `discover` 출력에서 채널 이름 노드의 resource-id 를 config 에 기입")

        opened = 0
        prev = None
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
        return opened
    finally:
        helper.stop()
        log.info("가상 디스플레이 해제")


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
    ap.add_argument("cmd", choices=["run", "once", "discover", "check-guard"])
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

    if args.cmd == "check-guard":
        ensure_connected(cfg)
        print("물리 화면 Resumed 패키지:", sorted(foreground_packages_display0(adb)) or "(없음)")
        return

    prepare(cfg, adb)
    try:
        if args.cmd in ("once", "discover"):
            r = run_cycle(cfg, adb, "discover" if args.cmd == "discover" else "walk", notifier)
            log.info("결과: %s", "건너뜀" if r is None else "%d개 열음" % r)
            return
        notifier.send("데몬 시작 (폴링 %d초)" % cfg["poll_interval_sec"])
        failures, alerted = 0, False
        after, repeat = cfg["alert_after_failures"], cfg["alert_repeat_every"]
        while True:
            try:
                run_cycle(cfg, adb, "walk", notifier)
                if alerted:
                    notifier.send("복구됨: 사이클이 다시 정상 동작합니다.")
                    alerted = False
                failures = 0
            except NotCalibrated as e:
                notifier.send("설정 필요로 종료: %s" % e)
                sys.exit("설정 필요: %s" % e)
            except Exception as e:
                failures += 1
                log.exception("사이클 실패 (연속 %d회)", failures)
                if failures == after or (failures > after and (failures - after) % repeat == 0):
                    first = (str(e).strip().splitlines() or [type(e).__name__])[0][:300]
                    notifier.send("사이클 연속 %d회 실패: %s" % (failures, first))
                    alerted = True
            time.sleep(cfg["poll_interval_sec"])
    except KeyboardInterrupt:
        log.info("종료")


if __name__ == "__main__":
    main()
