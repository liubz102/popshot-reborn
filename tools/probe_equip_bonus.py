#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_equip_bonus.py —— 从**跑着的客户端**里读出「战斗要用的那张装备加成表」

回答的是用户 2026-09-05 的那个问题：**穿上装备之后，进游戏里真的生效了吗？**
血条不显示数字，看不出来 —— 那就直接去读客户端算 `MaxHp` 时读的那一格。

## 读的是哪一格（FINDINGS §1 / §2）

    [0x72e29c]                      LobbyStage
      +0x1cc                        我的座位号
      +0x40 + seat*0x3c + 0x00      座位占用（byte）
      +0x40 + seat*0x3c + 0x0c      座位的**角色 id**（= 取加成时的桶号 key）
      +0x250 + seat*4               ★ EquipmentEx*  ← `0x030b` 的处理器写的就是它
        +0x3c                       桶表（hash map）：[+4] 桶数组首 [+8] 尾
          节点 +0x00 next  +0x04 桶号 key  +0x08 EquipBonusEntry
            EquipBonusEntry +idx*4  ★ 该属性的合计值（idx 见下表）

`GetEquipBonus(seat, idx, key)`（`0x407014` → `0x41543e`）取的是
**桶 -1（通用）+ 桶 key（本角色专属）** 两格之和；`EquipmentEx::Equip`
（`0x414e43`）按 `key = itemId/1000000 - 1` 分桶。

战斗里这张表被读的地方：`MaxHp = 基础 + 桶[5]`（`0x50a06b`，**加法、100% 生效**）、
`MaxSp = 基础 + 桶[6]`、伤害 `× (100 + 桶[1]) / 100`（`0x4806bf`，★ 只有 15%
概率触发）、挨打 `× (100 − 桶[2]) / 100`（同一道概率门）、移速 `× (100+桶[4])/100`。

⇒ **桶里读得到 Hp=29，就等于「进游戏血上限真的是 129」** —— 那一步只是一次加法。

## 用法

    python tools/probe_equip_bonus.py [pid] [--watch 秒数] [--interval 秒]

不给 pid 就自己找 `BigShot.exe`。`--watch` 只在快照变化时打印一行，
可以一边跑一边用 `tools/gs_ctl.py equip …` 换装，看这张表跟着变。

★ 只读内存，不写、不注入。客户端在大厅 / 房间里就能读 —— `0x030b` 一到
这张表就建好了，**不用真开一局**。
"""
import ctypes as C
import subprocess
import sys
import time
from ctypes import wintypes as W

# 控制台默认是 CP936，「⇒」这类字符编不进去会直接抛 UnicodeEncodeError。
# 和 `gs_ctl.py` / `probe_input.py` 一个口径：输出一律按 UTF-8 走。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

k32 = C.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.ReadProcessMemory.argtypes = [W.HANDLE, W.LPCVOID, W.LPVOID,
                                  C.c_size_t, C.POINTER(C.c_size_t)]

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

LOBBY_STAGE = 0x72E29C
SEAT_BASE = 0x40
SEAT_STRIDE = 0x3C
SEAT_COUNT = 6
EQUIP_BASE = 0x250          # LobbyStage + 0x250 + seat*4 = EquipmentEx*
BUCKET_MAP = 0x3C           # EquipmentEx + 0x3c = 桶表

#: 属性号 → 名字（`0x413096` 建的名字表，FINDINGS §2）。0 号是空串、保留未用。
ATTRIBUTES = {
    1: "Attack 攻击%", 2: "Defense 防御%", 3: "Critical", 4: "MoveSpd 移速%",
    5: "Hp 生命", 6: "Sp 体力", 7: "TeamDmg", 8: "SelfDmg",
    9: "AntiGhostCnt", 10: "HeartBoost", 11: "IncSplashRange",
    12: "DashAttack", 13: "TeamReflection",
}
ATTRIBUTE_COUNT = 14

#: 角色基础生命（`ChrProps.ini`，和 `server/chrprops.py` 的默认值一致）。
#: 只用来把「桶里的 +N」翻译成人看得懂的「血条最大值」，读不到就不翻译。
BASE_HP = 100
BASE_SP = 100

#: 桶号 −1 = 通用（`itemId < 1e6` 的东西），其余 = `itemId/1000000 − 1` = 角色号。
GENERIC_BUCKET = -1
CHARACTER_ZH = {0: "泰尔", 1: "卡希尔", 2: "布洛克"}


def read(h, addr, n):
    buf = (C.c_ubyte * n)()
    got = C.c_size_t()
    if not addr or not k32.ReadProcessMemory(h, W.LPCVOID(addr), buf, n,
                                             C.byref(got)):
        return None
    return bytes(buf)


def u32(h, addr):
    b = read(h, addr, 4)
    return None if b is None else int.from_bytes(b, "little")


def i32(h, addr):
    b = read(h, addr, 4)
    return None if b is None else int.from_bytes(b, "little", signed=True)


def u8(h, addr):
    b = read(h, addr, 1)
    return None if b is None else b[0]


def find_pid():
    """没给 pid 时自己找 BigShot.exe。找不到 / 找到多个都说清楚。"""
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq BigShot.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace").stdout
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("bigshot"):
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def buckets_of(h, equipment):
    """把 `EquipmentEx` 的桶表整张倒出来：``[(桶号, [属性 0..13 的值])]``。

    ★ 走的是**整个桶数组**，不是按 key 去 hash —— 探针的目的是「看看里面
    到底有什么」，全倒出来比按号去查更不容易骗自己。
    """
    first = u32(h, equipment + BUCKET_MAP + 4)
    last = u32(h, equipment + BUCKET_MAP + 8)
    if not first or not last or last < first:
        return None
    count = (last - first) // 4
    if not 0 < count < 4096:            # 桶数组是客户端自己建的，不该离谱
        return None
    out = []
    for i in range(count):
        node = u32(h, first + i * 4)
        seen = 0
        while node and seen < 64:       # 链表；桶再挤也不该有 64 个
            key = i32(h, node + 4)
            entry = node + 8
            values = [i32(h, entry + a * 4) for a in range(ATTRIBUTE_COUNT)]
            if key is None or any(v is None for v in values):
                return None
            out.append((key, values))
            node = u32(h, node)
            seen += 1
    out.sort(key=lambda kv: kv[0])
    return out


def snapshot(h):
    lobby = u32(h, LOBBY_STAGE)
    if not lobby:
        return None
    me = u32(h, lobby + 0x1CC)
    seats = []
    for i in range(SEAT_COUNT):
        base = lobby + SEAT_BASE + i * SEAT_STRIDE
        occupied = u8(h, base + 0x00)
        char_id = u32(h, base + 0x0C)
        equipment = u32(h, lobby + EQUIP_BASE + i * 4)
        table = buckets_of(h, equipment) if equipment else None
        seats.append((occupied, char_id, equipment, table))
    return (lobby, me, tuple(seats))


def total(table, key, attribute):
    """`GetEquipBonus(seat, attribute, key)` 的算法：**通用桶 + 本角色桶**。"""
    got = 0
    for bucket, values in table:
        if bucket in (GENERIC_BUCKET, key):
            got += values[attribute]
    return got


def render(snap):
    lobby, me, seats = snap
    lines = ["LobbyStage=%#x 我的座位=%s" % (lobby, me)]
    for i, (occupied, char_id, equipment, table) in enumerate(seats):
        if not occupied and not equipment:
            continue
        mark = " ★我" if i == me else ""
        who = CHARACTER_ZH.get(char_id, "?")
        lines.append("  座位%d%s 占用=%s 角色id=%s(%s) EquipmentEx=%#x"
                     % (i, mark, occupied, char_id, who, equipment or 0))
        if table is None:
            lines.append("      桶表读不出来（还没收到 0x030b？）")
            continue
        if not table:
            lines.append("      桶表是空的 —— 一件装备都没算进来")
        for bucket, values in table:
            named = ", ".join("%s=%d" % (ATTRIBUTES[a], values[a])
                              for a in sorted(ATTRIBUTES)
                              if values[a])
            tag = "通用" if bucket == GENERIC_BUCKET else CHARACTER_ZH.get(
                bucket, "角色%d" % bucket)
            lines.append("      桶 %-3d(%s): %s"
                         % (bucket, tag, named or "全 0"))
        hp = total(table, char_id, 5)
        sp = total(table, char_id, 6)
        lines.append("      ⇒ GetEquipBonus(座位%d, Hp, %s) = %+d"
                     "  ⇒ 进游戏血条最大值 = %d + %d = **%d**"
                     % (i, char_id, hp, BASE_HP, hp, BASE_HP + hp))
        lines.append("      ⇒ 同理 SP 上限 = %d + %d = %d；攻击 %+d%%、"
                     "防御 %+d%%（这两条只有 15%% 概率触发）、移速 %+d%%"
                     % (BASE_SP, sp, BASE_SP + sp,
                        total(table, char_id, 1), total(table, char_id, 2),
                        total(table, char_id, 4)))
    return "\n".join(lines)


def main():
    args = list(sys.argv[1:])
    watch = 0.0
    interval = 0.5
    pid = None
    while args:
        a = args.pop(0)
        if a == "--watch":
            watch = float(args.pop(0)) if args else 60.0
        elif a == "--interval":
            interval = float(args.pop(0))
        else:
            pid = int(a)
    if pid is None:
        found = find_pid()
        if not found:
            print("没找到跑着的 BigShot.exe —— 先 start.bat 把游戏开起来")
            return 2
        if len(found) > 1:
            print("找到多个 BigShot.exe：%s —— 请指定 pid" % found)
            return 2
        pid = found[0]
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
                        False, pid)
    if not h:
        print("打不开进程 %d（错误码 %d）—— 32 位客户端要用同位数的 Python，"
              "必要时用管理员身份跑" % (pid, C.get_last_error()))
        return 2
    deadline = time.time() + watch
    last = None
    while True:
        snap = snapshot(h)
        if snap is None:
            text = "读不到 LobbyStage —— 客户端还没进到大厅？"
        else:
            text = render(snap)
        if text != last:
            print("[%s] %s" % (time.strftime("%H:%M:%S"), text))
            sys.stdout.flush()
            last = text
        if time.time() >= deadline:
            break
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
