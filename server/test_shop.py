#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""商店 / 合成 / 仓库那几发包的纯函数测试（`server/shop.py`）。

这里守的是三件事：

1. **`0x0600` / `0x0605` 的线格式是 `u8 + i32 + u16 + i32`**。
   FINDINGS §19 曾经把它「实测纠正」成 `u8 + u16 + i32 + i32`，错了整整一版
   （判据见 §20）—— 所以本文件用**实机抓到的原始字节**当用例，
   一改错就当场红。
2. **组包函数写出来的字节，能被「照客户端 Deserialize 写的解析器」读回来**。
   下行三发都没在线上验过，唯一能做的就是让收发两侧对着同一份反汇编各写一遍。
3. **发下去的 itemId 必须是客户端认得的**（`ownable`）—— 不然界面上是空格子
   或者一个提示框，比报错难查得多（§11 / §21）。

★ 本模块还导出几个给 `test_gameserver` 用的东西：
`parse_shop_item_list` / `parse_rep_equipped_list` / `parse_rep_composition_list`
和 `shop_config` / `recipe_config`。
"""
import contextlib
import json
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import shop                                                    # noqa: E402
import shopcfg                                                 # noqa: E402
import shopdata                                                # noqa: E402
from test_shopdata import SYNTHETIC, make_table                # noqa: E402


# ---------------------------------------------------------------------------
# 「照客户端 Deserialize 写一遍」的解析器。★ 故意**不复用** `shop.py` 的常量
#   和结构 —— 复用的话组包写错了解析也跟着错，两边一起错就测不出来。
# ---------------------------------------------------------------------------
class _Wire(object):
    """按客户端流原语（`re/packet_api.md` §1.4）逐个读回来。"""

    def __init__(self, data):
        self.data = data
        self.at = 0

    def take(self, n):
        if self.at + n > len(self.data):
            raise AssertionError("包体短了：要 %d 字节，只剩 %d"
                                 % (n, len(self.data) - self.at))
        chunk = self.data[self.at:self.at + n]
        self.at += n
        return chunk

    def i32(self):
        return struct.unpack("<i", self.take(4))[0]

    def i16(self):
        return struct.unpack("<h", self.take(2))[0]

    def u8(self):
        return struct.unpack("<B", self.take(1))[0]

    def f64(self):
        return struct.unpack("<d", self.take(8))[0]

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def wstr(self):
        return self.take(self.u16() * 2).decode("utf-16le")

    def done(self):
        assert self.at == len(self.data), \
            "包体还剩 %d 字节没读完" % (len(self.data) - self.at)


def parse_shop_item_list(body):
    """`0x0500` 的包体 → `(总页数, 当前页, [[（物品id, 名字, 价格）, …], …])`。

    照 `gspRepShopItemList::Deserialize 0x4439cc` → `ShopStockGroup 0x4437fe`
    → `ShopStockGroupItem 0x4436c7` → `ShopStock 0x44360a` 逐层写的（§21）。
    """
    wire = _Wire(body)
    pages, page, group_count = wire.i32(), wire.i32(), wire.i32()
    groups = []
    for _ in range(group_count):
        options = []
        for _ in range(wire.i32()):
            wire.wstr()                       # 档位名（语义未查明，我们发空串）
            item_id = wire.i32()
            name = wire.wstr()
            price = wire.i32()
            wire.i32()                        # ❓
            wire.i32()                        # 货币
            wire.wstr()                       # ❓
            wire.i32()                        # ❓
            for _ in range(wire.i32()):       # Item@ShopStock ×k
                wire.i32(), wire.i32(), wire.i32()
            options.append((item_id, name, price))
        groups.append(options)
    wire.done()
    return pages, page, groups


def parse_rep_equipped_list(body):
    """`0x0604` 的包体 → `[物品 id]`。掩码用 `parse_equipped_masks` 单独看。"""
    return parse_equipped_masks(body)[1]


def parse_equipped_masks(body):
    """`0x0604` 的包体 → `([三个槽位掩码], [物品 id])`。

    ★ 那 12 字节**不是死字段**（§31）：客户端拿 `(掩码[角色] & PartFlag)`
    判「这件穿着没有」。全 0 = 「什么都没穿」。
    """
    wire = _Wire(body)
    masks = [wire.i32() for _ in range(3)]
    items = [wire.i32() for _ in range(wire.i32())]
    wire.done()
    return masks, items


def parse_rep_item_info(body):
    """`0x0501` 的包体 → `([物品定义 dict], 用途标志)`。

    照 `ItemInfo::Deserialize 0x5586a6` 逐格写的（§28）：
    四个 i32、两个 wstr、两个 i32、一个 **i16**、四个 i32。
    """
    wire = _Wire(body)
    records = []
    for _ in range(wire.i32()):
        record = {"id": wire.i32()}
        wire.i32()                              # +0x08 ❓
        record["part_flag"] = wire.i32()
        record["flags"] = wire.i32()
        record["name"] = wire.wstr()
        wire.wstr()                             # +0x18 ❓
        record["level"] = wire.i32()
        wire.i32()                              # +0x20 ❓
        record["character"] = wire.i16()
        wire.i32()                              # +0x28 ❓
        wire.i32()                              # +0x2c ❓
        record["durability"] = wire.i32()
        record["repairs"] = wire.i32()
        records.append(record)
    purpose = wire.u8()
    wire.done()
    return records, purpose


def parse_rep_inventory(body):
    """`0x0601`（服务端方向）的包体 → `[(id, 数量, 剩余分钟, 修理次数)]`。

    照持有物条目的 `Deserialize 0x412621` 写的（§29）。
    """
    wire = _Wire(body)
    entries = [(wire.i32(), wire.i32(), wire.f64(), wire.i32())
               for _ in range(wire.i32())]
    wire.done()
    return entries


@contextlib.contextmanager
def config_dir(shop=(), recipe=(), items=(), drops=(), data_dir=None,
               allow_bad=False):
    """临时把 `shopcfg` 指到一份现搓的**四份**配置上。

    ★ 全量测试默认把 `shopcfg.DATA_DIR` 指向**空目录**（`run_tests.py`），
    这样「货架上有什么」就不取决于开发机上那份用户随时在改的运营配置。
    要具体内容的用例用这个上下文管理器自己铺。

    ⚠ **四份一起铺，一份都不能少**：`shelf_entries` / `item_info_records`
    这些函数会把**每一份**读到的警告都带出来（物品库读不到 = 等级和角色
    限定全按原版走，那是个该说出来的事），少铺一份用例就会收到
    「没有找到 xxx.json」而看上去像是过滤器写错了。
    """
    saved = shopcfg.DATA_DIR
    with tempfile.TemporaryDirectory() as tmp:
        target = data_dir or tmp
        for filename, key, rows in (
                (shopcfg.SHOP_FILENAME, "items", shop),
                (shopcfg.RECIPE_FILENAME, "recipes", recipe),
                (shopcfg.ITEMS_FILENAME, "items", items),
                (shopcfg.DROPS_FILENAME, "rules", drops)):
            with open(shopcfg.path_of(filename, target), "w",
                      encoding="utf-8", newline="\n") as fp:
                json.dump({"format": 1, key: list(rows)}, fp,
                          ensure_ascii=False)
        shopcfg.DATA_DIR = target
        shopcfg.invalidate()
        # ★ 立刻读一遍确认它们过得了校验。配置读坏时 `shopcfg` 是**返回空表 +
        #   警告**（D10），用例看到的就是「一条都没有」—— 和「过滤器把它们
        #   滤掉了」长得一模一样。踩过一次（配方号撞车），所以在这儿拦。
        # `allow_bad=True` 是给「**故意**铺一份坏配置，看服务端 fail-safe」
        # 那几条用例开的口子。
        if not allow_bad:
            for name, load in (("shop", shopcfg.shop),
                               ("recipe", shopcfg.recipes),
                               ("items", shopcfg.items),
                               ("drops", shopcfg.drops)):
                _, warnings = load(target)
                assert not warnings, "用例铺的 %s.json 自己就不合法: %r" % (
                    name, warnings)
        try:
            yield target
        finally:
            shopcfg.DATA_DIR = saved
            shopcfg.invalidate()


def shop_config(items=(), data_dir=None, **rest):
    """只关心商店目录时的简写。`items` 是**商店条目**（沿用老签名）。"""
    return config_dir(shop=items, data_dir=data_dir, **rest)


def recipe_config(recipes=(), data_dir=None, **rest):
    """只关心合成配方时的简写。"""
    return config_dir(recipe=recipes, data_dir=data_dir, **rest)


def parse_rep_composition_list(body):
    """`0x0505` 的包体 → `(总页数, 当前页, [配方…])`。

    照 `gspRepCompositionList::Deserialize 0x443ae1` → `CompositionRule
    0x44394d` → `CompositionMaterial 0x4438dc` 逐层写的（§27）。
    ★ 材料数量是 **i16** —— 写成 i32 的话这里第一个读歪。
    """
    wire = _Wire(body)
    pages, page, count = wire.i32(), wire.i32(), wire.i32()
    rules = []
    for _ in range(count):
        rule = {"result": wire.i32(), "cost": wire.i32()}
        rule["materials"] = [(wire.i32(), wire.i16())
                             for _ in range(wire.i32())]
        rule["days"] = wire.i32()
        wire.i32()                            # ❌ 死字段
        rules.append(rule)
    wire.done()
    return pages, page, rules


class _ShopCase(unittest.TestCase):
    """把 `shopdata.STORE` 换成 `test_shopdata` 那张小表，用完还原。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        items_path = os.path.join(self.tmp.name, "shop_items.json")
        with open(items_path, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(make_table(SYNTHETIC), fp, ensure_ascii=False)
        saved = shopdata.STORE
        shopdata.STORE = shopdata._Store(items_path)
        self.addCleanup(setattr, shopdata, "STORE", saved)
        self.addCleanup(shopcfg.invalidate)
        shopcfg.invalidate()


class ShopListRequestTests(unittest.TestCase):
    """`0x0600` / `0x0605` 的上行载荷（11 字节）。"""

    #: ★ 实机抓到的原始载荷（`logs/server-20260904-223901.out`）。
    #: 这四条是本文件的地基 —— 线格式记错时它们第一个红。
    REAL = [
        # 22:13:12 进商店第 1 发：分类 2（신상품 新商品，合成面板的默认值）
        ("0002000000000000000000", (0, 2, 0, 0)),
        # 22:13:23 点了「무기 武器」父标签 -> 组 6 序号 0
        ("0000000600000000000000", (0, 0x60000, 0, 0)),
        # 22:13:33 分类 0（= 全部）
        ("0000000000000000000000", (0, 0, 0, 0)),
        # 0x0605 那一发：分类 0x50003 = 이벤트 活动
        ("0003000500000000000000", (0, 0x50003, 0, 0)),
    ]

    def test_the_wire_format_is_u8_i32_u16_i32(self):
        """★★ 记错过一次的那条（§19 → §20）。

        判据不是「哪个数好看」，是三条独立证据：`Serialize 0x443a38` 逐句
        写 1/4/2/4 字节、合成面板 ctor `0x45e0a3` 把分类初始化成 2、
        以及 `0x60000` 正好是标签树里 무기 那一组的父标签。
        """
        for hexed, expected in self.REAL:
            request = shop.parse_shop_list_request(bytes.fromhex(hexed))
            self.assertEqual(
                expected,
                (request.character, request.category, request.page, request.flag),
                hexed)

    def test_a_wrong_length_is_refused_not_guessed(self):
        for payload in (b"", b"\x00" * 10, b"\x00" * 12):
            with self.assertRaises(ValueError):
                shop.parse_shop_list_request(payload)

    def test_build_and_parse_round_trip(self):
        raw = shop.build_shop_list_request(character=2, category=0x60001,
                                           page=3, flag=0)
        self.assertEqual(shop.SHOP_LIST_REQUEST_SIZE, len(raw))
        request = shop.parse_shop_list_request(raw)
        self.assertEqual((2, 0x60001, 3, 0),
                         (request.character, request.category, request.page,
                          request.flag))

    def test_the_mercenary_tab_sends_the_all_characters_marker(self):
        # 客户端在组 3（용병）下强制填 0xff（`0x445e75: cmp ax, 3`）。
        # 服务端拿它当「不限角色」，所以这个常量不能随便改。
        self.assertEqual(0xFF, shop.CHARACTER_ANY)
        raw = shop.build_shop_list_request(character=shop.CHARACTER_ANY)
        self.assertEqual(0xFF, shop.parse_shop_list_request(raw).character)


class CategoryTests(_ShopCase):
    """分类 id = `(组 << 16) | 序号`（§22）。"""

    def test_每个部位落到自己的标签(self):
        self.assertEqual(0x10001, shop.category_of(1010001))   # 上衣
        self.assertEqual(0x10002, shop.category_of(1020001))   # 下装
        self.assertEqual(0x60001, shop.category_of(1120041))   # 武器槽 1
        self.assertEqual(0x60002, shop.category_of(1120051))   # 武器槽 2

    def test_套装按组合值走套装标签(self):
        # `part_flag` 是组合值（上衣+下装+头+鞋+手套 = 31）⇒ 不是任何单件部位。
        self.assertEqual(31, shopdata.part_flag(1990001))
        self.assertEqual(shop.CATEGORY_SET, shop.category_of(1990001))

    def test_材料和不认识的落到各自的兜底(self):
        self.assertEqual(shop.CATEGORY_MATERIAL, shop.category_of(30018))
        self.assertEqual(shop.CATEGORY_OTHER, shop.category_of(9999999))

    def test_判据是_part_flag_不是_id_里的部位码(self):
        # §14 踩过：`1_08_0039` 的 `08` 看着像尾饰，实际 `PartFlag` 是鞋。
        # 这里直接用一张「id 说是上衣、flag 说是鞋」的合成条目钉死判据。
        saved = shopdata.STORE
        table = dict(SYNTHETIC)
        table["1010999"] = {"id": 1010999, "kind": "armor", "part_flag": 8,
                            "part": 1, "ownable": True, "stock": True}
        path = os.path.join(self.tmp.name, "trap.json")
        with open(path, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(make_table(table), fp, ensure_ascii=False)
        shopdata.STORE = shopdata._Store(path)
        try:
            self.assertEqual(0x10004, shop.category_of(1010999))   # 鞋，不是上衣
        finally:
            shopdata.STORE = saved

    def test_父标签收下整组(self):
        self.assertTrue(shop.category_matches(0x60000, 0x60001))
        self.assertTrue(shop.category_matches(0x60000, 0x60003))
        self.assertFalse(shop.category_matches(0x60000, 0x10001))

    def test_零号分类不是通配_它是英雄标签(self):
        """★★ 2026-09-05 实机抓到的 bug：点「人物 → 英雄」列出一堆武器。

        根因是把 `分类 = 0` 当成了「全部」。它其实是那个标签的**真 id**
        （§22）—— 按父标签规则算就是「组 0」，我们一件商品都不归在组 0，
        所以正确行为是**空货架**。
        """
        for category in (0x10001, 0x60002, shop.CATEGORY_MATERIAL):
            self.assertFalse(shop.category_matches(0, category))
        # 「全部」得用专门的哨兵，而且它只给调试命令用，线上不会出现。
        self.assertEqual(-1, shop.CATEGORY_ALL)
        for category in (0x10001, 0x60002, shop.CATEGORY_MATERIAL):
            self.assertTrue(shop.category_matches(shop.CATEGORY_ALL, category))

    def test_新商品标签收全部(self):
        """★ 用户 2026-09-06 拍板：「对复活项目的玩家来说所有物品都是新的」。

        判据不是「哪个好看」，是**这一格空着的代价很实在** —— 客户端打开
        合成界面默认就停在「新商品」上（`0x45e2f0` 那张顶级标签表的第一项），
        空的话玩家第一眼看到的就是一片空白。
        """
        self.assertEqual(2, shop.CATEGORY_NEW)
        for category in (0x10001, 0x60001, shop.CATEGORY_MATERIAL,
                         shop.CATEGORY_SET, shop.CATEGORY_OTHER):
            self.assertTrue(shop.category_matches(shop.CATEGORY_NEW, category),
                            hex(category))

    def test_具体标签只收自己(self):
        self.assertTrue(shop.category_matches(0x10001, 0x10001))
        self.assertFalse(shop.category_matches(0x10001, 0x10002))


class ShelfTests(_ShopCase):
    """货架内容：从 `shop.json` 里挑出「这一页该显示什么」。"""

    def listed(self, *ids, **kw):
        price = kw.pop("price", 100)
        return [{"id": i, "name": "商品%d" % i, "listed": True, "price": price}
                for i in ids]

    def test_只发上架的(self):
        items = self.listed(1120041) + [
            {"id": 1120051, "name": "下架的", "listed": False, "price": 1}]
        with shop_config(items):
            entries, warnings = shop.shelf_entries()
        self.assertEqual([], warnings)
        self.assertEqual([1120041], [e["id"] for e in entries])

    def test_只有货架条目的那批连_shop_json_都过不了(self):
        """★ §11：`1510001` 只有 `[Stock-]` 没有 `[Item-]`，进不了背包。

        第一道闸在 `shopcfg.validate_shop` —— 它**整份文件拒收**，
        然后按 D10 保留上一份好的（一次都没读成功过就是空货架）+ 给警告。
        「货架空着 + 日志里写明哪一行不对」比「悄悄少一件」好查得多。
        """
        self.assertFalse(shopdata.ownable(1510001))
        with shop_config(self.listed(1510001, 1120041), allow_bad=True):
            entries, warnings = shop.shelf_entries()
        self.assertEqual([], entries)
        self.assertTrue(any("1510001" in w for w in warnings), warnings)

    def test_物品表变了之后运行时还有第二道闸(self):
        """★ 配置写的时候合法，`shop_items.json` 后来变了 —— 还得挡住。

        `shop.json` 是用户数据、`shop_items.json` 是随包发布的产物，
        两者会各自演进（比如某次客户端更新砍掉一件），所以下发前再过一遍
        `ownable()`。这一条就是那道闸的用例。
        """
        with shop_config(self.listed(1120041, 1120051)) as data_dir:
            # 先读一次：这时两件都合法，`shopcfg` 把解析结果缓存下来。
            before, _ = shop.shelf_entries(data_dir=data_dir)
            self.assertEqual([1120041, 1120051], [e["id"] for e in before])
            # 再把物品表换成「1120051 不再能进背包」的那一版。文件没动过，
            # `shopcfg` 的 mtime 缓存照旧命中 ⇒ 只剩下发前这一道闸。
            table = dict(SYNTHETIC)
            table["1120051"] = dict(table["1120051"], ownable=False)
            path = os.path.join(self.tmp.name, "shrunk.json")
            with open(path, "w", encoding="utf-8", newline="\n") as fp:
                json.dump(make_table(table), fp, ensure_ascii=False)
            shopdata.STORE = shopdata._Store(path)
            entries, _ = shop.shelf_entries(data_dir=data_dir)
        self.assertEqual([1120041], [e["id"] for e in entries])

    def test_按角色过滤_但_0xff_不过滤(self):
        # 1120041 是泰尔(0) 的、2120041 是卡希尔(1) 的。
        with shop_config(self.listed(1120041, 2120041)):
            everyone, _ = shop.shelf_entries(character=shop.CHARACTER_ANY)
            tai, _ = shop.shelf_entries(character=0)
        self.assertEqual([1120041, 2120041], [e["id"] for e in everyone])
        self.assertEqual([1120041], [e["id"] for e in tai])

    def test_一页八件(self):
        # 客户端商店面板只有 8 个格子（`0x45b397: cmp .., 0x80` 步长 0x10），
        # 多发的画不出来。
        self.assertEqual(8, shop.PAGE_SIZE)

    def test_分页与总页数(self):
        ids = [1120041, 1120051, 2120041, 1010001, 1020001, 1990001]
        with shop_config(self.listed(*ids)):
            _, first, _ = shop.shelf_page(page=0)
            self.assertEqual(len(ids), len(first))          # 6 件，一页装得下
            body, _, _ = shop.shelf_page(page=0)
        pages, page, groups = parse_shop_item_list(body)
        self.assertEqual((1, 0), (pages, page))
        self.assertEqual(len(ids), len(groups))

    def test_空货架也报一页(self):
        """★ 总页数发 0 的话客户端会把当前页夹成 −1（`0x45b2be` 的 `ecx-1`）。"""
        with shop_config([]):
            body, shown, _ = shop.shelf_page()
        self.assertEqual([], shown)
        self.assertEqual((1, 0, []), parse_shop_item_list(body))

    def test_越界的页号被夹回来(self):
        with shop_config(self.listed(1120041)):
            body, shown, _ = shop.shelf_page(page=99)
        pages, page, _ = parse_shop_item_list(body)
        # 和客户端 `0x45b2be` 的夹法一致 ⇒ 「服务端说第几页」永远等于
        # 「客户端显示第几页」。
        self.assertEqual((1, 0), (pages, page))
        self.assertEqual([1120041], [e["id"] for e in shown])

    def test_探针不开的时候那格未查明字段是零(self):
        # 默认必须是「什么都不填」—— 探针是**找字段**用的临时手段，
        # 平时挂着会往界面上糊一堆假数据。
        with shop_config(self.listed(1120041)):
            body, _, _ = shop.shelf_page()
        self.assertNotIn(struct.pack("<i", shop.SHELF_PROBE), body)
        with shop_config(self.listed(1120041)):
            probed, _, _ = shop.shelf_page(probe=shop.SHELF_PROBE)
        self.assertIn(struct.pack("<i", shop.SHELF_PROBE), probed)
        # 探针只动那一格，商品本身照旧 ⇒ 开着它也能正常买东西。
        self.assertEqual(parse_shop_item_list(body)[2],
                         parse_shop_item_list(probed)[2])

    def test_划线原价默认等于售价(self):
        """★ 2026-09-05 实机：提示框写成「0 → 3000」。

        `ShopStock+0x10` 是**划线原价**，客户端拿它和售价比，不相等就画成
        「原价 → 现价」（`0x45c354`）。不做打折就得如实等于售价（§31）。
        """
        with shop_config([{"id": 1120041, "name": "左轮", "listed": True,
                           "price": 3000}]):
            body, _, _ = shop.shelf_page()
        wire = _Wire(body)
        wire.i32(), wire.i32(), wire.i32(), wire.i32()   # 页数/当前页/n/档位数
        wire.wstr()                                      # 档位名
        wire.i32(), wire.wstr()                          # itemId, 名字
        self.assertEqual(3000, wire.i32())               # +0x0c 售价
        self.assertEqual(3000, wire.i32())               # +0x10 划线原价

    def test_货架条目带说明(self):
        # 提示框下半那块（`ShopStock+0x18`，§31）。原版的说明随服务端 DB
        # 没了，这里发的是**从本地数据现算**的（伤害 / 加成）。
        with shop_config([{"id": 1120041, "name": "左轮", "listed": True,
                           "price": 3000}]):
            body, _, _ = shop.shelf_page()
        self.assertIn("伤害 4".encode("utf-16le"), body)

    def test_价格和名字来自_shop_json(self):
        # ★ PLAN M5：价格只信 `shop.json`，包里的任何数值都不作数。
        with shop_config([{"id": 1120041, "name": "左轮 极速1",
                           "listed": True, "price": 3000}]):
            body, _, _ = shop.shelf_page()
        _, _, groups = parse_shop_item_list(body)
        self.assertEqual([[(1120041, "左轮 极速1", 3000)]], groups)


class SortTests(_ShopCase):
    """右下角那两个排序按钮（`0x0600` 的标志位，§25）。

    ★ 排序只能在服务端做：一页只发 8 件，客户端手里没有全表可排。
    """

    def listed(self, *ids):
        return [{"id": i, "name": "商品%d" % i, "listed": True, "price": 100}
                for i in ids]

    #: 小表的插入顺序（= `catalog_index`）：1010001 · 1020001 · 1990001 ·
    #: 1120041 · 1120051 · 2120041 · 30018 · 1510001。
    IDS = (1120041, 1010001, 2120041)

    def test_默认是基本顺序_按_id_升序(self):
        # 进商店时面板那个字节是 0（实测：11:55 那一串请求全是 标志=0）。
        self.assertEqual(0, shop.SORT_BASIC)
        with shop_config(self.listed(*self.IDS)):
            entries, _ = shop.shelf_entries(order=shop.SORT_BASIC)
        self.assertEqual([1010001, 1120041, 2120041], [e["id"] for e in entries])

    def test_上市顺序_按原版目录倒序(self):
        with shop_config(self.listed(*self.IDS)):
            entries, _ = shop.shelf_entries(order=shop.SORT_RELEASE)
        # 目录名次 0 / 3 / 5 ⇒ 新的在前。
        self.assertEqual([2120041, 1120041, 1010001], [e["id"] for e in entries])

    def test_两种顺序真的不一样(self):
        """★ 这一条是给「点了没反应」兜底的 —— 只要两边排出来一样，
        玩家点了按钮界面纹丝不动，和没实现是一个观感。"""
        with shop_config(self.listed(*self.IDS)):
            basic, _ = shop.shelf_entries(order=shop.SORT_BASIC)
            release, _ = shop.shelf_entries(order=shop.SORT_RELEASE)
        self.assertNotEqual([e["id"] for e in basic], [e["id"] for e in release])

    def test_顺序一路传到包体(self):
        with shop_config(self.listed(*self.IDS)):
            body, shown, _ = shop.shelf_page(order=shop.SORT_RELEASE)
        _, _, groups = parse_shop_item_list(body)
        self.assertEqual([2120041, 1120041, 1010001],
                         [options[0][0] for options in groups])
        self.assertEqual([2120041, 1120041, 1010001], [e["id"] for e in shown])

    def test_认不出来的标志位当基本顺序(self):
        """客户端只发过 0 和 1；真冒出第三个值也不能让货架空掉或炸掉。"""
        with shop_config(self.listed(*self.IDS)):
            odd, _ = shop.shelf_entries(order=7)
            basic, _ = shop.shelf_entries(order=shop.SORT_BASIC)
        self.assertEqual([e["id"] for e in basic], [e["id"] for e in odd])
        # 但日志要如实印出来，不能悄悄写成「基本顺序」。
        self.assertIn("7", shop.sort_name(7))

    def test_排序是全序_不然翻页会重复或漏格(self):
        """目录名次相同（两件都不在物品表里）时还要按 id 兜底。"""
        entries = [{"id": 999999002, "name": "甲", "listed": True, "price": 1},
                   {"id": 999999001, "name": "乙", "listed": True, "price": 1}]
        self.assertEqual(shopdata.catalog_index(999999001),
                         shopdata.catalog_index(999999002))
        ordered = shop.sort_entries(entries, shop.SORT_RELEASE)
        self.assertEqual([999999001, 999999002], [e["id"] for e in ordered])


class PacketTests(_ShopCase):
    """三发下行包的字节。★ 全是 🔍静态结论，没在线上验过。"""

    def test_货架包能被照客户端写的解析器读回来(self):
        stock = shop.build_shop_stock(1120041, "左轮", 3000)
        body = shop.build_rep_shop_item_list(
            2, 1, [shop.build_shop_stock_group([("", stock)])])
        self.assertEqual((2, 1, [[(1120041, "左轮", 3000)]]),
                         parse_shop_item_list(body))

    def test_一个格子可以有好几档买法(self):
        # 原版一个格子挂几档（🤔 猜是 7일 / 30일 / 영구）。本版只卖永久，
        # 但组包函数得撑得住 —— 不然以后加期限制要改的是线格式，不是数据。
        group = shop.build_shop_stock_group(
            [("7일", shop.build_shop_stock(1120041, "左轮", 300)),
             ("영구", shop.build_shop_stock(1120041, "左轮", 3000))])
        _, _, groups = parse_shop_item_list(
            shop.build_rep_shop_item_list(1, 0, [group]))
        self.assertEqual([(1120041, "左轮", 300), (1120041, "左轮", 3000)],
                         groups[0])

    def test_装备清单就是_0x030b_去掉座位号(self):
        """★ §23：`0x0604` 和 `0x030b` 的包体只差开头那个座位号。

        两边各写一遍容易分叉，这条直接拿 `build_slot_equipped_list` 的输出
        砍掉前 4 字节来比 —— 哪天有人只改了一边就当场红。
        """
        import gameserver
        items = [1120041, 1010001]
        mine = shop.build_rep_equipped_list(items)
        theirs = gameserver.build_slot_equipped_list(0, items)
        self.assertEqual(theirs[4:], mine)

    def test_没穿东西时掩码才是十二个零字节(self):
        body = shop.build_rep_equipped_list([])
        self.assertEqual(b"\x00" * 12, body[:12])
        self.assertEqual(shop.EQUIPPED_SLOT_MASK_BYTES, 12)
        self.assertEqual([], parse_rep_equipped_list(body))

    def test_掩码按角色分开点亮(self):
        """★★ §31：这 12 字节**不是死字段**（§23 那句话是错的）。

        房间里的「卸下」按钮拿 `(掩码[角色] & PartFlag) == PartFlag` 判
        「这件穿着没有」（`0x5584ab`）。全 0 ⇒ 它认为你没穿 ⇒ 弹「已卸下。」
        然后什么都不做（2026-09-05 实机现象）。
        """
        # 1120041 是泰尔（角色 0）的武器槽 1；2120041 是卡希尔（角色 1）的。
        masks, items = parse_equipped_masks(
            shop.build_rep_equipped_list([1120041, 2120041]))
        self.assertEqual([1024, 1024, 0], masks)
        self.assertEqual([1120041, 2120041], items)

    def test_不限角色的装备三个掩码都点亮(self):
        # 客户端自己就是这么做的（`0x5583f8` 那个三次循环）。
        original = shopdata.get

        def unlimited(item_id):
            item = original(item_id)
            if item is not None:
                item.character = None      # 「不限角色」
            return item

        shopdata.get = unlimited
        self.addCleanup(setattr, shopdata, "get", original)
        self.assertEqual((1024, 1024, 1024),
                         shop.equipment_slot_masks([1120041]))

    def test_材料不进掩码(self):
        # `part_flag == 0` 的东西不占槽位，掩码里不该有它。
        self.assertEqual((0, 0, 0), shop.equipment_slot_masks([30018]))
        self.assertEqual((0, 0, 0), shop.equipment_slot_masks([9999999]))

    def test_0x030b_的掩码和_0x0604_一样(self):
        # 两边都得算，不然「商店里穿上了、房间里说没穿」。
        import gameserver
        items = [1120041, 2120041]
        self.assertEqual(shop.build_rep_equipped_list(items),
                         gameserver.build_slot_equipped_list(0, items)[4:])

    def test_掩码个数不对就抛(self):
        with self.assertRaises(ValueError):
            shop.build_rep_equipped_list([], slot_masks=(0, 0))

    def test_只把客户端认得的_id_塞进清单(self):
        # 查不到的 id 会让客户端收集起来弹提示框（`0x447406`）。
        self.assertEqual([1120041],
                         shop.displayable_items([1120041, 1510001, 9999999]))

    def test_购买失败的原因码认不出来也不能填零(self):
        """★ `0x0502` 第二格是**失败原因码**（§30）：`0` 在界面上是
        「未定义的错误」—— 那句话对玩家和对我们都是零信息。兜底要 6「内部错误」。"""
        self.assertEqual(shop.BUY_REASON_LEVEL_LOW,
                         shop.buy_reason_code(shop.BUY_LEVEL))
        self.assertEqual(shop.BUY_REASON_ALREADY_OWNED,
                         shop.buy_reason_code(shop.BUY_ALREADY_OWNED))
        self.assertEqual(shop.BUY_REASON_NO_PIXEL,
                         shop.buy_reason_code(shop.BUY_NO_MONEY))
        for junk in (None, "", "以后新加的一条理由"):
            self.assertEqual(shop.BUY_REASON_INTERNAL,
                             shop.buy_reason_code(junk))

    def test_购买结果的第二格就是原因码(self):
        self.assertEqual(bytes.fromhex("00000000" "03000000" "00000000"),
                         shop.build_rep_item_buy(False,
                                                 shop.BUY_REASON_LEVEL_LOW))
        self.assertEqual(bytes.fromhex("01000000" "00000000" "00000000"),
                         shop.build_rep_item_buy(True))

    def test_买别的角色的装备不拦(self):
        """商店上方那排角色箭头就是给「替别的角色买」用的（§30）。"""
        table = {2120041: {"id": 2120041, "name": "卡希尔的枪",
                           "listed": True, "price": 100, "level": 1}}
        entry, why = shop.check_purchase(2120041, table, level=10, owned=set())
        self.assertIsNone(why)
        self.assertIs(table[2120041], entry)

    def test_礼物清单只支持空的(self):
        self.assertEqual(b"\x00\x00\x00\x00", shop.build_rep_gift_list())
        with self.assertRaises(ValueError):
            # `Gift` 的线格式还没逆 —— 静默发个半成品比不发更难查。
            shop.build_rep_gift_list([{"id": 1}])


class ItemInfoTests(_ShopCase):
    """`0x0601` 上行「给我定义」/ `0x0501` 下行物品定义（§28）。

    ★ 这一段守的是 2026-09-05 那四个实机 bug 的根因：**客户端的
    `ItemInfo` 表开机是空的**，不发 `0x0501` 就「无法从服务器读取道具信息」。
    """

    def test_请求就是实机抓到的那串字节(self):
        # `logs/server.out` 14:01:17.503 那一发（进商店后问 6 件穿着的）。
        raw = bytes.fromhex("06000000" "75690f00" "9e900f00" "aab70f00"
                            "a5de0f00" "b5051000" "0c171100" "02")
        ids, purpose = shop.parse_item_info_request(raw)
        self.assertEqual([1010037, 1020062, 1030058, 1040037, 1050037,
                          1120012], ids)
        # ★ 尾字节 = 用途标志，`2` = 装备清单要的 ⇒ 回去之后重建人物模型。
        self.assertEqual(shop.ITEM_INFO_FOR_EQUIPPED, purpose)

    def test_空请求也解得开(self):
        self.assertEqual(([], 0), shop.parse_item_info_request(b"\0\0\0\0\0"))

    def test_长度对不上就抛(self):
        for junk in (b"", b"\x01\0\0\0", b"\x01\0\0\0\x01\0\0\0"):
            with self.assertRaises(ValueError):
                shop.parse_item_info_request(junk)

    def test_用途标志原样回(self):
        """⚠⚠ `ShopStage::vft[0xb4]`（`0x44602a`）只在标志 == 2 时重建
        左侧人物模型。回错了 = 「买完 / 穿完人物不刷新」。"""
        for purpose in (0, 1, 2, 5, 255):
            body = shop.build_rep_item_info([], purpose)
            self.assertEqual(([], purpose), parse_rep_item_info(body))

    def test_装备的定义(self):
        records, _, _ = shop.item_info_records([1120041])
        record, purpose = parse_rep_item_info(
            shop.build_rep_item_info(records, 2))
        self.assertEqual(2, purpose)
        self.assertEqual(1, len(record))
        record = record[0]
        self.assertEqual(1120041, record["id"])
        self.assertEqual(1024, record["part_flag"])       # 武器槽 1
        # ★ 可装备那一位是硬要求：`0x0604` 的处理器只有它为 1 才 `Equip`。
        self.assertEqual(shop.ITEM_FLAG_EQUIPPABLE, record["flags"])
        self.assertEqual(0, record["character"])          # 泰尔专用
        self.assertEqual(shop.REPAIR_UNLIMITED, record["repairs"])

    def test_材料发计数位不发期限位(self):
        # 期限位会让提示框写「%d일」，天数取自持有物条目里那格 —— 我们
        # 不卖期限物，发下去就是「剩 0 天」。
        records, _, _ = shop.item_info_records([30018])
        record = parse_rep_item_info(shop.build_rep_item_info(records))[0][0]
        self.assertEqual(shop.ITEM_FLAG_COUNTED, record["flags"])
        self.assertEqual(0, record["part_flag"])

    def test_不限角色发的是负一不是零(self):
        """⚠ `0` 是「泰尔专用」，不限只能是 `-1`（`0x44e1b8` 拿 `0xffff` 判）。"""
        records, _, _ = shop.item_info_records([30018])   # 材料，不限角色
        record = parse_rep_item_info(shop.build_rep_item_info(records))[0][0]
        self.assertEqual(shop.CHARACTER_UNLIMITED, record["character"])

    def test_名字取自_shop_json_等级和角色取自物品库(self):
        """★ D31：等级和角色限定的唯一出处是 `items.json`。

        名字仍然优先取商店目录里那份（管理页在那儿改），但**等级和角色
        限定只认物品库** —— 同一件东西买来的和合成的不可能是两个门槛。
        """
        with config_dir(shop=[{"id": 1120041, "name": "左轮 爆裂1",
                               "listed": True, "price": 3000}],
                        items=[{"id": 1120041, "level": 5, "character": 1}]):
            records, _, _ = shop.item_info_records([1120041])
        record = parse_rep_item_info(shop.build_rep_item_info(records))[0][0]
        self.assertEqual("左轮 爆裂1", record["name"])
        # ★ 客户端拿它挡「穿上」（`0x445817`）—— 发大了玩家买到了却穿不上。
        self.assertEqual(5, record["level"])
        # ★ 物品库改成 1 = 卡希尔专用，即使原版数据里它是泰尔的（0）。
        self.assertEqual(1, record["character"])

    def test_物品库没登记就退回原版数据(self):
        with config_dir(shop=[{"id": 1120041, "name": "左轮 爆裂1",
                               "listed": True, "price": 3000}]):
            records, _, _ = shop.item_info_records([1120041])
        record = parse_rep_item_info(shop.build_rep_item_info(records))[0][0]
        self.assertEqual(1, record["level"])          # 不限等级
        self.assertEqual(0, record["character"])      # 原版数据里的角色

    def test_表里没有的_id_跳过而不是抛(self):
        records, skipped, _ = shop.item_info_records([1120041, 9999999, "abc"])
        self.assertEqual(1, len(records))
        self.assertEqual([9999999, "abc"], skipped)


class InventoryTests(_ShopCase):
    """`0x0700` 上行「给我持有物」/ `0x0601` 下行持有物清单（§29）。"""

    def test_装备和材料合成一张清单(self):
        entries, skipped = shop.inventory_records(
            {"1120041": {"count": 1, "expires": None}}, {"30018": 3})
        self.assertEqual([], skipped)
        self.assertEqual([(30018, 3, 0.0, 0), (1120041, 1, 0.0, 0)],
                         parse_rep_inventory(shop.build_rep_inventory(entries)))

    def test_永久物的剩余分钟必须是零(self):
        """★ 处理器 `0x554273` 拿它和 `0.0` 比，不等于才换算成到期时刻 ——
        发个大数会让界面上多出一行「还剩 N 天」。"""
        entries, _ = shop.inventory_records({"1120041": {"count": 1}})
        self.assertEqual(shop.PERMANENT_MINUTES,
                         parse_rep_inventory(
                             shop.build_rep_inventory(entries))[0][2])

    def test_只有货架的_id_不往仓库发(self):
        entries, skipped = shop.inventory_records(
            {"1120041": {"count": 1}, "1510001": {"count": 1}})
        self.assertEqual([1510001], skipped)
        self.assertEqual([1120041],
                         [e[0] for e in parse_rep_inventory(
                             shop.build_rep_inventory(entries))])
        self.assertEqual([1120041], shop.inventory_item_ids(
            {"1120041": {"count": 1}, "1510001": {"count": 1}}))

    def test_数量为零的不发(self):
        entries, _ = shop.inventory_records({"1120041": {"count": 0}},
                                            {"30018": 0})
        self.assertEqual([], entries)

    def test_空仓库也是一个合法包体(self):
        # 不发的话仓库面板等一个永远不来的应答。
        self.assertEqual([], parse_rep_inventory(shop.build_rep_inventory([])))


class CompositionListTests(_ShopCase):
    """`0x0605` 上行配方列表请求 / `0x0505` 下行配方（§27 / M7）。"""

    #: 小表里能当产物的三件（上衣 / 下装 / 武器），和一种材料。
    #: ★ 配方号自己往上数 —— 撞车的话 `validate_recipes` 直接拒收整份文件，
    #:   用例会退化成「配方表是空的」，看上去像过滤器写错了。
    def setUp(self):
        super().setUp()
        self.next_recipe_id = 0

    def recipe(self, result, cost=100, materials=None, **kw):
        self.next_recipe_id += 1
        entry = {"id": kw.pop("id", self.next_recipe_id), "result": result,
                 "cost": cost, "listed": kw.pop("listed", True),
                 "materials": materials or [{"id": 30018, "count": 2}]}
        entry.update(kw)
        return entry

    def test_线格式能被照客户端写的解析器读回来(self):
        body = shop.build_rep_composition_list(
            3, 1, [shop.build_composition_rule(
                1010001, 2500,
                [{"id": 30018, "count": 2}, {"id": 1020001, "count": 1}],
                days=7)])
        pages, page, rules = parse_rep_composition_list(body)
        self.assertEqual((3, 1), (pages, page))
        self.assertEqual([{"result": 1010001, "cost": 2500,
                           "materials": [(30018, 2), (1020001, 1)],
                           "days": 7}], rules)

    def test_材料数量是_i16_不是_i32(self):
        """★ 确认框读的是 `movsx eax, word [mat+8]`（§27）——
        写成 i32 的话「要 2 个」会被读成 2，但**下一格材料 id 整体错位**。"""
        raw = shop.build_composition_material(30018, 300)
        self.assertEqual(6, len(raw))
        self.assertEqual((30018, 300), struct.unpack("<ih", raw))

    def test_超过四种材料要抛(self):
        # 界面只有 4 个槽，第 5 种玩家根本看不见 —— 静默发出去等于骗人。
        with self.assertRaises(ValueError):
            shop.build_composition_rule(
                1010001, 100, [{"id": 30018, "count": 1}] * 5)

    def test_只列上架的(self):
        with recipe_config([self.recipe(1010001),
                            self.recipe(1020001, listed=False)]):
            shown, _ = shop.recipe_entries()
        self.assertEqual([1010001], [r["result"] for r in shown])

    def test_物品表变了之后运行时还有第二道闸(self):
        """产物进不了背包（§11）的一律不列 —— 发下去客户端查不到定义。

        ★ 写配置那会儿 `validate_recipes` 已经挡了一道，这条守的是**后来**
        `shop_items.json` 变了的情况（和货架那条同源，理由见
        `ShelfTests.test_物品表变了之后运行时还有第二道闸`）。
        """
        with recipe_config([self.recipe(1010001),
                            self.recipe(1020001)]) as data_dir:
            before, _ = shop.recipe_entries(data_dir=data_dir)
            self.assertEqual([1010001, 1020001],
                             [r["result"] for r in before])
            table = dict(SYNTHETIC)
            table["1020001"] = dict(table["1020001"], ownable=False)
            path = os.path.join(self.tmp.name, "shrunk.json")
            with open(path, "w", encoding="utf-8", newline="\n") as fp:
                json.dump(make_table(table), fp, ensure_ascii=False)
            shopdata.STORE = shopdata._Store(path)
            shown, _ = shop.recipe_entries(data_dir=data_dir)
        self.assertEqual([1010001], [r["result"] for r in shown])

    def test_按标签过滤(self):
        with recipe_config([self.recipe(1010001), self.recipe(1020001)]):
            上衣, _ = shop.recipe_entries(category=0x10001)
            下装, _ = shop.recipe_entries(category=0x10002)
            整组, _ = shop.recipe_entries(category=0x10000)
        self.assertEqual([1010001], [r["result"] for r in 上衣])
        self.assertEqual([1020001], [r["result"] for r in 下装])
        self.assertEqual([1010001, 1020001], [r["result"] for r in 整组])

    def test_配方没有等级门(self):
        """★★ D27：原版的合成面板**从头到尾不读玩家等级**，`0x0506` 也没有
        「等级太低」这一档。加过一版「等级不到就不列」，2026-09-05 实机被
        用户报「管理页明明上架了，游戏里看不到」—— 那是发明出来的规则。

        判据：`level` 键**根本进不了解析结果**（老 `recipe.json` 里残留的
        会被 `validate_recipes` 丢掉），所以过滤器不可能再用上它。
        """
        with recipe_config([dict(self.recipe(1010001), level=99),
                            dict(self.recipe(1020001), level=1)]):
            shown, _ = shop.recipe_entries()
        self.assertEqual([1010001, 1020001], [r["result"] for r in shown])
        self.assertNotIn("level", shown[0])

    def test_角色限定问物品库_不问配方(self):
        """★ D31：角色限定是**产物自己的**属性。配方里写 `character`
        没有用（`validate_recipes` 直接丢掉），要改去物品库改。"""
        with config_dir(recipe=[self.recipe(1010001, character=1),
                                self.recipe(1020001)],
                        items=[{"id": 1010001, "level": 1, "character": 1}]):
            角色0, _ = shop.recipe_entries(character=0)
            角色1, _ = shop.recipe_entries(character=1)
            不限, _ = shop.recipe_entries(character=shop.CHARACTER_ANY)
        # 1010001 原版数据里是角色 0 的装备，物品库改成 1 -> 按物品库走。
        # 1020001 没登记 -> 退回原版数据（角色 0）。
        self.assertEqual([1020001], [r["result"] for r in 角色0])
        self.assertEqual([1010001], [r["result"] for r in 角色1])
        self.assertEqual([1010001, 1020001], [r["result"] for r in 不限])

    def test_新商品那一格列全部配方(self):
        # 合成面板打开时默认停在这一格 —— 空的话玩家以为功能坏了。
        with recipe_config([self.recipe(1010001), self.recipe(1120041)]):
            新商品, _ = shop.recipe_entries(category=shop.CATEGORY_NEW)
            上衣, _ = shop.recipe_entries(category=0x10001)
        self.assertEqual([1010001, 1120041],
                         [r["result"] for r in 新商品])
        # ★ 武器产物在「装备」那几个标签下**点不到**（合成面板没有武器标签），
        #   现在靠「新商品」兜住了。
        self.assertEqual([1010001], [r["result"] for r in 上衣])

    def test_一页八条_和货架一样(self):
        """★ 判据是 `0x45f011` 的循环走 `0..0x100` 步长 `0x20`（8 格），
        不是「货架是 8 所以合成也是 8」。"""
        self.assertEqual(8, shop.COMPOSITION_PAGE_SIZE)

    def test_分页与总页数(self):
        recipes = [self.recipe(1010001, id=1), self.recipe(1020001, id=2),
                   self.recipe(1120041, id=3)]
        with recipe_config(recipes):
            body, shown, _ = shop.composition_page()
            pages, page, rules = parse_rep_composition_list(body)
        self.assertEqual((1, 0), (pages, page))
        self.assertEqual(3, len(rules))
        self.assertEqual([r["result"] for r in shown],
                         [r["result"] for r in rules])

    def test_一条配方都没有也报一页(self):
        """★ 总页数发 0 会让客户端把当前页夹成 -1（`0x45efb9` 的 `dec ecx`）。"""
        with recipe_config([]):
            body, shown, _ = shop.composition_page()
        self.assertEqual([], shown)
        self.assertEqual((1, 0, []), parse_rep_composition_list(body))

    def test_越界的页号被夹回来(self):
        with recipe_config([self.recipe(1010001)]):
            body, _, _ = shop.composition_page(page=9)
        self.assertEqual((1, 0), parse_rep_composition_list(body)[:2])

    def test_排序是全序(self):
        # 同产物排序键会撞车时靠配方号兜底 —— 不然翻页会重复或漏格。
        first = self.recipe(1010001, id=7)
        second = self.recipe(1020001, id=3)
        self.assertEqual([1010001, 1020001],
                         [r["result"] for r in
                          shop.sort_recipes([second, first])])
        self.assertEqual([1010001, 1020001],
                         [r["result"] for r in
                          shop.sort_recipes([first, second])])

    def test_产物和材料的_id_都要喂定义(self):
        """⚠ `0x0505` 的处理器逐条查产物 `rule+4` **和每一种材料** `mat+4`
        （`0x447575` / `0x4475bf`）—— 少喂材料，四个槽就是没名字的空格子。"""
        recipes = [self.recipe(1010001,
                               materials=[{"id": 30018, "count": 2},
                                          {"id": 1020001, "count": 1}]),
                   self.recipe(1120041, materials=[{"id": 30018, "count": 1}])]
        self.assertEqual([1010001, 30018, 1020001, 1120041],
                         shop.composition_item_ids(recipes))

    def test_合成的用途标志是三(self):
        # `0x447603: push 3` —— 客户端问定义时带的就是它。
        self.assertEqual(3, shop.ITEM_INFO_FOR_COMPOSITION)


class ComposeTests(_ShopCase):
    """`0x0606` 上行合成 / `0x0506` 下行结果码（§27 / §33）。"""

    RECIPE = {"id": 1, "result": 1010001, "cost": 500, "listed": True,
              "character": None, "days": 0,
              "materials": [{"id": 30018, "count": 2}]}

    def test_上行带的是产物_id_不是配方号(self):
        self.assertEqual(1010001,
                         shop.parse_compose_request(struct.pack("<i", 1010001)))
        for payload in (b"", b"\x00" * 3, b"\x00" * 8):
            with self.assertRaises(ValueError):
                shop.parse_compose_request(payload)

    def test_结果码就是那一个_i32(self):
        for code in (shop.COMPOSE_OK, shop.COMPOSE_NO_MONEY,
                     shop.COMPOSE_NO_MATERIAL, shop.COMPOSE_ALREADY_OWNED):
            body = shop.build_rep_compose_item(code)
            self.assertEqual(4, len(body))
            self.assertEqual(code, struct.unpack("<i", body)[0])

    def test_客户端认得的四档就是这四个数(self):
        """★ 处理器 `0x4476c4` 的 `sub eax,0 / dec eax` 链（§33）：
        0 成功 / 1 金币不足 / 2 材料不足 / 4 已拥有，**3 落进「未知的错误」**。"""
        self.assertEqual((0, 1, 2, 4),
                         (shop.COMPOSE_OK, shop.COMPOSE_NO_MONEY,
                          shop.COMPOSE_NO_MATERIAL, shop.COMPOSE_ALREADY_OWNED))
        self.assertEqual(3, shop.COMPOSE_UNKNOWN)

    def test_每条拒绝理由都挑得出码(self):
        self.assertEqual(shop.COMPOSE_NO_MONEY,
                         shop.compose_reason_code(shop.COMPOSE_MONEY))
        self.assertEqual(shop.COMPOSE_NO_MATERIAL,
                         shop.compose_reason_code(shop.COMPOSE_MATERIAL))
        self.assertEqual(shop.COMPOSE_ALREADY_OWNED,
                         shop.compose_reason_code(shop.COMPOSE_OWNED))
        # 认不出来的兜底不是 0（0 在这一发上是「成功」！）。
        self.assertEqual(shop.COMPOSE_UNKNOWN,
                         shop.compose_reason_code("没见过的理由"))
        self.assertNotEqual(shop.COMPOSE_OK,
                            shop.compose_reason_code("没见过的理由"))

    def check(self, owned=(), money=10000, materials=None,
              recipes=None, item_id=1010001):
        return shop.check_compose(item_id, recipes or [self.RECIPE],
                                  set(owned), money,
                                  materials if materials is not None
                                  else {30018: 5})

    def test_全都够就放行(self):
        recipe, why = self.check()
        self.assertIsNone(why)
        self.assertEqual(1010001, recipe["result"])

    def test_没有这条配方(self):
        self.assertEqual(shop.COMPOSE_NOT_LISTED, self.check(item_id=999999)[1])

    def test_下架的配方合不出来(self):
        """⚠ 管理页关掉一条 = 界面上看不到它，那就不该还能靠手搓的
        `0x0606` 合出来。"""
        closed = dict(self.RECIPE, listed=False)
        self.assertEqual(shop.COMPOSE_NOT_LISTED, self.check(recipes=[closed])[1])

    def test_已经有了(self):
        self.assertEqual(shop.COMPOSE_OWNED, self.check(owned=[1010001])[1])

    def test_材料不够(self):
        self.assertEqual(shop.COMPOSE_MATERIAL,
                         self.check(materials={30018: 1})[1])
        self.assertEqual(shop.COMPOSE_MATERIAL, self.check(materials={})[1])

    def test_金币不够(self):
        self.assertEqual(shop.COMPOSE_MONEY, self.check(money=499)[1])
        self.assertIsNone(self.check(money=500)[1])

    def test_材料比金币先报(self):
        """两样都缺时先说材料 —— 客户端的错误框一次只显示一条，
        先报玩家自己攒得出来的那个。"""
        self.assertEqual(shop.COMPOSE_MATERIAL,
                         self.check(money=0, materials={})[1])

    def test_同一种材料写两格会合并(self):
        recipe = dict(self.RECIPE,
                      materials=[{"id": 30018, "count": 2},
                                 {"id": 30018, "count": 3}])
        self.assertEqual({30018: 5}, shop.recipe_material_map(recipe))

    def test_按产物找配方只认上架的(self):
        self.assertIsNone(shop.find_recipe(
            1010001, [dict(self.RECIPE, listed=False)]))
        self.assertIsNotNone(shop.find_recipe(1010001, [self.RECIPE]))


if __name__ == "__main__":
    unittest.main()
