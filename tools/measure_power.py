#!/usr/bin/env python3
"""데몬이 폰에 주는 부하(모바일 트래픽, 배터리)를 A/B 로 재는 도구. 표준 라이브러리만 사용한다.

  preflight  측정 조건(충전기 분리, 잠금, Wi-Fi 끔 등)을 점검하고 스냅샷을 한 번 출력한다 (폰은 읽기만)
  run        구간을 자동 전환하며 측정한다 (데몬을 켜고 끈다)
  analyze    결과 파일을 분석해 구간별 표와 판정을 출력한다

구간: A = 데몬 켬(현재 폴링), B = 데몬 끔(adb 연결은 유지). 기본 계획 ABBA, 구간당 90분.
각 구간은 [전환 -> 안정화 -> 시작 판독 -> 대기 -> 끝 판독] 이고, 구간 중에는 폰에 아무 명령도 보내지 않는다
(A 구간에서는 데몬의 잠금 확인만 있다). 판독 항목:
  - /proc/net/dev (모바일 rmnet*, Wi-Fi wlan*, 터널 tun* 의 패킷/바이트) : 실시간이고 리셋되지 않는다
  - dumpsys battery (충전 카운터 µAh = 연료 게이지, 충전 상태, 잔량)
  - 잠금 여부, Wi-Fi 켜짐, 구간 중 화면 켜짐 이벤트 (구간이 오염됐는지 검사)
batterystats 의 앱별 카운터는 갱신 지연과 리셋이 있어 쓰지 않는다.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "daemon"))
import kakao_mute as km  # noqa: E402

CAPACITY_MAH = 3900                    # Galaxy S24 정격(batterystats 의 Capacity 값)
WORTH_FRACTION = 0.03                  # 하루 환산 이 비율 이상이면 구현 가치가 있다고 본다
MARK = ("@@NET", "@@BATT", "@@LOCK", "@@DATE", "@@WIFI", "@@SCREEN")


# ----------------------------------------------------------------- 폰 판독
def snapshot_command(since_phone_date=None):
    parts = ["echo @@NET; cat /proc/net/dev", "echo @@BATT; dumpsys battery",
             "echo @@LOCK; dumpsys trust | grep deviceLocked",
             "echo @@DATE; date '+%m-%d %H:%M:%S'", "echo @@WIFI; settings get global wifi_on"]
    if since_phone_date:
        parts.append("echo @@SCREEN; logcat -b events -d -t '%s.000' -v time | grep screen_toggled; true" % since_phone_date)
    else:
        parts.append("echo @@SCREEN")
    return "; ".join(parts)


def parse_snapshot(text):
    sections, cur = {}, None
    for line in text.splitlines():
        s = line.strip()
        if s in MARK:
            cur = s
            sections[cur] = []
        elif cur:
            sections[cur].append(line.rstrip("\r"))
    net = {}
    for l in sections.get("@@NET", []):
        m = re.match(r"^\s*([\w\.\-]+):\s*(\d+)\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)\s+(\d+)", l)
        if m:
            net[m.group(1)] = [int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))]
    batt = "\n".join(sections.get("@@BATT", []))

    def flag(name):
        m = re.search(r"%s powered:\s*(true|false)" % name, batt)
        return None if not m else m.group(1) == "true"

    cc = re.search(r"Charge counter:\s*(\d+)", batt)
    lv = re.search(r"^\s*level:\s*(\d+)", batt, re.M)
    lock = "\n".join(sections.get("@@LOCK", []))
    m = re.search(r"deviceLocked=(\d)", lock)
    date = (sections.get("@@DATE") or [""])[0].strip()
    wifi = (sections.get("@@WIFI") or [""])[0].strip()
    screen = [l.strip() for l in sections.get("@@SCREEN", []) if "screen_toggled" in l]
    return {"net": net, "cc_uAh": int(cc.group(1)) if cc else None, "level": int(lv.group(1)) if lv else None,
            "ac": flag("AC"), "usb": flag("USB"), "wireless": flag("Wireless"),
            "locked": None if not m else m.group(1) == "1", "phone_date": date, "wifi_on": wifi,
            "screen_events": screen}


def read_snapshot(adb, since_phone_date=None, attempts=5):
    for i in range(attempts):
        try:
            out = adb.run("shell", snapshot_command(since_phone_date), timeout=60)
            break
        except (km.AdbError, subprocess.SubprocessError):
            if i == attempts - 1:
                raise
            time.sleep(30)
    snap = parse_snapshot(out)
    snap["host_ts"] = time.time()
    snap["tailscale"] = tailscale_line()
    return snap


def tailscale_line():
    try:
        out = subprocess.run(["tailscale", "status"], capture_output=True, timeout=15).stdout.decode("utf-8", "replace")
        for l in out.splitlines():
            if " s24 " in l or "android" in l:
                return " ".join(l.split())[:140]
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


# ----------------------------------------------------------------- 사전 점검
def preflight_problems(snap, min_level=70):
    """측정을 막아야 하는 문제 목록. 비어 있으면 통과."""
    p = []
    if any(snap.get(k) for k in ("ac", "usb", "wireless")):
        p.append("충전기가 연결돼 있음 (에너지 측정은 충전기를 뗀 상태에서만 유효)")
    if snap.get("level") is None or snap["level"] < min_level:
        p.append("배터리 잔량 %s%% < %d%% (측정 중 방전/절전 모드 진입 위험)" % (snap.get("level"), min_level))
    if snap.get("locked") is not True:
        p.append("폰이 잠겨 있지 않음 (잠금 + 화면 꺼짐 상태에서 측정해야 함)")
    if snap.get("wifi_on") not in ("0",):
        p.append("Wi-Fi 가 켜져 있음 (모바일 데이터만 쓰도록 꺼야 함, wifi_on=%r)" % snap.get("wifi_on"))
    if snap.get("cc_uAh") is None:
        p.append("충전 카운터를 읽지 못함")
    if not any(k.startswith("rmnet") for k in snap.get("net", {})):
        p.append("모바일 인터페이스(rmnet*)를 찾지 못함")
    return p


# ----------------------------------------------------------------- 데몬 제어
def daemon_pids():
    if os.name == "nt":
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'kakao_mute\\.py' -and "
              "$_.CommandLine -match ' run' -and $_.Name -match 'python' } | ForEach-Object { $_.ProcessId }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True).stdout.decode("utf-8", "replace")
        return [int(x) for x in out.split() if x.isdigit()]
    out = subprocess.run(["pgrep", "-f", "kakao_mute.py run"], capture_output=True).stdout.decode()
    return [int(x) for x in out.split() if x.isdigit()]


def stop_daemon():
    for pid in daemon_pids():
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, 15)
    time.sleep(2)
    return not daemon_pids()


def start_daemon():
    if daemon_pids():
        return True
    out = open(ROOT / "daemon" / "kakao_mute.out.log", "ab")
    err = open(ROOT / "daemon" / "kakao_mute.err.log", "ab")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000     # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, "-u", str(ROOT / "daemon" / "kakao_mute.py"), "run"], cwd=str(ROOT),
                     stdout=out, stderr=err, **kw)
    time.sleep(3)
    return bool(daemon_pids())


# ----------------------------------------------------------------- 측정 실행
def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def record(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def run_measurement(adb, cfg, plan, window_sec, settle_sec, out_path, force=False):
    snap = read_snapshot(adb)
    problems = preflight_problems(snap)
    if problems and not force:
        log("사전 점검 실패:")
        for p in problems:
            log("  - " + p)
        return 2
    record(out_path, {"type": "plan", "plan": plan, "window_sec": window_sec, "settle_sec": settle_sec,
                      "started": time.strftime("%Y-%m-%d %H:%M:%S"), "preflight_problems": problems,
                      "cfg": {k: cfg[k] for k in ("lock_check_interval_sec", "check_interval_sec") if k in cfg}})
    try:
        for idx, label in enumerate(plan):
            ok = start_daemon() if label == "A" else stop_daemon()
            log("구간 %d/%d [%s] 데몬 %s (%s)" % (idx + 1, len(plan), label, "켬" if label == "A" else "끔", "성공" if ok else "전환 실패"))
            record(out_path, {"type": "switch", "window": idx, "label": label, "ok": ok, "ts": time.time()})
            time.sleep(settle_sec)
            start = read_snapshot(adb)
            start.update(type="snapshot", window=idx, label=label, phase="start")
            record(out_path, start)
            log("  시작 판독: 충전 카운터 %s µAh, 잔량 %s%%" % (start["cc_uAh"], start["level"]))
            end_at = time.time() + window_sec
            while time.time() < end_at:
                time.sleep(min(60, max(1, end_at - time.time())))
            end = read_snapshot(adb, since_phone_date=start["phone_date"])
            end.update(type="snapshot", window=idx, label=label, phase="end")
            record(out_path, end)
            log("  끝 판독: 충전 카운터 %s µAh (%+d), 화면 이벤트 %d건" % (
                end["cc_uAh"], (end["cc_uAh"] or 0) - (start["cc_uAh"] or 0), len(end["screen_events"])))
    finally:
        start_daemon()
        log("데몬을 켠 상태로 복구")
    log("측정 종료. 분석: python tools/measure_power.py analyze %s" % out_path)
    return 0


# ----------------------------------------------------------------- 분석
def sum_prefix(net, prefix):
    tot = [0, 0, 0, 0]
    for k, v in net.items():
        if k.startswith(prefix):
            tot = [a + b for a, b in zip(tot, v)]
    return tot


def window_stats(start, end):
    """한 구간의 지표와 유효성. start/end 는 snapshot 레코드."""
    reasons = []
    for s, name in ((start, "시작"), (end, "끝")):
        if any(s.get(k) for k in ("ac", "usb", "wireless")):
            reasons.append("%s 시점에 충전기 연결" % name)
        if s.get("locked") is not True:
            reasons.append("%s 시점에 잠금 해제 상태" % name)
        if s.get("wifi_on") != "0":
            reasons.append("%s 시점에 Wi-Fi 켜짐" % name)
    if end.get("screen_events"):
        reasons.append("구간 중 화면 켜짐/꺼짐 이벤트 %d건" % len(end["screen_events"]))
    if start["cc_uAh"] is None or end["cc_uAh"] is None:
        reasons.append("충전 카운터 없음")
    hours = (end["host_ts"] - start["host_ts"]) / 3600.0
    rate = None
    if start["cc_uAh"] is not None and end["cc_uAh"] is not None and hours > 0:
        rate = (start["cc_uAh"] - end["cc_uAh"]) / 1000.0 / hours       # 방전 속도 mAh/h
        if rate < 0:
            reasons.append("충전 카운터가 증가함(충전됨)")
    d = {}
    for pfx in ("rmnet", "tun", "wlan"):
        a, b = sum_prefix(start["net"], pfx), sum_prefix(end["net"], pfx)
        d[pfx] = [x - y for x, y in zip(b, a)]
    return {"label": start["label"], "window": start["window"], "hours": hours, "rate_mAh_h": rate,
            "drain_mAh": None if rate is None else rate * hours,
            "level": (start["level"], end["level"]), "mobile_pkts": d["rmnet"][1] + d["rmnet"][3],
            "mobile_bytes": d["rmnet"][0] + d["rmnet"][2], "tun_pkts": d["tun"][1] + d["tun"][3],
            "wifi_pkts": d["wlan"][1] + d["wlan"][3], "valid": not reasons, "reasons": reasons,
            "tailscale": (start.get("tailscale") or "")[-60:]}


def load_windows(path):
    snaps = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    snaps = [s for s in snaps if s.get("type") == "snapshot"]
    by = {}
    for s in snaps:
        by.setdefault(s["window"], {})[s["phase"]] = s
    return [window_stats(v["start"], v["end"]) for _, v in sorted(by.items()) if "start" in v and "end" in v]


def verdict(windows):
    valid = [w for w in windows if w["valid"] and w["rate_mAh_h"] is not None]
    a = [w["rate_mAh_h"] for w in valid if w["label"] == "A"]
    b = [w["rate_mAh_h"] for w in valid if w["label"] == "B"]
    out = {"A_n": len(a), "B_n": len(b)}
    if not a or not b:
        out["text"] = "판정 불가: 유효한 A/B 구간이 각각 최소 1개씩 필요 (A %d개, B %d개)" % (len(a), len(b))
        return out
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    effect = mean_a - mean_b
    out.update(mean_A=mean_a, mean_B=mean_b, effect_mAh_h=effect, per_day_mAh=effect * 24)
    threshold = CAPACITY_MAH * WORTH_FRACTION
    out["threshold_per_day_mAh"] = threshold
    if len(b) >= 2:
        noise = abs(b[0] - b[1])
        out["noise_mAh_h"] = noise
        significant = effect > 2 * noise
    else:
        out["noise_mAh_h"] = None
        significant = None
    worth = effect * 24 >= threshold
    if significant is None:
        out["text"] = "잡음을 알 수 없음(유효한 B 구간 1개). 효과 %.1f mAh/h (하루 %.0f mAh)는 %s" % (
            effect, effect * 24, "기준 이상이지만 신뢰 불가" if worth else "기준 미만")
    elif significant and worth:
        out["text"] = "구현 가치 있음: 효과 %.1f mAh/h (하루 %.0f mAh ≥ %.0f)가 잡음 %.1f의 2배보다 큼" % (
            effect, effect * 24, threshold, out["noise_mAh_h"])
    elif worth and not significant:
        out["text"] = "효과는 기준 이상이나 잡음(%.1f mAh/h)과 구분 안 됨: 측정을 더 길게 반복해야 함" % out["noise_mAh_h"]
    else:
        out["text"] = "구현 가치 낮음: 효과 %.1f mAh/h (하루 %.0f mAh)가 기준 %.0f mAh/일 미만" % (effect, effect * 24, threshold)
    return out


def cmd_analyze(path):
    windows = load_windows(path)
    print("%-3s %-6s %6s %12s %12s %10s %9s %9s  %s" % ("구간", "데몬", "시간h", "방전mAh/h", "잔량%", "모바일pkt", "터널pkt", "wlan pkt", "유효"))
    for w in windows:
        print("%-3d %-6s %6.2f %12s %12s %10d %9d %9d  %s" % (
            w["window"] + 1, "켬" if w["label"] == "A" else "끔", w["hours"],
            "-" if w["rate_mAh_h"] is None else "%.1f" % w["rate_mAh_h"], "%s→%s" % w["level"],
            w["mobile_pkts"], w["tun_pkts"], w["wifi_pkts"], "유효" if w["valid"] else "무효: " + "; ".join(w["reasons"])))
    v = verdict(windows)
    print()
    for k in ("mean_A", "mean_B", "effect_mAh_h", "per_day_mAh", "noise_mAh_h", "threshold_per_day_mAh"):
        if k in v and v[k] is not None:
            print("  %-24s %.2f" % (k, v[k]))
    print("판정:", v["text"])
    return 0


# ----------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["preflight", "run", "analyze"])
    ap.add_argument("file", nargs="?", help="analyze: 결과 파일")
    ap.add_argument("--config", default=str(ROOT / "daemon" / "config.json"))
    ap.add_argument("--plan", default="ABBA")
    ap.add_argument("--window-min", type=float, default=90)
    ap.add_argument("--settle-sec", type=float, default=200, help="전환 후 판독 전 대기. 데몬을 끌 때 진행 중이던 헬퍼(최대 180초)가 끝나도록 둔다")
    ap.add_argument("--out", default=None)
    ap.add_argument("--force", action="store_true", help="사전 점검에 실패해도 진행")
    args = ap.parse_args()

    if args.cmd == "analyze":
        if not args.file:
            ap.error("analyze 는 결과 파일이 필요")
        return cmd_analyze(args.file)

    cfg = km.load_config(args.config)
    adb = km.Adb(cfg["serial"])
    km.ensure_connected(cfg)
    if args.cmd == "preflight":
        snap = read_snapshot(adb)
        print(json.dumps({k: v for k, v in snap.items() if k != "net"}, ensure_ascii=False, indent=1))
        print("모바일 pkt (rmnet*):", sum_prefix(snap["net"], "rmnet"), "터널 (tun*):", sum_prefix(snap["net"], "tun"))
        problems = preflight_problems(snap)
        print("사전 점검:", "통과" if not problems else "")
        for p in problems:
            print("  -", p)
        return 0 if not problems else 2

    if set(args.plan) - {"A", "B"}:
        ap.error("--plan 은 A, B 로만 구성")
    out = Path(args.out) if args.out else HERE / "results" / ("power_%s.jsonl" % time.strftime("%Y%m%d_%H%M%S"))
    out.parent.mkdir(parents=True, exist_ok=True)
    log("결과 파일: %s | 계획 %s, 구간 %.0f분, 총 약 %.1f시간" % (
        out, args.plan, args.window_min, len(args.plan) * (args.window_min * 60 + args.settle_sec + 10) / 3600))
    return run_measurement(adb, cfg, args.plan, args.window_min * 60, args.settle_sec, out, args.force)


if __name__ == "__main__":
    sys.exit(main())
