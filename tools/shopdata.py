#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shopdata.py —— 从原版 ini 离线提取**物品表**（V0.3 合成与商店 M1）。

    python tools\\shopdata.py                    # 提取到 server\\shop_items.json
    python tools\\shopdata.py --dump 1120041     # 顺便打印某几件，人工核对
    python tools\\shopdata.py --dump-kind weapon # 打印某一类的全部

## 提取哪几张表、为什么

| 源文件 | 拿什么 | 干嘛用 |
|---|---|---|
| `ShopItem-Chn.ini` | itemId / `PartFlag` / `Tag` / 图标名 | 商店目录的骨架；`PartFlag` 是**装备槽冲突判定**的唯一依据 |
| `EquipBonus-Chn.ini` | 13 格加成 | 商店 / 仓库里显示「这件加多少」。★ 服务端**不下发**它，客户端自己有（§1） |
| `weapon.ini` | `Damage` / `ReloadTime` / `MagazineCount` … | 武器的强弱 —— 定价和展示要用 |
| `Promotion-chn.ini` | `RewardN` / `PromotionItemID` | **材料掉落的原版基线**（M6 的 `drops.json` 拿它做底） |

## 为什么服务端不直接读这些 ini

服务端包里**没有** `Pack_decrypt/` —— 那是 368 MB 客户端安装包解出来的资源，
云端根本没有。和武器表 / 地形数据同一个道理（V0.3bot D19 / D29）。

## ★ 为什么用中文版那三份

中文版比韩版少 622 条（3 级手枪 / 狙击整批没上、期限版外观砍掉一大半）。
**发一个中文版客户端没有的 id 下去，客户端在自己的 `ShopItem-Chn.ini` 里查不到，
图标画不出来。** 所以口径就是中文版那份（`Chinese.ini` 里
`Data/ShopItem.ini=Data/ShopItem-Chn.ini` 这条替换规则决定的）。

## 产物

`server/shop_items.json`（进 git、进服务端包），读它的是 `server/shopdata.py`：

    items        {itemId(str): {part_flag, character, part, kind, …}}
    by_kind      {kind: [itemId, …]}
    promotions   [ {原版任务奖励定义…} ]    ← M6 的掉落基线
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

def _load_sibling(name):
    """按**路径**加载 `tools/` 下的同伴模块，挂一个独立模块名。

    ★★ **不能写 `sys.path.insert(0, HERE)` + `import weapondata`**：
    `server/` 下也有一个 `weapondata.py`（运行时读取器，同名不同物），
    `mapdata.py` / `chrprops.py` 也是成对的。跑全量测试时 `server/` 先被
    导入过，`weapondata` 已经在 `sys.modules` 里 —— 后插的 `sys.path`
    根本不起作用，`import weapondata` 拿到的是服务端那份，
    当场 `ImportError: cannot import name 'read_ini'`（会话 01 踩过）。
    """
    import importlib.util
    path = os.path.join(HERE, name + ".py")
    spec = importlib.util.spec_from_file_location("shopdata_" + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# `read_ini` 已经把「UTF-16LE / UTF-8 BOM / CP949 / CP936 四选一 + `#Key=` 当注释
# + 重复节名后盖前」这套原版 ini 的怪脾气处理干净了，不要再写第二份。
_weapondata = _load_sibling("weapondata")
read_ini = _weapondata.read_ini
WeaponDataError = _weapondata.WeaponDataError

#: 产物格式版本。加/改字段时 +1，`server/shopdata.py` 拿它判「这份产物我认不认识」。
FORMAT = 1


class ShopDataError(Exception):
    pass


# --------------------------------------------------------------------------
# 装备加成
# --------------------------------------------------------------------------

#: ★★ **index → 属性名**，顺序不能动。
#:
#: 来源是客户端静态初始化器 `0x413096` 建的名字表 `0x732bfc..0x732c30`，
#: 解析循环（`0x4131f7`）`edi` 从 **1** 起 —— index 0 是个空串，保留未用。
#: 战斗里 `0x407014 GetEquipBonus(座位, index, key)` 就按这个 index 取（§2）。
#:
#: 我们其实用不到 index（加成是客户端自己算的），但**存下来能让人一眼看出
#: 这张表和客户端是对齐的**，以后要发 `SetEquipBonusFunction` 也现成。
BONUS_FIELDS = (
    (1, "Attack", "attack"),
    (2, "Defense", "defense"),
    (3, "Critical", "critical"),
    (4, "MoveSpd", "movespd"),
    (5, "Hp", "hp"),
    (6, "Sp", "sp"),
    (7, "TeamDmg", "teamdmg"),
    (8, "SelfDmg", "selfdmg"),
    (9, "AntiGhostCnt", "antighostcnt"),
    (10, "HeartBoost", "heartboost"),
    (11, "IncSplashRange", "incsplashrange"),
    (12, "DashAttack", "dashattack"),
    (13, "TeamReflection", "teamreflection"),
)

#: `EquipBonus.ini` 的键名 -> 产物里的键。★ 大小写不敏感（原版自己就不一致）。
_BONUS_BY_INI_NAME = {ini.lower(): out for _idx, ini, out in BONUS_FIELDS}


def parse_bonus_value(text):
    """把 `EquipBonus.ini` 的一格解析成 `(数值, Lua源码)`，两者只有一个非 None。

    ★★ **数值必须用整数解析**，因为客户端就是这么干的：`0x413268` 调的是
    `_wtoi`，`Defense=5.0` 在客户端眼里是 **5**、`Hp=0.5` 是 **0**。
    这里要是用 `float()`，服务端显示的数字和玩家实际吃到的加成就对不上。

    非数字的那 12 条是 **Lua 源码**（客户端 `0x41322b` 走
    `TouchEquipBonus` + `SetEquipBonusFunction` 那条分支），例如
    `Attack=if mychr:GetLastBulletROHIdx() == 110001 then return 15 else return 0 end`。
    这种没法预先算出一个数 —— 原样留字符串，展示时标成「条件加成」。
    """
    text = (text or "").strip()
    if not text:
        return None, None
    # 客户端的判据：第一个宽字符是数字或 '-' 就走数值路径（`0x413208` +
    # `iswdigit`），否则整格当 Lua 源码。这里照抄，别用 try/float 猜。
    head = text[0]
    if head.isdigit() or head == "-":
        try:
            return int(float(text)), None
        except ValueError:
            return None, text
    return None, text


def load_equip_bonus(path):
    """`{itemId(int): {"bonus": {属性: 整数}, "bonus_lua": {属性: 源码}}}`。"""
    out = {}
    for section, fields in read_ini(path).items():
        try:
            item_id = int(section.strip())
        except (TypeError, ValueError):
            continue
        bonus = collections.OrderedDict()
        bonus_lua = collections.OrderedDict()
        for key, value in fields.items():
            name = _BONUS_BY_INI_NAME.get(key.strip().lower())
            if name is None:
                continue
            number, lua = parse_bonus_value(value)
            if lua is not None:
                bonus_lua[name] = lua
            elif number:            # 0 不存：加 0 等于没加，白占产物体积
                bonus[name] = number
        if bonus or bonus_lua:
            out[item_id] = (bonus, bonus_lua)
    return out


# --------------------------------------------------------------------------
# 物品分类
# --------------------------------------------------------------------------

#: 7 位 id 的**部位码 -> 种类**。部位码 = id 的第 2、3 位。
#:
#: `51..63` 是「同一件外观的期限制售卖形态」（部位码 + 50），只有 `[Stock-]`
#: 没有 `[Item-]`；归类时先减 50 再查这张表，另打一个 `timed` 标记。
PART_KIND = {
    1: "armor",     # 上衣      PartFlag 1
    2: "armor",     # 下装      PartFlag 2
    3: "armor",     # 手套      PartFlag 16
    4: "armor",     # 鞋        PartFlag 8
    5: "armor",     # 头 / 脸   PartFlag 4
    6: "dash",      # 突击技    PartFlag 32
    7: "spray",     # 喷漆      PartFlag 64
    8: "armor",     # 尾饰      PartFlag 256
    9: "armor",     # 翅膀      PartFlag 512
    11: "armor",    # 头饰      PartFlag 128
    12: "weapon",   # 武器      PartFlag 1024 / 2048 / 4096
    13: "ring",     # 戒指      PartFlag 16384
    21: "key",      # 金钥匙
    39: "package",  # 套装礼包
    99: "package",  # 套装打包
}

#: **合成材料的 id 段**（FINDINGS §8）。这几段全是 `PartFlag=0` + `Tag` 空 +
#: 只有 `[Item-]` 没有 `[Stock-]`（从不出售）。
#: ★ `4xxxx` 和 `5xxxx` 是**两套完全同名同图**的材料，两套都收。
MATERIAL_RANGES = (
    (10001, 10099),    # 黑 / 红 / 绿 / 蓝珠
    (20001, 20099),    # 铁矿石
    (30001, 30099),    # 水管 / 火焰碎片 / 制作书 / 不死鸟之羽泪 / 青铜管 / 浮游石
    (40001, 40099),    # Z 芯片 / 龙之泪 / 太阳碎片 …
    (50001, 50099),    # ↑ 同名同图的第二套
    (60001, 60099),    # 称号素材
    (110001, 110099),  # 武器精通素材（左轮高手 …）
    (120001, 120099),
    (130001, 130099),
)

#: 6 位 id 的**前两位 -> 种类**（材料段已经被 `MATERIAL_RANGES` 先接走了）。
PREFIX_KIND_6 = {
    21: "consumable",  # 命 / 换角色卡 / 扳手 / 喇叭 / 战绩重置
    22: "pet",
    23: "enchant",     # 月亮附魔石
    38: "package",     # 活动礼包
    56: "title",
    61: "title",
    62: "title",
    63: "title",
    71: "consumable",  # 期限版消耗品
    72: "pet",         # 期限版宠物
}

#: 武器槽 `PartFlag` -> 槽位序号。
WEAPON_SLOT_BY_FLAG = {1024: 1, 2048: 2, 4096: 3}

#: 三个武器系列。key 是 id 尾四位 `//10` 的十位段（见 `weapon_variant`）。
SERIES_NAMES = {"D": "爆裂", "R": "极速", "F": "复合"}


def classify(item_id, part_flag):
    """返回 `(kind, part, character, timed)`。

    - `kind`  weapon / armor / material / character / … （见 `PART_KIND` 等）
    - `part`  7 位 id 的部位码；其它 id 是 `None`
    - `character` 角色限定（0/1/2 = 泰尔 / 卡希尔 / 布洛克；`None` = 不限）
    - `timed` 这条是不是「期限制售卖形态」（部位码 51..63）
    """
    for low, high in MATERIAL_RANGES:
        if low <= item_id <= high:
            return "material", None, None, False

    text = str(item_id)
    if len(text) == 9:
        # 商城角色本体：`(角色 id + 1) * 1000000 + 400001`（account_store 同款）
        return "character", None, None, False
    if len(text) == 7:
        character = int(text[0]) - 1
        part = int(text[1:3])
        timed = 51 <= part <= 63
        base = part - 50 if timed else part
        return PART_KIND.get(base, "other"), base, character, timed
    if len(text) == 6:
        return PREFIX_KIND_6.get(int(text[:2]), "other"), None, None, False
    return "other", None, None, False


def weapon_variant(item_id):
    """武器 id -> `(系列, 槽位, 等级)`；不是 D/R/F 变体就返回 `(None, None, None)`。

    id 形如 `C 12 0 SS T`（★ 尾四位是 `0SST`）：
    `SS` = 1~3 → **D 爆裂**的槽 1/2/3，4~6 → **R 极速**，7~9 → **F 复合**；
    `T` = 1/2/3 级。

    ★ 用尾**四**位判、且把范围卡在 `11..93`，是为了避开三处撞车：
      `1120001..1120004`（SE / Classic，尾四位 1..4）、
      `1120101`（Platinum，尾四位 101），以及榴弹类的
      `1120220 / 1120221 / 1120222`（= 基础 id `1120022` 사과탄 D2 的三个
      **售卖变体**，尾四位 `SSTV`）。只取尾两位的话这三批都会被误判成 D 系。

    ★ 那 54 条 `SSTV` 变体和部位码 +50 的期限版一样，**只有 `[Stock-]`
      没有 `[Item-]`** ⇒ 没有 `Tag`、没有 `PartFlag`、`ownable` 为假。
      它们进不了背包，我们的商店也不会上架。
    """
    tail = item_id % 10000
    if not (11 <= tail <= 93):
        return None, None, None
    group, tier = divmod(tail, 10)
    if not (1 <= group <= 9 and 1 <= tier <= 3):
        return None, None, None
    return "DRF"[(group - 1) // 3], ((group - 1) % 3) + 1, tier


#: 图标文件名里的类别前缀（去掉之后才是物品名）。
_ICON_PREFIXES = ("무기_", "돌격기_", "셋트_")

#: 形如 `ch00B0015` / `ch00^W0020` 的是**模型编号**，不是名字。
_MODEL_CODE = re.compile(r"^ch\d{2}[\^A-Za-z0-9]")


def icon_name(image):
    """`Images/Shop/무기_리볼버 R1.png` -> `('무기_리볼버 R1', '리볼버 R1')`。

    第二个值是**韩文物品名** —— 原版把名字藏在图标文件名里，
    `ShopItem.ini` 本身一个名字字段都没有，`Chinese.ini` 里也查不到
    （FINDINGS §5）。所以中文名只能靠人翻，这里先把韩文名捞出来。

    模型编号那种（`ch00B0015`）没有名字，第二个值给 `None`。
    """
    stem = os.path.splitext(os.path.basename((image or "").replace("\\", "/")))[0]
    if not stem:
        return None, None
    if _MODEL_CODE.match(stem):
        return stem, None
    name = stem
    for prefix in _ICON_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return stem, name.strip() or None


# --------------------------------------------------------------------------
# 武器数值
# --------------------------------------------------------------------------

#: 从 `weapon.ini` 带出来的字段：`ini 名 -> (产物键, 转换)`。
#: 只留「商店里要展示 / 定价要参考」的那几格 —— 弹道那一大堆 bot 才用，
#: 已经在 `bot_weapons.json` 里了，不重复。
WEAPON_FIELDS = (
    ("Name", "name", str),
    ("Damage", "damage", int),
    ("HeadDamage", "head_damage", int),
    ("LegsDamage", "legs_damage", int),
    ("SplashDamage", "splash_damage", int),
    ("SplashRange", "splash_range", int),
    ("ReloadTime", "reload_ms", int),
    ("MagazineCount", "magazine", int),
    ("CoolingTime", "cooling_ms", int),
    ("Velocity", "velocity", int),
    ("ROH", "roh", int),
)


def load_weapons_by_ammo(path):
    """`{弹药 Id(int): {展示字段…}}`。

    ★ `ShopItem.ini` 里武器条目的 `Tag` **就是这个弹药 Id**（写成
    `1000015.0` 这种带小数点的形式），靠它把商店条目和武器数值接起来。
    """
    out = {}
    for _section, fields in read_ini(path).items():
        raw_id = fields.get("Id")
        if raw_id is None:
            continue
        try:
            ammo = int(float(raw_id))
        except (TypeError, ValueError):
            continue
        record = collections.OrderedDict()
        for ini_name, key, cast in WEAPON_FIELDS:
            value = fields.get(ini_name)
            if value is None:
                continue
            if cast is str:
                text = value.strip()
                if text:
                    record[key] = text
                continue
            try:
                record[key] = int(float(value))
            except (TypeError, ValueError):
                # ★ 原版自己有笔误：`ch00-02SE` 写的是 `Damage=22호`
                #   （韩文「호」黏在数字后面）。剥掉非数字前缀再试一次。
                match = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(value))
                if match:
                    record[key] = int(float(match.group(1)))
        out[ammo] = record          # 后读盖先读，和客户端逐行读进 map 一致
    return out


def tag_ammo_id(tag):
    """`Tag=1000015.0` -> `1000015`；不是数字（模型路径 / 颜色 / 空）就 `None`。"""
    text = (tag or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 商店条目
# --------------------------------------------------------------------------

_SECTION = re.compile(r"^(Stock|Item)-(\d+)$", re.IGNORECASE)


def build_items(shop_sections, bonus_table, weapons_by_ammo, warn):
    """把三张表拼成 `{itemId(str): 记录}`。"""
    items = collections.OrderedDict()
    for section, fields in shop_sections.items():
        match = _SECTION.match(section.strip())
        if match is None:
            continue
        kindtag = match.group(1).lower()
        item_id = int(match.group(2))
        entry = items.get(str(item_id))
        if entry is None:
            entry = _new_item(item_id, fields, bonus_table, weapons_by_ammo, warn)
            items[str(item_id)] = entry
        # `[Stock-]` = 出现在货架上过；`[Item-]` = 能进背包的持有物条目。
        # 材料只有 Item，纯期限售卖形态只有 Stock，普通商品两个都有。
        entry["stock" if kindtag == "stock" else "ownable"] = True
        if not entry.get("icon"):
            stem, name = icon_name(fields.get("Image"))
            if stem:
                entry["icon"] = stem
                if name:
                    entry["name_kr"] = name
    return items


def _new_item(item_id, fields, bonus_table, weapons_by_ammo, warn):
    part_flag = 0
    raw_flag = fields.get("PartFlag")
    if raw_flag is not None:
        try:
            part_flag = int(float(raw_flag))
        except (TypeError, ValueError):
            warn("PartFlag 解析不了：%s -> %r" % (item_id, raw_flag))

    kind, part, character, timed = classify(item_id, part_flag)
    stem, name = icon_name(fields.get("Image"))

    entry = collections.OrderedDict()
    entry["id"] = item_id
    entry["kind"] = kind
    entry["part_flag"] = part_flag
    if part is not None:
        entry["part"] = part
    if character is not None:
        entry["character"] = character
    if timed:
        entry["timed"] = True
    if stem:
        entry["icon"] = stem
    if name:
        entry["name_kr"] = name
    entry["stock"] = False
    entry["ownable"] = False

    if kind == "weapon":
        slot = WEAPON_SLOT_BY_FLAG.get(part_flag)
        series, series_slot, tier = weapon_variant(item_id)
        if slot is not None:
            entry["slot"] = slot
        if series is not None:
            entry["series"] = series
            entry["tier"] = tier
            # ★ 交叉校验：id 推出来的槽位必须和 `PartFlag` 一致。
            #   对不上说明 id 编码的规律理解错了，宁可当场喊出来。
            if slot is not None and series_slot != slot:
                warn("武器 %s 的槽位对不上：id 推出 %d，PartFlag 是 %d"
                     % (item_id, series_slot, slot))
        ammo = tag_ammo_id(fields.get("Tag"))
        if ammo is not None:
            entry["ammo_id"] = ammo
            weapon = weapons_by_ammo.get(ammo)
            if weapon:
                entry["weapon"] = weapon
            else:
                warn("武器 %s 的弹药 id %s 在 weapon.ini 里查不到" % (item_id, ammo))

    bonus, bonus_lua = bonus_table.get(item_id, (None, None))
    if bonus:
        entry["bonus"] = bonus
    if bonus_lua:
        # ★ 条件加成（Lua）没法预先算成一个数，展示时要标出来。
        entry["bonus_lua"] = bonus_lua
    return entry


# --------------------------------------------------------------------------
# 任务奖励基线（M6 的 drops.json 拿它做底）
# --------------------------------------------------------------------------

#: `RewardN=<类型>,<参数…>` 的类型码。
REWARD_KINDS = {0: "money", 1: "item", 2: "experience", 3: "item"}


def parse_reward(text):
    """`1,0030018,0` -> `{"kind": "item", "item_id": 30018, "days": 0}`。"""
    parts = [p.strip() for p in (text or "").split(",")]
    if not parts or not parts[0]:
        return None
    try:
        code = int(parts[0])
    except ValueError:
        return None
    kind = REWARD_KINDS.get(code)
    if kind is None:
        return None
    out = collections.OrderedDict((("kind", kind),))
    try:
        if kind == "item":
            out["item_id"] = int(parts[1])
            # 类型 3 的第三格恒是 100（韩版的「永久」写法），类型 1 才是天数。
            out["days"] = 0 if code == 3 else int(parts[2]) if len(parts) > 2 else 0
        else:
            out["amount"] = int(parts[1])
    except (IndexError, ValueError):
        return None
    return out


def load_promotions(path):
    """`Promotion-chn.ini` -> 列表。**只留和奖励有关的字段**，剧情文本不要。"""
    out = []
    for section, fields in read_ini(path).items():
        rewards = []
        for slot in range(4):
            reward = parse_reward(fields.get("Reward%d" % slot))
            if reward:
                rewards.append(reward)
        drop_id = fields.get("PromotionItemID")
        if not rewards and drop_id is None:
            continue

        entry = collections.OrderedDict()
        entry["section"] = section
        for ini_name, key in (("PromotionID", "promotion_id"),
                              ("Name", "name"),
                              ("MapFileName", "map")):
            value = (fields.get(ini_name) or "").strip()
            if value:
                entry[key] = value
        for ini_name, key in (("UserLevelLimit", "level"),
                              ("CharacterLimit", "character_limit"),
                              ("PromotionType", "type"),
                              ("PromotionGameContextType", "context_type"),
                              ("PromotionQuestStageID", "quest_stage"),
                              ("PromotionDifficultyLevel", "difficulty")):
            raw = fields.get(ini_name)
            if raw is None:
                continue
            try:
                value = int(float(raw))
            except (TypeError, ValueError):
                continue
            if key == "level":
                # ★ `UserLevelLimit` 有 `1011`~`1016` 这种值 —— **1000 是标记位，
                #   不是等级**。实测（106 条全查过）：
                #     · `>= 1000` 的 51 条**全部**是 `PromotionType=4` 且**全部**带
                #       `PromotionQuestStageID`（= 闯关模式的成就）；
                #     · `UserLevelLimit % 1000` 和文案 `TextMissionInfo` 里的
                #       「等级N」**105 条全对得上，0 例外**。
                #   所以真等级是 `% 1000`；原值也留一份，免得以后要用那个标记位。
                if value >= 1000:
                    entry["level_raw"] = value
                value %= 1000
            entry[key] = value
        if rewards:
            entry["rewards"] = rewards
        if drop_id is not None:
            try:
                drop = collections.OrderedDict((("item_id", int(float(drop_id))),))
                prob = fields.get("PromotionItemProb")
                drop["prob"] = int(float(prob)) if prob is not None else 100
                entry["item_drop"] = drop
            except (TypeError, ValueError):
                pass
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# 找源文件
# --------------------------------------------------------------------------

def find_data_file(name, explicit=None):
    """找 `Pack_decrypt/Data/<name>`（和 `weapondata.find_weapon_ini` 同一套口径）。"""
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append(os.path.join(ROOT, "Pack_decrypt", "Data", name))
    candidates.append(os.path.abspath(os.path.join(
        ROOT, "..", "..", "main", "Pack_decrypt", "Data", name)))
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    raise SystemExit(
        "找不到 %s。试过：\n  %s\n用命令行参数指路，例如"
        " --shop-ini D:\\git\\popshot-reborn\\main\\Pack_decrypt\\Data\\%s"
        % (name, "\n  ".join(candidates), name))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="从原版 ini 提取物品表")
    ap.add_argument("--shop-ini", help="ShopItem-Chn.ini 的路径")
    ap.add_argument("--bonus-ini", help="EquipBonus-Chn.ini 的路径")
    ap.add_argument("--weapon-ini", help="weapon.ini 的路径")
    ap.add_argument("--promotion-ini", help="Promotion-chn.ini 的路径")
    ap.add_argument("--out", help="输出文件（默认 server\\shop_items.json）")
    ap.add_argument("--dump", nargs="*", metavar="itemId",
                    help="打印这几件物品")
    ap.add_argument("--dump-kind", metavar="KIND",
                    help="打印某一类的全部（weapon / material / armor …）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    shop_path = find_data_file("ShopItem-Chn.ini", args.shop_ini)
    bonus_path = find_data_file("EquipBonus-Chn.ini", args.bonus_ini)
    weapon_path = find_data_file("weapon.ini", args.weapon_ini)
    promo_path = find_data_file("Promotion-chn.ini", args.promotion_ini)
    out_path = args.out or os.path.join(ROOT, "server", "shop_items.json")

    warnings = []

    def warn(message):
        warnings.append(message)

    try:
        shop_sections = read_ini(shop_path)
        bonus_table = load_equip_bonus(bonus_path)
        weapons_by_ammo = load_weapons_by_ammo(weapon_path)
        promotions = load_promotions(promo_path)
    except WeaponDataError as exc:
        raise SystemExit("解析失败：%s" % exc)

    items = build_items(shop_sections, bonus_table, weapons_by_ammo, warn)
    if not items:
        raise SystemExit("%s 里一条商店条目都没有" % shop_path)

    # 加成表里有、商店表里没有的 id —— 说明中文版砍掉了那件东西的货架条目，
    # 但加成还留着。**不要**把它们补进 items：客户端查不到图标。
    orphan_bonus = sorted(i for i in bonus_table if str(i) not in items)

    by_kind = collections.OrderedDict()
    for key, entry in items.items():
        by_kind.setdefault(entry["kind"], []).append(int(key))
    for ids in by_kind.values():
        ids.sort()
    by_kind = collections.OrderedDict(sorted(by_kind.items()))

    table = collections.OrderedDict((
        ("format", FORMAT),
        ("source", "+".join(os.path.basename(p) for p in
                            (shop_path, bonus_path, weapon_path, promo_path))),
        ("count", len(items)),
        ("bonus_index", collections.OrderedDict(
            (out, idx) for idx, _ini, out in BONUS_FIELDS)),
        ("by_kind", by_kind),
        ("items", items),
        ("promotions", promotions),
    ))

    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    # ★ `newline="\n"`：Windows 文本模式会把 `\n` 转成 `\r\n`，
    #   而铁律 3 要求 `.json` 一律 **LF 无 BOM**（服务端包要在 Linux 上跑）。
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(table, fp, ensure_ascii=False, separators=(",", ":"))
        fp.write("\n")
    os.replace(tmp_path, out_path)

    _dump(items, by_kind, args)

    if not args.quiet:
        size = os.path.getsize(out_path)
        print("完成：%d 件物品 -> %s（%.1f KB）" % (len(items), out_path, size / 1024.0))
        print("   按种类：" + "、".join(
            "%s %d" % (kind, len(ids)) for kind, ids in by_kind.items()))
        print("   任务奖励定义 %d 条（M6 的掉落基线）" % len(promotions))
        if orphan_bonus:
            print("   ⓘ %d 个 id 有装备加成但中文版没有货架条目（已跳过，"
                  "发下去客户端查不到图标）：%s%s"
                  % (len(orphan_bonus),
                     "、".join(str(i) for i in orphan_bonus[:6]),
                     " …" if len(orphan_bonus) > 6 else ""))
        for message in warnings[:20]:
            print("   ⚠ " + message)
        if len(warnings) > 20:
            print("   ⚠ …另有 %d 条警告" % (len(warnings) - 20))
    return 0


def _dump(items, by_kind, args):
    wanted = list(args.dump or [])
    if args.dump_kind:
        wanted += [str(i) for i in by_kind.get(args.dump_kind, ())]
    for item_id in wanted:
        entry = items.get(str(item_id))
        print("--- %s ---" % item_id)
        if entry is None:
            print("    表里没有这个 id")
            continue
        for key, value in entry.items():
            print("    %-12s %s" % (key, value))


if __name__ == "__main__":
    sys.exit(main())
