#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shopcfg.py —— 商店 / 合成 / 掉落的**运行时配置**（V0.3 合成与商店 M1）。

四份 JSON，都在 `server/data/`，**用户随时手改，改完不用重启**：

| 文件 | 管什么 |
|---|---|
| `items.json` | ★ **物品库**：中文名 + 等级门槛 + 角色限定的**唯一**出处（D31）|
| `shop.json` | 哪些东西上架、卖多少钱 |
| `recipe.json` | 合成配方（产物 / 花费 / **最多 4 种材料**）|
| `drops.json` | 打完一局掉什么材料 |

⚠ **中文名、等级、角色限定只在 `items.json` 里**。它们是**物品自己的**属性
（后两个客户端在「穿上」那一刻才读，`ItemInfo+0x1c` / `+0x24`），不是
「这次买卖」或「这条配方」的属性 —— 分两处存必然对不上。
查名字一律走 `name_of()` / `item_name()`，查门槛一律走 `rule_of()` / `item_rule()`。

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
import shutil
import threading
import time

import shopdata

#: 认得的配置格式版本。用户手改时不用管它；将来结构变了靠它做迁移。
FORMAT = 1

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

#: ★ **物品库**：中文名、等级门槛、角色限定的**唯一**出处（D31）。
#: 以前这几样散在 `shop.json`（等级 + 名字）和 `recipe.json`（角色 + 名字）里，
#: 结果「同一件东西在商店卖和靠合成拿，等级要求和名字可以不一样」—— 那是没有
#: 意义的，客户端只认 `ItemInfo` 里的一份。用户 2026-09-06 拍板搬到这里。
ITEMS_FILENAME = "items.json"
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
#: 所以这张表是**我们自己翻的**，用户可以在管理页的「物品库」里改（改的是
#: `items.json` 的 `name`，这张表只在**首次生成**和「物品库里没登记」时用）。
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

#: 关卡 id → 中文名。管理页的「关卡」下拉框用它。
#:
#: ★ **就这七个**：客户端建房时的关卡下拉框只认静态表 `0x6dc52c` 里的
#:   `(3, 2, 1, 4, 5, 6, 7)`（`tools/probe_quest_list.py` 逆出来的）。
#:   `drops.json` 的 `stage` 比的就是这个 id（`gameserver.quest_materials`
#:   拿它和房间描述符的第一个参数比）。
#:
#: ★ 名字取的是**boss / 主题名**，不是成就名 —— 用户是按「岩浆巨龙」这种
#:   叫法记关卡的。两个独立来源对得上，所以可以放心写死：
#:
#:   | id | `map.ini` 主题 → `Chinese.ini` | `Promotion-chn.ini` 的成就名 |
#:   |---|---|---|
#:   | 1 | 불프로그 → 机械青蛙 | [任务] 起火的村庄! |
#:   | 2 | 드라카 → 岩浆巨龙 | [任务] 埃斯佩拉的大怪兽! |
#:   | 3 | 비밀의 섬 → 神秘岛 | [任务] 神秘岛攻略! |
#:   | 4 | 자미로건쉽 → 鲸鱼战舰 | [任务] 鲸鱼战舰击破! |
#:   | 5 | 다크나이트 → 黑骑士 | [任务] 加密尔地下废墟的秘密! |
#:   | 6 | 브레그마 → 太阳齿轮 | [任务] 突破扎米洛基地! |
#:   | 7 | 자미로 비밀기지 → **没有中文译名** | [任务] 寻找扎米洛的痕迹! |
#:
#:   id 和主题的对应有两条独立佐证：`map.ini` 里几张 boss 图的
#:   `RequiredQuestClear`（Draka=2 / Island=3 / Airship=4 / Darknight=5 /
#:   Bregma=6），以及同一份文件里那行注释
#:   `1:개굴 2:용 3:섬 4:비행기 5:말 6:해`。
#:
#: ★ 第 7 关的名字是**我们自己拼的**（原版没给中文）：「扎米洛」照第 6 关
#:   成就名里的官方译法，「秘密基地」照韩文主题名 `자미로 비밀기지`。
QUEST_ZH = {
    1: "机械青蛙",
    2: "岩浆巨龙",
    3: "神秘岛",
    4: "鲸鱼战舰",
    5: "黑骑士",
    6: "太阳齿轮",
    7: "扎米洛秘密基地",
}

#: 难度号 → **游戏里的中文名**。`drops.json` 的 `difficulty` 存的就是这个号，
#: 管理页的难度下拉框照它显示（用户 2026-09-05：「别再显示 1234」）。
#:
#: 号码 → 难度的对应是**客户端自己拼地图文件名**那一段定死的
#: （`mapdata.DIFFICULTY_SUFFIX`，出处 `0x405742` 的四发 `dec edi`）：
#: `1=#Easy 2=#Normal 3=#Hard 4=#Extreme`。
#:
#: 中文名取的是 `Chinese.ini` 的官方译法：
#: `이지 모드=简单` / `노멀 모드=普通` / `하드 모드=困难`
#: （另一处 `EASY=简单` / `NORMAL=普通` / `HARD=困难` 互相印证）。
#:
#: ★ 难度 4 的名字是**我们自己拼的**：中文版把 `익스트림 모드` 原样留成了
#:   `Extreme Mode`（没翻），管理页里摆一行英文太出戏，照 `#Extreme` 译作
#:   「极限」。和第 7 关的名字是同一种情况（见上面 `QUEST_ZH`）。
DIFFICULTY_ZH = {1: "简单", 2: "普通", 3: "困难", 4: "极限"}

#: `shopdata` 的 `kind` → 中文。管理页的物品选择器按它分页签。
#: ★ 键要和 `shop_items.json` 的 `by_kind` 对得上；查不到的 kind 原样显示，
#: 不要在这里硬编码一张「全部 kind」的清单（物品表换一版就可能多出一类）。
KIND_ZH = {
    "armor": "铠甲", "character": "角色", "consumable": "消耗品",
    "dash": "冲刺", "key": "钥匙", "material": "材料", "package": "礼包",
    "pet": "宠物", "ring": "戒指", "spray": "喷漆", "title": "称号",
    "weapon": "武器",
}

#: 套装部位后缀（韩文名的最后一段）→ 中文。
#:
#: ⚠⚠ **同一个部位有两种写法**，原版自己就不统一（2026-09-05 实机发现）：
#: 下装在 카실 / 프로코 身上叫 `다리`（腿），在 타이 身上叫 `바지`（裤子）；
#: 鞋一律叫 `신발`，`발`（脚）从来没单独出现过。
#: 漏掉 `바지` 的后果是**泰尔的四条下装全部不进合成配方**，而且中文名翻不出来
#: —— 用户 2026-09-05 报的「泰尔只能看到上衣和手套」就是它。
PART_SUFFIX_ZH = {"몸": "上衣", "다리": "下装", "바지": "下装", "손": "手套",
                  "신발": "鞋", "발": "鞋", "머리": "头盔"}


def _split_part_suffix(name):
    """`타이_마스터리아머 신발` → `("타이_마스터리아머", "鞋")`；没后缀就 `(原样, "")`。

    ★ **按长度从长到短试**：`신발` 也以 `발` 结尾，先撞上 `발` 会把它切成
    `…아머 신`，套装名对不上、那一件就悄悄消失（正是上面那个 bug 的机制）。
    dict 的插入顺序在这儿不该成为正确性的一部分 —— 排一下就不用记住了。
    """
    for suffix in sorted(PART_SUFFIX_ZH, key=len, reverse=True):
        if name.endswith(suffix):
            return name[:-len(suffix)].strip().rstrip("_"), PART_SUFFIX_ZH[suffix]
    return name, ""


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
    name, part_zh = _split_part_suffix((item.name_kr or "").strip())
    part = ("·" + part_zh) if part_zh else ""
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
# 物品说明（商店 / 仓库提示框下半那块，V0.3商店 §31）
# --------------------------------------------------------------------------
#: 装备加成的键 → (中文名, 是不是百分比)。★ 名字和算法都照客户端来
#: （§2 的 `Attack %+d%%` / `HP %+d` 那一组；界面上写的就是这五个词）。
BONUS_ZH = {
    "hp": ("生命", False),
    "sp": ("体力", False),
    "attack": ("攻击", True),
    "defense": ("防御", True),
    "movespd": ("速度", True),
}
#: 上面那张表之外的稀有加成（`antighostcnt` / `heartboost` 之类，全表 7 件）。
#: 客户端界面上根本没有它们的格子，说明里也就不提。

#: 说明最多几行。★ 客户端把这串按 `|` 切成**最多 3 段**（`0x45c4c9` 的
#: `cmp .., 2`）分给三个标签，我们只用第一段（240×88 那个大框），
#: 段内换行用 `\n`。
ITEM_DESC_MAX_LINES = 4


def item_desc_zh(item):
    """物品说明。**从本地数据现算**，原版那份说明随服务端 DB 一起没了。

    ⚠ 这不是「发明玩法」（铁律 12）—— 里面每个数都是客户端**自己也查得到**
    的（武器数值来自 `weapon.ini`、装备加成来自 `EquipBonus-Chn.ini`），
    只是原版把它们写在服务端下发的说明里，我们照着重新拼一遍。

    翻不出内容就返回空串（提示框那块留白，和以前一样）。
    """
    if item is None:
        return ""
    lines = []
    weapon = item.weapon or {}
    if weapon:
        damage = weapon.get("damage")
        head = weapon.get("head_damage")
        if damage is not None:
            lines.append("伤害 %d%s" % (damage,
                                       "（爆头 %d）" % head if head else ""))
        magazine = weapon.get("magazine")
        reload_ms = weapon.get("reload_ms")
        parts = []
        if magazine:
            parts.append("弹匣 %d 发" % magazine)
        if reload_ms:
            parts.append("换弹 %.2f 秒" % (reload_ms / 1000.0))
        if parts:
            lines.append("　".join(parts))
        velocity = weapon.get("velocity")
        if velocity:
            lines.append("初速 %d" % velocity)
    for key, value in sorted((item.bonus or {}).items()):
        label = BONUS_ZH.get(key)
        if label is None or not value:
            continue
        lines.append("%s %+d%s" % (label[0], value, "%" if label[1] else ""))
    if item.bonus_lua:
        # 条件加成（Lua 源码）客户端自己会算，服务端解释不了 —— 只提一句。
        lines.append("（附带条件加成）")
    return "\n".join(lines[:ITEM_DESC_MAX_LINES])


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


# --------------------------------------------------------------------------
# 物品库 items.json —— 中文名 + 等级门槛 + 角色限定的唯一出处（D31）
# --------------------------------------------------------------------------

#: 哪些物品有「等级 / 角色限定」这两栏。
#:
#: ★ 判据是 **`part_flag != 0`（占装备槽）**，不是「铠甲和武器」这两个类别名
#:   —— 客户端在**穿的那一刻**才读这两个字段（`0x445817` 比等级、
#:   `0x5584ab` 比角色掩码），穿不上身的东西（材料 / 消耗品 / 钥匙 / 礼包）
#:   读了也没人用。按 `part_flag` 分正好把 `ring`（戒指）/ `dash`（冲刺）/
#:   `pet` / `spray` / `title`（称号）这几类也一起收进来 —— 它们同样占槽位，
#:   同样要这两栏，按类别名列白名单会漏掉它们。
#:
#: ⚠ 这**只决定有没有那两栏**，不决定「收不收进物品库」——
#:   物品库收**全部**能进背包的东西（808 件），因为中文名人人都要一个。
def has_level_and_character(item):
    """这件东西要不要「等级 / 角色限定」两栏。"""
    return bool(item is not None and item.ownable and item.part_flag)


def default_items():
    """从 `shop_items.json` + 我们自己的定价表生成一份默认 `items.json`。

    ★ **收全部能进背包的东西**（`ownable`，808 件）—— 中文名是每一件都有的
    （材料、礼包、消耗品在管理页上也要认得出来）。等级和角色限定只给
    **占装备槽**的那 734 件写（`has_level_and_character`）。

    等级的来路（**都是复活工程定的**，原版的随服务端 DB 一起没了）：

    * 武器 —— 按档次 `WEAPON_LEVEL`（和 `default_shop` 的价格表配套）；
    * 合成产物 —— `RECIPE_LINES` 里那条产线的档位；
    * 其余 —— `1`（不限）。

    角色限定取原版数据（`shopdata` 的 `character`），键不写 = 不限。
    """
    levels = {}
    for _recipe, level in _recipe_seed():
        levels[_recipe["result"]] = level
    entries = []
    for kind in shopdata.kinds():
        for item_id in shopdata.ids_of_kind(kind):
            item = shopdata.get(item_id)
            if item is None or not item.ownable:
                continue
            entry = {"id": item.id, "name": item_name_zh(item)}
            if has_level_and_character(item):
                if item_id in levels:
                    entry["level"] = levels[item_id]
                elif item.kind == "weapon" and item.series:
                    entry["level"] = WEAPON_LEVEL.get(item.tier or 1, 5)
                else:
                    entry["level"] = 1
                if item.character is not None:
                    entry["character"] = int(item.character)
            entries.append(entry)
    entries.sort(key=lambda e: e["id"])
    return {"format": FORMAT, "items": entries}


def validate_items(raw):
    """`items.json` → `{itemId: 条目}`；有一条不对就抛 `ConfigError`。"""
    if not isinstance(raw, dict):
        raise ConfigError("items.json 的最外层必须是一个对象")
    listing = raw.get("items")
    if not isinstance(listing, list):
        raise ConfigError("items.json 缺少 items 列表")
    out = {}
    for index, entry in enumerate(listing):
        where = "items[%d]" % index
        if not isinstance(entry, dict):
            raise ConfigError("%s 不是对象" % where)
        item_id = _as_int(entry.get("id"), where + ".id", low=1)
        _check_item_id(item_id, where)
        if item_id in out:
            raise ConfigError("%s：物品 %d 出现了两次" % (where, item_id))
        item = shopdata.get(item_id)
        # ★ 穿不上身的东西（材料 / 消耗品 / 钥匙 / 礼包 / 角色卡）**只有中文名**
        #   —— 文件里就算留着 level / character 也当没看见，免得有人以为
        #   「给材料设个 5 级就要 5 级才能捡」，而客户端根本不看这两个字段。
        level, character = 1, None
        if has_level_and_character(item):
            character = entry.get("character")
            if character is not None:
                character = _as_int(character, where + ".character",
                                    low=0, high=2)
            level = _as_int(entry.get("level", 1), where + ".level", low=1)
        out[item_id] = {
            "id": item_id,
            "name": str(entry.get("name") or item_name_zh(item)),
            "kind": item.kind,
            "level": level,
            "character": character,
        }
    return out


def rule_of(table, item_id):
    """一件东西的「等级 + 角色限定」，返回 `(等级, 角色 或 None)`。

    `table` 是 `items()` 的结果。**物品库里没登记就退回原版数据**：
    不限等级（1）、角色照 `shop_items.json`。

    ★ 这是全服务端唯一该问「这件东西几级能穿 / 谁能穿」的地方（D31）——
    `shop.json` / `recipe.json` 里都没有这两个字段了。
    """
    entry = (table or {}).get(int(item_id))
    if entry is not None:
        return entry["level"], entry["character"]
    item = shopdata.get(item_id)
    return 1, (None if item is None else item.character)


def name_of(table, item_id):
    """一件东西的中文名。★ **全服务端唯一该问名字的地方**（D31）。

    `table` 是 `items()` 的结果；物品库里没登记就退回 `item_name_zh()`
    自己翻的那一份（再翻不出来就是韩文名）。
    """
    entry = (table or {}).get(int(item_id))
    if entry is not None and entry.get("name"):
        return entry["name"]
    return item_name_zh(shopdata.get(item_id))


def item_rule(item_id, data_dir=None):
    """`rule_of` 的独立版本（自己去读一次配置）。**警告会被丢掉** ——
    要在日志里看到「配置读坏了」的调用点请自己调 `items()`。"""
    return rule_of(items(data_dir)[0], item_id)


def item_name(item_id, data_dir=None):
    """`name_of` 的独立版本（自己去读一次配置）。警告同样会被丢掉。"""
    return name_of(items(data_dir)[0], item_id)


def default_shop():
    """从 `shop_items.json` 生成一份默认 `shop.json`。

    ★ **只收 63 件 D/R/F 武器**（都得 `ownable`，只有货架条目的进不了背包，
    §11）—— 这是本版商店的主体，也是唯一真的摆上货架的东西。

    ⚠ 以前这里还会把**全部材料**和**合成产物**收进来（`listed=false`），
    理由只有一个：「给它们一个中文名」。中文名搬进 `items.json` 之后
    （D31）那个理由没了，再收 86 条空条目只会让「商店货架」这一页
    看上去像个全物品表。要卖它们的话在管理页上「＋ 添加一条」就行。
    """
    entries = []

    for item_id in shopdata.ids_of_kind("weapon"):
        item = shopdata.get(item_id)
        if not item.ownable or not item.series or item.character is None:
            continue
        tier = item.tier or 1
        entries.append({
            "id": item.id,
            "kind": "weapon",
            "listed": True,
            "price": WEAPON_PRICE.get(tier, 3000),
            "days": 0,
        })

    entries.sort(key=lambda e: (e["kind"], e["id"]))
    # ★ 不写 `_说明`：那几句话是给**手改 json 的人**看的，而现在唯一的编辑入口
    #   是管理页（D16）。说明文字挪进了 `SCHEMA[...]["help"]`，页面直接渲染。
    return {"format": FORMAT, "items": entries}


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
            base, part_zh = _split_part_suffix(name)
            if not part_zh:            # 名字里没有部位后缀 = 不是套装的一件
                continue
            for korean in ("타이", "카실", "프로코"):
                if base.startswith(korean):
                    base = base[len(korean):].strip("_ ")
                    break
            groups.setdefault((item.character, base), []).append(item)
    return groups


def _recipe_seed():
    """`[(配方, 产物的装备等级)]` —— `default_recipes` 和 `default_items` 共用。

    ★ **等级不进 `recipe.json`**（D27）：合成本身**没有等级门**（原版的合成
    面板从头到尾不读玩家等级，FINDINGS §33），`RECIPE_LINES` 里那个数是
    **产物穿上时**的要求，归 `items.json` 管（D31）。两处各留一份的话，
    改了一处另一处不动，很快就对不上。
    """
    sets = _armor_sets()
    seed = []
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
                seed.append(({
                    "id": next_id,
                    "result": item.id,
                    "listed": True,
                    "cost": weight * gold_per_point,
                    "days": 0,
                    "materials": need[:MAX_MATERIALS],
                }, level))
                next_id += 1
    return seed


def default_recipes():
    """从 `shop_items.json` 生成一份默认 `recipe.json`。

    ★ **原版配方在客户端里彻底不存在**（FINDINGS §7），这一份是**我们自己
    设计的**（D2）。用户会在管理页里调，所以这里追求的是「一眼看得懂、
    改起来容易」，不是「一次到位」。
    """
    # `_说明` 见 `default_shop()` 那条注释：说明文字在 `SCHEMA` 里，不写进文件。
    return {"format": FORMAT,
            "recipes": [recipe for recipe, _level in _recipe_seed()]}


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

    # `_说明` 见 `default_shop()` 那条注释：说明文字在 `SCHEMA` 里，不写进文件。
    return {"format": FORMAT, "rules": rules}


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
    """`shop.json` → `{itemId: 条目}`；有一条不对就抛 `ConfigError`。

    ⚠ **这里没有 `level`、也没有 `name`**（D31）：等级门槛和中文名都搬去
    `items.json` 了 —— 同一件东西在商店买和靠合成拿，穿上它的条件和它叫
    什么名字都只有一份，客户端也只认一份。老文件里残留的键在这儿被丢掉，
    不报错。
    """
    if not isinstance(raw, dict):
        raise ConfigError("shop.json 的最外层必须是一个对象")
    # ★ 局部名别叫 `items` —— 模块级的 `items()` 是读物品库的入口，撞名之后
    #   在这个函数里就再也调不到它了（以后想在这儿查等级会当场踩坑）。
    listing = raw.get("items")
    if not isinstance(listing, list):
        raise ConfigError("shop.json 缺少 items 列表")
    out = {}
    for index, entry in enumerate(listing):
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
            "kind": item.kind,
            "listed": bool(entry.get("listed", False)),
            "price": _as_int(entry.get("price", 0), where + ".price", low=0),
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
    seen_results = {}
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
        # ★★ **一个产物只能有一条配方** —— `0x0606` 上行带的是**产物 itemId**
        #    而不是配方号（`0x45d738: push [rule+4]`，FINDINGS §27）⇒ 两条配方
        #    同产物时服务端根本分不清玩家点的是哪一条。这条不是我们的规矩，
        #    是协议的形状决定的，所以宁可拒收也不能「取第一条」。
        if result in seen_results:
            raise ConfigError(
                "%s：产物 %d 已经在 %s 里有配方了 —— 合成请求只带产物 id，"
                "一个产物只能有一条配方" % (where, result, seen_results[result]))
        seen_results[result] = where

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

        # ★ **配方没有 `level`、没有 `character`、也没有 `name`**：
        #   · 等级 —— 原版的合成面板从头到尾不读玩家等级，`0x0506` 的结果码里
        #     也没有「等级太低」这一档（D27）；
        #   · 角色限定 —— 那是**物品自己的**属性，不是「这条配方」的属性；
        #   · 中文名 —— 同上，「产物叫什么」不随「怎么拿到它」变（D31）。
        #   三个键都在 `items.json` 里；老文件里残留的在这儿被丢掉，不报错。
        out.append({
            "id": recipe_id,
            "result": result,
            "listed": bool(entry.get("listed", True)),
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
# 字段描述表 —— 管理页照着它生成输入框
# --------------------------------------------------------------------------
#
# ★★ **为什么放在这里，而不是放在 `web/admin.html` 里**（D16）
#
# 用户的要求是「以后新增的字段也要同步显示在画面上」。字段的**真相**在
# `validate_*` 里，所以描述表就得**贴着 validator 放**：加字段的人一眼看到
# 两边都要动。`test_shopcfg` 里有一条用例卡死这件事 ——
# validator 产出的每个键都必须在 SCHEMA 里、反之亦然，**单边加字段测试立刻红**。
#
# 这不是「靠人记得改两处」，是让漏改**必然**被发现（铁律 10 的同一种思路：
# 判据要来自掌握事实的那一方，不要靠约定）。
#
# ⇒ 前台还有第二道保险：**数据里有、SCHEMA 里没有**的键，管理页退回通用输入框
#   （数字→数字框 / 字符串→文本框 / 布尔→开关），所以哪怕 SCHEMA 一时没跟上，
#   那个字段也不会从画面上消失，更不会在保存时被吃掉。
#
# ## 字段类型（前台认得的全部）
#
#   item      物品选择器（图标 + 中文名 + #id）。`kinds` 限定只让选哪几类
#   text      文本框
#   int       数字框（`min` / `max`）
#   bool      开关
#   choice    下拉（`options`）。`optional=True` 时多一个「不限」= 键不写进 json
#   materials 合成材料槽（固定画 `max` 格，每格 `fields`）
#
# `optional` = 这个键可以整个不出现在 json 里（前台留空就不写）。
# `readonly` = 只读展示，值由别的字段推出来（比如 `kind` 由 `id` 决定）。

#: 四份配置各自的：列表键、标题、页面上的说明、字段表。
#:
#: ★ `help` 就是原来写在 json 里那几行 `_说明` —— 用户拍板搬到页面上、
#:   不再写进文件（D16）。
SCHEMA = {
    "items": {
        "list_key": "items",
        "title": "物品库",
        "unit": "件物品",
        "help": [
            "改完保存即刻生效，不用重启服务端。",
            "★ 这里是**中文名、等级门槛、角色限定的唯一出处** —— 「商店货架」"
            "和「合成配方」里都不再有这三栏。同一件东西不管是买来的还是合成的，"
            "它叫什么、几级能穿、谁能穿，都只有一份。",
            "★ 这一页收**全部能进背包的物品**（包括材料 / 礼包 / 消耗品 / "
            "角色卡）。但 等级 和 角色限定 只给**占装备槽**的东西"
            "（铠甲 / 武器 / 戒指 / 冲刺 / 宠物 / 喷漆 / 称号）—— 别的穿不上身，"
            "客户端根本不看这两个字段，所以那些卡片上没有这两个框。",
            "等级是**穿上时**的门槛，由客户端自己判（买和合成都不拦）；"
            "角色限定留空 = 谁都能穿。",
            "⚠ 角色限定改宽了只是让别的角色**能穿**，模型和贴图还是原来那个"
            "角色的 —— 原版的装备本来就是一人一套。",
            "每张卡片右上角写着它现在在哪儿上架（商店 / 合成 / 未上架），"
            "上架内容在「商店货架」和「合成配方」两页改。",
        ],
        "fields": [
            {"key": "id", "label": "物品", "type": "item"},
            {"key": "kind", "label": "类别", "type": "text", "readonly": True,
             "help": "由物品本身决定，改不了"},
            {"key": "name", "label": "中文名", "type": "text",
             "help": "原版没有中文物品名，这一份是复活工程自己翻的，随便改"},
            {"key": "level", "label": "等级", "type": "int", "min": 1,
             "help": "穿上它要几级。1 = 不限"},
            {"key": "character", "label": "角色限定", "type": "choice",
             "optional": True, "empty_label": "不限",
             "options": [{"value": cid, "label": name}
                         for cid, name in sorted(CHARACTER_ZH.items())]},
        ],
    },
    "shop": {
        "list_key": "items",
        "title": "商店货架",
        "unit": "件商品",
        "help": [
            "改完保存即刻生效，不用重启服务端。",
            "这一页就是**货架**：列在这儿的才有可能被卖，「上架」关掉的收着不摆。"
            "要卖新东西按下面的「＋ 添加一条」挑。",
            "价格是金币（原版的「픽셀」，中文版译作金币）。天数 0 = 永久。",
            "★ 只能选客户端认识、且能进背包的物品 —— 别的发下去界面上是个空格子。",
            "★ **中文名、等级、角色限定都在「物品库」里改**，这一页没有那三栏"
            "（D31）。卡片上的名字是物品库里那一份，点不动。",
            "★ 商店和合成**二选一**：一件东西在这里上架，就会自动从合成里下架。",
        ],
        "fields": [
            {"key": "id", "label": "物品", "type": "item"},
            {"key": "kind", "label": "类别", "type": "text", "readonly": True,
             "help": "由物品本身决定，改不了"},
            {"key": "price", "label": "价格", "type": "int", "min": 0,
             "suffix": "金币"},
            {"key": "days", "label": "天数", "type": "int", "min": 0,
             "help": "0 = 永久"},
            {"key": "listed", "label": "上架", "type": "bool"},
        ],
    },
    "recipe": {
        "list_key": "recipes",
        "title": "合成配方",
        "unit": "条配方",
        "help": [
            "改完保存即刻生效，不用重启服务端。",
            "★ 一条配方最多 4 种材料 —— 原版合成界面只有 4 个材料槽，"
            "第 5 种玩家根本看不见，所以这里也只画 4 格。",
            "★ 原版没有合成成功率（界面上没有任何概率控件），别加。",
            "★ 一个产物只能有一条配方 —— 合成请求只带产物 id，两条同产物服务端分不清。",
            "★ 合成界面的标签是「新商品 / 道具 / 装备 / 称号 / 活动」五个大类"
            "（装备下面还有 头 / 上衣 / 下装 / 手套 / 鞋，道具下面有 装饰 / 其他）"
            "—— **没有武器**，武器产物只能在「新商品」里找得到。",
            "★ 配方**没有等级要求** —— 原版的合成面板不看等级，"
            "上架的配方所有人都看得到、也合得出来。"
            "产物的中文名、穿上时的等级门槛和角色限定都在「物品库」那一页改"
            "（D31）；卡片上的名字是物品库里那一份，点不动。",
            "★ 商店和合成**二选一**：一件东西在这里上架，就会自动从商店里下架。",
            "原版配方随 2009 年停服的服务端一起没了，这一份是复活工程自己设计的，随便改。",
        ],
        "fields": [
            {"key": "id", "label": "配方号", "type": "int", "min": 1,
             "readonly": True, "help": "自动编号"},
            {"key": "result", "label": "产物", "type": "item"},
            {"key": "materials", "label": "材料", "type": "materials",
             "max": MAX_MATERIALS, "fields": [
                 {"key": "id", "label": "材料", "type": "item",
                  "kinds": ["material"]},
                 {"key": "count", "label": "数量", "type": "int",
                  "min": 1, "max": 800},
             ]},
            {"key": "cost", "label": "花费", "type": "int", "min": 0,
             "suffix": "金币"},
            {"key": "days", "label": "天数", "type": "int", "min": 0,
             "help": "0 = 永久"},
            {"key": "listed", "label": "上架", "type": "bool",
             "help": "关掉 = 合成界面里看不到这条"},
        ],
    },
    "drops": {
        "list_key": "rules",
        "title": "材料掉落",
        "unit": "条规则",
        "help": [
            "改完保存即刻生效，不用重启服务端。",
            "关卡和难度留空 = 不限。概率是百分比，100 = 必掉。",
            "前几条是原版基线（Promotion.ini 里那 4 关），后面是复活工程加的扩展。",
            "★ 配方用到的每一种材料都得掉得出来 —— 漏一种，那条产线就永远合不出来。",
        ],
        "fields": [
            {"key": "mode", "label": "模式", "type": "choice",
             "options": [{"value": "quest", "label": "闯关"},
                         {"value": "pvp", "label": "对战"}]},
            # ★ 下拉里只有客户端认得的那七关（`QUEST_ZH`），但**校验器不设上限**
            #   —— 关卡有几个是客户端的事，不该由掉落表来立规矩。手改进来的
            #   别的号码，管理页会原样多加一项显示出来，不会被吃掉。
            {"key": "stage", "label": "关卡", "type": "choice",
             "optional": True, "empty_label": "不限",
             "options": [{"value": qid, "label": "%d · %s" % (qid, name)}
                         for qid, name in sorted(QUEST_ZH.items())]},
            # ★ 难度显示成「1 · 简单」这种游戏里的叫法（`DIFFICULTY_ZH`），
            #   和上面的关卡下拉框一个格式。校验器那边照旧卡 1..4 —— 难度
            #   有几档是客户端拼地图文件名时定死的，不是运营配置说了算。
            {"key": "difficulty", "label": "难度", "type": "choice",
             "optional": True, "empty_label": "不限",
             "options": [{"value": n, "label": "%d · %s" % (n, name)}
                         for n, name in sorted(DIFFICULTY_ZH.items())]},
            {"key": "material", "label": "材料", "type": "item",
             "kinds": ["material"]},
            {"key": "count", "label": "数量", "type": "int", "min": 1,
             "max": 800},
            {"key": "prob", "label": "概率", "type": "int", "min": 0,
             "max": 100, "suffix": "%"},
            {"key": "cleared_only", "label": "只有通关才给", "type": "bool"},
            {"key": "note", "label": "备注", "type": "text", "optional": True,
             "help": "只给人看，服务端不读它"},
        ],
    },
}


def schema_keys(which):
    """某份配置里**一条记录**认得的全部键。给一致性用例和前台用。"""
    return set(field["key"] for field in SCHEMA[which]["fields"])


# --------------------------------------------------------------------------
# 读盘（带热重载）
# --------------------------------------------------------------------------

_SPECS = {
    # ★ 物品库排最前面 —— 另外两份都要问它「这件东西几级 / 谁能穿」（D31）。
    ITEMS_FILENAME: (validate_items, default_items, {}),
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


def items(data_dir=None, _reload=False):
    """物品库 `{itemId: 条目}` —— 等级门槛 + 角色限定（D31）。"""
    return _load(ITEMS_FILENAME, data_dir, _reload)


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
    """四份配置不存在就生成，**已存在一律不覆盖**（D7）。返回新建了哪几个。

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


# --------------------------------------------------------------------------
# 补齐（只增不改）
# --------------------------------------------------------------------------

#: 哪两份配置补得了，以及「一条记录的身份」是哪个键。
#:
#: ★ `drops.json` 不在里面：一条掉落规则没有天然主键（同一种材料可以有
#:   好几条不同关卡 / 难度的规则），「有没有」判不出来，补齐只会补出重复。
BACKFILL_KEYS = {
    #: ★ 物品库尤其要它：物品表里加进来的东西、以前漏收的类别，
    #:   都只能靠这里补 —— 「物品库列不全」正是用户 2026-09-06 报的问题。
    ITEMS_FILENAME: ("items", "id"),
    SHOP_FILENAME: ("items", "id"),
    #: ★ 配方按**产物**认身份，不是配方号 —— 一个产物只能有一条配方
    #:   （`0x0606` 只带产物 id，FINDINGS §27），配方号只是行号。
    RECIPE_FILENAME: ("recipes", "result"),
}


def backfill_defaults(data_dir=None, apply=False):
    """把默认表里有、现有文件里**没有**的条目补进去。返回 `{文件名: [新条目]}`。

    ★ **只增不改**：已经在文件里的条目一个字节都不动（用户改过的价格 /
    花费 / 上架开关全部原样留着，铁律 11）。**幂等** —— 补完再跑一次是空的。

    ★ 为什么需要它：`ensure_files` 只在文件**不存在**时生成一份（D7），
    所以「我们后来发现少收了一批物品」这种事没有别的出口。
    2026-09-05 就撞上一次：`PART_SUFFIX_ZH` 漏了 `바지` / `신발`，
    泰尔的四条下装和四双鞋从来没进过默认配方。

    `apply=False`（默认）只算不写 —— 先看清楚要加什么再决定。
    真写的时候先把原文件复制一份 `*.bak-<时刻>` 放在旁边。
    """
    added = {}
    for filename, (list_key, id_key) in BACKFILL_KEYS.items():
        path = path_of(filename, data_dir)
        if not os.path.exists(path):
            continue                    # 没有就该由 `ensure_files` 去生成
        try:
            with open(path, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
        except (OSError, ValueError):
            # 读不了就跳过。**绝不拿默认值盖掉一份读不懂的文件**（D10）。
            continue
        entries = raw.get(list_key)
        if not isinstance(entries, list):
            continue
        have = set()
        for entry in entries:
            if isinstance(entry, dict) and entry.get(id_key) is not None:
                have.add(int(entry[id_key]))
        build = _SPECS[filename][1]
        fresh = [entry for entry in build().get(list_key, [])
                 if int(entry[id_key]) not in have]
        if not fresh:
            continue
        if filename == RECIPE_FILENAME:
            # 配方号接着现有的往下数，别和已有的撞（撞了整份文件都不合法）。
            top = 0
            for entry in entries:
                try:
                    top = max(top, int(entry.get("id", 0)))
                except (TypeError, ValueError):
                    pass
            for offset, entry in enumerate(fresh, start=1):
                entry["id"] = top + offset
        added[filename] = fresh
        if not apply:
            continue
        merged = dict(raw)
        merged[list_key] = list(entries) + fresh
        # 存盘前必过校验：宁可什么都不写，也不要写出一份服务端读不了的文件。
        _SPECS[filename][0](merged)
        shutil.copyfile(path, "%s.bak-%s"
                        % (path, time.strftime("%Y%m%d-%H%M%S")))
        write_json(path, merged)
    if apply and added:
        invalidate(data_dir)
    return added
