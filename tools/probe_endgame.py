#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_endgame.py —— 读客户端里 `gspEndGame` 落位后的结算数据

`0x0411 gspEndGame` 的处理器 `0x551804` 把包体搬成一个 13 dword 的紧凑结构，
再由 `0x4a4096` 存进 **`GameContextQuest + 0x3ec + seat*0x34`**（`0x4a40b2`：
`imul esi,esi,0x34` / `lea edi,[esi+eax+0x3ec]` / `rep movsd` 0xd 次）。

★ 基址容易看错：`0x4a4096` 开头的 `mov ecx,[0x72e29c]` 只是拿 LobbyStage 去做
座位有效性检查（`0x4045f9`）；真正的基址是 `[esp+8]`，也就是调用方
`0x5518a7: push eax` 压进去的那个 —— 而那个 eax 来自 `0x551844` 的
`dynamic_cast<GameContextQuest*>([0x72e2dc])`。

配合 `tools/gs_ctl.py endgame-probe`（12 个业务值发 101..112）就能把
「包字段 -> 客户端结构槽」的映射一次性钉死，不用去猜结算界面上哪一格是什么。
这比从 UI 代码静态反推快得多：真正读这块内存的 `0x4a4b02` 是玩家标签渲染，
绕得很远（FINDINGS §91）。

同时打印 `0x551804` 会更新的四个全局：

    [0x72e330]  += pkt+0x1c    ★ 累加，不是赋值
    [0x72e33c]   = pkt+0x10
    [0x72e340]   = pkt+0x18    ← 这个不进结算结构，只落全局
    [0x72e344]   = pkt+0x14

用法：
    python tools/probe_endgame.py <pid> [座位号]

要看 §92 那张落位表对不对，就在发包前后各跑一次。
"""
import ctypes as C
import sys
from ctypes import wintypes as W

k32 = C.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.ReadProcessMemory.argtypes = [W.HANDLE, W.LPCVOID, W.LPVOID,
                                  C.c_size_t, C.POINTER(C.c_size_t)]

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

LOBBY_STAGE = 0x72E29C
RESULT_BASE = 0x3EC          # 0x4a40b2
RESULT_STRIDE = 0x34         # 每座位 13 个 dword
RESULT_DWORDS = 13

GAME_CONTEXT = 0x72E2DC      # 0x5518de：结算界面 0x4913fc 的 this

#: `0x551804` 在「座位号 == 我的」时更新的四个全局（0x5518c0..0x5518d9）。
GLOBALS = [
    (0x72E330, "+= pkt+0x1c  ★累加"),
    (0x72E33C, " = pkt+0x10"),
    (0x72E340, " = pkt+0x18  （不进结算结构）"),
    (0x72E344, " = pkt+0x14"),
]

#: §92 从 `0x551854` 逐条核对出的「紧凑结构槽 -> 包字段」。
SLOT_SOURCE = [
    "pkt+0x08 低字节(成功 bool)",
    "pkt+0x0c", "pkt+0x10", "pkt+0x14", "pkt+0x1c", "pkt+0x20",
    "pkt+0x24", "pkt+0x28",
    "★ 从没被赋值（栈垃圾）",
    "pkt+0x2c", "pkt+0x30", "pkt+0x34", "pkt+0x38",
]


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


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pid = int(sys.argv[1])
    seat = int(sys.argv[2]) if len(sys.argv) > 2 else None

    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        print(f"!! OpenProcess({pid}) 失败: {C.get_last_error()}")
        return 1

    lobby = u32(h, LOBBY_STAGE)
    if not lobby:
        print("!! LobbyStage 是空的（还没进大厅？）")
        return 1
    my_seat = u32(h, lobby + 0x1CC)
    print(f"LobbyStage = 0x{lobby:08x}   我的座位 = {my_seat}")

    context = u32(h, GAME_CONTEXT)
    if context:
        # 0x49140c：[ctx+4] 非 0 时 0x4913fc 直接返回，结算界面根本不建。
        print(f"GameContext = 0x{context:08x}  [ctx+4] = {read(h, context + 4, 1)[0]}"
              f"   （非 0 表示结算界面已建过，0x4913fc 会直接返回）")

    if not context:
        print("!! GameContext 是空的（不在关卡里？）")
        return 1

    seats = [seat] if seat is not None else range(6)
    for s in seats:
        base = context + RESULT_BASE + s * RESULT_STRIDE
        raw = read(h, base, RESULT_DWORDS * 4)
        if raw is None:
            print(f"座位 {s}: 读不到 0x{base:08x}")
            continue
        values = [int.from_bytes(raw[i * 4:i * 4 + 4], "little", signed=True)
                  for i in range(RESULT_DWORDS)]
        if seat is None and not any(values):
            continue
        print(f"\n座位 {s} 的结算结构 @0x{base:08x}:")
        for i, (value, source) in enumerate(zip(values, SLOT_SOURCE)):
            print(f"  +0x{i * 4:02x}  {value:>12}  0x{value & 0xffffffff:08x}"
                  f"   <- {source}")

    print("\n0x551804 更新的全局（只在包里的座位号 == 我的座位时才写）:")
    for addr, note in GLOBALS:
        print(f"  [0x{addr:08x}] = {i32(h, addr):>12}   {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
