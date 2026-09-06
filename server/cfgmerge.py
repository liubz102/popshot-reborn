#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cfgmerge.py —— 管理页保存时的**三方合并 + 冲突检测**（V0.3商店 D36）。

## 为什么要有它

管理页一次 `POST` 的是**整份文件**。两个运营同时开着页面，后按保存的那个人
会把前一个人的改动无声抹掉 —— 谁也不会发现，因为两边的画面都「保存成功」。

⇒ 保存时把「**我打开这一页时拿到的那份**」一起带上，做一次三方合并：

    base   = 我打开页面时服务端给我的那份
    mine   = 我现在编辑器里的这份
    theirs = 此刻磁盘上的那份

| 我 | 对方 | 结果 |
|---|---|---|
| 没动 | 改了 / 删了 / 加了 | **用对方的**（这就是「自动合并」）|
| 改了 | 没动 | 用我的 |
| 改了 | 改成了**一样**的 | 不算冲突 |
| 改了 | 改成了**不一样**的 | **冲突** |
| 删了 | 改了（或反过来）| **冲突** |
| 加了同一个身份、值不同 | 同左 | **冲突** |

合并结果以 `theirs` 为骨架（磁盘上的顺序原样留着），我新增的追加在末尾。

## ★ 记录的「身份」是什么

**按身份配对，绝不按下标** —— 别人在前面插一条，我的每一条下标就全错位了。

| 配置 | 自然键 |
|---|---|
| `items` / `shop` | `id` |
| `recipe` | `id`（配方号。SCHEMA 里是 readonly 的自动编号，跟着卡片走）|
| `drops` | `(mode, stage, difficulty, material)` —— 用户 2026-09-06 拍板 |

`drops` 是唯一**没有天然主键**的一份（同一种材料可以有好几条不同关卡 / 难度
的规则）。四元组就是「在什么情况下掉什么」，正是运营心里那条规则的身份。
四元组还撞车的（同关同难度同材料写了两条）按**出现先后**配对 ⇒ 身份统一是
`(自然键, 同键出现序)`，前三份的出现序恒为 0（validator 本来就不许重复）。

## 只用标准库

和 `shopcfg` 一个口径：服务端的便携运行时里没有第三方包，CPython 3.8
（Win7 运行时）也要能跑。
"""
import json

import shopcfg

#: 每份配置的自然键由哪几个字段拼出来。★ 加一份配置时在这儿登记一行。
KEY_FIELDS = {
    "items": ("id",),
    "shop": ("id",),
    "recipe": ("id",),
    "drops": ("mode", "stage", "difficulty", "material"),
}

#: 商店 ⇄ 合成互斥时，「这条记录说的是哪件物品」看哪个字段。
LISTING_ITEM_FIELD = {"shop": "id", "recipe": "result"}

#: 互斥的另一半是谁。
OTHER_LISTING = {"shop": "recipe", "recipe": "shop"}

#: 身份序列化时的两个分隔符。字段值只会是整数、`None`、
#: 或者 `mode` 那两个固定单词，撞不上。
_FIELD_SEP = "|"
_SEQ_SEP = "#"


class MergeResult(object):
    """`merge()` 的结果。"""

    __slots__ = ("entries", "conflicts", "applied", "adopted")

    def __init__(self, entries, conflicts, applied, adopted):
        #: 合并后的那个列表。**`clean` 才决定写不写盘** —— 撞车时它是
        #: 「只应用没撞车的那些」的结果，调用方拿它接着判互斥。
        self.entries = entries
        #: `[(身份, 原因)]` —— 撞车的，**一个字节都不该写**。
        self.conflicts = conflicts
        #: `[身份]` —— 我改的、没撞车的（「未冲突的物品」就是它）。
        self.applied = applied
        #: `[身份]` —— 对方改的、我没动的（这次跟着合并进来的）。
        self.adopted = adopted

    @property
    def clean(self):
        return not self.conflicts


# --------------------------------------------------------------------------
# 身份
# --------------------------------------------------------------------------

def _cell(entry, field):
    """一个身份字段的值。**取不到就是 `None`**，绝不抛异常。

    ★ `mode` 缺省是 `"quest"`：`validate_drops` 就是这么补的，两边要一致，
    否则「文件里没写 mode 的老规则」和「页面上选了闯关的新规则」会被当成
    两条不同的记录。
    """
    value = entry.get(field) if isinstance(entry, dict) else None
    if field == "mode":
        return str(value or "quest")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        # 手改坏了的值（`"abc"`）照原样当身份的一部分 —— 它进不了 validator，
        # 但在这儿要能稳定地和自己配对。
        return str(value)


def natural_key(which, entry):
    """一条记录的自然键（不含出现序）。"""
    return tuple(_cell(entry, field) for field in KEY_FIELDS[which])


def index_entries(which, entries):
    """`[(身份, 记录)]`，**保持原顺序**。身份 = `(自然键, 同键出现序)`。"""
    seen = {}
    out = []
    for entry in entries or ():
        nat = natural_key(which, entry)
        seq = seen.get(nat, 0)
        seen[nat] = seq + 1
        out.append(((nat, seq), entry))
    return out


def as_map(which, entries):
    """`{身份: 记录}`。顺序信息丢掉，只在比较时用。"""
    return dict(index_entries(which, entries))


def key_text(key):
    """身份 → 一个能塞进 JSON 的字符串（前后台之间传的就是它）。"""
    nat, seq = key
    body = _FIELD_SEP.join("" if cell is None else str(cell) for cell in nat)
    return "%s%s%d" % (body, _SEQ_SEP, seq)


# --------------------------------------------------------------------------
# 比较
# --------------------------------------------------------------------------

def _canon(entry):
    """一条记录的规范形式。★ 拿它比「改没改」。

    用 `sort_keys` 的 JSON 而不是 `==`：键的**顺序**不该算改动
    （前台改一个可选字段会把键删掉再加回来）。
    """
    try:
        return json.dumps(entry, sort_keys=True, ensure_ascii=False,
                          default=repr)
    except (TypeError, ValueError):
        return repr(entry)


def _same(a, b):
    if a is None or b is None:
        return a is b
    return _canon(a) == _canon(b)


# --------------------------------------------------------------------------
# 合并
# --------------------------------------------------------------------------

#: 冲突的原因（画在提示框里，人得知道为什么撞了）。
REASON_BOTH_EDITED = "另一个人也改了这一条"
REASON_I_DELETED = "我删掉了它，另一个人却改了它"
REASON_THEY_DELETED = "另一个人删掉了它，我却改了它"
REASON_BOTH_ADDED = "两个人各加了一条一样身份的记录"


def merge(which, base, mine, theirs, only=None):
    """三方合并。`only` 不是 `None` 时**只应用这几个身份上我的改动**。

    `only` 是身份字符串（`key_text`）的集合 —— 「单独提交未冲突物品」那一轮
    带的就是它。★ 那一轮**照样要重新判一次冲突**：从我按下确定到这一发到
    服务端之间，第三个人可能刚好也改了同一条（用户 2026-09-06 明确要求）。
    """
    base_map = as_map(which, base)
    mine_map = as_map(which, mine)
    theirs_order = index_entries(which, theirs)
    theirs_map = dict(theirs_order)

    conflicts = []
    applied = []
    adopted = []
    #: 我这边最终要用的值。`key -> 记录 或 None（删掉）`
    override = {}

    for key in set(base_map) | set(mine_map):
        old = base_map.get(key)
        new = mine_map.get(key)
        if _same(old, new):
            continue                      # 我没动这一条
        if only is not None and key_text(key) not in only:
            continue                      # 这一轮不提交它 ⇒ 当我没改过
        cur = theirs_map.get(key)
        if _same(old, cur):               # 对方没动 ⇒ 我的改动直接生效
            override[key] = new
            applied.append(key)
            continue
        if _same(new, cur):               # 两个人改成了一样的 ⇒ 不算撞车
            applied.append(key)
            continue
        if new is None:
            conflicts.append((key, REASON_I_DELETED))
        elif cur is None and old is not None:
            conflicts.append((key, REASON_THEY_DELETED))
        elif old is None:
            conflicts.append((key, REASON_BOTH_ADDED))
        else:
            conflicts.append((key, REASON_BOTH_EDITED))

    for key in set(base_map) | set(theirs_map):
        # 对方改的 / 加的 / 删的，而**我没动** —— 这就是「无需提示的自动合并」。
        if _same(base_map.get(key), theirs_map.get(key)):
            continue
        if _same(base_map.get(key), mine_map.get(key)):
            adopted.append(key)

    # ★ **撞车了也照样把 `entries` 算出来**（`clean` 才决定写不写盘）：
    #   那份结果正好是「只提交没撞车的那些」会得到的东西，调用方要拿它
    #   接着判**商店 ⇄ 合成互斥**。不算的话，第一轮列给运营的「未冲突」清单里
    #   可能混着互斥撞车的条目，他点了「单独提交」第二轮才被拒 —— 话没一次说完。
    #
    # 以 `theirs` 为骨架：磁盘上的顺序是最新的，我的新增追加在末尾。
    # （下标顺序只是给人看的，服务端读的时候按身份索引。）
    entries = []
    for key, entry in theirs_order:
        if key in override:
            if override[key] is not None:
                entries.append(override[key])
            continue                      # 我删掉了它
        entries.append(entry)
    for key, entry in index_entries(which, mine):
        if key in theirs_map or key not in override:
            continue
        if override[key] is not None:
            entries.append(override[key])
    return MergeResult(entries, conflicts, applied, adopted)


# --------------------------------------------------------------------------
# 商店 ⇄ 合成互斥（跨文件的冲突）
# --------------------------------------------------------------------------

def listed_item_ids(which, entries):
    """这一份配置里**正在上架**的物品 id。"""
    field = LISTING_ITEM_FIELD[which]
    out = set()
    for entry in entries or ():
        if not isinstance(entry, dict) or not entry.get("listed"):
            continue
        try:
            out.add(int(entry[field]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def listing_conflicts(which, my_base, merged, cross_base, cross_theirs):
    """「我让它在商店上架，同时另一个人让它在合成上架」—— 返回 `[(身份, 物品id)]`。

    ★ **窄口径**：只看「**我这次**让它上架的」∩「**对方这次**让它上架的」。

    「我 base 里 X 本来就在商店上架、这次没动它，对方把 X 加进了合成」不该拦我
    保存别的东西 —— 那是对方造成的不一致，让他自己那一发去处理。

    ★ 和 D33 分工：前台的 `listingClash()` 管「**我自己**两边都勾了」
    （弹确认框、自动下架另一边）；这一道管「**对方**刚把它在另一边上架了」——
    我手上那份另一边的副本是旧的，前台**根本看不见**这件事。
    """
    other = OTHER_LISTING.get(which)
    if other is None:
        return []
    mine_new = listed_item_ids(which, merged) - listed_item_ids(which, my_base)
    theirs_new = (listed_item_ids(other, cross_theirs)
                  - listed_item_ids(other, cross_base))
    clashing = mine_new & theirs_new
    if not clashing:
        return []
    field = LISTING_ITEM_FIELD[which]
    out = []
    for key, entry in index_entries(which, merged):
        try:
            item_id = int(entry[field])
        except (KeyError, TypeError, ValueError):
            continue
        if item_id in clashing and entry.get("listed"):
            out.append((key, item_id))
    return out


def listing_reason(which):
    other_zh = "合成" if which == "shop" else "商店"
    return "另一个人刚把它在%s里上架了" % other_zh


# --------------------------------------------------------------------------
# 给人看的名字
# --------------------------------------------------------------------------

def _item_label(item_id):
    name = shopcfg.item_name(item_id)
    return "%s（#%s）" % (name, item_id) if name else "#%s" % item_id


def label_of(which, key, entry=None):
    """一条记录在提示框里怎么写。★ 名字一律走 `shopcfg.item_name()`（D31）。"""
    nat = key[0]
    if which in ("items", "shop"):
        return _item_label(nat[0])
    if which == "recipe":
        result = (entry or {}).get("result")
        head = _item_label(result) if result is not None else "（产物未知）"
        return "%s　配方 #%s" % (head, nat[0])
    # drops：「铁矿石（#20007）　闯关 · 关卡5 岩浆巨龙 · 困难」
    mode, stage, difficulty, material = nat
    parts = [_item_label(material),
             "闯关" if mode != "pvp" else "对战"]
    parts.append("关卡 %s %s" % (stage, shopcfg.QUEST_ZH.get(stage, ""))
                 if stage is not None else "不限关卡")
    parts.append(shopcfg.DIFFICULTY_ZH.get(difficulty, "难度 %s" % difficulty)
                 if difficulty is not None else "不限难度")
    return "　".join(parts).strip()


def describe(which, key, reason, entry=None):
    """一条冲突 / 可合并项发给前台的样子。"""
    row = {"key": key_text(key), "label": label_of(which, key, entry)}
    if reason:
        row["reason"] = reason
    return row


def entries_of(which, raw):
    """一份配置的 json 对象 → 它那个列表。读不出来就当空的。"""
    if not isinstance(raw, dict):
        return []
    listing = raw.get(shopcfg.SCHEMA[which]["list_key"])
    return listing if isinstance(listing, list) else []
