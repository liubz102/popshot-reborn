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

★ 本模块还导出三个给 `test_gameserver` 用的东西：
`parse_shop_item_list` / `parse_rep_equipped_list` / `shop_config`。
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
    """`0x0604` 的包体 → `[物品 id]`（掩码那 12 字节顺手断言是 0）。"""
    wire = _Wire(body)
    masks = [wire.i32() for _ in range(3)]
    assert masks == [0, 0, 0], masks
    items = [wire.i32() for _ in range(wire.i32())]
    wire.done()
    return items


@contextlib.contextmanager
def shop_config(items=(), data_dir=None):
    """临时把 `shopcfg` 指到一份现搓的 `shop.json` 上。

    ★ 全量测试默认把 `shopcfg.DATA_DIR` 指向**空目录**（`run_tests.py`），
    这样「货架上有什么」就不取决于开发机上那份用户随时在改的运营配置。
    要具体商品的用例用这个上下文管理器自己铺。
    """
    saved = shopcfg.DATA_DIR
    with tempfile.TemporaryDirectory() as tmp:
        target = data_dir or tmp
        path = shopcfg.path_of(shopcfg.SHOP_FILENAME, target)
        with open(path, "w", encoding="utf-8", newline="\n") as fp:
            json.dump({"format": 1, "items": list(items)}, fp,
                      ensure_ascii=False)
        shopcfg.DATA_DIR = target
        shopcfg.invalidate()
        try:
            yield target
        finally:
            shopcfg.DATA_DIR = saved
            shopcfg.invalidate()


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
        with shop_config(self.listed(1510001, 1120041)):
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

    def test_探针不开的时候三个未查明字段全是空的(self):
        # 默认必须是「什么都不填」—— 探针是**找字段**用的临时手段，
        # 平时挂着会往界面上糊一堆假数据。
        with shop_config(self.listed(1120041)):
            body, _, _ = shop.shelf_page()
        self.assertNotIn("※探针※".encode("utf-16le"), body)
        with shop_config(self.listed(1120041)):
            probed, _, _ = shop.shelf_page(probe=shop.SHELF_PROBE)
        self.assertIn("※探针※".encode("utf-16le"), probed)
        # 探针只动那三格，商品本身照旧 ⇒ 开着它也能正常买东西。
        self.assertEqual(parse_shop_item_list(body)[2],
                         parse_shop_item_list(probed)[2])

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

    def test_装备清单的掩码是十二个零字节(self):
        # 处理器 `0x447278` 压根不读这 12 字节（`0x404c3f` 读进
        # `Equipment+0x0c` 就再没人碰过）⇒ 填 0，别自作主张塞东西。
        body = shop.build_rep_equipped_list([])
        self.assertEqual(b"\x00" * 12, body[:12])
        self.assertEqual(shop.EQUIPPED_SLOT_MASK_BYTES, 12)
        self.assertEqual([], parse_rep_equipped_list(body))

    def test_掩码个数不对就抛(self):
        with self.assertRaises(ValueError):
            shop.build_rep_equipped_list([], slot_masks=(0, 0))

    def test_只把客户端认得的_id_塞进清单(self):
        # 查不到的 id 会让客户端收集起来弹提示框（`0x447406`）。
        self.assertEqual([1120041],
                         shop.displayable_items([1120041, 1510001, 9999999]))

    def test_礼物清单只支持空的(self):
        self.assertEqual(b"\x00\x00\x00\x00", shop.build_rep_gift_list())
        with self.assertRaises(ValueError):
            # `Gift` 的线格式还没逆 —— 静默发个半成品比不发更难查。
            shop.build_rep_gift_list([{"id": 1}])


if __name__ == "__main__":
    unittest.main()
