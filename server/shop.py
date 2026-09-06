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


def w_i16(v):
    """写 2 字节有符号（流原语 `0x5d59f1` 读 2 字节 + `movsx`）。"""
    return struct.pack("<h", int(v))


def w_f64(v):
    """写 8 字节 double（流原语 `0x5d5a0d` 读 8 字节）。"""
    return struct.pack("<d", float(v))


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

#: 「신상품 / 新商品」标签（商店和合成面板都有，而且**合成面板打开时默认
#: 停在这一格**）。
#:
#: ★ **它收全部** —— 用户 2026-09-06 拍板：「对复活项目的玩家来说所有物品
#:   都是新的」。原版按什么定「新」随 2009 年停服的服务端 DB 一起没了，
#:   而这一格空着的代价很实在：玩家打开合成界面第一眼就是空白。
#: ★ 排序照旧听请求里那个标志位（§25）：想看「最新的」就点「上市顺序」，
#:   那是 `shopdata.catalog_index()` 倒序（原版目录行序，新的在前）。
CATEGORY_NEW = 2


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
    if requested == CATEGORY_NEW:
        return True                 # 「新商品」= 全收，见 `CATEGORY_NEW`
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
def build_shop_stock(item_id, name, price, currency=0, list_price=None,
                     note="", unknown2=0, grants=()):
    """一档买法的详情（`ShopStock`，Des `0x44360a`）。

    | 线上 | 结构偏移 | 含义 |
    |---|---|---|
    | i32 | `+0x04` | 物品 id（图标就是拿它查 ItemDB 的）|
    | wstr | `+0x08` | 商品名，画在格子上 |
    | i32 | `+0x0c` | **价格**（旁边的标签是 `가격`）|
    | i32 | `+0x10` | ★ **划线原价**（提示框里「原价 → 现价」左边那个数）|
    | i32 | `+0x14` | 货币：`0` = 픽셀（中文版叫「金币」）/ 非 0 = 캐시（「游戏币」）|
    | wstr | `+0x18` | ★ **商品说明**（提示框下半那三行，`\\|` 分段，最多 3 段）|
    | i32 | `+0x1c` | ❓ 未查明，填 0 |
    | i32 n + n×`Item@ShopStock` | `+0x20` | **买了到手的东西** |

    ⚠⚠ **`list_price` 不给就等于 `price`** —— 提示框拿它和价格比
    （`0x45c354`），**不相等就画成「原价 → 现价」**。填 0 的话玩家看到的是
    「0 → 3000」（2026-09-05 实机现象）。要做打折就把原价填上去。

    ★ §26 那轮探针把 `+0x10` / `+0x18` 判成「死字段」，**那只对货架格子成立**
    —— 提示框（`0x45c302` 起）两个都读。§26 结尾自己写过这个坑。

    `grants` 不给就默认「买什么到手什么」= `[item_id]`。
    ★ 每个 `Item@ShopStock` 是 3 个 int，只有第一个（物品 id）被客户端读
    （`0x4159e7` 拿它查 ItemDB），后两个语义未查明，填 0。
    """
    ids = list(grants) if grants else [int(item_id)]
    body = (w_i32(item_id)
            + w_wstr(name)
            + w_i32(price)
            + w_i32(price if list_price is None else list_price)
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
    rules, more = shopcfg.items(data_dir)
    out = []
    for item_id in table:
        entry = table[item_id]
        if not entry.get("listed"):
            continue
        if not shopdata.ownable(item_id):
            continue
        if not category_matches(category, category_of(item_id)):
            continue
        # ★ 角色限定问**物品库**，不问 `shopdata`（D31）—— 管理员在物品库里
        #   把一件东西改成「不限」，货架上就该三个角色都看得到。
        if character != CHARACTER_ANY:
            limit = shopcfg.rule_of(rules, item_id)[1]
            if limit is not None and int(limit) != int(character):
                continue
        out.append(entry)
    return sort_entries(out, order), warnings + more


def page_count(total):
    """`total` 件商品要几页。★ **一件都没有也算一页** —— 总页数发 0 的话
    客户端会把当前页夹成 -1（`0x45b2be` 的 `ecx-1`），没必要冒这个险。"""
    if total <= 0:
        return 1
    return (total + PAGE_SIZE - 1) // PAGE_SIZE


#: 「还没查明的字段」的探针值（`shelf-probe on` 用）。
#:
#: 原来是三格（`ShopStock` 的 `+0x10` / `+0x18` / `+0x1c`，§21）。2026-09-05
#: 实机把前两格钉死了 —— `+0x10` = 划线原价、`+0x18` = 商品说明（§31），
#: **只剩 `+0x1c` 一格**，探针就缩成一个数。
#: ★ 挑的值要「一看就不是正常数据」：`888` 不会和价格 / 等级撞车。
SHELF_PROBE = 888

#: `0x0502` 第三格（`gspRepItemBuy` 的 `+0x0c`）也还没查明，同一套探针。
BUY_RESULT_PROBE = 888


def shelf_page(category=CATEGORY_ALL, page=0, character=CHARACTER_ANY,
               data_dir=None, probe=None, order=SORT_BASIC):
    """把一页货架组成 `0x0500` 的包体。返回 `(包体, 这一页的条目, 警告)`。

    页号越界就夹回 `[0, 总页数-1]` —— 和客户端 `0x45b2be` 的夹法一致，
    这样「服务端说第几页」和「客户端显示第几页」永远一致。

    `order` 是请求里那个标志位（`SORT_BASIC` / `SORT_RELEASE`）。
    `probe` 给 `SHELF_PROBE` 时，把 `ShopStock` 里**仅剩那一格**未查明的字段
    （`+0x1c`）填成探针值（默认 `None` = 填 0）。

    ★ **划线原价一律等于售价** —— 我们不做打折，填别的会让提示框画出
    「原价 → 现价」两个数（§31）。**名字**取物品库（`items.json`，D31），
    **说明**取自动生成的那一套（`shopcfg.item_desc_zh`），翻不出来就发空串
    （提示框那块留白）。
    """
    entries, warnings = shelf_entries(category, character, data_dir, order)
    # ★ 警告不在这儿再收一遍 —— `shelf_entries` 已经把物品库那份带出来了，
    #   加两次的话日志里同一句话会打两行。
    rules, _more = shopcfg.items(data_dir)
    pages = page_count(len(entries))
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    page = max(0, min(page, pages - 1))
    shown = entries[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    groups = []
    for entry in shown:
        stock = build_shop_stock(entry["id"],
                                 shopcfg.name_of(rules, entry["id"]),
                                 entry.get("price", 0),
                                 note=shopcfg.item_desc_zh(
                                     shopdata.get(entry["id"])),
                                 unknown2=0 if probe is None else int(probe))
        groups.append(build_shop_stock_group([("", stock)]))
    return build_rep_shop_item_list(pages, page, groups), shown, warnings


# ---------------------------------------------------------------------------
# `Equipment` 头上那 12 字节 = **每个角色的槽位掩码**（§31）
# ---------------------------------------------------------------------------
#: 三个角色（泰尔 / 卡希尔 / 布洛克）各一个 `PartFlag` 掩码。
EQUIPPED_SLOT_MASK_COUNT = 3


def equipment_slot_masks(item_ids):
    """一串穿着中的 itemId → 客户端要的三个槽位掩码。

    照客户端自己的 `Equipment::Equip`（`0x5583d3`）算：

    * 角色限定是 `0/1/2` 的，只点亮**那个角色**的掩码；
    * 角色限定是 `-1`（不限）的，**三个掩码全点亮**。

    ⚠⚠ **不能全发 0**（§23 原来那句「处理器不读它」是错的）：
    房间里的「卸下」按钮拿 `0x5584ab` 判「这件穿着没有」，判据正是
    `(掩码[角色] & PartFlag) == PartFlag`。掩码是 0 ⇒ 它认为你没穿 ⇒
    弹「已卸下。」然后**什么都不做**（2026-09-05 实机现象）。
    """
    masks = [0] * EQUIPPED_SLOT_MASK_COUNT
    for raw in item_ids or ():
        item = shopdata.get(raw)
        if item is None or not item.part_flag:
            continue
        character = item.character
        if character is None:
            for index in range(EQUIPPED_SLOT_MASK_COUNT):
                masks[index] |= item.part_flag
        elif 0 <= int(character) < EQUIPPED_SLOT_MASK_COUNT:
            masks[int(character)] |= item.part_flag
    return tuple(masks)


# ---------------------------------------------------------------------------
# `0x0604` gspRepEquippedList（服务端 → 客户端）
# ---------------------------------------------------------------------------
#: `Equipment` 前面那 12 个字节 = **3 个槽位掩码**（每个角色一个，§31）。
#: ⚠ §23 原来写「处理器不读它 ⇒ 填 0 就行」，**那是错的** ——
#: `0x5584ab`「这件穿着没有」就是拿它判的。用 `equipment_slot_masks()` 算。
EQUIPPED_SLOT_MASK_BYTES = 12


def build_rep_equipped_list(item_ids=(), slot_masks=None):
    """opcode `0x0604` 的包体 —— 和 `0x030b` **只差一个座位号**。

        12 字节 掩码×3 + i32 物品数 + 物品数 × i32 物品 id

    `slot_masks` 不给就按 `item_ids` 算（`equipment_slot_masks`）——
    **这是默认行为**，全 0 会让客户端认为「什么都没穿」（§31）。

    ⚠ 客户端对**查不到的 id** 会收集起来弹提示框（`0x447406`）⇒ 调用方要先
    过一遍 `shopdata.ownable()`。这里不替它过滤：本函数是纯组包，
    「发什么」的判断留在调用点（和 `build_slot_equipped_list` 一个口径）。
    """
    if slot_masks is None:
        slot_masks = equipment_slot_masks(item_ids)
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


#: `0x0502` 第二格 = **失败原因码**（✅实测：填 0 时界面写「未定义的错误」）。
#: 客户端 `0x4586e1` 的 `switch ([ebp+0xc])`（`0x45915f`）逐个对出来的：
#:
#: | 码 | 界面文案（中文版）|
#: |---|---|
#: | 1 | 还缺少 %d 金币 —— **%d 是客户端自己算的**（`0x457dae`），不在包里 |
#: | 2 | 还差 %d 游戏币 |
#: | 3 | 等级太低 |
#: | 4 | 等级太高 |
#: | 5 | 购物车已满 |
#: | 6 | 内部错误 |
#: | 7 | 已拥有的道具 |
#: | 其余（含 **0**）| 未定义的错误 |
BUY_REASON_NO_PIXEL = 1        # 金币不够
BUY_REASON_NO_CASH = 2         # 游戏币不够（本版不卖 캐시 商品）
BUY_REASON_LEVEL_LOW = 3
BUY_REASON_LEVEL_HIGH = 4
BUY_REASON_FULL = 5
BUY_REASON_INTERNAL = 6
BUY_REASON_ALREADY_OWNED = 7


def build_rep_item_buy(ok, reason=0, unknown2=0):
    """opcode `0x0502` 的包体 —— `int32 bool + i32 原因码 + i32 ❓`（Des `0x54c891`）。

    ★ 第一格在线上是 **4 字节**（`0x5d59de` = int32 → bool），内存里才是 1 字节。

    ⚠⚠ **`reason` 不是「未查明字段」** —— 2026-09-05 实机落锤：失败时客户端
    **会弹「购买失败」框**，正文就是拿这一格查表（`0x45915f` 的 switch）。
    填 0 = 玩家看到「未定义的错误」，等于我们知道原因却不说。
    ⇒ 每条拒绝都要挑一个 `BUY_REASON_*`。

    第三格语义仍然 ❓未查明，填 0；要找它就用 `shelf-probe` 那套探针法（D19）。
    """
    return w_i32(1 if ok else 0) + w_i32(reason) + w_i32(unknown2)


#: 买不成的原因（服务端自己的说法，进日志）→ 客户端的原因码。
#: ★ 两套分开：日志要看得出「是哪一条规则拦的」，界面只有 7 种说法。
BUY_NOT_LISTED = "not_listed"
BUY_UNKNOWN_ITEM = "unknown_item"
BUY_LEVEL = "level_too_low"
BUY_ALREADY_OWNED = "already_owned"
BUY_NO_MONEY = "not_enough_money"

BUY_REASON_CODE = {
    BUY_NOT_LISTED: BUY_REASON_INTERNAL,
    BUY_UNKNOWN_ITEM: BUY_REASON_INTERNAL,
    BUY_LEVEL: BUY_REASON_LEVEL_LOW,
    BUY_ALREADY_OWNED: BUY_REASON_ALREADY_OWNED,
    BUY_NO_MONEY: BUY_REASON_NO_PIXEL,
}


def buy_reason_code(reason):
    """服务端的拒绝理由 → 客户端认得的原因码；认不出来一律「内部错误」。

    ★ 兜底**不是 0** —— 0 在界面上是「未定义的错误」，那句话对玩家毫无信息，
    对我们也一样（分不清是新加了一条拒绝理由还是真的出了内部错误）。
    """
    return BUY_REASON_CODE.get(reason, BUY_REASON_INTERNAL)


def check_purchase(item_id, table, level, owned, data_dir=None):
    """一件商品能不能买。返回 `(条目, 原因)`；能买时原因是 `None`。

    ★ **价格和上架与否只信 `shop.json`**（`table`），包里的任何数值都不作数
    —— 客户端发上来的只有 itemId，价格是我们自己查的（PLAN M5）。

    ★ 「已拥有就不能再买」是原版规则（失败文案 `이미 소지하고 있습니다`，§7）。
    材料类不受这条约束，但材料本来也不上架。

    ⚠ **不按「玩家当前是哪个角色」拦** —— 商店上方那排角色箭头就是给
    「给别的角色买装备」用的（货架本来就按预览角色过滤，`shelf_entries`）。
    2026-09-05 实机：拿泰尔买布洛克的火箭筒被拦下，那是我们多加的规矩。
    """
    entry = table.get(int(item_id))
    if entry is None or not entry.get("listed"):
        return None, BUY_NOT_LISTED
    if not shopdata.ownable(item_id):
        return None, BUY_UNKNOWN_ITEM
    # ★ 等级门槛问**物品库**（D31）—— `shop.json` 里已经没有这个字段了。
    if level is not None and level < shopcfg.item_rule(item_id, data_dir)[0]:
        return entry, BUY_LEVEL
    if int(item_id) in owned and shopdata.get(item_id).part_flag != 0:
        return entry, BUY_ALREADY_OWNED
    return entry, None


# ---------------------------------------------------------------------------
# `0x0505` gspRepCompositionList（服务端 → 客户端）—— `0x0605` 的应答
#
# ★ 请求 `0x0605` 的线格式和 `0x0600` **一模一样**（含排序位），所以它复用
#   上面的 `parse_shop_list_request`。这里只管应答那一半。
# ---------------------------------------------------------------------------
#: 合成面板一页几条。`0x45f011` 的循环走 `0 .. 0x100` 步长 `0x20`（一条
#: `CompositionRule` 的内存大小）⇒ **8 格**，和货架一样。
COMPOSITION_PAGE_SIZE = 8

#: 合成界面点得到的**子标签**（`0x45e42f` 逐条读出来的，§33）。
#:
#: 顶级标签有 5 个（`0x45e2f0` 那张表，索引 0/1/2/8/9）：
#: **新商品 / 道具 / 装备 / 称号 / 活动** —— ✅ 2026-09-06 用户实机确认。
#: 「装备」下面是 头 / 上衣 / 下装 / 手套 / 鞋，「道具」下面是 装饰 / 其他；
#: 「称号」和「活动」这一版没有内容。
#:
#: ⚠ **没有武器、没有套装**（商店那棵树有 `0x60000` 和 `0x10006`，这棵没有）
#: ⇒ 产物归在那两类的配方，玩家只能在**新商品**那一格找到它
#: （`CATEGORY_NEW` 收全部，用户 2026-09-06 拍板）。
COMPOSITION_CATEGORIES = (
    CATEGORY_NEW,   # 신상품 新商品（顶级，收全部）
    0x10005,        # 머리 头
    0x10001,        # 상의 上衣
    0x10002,        # 하의 下装
    0x10003,        # 장갑 手套
    0x10004,        # 신발 鞋
    0x40001,        # 치장 装饰
    0x40002,        # 기타 其他
)


def build_composition_material(item_id, count):
    """一格材料（`CompositionMaterial`，vft `0x666104`，Des `0x4438dc`）。

    ★ 数量是 **i16**，不是 i32（确认框 `0x45d6xx` 是 `movsx eax, word [mat+8]`）。
    线上 6 字节，内存里步长 `0xc`。
    """
    return w_i32(item_id) + w_i16(count)


def build_composition_rule(result, cost, materials=(), days=0, unknown=0):
    """一条配方（`CompositionRule`，Des `0x44394d`，内存 `0x20`）。

    | 线上 | 含义 |
    |---|---|
    | i32 | ★ **产物 itemId** —— 图标 / 名字都拿它查 ItemDB，**`0x0606` 发回来的就是它** |
    | i32 | ★ **合成费用**（金币）—— 客户端**自己**拿 `[0x72e330]` 和它比，算得起买不起 |
    | i32 m + m×`CompositionMaterial` | 材料，**最多 4 种**（界面只有 4 个槽，§7）|
    | i32 | 产物有效天数：`>0` 显示「기간:%d일」，`<=0` 走 ItemDB 的修理次数 |
    | i32 | ❌ 死字段（五个消费点一个都没读，§27）|

    ★ **等级 / 角色限定不在包里** —— 提示框上那两行是客户端查 ItemDB 得到的，
    和货架同款（§21）。
    """
    materials = list(materials)
    if len(materials) > shopcfg.MAX_MATERIALS:
        raise ValueError("一条配方最多 %d 种材料，给了 %d 种"
                         % (shopcfg.MAX_MATERIALS, len(materials)))
    out = [w_i32(result), w_i32(cost), w_i32(len(materials))]
    for material in materials:
        out.append(build_composition_material(material["id"],
                                              material["count"]))
    out.append(w_i32(days))
    out.append(w_i32(unknown))
    return b"".join(out)


def build_rep_composition_list(total_pages, page, rules):
    """opcode `0x0505` 的包体 —— 和 `0x0500` 一个骨架（Des `0x443ae1`）。

    `i32 总页数 + i32 当前页 + i32 n + n×CompositionRule`。
    ★ 总页数写进面板 `+0x144`、当前页写进 `+0x140`，客户端会把当前页
    夹到 `[0, 总页数-1]`（`0x45efb9`）⇒ **一条配方都没有也得说「1 页」**。
    """
    rules = list(rules)
    return (w_i32(total_pages) + w_i32(page) + w_i32(len(rules))
            + b"".join(rules))


def recipe_entries(category=CATEGORY_ALL, character=CHARACTER_ANY,
                   data_dir=None, order=SORT_BASIC):
    """这个标签下**全部**能合成的配方，按 `order` 排。返回 `(配方, 警告)`。

    三道过滤：

    1. `listed` —— 管理页里那个开关；
    2. `shopdata.ownable()` 的产物 —— 和货架同一条约束（§11）：客户端表里
       查不到的 id 发下去是个空格子，处理器 `0x447575` 还会去要一遍定义；
    3. 分类 —— 合成面板的标签树只有 8 个（`COMPOSITION_CATEGORIES`）；
    4. 角色 —— 配方自己写了 `character` 就按它，没写就退回产物的角色限定。

    ⚠⚠ **故意没有等级过滤**（D27，2026-09-05 实机推翻了自己的第一版）：
    原版的合成面板**从头到尾不读玩家等级**（§32 那 49 处引用里
    `0x45c000`~`0x45f000` 一处都没有），`0x0506` 的结果码里也没有
    「等级太低」这一档 ⇒ 原版合成压根没有等级门。装备的等级要求在
    **穿的时候**由客户端自己判（`0x445817`，数来自 `shop.json`）。
    ★ 加过一版「等级不到就不列」，用户实机报「管理页明明上架了却看不到」
      —— 那正是这种发明出来的规则的典型症状。别再加回来。
    """
    table, warnings = shopcfg.recipes(data_dir)
    rules, more = shopcfg.items(data_dir)
    out = []
    for recipe in table:
        if not recipe.get("listed"):
            continue
        result = recipe["result"]
        if not shopdata.ownable(result):
            continue
        if not category_matches(category, category_of(result)):
            continue
        # ★ 角色限定是**产物自己的**属性，问物品库（D31）——
        #   配方里不再有 `character` 那个字段。
        if character != CHARACTER_ANY:
            limit = shopcfg.rule_of(rules, result)[1]
            if limit is not None and int(limit) != int(character):
                continue
        out.append(recipe)
    return sort_recipes(out, order), warnings + more


def sort_recipes(recipes, order=SORT_BASIC):
    """按玩家选的顺序排配方 —— 和货架同一套规矩（`sort_entries`），
    只是键取的是**产物 id**。★ 一律再按配方号兜底，排序必须是全序。"""
    try:
        order = int(order)
    except (TypeError, ValueError):
        order = SORT_BASIC
    if order == SORT_RELEASE:
        return sorted(recipes,
                      key=lambda r: (-shopdata.catalog_index(r["result"]),
                                     int(r["result"]), int(r["id"])))
    return sorted(recipes, key=lambda r: (int(r["result"]), int(r["id"])))


def composition_page(category=CATEGORY_ALL, page=0, character=CHARACTER_ANY,
                     data_dir=None, order=SORT_BASIC):
    """把一页配方组成 `0x0505` 的包体。返回 `(包体, 这一页的配方, 警告)`。

    页号的夹法和货架一致（`0x45efb9` 自己也会再夹一次），这样「服务端说
    第几页」和「客户端显示第几页」永远一样。
    """
    entries, warnings = recipe_entries(category, character, data_dir, order)
    pages = page_count(len(entries))
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    page = max(0, min(page, pages - 1))
    shown = entries[page * COMPOSITION_PAGE_SIZE:
                    (page + 1) * COMPOSITION_PAGE_SIZE]
    # ★ 天数恒发 0（D35）：本版一件期限物品都不卖，`recipe.json` 里也没有这个
    #   字段了。线上那一格还得占着 —— 包结构是客户端定的。
    rules = [build_composition_rule(r["result"], r.get("cost", 0),
                                    r.get("materials", ()), days=0)
             for r in shown]
    return build_rep_composition_list(pages, page, rules), shown, warnings


def composition_item_ids(recipes):
    """一页配方要用到的**全部** itemId（产物 + 材料），去重保序。

    ⚠ **材料也要**：`0x0505` 的处理器 `0x4475ad` 逐条把 `mat+4` 拿去查
    ItemDB，查不到就攒起来发一发 `0x0601`（用途标志 **3**）——
    不先喂定义，材料槽就是四个没名字的空格子。
    """
    seen = []
    for recipe in recipes:
        for item_id in [recipe["result"]] + [m["id"] for m
                                             in recipe.get("materials", ())]:
            if item_id not in seen:
                seen.append(int(item_id))
    return seen


# ---------------------------------------------------------------------------
# `0x0606` 合成（客户端 → 服务端）/ `0x0506` gspRepComposeItem
# ---------------------------------------------------------------------------
def parse_compose_request(payload):
    """`0x0606` 的载荷 → **产物 itemId**。单 `i32`（SetType `0x44768a`）。

    ⚠⚠ **它不是配方号**（`0x45d738: push [rule+4]` —— push 的是 `CompositionRule`
    的第一个字段，也就是产物）⇒ **一个产物只能有一条配方**，
    `recipe.json` 里 `result` 撞车 `shopcfg.validate_recipes` 会拒收。
    """
    if len(payload) != 4:
        raise ValueError("合成请求应当是 4 字节，收到 %d" % len(payload))
    return struct.unpack("<i", payload)[0]


#: `0x0506` 那一个 i32 = **结果码**（🔍静态，处理器 `0x4476c4` 的
#: `sub eax,0 / dec eax` 链逐档解出来的，§33）：
#:
#: | 码 | 标题 | 正文（中文版对应的话）|
#: |---|---|---|
#: | 0 | 合成成功 | 「合成成功。请在我的仓库里装备道具。」（产物有天数时多一行期限）|
#: | 1 | 合成失败 | 「金币不足…」|
#: | 2 | 合成失败 | 「材料不足…」|
#: | 4 | 合成失败 | 「要合成的道具已经在我的仓库里了。」|
#: | 其余（含 **3**）| 合成失败 | 「未知的错误」|
#:
#: ⚠ **没有「等级太低」这一档** —— 这就是为什么等级只当列表过滤用（D27）。
COMPOSE_OK = 0
COMPOSE_NO_MONEY = 1
COMPOSE_NO_MATERIAL = 2
COMPOSE_ALREADY_OWNED = 4
COMPOSE_UNKNOWN = 3           # 客户端在这一档上写「未知的错误」


def build_rep_compose_item(code=COMPOSE_OK):
    """opcode `0x0506` 的包体 —— 单 `i32` 结果码（Des `0x66623c` 那一族）。"""
    return w_i32(code)


#: 合不成的原因（服务端自己的说法，进日志）→ 客户端的结果码。
#: ★ 和购买那一套一样分两层：日志要看得出是哪条规则拦的，界面只有 4 种说法。
COMPOSE_NOT_LISTED = "not_listed"
COMPOSE_UNKNOWN_ITEM = "unknown_item"
COMPOSE_OWNED = "already_owned"
COMPOSE_MATERIAL = "not_enough_materials"
COMPOSE_MONEY = "not_enough_money"

COMPOSE_REASON_CODE = {
    COMPOSE_NOT_LISTED: COMPOSE_UNKNOWN,
    COMPOSE_UNKNOWN_ITEM: COMPOSE_UNKNOWN,
    COMPOSE_OWNED: COMPOSE_ALREADY_OWNED,
    COMPOSE_MATERIAL: COMPOSE_NO_MATERIAL,
    COMPOSE_MONEY: COMPOSE_NO_MONEY,
}


def compose_reason_code(reason):
    """服务端的拒绝理由 → 客户端认得的结果码；认不出来一律「未知的错误」。

    ★ 兜底和购买那边不一样：`0x0506` **没有「内部错误」这一档**，
    能挑的只有那三个具体原因和一个「未知」。
    """
    return COMPOSE_REASON_CODE.get(reason, COMPOSE_UNKNOWN)


def find_recipe(item_id, recipes):
    """产物 itemId → 那一条配方；没有就 `None`。

    ⚠ 只认 `listed` 的：管理页把一条关掉 = 合成界面里看不到它，
    那就不该还能靠一发手搓的 `0x0606` 合出来。
    """
    item_id = int(item_id)
    for recipe in recipes:
        if int(recipe["result"]) == item_id and recipe.get("listed"):
            return recipe
    return None


def check_compose(item_id, recipes, owned, money, materials):
    """这一条能不能合。返回 `(配方, 原因)`；能合时原因是 `None`。

    ★ **校验全做完再动存档** —— 和购买同一条理由（§30）：扣完钱才发现材料
    不够就成了「钱没了东西没有」，那是最难查的一种账。真正的原子性由
    `AccountStore.compose_item()` 在同一把锁里保证，这里只负责**挑原因码**。

    ★ 顺序是「不存在 → 已拥有 → 材料 → 金币」：客户端的错误框一次
    只显示一条，先报**玩家自己能补上**的那个（材料比金币更常见）。

    ⚠ **没有等级这一条**（D27）—— 原版合成不看等级，见 `recipe_entries`。
    """
    recipe = find_recipe(item_id, recipes)
    if recipe is None:
        return None, COMPOSE_NOT_LISTED
    result = int(recipe["result"])
    if not shopdata.ownable(result):
        return recipe, COMPOSE_UNKNOWN_ITEM
    if result in owned:
        return recipe, COMPOSE_OWNED
    for material in recipe.get("materials", ()):
        if int(materials.get(int(material["id"]), 0)) < int(material["count"]):
            return recipe, COMPOSE_MATERIAL
    if money is not None and money < int(recipe.get("cost", 0)):
        return recipe, COMPOSE_MONEY
    return recipe, None


def recipe_material_map(recipe):
    """一条配方的材料 → `{itemId: 数量}`（`AccountStore.consume_materials` 的入参）。"""
    out = {}
    for material in recipe.get("materials", ()):
        item_id = int(material["id"])
        out[item_id] = out.get(item_id, 0) + int(material["count"])
    return out


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


# ---------------------------------------------------------------------------
# `0x0601` 上行「这些 id 我不认识」/ `0x0501` 下行物品定义（§28）
#
# ★★ 客户端的 `ItemInfo` 表 `[0x72e1dc]` **开机是空的，只有服务端能填**。
#    本地 `ShopItem-Chn.ini` 填的是另一张表（图标 + PartFlag，`[0x72e1e0]`）。
#    ⇒ 不发这一发，「穿装备 / 买东西 / 仓库列表 / 左侧人物模型」全废，
#      客户端弹的是「无法从服务器读取道具信息」。
# ---------------------------------------------------------------------------
#: `ItemInfo+0x10` 的形态标志位（`0x443444` 按位挑提示文案，§28）。
ITEM_FLAG_COUNTED = 0x01      # 计数持有 ->「소지개수 : %d개」，数字取条目 +0x08
ITEM_FLAG_TIMED = 0x02        # 期限持有 ->「%d일」，天数取条目 +0x0c
ITEM_FLAG_EQUIPPABLE = 0x08   # ★ 可装备 —— `0x0604` 的处理器只认这一位
ITEM_FLAG_PERCENT = 0x10      # 和 TIMED 一起 =「100%」
ITEM_FLAG_MARK = 0x20         # ❓ 仓库格子上多画一个标记
ITEM_FLAG_GAMES = 0x40        # 按局数 ->「%d게임」

#: 角色限定的「不限」。★ **不能用 0** —— `0` 是「泰尔专用」（`0x44e1b8`
#: 拿 `0xffff` 判不限，其余值当角色下标）。
CHARACTER_UNLIMITED = -1

#: `item_info_of(character=…)` 的「没传」哨兵。★ **不能用 `None`** ——
#: `None` 在这个字段上是有意义的值（「不限角色」），拿它当「没传」就没法
#: 表达「管理员在物品库里把这件东西改成了不限」（D31）。
_ORIGINAL = object()

#: 修理次数上限的「不限」。`< 0` 时客户端跳过「修够次数了」那条分支
#: （`0x412934: test eax,eax / jl`）。本版不做修理，一律发它。
REPAIR_UNLIMITED = -1

#: 请求里那个用途标志的取值（§28）。**必须原样回**，`ShopStage` 只在
#: 标志 == `ITEM_INFO_FOR_EQUIPPED` 时重建左侧人物模型（`0x44602a`）。
ITEM_INFO_FOR_SHELF = 0       # 货架 0x0500 里有不认识的 id
ITEM_INFO_FOR_INVENTORY = 1   # 持有物 0x0601 里有不认识的 id
ITEM_INFO_FOR_EQUIPPED = 2    # ★ 装备清单 0x0604 里有不认识的 id
ITEM_INFO_FOR_COMPOSITION = 3  # 合成配方 0x0505 里有不认识的 id（`0x447603: push 3`）
ITEM_INFO_FOR_RESULT = 5      # 结算界面


def parse_item_info_request(payload):
    """`0x0601`（**客户端方向**）的载荷 → `([itemId, …], 用途标志)`。

    线格式 `i32 n + n×i32 + u8`（Ser `0x559318` + `0x5540cd` 那个尾字节）。

    ⚠⚠ 和服务端方向的 `0x0601`（持有物清单，`build_rep_inventory`）**同号反向**。
    """
    if len(payload) < 5:
        raise ValueError("物品定义请求至少要 i32 计数 + 1 字节标志，收到 %d 字节"
                         % len(payload))
    count = struct.unpack_from("<i", payload, 0)[0]
    if count < 0 or len(payload) != 5 + count * 4:
        raise ValueError("物品定义请求说有 %d 个 id，载荷却是 %d 字节"
                         % (count, len(payload)))
    ids = list(struct.unpack_from("<%di" % count, payload, 4)) if count else []
    return ids, payload[-1]


def build_item_info(item_id, name="", part_flag=0, flags=0, level=0,
                    character=CHARACTER_UNLIMITED, durability=0,
                    repairs=REPAIR_UNLIMITED, desc=""):
    """一条 `ItemInfo`（Des `0x5586a6`）。字段表和出处见 FINDINGS §28。

    ★ 四个 ❓字段（结构偏移 `+0x08` / `+0x20` / `+0x28` / `+0x2c`）
    全镜像找不到消费点，和 `ShopStock` 剩下那格同款 —— 填 0。
    ★ `desc`（`+0x18`）是**仓库提示框下半那三行**（`0x4554e7` 按 `|` 切成
    最多 3 段，§31）。原版的说明文字随服务端 DB 一起没了。
    """
    return (w_i32(item_id)          # +0x04 itemId（map 的 key）
            + w_i32(0)              # +0x08 ❓
            + w_i32(part_flag)      # +0x0c PartFlag 部位掩码
            + w_i32(flags)          # +0x10 形态标志
            + w_wstr(name)          # +0x14 物品名
            + w_wstr(desc)          # +0x18 ★ 物品说明（| 分段，最多 3 段）
            + w_i32(level)          # +0x1c ★ 等级要求（> 玩家等级就穿不上）
            + w_i32(0)              # +0x20 ❓
            + w_i16(character)      # +0x24 ★ 角色限定，-1 = 不限
            + w_i32(0)              # +0x28 ❓
            + w_i32(0)              # +0x2c ❓
            + w_i32(durability)     # +0x30 最大耐久
            + w_i32(repairs))       # +0x34 修理次数上限，< 0 = 不限


def build_rep_item_info(records, purpose=ITEM_INFO_FOR_SHELF):
    """opcode `0x0501` 的包体 —— `i32 n + n×ItemInfo + u8 用途标志`。

    ⚠⚠ `purpose` **必须是请求里那个字节的原值**：`ShopStage::vft[0xb4]`
    （`0x44602a`）第一句就是 `cmp byte, 2`，只有 `2` 才重建左侧人物模型。
    主动下发（没人问就发）时用 `ITEM_INFO_FOR_SHELF`（0）—— 它在客户端
    那边是「只入表，不动界面」。
    """
    body = w_i32(len(records))
    for record in records:
        body += record
    return body + w_byte(purpose)


def item_info_of(item_id, name=None, level=0, character=_ORIGINAL, desc=""):
    """按 `shopdata` 的物品表派生一条 `ItemInfo`；表里没有返回 `None`。

    * `name` 不给就退回韩文名（`shopcfg.item_name_zh` 由调用方决定要不要用）；
    * **形态标志**：占槽位的发 `ITEM_FLAG_EQUIPPABLE`，其余（材料 / 消耗品）
      发 `ITEM_FLAG_COUNTED`。本版不卖期限物，一件都不发 `ITEM_FLAG_TIMED`；
    * **角色限定**：`None`（不限）要翻成 `-1`，**不能是 0**（§28）。
      不传 `character` 就照原版数据；管理员在物品库里改过就传那一份（D31）；
    * `desc` 是提示框下半那块（`+0x18`，`|` 分段最多 3 段，§31）。
    """
    item = shopdata.get(item_id)
    if item is None:
        return None
    if item.part_flag:
        flags = ITEM_FLAG_EQUIPPABLE
    else:
        flags = ITEM_FLAG_COUNTED
    if character is _ORIGINAL:
        character = item.character          # 不给就照原版数据
    if character is None:
        character = CHARACTER_UNLIMITED
    return build_item_info(item.id,
                           name=name if name is not None else (item.name_kr or ""),
                           part_flag=item.part_flag,
                           flags=flags,
                           level=level,
                           character=int(character),
                           desc=desc)


def item_info_records(item_ids, data_dir=None):
    """一串 id → `(ItemInfo 字节表, 认不出来被跳掉的 id)`。

    名字、等级门槛、角色限定**全部取物品库**（`items.json`，D31）；物品库里
    没登记就退回 `shopcfg.item_name_zh()` 翻的中文名 + 不限等级 + 原版角色。

    ⚠ **等级发的是物品库里那个门槛** —— 客户端拿它挡「穿上」
    （`0x445817`），发大了会让玩家「买到了却穿不上」。
    """
    rules, warnings = shopcfg.items(data_dir)
    records = []
    skipped = []
    for raw in item_ids:
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            skipped.append(raw)
            continue
        item = shopdata.get(item_id)
        if item is None:
            skipped.append(item_id)
            continue
        name = shopcfg.name_of(rules, item_id)
        level, character = shopcfg.rule_of(rules, item_id)
        record = item_info_of(item_id, name=name, level=level,
                              character=character,
                              desc=shopcfg.item_desc_zh(item))
        if record is None:
            skipped.append(item_id)
            continue
        records.append(record)
    return records, skipped, warnings


# ---------------------------------------------------------------------------
# `0x0700` 上行「给我持有物清单」/ `0x0601` 下行持有物清单（§29）
# ---------------------------------------------------------------------------
#: 「永久持有」在线上就是 `0.0` 分钟。★ 处理器 `0x554273` 拿它和 `0.0` 比，
#: 不等于才把它换算成「到期时刻」。⇒ 永久物一定要发 0.0，发个大数会变成
#: 「还剩 N 天」，界面上就多出一行期限。
PERMANENT_MINUTES = 0.0


def build_inventory_entry(item_id, count=1, minutes=PERMANENT_MINUTES,
                          repairs=0):
    """一条持有物（Des `0x412621`，内存步长 `0x20`）。

    | 线上 | 结构偏移 | 含义 |
    |---|---|---|
    | i32 | `+0x04` | 物品 id |
    | i32 | `+0x08` | 数量 / 剩余局数 |
    | f64 | `+0x10` | **剩余分钟数**，`0` = 永久 |
    | i32 | `+0x18` | 已修理次数 |
    """
    return (w_i32(item_id) + w_i32(count) + w_f64(minutes) + w_i32(repairs))


def build_rep_inventory(entries):
    """opcode `0x0601`（**服务端方向**）的包体 —— `i32 n + n×持有物条目`。

    ⚠⚠ 和客户端方向的 `0x0601`（物品定义请求）**同号反向**。
    ★ 客户端收到它就**整份换掉**全局背包并重建仓库面板（`0x4126cb` →
    `ShopStage::vft[0xb8] = 0x446f8a`）⇒ 每次都要发全量，不能发增量。
    """
    body = w_i32(len(entries))
    return body + b"".join(entries)


def inventory_records(inventory, materials=None):
    """存档的 `inventory` + `materials` → `(条目字节表, 认不出来的 id)`。

    `inventory` 是 `{itemId: {"count", "expires"}}`（`account_store`
    的形状），`materials` 是 `{itemId: 数量}`。

    ★ 两份合成**一张**清单：原版的仓库面板有「재료 材料」标签（分类
    `0x50001`，§22），材料就是从这张清单里按 `PartFlag == 0` 分出去的。
    ⚠ 客户端认不出来的 id 一律不发（`shopdata.ownable()`）—— 它会收集起来
    弹「无法从服务器读取道具信息」。
    """
    entries = []
    skipped = []
    merged = {}
    for source in (inventory or {}, materials or {}):
        for raw, value in source.items():
            try:
                item_id = int(raw)
            except (TypeError, ValueError):
                skipped.append(raw)
                continue
            if isinstance(value, dict):
                count = int(value.get("count", 1) or 0)
            else:
                count = int(value or 0)
            if count <= 0:
                continue
            merged[item_id] = merged.get(item_id, 0) + count
    for item_id in sorted(merged):
        if not shopdata.ownable(item_id):
            skipped.append(item_id)
            continue
        entries.append(build_inventory_entry(item_id, merged[item_id]))
    return entries, skipped


def inventory_item_ids(inventory, materials=None):
    """`inventory_records` 会真的发出去的那些 id（定义要先于清单下发）。"""
    ids = set()
    for source in (inventory or {}, materials or {}):
        for raw in source:
            try:
                item_id = int(raw)
            except (TypeError, ValueError):
                continue
            if shopdata.ownable(item_id):
                ids.add(item_id)
    return sorted(ids)
