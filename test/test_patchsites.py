#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字节补丁的**站点**核对：`hook/bshook.c` 里写死的地址和字节，拿脱壳镜像验一遍。

为什么值得单开一个模块：补丁打在 exe 的运行时内存上，**打错了没有编译期报错**
—— 最好的情况是补丁不生效（特征串对不上，日志里一行 `!!`），最坏的情况是
跳进指令中间，随机崩在别处。而「特征串在不在、唯一不唯一、跳转落在哪」
全部是**离线可判定**的：`re/BigShot_22524.img` 是拉平的内存镜像，
**文件偏移 == VA − 0x400000**。

目前只收 **索引缓冲空指针判据**（§35 / D25）。挑它先做，是因为它是本仓库
第一个**跳板式**补丁（站点塞不下，要跳到 VirtualAlloc 的代码洞里），
而且它的触发条件是「D3D 设备丢失」—— 实机上没法按需复现，
所以能离线钉死的每一条都要钉死。

⚠ 依赖仓库布局（`hook/`、`re/`），发布包里没有它们，自动跳过。
"""
import os
import re
import struct
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BSHOOK = os.path.join(ROOT, "hook", "bshook.c")
IMG = os.path.join(ROOT, "re", "BigShot_22524.img")
IMAGE_BASE = 0x400000


def load_image():
    with open(IMG, "rb") as f:
        return f.read()


def read_va(img, va, n):
    off = va - IMAGE_BASE
    assert 0 <= off <= len(img) - n, "VA %08X 落在镜像外" % va
    return img[off:off + n]


def c_source():
    with open(BSHOOK, "r", encoding="utf-8") as f:
        return f.read()


def c_define(src, name):
    """抠 `#define <name> <整数>`（十进制或 0x…，允许 u 后缀）。"""
    m = re.search(r"^#define\s+%s\s+(0[xX][0-9A-Fa-f]+|\d+)[uU]?\s*(?:/\*|$)"
                  % re.escape(name), src, re.M)
    if not m:
        raise AssertionError("bshook.c 里找不到 #define %s" % name)
    return int(m.group(1), 0)


def c_byte_array(src, name):
    """抠 `static const unsigned char <name>[..] = { … };` 里的字节。

    ★ 要先把 C 注释去掉：这几张表的注释里全是 `mov [ebp+0x54], eax` 这种，
      里头的 `0x54` 会被当成表里的一项。
    """
    m = re.search(r"unsigned char\s+%s\s*\[[^\]]*\]\s*=\s*\{(.*?)\};"
                  % re.escape(name), src, re.S)
    if not m:
        raise AssertionError("bshook.c 里找不到数组 %s" % name)
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return bytes(int(v, 0) for v in re.findall(r"0[xX][0-9A-Fa-f]+|\b\d+\b", body))


class D3dIndexBufferPatchTest(unittest.TestCase):
    """§35 / D25 —— `IDirect3DIndexBuffer9::Lock()` 失败时别往 NULL 里 memcpy。"""

    @classmethod
    def setUpClass(cls):
        for path in (BSHOOK, IMG):
            if not os.path.isfile(path):
                raise unittest.SkipTest("不在源码仓库里（缺 %s），跳过" % path)
        cls.img = load_image()
        cls.src = c_source()
        cls.sig_va = c_define(cls.src, "D3D_IB_SIG_VA")
        cls.patch_off = c_define(cls.src, "D3D_IB_PATCH_OFF")
        cls.patch_len = c_define(cls.src, "D3D_IB_PATCH_LEN")
        cls.resume_va = c_define(cls.src, "D3D_IB_RESUME_VA")
        cls.bail_va = c_define(cls.src, "D3D_IB_BAIL_VA")
        cls.sig = c_byte_array(cls.src, "D3D_IB_SIG")
        cls.cave = c_byte_array(cls.src, "D3D_IB_CAVE")
        cls.bail_off = c_define(cls.src, "D3D_IB_CAVE_BAIL_OFF")
        cls.resume_off = c_define(cls.src, "D3D_IB_CAVE_RESUME_OFF")

    # --- 站点本身 ---------------------------------------------------------
    def test_the_signature_is_what_the_image_really_has(self):
        self.assertEqual(read_va(self.img, self.sig_va, len(self.sig)), self.sig,
                         "D3D_IB_SIG 和镜像对不上 —— 地址或字节抄错了")

    def test_the_signature_is_unique_in_the_whole_image(self):
        # 不唯一 = 这串字节不足以认出「就是这个客户端的这一处」，补丁可能打错地方。
        self.assertEqual(1, self.img.count(self.sig),
                         "D3D_IB_SIG 在镜像里出现 %d 次，不唯一" % self.img.count(self.sig))

    def test_the_stolen_bytes_are_exactly_what_the_patch_overwrites(self):
        # 洞里头 9 个字节必须是站点原样搬过去的三条指令，否则跳板一回去语义就变了。
        site = read_va(self.img, self.sig_va + self.patch_off, self.patch_len)
        self.assertEqual(site, self.cave[:self.patch_len],
                         "代码洞开头没有原样保留被覆盖掉的那 %d 字节" % self.patch_len)

    def test_the_patch_stops_right_before_the_fill_loop(self):
        # 站点多吃一个字节就啃进循环头，回跳会落在指令中间。
        self.assertEqual(self.sig_va + self.patch_off + self.patch_len, self.resume_va,
                         "补丁长度和回跳地址对不上")
        self.assertGreaterEqual(self.patch_len, 5, "放不下 E9 rel32")

    # --- 两个跳转目标确实是指令边界，而且是我们以为的那条指令 ---------------
    def test_the_resume_target_is_the_head_of_the_fill_loop(self):
        # 0x5bf6d9: mov eax,[ebp+0x5c] / xor ecx,ecx
        self.assertEqual(b"\x8b\x45\x5c\x33\xc9", read_va(self.img, self.resume_va, 5))

    def test_the_loop_back_edge_really_lands_on_the_resume_target(self):
        # 循环底部：cmp [ebp+0x60],0xc00 / jl <循环头>。★ 这条同时证明了
        # 「没有别的跳转会落进被覆盖的那 9 个字节」—— 全函数只有这一条往回跳。
        tail = read_va(self.img, self.resume_va, 0x5C)
        idx = tail.index(b"\x81\x7d\x60\x00\x0c\x00\x00")     # cmp [ebp+0x60], 0xc00
        jl_va = self.resume_va + idx + 7
        self.assertEqual(0x7C, self.img[jl_va - IMAGE_BASE], "循环底部不是 jl rel8")
        rel = struct.unpack("<b", self.img[jl_va + 1 - IMAGE_BASE:jl_va + 2 - IMAGE_BASE])[0]
        self.assertEqual(self.resume_va, jl_va + 2 + rel,
                         "循环的回跳目标不是 D3D_IB_RESUME_VA")

    def test_the_bail_target_sits_just_after_the_unlock_call(self):
        # 指针是 NULL 时压根没锁上，所以要跳到 Unlock **之后**：
        #   mov eax,[ebx+0x13c] / mov ecx,[eax] / push eax / call [ecx+0x30]
        unlock = b"\x8b\x83\x3c\x01\x00\x00\x8b\x08\x50\xff\x51\x30"
        self.assertEqual(unlock, read_va(self.img, self.bail_va - len(unlock), len(unlock)),
                         "D3D_IB_BAIL_VA 前面那几条不是 Unlock 序列")
        # 落点本身是 `mov ecx,ebx`（下一句 call 0x5bd247 和索引缓冲无关）
        self.assertEqual(b"\x8b\xcb", read_va(self.img, self.bail_va, 2))

    # --- 代码洞自己的控制流 ------------------------------------------------
    def test_the_cave_is_the_byte_sequence_we_think_it_is(self):
        # ★ 手抄一份对照：改了洞就得在这里同步改一遍，逼着改的人重新想一遍。
        expected = (
            self.cave[:self.patch_len]      # 偷来的三条原指令
            + b"\x83\x7d\x58\x00"           # cmp dword [ebp+0x58], 0   ← Lock 填的指针
            + b"\x75\x05"                   # jne +5 → 第二条 E9
            + b"\xe9\x00\x00\x00\x00"       # jmp BAIL   （rel32 装的时候现算）
            + b"\xe9\x00\x00\x00\x00"       # jmp RESUME （同上）
        )
        self.assertEqual(expected, self.cave)

    def test_the_conditional_skips_exactly_over_the_bail_jump(self):
        jcc = self.patch_len + 4                       # cmp 占 4 字节
        self.assertEqual(0x75, self.cave[jcc], "判据后面不是 jne rel8")
        target = jcc + 2 + self.cave[jcc + 1]
        self.assertEqual(self.bail_off - 1 + 5, target,
                         "jne 没有正好跳过那条 `jmp BAIL`")
        self.assertEqual(0xE9, self.cave[self.bail_off - 1])
        self.assertEqual(0xE9, self.cave[self.resume_off - 1])
        self.assertEqual(len(self.cave), self.resume_off + 4,
                         "代码洞的长度和最后一个 rel32 对不上")

    def test_the_rel32_slots_are_left_blank_in_the_template(self):
        # 模板里必须是 0：真值要按洞的实际地址算，写死任何非零值都是错的。
        for off in (self.bail_off, self.resume_off):
            self.assertEqual(b"\x00\x00\x00\x00", self.cave[off:off + 4])

    # --- 安装侧 ------------------------------------------------------------
    def test_the_installer_refuses_to_patch_twice(self):
        # 反复调 try_patch_* 是常态（patch_thread 轮询）；重入一次就会再
        # VirtualAlloc 一个洞、再把站点覆盖一遍 —— 第二次覆盖的是 E9 自己。
        body = self.src[self.src.index("static int try_patch_d3d_ib_lock(void)"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("g_d3d_ib_patched", body)
        self.assertIn("p[0] == 0xE9", body, "缺少「已经装过了」的判据")

    def test_the_patch_thread_actually_installs_it(self):
        self.assertIn("try_patch_d3d_ib_lock()", self.src)
        self.assertGreaterEqual(self.src.count("try_patch_d3d_ib_lock"), 2,
                                "定义了但没有人调用")


if __name__ == "__main__":
    unittest.main()
