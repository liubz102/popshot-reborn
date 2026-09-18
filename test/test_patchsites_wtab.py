#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自定义武器表钩子（X_Mod · X3）的站点核对 —— `hook/bshook.c` 里写死的地址和字节拿脱壳镜像验一遍。

和 `test_patchsites.py` 同一个道理：钩子打在 exe 的运行时内存上，**打错了没有编译期报错**。
这里能离线钉死的：三个站点的特征字节、偷走的指令边界、「未知 opcode 的默认分支真的只是
`xor al,al`」、以及模式判据用的四个返回地址前面**确实是 `call 0x48b50d`**。
"""
import os
import struct
import unittest

import test_patchsites as base

HERE = os.path.dirname(os.path.abspath(__file__))


class WeaponTableHookTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        for path in (base.BSHOOK, base.IMG):
            if not os.path.isfile(path):
                raise unittest.SkipTest("不在源码仓库里（缺 %s），跳过" % path)
        cls.img = base.load_image()
        cls.src = base.c_source()
        cls.dispatch = base.c_define(cls.src, "WTAB_DISPATCH_VA")
        cls.load = base.c_define(cls.src, "WTAB_LOAD_VA")
        cls.lookup = base.c_define(cls.src, "WTAB_LOOKUP_VA")
        cls.table = base.c_define(cls.src, "WTAB_TABLE_VA")

    def test_the_three_signatures_are_what_the_image_has(self):
        for name, va in (("WTAB_DISPATCH_SIG", self.dispatch), ("WTAB_LOAD_SIG", self.load),
                         ("WTAB_LOOKUP_SIG", self.lookup)):
            sig = base.c_byte_array(self.src, name)
            self.assertEqual(sig, base.read_va(self.img, va, len(sig)), name)

    def test_the_signatures_are_where_we_expect_and_nowhere_surprising(self):
        """Load 的序言全镜像唯一；分发器那 9 字节出现 **2 次** —— 另一处是中继连接上那份
        一模一样的 `ServerConnection` 副本（`0x5568c1` 那一带，V0.3 §222 记过），钩子按
        写死的 VA 装、不按特征串搜，所以两份不冲突。计数变了就说明镜像不是这一版。"""
        self.assertEqual(1, self.img.count(base.c_byte_array(self.src, "WTAB_LOAD_SIG")))
        self.assertEqual(2, self.img.count(base.c_byte_array(self.src, "WTAB_DISPATCH_SIG")))

    def test_the_dispatch_prologue_is_three_whole_instructions(self):
        """`push esi`(1) `mov esi,ecx`(2) `mov ecx,[0x72e29c]`(6) = 9 字节，
        内联钩子偷 >= 5 字节时按指令边界正好偷走这三条，不会切在指令中间。"""
        sig = base.c_byte_array(self.src, "WTAB_DISPATCH_SIG")
        self.assertEqual(9, len(sig))
        self.assertEqual(0x56, sig[0])
        self.assertEqual(bytes([0x8b, 0xf1]), sig[1:3])
        self.assertEqual(bytes([0x8b, 0x0d]), sig[3:5])
        self.assertEqual(0x72e29c, struct.unpack("<I", sig[5:9])[0])

    def test_the_load_prologue_is_exactly_a_five_byte_mov(self):
        sig = base.c_byte_array(self.src, "WTAB_LOAD_SIG")
        self.assertEqual(5, len(sig))
        self.assertEqual(0xb8, sig[0])

    def test_unknown_opcodes_really_fall_into_a_harmless_default(self):
        """`0x54e546: xor al,al / jmp 0x54e565` —— 不装钩子（或老客户端）时 0x0F01 就走到这儿。"""
        self.assertEqual(bytes([0x32, 0xc0, 0xeb]), base.read_va(self.img, 0x54e546, 3))

    def test_the_mode_return_addresses_follow_a_call_to_load(self):
        """四个返回地址前 5 字节都是 `call 0x48b50d`，模式判据靠它们。"""
        for name in ("WTAB_RET_QUEST", "WTAB_RET_PVP", "WTAB_RET_PVP2", "WTAB_RET_BOOT"):
            ret = base.c_define(self.src, name)
            code = base.read_va(self.img, ret - 5, 5)
            self.assertEqual(0xe8, code[0], name)
            target = (ret + struct.unpack("<i", code[1:5])[0]) & 0xFFFFFFFF
            self.assertEqual(self.load, target, "%s 前面那条 call 不是 Load" % name)

    def test_the_lookup_reads_the_table_we_pass(self):
        """`0x4157bf` 从 `[ecx+4]`/`[ecx+8]` 取桶数组，`[eax]` 取 id —— 容器指针和 id 指针传对了才有意义。"""
        code = base.read_va(self.img, self.lookup, 12)
        self.assertEqual(bytes([0x56, 0x8b, 0x30, 0x57, 0x8b, 0x79, 0x08, 0x2b, 0x79, 0x04]), code[:10])
        self.assertEqual(0x72e788, self.table)

    def test_the_field_table_matches_the_server(self):
        """hook 的 12 格顺序 / 类型 == `server/weaponcfg.FIELDS`（那边由 test_weaponcfg 钉着）。"""
        body = self.src[self.src.index("WTAB_FIELD[WTAB_FIELDS] = {"):]
        body = body[:body.index("};")]
        import re
        rows = re.findall(r"\{\s*(0x[0-9a-fA-F]+),\s*([01]),\s*\"(\w+)\"\s*\}", body)
        self.assertEqual([("0x34", "0", "Damage"), ("0x38", "0", "HeadDamage"), ("0x3c", "0", "LegsDamage"),
                          ("0x48", "0", "SplashDamage"), ("0x4c", "0", "SplashRange"), ("0x60", "0", "MagazineCount"),
                          ("0x5c", "0", "CoolingTime"), ("0x64", "0", "ReloadTime"), ("0x58", "0", "LoadingTime"),
                          ("0x24", "1", "Velocity"), ("0x28", "1", "MaxVelocity"), ("0x30", "1", "GravityFactor")],
                         rows)

    def test_the_patch_thread_actually_installs_it(self):
        self.assertGreaterEqual(self.src.count("try_install_weapon_table_hooks"), 2, "定义了但没有人调用")
        self.assertIn("wtab_apply_if_main_thread();", self.src)


if __name__ == "__main__":
    unittest.main()
