#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
probe_input.py —— 「双击 A/D 出不了近身攻击」的现场探针（会话 17 新增）

用户实机反馈：**双击 A 或 D 出近身攻击时灵时不灵，连续快速按很多次时基本发不
出来**；连本机服务器（单机）也一样，**待机房间里就能复现**。

静态逆向已经把整条链读到底了（FINDINGS §183），这个探针把每一个判据都读出来，
按一次就能看出卡在哪一条。**纯 `ReadProcessMemory`，不注入、不改客户端一个字节。**

## 链路（全部地址都是脱壳后的镜像地址）

```text
WndProc 0x40ee72 → InputSystem::OnMessage 0x42979c      实例 = [0x72e2bc]
    按下 0x429b04:  已经按着就丢弃；否则 inc [键+0x305]、[键+0x205]=1
    抬起 0x429af4:  [键+0x205]=0     ★ 不动 +0x305
InputSystem::Update 0x429ba3（每帧一次，调用点 0x42b52e）:
    memcpy(this+4, this+0x205, 0x201)    上一帧的按下标记
    全部 256 个键的 +0x305 各减 1        ★「刚按过」只亮一帧（除非一帧内按了多次）
0x429bf0 GetKeyState(vk):
    +0x305 > 0 → 0x33 刚按过 ／ +0x205 → 0x41 按住 ／ 上一帧按着 → 0x22 刚松开 ／ 0x10 空闲
玩家输入函数 0x5154d3：A/←/Q 取 max → [char+0x2b8]，D/→/E → [char+0x2c0]

双击判定 0x515b03（左）/ 0x515b6a（右），**七条全过才出招**：
    ① [char+0x5c4] == 0                      别的动作还没结束（0x5d5eb0）
    ② now - [char+0x744] >  600 ms (0x258)   ★ 冲刺冷却
    ③ [char+0x5d4] == 0                      上一发冲刺还没收尾
    ④ 技能冷却表 [char+0x6a0] 里 0 号不在冷却（0x401c0c）
    ⑤ 这一帧 [char+0x2b8]/[+0x2c0] **恰好等于 0x33**
    ⑥ now - [char+0x748]/[+0x74c] < 250 ms (0xfa)
    ⑦ 一个浮点条件（0x515a96，速度/体力之类）
  命中 → 0x51515c(this, ∓1, [char+0x5d0])、[char+0x5d4]=1、[char+0x744]=now
  ★ ①~④ 任何一条不过，**整块被跳过**（0x515af9），连「记下这次点按」都不做 ——
    也就是说**冷却期间的点按全部丢失**。这一条最可能就是「连按反而发不出」的根。

## 用法

```bash
runtime\python\python.exe tools\probe_input.py <pid>          # 盯 30 秒
runtime\python\python.exe tools\probe_input.py <pid> 60       # 盯 60 秒
runtime\python\python.exe tools\probe_input.py <pid> 60 0.001 # 自定采样间隔
```

`<pid>` 是 `BigShot.exe` 的进程号（`tasklist | findstr BigShot`）。

**怎么用它取证**（在**待机房间**里，角色能跑动的那个画面）：

1. 先**慢慢双击 A 三次**（正常应该出得来招）；
2. 再**连按 A 十几次**（用户说这时基本出不来）；
3. 把整段输出发回来。

★ 窗口不在前台时游戏主循环基本不跑，读到的值会「冻住」 —— 探针跑起来之后
  记得把游戏窗口点回前台再按键。
"""
import ctypes as C
import sys
import time
from ctypes import wintypes as W

k32 = C.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.ReadProcessMemory.argtypes = [W.HANDLE, W.LPCVOID, W.LPVOID,
                                  C.c_size_t, C.POINTER(C.c_size_t)]

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

# 输出里有 ✓ / ✗ / ★，重定向到文件时 Windows 默认按 GBK 写会直接抛异常。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

#: 全局单例。和 `probe_death.py` 用的是同一批（那边已经实机验证过）。
INPUT_SYSTEM = 0x72E2BC          # 0x40eef5 / 0x515640 都读它
LOBBY_STAGE = 0x72E29C           # +0x1cc = 我的座位，+0x1d0+座位*4 = 角色对象
CLOCK_ROOT = 0x72E2B4            # 0x409fdd = [[[0x72e2b4]+8]+0xd4] 游戏时钟（毫秒）

#: `InputSystem` 里三张 256 项的字节表（0x429ba3 / 0x429b04 / 0x429bf0）。
OFF_PREV_DOWN = 0x004            # 上一帧的按下标记（Update 里 memcpy 过来的）
OFF_DOWN = 0x205                 # 当前按下标记
OFF_TAPS = 0x305                 # ★「刚按过」计数器，每帧减 1

#: 角色对象上的字段（0x5154d3 / 0x515a90 / 0x515b03，见上面的链路图）。
OFF_AXIS_LEFT = 0x2B8
OFF_AXIS_UP = 0x2BC
OFF_AXIS_RIGHT = 0x2C0
OFF_AXIS_DOWN = 0x2C4
OFF_BUSY = 0x5C4                 # [char+0x5c0] 那个容器的 +4；非 0 = 忙
OFF_GAUGE = 0x2A4                # ★ 判据⑦ 拿它和一个按角色查出来的阈值比（0x515a8c）
OFF_KIND = 0x2B0                 # 查那个阈值用的角色/类型下标（0x515a5a）
OFF_DASHING = 0x5D4              # 冲刺还没收尾
OFF_LAST_DASH = 0x744            # 上一次冲刺的时刻（冷却起点）
OFF_LAST_TAP_L = 0x748
OFF_LAST_TAP_R = 0x74C

#: 两个阈值常量，改它们就是「加宽判定窗口」的那一刀（§183）。
DASH_COOLDOWN_MS = 0x258         # 600
DOUBLE_TAP_MS = 0xFA             # 250

#: 三个键映射到同一条轴（0x515640 起对每条轴取 max）。
AXIS_KEYS = {
    "左": (0x41, 0x25, 0x51),    # A / ← / Q
    "右": (0x44, 0x27, 0x45),    # D / → / E
}
KEY_NAMES = {0x41: "A", 0x25: "←", 0x51: "Q",
             0x44: "D", 0x27: "→", 0x45: "E"}

STATE_NAMES = {0x10: "空闲", 0x22: "刚松开", 0x33: "刚按过", 0x41: "按住"}


def read(handle, addr, size):
    buf = (C.c_ubyte * size)()
    got = C.c_size_t(0)
    if not addr or not k32.ReadProcessMemory(handle, C.c_void_p(addr), buf,
                                             size, C.byref(got)):
        return None
    return bytes(buf[:got.value])


def u32(handle, addr):
    raw = read(handle, addr, 4)
    return None if raw is None else int.from_bytes(raw, "little")


def i32(handle, addr):
    raw = read(handle, addr, 4)
    return None if raw is None else int.from_bytes(raw, "little", signed=True)


def u8(handle, addr):
    raw = read(handle, addr, 1)
    return None if raw is None else raw[0]


def f32(handle, addr):
    raw = read(handle, addr, 4)
    if raw is None:
        return None
    return C.cast(C.pointer(C.c_uint32(int.from_bytes(raw, "little"))),
                  C.POINTER(C.c_float)).contents.value


def game_clock(handle):
    """`0x409fdd`：游戏内部时钟（毫秒）。判定里的 `now` 就是它。"""
    root = u32(handle, CLOCK_ROOT)
    if not root:
        return None
    obj = u32(handle, root + 8)
    if not obj:
        return None
    return u32(handle, obj + 0xD4)


def key_state(down, prev_down, taps):
    """照抄 `0x429bf0` 的三段判断。注意**「刚按过」排在「按住」前面**。"""
    if taps:
        return 0x33
    if down:
        return 0x41
    if prev_down:
        return 0x22
    return 0x10


def my_character(handle):
    """`[[0x72e29c]+0x1d0 + 我的座位*4]` —— 和 `probe_death.py` 同一条路。"""
    lobby = u32(handle, LOBBY_STAGE)
    if not lobby:
        return None, None
    seat = i32(handle, lobby + 0x1CC)
    if seat is None or not 0 <= seat < 6:
        return None, seat
    return u32(handle, lobby + 0x1D0 + seat * 4), seat


def sample(handle):
    """一次快照。读不到就返回 ``None``（游戏还没进房间 / 已经退了）。"""
    inp = u32(handle, INPUT_SYSTEM)
    if not inp:
        return None
    snap = {"now": game_clock(handle), "keys": {}, "axis": {}}
    for vk in KEY_NAMES:
        down = u8(handle, inp + OFF_DOWN + vk)
        prev = u8(handle, inp + OFF_PREV_DOWN + vk)
        taps = u8(handle, inp + OFF_TAPS + vk)
        if down is None or prev is None or taps is None:
            return None
        snap["keys"][vk] = {"down": down, "prev": prev, "taps": taps,
                            "state": key_state(down, prev, taps)}

    char, seat = my_character(handle)
    snap["seat"] = seat
    snap["char"] = char
    if char:
        snap["axis"]["左"] = i32(handle, char + OFF_AXIS_LEFT)
        snap["axis"]["右"] = i32(handle, char + OFF_AXIS_RIGHT)
        snap["busy"] = u32(handle, char + OFF_BUSY)
        snap["gauge"] = f32(handle, char + OFF_GAUGE)
        snap["kind"] = i32(handle, char + OFF_KIND)
        snap["dashing"] = u8(handle, char + OFF_DASHING)
        snap["last_dash"] = i32(handle, char + OFF_LAST_DASH)
        snap["last_tap_L"] = i32(handle, char + OFF_LAST_TAP_L)
        snap["last_tap_R"] = i32(handle, char + OFF_LAST_TAP_R)
    return snap


def gate_report(snap, axis):
    """这一帧那条轴过不过前四关 + 双击窗口，逐条给结论。"""
    if not snap.get("char"):
        return "（还没有角色对象）"
    now = snap.get("now")
    last_tap = snap["last_tap_L"] if axis == "左" else snap["last_tap_R"]
    checks = []
    checks.append(("①不忙", (snap.get("busy") or 0) == 0,
                   f"[+0x5c4]={snap.get('busy')}"))
    if now is not None and snap.get("last_dash") is not None:
        since = now - snap["last_dash"]
        checks.append(("②冷却", since > DASH_COOLDOWN_MS,
                       f"距上次冲刺 {since} ms（要 > {DASH_COOLDOWN_MS}）"))
    checks.append(("③不在冲刺", (snap.get("dashing") or 0) == 0,
                   f"[+0x5d4]={snap.get('dashing')}"))
    checks.append(("⑤状态=0x33", snap["axis"].get(axis) == 0x33,
                   f"轴={STATE_NAMES.get(snap['axis'].get(axis), snap['axis'].get(axis))}"))
    # ⑦ 只能报数值：阈值要调 0x4716c7(角色下标) 才算得出来，从进程外算不了。
    # 但只要看「成功那几次 vs 失败那几次」的数值差，阈值自己就浮出来了。
    if snap.get("gauge") is not None:
        checks.append(("⑦槽位", True, f"[+0x2a4]={snap['gauge']:.2f}"))
    if now is not None and last_tap is not None:
        gap = now - last_tap if last_tap else None
        checks.append(("⑥250ms内", gap is not None and 0 < gap < DOUBLE_TAP_MS,
                       f"距上次同向点按 {gap} ms（要 < {DOUBLE_TAP_MS}）"
                       if gap is not None else "还没记过点按"))
    return "  ".join(f"{'✓' if ok else '✗'}{name}[{detail}]"
                     for name, ok, detail in checks)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pid = int(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 0.002

    handle = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
                             False, pid)
    if not handle:
        print(f"打不开进程 {pid}（错误 {C.get_last_error()}）。"
              f"pid 对不对？要不要用管理员跑？")
        return 1

    print(f"盯 pid={pid} {seconds:.0f} 秒，采样间隔 {interval * 1000:.0f} ms。")
    print("★ 现在把游戏窗口点回前台 —— 不在前台时主循环不跑，读数会冻住。")
    print("★ 先慢慢双击 A 三次，再连按 A 十几次。\n")

    started = time.monotonic()
    previous = None
    taps_seen = {vk: 0 for vk in KEY_NAMES}
    tapped_frames = {"左": 0, "右": 0}
    dashes = 0
    rejected = {"①不忙": 0, "②冷却": 0, "③不在冲刺": 0, "⑥250ms内": 0}

    while time.monotonic() - started < seconds:
        snap = sample(handle)
        if snap is None:
            time.sleep(0.05)
            continue
        stamp = (time.monotonic() - started) * 1000.0

        if previous is not None:
            # -- 按下 / 抬起 / 计数器变化 ------------------------------------
            for vk, name in KEY_NAMES.items():
                was, now_key = previous["keys"][vk], snap["keys"][vk]
                if now_key["taps"] > was["taps"]:
                    taps_seen[vk] += 1
                    print(f"[{stamp:8.1f}ms] 按下 {name}    "
                          f"计数器 {was['taps']} → {now_key['taps']}"
                          + ("   ⚠ 计数器 >1：这一帧里按了不止一次"
                             if now_key["taps"] > 1 else ""))
                elif now_key["taps"] < was["taps"]:
                    print(f"[{stamp:8.1f}ms]   （帧）{name} 计数器 "
                          f"{was['taps']} → {now_key['taps']}")
                if was["down"] and not now_key["down"]:
                    print(f"[{stamp:8.1f}ms] 抬起 {name}")

            # -- 轴状态变化 + 判定 -------------------------------------------
            for axis in ("左", "右"):
                before = previous["axis"].get(axis)
                after = snap["axis"].get(axis)
                if before == after:
                    continue
                shown = STATE_NAMES.get(after, after)
                line = f"[{stamp:8.1f}ms] 轴{axis} → {shown}"
                if after == 0x33:
                    tapped_frames[axis] += 1
                    line += "\n            " + gate_report(snap, axis)
                print(line)

            # -- 判据字段的任何变化都记 --------------------------------------
            #    ★ 只在「轴变成 0x33」时打是不够的：轴可能**一直**停在 0x33 上，
            #      那时候到底是哪一关把招式挡住的，只有这几行看得见。
            for field, label in (("busy", "①忙[+0x5c4]"),
                                 ("dashing", "③冲刺中[+0x5d4]"),
                                 ("last_dash", "②冷却起点[+0x744]"),
                                 ("last_tap_L", "⑥左点按[+0x748]"),
                                 ("last_tap_R", "⑥右点按[+0x74c]")):
                was, now_value = previous.get(field), snap.get(field)
                if was == now_value or now_value is None:
                    continue
                print(f"[{stamp:8.1f}ms]   {label} {was} → {now_value}")
                if field == "last_dash":
                    dashes += 1
                    gauge = snap.get("gauge")
                    print(f"[{stamp:8.1f}ms] ★★ 冲刺出招了（第 {dashes} 次）"
                          + (f"  [+0x2a4]={gauge:.2f}" if gauge is not None else "")
                          + f" —— 接下来 {DASH_COOLDOWN_MS} ms 是冷却期，"
                          f"期间的点按**全部不记**")

        previous = snap
        time.sleep(interval)

    print("\n================ 小结 ================")
    print(f"座位 {previous.get('seat') if previous else '?'}，"
          f"角色对象 {previous.get('char') and hex(previous['char'])}")
    for vk, name in KEY_NAMES.items():
        if taps_seen[vk]:
            print(f"{name} 按下 {taps_seen[vk]} 次")
    for axis in ("左", "右"):
        print(f"轴{axis} 进入「刚按过(0x33)」{tapped_frames[axis]} 次")
    print(f"冲刺出招 {dashes} 次")
    print("\n判读：按下次数 ≫ 进入 0x33 的次数 ⇒ 一帧里按了多次被合并了；"
          "\n      进入 0x33 的次数 ≫ 冲刺次数 ⇒ 被 ①~④ 那几关挡住了"
          "（看上面每次 0x33 那一行的 ✗ 在哪一条）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
