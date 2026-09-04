#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shopcfg.py —— 商店 / 合成 / 掉落的**运行时配置**（V0.3 合成与商店 M1）。

三份 JSON，都在 `server/data/`，**用户随时手改，改完不用重启**：

| 文件 | 管什么 |
|---|---|
| `shop.json` | 哪些东西上架、卖多少钱、要几级、中文名 |
| `recipe.json` | 合成配方（产物 / 花费 / 等级 / **最多 4 种材料**） |
| `drops.json` | 打完一局掉什么材料 |

## 和 `shopdata.py` 的分工（别搞混）

- `shopdata.py` 读 `server/shop_items.json` —— **原版数据的只读镜像**，
  随代码走、进 git、进包。回答「这个 id 客户端认不认识、占哪个槽、加多少」。
- `shopcfg.py`（本文件）读 `server/data/*.json` —— **用户的运营配置**，
  运行时生成、`.gitignore`、**打包时不拷**（D7）。回答「卖不卖、多少钱、怎么合」。

## 热重载（用户要求：改完不重启即刻生效）

照 `versioning.load_client_filter()` 的口径：按 **mtime + size** 缓存，
每次取用查一眼，变了才重读（`_reload=True` 只给测试用 —— 同一毫秒内
连改两次时 mtime 粒度盖不住）。

## 坏文件怎么办：**fail-safe，绝不覆盖**

用户可能正编辑到一半。所以：

1. JSON 解析不了 / 校验不过 → **保留上一份好的**，只记警告；
2. 一次都没读成功过 → 返回**空目录**（商店空着，其余照常），不是内置默认值
   —— 「商店突然多出一堆没上架的东西」比「商店空着」更难查；
3. **任何情况下都不回写文件**。生成只发生在「文件不存在」那一次（`ensure_files`）。

## 铁律：只用标准库

服务端便携运行时里没有第三方包。CPython 3.8（Win7 运行时）也要能跑。
"""
import json
import os
import threading

import shopdata

#: 认得的配置格式版本。用户手改时不用管它；将来结构变了靠它做迁移。
FORMAT = 1

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

SHOP_FILENAME = "shop.json"
RECIPE_FILENAME = "recipe.json"
DROPS_FILENAME = "drops.json"

#: ★ 合成界面只有 4 个材料槽（`ComposeItemNewUI.ui` 的 `ImgBar0~3`，§7）。
#: 配方写第 5 种材料，玩家在界面上根本看不见 —— 校验时直接拒绝。
MAX_MATERIALS = 4

_lock = threading.RLock()
#: ``{路径: (stamp, 解析结果, 警告列表)}`` —— 热重载缓存。
_cache = {}


class ConfigError(Exception):
    """校验失败。管理页保存时拿它当「哪一行不对」的提示。"""


# --------------------------------------------------------------------------
# 中文名
# --------------------------------------------------------------------------

#: 韩文 → 中文。原版**没有中文物品名**：`ShopItem.ini` 一个名字字段都没有，
#: 名字藏在图标文件名里，`Chinese.ini` 里也查不到（FINDINGS §5）。
#: 所以这张表是**我们自己翻的**，用户可以在管理页里改（改的是 `shop.json`
#: 的 `name`，这张表只在**首次生成**时用一次）。
#:
#: 有出处的两个：`초록구슬 = 绿色小珠`（`Chinese.ini` 里一整句消息里带出来的）、
#: `청동파이프 = 青铜管`（`Promotion-chn.ini` 的 `TextReward`；同一个词在
#: 另外两关被译成「橡皮管」，原版自己就不一致，这里统一用「青铜管」）。
NAME_ZH = {
    # ---- 武器：基础名（后面接 系列 + 级别）----
    "리볼버": "左轮",
    "사과탄": "苹果雷",
    "T1": "狙击枪T1",
    "T2": "狙击枪T2",
    "카멜나이트": "骆驼骑士",
    "화염탄": "火焰弹",
    "캐논왈츠": "加农炮",
    "머신건": "机关枪",
    "스크류런처": "螺旋炮",
    "바주카": "火箭筒",
    # ---- 合成材料 ----
    "검은구슬": "黑色小珠",
    "붉은구슬": "红色小珠",
    "초록구슬": "绿色小珠",
    "푸른구슬": "蓝色小珠",
    "철광석": "铁矿石",
    "파이프": "水管",
    "화염조각": "火焰碎片",
    "제작서17": "制作书17",
    "불사조의 깃털": "不死鸟之羽",
    "불사조의 눈물": "不死鸟之泪",
    "청동파이프": "青铜管",
    "부유석": "浮游石",
    "Z칩": "Z芯片",
    "용의 눈물": "龙之泪",
    "병사의 찢어진 군복": "士兵的破军服",
    "전함의 프로펠러": "战舰螺旋桨",
    "부서진투구": "破碎的头盔",
    "태양의 파편": "太阳碎片",
    "무적승리자": "无敌胜利者",
    "파이터": "格斗家",
    "슈터": "射手",
    "럭키가이": "幸运儿",
    "빈대": "臭虫",
    "하트마니아": "红心狂",
    "팀킬쟁이": "误伤王",
    "제풀쟁이": "自爆王",
    "리볼버고수": "左轮高手",
    "사과탄고수": "苹果雷高手",
    "T1고수": "狙击高手",
    "카멜나이트고수": "骆驼骑士高手",
    "화염탄고수": "火焰弹高手",
    "캐논왈츠고수": "加农炮高手",
    "머신건고수": "机关枪高手",
    "스크류런쳐고수": "螺旋炮高手",
    "바주카고수": "火箭筒高手",
    # ---- 合成产物：套装名 ----
    "마스터리아머": "大师铠甲",
    "마이너피닉스아머": "初阶凤凰铠甲",
    "메이져피닉스아머": "高阶凤凰铠甲",
    "머시너리아머": "佣兵铠甲",
}

#: 三个武器系列。★ 这三个中文名是**用户记忆里的原版叫法**，别改。
SERIES_ZH = {"D": "爆裂", "R": "极速", "F": "复合"}

#: 角色 id → 中文名（`Data/ChrProps.ini` 的前三个，V0.1 §119）。
CHARACTER_ZH = {0: "泰尔", 1: "卡希尔", 2: "布洛克"}

#: 套装部位后缀（韩文名的最后一段）→ 中文。
PART_SUFFIX_ZH = {"몸": "上衣", "다리": "下装", "손": "手套",
                  "발": "鞋", "머리": "头盔"}


def weapon_name_zh(item):
    """`리볼버 R1` → `左轮 极速1`；翻不出来就原样返回韩文名。"""
    name = item.name_kr or ""
    base = name
    if item.series and item.tier:
        suffix = " %s%d" % (item.series, item.tier)
        if name.endswith(suffix):
            base = name[:-len(suffix)]
        zh = NAME_ZH.get(base.strip())
        if zh:
            return "%s %s%d" % (zh, SERIES_ZH.get(item.series, item.series),
                                item.tier)
    return NAME_ZH.get(base.strip(), name)


def armor_name_zh(item):
    """`타이_메이져피닉스아머 몸` → `泰尔 高阶凤凰铠甲·上衣`。"""
    name = (item.name_kr or "").strip()
    part = ""
    for suffix, zh in PART_SUFFIX_ZH.items():
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip().rstrip("_")
            part = "·" + zh
            break
    # 套装名前面常带角色名（`타이_` / `카실_` / `프로코_`），去掉后再查表。
    for korean in ("타이", "카실", "프로코"):
        if name.startswith(korean):
            name = name[len(korean):].strip().rstrip("_").lstrip("_").strip()
            break
    zh = NAME_ZH.get(name)
    if zh is None:
        return (item.name_kr or "").strip()
    character = CHARACTER_ZH.get(item.character)
    return "%s %s%s" % (character, zh, part) if character else zh + part


def item_name_zh(item):
    """任何一件东西的中文名；翻不出来退回韩文名，再不行退回 `#id`。"""
    if item is None:
        return ""
    if item.kind == "weapon":
        name = weapon_name_zh(item)
    elif item.kind in ("armor", "ring"):
        name = armor_name_zh(item)
    else:
        name = NAME_ZH.get((item.name_kr or "").strip(), item.name_kr or "")
    return name or ("#%d" % item.id)


# --------------------------------------------------------------------------
# 默认 shop.json
# --------------------------------------------------------------------------

#: 武器售价 / 等级门槛，按**级别**分档。
#:
#: 定价参考：现在线上玩家的金币在 0~8800 之间（`accounts.json`），
#: 闯关一局大约几十到几百。所以 1 级「打几局就买得起」、
#: 2 级「攒一阵」、3 级「一个阶段目标」。用户可以在管理页随时调。
WEAPON_PRICE = {1: 3000, 2: 8000, 3: 18000}
WEAPON_LEVEL = {1: 5, 2: 10, 3: 18}


def default_shop():
    """从 `shop_items.json` 生成一份默认 `shop.json`。

    收三类东西（**都得 `ownable`**，只有货架条目的进不了背包，§11）：

    1. **63 件 D/R/F 武器** —— 上架，这是本版商店的主体；
    2. **全部材料** —— 不上架（`listed=false`），收进来只是为了有个中文名；
    3. **合成产物** —— 不上架，同上（它们靠合成获得，不卖）。
    """
    entries = []

    for item_id in shopdata.ids_of_kind("weapon"):
        item = shopdata.get(item_id)
        if not item.ownable or not item.series or item.character is None:
            continue
        tier = item.tier or 1
        entries.append({
            "id": item.id,
            "name": item_name_zh(item),
            "kind": "weapon",
            "listed": True,
            "price": WEAPON_PRICE.get(tier, 3000),
            "level": WEAPON_LEVEL.get(tier, 5),
            "days": 0,
        })

    for item_id in shopdata.ids_of_kind("material"):
        item = shopdata.get(item_id)
        if not item.ownable:
            continue
        entries.append({
            "id": item.id,
            "name": item_name_zh(item),
            "kind": "material",
            "listed": False,
            "price": 0,
            "level": 1,
            "days": 0,
        })

    seen = set(entry["id"] for entry in entries)
    for recipe in default_recipes()["recipes"]:
        item_id = recipe["result"]
        if item_id in seen:
            continue
        item = shopdata.get(item_id)
        if item is None:
            continue
        seen.add(item_id)
        entries.append({
            "id": item_id,
            "name": recipe["name"],
            "kind": item.kind,
            "listed": False,
            "price": 0,
            "level": recipe.get("level", 1),
            "days": 0,
        })

    entries.sort(key=lambda e: (e["kind"], e["id"]))
    return {
        "format": FORMAT,
        "_说明": [
            "商店目录。改完保存即刻生效，不用重启服务端。",
            "listed=false 的不上架（材料和合成产物收在这里只是为了有个中文名）。",
            "price 是金币（原版的「픽셀」，中文版译作金币）。days=0 是永久。",
            "id 必须是客户端认识的（server/shop_items.json 里有、且能进背包）。",
        ],
        "items": entries,
    }


# --------------------------------------------------------------------------
# 默认 recipe.json
# --------------------------------------------------------------------------

#: 合成配方的三条产线。每条 = `(套装韩文名, 档位)`。
#:
#: **主题是照原版材料的名字定的**（D2 / FINDINGS §7）：
#: 原版材料里有「不死鸟之羽 / 不死鸟之泪」，装备里正好有一整条
#: 「피닉스아머（凤凰铠甲）」产线 —— 这两个对得上不是巧合。
#: 「머시너리아머（佣兵铠甲）」是纯防御向，配铁 / 管一类的工业材料。
#:
#: 每档的材料 = 一种「主题材料」+ 一种「通用矿料」+ 一种「珠子」，最多 4 种
#: （UI 只有 4 个槽）。数量和花费按那一件的加成大小缩放。
#:
#: ★ 系数是**照掉落速度倒推的**，不是拍脑袋：`drops.json` 默认一局最多掉
#: 1 个材料、概率 25%~100%，所以「一件中档装备 ≈ 10 局左右」才不至于劝退。
#: 一件加成合计 9 点的上衣按下面的系数是 4+2+5 = 11 个材料。
#: 觉得快了慢了直接在管理页改 `recipe.json`，不用动代码。
RECIPE_LINES = (
    # (套装韩文名, 等级门槛, 每点加成的金币, 材料表)
    ("머시너리아머", 10, 260, ((20007, 0.40), (30018, 0.25), (10001, 0.60))),
    ("마스터리아머", 14, 320, ((20007, 0.40), (30005, 0.25), (10002, 0.60))),
    ("마이너피닉스아머", 18, 420, ((30016, 0.30), (30006, 0.40), (10002, 0.55))),
    ("메이져피닉스아머", 24, 560, ((30016, 0.30), (30017, 0.25),
                                   (30006, 0.40), (10002, 0.55))),
)


def _bonus_weight(item):
    """一件装备「有多强」—— 拿它缩放材料数量和花费。

    ★ 用**绝对值之和**：有几件是「攻 -2 防 +9」这种，负的那格也是设计的一
    部分（防御向套装故意扣攻击），当成 0 会把它们算得太便宜。
    """
    return sum(abs(int(v)) for v in (item.bonus or {}).values()) or 1


def _armor_sets():
    """`{(角色, 套装韩文名): [Item, …]}` —— 只收三个初期角色、能进背包的。"""
    groups = {}
    for kind in ("armor", "ring"):
        for item_id in shopdata.ids_of_kind(kind):
            item = shopdata.get(item_id)
            if item.timed or not item.ownable or item.character not in (0, 1, 2):
                continue
            name = (item.name_kr or "").strip()
            if not name or not item.bonus:
                continue
            for suffix in PART_SUFFIX_ZH:
                if name.endswith(suffix):
                    base = name[:-len(suffix)].strip().rstrip("_")
                    for korean in ("타이", "카실", "프로코"):
                        if base.startswith(korean):
                            base = base[len(korean):].strip("_ ")
                            break
                    groups.setdefault((item.character, base), []).append(item)
                    break
    return groups


def default_recipes():
    """从 `shop_items.json` 生成一份默认 `recipe.json`。

    ★ **原版配方在客户端里彻底不存在**（FINDINGS §7），这一份是**我们自己
    设计的**（D2）。用户会在管理页里调，所以这里追求的是「一眼看得懂、
    改起来容易」，不是「一次到位」。
    """
    sets = _armor_sets()
    recipes = []
    next_id = 1
    for korean, level, gold_per_point, materials in RECIPE_LINES:
        for character in (0, 1, 2):
            pieces = sorted(sets.get((character, korean), ()),
                            key=lambda it: it.id)
            for item in pieces:
                weight = _bonus_weight(item)
                need = []
                for material_id, ratio in materials:
                    if not shopdata.exists(material_id):
                        continue        # 中文版没有这种材料就跳过这一格
                    need.append({"id": material_id,
                                 "count": max(1, int(round(weight * ratio)))})
                if not need:
                    continue
                recipes.append({
                    "id": next_id,
                    "result": item.id,
                    "name": item_name_zh(item),
                    "listed": True,
                    "level": level,
                    "character": character,
                    "cost": weight * gold_per_point,
                    "days": 0,
                    "materials": need[:MAX_MATERIALS],
                })
                next_id += 1
    return {
        "format": FORMAT,
        "_说明": [
            "合成配方。改完保存即刻生效，不用重启服务端。",
            "★ 一条配方最多 4 种材料 —— 原版合成界面只有 4 个材料槽，第 5 种玩家看不见。",
            "★ 原版没有合成成功率（界面上没有任何概率控件），别加。",
            "cost 是金币；character 是角色限定（0 泰尔 / 1 卡希尔 / 2 布洛克，省略 = 不限）。",
            "原版配方随 2009 年停服的服务端一起没了，这一份是复活工程自己设计的，随便改。",
        ],
        "recipes": recipes,
    }


# --------------------------------------------------------------------------
# 默认 drops.json
# --------------------------------------------------------------------------

#: 原版基线：`Promotion.ini` 里给材料的 4 关（三个角色线一模一样，
#: 所以按 `(quest_stage, difficulty)` 去重后就这 4 条，FINDINGS §12）。
#: `prob=100` = 必掉。
#:
#: 扩展部分：任务模式按**难度**掉材料。原版珠子主要来自对战模式
#: （新浪 2007 攻略页），但用户的期望是「打任务掉材料」，所以两边都给（D4）。
#:
#: ★★ **这张表必须覆盖 `RECIPE_LINES` 用到的每一种材料** —— 漏一种，
#: 那条产线就永远合不出来。`test_every_recipe_material_can_actually_drop`
#: 守着这一条（会话 01 就是被它抓出来的：配方要铁矿石，掉落表里没有）。
#:
#: 难度越高、材料越好：珠子人人有份，不死鸟系只有最高难度掉。
DEFAULT_MATERIAL_DROPS = (
    # (材料 id, {难度: 概率%})            用在哪条产线
    (10003, {1: 25}),                     # 绿珠   —— 低难度的保底产出
    (10004, {2: 40}),                     # 蓝珠
    (10002, {3: 60, 4: 70}),              # 红珠   —— 大师 / 凤凰两条线
    (10001, {4: 80}),                     # 黑珠   —— 佣兵线
    (20007, {2: 30, 3: 40, 4: 50}),       # 铁矿石 —— 佣兵 + 大师
    (30005, {2: 25, 3: 35, 4: 45}),       # 水管   —— 大师
    (30018, {3: 20, 4: 30}),              # 青铜管 —— 佣兵（原版基线只在 3 张图给，
                                          #           光靠那个凑不出一套）
    (30006, {3: 30, 4: 40}),              # 火焰碎片 —— 凤凰两档
    (30016, {4: 25}),                     # 不死鸟之羽 —— 凤凰
    (30017, {4: 15}),                     # 不死鸟之泪 —— 高阶凤凰，最稀有
)


def default_drops():
    """从 `shop_items.json` 的 `promotions` 生成一份默认 `drops.json`。"""
    rules = []
    seen = set()
    for promo in shopdata.promotions():
        stage = promo.get("quest_stage")
        difficulty = promo.get("difficulty")
        if stage is None or difficulty is None:
            continue
        for reward in promo.get("rewards", ()):
            if reward.get("kind") != "item":
                continue
            item_id = reward.get("item_id")
            if not shopdata.is_material(item_id):
                continue
            key = (stage, difficulty, item_id)
            if key in seen:
                continue        # 三个角色线一模一样，去重
            seen.add(key)
            rules.append({
                "mode": "quest",
                "stage": stage,
                "difficulty": difficulty,
                "material": item_id,
                "count": 1,
                "prob": 100,
                "cleared_only": True,
                "note": "原版基线（Promotion.ini %s）" % promo.get("promotion_id", ""),
            })
    rules.sort(key=lambda r: (r["stage"], r["difficulty"], r["material"]))

    for material, by_difficulty in DEFAULT_MATERIAL_DROPS:
        if not shopdata.exists(material):
            continue
        for difficulty in sorted(by_difficulty):
            rules.append({
                "mode": "quest",
                "difficulty": difficulty,
                "material": material,
                "count": 1,
                "prob": by_difficulty[difficulty],
                "cleared_only": True,
                "note": "扩展：任务模式按难度掉材料",
            })

    if shopdata.exists(10001):
        rules.append({
            "mode": "pvp",
            "material": 10001,
            "count": 1,
            "prob": 15,
            "cleared_only": False,
            "note": "扩展：对战模式也给一点（原版珠子主要来自对战）",
        })

    return {
        "format": FORMAT,
        "_说明": [
            "材料掉落。改完保存即刻生效，不用重启服务端。",
            "mode: quest 闯关 / pvp 对战。stage 和 difficulty 省略 = 不限。",
            "prob 是百分比（100 = 必掉）。cleared_only=true 表示只有通关才给。",
            "前几条是原版基线（Promotion.ini 里那 4 关），后面是复活工程加的扩展。",
        ],
        "rules": rules,
    }


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

def _as_int(value, name, low=None, high=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError("%s 不是整数：%r" % (name, value))
    if low is not None and number < low:
        raise ConfigError("%s 不能小于 %d：%r" % (name, low, value))
    if high is not None and number > high:
        raise ConfigError("%s 不能大于 %d：%r" % (name, high, value))
    return number


def _check_item_id(item_id, name):
    """★ 下发给客户端的 id 必须**客户端认识 + 能进背包**，否则界面上是空格子。"""
    if not shopdata.exists(item_id):
        raise ConfigError("%s：物品 %s 不在 shop_items.json 里"
                          "（中文版客户端不认识它）" % (name, item_id))
    if not shopdata.ownable(item_id):
        raise ConfigError("%s：物品 %s 进不了背包"
                          "（原版只给了它货架条目）" % (name, item_id))


def validate_shop(raw):
    """`shop.json` → `{itemId: 条目}`；有一条不对就抛 `ConfigError`。"""
    if not isinstance(raw, dict):
        raise ConfigError("shop.json 的最外层必须是一个对象")
    items = raw.get("items")
    if not isinstance(items, list):
        raise ConfigError("shop.json 缺少 items 列表")
    out = {}
    for index, entry in enumerate(items):
        where = "items[%d]" % index
        if not isinstance(entry, dict):
            raise ConfigError("%s 不是对象" % where)
        item_id = _as_int(entry.get("id"), where + ".id", low=1)
        _check_item_id(item_id, where)
        if item_id in out:
            raise ConfigError("%s：物品 %d 出现了两次" % (where, item_id))
        item = shopdata.get(item_id)
        out[item_id] = {
            "id": item_id,
            "name": str(entry.get("name") or item_name_zh(item)),
            "kind": item.kind,
            "listed": bool(entry.get("listed", False)),
            "price": _as_int(entry.get("price", 0), where + ".price", low=0),
            "level": _as_int(entry.get("level", 1), where + ".level", low=1),
            "days": _as_int(entry.get("days", 0), where + ".days", low=0),
        }
    return out


def validate_recipes(raw):
    """`recipe.json` → `[配方…]`；有一条不对就抛 `ConfigError`。"""
    if not isinstance(raw, dict):
        raise ConfigError("recipe.json 的最外层必须是一个对象")
    recipes = raw.get("recipes")
    if not isinstance(recipes, list):
        raise ConfigError("recipe.json 缺少 recipes 列表")
    out = []
    seen_ids = set()
    for index, entry in enumerate(recipes):
        where = "recipes[%d]" % index
        if not isinstance(entry, dict):
            raise ConfigError("%s 不是对象" % where)
        recipe_id = _as_int(entry.get("id", index + 1), where + ".id", low=1)
        if recipe_id in seen_ids:
            raise ConfigError("%s：配方号 %d 出现了两次" % (where, recipe_id))
        seen_ids.add(recipe_id)

        result = _as_int(entry.get("result"), where + ".result", low=1)
        _check_item_id(result, where)

        materials = entry.get("materials")
        if not isinstance(materials, list) or not materials:
            raise ConfigError("%s 没有材料" % where)
        # ★ 原版合成界面只有 4 个材料槽，第 5 种玩家根本看不见。
        if len(materials) > MAX_MATERIALS:
            raise ConfigError("%s：材料最多 %d 种，写了 %d 种"
                              % (where, MAX_MATERIALS, len(materials)))
        need = []
        used = set()
        for slot, material in enumerate(materials):
            spot = "%s.materials[%d]" % (where, slot)
            if not isinstance(material, dict):
                raise ConfigError("%s 不是对象" % spot)
            material_id = _as_int(material.get("id"), spot + ".id", low=1)
            _check_item_id(material_id, spot)
            if material_id in used:
                raise ConfigError("%s：材料 %d 在同一条配方里出现了两次"
                                  % (spot, material_id))
            used.add(material_id)
            need.append({
                "id": material_id,
                "count": _as_int(material.get("count", 1),
                                 spot + ".count", low=1, high=800),
            })

        character = entry.get("character")
        if character is not None:
            character = _as_int(character, where + ".character", low=0, high=2)

        out.append({
            "id": recipe_id,
            "result": result,
            "name": str(entry.get("name") or item_name_zh(shopdata.get(result))),
            "listed": bool(entry.get("listed", True)),
            "level": _as_int(entry.get("level", 1), where + ".level", low=1),
            "character": character,
            "cost": _as_int(entry.get("cost", 0), where + ".cost", low=0),
            "days": _as_int(entry.get("days", 0), where + ".days", low=0),
            "materials": need,
        })
    return out


def validate_drops(raw):
    """`drops.json` → `[规则…]`；有一条不对就抛 `ConfigError`。"""
    if not isinstance(raw, dict):
        raise ConfigError("drops.json 的最外层必须是一个对象")
    rules = raw.get("rules")
    if not isinstance(rules, list):
        raise ConfigError("drops.json 缺少 rules 列表")
    out = []
    for index, entry in enumerate(rules):
        where = "rules[%d]" % index
        if not isinstance(entry, dict):
            raise ConfigError("%s 不是对象" % where)
        mode = entry.get("mode", "quest")
        if mode not in ("quest", "pvp"):
            raise ConfigError("%s.mode 只能是 quest 或 pvp：%r" % (where, mode))
        material = _as_int(entry.get("material"), where + ".material", low=1)
        _check_item_id(material, where)
        if not shopdata.is_material(material):
            raise ConfigError("%s：%d 不是合成材料" % (where, material))
        rule = {
            "mode": mode,
            "material": material,
            "count": _as_int(entry.get("count", 1), where + ".count",
                             low=1, high=800),
            "prob": _as_int(entry.get("prob", 100), where + ".prob",
                            low=0, high=100),
            "cleared_only": bool(entry.get("cleared_only", True)),
        }
        for key, low, high in (("stage", 1, None), ("difficulty", 1, 4)):
            value = entry.get(key)
            if value is not None:
                rule[key] = _as_int(value, "%s.%s" % (where, key), low, high)
        if entry.get("note"):
            rule["note"] = str(entry["note"])
        out.append(rule)
    return out


# --------------------------------------------------------------------------
# 读盘（带热重载）
# --------------------------------------------------------------------------

_SPECS = {
    SHOP_FILENAME: (validate_shop, default_shop, {}),
    RECIPE_FILENAME: (validate_recipes, default_recipes, []),
    DROPS_FILENAME: (validate_drops, default_drops, []),
}


def path_of(filename, data_dir=None):
    return os.path.join(data_dir or DATA_DIR, filename)


def _load(filename, data_dir=None, _reload=False):
    """`(解析结果, 警告列表)`。**坏文件保留上一份好的，绝不回写。**"""
    validate, _build, empty = _SPECS[filename]
    path = path_of(filename, data_dir)
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _lock:
            cached = _cache.get(path)
        if cached:
            # 文件被删了/暂时读不到：保留上一份，别让商店突然清空。
            return cached[1], ["读不到 %s，继续用上一次读到的内容" % path]
        return empty, ["没有找到 %s，%s 是空的" % (path, filename)]

    with _lock:
        cached = _cache.get(path)
    if cached and cached[0] == stamp and not _reload:
        return cached[1], cached[2]

    try:
        with open(path, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
        parsed = validate(raw)
    except (IOError, OSError, ValueError, ConfigError) as exc:
        # ★ 用户可能正编辑到一半。保留上一份好的，什么都不写。
        warning = "%s 读不了或不合法（%s）；" % (path, exc)
        if cached:
            return cached[1], [warning + "继续用上一次读到的内容"]
        return empty, [warning + "当它是空的"]

    result = (stamp, parsed, [])
    with _lock:
        _cache[path] = result
    return parsed, []


def shop(data_dir=None, _reload=False):
    """`{itemId: 条目}`。"""
    return _load(SHOP_FILENAME, data_dir, _reload)


def recipes(data_dir=None, _reload=False):
    """`[配方…]`。"""
    return _load(RECIPE_FILENAME, data_dir, _reload)


def drops(data_dir=None, _reload=False):
    """`[掉落规则…]`。"""
    return _load(DROPS_FILENAME, data_dir, _reload)


def invalidate(data_dir=None):
    """丢掉缓存。管理页保存之后调一下，省得等 mtime 粒度。"""
    with _lock:
        if data_dir is None:
            _cache.clear()
            return
        for filename in _SPECS:
            _cache.pop(path_of(filename, data_dir), None)


# --------------------------------------------------------------------------
# 首次生成
# --------------------------------------------------------------------------

def write_json(path, data):
    """原子写：tmp → fsync → replace（和 `account_store._write_unlocked` 同款）。

    ★ `newline="\\n"`：铁律 3 要求 `.json` 一律 LF 无 BOM
    （服务端包要在 Linux 上跑）。
    """
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def ensure_files(data_dir=None):
    """三份配置不存在就生成，**已存在一律不覆盖**（D7）。返回新建了哪几个。

    ★ 这是**唯一会自动写**这三个文件的地方。云上升级时用户手改过的价格 /
    配方 / 掉落必须原样留着 —— 覆盖它们等于把运营数据抹了（铁律 11）。
    （另一个写入点是管理页的保存按钮，`web/admin.py`：那是用户**主动**按的，
    而且存盘前必过 `validate_*`。除此之外谁都不许写。）
    """
    created = []
    for filename, (_validate, build, _empty) in _SPECS.items():
        path = path_of(filename, data_dir)
        if os.path.exists(path):
            continue
        write_json(path, build())
        created.append(filename)
    if created:
        invalidate(data_dir)
    return created
