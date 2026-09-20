#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""座位取值器那 3 个字节：**把改前改后的机器码都真跑一遍**，逐个输入对比。

为什么不满足于「肉眼看等价」（bug调查/25，D51）：

`0x4045f9` 有 **97 个调用点**，是大厅 / 房间 UI 的热路径。补法是原地改 3 个
字节，靠的是「`jl`(负数) + `jge`(≥6) 两条检查，换成一条**无符号** `jae` 就够」
这个推导。推导错一位，97 个调用点一起错，而且**症状是随机的**（某个座位读到
隔壁的字节），不会当场报错。

那段代码正好是**位置无关**的（两条 jcc 都是 rel8、没有绝对地址），所以可以
`VirtualAlloc` 出来直接调用。⇒ 把「我觉得等价」变成「全输入跑过、逐个相等」。

⚠ 只能在 **32 位 Windows Python** 上跑（`runtime-win7\\python`，3.8.10 win32）。
  64 位那套自动跳过 —— 别删这个跳过，也别为它去写 64 位版本：要验的就是
  **那段 32 位机器码本身**。
"""
import ctypes
import os
import re
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BSHOOK = os.path.join(ROOT, "hook", "bshook.c")

IS_X86_WINDOWS = (sys.platform == "win32" and struct.calcsize("P") == 4)

#: `this` 指向的那张表：6 条 `0x3c` 字节的记录，从 `+0x40` 起。
SEAT_STRIDE = 0x3C
SEAT_BASE = 0x40
SEAT_COUNT = 6
TABLE_SIZE = SEAT_BASE + SEAT_STRIDE * SEAT_COUNT

#: 试遍：合法下标、两端越界、负数、两个极值。
INDICES = [-0x80000000, -2, -1, 0, 1, 2, 3, 4, 5, 6, 7, 99, 0x7FFFFFFF]


def c_byte_array(src, name):
    m = re.search(r"unsigned char\s+%s\s*\[[^\]]*\]\s*=\s*\{(.*?)\};"
                  % re.escape(name), src, re.S)
    assert m, name
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return bytes(int(v, 0) for v in re.findall(r"0[xX][0-9A-Fa-f]+|\b\d+\b", body))


def c_define(src, name):
    m = re.search(r"^#define\s+%s\s+(0[xX][0-9A-Fa-f]+|\d+)[uU]?\s*(?:/\*|$)"
                  % re.escape(name), src, re.M)
    assert m, name
    return int(m.group(1), 0)


class _Code:
    """把一段位置无关的 x86 机器码放进可执行内存，按 `(this, idx)` 调用。

    原函数是 `this=ecx` / `idx=eax` 的怪约定，前面垫一个 cdecl 壳转一下。
    """

    SHIM = (b"\x8b\x4c\x24\x04"      # mov ecx,[esp+4]   this
            b"\x8b\x44\x24\x08"      # mov eax,[esp+8]   idx
            b"\xe8\x01\x00\x00\x00"  # call +1 -> 壳的正后面（壳长 14，下一条在 13）
            b"\xc3")                 # ret               （cdecl，调用方清栈）

    def __init__(self, body):
        # ★ 自检：`call rel32` 必须正好落在壳的后面。差一点就会从函数**中间**
        #   开始执行，而且照样跑得下去 —— 2026-09-20 真踩过：rel 写成 3，
        #   原版碰巧蒙对、打过补丁的那份全返回 0，差点被当成补丁写错了。
        rel = int.from_bytes(self.SHIM[9:13], "little")
        assert 13 + rel == len(self.SHIM), "壳里的 call rel32 和壳长对不上"
        blob = self.SHIM + body
        self.size = len(blob)
        k32 = ctypes.windll.kernel32
        k32.VirtualAlloc.restype = ctypes.c_void_p
        k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.c_uint32, ctypes.c_uint32]
        self.mem = k32.VirtualAlloc(None, self.size, 0x3000, 0x40)  # COMMIT|RESERVE, RWX
        assert self.mem, "VirtualAlloc 失败"
        ctypes.memmove(self.mem, blob, self.size)
        proto = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
        self.call = proto(self.mem)

    def free(self):
        if self.mem:
            ctypes.windll.kernel32.VirtualFree(ctypes.c_void_p(self.mem), 0, 0x8000)
            self.mem = None


@unittest.skipUnless(IS_X86_WINDOWS, "要 32 位 Windows Python（runtime-win7）才跑得了这段机器码")
@unittest.skipUnless(os.path.isfile(BSHOOK), "不在源码仓库里，跳过")
class SeatGetterEquivalenceTests(unittest.TestCase):
    """`0x4045f9`：改前 / 改后，全输入逐个对比。"""

    @classmethod
    def setUpClass(cls):
        src = open(BSHOOK, encoding="utf-8").read()
        cls.before = c_byte_array(src, "SEATGET_SIG")
        after = bytearray(cls.before)
        for off, want in ((1, 0xC9), (2, 0x74), (7, 0x73)):
            after[off] = want
        cls.after = bytes(after)
        cls.orig = _Code(cls.before)
        cls.patched = _Code(cls.after)
        # 6 个座位，第 i 个的那一格填 0xA0+i，好认。
        cls.table = ctypes.create_string_buffer(TABLE_SIZE)
        for i in range(SEAT_COUNT):
            cls.table[SEAT_BASE + i * SEAT_STRIDE] = bytes([0xA0 + i])
        cls.addr = ctypes.cast(cls.table, ctypes.c_void_p)

    @classmethod
    def tearDownClass(cls):
        cls.orig.free()
        cls.patched.free()

    def test_the_patch_is_exactly_as_long_as_the_original(self):
        self.assertEqual(len(self.before), len(self.after))

    def test_a_real_object_gives_byte_for_byte_the_same_answer(self):
        # `this` 非空时两者必须**完全一样** —— 合法下标、越界、负数全试。
        for idx in INDICES:
            with self.subTest(idx=idx):
                self.assertEqual(self.orig.call(self.addr, idx),
                                 self.patched.call(self.addr, idx))

    def test_the_valid_seats_still_read_their_own_byte(self):
        for i in range(SEAT_COUNT):
            with self.subTest(seat=i):
                self.assertEqual(0xA0 + i, self.patched.call(self.addr, i))

    def test_out_of_range_indices_still_return_zero(self):
        for idx in (-0x80000000, -2, -1, 6, 7, 99, 0x7FFFFFFF):
            with self.subTest(idx=idx):
                self.assertEqual(0, self.patched.call(self.addr, idx))

    def test_a_null_this_returns_zero_instead_of_reading_address_0x40(self):
        # ★ 这就是补它的全部理由。改之前跑这一条会 ACCESS_VIOLATION，
        #   所以**只跑打过补丁的那份**。
        for idx in INDICES:
            with self.subTest(idx=idx):
                self.assertEqual(0, self.patched.call(None, idx))


@unittest.skipUnless(IS_X86_WINDOWS, "要 32 位 Windows Python（runtime-win7）才跑得了这段机器码")
@unittest.skipUnless(os.path.isfile(BSHOOK), "不在源码仓库里，跳过")
class SeatPtrEquivalenceTests(unittest.TestCase):
    """`0x404d42`：同一个改法的另一处。它是 `idx=ecx` / `base=edx`，返回指针。"""

    #: 这一处的怪约定要换个壳：**edx=base、ecx=idx**。调用点仍写成
    #: `call(base, idx)`，和上面那组的 `call(this, idx)` 排列一致。
    SHIM = (b"\x8b\x54\x24\x04"      # mov edx,[esp+4]   base
            b"\x8b\x4c\x24\x08"      # mov ecx,[esp+8]   idx
            b"\xe8\x01\x00\x00\x00"  # call +1
            b"\xc3")

    @classmethod
    def setUpClass(cls):
        src = open(BSHOOK, encoding="utf-8").read()
        cls.before = c_byte_array(src, "SEATPTR_SIG")
        after = bytearray(cls.before)
        for off, want in ((1, 0xD2), (2, 0x74), (7, 0x73)):
            after[off] = want
        cls.after = bytes(after)
        saved = _Code.SHIM
        try:
            _Code.SHIM = cls.SHIM
            cls.orig = _Code(cls.before)
            cls.patched = _Code(cls.after)
        finally:
            _Code.SHIM = saved
        cls.table = ctypes.create_string_buffer(TABLE_SIZE)
        cls.base = ctypes.cast(cls.table, ctypes.c_void_p).value

    @classmethod
    def tearDownClass(cls):
        cls.orig.free()
        cls.patched.free()

    def test_a_real_base_gives_the_same_pointer_as_before(self):
        for idx in INDICES:
            with self.subTest(idx=idx):
                self.assertEqual(self.orig.call(self.base, idx) & 0xFFFFFFFF,
                                 self.patched.call(self.base, idx) & 0xFFFFFFFF)

    def test_valid_seats_point_at_the_right_record(self):
        for i in range(SEAT_COUNT):
            with self.subTest(seat=i):
                self.assertEqual((self.base + SEAT_BASE + i * SEAT_STRIDE) & 0xFFFFFFFF,
                                 self.patched.call(self.base, i) & 0xFFFFFFFF)

    def test_a_null_base_returns_zero_instead_of_a_small_bogus_pointer(self):
        # 改之前：base 为 0 时照样算出 `0x40 + idx*0x3c` 这种小地址交出去，
        # 调用方一解引用就崩。现在它遵守自己「座位无效就返回 0」的契约。
        for idx in range(SEAT_COUNT):
            with self.subTest(idx=idx):
                self.assertNotEqual(0, self.orig.call(0, idx) & 0xFFFFFFFF,
                                    "原版在 base=0 时居然返回了 0？结论要重核")
                self.assertEqual(0, self.patched.call(0, idx) & 0xFFFFFFFF)


if __name__ == "__main__":
    unittest.main()
