#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_room_ctx.py —— 判断「当前场景建对了没有」的探针（会话 13 新增）

FINDINGS §102 就是靠它一次定案的：待机房间背景纯黑时，读出来的
`GameContext` 是 `GameContextQuest03`（战斗上下文）而不是
`GameContextWaitingRoom`，根因随即锁定到 `Session+0x04`（房间状态）。

读的四个全局：

    [0x72e2a4]  App          +0x54 当前 stage、+0x5c 待切 stage
    [0x72e29c]  LobbyStage   +0x04 房间状态(2=待机中) +0x10 地图名
                             +0x1c descriptor.type   +0x1cc 我的座位
                             +0x40+i*0x3c 座位 i     +0x1d0+i*4 座位角色对象
    [0x72e2d4]  World        +0x1c6c 最后一次**加载成功**的地图名（去掉 .map）
                             +0x1d7c 场景标签（待机房间是 "room"）
    [0x72e2dc]  GameContext  按 vftable 认类型，见 VFT_NAMES

用法：
    python tools/probe_room_ctx.py <pid>

★ **stage 切换是延后到主循环执行的，而游戏窗口不在前台时主循环基本不跑。**
发完切 stage 的包要先把窗口拉到前台（`tools/screenshot.py` 会顺手做这件事）
再读，否则只会看到 `+0x5c` 挂着待切值、上下文还是旧的。
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

APP = 0x72E2A4
LOBBY_STAGE = 0x72E29C
WORLD = 0x72E2D4
GAME_CONTEXT = 0x72E2DC

SEAT_BASE = 0x40
SEAT_STRIDE = 0x3C
SEAT_COUNT = 6

#: 用 tools/rtti_map.py 从 RTTI 反查出来的上下文 vftable。
VFT_NAMES = {
    0x6713EC: "GameContextWaitingRoom",   # 待机房间（Maps/ReadyRoom.map）
    0x670B4C: "GameContext",
    0x6739AC: "GameContextQuest",
    0x674F14: "GameContextQuest03",       # 神秘岛，闯关本体
}


def read(h, addr, n):
    buf = (C.c_ubyte * n)()
    got = C.c_size_t(0)
    if not k32.ReadProcessMemory(h, C.c_void_p(addr), buf, n, C.byref(got)):
        return None
    return bytes(buf[:got.value])


def u32(h, addr):
    raw = read(h, addr, 4)
    return None if raw is None else int.from_bytes(raw, "little")


def tstring(h, ptr):
    """读一个 `str::TString<wchar_t>`：指针指向字符数据，长度在 -0x14 处。"""
    if not ptr:
        return ""
    length = u32(h, ptr - 0x14)
    if length is None or not 0 <= length <= 4096:
        return "<坏指针>"
    raw = read(h, ptr, length * 2) or b""
    return raw.decode("utf-16le", "replace")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pid = int(sys.argv[1])
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        print(f"OpenProcess({pid}) 失败: {C.get_last_error()}")
        return 1

    app = u32(h, APP) or 0
    lobby = u32(h, LOBBY_STAGE) or 0
    world = u32(h, WORLD) or 0
    ctx = u32(h, GAME_CONTEXT) or 0

    print(f"App         [{APP:#x}] = {app:#010x}")
    if app:
        print(f"    +0x54 当前 stage = {u32(h, app + 0x54)}"
              f"   (4=大厅 5=房间 6=加载 7=游戏)")
        print(f"    +0x5c 待切 stage = {u32(h, app + 0x5C)}"
              f"   (非 0 = 还没轮到主循环执行，窗口不在前台时会一直挂着)")

    print(f"GameContext [{GAME_CONTEXT:#x}] = {ctx:#010x}")
    if ctx:
        vft = u32(h, ctx) or 0
        name = VFT_NAMES.get(vft, "未知（用 tools/rtti_map.py 反查）")
        print(f"    vft = {vft:#010x}  {name}")

    print(f"World       [{WORLD:#x}] = {world:#010x}")
    if world:
        print(f"    +0x1c6c 已加载地图 = {tstring(h, u32(h, world + 0x1C6C))!r}"
              f"   (待机房间应是 'room-06')")
        print(f"    +0x1d7c 场景标签   = {tstring(h, u32(h, world + 0x1D7C))!r}"
              f"   (待机房间应是 'room')")

    print(f"LobbyStage  [{LOBBY_STAGE:#x}] = {lobby:#010x}")
    if lobby:
        status = u32(h, lobby + 0x04)
        print(f"    +0x04 房间状态 = {status}"
              f"   ({'待机中 ✓' if status == 2 else '≠2 -> 客户端会建战斗上下文，房间就是黑的'})")
        print(f"    +0x10 地图名   = {tstring(h, u32(h, lobby + 0x10))!r}")
        print(f"    +0x1c descriptor.type = {u32(h, lobby + 0x1C)}"
              f"   (1=普通 2=闯关 5=天梯)")
        print(f"    +0x34 房主座位 = {u32(h, lobby + 0x34)}"
              f"   +0x1cc 我的座位 = {u32(h, lobby + 0x1CC)}")
        for i in range(SEAT_COUNT):
            seat = lobby + SEAT_BASE + i * SEAT_STRIDE
            flags = read(h, seat, 2) or b"\0\0"
            obj = u32(h, lobby + 0x1D0 + i * 4) or 0
            print(f"    seat[{i}] 占用={flags[0]} 关闭={flags[1]} "
                  f"昵称={tstring(h, u32(h, seat + 0x04))!r} "
                  f"角色id={u32(h, seat + 0x0C)} "
                  f"等级={(u32(h, seat + 0x10) or 0) & 0xFFFF} "
                  f"角色对象={obj:#010x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
