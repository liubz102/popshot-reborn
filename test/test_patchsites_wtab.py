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
        cls.merge = base.c_define(cls.src, "WTAB_MERGE_VA")
        cls.table = base.c_define(cls.src, "WTAB_TABLE_VA")

    def test_the_three_signatures_are_what_the_image_has(self):
        for name, va in (("WTAB_DISPATCH_SIG", self.dispatch), ("WTAB_LOAD_SIG", self.load),
                         ("WTAB_LOOKUP_SIG", self.lookup), ("WTAB_MERGE_SIG", self.merge)):
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

    def test_the_merge_site_is_the_other_way_into_the_weapon_table(self):
        """★★ `0x48adb0` 是**第二条**会改写武器记录的路径（§44）：它加载一份额外的 ini
        并把小节逐条解析进同一张表，**不经过 `WeaponTable::Load`**。
        判据有三条，缺一条这个钩子就白挂：
          ① 它的序言是 5 字节 `mov eax,<imm32>`（SEH 序言），偷得整整齐齐；
          ② 它确实调那个逐节解析函数 `0x488d31`（和 Load 里那个是同一个）；
          ③ 它确实往全局武器表 `WTAB_TABLE_VA` 上写。
        """
        sig = base.c_byte_array(self.src, "WTAB_MERGE_SIG")
        self.assertEqual(5, len(sig))
        self.assertEqual(0xB8, sig[0], "序言不是 mov eax,imm32，偷 5 字节会切断指令")
        body = base.read_va(self.img, self.merge, 0x200)
        # ② call 0x488d31 —— 相对调用，直接扫编码
        want = None
        for off in range(len(body) - 5):
            if body[off] == 0xE8:
                target = self.merge + off + 5 + struct.unpack_from("<i", body, off + 1)[0]
                if target == 0x488D31:
                    want = off
                    break
        self.assertIsNotNone(want, "0x48adb0 里没找到 call 0x488d31（逐节解析）")
        # ③ 引用全局武器表
        self.assertIn(struct.pack("<I", self.table), body,
                      "0x48adb0 里没引用 WTAB_TABLE_VA，站点八成认错了")

    def test_the_parser_has_exactly_two_ways_in(self):
        """★ 逐节解析函数 `0x488d31` 全镜像只有**两个**调用者：`Load` 里那个
        和 `0x48adb0`。再冒出第三个就说明还有一条我们没钩的路 —— 那正是
        2026-09-19 闯关里「PVE 值被冲回 ini 原值」的来路。"""
        callers = []
        for off in range(0, len(self.img) - 5):
            if self.img[off] != 0xE8:
                continue
            va = 0x400000 + off
            if va + 5 + struct.unpack_from("<i", self.img, off + 1)[0] == 0x488D31:
                callers.append(va)
        self.assertEqual([0x48AEF0, 0x48B694], sorted(callers),
                         "weapon.ini 逐节解析的调用者变了：%s" % [hex(c) for c in callers])

    def test_the_lookup_reads_the_table_we_pass(self):
        """`0x4157bf` 从 `[ecx+4]`/`[ecx+8]` 取桶数组，`[eax]` 取 id —— 容器指针和 id 指针传对了才有意义。"""
        code = base.read_va(self.img, self.lookup, 12)
        self.assertEqual(bytes([0x56, 0x8b, 0x30, 0x57, 0x8b, 0x79, 0x08, 0x2b, 0x79, 0x04]), code[:10])
        self.assertEqual(0x72e788, self.table)

    def test_the_field_table_matches_the_server(self):
        """hook 的 14 格顺序 / 类型 == `server/weaponcfg.FIELDS`（那边由 test_weaponcfg 钉着）。"""
        body = self.src[self.src.index("WTAB_FIELD[WTAB_FIELDS] = {"):]
        body = body[:body.index("};")]
        import re
        rows = re.findall(r"\{\s*(0x[0-9a-fA-F]+),\s*([01]),\s*\"(\w+)\"\s*\}", body)
        self.assertEqual([("0x34", "0", "Damage"), ("0x38", "0", "HeadDamage"), ("0x3c", "0", "LegsDamage"),
                          ("0x48", "0", "SplashDamage"), ("0x4c", "0", "SplashRange"), ("0x60", "0", "MagazineCount"),
                          ("0x5c", "0", "CoolingTime"), ("0x64", "0", "ReloadTime"), ("0x58", "0", "LoadingTime"),
                          ("0x24", "1", "Velocity"), ("0x28", "1", "MaxVelocity"), ("0x30", "1", "GravityFactor"),
                          ("0x78", "0", "HomingAngle"), ("0x7c", "1", "HomingRange")],
                         rows)

    def test_the_homing_offsets_and_types_are_what_the_exe_really_uses(self):
        """★★ 追踪两格（X7）的偏移**和类型**拿脱壳镜像离线钉死。

        为什么值得单写一条：`+0x78` 和 `+0x7c` 挨着，而且 ini 里两个值长得一样
        （都是整数字面量），**写反了服务端照样发得出去、hook 照样写得进去**
        —— 只有实机才看得出「追踪距离变成了 3.1e-43」。这三条指令一钉，
        记错偏移或记反类型**离线就红**（§44 的教训：清单式结论漏的是清单外的东西）。

        i32 / f32 从**指令**读出来，不是从注释读出来的：
          `fild dword` 取整数、`fcomp dword` 比的是 f32、`fstp dword` 存 f32。
        """
        # 开关：HomingAngle == 0 就直接跳到函数尾 —— 客户端没有独立的「Homing」开关
        self.assertEqual(bytes([0x83, 0x78, 0x78, 0x00]), base.read_va(self.img, 0x0047E35A, 4),
                         "追踪开关不在 [记录+0x78] 上了")
        self.assertEqual(bytes([0x0F, 0x84]), base.read_va(self.img, 0x0047E35F, 2), "开关后面不是 je")
        # 用：HomingAngle 走 fild（i32）、HomingRange 走 fcomp（f32）
        self.assertEqual(bytes([0xDB, 0x40, 0x78]), base.read_va(self.img, 0x0047E53A, 3),
                         "HomingAngle 不是 `fild dword [eax+0x78]` 了（类型可能不再是 i32）")
        self.assertEqual(bytes([0xD8, 0x58, 0x7C]), base.read_va(self.img, 0x0047E45B, 3),
                         "HomingRange 不是 `fcomp dword [eax+0x7c]` 了（类型可能不再是 f32）")
        # 解析：ini → 记录，同样一格 i32 一格 f32
        self.assertEqual(bytes([0x89, 0x47, 0x78]), base.read_va(self.img, 0x00489450, 3),
                         "解析 HomingAngle 的落点不是 `mov [edi+0x78], eax` 了")
        self.assertEqual(bytes([0xD9, 0x5F, 0x7C]), base.read_va(self.img, 0x0048941E, 3),
                         "解析 HomingRange 的落点不是 `fstp dword [edi+0x7c]` 了")

    def test_homing_runs_for_every_projectile_class(self):
        """★★ `Projectile::Homing`（`0x47e347`）全镜像**只有一个**调用者，
        而且在 `BulletObj::Tick`（`0x47de6a`）里 —— 所有弹体类的 Tick 都会
        走到它（手雷 / 火瓶那些自己有 Tick 的，头几条就 `call 0x47de6a`）。

        这是「**任意**自定义武器都能开追踪」的全部依据。再冒出第二个调用者、
        或者 `AppleGrenade` / `FlamingBottle` 不再转调基类，这条当场红。
        """
        def callers_of(target):
            out = []
            for off in range(0, len(self.img) - 5):
                if self.img[off] != 0xE8:
                    continue
                va = 0x400000 + off
                if va + 5 + struct.unpack_from("<i", self.img, off + 1)[0] == target:
                    out.append(va)
            return sorted(out)

        self.assertEqual([0x47DF56], callers_of(0x47E347),
                         "Projectile::Homing 的调用者变了 —— 追踪的适用范围跟着变")
        base_tick_callers = callers_of(0x47DE6A)
        # 18 把自定义武器只用到这三类弹体：GeneralBullet(= BulletObj 本体) /
        # AppleGrenade（泰尔 2 号）/ FlamingBottle（卡希尔 2 号）。后两类必须转调基类。
        for name, va in (("AppleGrenade::Tick", 0x47C91D), ("FlamingBottle::Tick", 0x482980)):
            self.assertIn(va, base_tick_callers, "%s 不再转调 BulletObj::Tick" % name)

    def test_the_patch_thread_actually_installs_it(self):
        self.assertGreaterEqual(self.src.count("try_install_weapon_table_hooks"), 2, "定义了但没有人调用")
        self.assertIn("wtab_apply_if_main_thread();", self.src)


if __name__ == "__main__":
    unittest.main()
