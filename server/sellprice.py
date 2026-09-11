#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卖出价格 —— 管理页「装备卖出」按什么价收东西（用户 2026-09-12）。

落在 `server/data/sell_price.json`，和账号存档 / 运营配置同一个目录。
目录**每次现取** `shopcfg.DATA_DIR`（不在 import 时抓快照）—— 打包自检的
`--data-dir` 和测试的临时目录都靠这一条把落脚点挪走。

★ **它是运营数值，但故意不做成第六份「运营配置」**（不进 `shopcfg._SPECS` /
`SCHEMA` / `web/admin.py` 的 `CONFIG_FILES` / `admin.js` 的 `CONFIGS`）：

* 那四张表是**配置标签页**那条链，进了就会多出一个标签页，而用户要的是
  「装备卖出」页上方一个按钮弹出来的小窗；
* `_SPECS` 的插入顺序就是 `shopcfg.all_write_locks()` 的加锁顺序，
  没必要为七个数去动那条全项目唯一的顺序。

不做成配置页并没有失去什么：写锁借 `shopcfg.write_lock()`（数据备份要一次
拿齐全部写锁，住在那边才拿得到）、原子写借 `shopcfg.write_json()`（LF 无 BOM，
铁律 3）、数据备份自己会认（`databackup.data_files` 收目录下所有 json）。

★★ **读不到 / 读坏了一律退回 `DEFAULTS`，绝不回空表。** 空表的后果是
「玩家卖东西一分钱拿不到」，而他东西已经没了 —— 这和 `rewards` 那份
「打完一局一分不给」是同一类事故（`shopcfg._USE_DEFAULT`）。
⇒ 启动路径上**碰都不碰它**，第一次有人按「保存」才落盘；包里
`server\\data\\` 因此保持空目录（`Assert-PackageDataClean` 守着）。

出厂默认值随代码走（就在下面的 `DEFAULTS` 里），新下载发布包的人第一次
开服拿到的就是这一份。
"""
from __future__ import annotations

import json
import os
import time

import shop
import shopcfg
import shopdata
import shopdefaults

#: 文件名。★ 加了新的 `server/data/*.json` 记得同时改 `.gitignore`。
FILENAME = "sell_price.json"

#: 文件格式版本。以后字段变了靠它分辨老文件。
FORMAT = 1

#: 材料的五个小类。★ 顺序 = 价格设置弹窗上从上到下的顺序。
CLASS_BEAD = "bead"
CLASS_GENERIC = "generic"
CLASS_SPECIAL_LOW = "special_low"
CLASS_SPECIAL_HIGH = "special_high"
CLASS_CARD = "card"

MATERIAL_CLASSES = (CLASS_BEAD, CLASS_GENERIC, CLASS_SPECIAL_LOW,
                    CLASS_SPECIAL_HIGH, CLASS_CARD)

MATERIAL_CLASS_ZH = {
    CLASS_BEAD: "珠子",
    CLASS_GENERIC: "通用材料",
    CLASS_SPECIAL_LOW: "特殊材料（低档）",
    CLASS_SPECIAL_HIGH: "特殊材料（高档）",
    CLASS_CARD: "卡片",
}

#: 装备百分比那一项的键；`other_price` 是「查不到买价也没有配方」的兜底价。
KEY_PERCENT = "equip_percent"
KEY_OTHER = "other_price"

#: 百分比的上限 —— **100，卖价不许高过买价**（用户 2026-09-12 拍板）。
#: 超过 100 的话「买了再卖」「合成了再卖」就是一条净赚的循环，玩家挂机就能
#: 刷爆金币；那不是「运营的自由」，是一个没人会当场发现的经济漏洞。
#: ⇒ 上限从一开始的 1000 收到 100，前台那个输入框也照这个数钳。
PERCENT_MAX = 100

#: 出厂默认值 —— **七个数全是用户 2026-09-12 定的**
#: （`other_price` 当天先由本工程定成 100，用户当天下午改口 500）。
DEFAULTS = {
    CLASS_BEAD: 100,
    CLASS_GENERIC: 200,
    CLASS_SPECIAL_LOW: 300,
    CLASS_SPECIAL_HIGH: 600,
    CLASS_CARD: 200,
    KEY_PERCENT: 95,
    KEY_OTHER: 500,
}

#: 卖价的来源，回给前台画浮窗用。
SOURCE_MATERIAL = "material"
SOURCE_SHOP = "shop"
SOURCE_RECIPE = "recipe"
SOURCE_OTHER = "other"


def _bead_ids():
    """珠子 = 掉落设计表里「简单档给哪色」和「对战给哪几色」的并集。"""
    out = set(shopdefaults.EASY_BEAD.values())
    out.update(material for material, _prob, _win in shopdefaults.PVP_BEADS)
    return frozenset(out)


def _generic_ids():
    """通用材料（矿料）= 每关每档给的那一样。"""
    out = set()
    for by_difficulty in shopdefaults.GENERIC.values():
        out.update(by_difficulty.values())
    return frozenset(out)


#: ★ 五个小类**一个硬编码清单都没有新建** —— 全部从 `shopdefaults` 那份
#: 掉落设计表推出来（D49 定的那张表就是「哪样材料算哪一档」的唯一出处）。
#: 设计表改了，卖价的分类自动跟着改。
_BEADS = _bead_ids()
_GENERICS = _generic_ids()


def material_class(item_id):
    """一件材料落在哪个小类；不是材料就回 `None`。

    ★★ **查的顺序有意义，不能只按 id 开头切**：`30016 不死鸟之羽` /
    `30017 不死鸟之泪` 的 id 在 `3xxxx` 段（看着像通用材料），语义上却是
    第 7 关的低 / 高档特殊材料 —— 它们在 `shopdefaults.SPECIAL[7]` 里，
    因此也在 `LOW_TIER` / `HIGH_TIER` 里。先查这两张表就绕过了这个坑。

    ★ 卡片的 `kind` 也是 `"material"`（部位码 6 / 11 / 12 / 13），
    在游戏仓库里却单占「收集品 → 卡片」那一格 —— 判据用现成的
    `shop.warehouse_category_of()`，别再抄一遍部位码。
    """
    item_id = int(item_id)
    if shopdata.kind(item_id) != "material":
        return None
    if item_id in shopdefaults.LOW_TIER:
        return CLASS_SPECIAL_LOW
    if item_id in shopdefaults.HIGH_TIER:
        return CLASS_SPECIAL_HIGH
    if shop.warehouse_category_of(item_id) == shop.WAREHOUSE_CARD:
        return CLASS_CARD
    if item_id in _BEADS:
        return CLASS_BEAD
    # 剩下的就是矿料。走到这里的必然在 `_GENERICS` 里（`test_sellprice` 钉着
    # 「43 件材料零缺零重」），写成兜底是为了将来有人往物品表里加一种材料、
    # 却忘了往掉落表里登记时**仍然卖得掉**，而不是突然变成「不可出售」。
    return CLASS_GENERIC


def material_groups():
    """每个小类都有哪些材料 —— 给价格设置弹窗把名字列出来。

    `{类名: [物品 id, ...]}`，id 排好序。管理员照着能一眼看见
    「不死鸟之羽 / 之泪」归在特殊档里，而不是按 id 开头自己猜。
    """
    out = dict((name, []) for name in MATERIAL_CLASSES)
    for item_id in shopdata.ids_of_kind("material"):
        name = material_class(item_id)
        if name is not None:
            out[name].append(int(item_id))
    for bucket in out.values():
        bucket.sort()
    return out


def path(data_dir=None):
    return os.path.join(data_dir or shopcfg.DATA_DIR, FILENAME)


def _lock():
    """写锁。和运营配置共用 `shopcfg` 那套按文件名分的锁 —— 数据备份拷贝时
    要一次拿齐全部写锁（`all_write_locks`），住在那边才拿得到。"""
    return shopcfg.write_lock(FILENAME)


def _set_aside(target, why, log):
    """读不动的旧文件挪到一边，**不直接覆盖**（照 `gifthistory._set_aside`）。

    价格表是人手改出来的，解析失败就当它不存在、下一发写盘顺手抹掉，
    等于把他改过的数悄悄销毁。挪成 `sell_price.json.bad-<时刻>` 放着。
    """
    spare = "%s.bad-%s" % (target, time.strftime("%Y%m%d-%H%M%S"))
    try:
        os.replace(target, spare)
    except OSError:
        spare = None
    if log:
        log("⚠ %s 读不了（%s）%s" % (
            target, why,
            "，已挪到 %s" % spare if spare else "，而且挪不走，下一发会覆盖它"))


def _as_int(raw, label, low=0, high=None):
    """认 int 和「整数形状的」str / float；越界就抛 `ValueError`。"""
    if isinstance(raw, bool):        # bool 是 int 的子类，别让 True 变成 1
        raise ValueError("%s 要是整数" % label)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("%s 要是整数" % label) from None
    if value < low:
        raise ValueError("%s 不能小于 %d" % (label, low))
    if high is not None and value > high:
        raise ValueError("%s 不能大于 %d" % (label, high))
    return value


def validate(raw):
    """一份价格表 → 补齐并校验过的 `dict`。不合法抛 `ValueError`。

    ★ 缺项按 `DEFAULTS` 补齐（幂等，反复读不写坏），多出来的键丢掉 ——
    老文件加了新字段时不用迁移，铁律 11 那条「读盘时按默认值补齐」。
    """
    if not isinstance(raw, dict):
        raise ValueError("卖出价格表的顶层要是一个对象")
    values = raw.get("prices") if isinstance(raw.get("prices"), dict) else raw
    out = {}
    for name in MATERIAL_CLASSES:
        if name in values:
            out[name] = _as_int(values[name], MATERIAL_CLASS_ZH[name] + "的单价")
        else:
            out[name] = DEFAULTS[name]
    if KEY_PERCENT in values:
        out[KEY_PERCENT] = _as_int(values[KEY_PERCENT], "装备卖出百分比",
                                   high=PERCENT_MAX)
    else:
        out[KEY_PERCENT] = DEFAULTS[KEY_PERCENT]
    if KEY_OTHER in values:
        out[KEY_OTHER] = _as_int(values[KEY_OTHER], "其他物品的价格")
    else:
        out[KEY_OTHER] = DEFAULTS[KEY_OTHER]
    return out


def load(data_dir=None, log=None):
    """当前的价格表。★ **读不到 / 读坏了一律退回 `DEFAULTS`。**"""
    with _lock():
        return _load_unlocked(path(data_dir), log)


def _load_unlocked(target, log):
    try:
        with open(target, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
    except FileNotFoundError:
        return dict(DEFAULTS)            # 还没人配过 = 用出厂价，不是「没有价」
    except (IOError, OSError, ValueError) as exc:
        _set_aside(target, exc, log)
        return dict(DEFAULTS)
    try:
        return validate(raw)
    except ValueError as exc:
        _set_aside(target, exc, log)
        return dict(DEFAULTS)


def save(values, data_dir=None, log=None):
    """校验 → 原子写，返回落盘的那一份。不合法抛 `ValueError`，一个字节不写。"""
    checked = validate(values)
    with _lock():
        shopcfg.write_json(path(data_dir),
                           {"format": FORMAT, "prices": checked})
    if log:
        log("[sell] 卖出价格表已更新：%s" % json.dumps(checked, sort_keys=True))
    return checked


def tables(data_dir=None, log=None):
    """`(货架表, {产出物 id: 配方})`。

    ★ `shopcfg` 那几个读取器回的是 `(内容, 警告列表)`，别忘了拆。
    配置读坏时它们返回**上一份好的**（一次都没读成功过就返回空表），
    警告照 `shop.py` 的做法只往日志里记一句，不打断卖出。
    """
    table, warnings = shopcfg.shop(data_dir)
    rows, more = shopcfg.recipes(data_dir)
    if log:
        for line in list(warnings) + list(more):
            log("⚠ [sell] %s" % line)
    # 一件装备只能有一条配方（`shopcfg.validate_recipes` 拒收同产物的第二条），
    # 所以这张表是一一对应的。
    return table, dict((int(row["result"]), row) for row in rows)


def recipe_index(data_dir=None, log=None):
    """`{产出物 id: 配方}`。"""
    return tables(data_dir, log)[1]


def quote(item_id, prices, shop_table, recipe_by_result):
    """**一件**（或一个）这东西值多少钱、要返还哪些材料。

    回 `{"id", "sellable", "source", "unit", "materials", "reason"}`，
    `unit` 是**单价**、`materials` 是**一件**返还的材料 —— 数量由调用方乘。

    四条分支（互斥，见计划 §一）：

    | 来源 | 卖价 | 返还材料 |
    |---|---|---|
    | 材料（含卡片） | 该小类单价 | — |
    | `shop.json` 里有价 | `price × 百分比 // 100` | — |
    | `recipe.json` 里有配方 | `cost × 百分比 // 100` | **配方材料原样** |
    | 其余 ownable | 「其他物品」兜底价 | — |

    ★ **`listed == false` 仍然按表里的价算**：下架只决定「还买不买得到」，
    不该让玩家手里已经有的东西突然卖不掉。
    ★ 商店和合成是互斥的（`cfgmerge` 守着，实测两边的 id 集合不相交），
    所以中间两条分支不会打架；万一真撞上了，先查商店。
    """
    item_id = int(item_id)
    blank = {"id": item_id, "sellable": False, "source": "",
             "unit": 0, "materials": {}, "reason": ""}
    if not shopdata.ownable(item_id):
        blank["reason"] = "客户端不认得这件物品，不能出售"
        return blank
    name = material_class(item_id)
    if name is not None:
        return {"id": item_id, "sellable": True, "source": SOURCE_MATERIAL,
                "unit": int(prices[name]), "materials": {}, "reason": ""}
    percent = int(prices[KEY_PERCENT])
    entry = shop_table.get(item_id)
    if entry is not None:
        return {"id": item_id, "sellable": True, "source": SOURCE_SHOP,
                "unit": int(entry.get("price") or 0) * percent // 100,
                "materials": {}, "reason": ""}
    recipe = recipe_by_result.get(item_id)
    if recipe is not None:
        materials = {}
        for slot in recipe.get("materials") or ():
            mid = int(slot["id"])
            materials[mid] = materials.get(mid, 0) + int(slot["count"])
        return {"id": item_id, "sellable": True, "source": SOURCE_RECIPE,
                "unit": int(recipe.get("cost") or 0) * percent // 100,
                "materials": materials, "reason": ""}
    return {"id": item_id, "sellable": True, "source": SOURCE_OTHER,
            "unit": int(prices[KEY_OTHER]), "materials": {}, "reason": ""}


def quote_all(prices=None, data_dir=None, log=None):
    """整份物品表的报价 `{物品 id: quote}` —— 前台一次拿走，浮窗照着画。"""
    if prices is None:
        prices = load(data_dir=data_dir, log=log)
    shop_table, recipes = tables(data_dir, log)
    out = {}
    for kind in shopdata.kinds():
        for item_id in shopdata.ids_of_kind(kind):
            if shopdata.ownable(item_id):
                out[int(item_id)] = quote(item_id, prices, shop_table, recipes)
    return out


def bundle(lines, prices=None, data_dir=None, log=None):
    """把一份「待卖出清单」算成一笔交易。不合法抛 `ValueError`。

    `lines` = `[{"id": 物品 id, "count": 数量}]`。
    回 `{"plan", "money", "returned", "prices"}`：

    * `plan` —— 每行 `{"id","count","unit","money","source","materials"}`，
      `materials` **已经乘过数量**；`account_store.sell_items()` 直接吃它；
    * `money` —— 金币小计之和（**不封顶**，封顶在存档层做，那里才知道余额）；
    * `returned` —— 全部返还材料**合并累加**后的 `{id: 数量}`。

    ★ 装备（`stackable()` 为假）一律按 1 件算 —— 客户端的数量那一格根本
    没人读（§28），存档里也只有「有 / 没有」。前台传什么都不算数。
    """
    if prices is None:
        prices = load(data_dir=data_dir, log=log)
    shop_table, recipes = tables(data_dir, log)
    merged = {}
    order = []
    for row in lines or ():
        try:
            item_id = int(row["id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("待卖出清单里有一条没写物品 id") from None
        if not shopdata.exists(item_id):
            raise ValueError("物品 %d 不在物品表里" % item_id)
        count = _as_int(row.get("count", 1), "物品 %d 的数量" % item_id, low=1)
        if not shopdata.stackable(item_id):
            count = 1
        if item_id not in merged:
            order.append(item_id)
        merged[item_id] = merged.get(item_id, 0) + count
    if not merged:
        raise ValueError("待卖出清单是空的")
    plan, money, returned = [], 0, {}
    for item_id in order:
        count = merged[item_id]
        priced = quote(item_id, prices, shop_table, recipes)
        if not priced["sellable"]:
            raise ValueError("%s：%s" % (shopcfg.item_name(item_id,
                                                          data_dir=data_dir),
                                        priced["reason"]))
        subtotal = priced["unit"] * count
        materials = dict((mid, n * count)
                         for mid, n in priced["materials"].items())
        for mid, n in materials.items():
            returned[mid] = returned.get(mid, 0) + n
        money += subtotal
        plan.append({"id": item_id, "count": count, "unit": priced["unit"],
                     "money": subtotal, "source": priced["source"],
                     "materials": materials})
    return {"plan": plan, "money": money, "returned": returned,
            "prices": prices}
