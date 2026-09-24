#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_splashteam.py —— 从外部读客户端武器表，看 SplashTeam(+0x54) 到底是几

用途：查「任务模式里溅射伤不伤队友」。`Data/Quest/weapon.ini` 给 114 条玩家武器
写了 SplashTeam=1，闯关进图时由 0x48adb0 叠加进同一张表（X_Mod §55 / §57）。
这个探针直接读活内存，回答「它到底有没有落进记录」。

地址链全部来自 X_Mod §53（客户端没有 ASLR，ImageBase 固定 0x400000）：

    武器表容器 0x72e788：[+4] 桶数组首、[+8] 尾，桶数 = 差/4
    节点 { next(+0), key = 武器Id(+4), 记录指针(+8) }，记录 0x254 B
    当前 stage = [[0x72e2a4] + 0x54]：4 大厅 / 5 待机房 / 6 加载中 / 7 关卡里

字段偏移来自 packet_api §6.7 + V0.3bot §69（+0x54 = SplashTeam，byte）。

    python probe_splashteam.py              # 读一次
    python probe_splashteam.py --watch      # 每 2 秒读一次，进图前后对照
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time

WTAB_CONTAINER = 0x72E788
STAGE_PTR = 0x72E2A4
STAGE_OFF = 0x54
RECORD_SIZE = 0x254

STAGE_NAME = {4: "大厅", 5: "待机房间", 6: "加载中", 7: "★关卡里"}

# 关心的字段：(偏移, 宽度, 名字)
FIELDS = [
    (0x34, 4, "Damage"),
    (0x48, 4, "SplashDamage"),
    (0x4C, 4, "SplashRange"),
    (0x50, 4, "SplashTime"),
    (0x54, 1, "★SplashTeam"),
    (0x60, 4, "Magazine"),
]

# 布洛克(ch02) + 泰尔(ch00) 的对照组
WATCH = [
    (1002010, "ch02-01  布洛克 1号机枪（无溅射）"),
    (1002020, "ch02-02  布洛克 2号（初始，Quest 表里有）"),
    (1002030, "ch02-03  布洛克 3号（初始，Quest 表里有）"),
    (1002225, "ch02-02D3 爆裂3 2号（原版，Quest 表里有）"),
    (1002235, "ch02-03D3 爆裂3 3号（原版，Quest 表里有）"),
    (1002325, "ch02-02C 自定义金 2号（Quest 表里没有）"),
    (1002335, "ch02-03C 自定义金 3号（Quest 表里没有）"),
    (1000020, "ch00-02  泰尔 2号（初始，Quest 表里有）"),
]

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def find_pid(name="BigShot.exe"):
    TH32CS_SNAPPROCESS = 0x2

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    e = ENTRY()
    e.dwSize = ctypes.sizeof(ENTRY)
    out = []
    if k32.Process32First(snap, ctypes.byref(e)):
        while True:
            if e.szExeFile.decode("mbcs", "replace").lower() == name.lower():
                out.append(e.th32ProcessID)
            if not k32.Process32Next(snap, ctypes.byref(e)):
                break
    k32.CloseHandle(snap)
    return out


class Mem:
    def __init__(self, pid):
        self.h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.h:
            raise OSError(f"OpenProcess 失败 pid={pid} err={ctypes.get_last_error()}")

    def read(self, addr, n):
        buf = (ctypes.c_char * n)()
        got = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got))
        if not ok or got.value != n:
            return None
        return bytes(buf)

    def u32(self, addr):
        b = self.read(addr, 4)
        return None if b is None else struct.unpack("<I", b)[0]


def walk_table(mem):
    """遍历哈希链，返回 {武器Id: 记录指针}。"""
    first = mem.u32(WTAB_CONTAINER + 4)
    last = mem.u32(WTAB_CONTAINER + 8)
    if not first or not last or last <= first:
        return {}
    nbuckets = (last - first) // 4
    if not (0 < nbuckets < 100000):
        return {}
    table = {}
    for i in range(nbuckets):
        node = mem.u32(first + i * 4)
        guard = 0
        while node and guard < 10000:
            guard += 1
            head = mem.read(node, 12)
            if head is None:
                break
            nxt, key, rec = struct.unpack("<III", head)
            if rec:
                table[key] = rec
            node = nxt
    return table


def stage_of(mem):
    p = mem.u32(STAGE_PTR)
    if not p:
        return None
    return mem.u32(p + STAGE_OFF)


def dump(mem, once=True):
    st = stage_of(mem)
    table = walk_table(mem)
    print(f"stage = {st} ({STAGE_NAME.get(st, '?')})   武器表 {len(table)} 条")
    if not table:
        print("  （武器表还没加载）")
        return
    for wid, label in WATCH:
        rec = table.get(wid)
        if not rec:
            print(f"  {wid}  {label}\n      <表里没有这一条>")
            continue
        blob = mem.read(rec, RECORD_SIZE)
        if blob is None:
            print(f"  {wid}  {label}\n      <记录读不到>")
            continue
        parts = []
        for off, width, name in FIELDS:
            if width == 4:
                v = struct.unpack_from("<i", blob, off)[0]
            else:
                v = blob[off]
            parts.append(f"{name}={v}")
        print(f"  {wid}  {label}")
        print("      " + "  ".join(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="每 2 秒读一次，直到 Ctrl-C")
    ap.add_argument("--pid", type=int, default=0)
    args = ap.parse_args()

    pid = args.pid
    if not pid:
        pids = find_pid()
        if not pids:
            print("没找到 BigShot.exe —— 先把游戏跑起来")
            return 1
        pid = pids[0]
        if len(pids) > 1:
            print(f"⚠ 有 {len(pids)} 个 BigShot.exe，用第一个 {pid}")
    print(f"pid = {pid}")
    mem = Mem(pid)
    if not args.watch:
        dump(mem)
        return 0
    last = None
    while True:
        st = stage_of(mem)
        if st != last:
            print(f"\n===== {time.strftime('%H:%M:%S')} stage 变成 {st} =====")
            dump(mem)
            last = st
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
