#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_quest_list.py —— 读活着的客户端里的地图/关卡目录

「建立房间(任务)」的关卡下拉框空着时，要先分清是客户端本地目录本身没有
关卡记录，还是记录在但被过滤条件挡掉了。

目录是 `[0x72e3d8]` 指向的一棵红黑树（STLport set/map）：
    节点 +0x04 parent  +0x08 left  +0x0c right  +0x14 记录指针
    `[树头+8]` = 最左节点 = begin()，遍历到回到树头为止（`0x5e4040` 是 ++）

记录字段（由填充循环 `0x4368b3` 和查表 `0x40b6a2` 反推）：
    +0x10  名字（宽字符串对象，第 1 个 dword 是字符指针）
           类型判定 `0x40b1fb(rec, 2)` = 名字里含 "Quest"
    +0x28  最少人数（填充循环要求 当前人数 >= 它）
    +0x40  关卡 id，要等于 `0x6dc52c` 表里的 3/2/1/4/5/6/7 之一
    +0x48  掩码，要求 `mask & (1 << [[0x72e320]])` 非 0

用法：
    python tools/probe_quest_list.py <pid> [最多列几条]
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

CATALOG = 0x72E3D8
STATE_MACHINE = 0x72E320
QUEST_IDS = (3, 2, 1, 4, 5, 6, 7)      # 静态表 0x6dc52c


class Mem:
    def __init__(self, pid):
        self.h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
                                 False, pid)
        if not self.h:
            raise OSError(f"OpenProcess({pid}) 失败: {C.get_last_error()}")

    def read(self, addr, n):
        if not addr:
            return None
        buf = (C.c_ubyte * n)()
        got = C.c_size_t()
        if not k32.ReadProcessMemory(self.h, W.LPCVOID(addr), buf, n, C.byref(got)):
            return None
        return bytes(buf[:got.value])

    def u32(self, addr):
        b = self.read(addr, 4)
        return None if b is None else int.from_bytes(b, "little")

    def i32(self, addr):
        v = self.u32(addr)
        return None if v is None else (v - (1 << 32) if v >= (1 << 31) else v)

    def wstr(self, addr, maxchars=64):
        raw = self.read(addr, maxchars * 2)
        if not raw:
            return None
        end = raw.find(b"\x00\x00")
        if end < 0:
            end = len(raw)
        elif end % 2:
            end += 1
        return raw[:end].decode("utf-16le", "replace")


def rb_next(mem, node):
    """红黑树 ++，与 0x5e4040 逐分支等价。"""
    right = mem.u32(node + 0x0c)
    if right:
        node = right
        while True:
            left = mem.u32(node + 8)
            if not left:
                return node
            node = left
    parent = mem.u32(node + 4)
    while parent is not None and mem.u32(parent + 0x0c) == node:
        node = parent
        parent = mem.u32(parent + 4)
    return parent


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    pid = int(sys.argv[1])
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    mem = Mem(pid)

    state = mem.u32(STATE_MACHINE)
    locale = mem.u32(state) if state else None
    print(f"[0x{STATE_MACHINE:08x}] = {state and hex(state)}  首字段(掩码位) = {locale}")

    head = mem.u32(CATALOG)
    print(f"[0x{CATALOG:08x}] = {head and hex(head)}")
    if not head:
        print("目录指针为空 —— 客户端还没建目录")
        return

    node = mem.u32(head + 8)
    total = 0
    quests = []
    while node and node != head and total < limit:
        rec = mem.u32(node + 0x14)
        if rec:
            name = mem.wstr(mem.u32(rec + 0x10) or 0) or ""
            qid = mem.i32(rec + 0x40)
            if "Quest" in name:
                quests.append((name, qid, mem.i32(rec + 0x28), mem.u32(rec + 0x48)))
        total += 1
        node = rb_next(mem, node)

    print(f"目录里共 {total} 条记录，其中名字含 'Quest' 的 {len(quests)} 条：")
    for name, qid, minplayers, mask in quests:
        ok = qid in QUEST_IDS
        bit = None if locale is None or mask is None else bool(mask & (1 << locale))
        print(f"   {name:38s} id={qid:<5} 最少人数={minplayers:<4} "
              f"掩码={mask if mask is None else hex(mask)}  "
              f"在下拉表={'是' if ok else '否'} 掩码通过={bit}")


if __name__ == "__main__":
    main()
