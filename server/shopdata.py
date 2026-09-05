#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shopdata.py —— 读 `shop_items.json` 里的**物品表**（V0.3 合成与商店 M1）。

和 `weapondata.py` / `mapdata.py` 同一套路数：产物是 `tools/shopdata.py` 从原版
`Pack_decrypt/Data/*.ini` 离线抽出来的，**随代码走** —— 进 git、进服务端包，
不放 `data/`（那儿只装用户数据和用户会手改的配置）。

## 这张表回答什么

    part_flag(id)      这件东西占哪个装备槽 —— ★ 装备冲突判定的唯一依据
    conflicts(a, b)    两件能不能同时穿
    bonus(id)          装备加成（**只用于展示**，真正生效的是客户端自己算的，§1）
    kind(id)           weapon / armor / material / …  商店分类和校验
    exists(id)         ★ 这个 id 中文版客户端认不认识 —— 不认识就别发下去

## ★ 为什么「id 存不存在」这么重要

中文版 `ShopItem-Chn.ini` 比韩版少 622 条。**发一个客户端表里没有的 id，
客户端查不到图标，界面上就是个空格子**（结算界面那一栏更直接：
`0x415a94` 查不到就整件跳过，§3）。所以任何要下发的 id 都得先过 `exists()`。

## 铁律：只用标准库

服务端的便携运行时里没有第三方包。这里只有 `json` / `os`，
CPython 3.8（Win7 运行时）也能跑。
"""
import json
import os

#: 认得的产物格式版本。对不上就当没有数据 —— 宁可商店空着，
#: 也不要按错的布局解出一堆槽位错乱的装备。
FORMAT = 1

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "shop_items.json")

#: 空表。产物缺失 / 版本对不上时用它，让调用方拿到的形状始终一致。
_EMPTY = {"items": {}, "by_kind": {}, "promotions": [], "bonus_index": {}}

#: 武器槽的三个 `PartFlag`（`part_flag` 里的位）。
WEAPON_SLOT_FLAGS = (1024, 2048, 4096)


class Item(object):
    """一件物品。字段缺了就给缺省值，**调用方不用到处 `.get()`**。"""

    __slots__ = ("id", "kind", "part_flag", "part", "character", "timed",
                 "icon", "name_kr", "stock", "ownable", "slot", "series",
                 "tier", "ammo_id", "weapon", "bonus", "bonus_lua")

    def __init__(self, raw):
        self.id = int(raw.get("id", 0))
        self.kind = raw.get("kind") or "other"
        self.part_flag = int(raw.get("part_flag", 0) or 0)
        self.part = raw.get("part")
        #: 角色限定：0/1/2 = 泰尔 / 卡希尔 / 布洛克；`None` = 不限。
        self.character = raw.get("character")
        self.timed = bool(raw.get("timed"))
        self.icon = raw.get("icon") or ""
        #: 韩文物品名。★ 原版把名字藏在图标文件名里；`ch00B0015` 那种
        #: 模型编号没有名字，这里就是 `None`（中文名要人工填进 shop.json）。
        self.name_kr = raw.get("name_kr")
        #: 原版货架上出现过（有 `[Stock-]` 节）。
        self.stock = bool(raw.get("stock"))
        #: 能进背包（有 `[Item-]` 节）。★ **只有 `ownable` 的才能给玩家** ——
        #: 纯期限售卖形态只有货架条目，塞进背包客户端认不出来。
        self.ownable = bool(raw.get("ownable"))
        self.slot = raw.get("slot")
        self.series = raw.get("series")
        self.tier = raw.get("tier")
        self.ammo_id = raw.get("ammo_id")
        self.weapon = raw.get("weapon") or {}
        self.bonus = raw.get("bonus") or {}
        #: 条件加成（Lua 源码）。客户端自己会算，服务端只能原样展示。
        self.bonus_lua = raw.get("bonus_lua") or {}

    @property
    def equippable(self):
        """占槽位的才叫装备。材料 / 消耗品的 `part_flag` 是 0。"""
        return self.part_flag != 0

    def __repr__(self):
        return "<Item %d %s %s>" % (self.id, self.kind, self.name_kr or self.icon)


class _Store(object):
    """整张表 350 KB，一次读进来留着（和 `weapondata._Store` 同款）。"""

    def __init__(self, path=DATA_PATH):
        self.path = path
        self._table = None
        self._cache = {}
        self._order = None

    def table(self):
        if self._table is None:
            self._table = self._read()
        return self._table

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                table = json.load(fp)
        except (IOError, OSError, ValueError):
            # 没有物品表不该让服务端起不来 —— 商店空着，其余照常。
            return dict(_EMPTY)
        if table.get("format") != FORMAT:
            return dict(_EMPTY)
        return table

    def get(self, item_id):
        try:
            key = str(int(item_id))
        except (TypeError, ValueError):
            return None
        if key in self._cache:
            return self._cache[key]
        raw = self.table().get("items", {}).get(key)
        item = None if raw is None else Item(raw)
        self._cache[key] = item
        return item

    def ids_of_kind(self, kind):
        return list(self.table().get("by_kind", {}).get(kind, ()))

    def kinds(self):
        return sorted(self.table().get("by_kind", {}))

    def promotions(self):
        return self.table().get("promotions", [])

    def count(self):
        return len(self.table().get("items", {}))

    def catalog_order(self):
        """`{id 字符串: 在原版 ini 里的行号}`。★ 只建一次，跟着表一起失效。"""
        if self._order is None:
            self._order = dict((key, index) for index, key
                               in enumerate(self.table().get("items", {})))
        return self._order


STORE = _Store()


# --------------------------------------------------------------------------
# 模块级 API
# --------------------------------------------------------------------------

def get(item_id):
    """按 id 取物品；表里没有返回 `None`（**调用方必须能接受**）。"""
    return STORE.get(item_id)


def exists(item_id):
    """★ 中文版客户端认不认识这个 id。下发任何 id 之前先问它。"""
    return STORE.get(item_id) is not None


def kind(item_id):
    item = STORE.get(item_id)
    return item.kind if item else "other"


def part_flag(item_id):
    """占哪几个装备槽的位掩码；不是装备（材料等）返回 0。"""
    item = STORE.get(item_id)
    return item.part_flag if item else 0


def bonus(item_id):
    """装备加成 `{属性: 整数}`。★ **只用于展示** —— 真正生效的是客户端
    自己查本地 `EquipBonus.ini` 算的（§1），服务端下发的数字改不了它。"""
    item = STORE.get(item_id)
    return dict(item.bonus) if item else {}


def is_material(item_id):
    return kind(item_id) == "material"


def is_weapon(item_id):
    return kind(item_id) == "weapon"


def equippable(item_id):
    item = STORE.get(item_id)
    return bool(item and item.equippable)


def ownable(item_id):
    """能不能进玩家背包。纯期限售卖形态（只有货架条目）不能。"""
    item = STORE.get(item_id)
    return bool(item and item.ownable)


def conflicts(a, b):
    """两件装备是不是抢同一个槽。

    ★ 判据是 `part_flag` **按位与非 0** —— 套装的 `part_flag` 是组合值
    （上衣+下装+头+鞋+手套 = 31），这一条规则同时覆盖单件和套装（D6）。
    ★ 非装备（`part_flag == 0`）永远不冲突：材料想拿多少拿多少。
    """
    return bool(part_flag(a) & part_flag(b))


def character_of(item_id):
    """角色限定；`None` = 不限。"""
    item = STORE.get(item_id)
    return item.character if item else None


def usable_by(item_id, character_id):
    """这个角色能不能用这件东西。"""
    limit = character_of(item_id)
    if limit is None:
        return True
    try:
        return int(limit) == int(character_id)
    except (TypeError, ValueError):
        return False


def ids_of_kind(kind_name):
    """某一类的全部 id（已排序）。"""
    return STORE.ids_of_kind(kind_name)


def kinds():
    return STORE.kinds()


def promotions():
    """原版任务奖励定义。M6 的 `drops.json` 拿它做基线。"""
    return STORE.promotions()


def count():
    return STORE.count()


#: `catalog_index()` 给表外 id 的名次。★ 比任何真实名次都大 ⇒ 它们排在最后，
#: 而不是插到最前面（`-1` 就会那样）。
CATALOG_LAST = 1 << 30


def catalog_index(item_id):
    """这件东西在**原版 `ShopItem-Chn.ini` 里的行号**（0 起）。

    `tools/shopdata.py` 抽表时是**按 ini 的行序**往 `items` 里塞的，JSON 对象
    又保序 ⇒ 这个名次就是原版目录顺序。商店右下角那个「上市顺序」按钮
    （`0x0600` 的标志位 = 1，V0.3商店 §25）就是拿它排的 —— 我们手上**没有
    真正的上市日期**（随 2009 年停服的服务端 DB 一起没了），目录顺序是
    唯一一个不用自己编的近似。

    表里没有的 id 返回 `CATALOG_LAST`（排最后）。
    """
    try:
        key = str(int(item_id))
    except (TypeError, ValueError):
        return CATALOG_LAST
    return STORE.catalog_order().get(key, CATALOG_LAST)


def resolve_equipped(item_ids):
    """把一串 itemId 收敛成**一套互不冲突**的装备，返回 `(保留, 丢弃)`。

    规则：**按给定顺序先到先得** —— 前面的占了槽，后面抢同一个槽的就丢掉。
    调用方（`set_equipped`）应当把「玩家刚点的那件」放在最前面，
    这样「换装」天然表现为「新的顶掉旧的」。

    ★ 不在物品表里的 id 一律丢掉：发下去客户端查不到图标（见模块开头）。
    """
    kept = []
    dropped = []
    used = 0
    for raw in item_ids or ():
        item = STORE.get(raw)
        if item is None or not item.ownable:
            dropped.append(raw)
            continue
        flag = item.part_flag
        if flag and (flag & used):
            dropped.append(raw)
            continue
        used |= flag
        kept.append(item.id)
    return kept, dropped
