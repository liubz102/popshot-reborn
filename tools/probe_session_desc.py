#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_session_desc.py —— 轮询 LobbyStage 里的 SessionDescriptor

FINDINGS §64 的核心疑点：建闯关房之后客户端走了 PvP 分支并崩溃，怀疑此刻
`[LobbyStage+0x1c]`（descriptor.type）是 0 而不是 2。客户端进房约 5 秒后就崩，
所以要在这个窗口里连续采样。

    [0x72e29c] = LobbyStage
        +0x18  SessionDescriptor 的 vftable（应为 0x65e09c）
        +0x1c  descriptor.type   ← 1=普通 2=闯关 5=天梯
        +0x20  args[0]
        +0x24  args[1]
        +0x28  args[2]
        +0x1c8 session id（来自 gspRepCreateSession 第二个 int32）
        +0x1cc 我的座位号        +0x34 房主座位号

用法：
    python tools/probe_session_desc.py <pid> [秒数] [采样间隔秒]
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

LOBBY_STAGE = 0x72E29C
DESC_VFT = 0x65E09C
TYPE_NAMES = {0: "未设置", 1: "普通", 2: "闯关", 5: "天梯"}


def u32(h, addr):
    buf = (C.c_ubyte * 4)()
    got = C.c_size_t()
    if not addr or not k32.ReadProcessMemory(h, W.LPCVOID(addr), buf, 4, C.byref(got)):
        return None
    return int.from_bytes(bytes(buf), "little")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    pid = int(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    step = float(sys.argv[3]) if len(sys.argv) > 3 else 0.2

    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        print(f"OpenProcess({pid}) 失败: {C.get_last_error()}")
        return

    last = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        lobby = u32(h, LOBBY_STAGE)
        if lobby:
            vft = u32(h, lobby + 0x18)
            t = u32(h, lobby + 0x1c)
            snap = (vft, t,
                    u32(h, lobby + 0x20), u32(h, lobby + 0x24),
                    u32(h, lobby + 0x1c8), u32(h, lobby + 0x1cc),
                    u32(h, lobby + 0x34))
        else:
            snap = None
        if snap != last:
            ts = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"
            if snap is None:
                print(f"[{ts}] LobbyStage 还没建（[0x72e29c] = 0）")
            else:
                vft, t, a0, a1, sid, me, host = snap
                ok = "✓" if vft == DESC_VFT else "✗"
                name = TYPE_NAMES.get(t, "?")
                print(f"[{ts}] LobbyStage={lobby:#x} desc.vft={vft:#010x}{ok} "
                      f"type={t} ({name}) args=({a0}, {a1}) "
                      f"session_id={sid} me={me} host={host}")
            last = snap
        time.sleep(step)
    print("采样结束")


if __name__ == "__main__":
    main()
