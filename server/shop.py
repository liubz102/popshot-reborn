#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shop.py —— 商店 / 合成 / 仓库那几发包的**纯函数**（V0.3商店 M4 第 2 步 / M5）。

这里只有「字节 ↔ 数据」和「哪件商品归哪个分类」。**不碰连接、不碰存档、
不写日志** —— `gameserver.py` 负责「解包 → 调这里 → 组包 → 发」（照 §10 的接入点）。

★ 协议出处：`re/packet_api.md` §3.8 / FINDINGS §20 ~ §23。三条最要命的：

1. **`0x0600` 是客户端方向的「货架目录请求」**，和服务端方向的
   `gspRepMoney` **同号反向**。收到它千万别当金币包处理。
2. `0x0500` 的元素是**三层嵌套**（货架格子 → 买法档位 → 商品详情 → 赠品清单），
   不是「3 个 int」。
3. **发下去的 itemId 必须 `shopdata.ownable()`** —— 客户端对查不到的 id
   会收集起来弹提示框（`0x445f8c` / `0x447406`）。

⚠ 下行三发（`0x0500` / `0x0604` / `0x0508`）**一次都没在线上验过**，
全是静态反汇编结论。别在文档里把它们升成 ✅。
"""
import struct

import shopdata
import shopcfg


# ---------------------------------------------------------------------------
# 线上原语。★ 和 `gameserver.w_*` 是同一套写法，这里自带一份是为了让本模块
#   能被单独 import（`gameserver` 反过来 import 它，不能循环）。
# ---------------------------------------------------------------------------
def w_i32(v):
    return struct.pack("<i", int(v))


def w_u16(v):
    return struct.pack("<H", int(v) & 0xFFFF)


def w_byte(v):
    """写**一个原始字节**。★ 不是 `gameserver.w_u8` —— 那个是 bool 写法。"""
    return struct.pack("<B", int(v) & 0xFF)


def w_wstr(s):
    """`u16 字符数 + UTF-16LE`（流原语 `0x5d5a5a`）。"""
    text = "" if s is None else str(s)
    return struct.pack("<H", len(text)) + text.encode("utf-16le")


# ---------------------------------------------------------------------------
# 分类 id（FINDINGS §22）
#
# 编码是 `(组 << 16) | 序号`，**低半字就是 §6 的部位码**。
# `(组 << 16) | 0` 是那一组的父标签，要回子标签的并集。
# ---------------------------------------------------------------------------
#: `part_flag`（单件）→ 分类 id。★ 判据只能是 `part_flag`，不能是 id 里的
#: 部位码 —— §14 已经踩过一次（`1_08_0039` 的 `08` 实际是鞋）。
PART_FLAG_CATEGORY = {
    1: 0x10001,      # 상의 上衣
    2: 0x10002,      # 하의 下装
    16: 0x10003,     # 장갑 手套
    8: 0x10004,      # 신발 鞋
    4: 0x10005,      # 머리 头 / 脸
    1024: 0x60001,   # 武器槽 1
    2048: 0x60002,   # 武器槽 2
    4096: 0x60003,   # 武器槽 3
}
#: 套装：`part_flag` 是组合值（上衣+下装+头+鞋+手套 = 31 这类）。
CATEGORY_SET = 0x10006
#: 재료 材料。
CATEGORY_MATERIAL = 0x50001
#: 기타 其他 —— 兜底用，凡是归不进上面几档的都落这儿。
CATEGORY_OTHER = 0x40002

#: 客户端一页画 8 个格子（商店面板的槽位循环 `0x45b397: cmp .., 0x80` 步长 0x10）。
#: ⇒ 服务端必须按 8 条一页切，多发的画不出来。
PAGE_SIZE = 8

#: `0x0600` 载荷里那个「角色过滤」字节：分类属于组 3（용병 雇佣兵）时客户端
#: 强制填这个值（`0x445e75: cmp ax, 3`），意思是「不限角色」。
CHARACTER_ANY = 0xFF


def category_of(item_id):
    """一件商品归哪个分类 id。"""
    item = shopdata.get(item_id)
    if item is None:
        return CATEGORY_OTHER
    flag = item.part_flag
    if flag == 0:
        return CATEGORY_MATERIAL if item.kind == "material" else CATEGORY_OTHER
    known = PART_FLAG_CATEGORY.get(flag)
    if known is not None:
        return known
    # 组合值 = 一件顶好几个槽的套装。
    return CATEGORY_SET


#: `shelf` 调试命令用的「不过滤」哨兵。★ **不能用 0** —— 分类 `0` 是
#: 「人物 → 英雄」那个标签的真 id（§22），拿它当通配的后果就是
#: 2026-09-05 实机看到的「点英雄结果列出一堆武器」。
CATEGORY_ALL = -1


def category_matches(requested, item_category):
    """客户端点的这个标签，要不要收这件商品。

    三种情况：

    * `CATEGORY_ALL`（`-1`，只有调试命令会给）—— 全收；
    * `(组 << 16)` 父标签 —— 收该组下面所有子标签；
    * 具体子标签 —— 只收它自己。

    ⚠ **`0` 不是通配**。它是「人物 → 英雄」标签的 id（§22 实测），
    按父标签规则算就是「组 0」—— 我们一件商品都不归在组 0，所以它
    自然落成空货架，这正是原版该有的样子。
    """
    requested = int(requested)
    if requested == CATEGORY_ALL:
        return True
    if requested == item_category:
        return True
    if requested & 0xFFFF == 0:
        return (requested >> 16) == (int(item_category) >> 16)
    return False


# ---------------------------------------------------------------------------
# `0x0600` gcpReqShopItemList（客户端 → 服务端）—— 11 字节
# ---------------------------------------------------------------------------
class ShopListRequest(object):
    """`0x0600` / `0x0605` 的载荷。**两发的线格式一模一样**（§20）。"""

    __slots__ = ("character", "category", "page", "flag")

    def __init__(self, character=0, category=0, page=0, flag=0):
        self.character = character
        self.category = category
        self.page = page
        self.flag = flag

    def __eq__(self, other):
        return (isinstance(other, ShopListRequest)
                and (self.character, self.category, self.page, self.flag)
                == (other.character, other.category, other.page, other.flag))

    def __repr__(self):
        return ("<ShopListRequest 角色=%s 分类=%#x 页=%d 排序=%s>"
                % ("不限" if self.character == CHARACTER_ANY else self.character,
                   self.category & 0xFFFFFFFF, self.page,
                   sort_name(self.flag)))


#: `0x0600` / `0x0605` 的载荷长度。★ **`u8 + i32 + u16 + i32`**，
#: 不是 `u8 + u16 + i32 + i32` —— 后者是 §19 记错过一次的形状（判据见 §20）。
SHOP_LIST_REQUEST_SIZE = 11


def parse_shop_list_request(payload):
    """`0x0600` / `0x0605` 的载荷 → `ShopListRequest`；长度不对就抛 `ValueError`。"""
    if len(payload) != SHOP_LIST_REQUEST_SIZE:
        raise ValueError("货架/配方列表请求应当是 %d 字节，收到 %d"
                         % (SHOP_LIST_REQUEST_SIZE, len(payload)))
    character, category, page, flag = struct.unpack("<BiHi", payload)
    return ShopListRequest(character, category, page, flag)


def build_shop_list_request(character=0, category=0, page=0, flag=0):
    """反过来组一发（只给测试和协议试探用，服务端自己不发这个方向）。"""
    return struct.pack("<BiHi", int(character) & 0xFF, int(category),
                       int(page) & 0xFFFF, int(flag))


# ---------------------------------------------------------------------------
# `0x0500` gspRepShopItemList（服务端 → 客户端）
# ---------------------------------------------------------------------------
def build_shop_stock(item_id, name, price, currency=0, unknown1=0,
                     note="", unknown2=0, grants=()):
    """一档买法的详情（`ShopStock`，Des `0x44360a`）。

    | 线上 | 含义 |
    |---|---|
    | i32 | 物品 id（图标就是拿它查 ItemDB 的）|
    | wstr | 商品名，画在格子上 |
    | i32 | **价格**（旁边的标签是 `가격`）|
    | i32 | ❓ 未查明，填 0 |
    | i32 | 货币：`0` = 픽셀（中文版叫「金币」）/ 非 0 = 캐시（「游戏币」）|
    | wstr | ❓ 未查明，填空串 |
    | i32 | ❓ 未查明，填 0 |
    | i32 n + n×`Item@ShopStock` | **买了到手的东西** |

    `grants` 不给就默认「买什么到手什么」= `[item_id]`。
    ★ 每个 `Item@ShopStock` 是 3 个 int，只有第一个（物品 id）被客户端读
    （`0x4159e7` 拿它查 ItemDB），后两个语义未查明，填 0。
    """
    ids = list(grants) if grants else [int(item_id)]
    body = (w_i32(item_id)
            + w_wstr(name)
            + w_i32(price)
            + w_i32(unknown1)
            + w_i32(currency)
            + w_wstr(note)
            + w_i32(unknown2)
            + w_i32(len(ids)))
    for granted in ids:
        body += w_i32(granted) + w_i32(0) + w_i32(0)
    return body


def build_shop_stock_group(options):
    """货架上的一个格子（`ShopStockGroup`）= 若干「买法档位」。

    `options` 是 `[(档位名, ShopStock 字节), …]`。
    ★ 本版只卖永久，一个格子就一档 —— 档位名那个 wstr 语义未查明（🤔 猜是
    「7일 / 30일」这类期限标签），填空串。
    """
    body = w_i32(len(options))
    for label, stock in options:
        body += w_wstr(label) + stock
    return body


def build_rep_shop_item_list(total_pages, page, groups):
    """opcode `0x0500` 的包体。

        i32 总页数 -> 面板 +0x14c；i32 当前页 -> 面板 +0x148；i32 n + n×格子

    ⚠ **总页数要说实话**：处理器 `0x45b300` 拿它夹当前页 ——
    发 0 的话客户端会把当前页拉回 0，玩家一翻页就弹回来。
    """
    body = w_i32(total_pages) + w_i32(page) + w_i32(len(groups))
    return body + b"".join(groups)


# ---------------------------------------------------------------------------
# 货架内容：从 `shop.json` 里挑出「这一页该显示什么」
# ---------------------------------------------------------------------------
#: 货架右下角那两个排序按钮（`0x0600` 载荷最后那个标志位，§25）。
#: 客户端把它当 bool：面板 `+0x160`，点第一个按钮写 0、点第二个写 1
#: （`0x45b5c4` / `0x45b5d5`），然后重发一发 `0x0600`。
#: **排序是服务端的活** —— 一页只发 8 件，客户端没有全表可排。
SORT_BASIC = 0        # 「基本顺序」= 进商店的默认状态（面板那个字节是 0）
SORT_RELEASE = 1      # 「上市顺序」


def sort_name(order):
    """日志用的中文名。★ 认不出来的值照原样印 —— 别悄悄当成默认排序，
    那样实机看日志时会以为「客户端只发过 0 和 1」。"""
    try:
        order = int(order)
    except (TypeError, ValueError):
        return repr(order)
    if order == SORT_BASIC:
        return "基本顺序"
    if order == SORT_RELEASE:
        return "上市顺序"
    return "未知(%d)" % order


def sort_entries(entries, order=SORT_BASIC):
    """按玩家选的顺序排货架。

    * `SORT_BASIC`「基本顺序」—— itemId 升序。id 是 `角色·部位·系列·档次`
      编出来的（§6），升序排出来同系列相邻、由低到高，就是玩家看惯的那个样子。
    * `SORT_RELEASE`「上市顺序」—— **原版目录顺序倒过来**（新的在前）。
      出处和「为什么只能这么近似」写在 `shopdata.catalog_index()` 里。

    ⚠ 名次相同的一律再按 id 兜底 —— 排序必须**全序**，否则同一批商品
    两次请求可能给出不同的页，玩家翻页会看到重复或漏掉的格子。
    """
    try:
        order = int(order)
    except (TypeError, ValueError):
        order = SORT_BASIC
    if order == SORT_RELEASE:
        return sorted(entries, key=lambda e: (-shopdata.catalog_index(e["id"]),
                                              int(e["id"])))
    return sorted(entries, key=lambda e: int(e["id"]))


def shelf_entries(category=CATEGORY_ALL, character=CHARACTER_ANY, data_dir=None,
                  order=SORT_BASIC):
    """该分类下**全部**上架商品，按 `order` 指定的顺序排。返回 `(条目, 警告)`。

    三道过滤，缺一不可：

    1. `listed` —— 管理页里那个上架开关；
    2. `shopdata.ownable()` —— 只有货架条目的那 226 件进不了背包（§11），
       发下去客户端查不到持有物定义，格子是空的；
    3. 角色限定 —— `character` 给 `CHARACTER_ANY`（客户端在雇佣兵标签下就发
       这个）时不过滤，否则只留这个角色能用的。

    ★ 配置读坏时 `shopcfg.shop()` 返回**上一份好的**（一次都没读成功过就返回
    空表）并给出警告，**绝不回写**（D10）⇒ 最坏情况是「货架空着」。
    """
    table, warnings = shopcfg.shop(data_dir)
    out = []
    for item_id in table:
        entry = table[item_id]
        if not entry.get("listed"):
            continue
        if not shopdata.ownable(item_id):
            continue
        if not category_matches(category, category_of(item_id)):
            continue
        if character != CHARACTER_ANY and not shopdata.usable_by(item_id, character):
            continue
        out.append(entry)
    return sort_entries(out, order), warnings


def page_count(total):
    """`total` 件商品要几页。★ **一件都没有也算一页** —— 总页数发 0 的话
    客户端会把当前页夹成 -1（`0x45b2be` 的 `ecx-1`），没必要冒这个险。"""
    if total <= 0:
        return 1
    return (total + PAGE_SIZE - 1) // PAGE_SIZE


#: 「那三个还没查明的字段」的探针值（`shelf-probe on` 用）。
#:
#: `ShopStock` 里有三格语义未查明（一个 i32、一个 wstr、又一个 i32，§21）。
#: 与其对着反汇编再啃一天，不如**在界面上找它们** —— 填成一眼认得出的值，
#: 实机看一下哪个数字 / 哪串字冒出来在哪儿，一轮就对上了。
#: ★ 挑的值要「一看就不是正常数据」：`777` / `888` 不会和价格等级撞车。
SHELF_PROBE = (777, "※探针※", 888)


def shelf_page(category=CATEGORY_ALL, page=0, character=CHARACTER_ANY,
               data_dir=None, probe=None, order=SORT_BASIC):
    """把一页货架组成 `0x0500` 的包体。返回 `(包体, 这一页的条目, 警告)`。

    页号越界就夹回 `[0, 总页数-1]` —— 和客户端 `0x45b2be` 的夹法一致，
    这样「服务端说第几页」和「客户端显示第几页」永远一致。

    `order` 是请求里那个标志位（`SORT_BASIC` / `SORT_RELEASE`）。
    `probe` 给 `SHELF_PROBE` 那样的三元组时，把 `ShopStock` 里三个未查明的
    字段填成探针值（默认 `None` = 全填 0 / 空串）。
    """
    entries, warnings = shelf_entries(category, character, data_dir, order)
    pages = page_count(len(entries))
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    page = max(0, min(page, pages - 1))
    shown = entries[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    unknown1, note, unknown2 = probe or (0, "", 0)
    groups = []
    for entry in shown:
        stock = build_shop_stock(entry["id"], entry.get("name") or "",
                                 entry.get("price", 0),
                                 unknown1=unknown1, note=note,
                                 unknown2=unknown2)
        groups.append(build_shop_stock_group([("", stock)]))
    return build_rep_shop_item_list(pages, page, groups), shown, warnings


# ---------------------------------------------------------------------------
# `0x0604` gspRepEquippedList（服务端 → 客户端）
# ---------------------------------------------------------------------------
#: `Equipment` 前面那 12 个字节（3 个槽位掩码）。**处理器 `0x447278` 不读它**
#: （`0x404c3f` 原样读 12 字节存进 `Equipment+0x0c` 就再没人碰过）⇒ 填 0。
EQUIPPED_SLOT_MASK_BYTES = 12


def build_rep_equipped_list(item_ids=(), slot_masks=(0, 0, 0)):
    """opcode `0x0604` 的包体 —— 和 `0x030b` **只差一个座位号**。

        12 字节 掩码×3 + i32 物品数 + 物品数 × i32 物品 id

    ⚠ 客户端对**查不到的 id** 会收集起来弹提示框（`0x447406`）⇒ 调用方要先
    过一遍 `shopdata.ownable()`。这里不替它过滤：本函数是纯组包，
    「发什么」的判断留在调用点（和 `build_slot_equipped_list` 一个口径）。
    """
    masks = tuple(slot_masks)
    if len(masks) * 4 != EQUIPPED_SLOT_MASK_BYTES:
        raise ValueError("装备清单需要正好 3 个槽位掩码，收到 %d 个" % len(masks))
    items = [int(item_id) for item_id in item_ids]
    return (b"".join(w_i32(mask) for mask in masks)
            + w_i32(len(items))
            + b"".join(w_i32(item_id) for item_id in items))


def displayable_items(item_ids):
    """挑出「商店界面认得出来」的那些 id（`0x0604` / `0x0500` 都要过这一道）。"""
    return [int(item_id) for item_id in item_ids if shopdata.ownable(item_id)]


# ---------------------------------------------------------------------------
# `0x0602` 购买（客户端 → 服务端）/ `0x0502` gspRepItemBuy（回）
# ---------------------------------------------------------------------------
def parse_item_buy_request(payload):
    """`0x0602` 的载荷 → `[itemId, …]`（购物车里那几件，§24）。

    线格式 `i32 n + n×i32`（Ser `0x55938f`）。那些 int 是**物品 id** ——
    客户端遍历购物车，每格取 `ShopStock+4`（`0x456923` 的 `add eax, 4`，
    条目步长 `0x2c` = `ShopStock` 的大小）。
    """
    if len(payload) < 4:
        raise ValueError("购买请求至少要有一个 int32 计数，收到 %d 字节"
                         % len(payload))
    count = struct.unpack_from("<i", payload, 0)[0]
    if count < 0 or len(payload) != 4 + count * 4:
        raise ValueError("购买请求说有 %d 件，载荷却是 %d 字节"
                         % (count, len(payload)))
    return list(struct.unpack_from("<%di" % count, payload, 4)) if count else []


def build_rep_item_buy(ok, unknown1=0, unknown2=0):
    """opcode `0x0502` 的包体 —— `int32 bool + i32 + i32`（Des `0x54c891`）。

    ★ 第一格在线上是 **4 字节**（`0x5d59de` = int32 → bool），内存里才是 1 字节。
    ⚠ 失败（`ok = 0`）时客户端**什么都不显示** —— 处理器 `0x44643d` 只是把
    刚建好的「购买结果」弹窗析构掉。⇒ 失败原因只能进服务端日志，
    玩家那边看到的是「点了没反应」。别指望客户端替我们解释。

    后两格语义 ❓未查明（`+8` 会被购买结果弹窗 `0x4586e1` 用到）。
    填 0；要找它们就用 `shelf-probe` 那套探针法（D19）。
    """
    return w_i32(1 if ok else 0) + w_i32(unknown1) + w_i32(unknown2)


#: 买不成的原因码。★ 只进日志 —— 客户端在失败路径上不显示任何东西（见上）。
BUY_NOT_LISTED = "not_listed"
BUY_UNKNOWN_ITEM = "unknown_item"
BUY_LEVEL = "level_too_low"
BUY_CHARACTER = "wrong_character"
BUY_ALREADY_OWNED = "already_owned"


def check_purchase(item_id, table, level, character, owned):
    """一件商品能不能买。返回 `(条目, 原因)`；能买时原因是 `None`。

    ★ **价格和上架与否只信 `shop.json`**（`table`），包里的任何数值都不作数
    —— 客户端发上来的只有 itemId，价格是我们自己查的（PLAN M5）。

    ★ 「已拥有就不能再买」是原版规则（失败文案 `이미 소지하고 있습니다`，§7）。
    材料类不受这条约束，但材料本来也不上架。
    """
    entry = table.get(int(item_id))
    if entry is None or not entry.get("listed"):
        return None, BUY_NOT_LISTED
    if not shopdata.ownable(item_id):
        return None, BUY_UNKNOWN_ITEM
    if level is not None and level < entry.get("level", 1):
        return entry, BUY_LEVEL
    if character is not None and not shopdata.usable_by(item_id, character):
        return entry, BUY_CHARACTER
    if int(item_id) in owned and shopdata.get(item_id).part_flag != 0:
        return entry, BUY_ALREADY_OWNED
    return entry, None


# ---------------------------------------------------------------------------
# `0x0702` 穿上 / `0x0703` 脱下（客户端 → 服务端）—— 都回 `0x0604`
# ---------------------------------------------------------------------------
def parse_equip_request(payload):
    """`0x0702` / `0x0703` 的载荷 → itemId。单 `i32`（Ser `0x559464` / `0x55949e`）。"""
    if len(payload) != 4:
        raise ValueError("穿脱请求应当是 4 字节，收到 %d" % len(payload))
    return struct.unpack("<i", payload)[0]


# ---------------------------------------------------------------------------
# `0x0508` gspRepGiftList（服务端 → 客户端）
# ---------------------------------------------------------------------------
def build_rep_gift_list(gifts=()):
    """opcode `0x0508` 的包体 = 单个 `vector<Gift>`（Des `0x443b33`）。

    ★ **本版不做礼物**（PLAN「本版不做」），所以只支持空清单 = 4 个 0 字节。
    留这个参数是为了让「以后要做」时函数签名不用改；给了非空就抛 ——
    `Gift` 的线格式还没逆，静默发个半成品比不发更难查。
    """
    if gifts:
        raise ValueError("Gift 的线格式还没逆出来，本版只支持空礼物清单")
    return w_i32(0)
