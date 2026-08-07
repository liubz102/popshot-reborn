#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_idle_timer.py —— 读客户端里那个「90 秒没动作就踢回大厅」的计时器

来源（FINDINGS §101）：

    RoomStage::Update 0x46762f 附近
        eax = [0x72e29c]                 ← LobbyStage
        ecx = eax + 0x3e8                ← Timer 对象
        call 0x5d5ecc                    ← Timer::IsExpired()  = elapsed >= duration
        ...  弹「1분 30초 이상 동작이 없어 로비로 돌아갑니다.」(0x66a9e0)

    0x4082ae  LobbyStage::ResetIdleTimer():  push 0x15f90(=90000ms); Timer::Start()

Timer 布局（0x5d5e37 Timer::Start 逐条读出来的）：

    +0x00  vptr        vtable[1] = GetTick()，返回毫秒整数
    +0x04  duration    0 = 停用（IsExpired 恒 false）
    +0x08  start       Start() 时的 tick
    +0x0c  deadline    start + duration

用法：
    python tools/probe_idle_timer.py <pid>              # 打一次快照
    python tools/probe_idle_timer.py <pid> --watch 2    # 每 2 秒打一行，盯 start 变没变

**盯 `start` 有没有变**就知道计时器有没有被重置。重置点只有四个
（`0x4082ae` 的调用方）：窗口过程收到 WM_LBUTTONUP/WM_MBUTTONUP/WM_RBUTTONUP/
WM_KEYUP（0x40ee3d）、`LobbyStage::ResetSession` 0x4054fa（0x40563b）、
弹完提示框自己重置（0x4676cb）、战斗中 0x4906ac。**没有任何一个是收包触发的。**
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
k32.GetTickCount.restype = W.DWORD

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

LOBBY_STAGE = 0x72E29C       # 登录后建的大厅对象
APP_CONTEXT = 0x72E2A4       # +0x54 = 当前 stage id（4 大厅 / 5 房间 / 6 加载 / 7 游戏）
IDLE_TIMER = 0x3E8           # LobbyStage + 0x3e8
IDLE_MS = 90000              # 0x4082ae 的 push 0x15f90（bshook patch 后是 0x40000000）

#: 引擎时钟对象的指针（`0x409ff3: mov ecx,[0x6d8a18]`）。
#: `0x5d72b4` 算的是 `(GetTickCount() - [clk+4]) * [clk+0xc]`，
#: 也就是**进程相对毫秒**，不是系统 uptime —— 直接拿 GetTickCount 减 start 会差出几小时。
CLOCK_PTR = 0x6D8A18

STAGE_NAMES = {4: "大厅", 5: "房间", 6: "准备/加载", 7: "游戏"}


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


def snapshot(h):
    lobby = u32(h, LOBBY_STAGE)
    app = u32(h, APP_CONTEXT)
    out = {"lobby": lobby, "app": app, "stage": None,
           "vptr": None, "duration": None, "start": None, "deadline": None,
           "now": None}
    clock = u32(h, CLOCK_PTR)
    if clock:
        base = u32(h, clock + 4)
        if base is not None:
            out["now"] = (k32.GetTickCount() - base) & 0xFFFFFFFF
    if app:
        out["stage"] = i32(h, app + 0x54)
    if lobby:
        t = lobby + IDLE_TIMER
        out["vptr"] = u32(h, t)
        out["duration"] = i32(h, t + 4)
        out["start"] = i32(h, t + 8)
        out["deadline"] = i32(h, t + 0xC)
    return out


def fmt(s, prev=None):
    stage = s["stage"]
    stage_txt = f"{stage}({STAGE_NAMES.get(stage, '?')})" if stage is not None else "?"
    if s["lobby"] is None or s["lobby"] == 0:
        return f"LobbyStage=NULL  stage={stage_txt}"
    elapsed = None
    if s["start"] is not None and s["now"] is not None:
        elapsed = s["now"] - s["start"]
    changed = ""
    if prev is not None and prev["start"] != s["start"]:
        changed = "  ★start 变了（计时器被重置）"
    expired = (s["duration"] and elapsed is not None and elapsed >= s["duration"])
    return (f"stage={stage_txt}  LobbyStage={s['lobby']:08x}  "
            f"duration={s['duration']}  start={s['start']}  "
            f"闲置={elapsed}ms"
            f"{'  ← 已超时' if expired else ''}"
            f"{changed}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pid = int(sys.argv[1])
    interval = 0.0
    if "--watch" in sys.argv:
        interval = float(sys.argv[sys.argv.index("--watch") + 1])
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        print(f"OpenProcess({pid}) 失败 err={C.get_last_error()}")
        return 1
    prev = None
    while True:
        s = snapshot(h)
        print(time.strftime("%H:%M:%S"), fmt(s, prev))
        sys.stdout.flush()
        prev = s
        if interval <= 0:
            break
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
