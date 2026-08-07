#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
thread_stacks.py —— 从外部采样一个 32 位进程所有线程的 EIP + EBP 回溯

为什么要这个：客户端登录成功后卡在一个不泵消息的忙等循环里（FINDINGS §49）。
想知道「卡在哪一行」，以前的办法是给 bshook 加 hook 再重编（D013）。
但只要能拿到线程的 EIP 和 EBP 链，从外部采样就够了 —— 完全脚本化，不用改客户端。

64 位 Python 读 32 位（WOW64）进程，必须用 `Wow64GetThreadContext` + WOW64_CONTEXT。

用法：
    python tools/thread_stacks.py <pid>            # 采样一次
    python tools/thread_stacks.py <pid> 5 0.5      # 采样 5 次，间隔 0.5 秒
        多次采样能区分「真的卡死在一点」和「在一个循环里转」。
"""
import ctypes as C
import sys
import time
from ctypes import wintypes as W

k32 = C.WinDLL("kernel32", use_last_error=True)

PROCESS_ALL_ACCESS = 0x1F0FFF
THREAD_ALL_ACCESS = 0x1F03FF
TH32CS_SNAPTHREAD = 0x00000004
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
WOW64_CONTEXT_FULL = 0x00010007

# WOW64_CONTEXT（x86 CONTEXT）里我们要的几个字段的偏移
OFF_EDI, OFF_ESI, OFF_EBX = 0x9C, 0xA0, 0xA4
OFF_EDX, OFF_ECX, OFF_EAX = 0xA8, 0xAC, 0xB0
OFF_EBP, OFF_EIP, OFF_ESP = 0xB4, 0xB8, 0xC4
CONTEXT_SIZE = 0x2CC


class THREADENTRY32(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD),
                ("th32ThreadID", W.DWORD), ("th32OwnerProcessID", W.DWORD),
                ("tpBasePri", C.c_long), ("tpDeltaPri", C.c_long),
                ("dwFlags", W.DWORD)]


class MODULEENTRY32(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("th32ModuleID", W.DWORD),
                ("th32ProcessID", W.DWORD), ("GlblcntUsage", W.DWORD),
                ("ProccntUsage", W.DWORD), ("modBaseAddr", C.POINTER(C.c_byte)),
                ("modBaseSize", W.DWORD), ("hModule", W.HMODULE),
                ("szModule", C.c_char * 256), ("szExePath", C.c_char * 260)]


def list_threads(pid):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    te = THREADENTRY32()
    te.dwSize = C.sizeof(te)
    out = []
    if k32.Thread32First(snap, C.byref(te)):
        while True:
            if te.th32OwnerProcessID == pid:
                out.append(te.th32ThreadID)
            if not k32.Thread32Next(snap, C.byref(te)):
                break
    k32.CloseHandle(snap)
    return out


def list_modules(pid):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    me = MODULEENTRY32()
    me.dwSize = C.sizeof(me)
    out = []
    if k32.Module32First(snap, C.byref(me)):
        while True:
            base = C.cast(me.modBaseAddr, C.c_void_p).value or 0
            out.append((base, base + me.modBaseSize, me.szModule.decode("latin1")))
            if not k32.Module32Next(snap, C.byref(me)):
                break
    k32.CloseHandle(snap)
    return sorted(out)


def whose(mods, addr):
    for lo, hi, name in mods:
        if lo <= addr < hi:
            return f"{name}+{addr - lo:#x}"
    return "?"


def read(hproc, addr, n):
    buf = C.create_string_buffer(n)
    got = C.c_size_t(0)
    if not k32.ReadProcessMemory(hproc, C.c_void_p(addr), buf, n, C.byref(got)):
        return None
    return buf.raw[:got.value]


def sample(pid, hproc, mods, maxdepth=24):
    k32.Wow64GetThreadContext.argtypes = [W.HANDLE, C.c_void_p]
    for tid in list_threads(pid):
        hthr = k32.OpenThread(THREAD_ALL_ACCESS, False, tid)
        if not hthr:
            continue
        k32.SuspendThread(hthr)
        ctx = C.create_string_buffer(CONTEXT_SIZE + 16)
        # ContextFlags 必须先填
        C.memmove(ctx, C.byref(C.c_uint32(WOW64_CONTEXT_FULL)), 4)
        ok = k32.Wow64GetThreadContext(hthr, C.byref(ctx))
        if not ok:
            k32.ResumeThread(hthr)
            k32.CloseHandle(hthr)
            continue
        raw = ctx.raw
        u32 = lambda off: int.from_bytes(raw[off:off + 4], "little")
        eip, ebp, esp = u32(OFF_EIP), u32(OFF_EBP), u32(OFF_ESP)
        print(f"  tid={tid:<6} EIP={eip:08x} [{whose(mods, eip)}]  "
              f"EBP={ebp:08x} ESP={esp:08x}  "
              f"eax={u32(OFF_EAX):08x} ecx={u32(OFF_ECX):08x} "
              f"edx={u32(OFF_EDX):08x} esi={u32(OFF_ESI):08x} edi={u32(OFF_EDI):08x}")
        # EBP 链回溯
        cur, depth = ebp, 0
        while cur and depth < maxdepth:
            fr = read(hproc, cur, 8)
            if not fr or len(fr) < 8:
                break
            nxt = int.from_bytes(fr[0:4], "little")
            ret = int.from_bytes(fr[4:8], "little")
            if not ret:
                break
            print(f"        ret={ret:08x}  [{whose(mods, ret)}]")
            if nxt <= cur:
                break
            cur = nxt
            depth += 1
        k32.ResumeThread(hthr)
        k32.CloseHandle(hthr)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    pid = int(sys.argv[1])
    times = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    gap = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    hproc = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not hproc:
        print(f"OpenProcess 失败: {C.get_last_error()}")
        return
    mods = list_modules(pid)
    print(f"pid={pid} 模块 {len(mods)} 个，主模块 = {mods[0][2] if mods else '?'}")
    for i in range(times):
        print(f"--- 采样 #{i + 1} ---")
        sample(pid, hproc, mods)
        if i + 1 < times:
            time.sleep(gap)
    k32.CloseHandle(hproc)


if __name__ == "__main__":
    main()
