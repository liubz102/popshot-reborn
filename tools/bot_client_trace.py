#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bot_client_trace.py —— 拿客户端 `CHAR.` 逐帧行量「收方到底把 bot 画成什么样」。

    python tools/bot_client_trace.py logs/bshook_*.log [座位]

`CHAR.` / `HB<` 是会话 68 加进 `hook/bshook.c` 的诊断（`start-debug.bat` 默认开，
精简模式设 `BSHOOK_CHAR_DIAG=1`）：每帧每个远端座位一行 `CHAR.`（0x50d404 入口，
本帧行走之后、腾空积分之前），每收一发心跳一行 `HB<`（0x5041e1 入口，解码之前）。

这是 D150 的验收尺子。量三样：

1. **腾空跳变**：相邻两帧 `|Δpos − v_上一帧|`。收方自己积分就是 `Δpos = v`
   （那一行的 `v` 已含本帧重力），多出来的就是心跳拉的 / 碰撞挡的。
   连续几帧都大 = 先停住再被拽着追（§191 那种）；只剩零星单帧 = 修好了。
2. **地面反向位移**：本帧键是 L/R，`Δx` 却朝反方向 —— 翻转之后收方朝旧方向
   多走的那几帧（§190）。
3. **心跳拉动**：`HB<` 到紧接着那一行 `CHAR.` 的位置差（含本帧最多一步行走）。

只用标准库；Windows 控制台先 `set PYTHONIOENCODING=utf-8`。
"""
import re
import sys

TIME = r"(\d\d:\d\d:\d\d\.\d\d\d)"
CHAR = re.compile(
    r"^\[" + TIME + r"\] CHAR\.\s+座位 (\d) 角色 ([0-9A-F]{8}) 位置\(\+34,38\) \(([-\d.]+), ([-\d.]+)\)"
    r" 速度\(\+120,124\) \(([-\d.]+), ([-\d.]+)\) 踩地\(\+128\) (\d) 走向\(\+4b4\) (-?\d+)"
    r" 键 (....) 冲刺\(\+4bc\) (\d) 约束\(\+164\) (-?\d+)/(-?\d+) 蹲\(\+2b5\) (\d)")
HB = re.compile(
    r"^\[" + TIME + r"\] HB<\s+座位 (\d) 角色 ([0-9A-F]{8}) 收心跳前 位置 \(([-\d.]+), ([-\d.]+)\)"
    r" 速度 \(([-\d.]+), ([-\d.]+)\) 踩地 (\d) 约束 (-?\d+)")


def seconds(text):
    h, m, s = text.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def quantiles(values):
    if not values:
        return "n=0"
    values = sorted(values)
    n = len(values)
    pick = lambda f: values[min(n - 1, int(f * n))]   # noqa: E731
    return (f"n={n} p50={pick(.5):.1f} p90={pick(.9):.1f} "
            f"p99={pick(.99):.1f} max={values[-1]:.1f}")


def parse(path):
    frames, pulls = {}, {}
    pending = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for number, line in enumerate(fh, 1):
            m = CHAR.match(line)
            if m:
                seat = int(m[2])
                row = dict(t=seconds(m[1]), time=m[1], line=number, obj=m[3],
                           x=float(m[4]), y=float(m[5]), vx=float(m[6]), vy=float(m[7]),
                           ground=int(m[8]), walk=int(m[9]), keys=m[10], fast=int(m[11]),
                           bind=int(m[12]), bind_ttl=int(m[13]), crouch=int(m[14]))
                frames.setdefault(seat, []).append(row)
                before = pending.pop(seat, None)
                if before is not None:
                    pulls.setdefault(seat, []).append((before, row))
                continue
            m = HB.match(line)
            if m:
                seat = int(m[2])
                pending[seat] = dict(t=seconds(m[1]), time=m[1], line=number, obj=m[3],
                                     x=float(m[4]), y=float(m[5]), vx=float(m[6]),
                                     vy=float(m[7]), ground=int(m[8]), bind=int(m[9]))
    return frames, pulls


def report(seat, rows, pulls):
    print(f"\n===== 座位 {seat}：{len(rows)} 帧，{len(pulls)} 发心跳 =====")
    objs = sorted({r["obj"] for r in rows})
    print(f"角色对象指针 {len(objs)} 个：{' '.join(objs)}"
          + ("　★ 一条命里不该变（D148 风险 2）" if len(objs) > 1 else ""))
    binds = sum(1 for r in rows if r["bind"])
    if binds:
        print(f"★ 约束(+164) 非零的帧 {binds}（BSM1 应恒 0）")
    jerks, streaks, streak, reversals, teleports = [], [], 0, [], 0
    for a, b in zip(rows, rows[1:]):
        if b["t"] - a["t"] > 0.1:
            streak = 0
            continue
        # 出生 / 重生 / 闯关归队是整段搬家，不是卡顿：单独计数，不进分布。
        if abs(b["x"] - a["x"]) > 200 or abs(b["y"] - a["y"]) > 200:
            teleports += 1
            streak = 0
            continue
        if not a["ground"]:
            jerk = ((b["x"] - a["x"] - a["vx"]) ** 2 + (b["y"] - a["y"] - a["vy"]) ** 2) ** 0.5
            jerks.append((jerk, a, b))
            if jerk > max(4.0, 0.5 * (a["vx"] ** 2 + a["vy"] ** 2) ** 0.5):
                streak += 1
            else:
                if streak:
                    streaks.append(streak)
                streak = 0
        else:
            dx = b["x"] - a["x"]
            key = b["keys"]
            if ("R" in key and dx < -0.5) or ("L" in key and dx > 0.5):
                reversals.append((abs(dx), a, b))
    if streak:
        streaks.append(streak)
    print(f"整段搬家（出生 / 重生 / 归队，> 200 px）{teleports} 次，不计入下面的分布")
    print("腾空跳变 |Δpos − v|（px）:", quantiles([j[0] for j in jerks]))
    print("跳变成串（连续大跳变的帧数）:", quantiles(streaks),
          "　★ 修好后应只剩 1~2 帧的，不再有 4 帧以上的串")
    print("地面反向位移（px）:", quantiles([r[0] for r in reversals]),
          f"　共 {len(reversals)} 帧")
    print("心跳拉动 |CHAR. − HB<|（px，含本帧一步行走）:",
          quantiles([((b["x"] - a["x"]) ** 2 + (b["y"] - a["y"]) ** 2) ** 0.5 for a, b in pulls]))
    print("腾空跳变最大的 10 处：")
    for jerk, a, b in sorted(jerks, key=lambda j: -j[0])[:10]:
        print(f"  {a['time']} L{a['line']} ({a['x']:.1f},{a['y']:.1f}) v=({a['vx']:.1f},{a['vy']:.1f})"
              f" -> {b['time']} ({b['x']:.1f},{b['y']:.1f}) 跳变 {jerk:.1f}")


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    frames, pulls = parse(argv[1])
    if not frames:
        print("没有 CHAR. 行 —— 这份日志是精简模式，或 hook 没装上（看 PATCH 行）")
        return 1
    seats = [int(argv[2])] if len(argv) > 2 else sorted(frames)
    for seat in seats:
        report(seat, frames.get(seat, []), pulls.get(seat, []))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
