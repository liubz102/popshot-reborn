#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`0x0F02` 仓库提示框说明文表（X_Mod · X10）。

游戏里那个提示框只有 5 行，装不下 PVE + PVP 两套数值 ⇒ 一次只画一套，
**画哪一套按玩家现在在哪走**（用户 2026-09-20）：

* 大厅的商店页 / 仓库页 —— 这一局是闯关还是对战还没定 ⇒ 画 PVE；
* 待机房间里那个快速换装的仓库 —— 模式已由房间定死 ⇒ 画房间那一套。

仓库提示框的文字来自客户端缓存住的 `ItemInfo+0x18`（重发 `0x0501` 刷不掉，§39），
所以这一套由服务端算好、经 `0x0F02` 推给 bshook，由它在绘制点现换。

本文件钉三样：
1. 线格式能编能解，key 是**物品 id**（不是 `0x0F01` 那个武器 Id —— 抄错了静默失效）；
2. 文案的两条硬约束（段内换行是字面 `\\n`、正文不许有裸 `|`）和长度上限；
3. 下发时机：登录 / 建房 / 进房 / 改模式 / 离房 / 被踢各一发，**内容没变就不发**。
"""
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for sub in ("server", "test"):
    path = os.path.join(ROOT, sub)
    if path not in sys.path:
        sys.path.insert(0, path)

import shop  # noqa: E402
import shopcfg  # noqa: E402
import shopdata  # noqa: E402
import weaponcfg  # noqa: E402

CUSTOM = 1920001          # 泰尔 1 号「左轮手枪 自定义1」
ORIGINAL = 1120011        # 左轮 爆裂1（原版，不该出现在这张表里）

HOOK_SRC = os.path.join(ROOT, "hook", "bshook.c")


def _c_define(name):
    """从 `hook/bshook.c` 里读一个 `#define`（跨文件对常量，别两边各写一份）。"""
    import re
    with open(HOOK_SRC, encoding="utf-8") as handle:
        text = handle.read()
    found = re.search(r"^#define\s+%s\s+(0[xX][0-9a-fA-F]+|\d+)" % name, text,
                      re.MULTILINE)
    if found is None:
        raise AssertionError("bshook.c 里没有 #define %s" % name)
    return int(found.group(1), 0)


class _Case(unittest.TestCase):

    def setUp(self):
        if not shopdata.exists(CUSTOM):
            raise unittest.SkipTest("shop_items.json 里没有自定义武器")
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._saved = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = self.dir
        shopcfg.invalidate()

    def tearDown(self):
        shopcfg.DATA_DIR = self._saved
        shopcfg.invalidate()
        self.tmp.cleanup()


class WireTests(_Case):

    def test_it_round_trips(self):
        weaponcfg.save_item(CUSTOM, params={"pve": {"damage": 50}, "pvp": {"damage": 9}})
        for mode in weaponcfg.MODES:
            payload = weaponcfg.build_desc_frame(mode=mode)
            fmt, serial, ctx, records = weaponcfg.parse_desc_frame(payload)
            self.assertEqual(weaponcfg.DESC_WIRE_FORMAT, fmt)
            self.assertEqual(weaponcfg.load(self.dir)["serial"], serial)
            self.assertEqual(weaponcfg.DESC_CTX[mode], ctx)
            self.assertEqual(len(weaponcfg.custom_item_ids()), len(records))

    def test_the_header_is_nine_bytes_and_the_hook_agrees(self):
        payload = weaponcfg.build_desc_frame()
        self.assertEqual(9, weaponcfg.DESC_HEADER_SIZE)
        self.assertEqual(weaponcfg.DESC_HEADER_SIZE,
                         struct.calcsize("<HIBH"))
        self.assertEqual(weaponcfg.DESC_HEADER_SIZE, _c_define("WDESC_HEADER_BYTES"))
        self.assertEqual(weaponcfg.DESC_WIRE_FORMAT, _c_define("WDESC_FORMAT"))
        self.assertEqual(0x0F02, _c_define("WDESC_OPCODE"))
        self.assertLessEqual(len(weaponcfg.custom_item_ids()), _c_define("WDESC_MAX"))
        # 一整帧要装得进 `u16 载荷长` 那一格
        self.assertLess(len(payload), 0xFFFF - 10)

    def test_the_key_is_the_item_id_not_the_weapon_id(self):
        """★★ ItemDB 的 key 是**物品 id**（`ItemInfo+4`），`0x0F01` 发的才是武器 Id。

        抄错的症状是「一条都匹配不上、提示框一个字没变」，**离线看不出来**
        —— 所以两边各钉一条。
        """
        _f, _s, _c, records = weaponcfg.parse_desc_frame(weaponcfg.build_desc_frame())
        item_ids = set(weaponcfg.custom_item_ids())
        ammo_ids = {shopdata.get(i).ammo_id for i in item_ids}
        self.assertEqual(item_ids, {i for i, _t in records})
        self.assertFalse(item_ids & ammo_ids, "物品 id 和武器 Id 撞上了，这条判据就废了")
        # 反过来：`0x0F01` 发的是武器 Id
        _f, _s, _m, wtab = weaponcfg.parse_hook_frame(weaponcfg.build_hook_frame())
        self.assertEqual(ammo_ids, {i for i, _v in wtab})

    def test_only_custom_weapons_are_in_it(self):
        _f, _s, _c, records = weaponcfg.parse_desc_frame(weaponcfg.build_desc_frame())
        self.assertNotIn(ORIGINAL, [i for i, _t in records])
        for item_id, _text in records:
            self.assertTrue(weaponcfg.is_custom(item_id))

    def test_the_text_is_already_escaped_for_the_client(self):
        """★ 和 `0x0501` 同源：段内换行必须是**字面** `\\` + `n`（§104），
        正文里不许有裸 `|`（那是段分隔符，`wcstok` 会多切一段）。"""
        weaponcfg.save_item(CUSTOM, desc="第一行\n第二行")
        _f, _s, _c, records = weaponcfg.parse_desc_frame(weaponcfg.build_desc_frame())
        for item_id, text in records:
            self.assertNotIn("\n", text, "id=%d 里有真换行，排版器不认（§104）" % item_id)
            self.assertLessEqual(text.count(shopcfg.DESC_SEPARATOR), 1)
        # 逐字节和服务端发 `0x0501` 时那一份相同 —— hook 换上去的不能是另一套算法
        text = dict(records)[CUSTOM]
        self.assertEqual(shop.desc_wire(shopcfg.item_desc_zh(shopdata.get(CUSTOM))), text)

    def test_the_first_line_follows_the_mode(self):
        weaponcfg.save_item(CUSTOM, params={"pve": {"damage": 50}, "pvp": {"damage": 9}})
        for mode in weaponcfg.MODES:
            _f, _s, _c, records = weaponcfg.parse_desc_frame(
                weaponcfg.build_desc_frame(mode=mode))
            text = dict(records)[CUSTOM]
            self.assertTrue(text.startswith(weaponcfg.MODE_ONLY_NOTE[mode]),
                            "%s 那一份的首行不对：%r" % (mode, text[:40]))
        pve = dict(weaponcfg.parse_desc_frame(
            weaponcfg.build_desc_frame(mode=weaponcfg.MODE_PVE))[3])[CUSTOM]
        pvp = dict(weaponcfg.parse_desc_frame(
            weaponcfg.build_desc_frame(mode=weaponcfg.MODE_PVP))[3])[CUSTOM]
        self.assertIn("伤害 50", pve)
        self.assertIn("伤害 9", pvp)

    def test_the_worst_case_text_still_fits_the_hook_buffer(self):
        """hook 那边一条文案的上限是 `WDESC_TEXT_MAX`（含结尾 NUL）。

        最坏情况 = 每一把都配满说明文（`DESC_MAX_CHARS` 字 / `DESC_MAX_LINES` 行）。
        溢出了 hook 会截断并打 `!!`，不会崩 —— 但那是「字少一截」，还是别发生。
        """
        worst = "毒\n" * (weaponcfg.DESC_MAX_LINES - 1)
        worst += "毒" * (weaponcfg.DESC_MAX_CHARS - len(worst))
        self.assertEqual(weaponcfg.DESC_MAX_CHARS, len(worst))
        self.assertEqual(weaponcfg.DESC_MAX_LINES, len(worst.split("\n")))
        for item_id in weaponcfg.custom_item_ids():
            weaponcfg.save_item(item_id, desc=worst)
        limit = _c_define("WDESC_TEXT_MAX")
        for mode in weaponcfg.MODES:
            _f, _s, _c, records = weaponcfg.parse_desc_frame(
                weaponcfg.build_desc_frame(mode=mode))
            self.assertLess(max(len(t) for _i, t in records), limit)


if __name__ == "__main__":
    unittest.main()
