#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bot_motion_compare.py —— bot 同步的两端对账（V0.3 §189 / §190 / §192 那几把尺子）。

    python tools/bot_motion_compare.py fire  logs/server.out logs/bshook_*.log
        每一发 bot 的 rpFire：客户端 `FIRE>` 实读位置 vs 服务端身体（上一发心跳 +
        逐格速度外推 / 两发心跳插值），带符号、按踩地 / 腾空分桶，列最差样例。
        ★ `FIRE>` 打在收包阶段、本帧物理还没跑，两端比自带约 1 帧的系统偏差
          （均值偏「客户端落后」）—— 看尾部（p90 / p99），别看均值。
    python tools/bot_motion_compare.py keys  logs/server.out
        踩地方向翻转（`◆走位来自` 行）到下一发心跳隔了多久（§190 的相位锁死）。
    python tools/bot_motion_compare.py embed logs/server.out
        服务端 bot 腾空心跳里「头圆 / 身圆嵌在实心地形里」的占比、成段的长度
        （§192：脚点模型飞过去、收方三圆被顶住的那种地方）。地图 / 角色 id 从
        日志里自己认（换房 `map='…'`、`/a` `/c` 行）。三圆扫掠上线后该接近 0。
    python tools/bot_motion_compare.py seat  logs/server.out 4 21:29:33.500 21:29:35.300
        把某个座位一段时间里的心跳 / 事件逐行解码打出来（对现场用）。

解析复用 `audit_bot_motion_logs.server_packets()`（会话 66 的脚本）。
只用标准库 + 服务端自己的 `mapdata` / `chrprops`；Windows 控制台先
`set PYTHONIOENCODING=utf-8`。
"""
import bisect
import os
import re
import statistics
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

from audit_bot_motion_logs import client_fires, seconds, server_packets  # noqa: E402

NAMES = {0x4001: "HB", 0x6: "Jump", 0x2: "Fire", 0x3: "Explode", 0x1: "ChgWpn",
         0xb: "Crouch", 0x4: "Splash", 0x7: "Dash", 0x5: "SetOnFire", 0x4005: "Load"}
TICK = 0.032
GRAVITY = 1.2
TIME = r"(\d\d:\d\d:\d\d\.\d\d\d)"


def quantiles(values):
    if not values:
        return "n=0"
    values = sorted(values)
    n = len(values)
    pick = lambda f: values[min(n - 1, int(f * n))]   # noqa: E731
    return (f"n={n} p50={pick(.5):.1f} p90={pick(.9):.1f} "
            f"p99={pick(.99):.1f} max={values[-1]:.1f}")


def load(server_log):
    packets, _ = server_packets(server_log)
    beats = {s: [] for s in range(1, 6)}
    events = {s: [] for s in range(1, 6)}
    for p in packets:
        if p["op"] == 0x4001:
            flags = struct.unpack_from("<I", p["body"], 19)[0]
            beats[p["seat"]].append(dict(
                t=p["t"], time=p["time"], line=p["line"], x=p["x"], y=p["y"],
                vx=p["vx"], vy=p["vy"], ground=int(p["ground"]), keys=p["keys"],
                fast=int(bool(flags & 8)), facing=((flags & 3) ^ 2) - 2,
                bsm=(struct.unpack_from("<III", p["body"], 35)
                     if len(p["body"]) == 47 else None)))
        else:
            events[p["seat"]].append(dict(t=p["t"], time=p["time"], line=p["line"],
                                          op=p["op"], seq=p["seq"], body=p["body"]))
    return beats, events


# ---------------------------------------------------------------------------
def cmd_fire(server_log, client_log):
    beats, events = load(server_log)
    rows = []
    for f in client_fires(client_log):
        seat = f["seat"]
        if seat not in beats:
            continue
        match = None
        for e in events[seat]:
            if e["op"] != 2 or not -0.2 <= f["t"] - e["t"] <= 0.2:
                continue
            mx, my = struct.unpack_from("<ff", e["body"], 6)
            if abs(mx - f["mx"]) < 1.5 and abs(my - f["my"]) < 1.5:
                match = e
                break
        if match is None:
            continue
        ts = match["t"]
        times = [h["t"] for h in beats[seat]]
        i = bisect.bisect_right(times, ts) - 1
        if i < 0 or i + 1 >= len(beats[seat]):
            continue
        a, b = beats[seat][i], beats[seat][i + 1]
        if b["t"] - a["t"] > 0.3 or ts - a["t"] > 0.2:
            continue
        ticks = round((ts - a["t"]) / TICK)
        if a["ground"]:
            w = (ts - a["t"]) / (b["t"] - a["t"]) if b["t"] > a["t"] else 0.0
            sx, sy = a["x"] + (b["x"] - a["x"]) * w, a["y"] + (b["y"] - a["y"]) * w
        else:
            sx = a["x"] + a["vx"] * ticks
            sy = a["y"] + sum(a["vy"] + GRAVITY * (k + 1) for k in range(ticks))
        jumps = [e for e in events[seat] if e["op"] == 6 and e["t"] <= ts]
        rows.append(dict(seat=seat, time=f["time"], dx=f["cx"] - sx, dy=f["cy"] - sy,
                         air=not a["ground"], vx=a["vx"], vy=a["vy"], keys=a["keys"],
                         since_jump=(ts - jumps[-1]["t"]) if jumps else 99.0,
                         stage=jumps[-1]["body"][1] if jumps else 0,
                         cline=f["line"], sline=a["line"]))
    air = [r for r in rows if r["air"]]
    gnd = [r for r in rows if not r["air"]]
    print(f"配上的开火 {len(rows)} 发（服务端时刻 = rpFire 发包时刻）")
    print("地面 |dx|:", quantiles([abs(r["dx"]) for r in gnd]),
          " |dy|:", quantiles([abs(r["dy"]) for r in gnd]))
    print("空中 |dx|:", quantiles([abs(r["dx"]) for r in air]),
          " |dy|:", quantiles([abs(r["dy"]) for r in air]))
    lag = [-r["dx"] * (1 if r["vx"] > 0 else -1) for r in air if r["vx"]]
    if lag:
        print("空中 沿运动方向的滞后（+ = 客户端落后）:", quantiles(lag),
              f" 均值 {statistics.mean(lag):.1f}")
    glag = [-r["dx"] * (1 if r["keys"] & 4 else -1) for r in gnd if r["keys"] & 5]
    if glag:
        print("地面 走路时沿按键方向的滞后（+ = 落后）:", quantiles(glag),
              f" 均值 {statistics.mean(glag):.1f}")
    print("地面 站着 |dx|:", quantiles([abs(r["dx"]) for r in gnd if not r["keys"] & 5]))
    for title, group, key in (("空中 |dx| 最差", air, lambda r: -abs(r["dx"])),
                              ("空中 |dy| 最差", air, lambda r: -abs(r["dy"])),
                              ("地面 |dx| 最差", gnd, lambda r: -abs(r["dx"]))):
        print(f"\n{title}:")
        for r in sorted(group, key=key)[:10]:
            print(f"  {r['time']} 座位{r['seat']} dx={r['dx']:+7.1f} dy={r['dy']:+7.1f}"
                  f" v=({r['vx']},{r['vy']}) keys={r['keys']:#x}"
                  f" 距上次跳 {r['since_jump'] * 1000:.0f}ms 段{r['stage']}"
                  f" 客户端行 {r['cline']} 服务端行 {r['sline']}")


# ---------------------------------------------------------------------------
INTENT = re.compile(r"^\[" + TIME + r"\] 房#\d+ bot (\d) +◆走位来自「[^」]+」 方向=([+-]\d)")


def cmd_keys(server_log):
    beats, _ = load(server_log)
    flips = {s: [] for s in range(1, 6)}
    last = {}
    with open(server_log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = INTENT.match(line)
            if not m:
                continue
            seat, direction = int(m[2]), int(m[3])
            if last.get(seat) is not None and last[seat] != direction:
                flips[seat].append(seconds(m[1]))
            last[seat] = direction
    delays, same = [], 0
    for seat in range(1, 6):
        times = [h["t"] for h in beats[seat]]
        for t in flips[seat]:
            i = bisect.bisect_left(times, t)
            if i >= len(times) or i == 0 or not beats[seat][i - 1]["ground"]:
                continue
            delay = (times[i] - t) * 1000
            delays.append(delay)
            same += delay < 2
    print(f"踩地方向翻转 {len(delays)} 次，其中同一格就进了心跳 {same} 次")
    print("翻转 -> 下一发心跳（ms）:", quantiles(delays))
    gaps = [(b["t"] - a["t"]) * 1000 for seat in range(1, 6)
            for a, b in zip(beats[seat], beats[seat][1:]) if a["ground"] and b["ground"]]
    print("踩地心跳间隔（ms）:", quantiles(gaps))


# ---------------------------------------------------------------------------
MAP = re.compile(r"^\[" + TIME + r"\] .*map='([^:']+)")
ROUND = re.compile(r"^\[" + TIME + r"\] \[sim\] 房间 #\d+ 32ms 循环起步（第 (\d+) 代")
ROUND_END = re.compile(r"^\[" + TIME + r"\] .*(模式结束|夺分模式结束|游戏结束|结算)")
SEAT_CHAR = re.compile(r"^\[" + TIME + r"\] .*(?:/a: 座位 (\d) 加入 bot \d（角色 (\d+)|"
                       r"/c: 座位 (\d) 的 bot \d -> 面板 1 = 角色 id (\d+))")


def rounds_of(server_log):
    """`[(开局时刻, 结束时刻, 地图名, {座位: 角色 id})]` —— 从日志里自己认。"""
    rounds, current_map, chars, open_round = [], None, {}, None
    with open(server_log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = MAP.match(line)
            if m:
                current_map = m[2]
                continue
            m = SEAT_CHAR.match(line)
            if m:
                seat = m[2] or m[4]
                chars[int(seat)] = int(m[3] or m[5])
                continue
            m = ROUND.match(line)
            if m:
                if open_round is not None:
                    open_round[1] = seconds(m[1])
                    rounds.append(tuple(open_round))
                open_round = [seconds(m[1]), None, current_map, dict(chars)]
                continue
            m = ROUND_END.match(line)
            if m and open_round is not None and open_round[1] is None:
                open_round[1] = seconds(m[1])
                rounds.append(tuple(open_round))
                open_round = None
    if open_round is not None:
        open_round[1] = float("inf")
        rounds.append(tuple(open_round))
    return rounds


def cmd_embed(server_log):
    import chrprops
    import mapdata
    beats, _ = load(server_log)
    for t0, t1, name, chars in rounds_of(server_log):
        try:
            terrain = mapdata.load(name)
        except Exception:                                   # noqa: BLE001
            terrain = None
        if terrain is None:
            print(f"\n===== {name}: 没有地形产物，跳过")
            continue
        print(f"\n===== {name} 开局 {t0:.1f}s ~ {t1:.1f}s  {terrain}")
        for seat in range(1, 6):
            air = [h for h in beats[seat] if not h["ground"] and t0 <= h["t"] <= t1]
            if not air:
                continue
            who = chrprops.get(chars.get(seat, 0))
            embedded, streaks, run, top_out = [], [], 0, 0
            for h in air:
                head, body = who.circles(h["x"], h["y"])[:2]
                inside = False
                for cx, cy, r, _region in (head, body):
                    ty = int(cy - r + 1)
                    if ty < 0:
                        top_out += 1                        # 图顶不算（真人自己也出得去）
                        continue
                    if terrain.is_solid(int(cx), ty) or terrain.is_solid(int(cx), int(cy)):
                        inside = True
                if inside:
                    embedded.append(h)
                    run += 1
                elif run:
                    streaks.append(run)
                    run = 0
            if run:
                streaks.append(run)
            share = 100.0 * len(embedded) / len(air)
            print(f"座位 {seat}（角色 {chars.get(seat, '?')}）：腾空心跳 {len(air)}，"
                  f"头 / 身嵌在实心里 {len(embedded)}（{share:.1f}%），"
                  f"成段 {len(streaks)} 段、最长 {max(streaks) if streaks else 0} 格；"
                  f"头顶出图顶 {top_out} 发（不算嵌入）")
            for h in embedded[:2]:
                print(f"    例：{h['time']} pos=({h['x']},{h['y']}) v=({h['vx']},{h['vy']})"
                      f" 服务端行 {h['line']}")


# ---------------------------------------------------------------------------
def cmd_seat(server_log, seat, t0, t1):
    beats, events = load(server_log)
    rows = ([("HB", h) for h in beats[seat]] + [(NAMES.get(e["op"], hex(e["op"])), e)
                                                for e in events[seat]])
    rows.sort(key=lambda r: (r[1]["t"], r[1]["line"]))
    for kind, r in rows:
        if not seconds(t0) <= r["t"] <= seconds(t1):
            continue
        if kind == "HB":
            bsm = "" if r["bsm"] is None else f" id={r['bsm'][0]} rev={r['bsm'][1]} tick={r['bsm'][2]}"
            print(f"{r['time']} L{r['line']:<7} HB      pos=({r['x']},{r['y']}) v=({r['vx']},{r['vy']})"
                  f" ground={r['ground']} fast={r['fast']} face={r['facing']:+d}"
                  f" keys={r['keys']:#04x}{bsm}")
        else:
            print(f"{r['time']} L{r['line']:<7} {kind:<7} seq={r['seq']} body={r['body'].hex()}")


def main(argv):
    if len(argv) >= 4 and argv[1] == "fire":
        cmd_fire(argv[2], argv[3])
    elif len(argv) >= 3 and argv[1] == "keys":
        cmd_keys(argv[2])
    elif len(argv) >= 3 and argv[1] == "embed":
        cmd_embed(argv[2])
    elif len(argv) >= 6 and argv[1] == "seat":
        cmd_seat(argv[2], int(argv[3]), argv[4], argv[5])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
