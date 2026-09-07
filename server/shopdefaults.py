#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shopdefaults.py —— 四份运营配置的**默认内容**（模板），从原版物品表程序化生成。

`shopcfg.default_items()` / `default_shop()` / `default_recipes()` / `default_drops()`
都是这里 `build_all()` 的切片；第一次开服 `ensure_files()` 写出来的就是它，
所以**新下载发布包的人拿到的默认数据 = 这张设计表**（用户 2026-09-07 拍板，D50）。
`tools/gen_listing.py` 是它的命令行壳：试算、自洽检查、把现有 `server/data/` 重新生成。

设计口径（写在这里是为了让模块自己就是说明书）：

* 商店卖「散件」：D/R/F + 特别版武器、没有名字的散装铠甲（#id / 模型编号）、
  装饰件（头饰 / 尾饰 / 翅膀 / 礼物）、染色剂、突击技、纯外观套装、强攻套装。
* 合成出「成套的、有名字的」东西：四条原有产线 + 金 / 银骑士甲 + 三个角色
  各自的三套（尾号 61 / 62 / 63）、戒指、宠物。
* ★ 材料掉落照**原版口径**（用户 2026-09-07 拍板，D49）：
  - **每一关有自己的特殊材料**，只在这一关掉：普通掉低档（`4xxxx` / 不死鸟之羽）、
    困难掉高档（`5xxxx` / 不死鸟之泪），各 100%；**困难不掉普通那一档**；
  - **简单只掉通用材料**（一样矿料 + 一色珠子，各 100%）；
  - 普通 / 困难 = 特殊材料 + 一样通用材料 ⇒ **一局就掉两种**；
  - **每关一种独占的通用材料**，三档都一样（青铜管 = 机械青蛙、浮游石 = 鲸鱼战舰、
    橡皮管 = 秘密基地 …）；原版 `Promotion.ini` 的 4 条基线保留 3 条（第 1 关
    青铜管 ×2、第 4 关浮游石），第 7 关简单那条青铜管让位给橡皮管；
  - 对战：赢了四色珠子各 30%、六种独占通用材料各 15%，输了只有绿珠 30%；
    特殊材料不进对战。
* ★ 配方照**风格配对**：同风格的装备用同风格的材料（黄金 / 白银 ← 龙、
  凤凰 ← 不死鸟、军官服 ← 士兵军服 …），**一条配方只用一个难度档的特殊材料**
  （低档套装用普通档、高档套装用困难档），矿料和珠子从同一关拿。
* ★ 称号和卡片**不上架**（用户 2026-09-06 第二轮）：卡片原版是按成就给的
  （击杀数 / 误伤次数…），本项目还没有成就统计，按概率掉不合理。
  等有了成就数据再把称号配方加回来。
* ★ 难度只有 1 / 2 / 3（中国区客户端选不到第 4 档「极限」，V0.3商店 §38）。
  `check_tiering` / `check_recipes` / `problems()` 守着上面这几条。

## 和 `shopcfg` 的关系（别绕成环）

本模块顶层 `import shopcfg`（要它的 `NAME_ZH` 翻译、`WEAPON_PRICE` 这些常量）；
`shopcfg` 只在 `default_*()` **被调用时**才 `import shopdefaults`。加载阶段两边
互不依赖，谁先被 import 都行。

## 为什么不缓存

`build_all()` 每次都从头算：测试会把 `shopdata.STORE` 换成一张小表，缓存会把
上一张表的结果串过去。整张表 808 件也就几十毫秒，`ensure_files` 一辈子只跑一次。

铁律：只用标准库，CPython 3.8（Win7 运行时）也要能跑。
"""
import collections

import shopcfg
import shopdata

FORMAT = shopcfg.FORMAT

# ---------------------------------------------------------------------------
# 中文名（`shopcfg.NAME_ZH` 翻不到 / 翻得不对的那些，按这里的覆盖）
# ---------------------------------------------------------------------------
#: 武器基础名 —— 2007 新浪官方资料页「称号系统」里的卡片名字就是武器名
#: （左轮手枪 / 苹果弹 / 狙击枪 / 复古短枪 / 火焰弹 / 华尔兹加农炮 / 重机枪 /
#: 榴弹发射器 / 火箭炮），照抄（D45）。
WEAPON_BASE_ZH = {
    "리볼버": "左轮手枪", "사과탄": "苹果弹", "T1": "狙击枪T1", "T2": "狙击枪T2",
    "카멜나이트": "复古短枪", "화염탄": "火焰弹", "캐논왈츠": "华尔兹加农炮",
    "머신건": "重机枪", "스크류런처": "榴弹发射器", "바주카": "火箭炮",
}
SPECIAL_WEAPON_ZH = {
    "리볼버 SE": "左轮手枪 SE", "사과탄 SE": "苹果弹 SE", "화염탄 SE": "火焰弹 SE",
    "카멜나이트 SE": "复古短枪 SE", "머신건 SE": "重机枪 SE",
    "스크류런처 SE": "榴弹发射器 SE", "T1 클래식": "狙击枪T1 经典版",
    "T2 플래티넘": "狙击枪T2 白金版", "캐논왈츠 클래식": "华尔兹加农炮 经典版",
    "바주카 클래식": "火箭炮 经典版", "캐논왈츠2": "华尔兹加农炮2",
    "디스트로이어": "毁灭者", "T2": "狙击枪T2",
}
#: 按 id 定的名字（同名同图的两套材料只能按 id 分；称号卡片照新浪页，§37）。
NAME_BY_ID = {
    102400001: "京",
    30005: "橡皮管", 30006: "熔岩碎片",
    30013: "制作书 (初级)", 30014: "制作书 (中级)", 30015: "制作书 (高级)",
    40008: "Z芯片", 50008: "Z晶片",
    40009: "龙之血", 50009: "龙之精华",
    40010: "破碎的衣服", 50010: "破碎的军服",
    40011: "螺旋桨", 50011: "合金螺旋桨",
    40012: "破碎的头盔", 50012: "黑骑士的头盔",
    40015: "太阳碎片", 50015: "太阳结晶",
    60001: "完美卡片", 60002: "格斗卡片", 60003: "射手卡片", 60004: "幸运卡片",
    60005: "厄运卡片", 60006: "红心卡片", 60007: "乌龙卡片", 60008: "信心卡片",
    110001: "左轮手枪卡片", 110002: "苹果弹卡片", 110003: "狙击枪卡片",
    120001: "复古短枪卡片", 120002: "火焰弹卡片", 120003: "华尔兹加农炮卡片",
    130001: "重机枪卡片", 130002: "榴弹发射器卡片", 130003: "火箭炮卡片",
    610001: "[左轮手枪高手]", 610002: "[苹果弹高手]", 610003: "[狙击枪高手]",
    620001: "[复古短枪高手]", 620002: "[火焰弹高手]", 620003: "[华尔兹加农炮高手]",
    630001: "[重机枪高手]", 630002: "[榴弹发射器高手]", 630003: "[火箭炮高手]",
}
#: 原版没给名字、但要摆上货架当「一件东西」卖的整套装备：`#1010049` 这种名字
#: 在列表里没法看，按「角色 + 描述」起个能认的名字（复活工程自己起的）。
SET_NAME_ZH = {
    49: "强攻套装 I", 50: "强攻套装 II", 51: "强攻套装 III", 52: "强攻套装 IV",   # 攻击 +30%
    22: "羽翼套装",                                                             # 带翅膀的外观全套
    59: "休闲套装",                                                             # 三件外观套
}
for _ch, _zh in shopcfg.CHARACTER_ZH.items():
    for _tail, _name in SET_NAME_ZH.items():
        NAME_BY_ID[(_ch + 1) * 1000000 + 10000 + _tail] = "%s %s" % (_zh, _name)

#: 金 / 银骑士甲：图标 `chNN?0015` = 金色、`chNN?0016` = 银色（看图确认过），
#: 每个角色三件：上衣 + 头盔 + 鞋（布洛克没有鞋，是手套）。
#: id 尾号 27/28、38/39 那两组是同图同属性的复制品，去重后只留 15/16。
KNIGHT_ZH = {"0015": "黄金铠甲", "0016": "白银铠甲"}
PART_ZH = {1: "上衣", 2: "下装", 4: "头盔", 8: "鞋", 16: "手套"}


def knight_color(item):
    """金 / 银骑士甲 → `"0015"` / `"0016"`；别的 → `None`。"""
    icon = item.icon or ""
    if (item.kind == "armor" and item.part_flag in PART_ZH and not item.name_kr
            and icon[:2].lower() == "ch" and icon[-4:] in KNIGHT_ZH):
        return icon[-4:]
    return None


def knight_name(item):
    return "%s %s·%s" % (shopcfg.CHARACTER_ZH.get(item.character, ""),
                         KNIGHT_ZH[knight_color(item)], PART_ZH[item.part_flag])


def weapon_name(item):
    name = (item.name_kr or "").strip()
    if item.series and item.tier:
        suffix = " %s%d" % (item.series, item.tier)
        base = name[:-len(suffix)] if name.endswith(suffix) else name
        zh = WEAPON_BASE_ZH.get(base.strip())
        if zh:
            return "%s %s%d" % (zh, shopcfg.SERIES_ZH.get(item.series, item.series),
                                item.tier)
    return SPECIAL_WEAPON_ZH.get(name) or shopcfg.item_name_zh(item)


def name_of(item):
    """一件东西的默认中文名：按 id 的覆盖表 → 武器 → 金银骑士甲 → `shopcfg.item_name_zh`。"""
    if item.id in NAME_BY_ID:
        return NAME_BY_ID[item.id]
    if item.kind == "weapon":
        return weapon_name(item)
    if knight_color(item):
        return knight_name(item)
    return shopcfg.item_name_zh(item)


def zh(item_id):
    """给检查报告用的名字；表里没有的 id 就写号码。"""
    item = shopdata.get(item_id)
    return name_of(item) if item is not None else str(item_id)


def dedupe(ownable):
    """同类别 / 同角色 / 同槽位 / 同图标 / 同加成 / 同武器数值 = 同一件东西，
    只留 id 最小的（用户 2026-09-06：`CH01B0023` 上架了两件一模一样的）。
    材料不去重：`4xxxx` / `5xxxx` 同名同图是普通 / 困难两档，故意的。
    返回 `(留下的, 去掉的, {去掉的 id: 留下的那件的 id})`，前两个都按 id 升序。"""
    seen = {}
    kept, dropped, twin_of = [], [], {}
    for item in sorted(ownable, key=lambda it: it.id):
        if item.kind == "material":
            kept.append(item)
            continue
        key = (item.kind, item.character, item.part_flag, item.icon,
               tuple(sorted((k, v) for k, v in (item.bonus or {}).items())),
               tuple(sorted((k, v) for k, v in (item.weapon or {}).items())))
        if key in seen:
            dropped.append(item)
            twin_of[item.id] = seen[key]
            continue
        seen[key] = item.id
        kept.append(item)
    return kept, dropped, twin_of


# ---------------------------------------------------------------------------
# 分类：每件东西走商店还是合成
# ---------------------------------------------------------------------------
def weight(item):
    """一件装备「有多强」= 加成绝对值之和；拿它缩放价格 / 材料数量 / 花费。
    ★ 用绝对值：「攻 -2 防 +9」那种负的那格也是设计的一部分，当 0 会算得太便宜。"""
    return sum(abs(int(v)) for v in (item.bonus or {}).values())


def part_single(item):
    return item.part_flag in (1, 2, 4, 8, 16)


def is_named_set_piece(item):
    """四条老产线 + 四条新产线：id 尾两位 60~67 的成套铠甲（都有韩文名）。"""
    return (item.kind == "armor" and part_single(item)
            and 60 <= item.id % 100 <= 67 and bool(item.name_kr))


def is_attack_set(item):
    return item.kind == "armor" and item.id % 100 in (49, 50, 51, 52) \
        and item.part_flag in (31, 23)


def is_cosmetic_set(item):
    return item.kind == "armor" and item.part_flag in (539, 11, 9, 19)


# ---------------------------------------------------------------------------
# 商店：价格 / 等级
# ---------------------------------------------------------------------------
WEAPON_PRICE = shopcfg.WEAPON_PRICE
WEAPON_LEVEL = shopcfg.WEAPON_LEVEL
#: 特别版武器：(价格, 等级)。数值都是散件级别（SE = D1 的皮），按同档定价。
SPECIAL_WEAPON = {
    1120003: (3000, 1), 1120002: (3000, 1), 2120002: (3000, 1),
    2120003: (3000, 1), 3120002: (3000, 1), 3120003: (3000, 1),   # SE ×6
    1120004: (8000, 10),    # 狙击枪T1 经典版 ≈ F 系 2 级
    2120004: (8000, 10),    # 华尔兹加农炮 经典版 ≈ R2
    3120004: (8000, 10),    # 火箭炮 经典版：装填 995 的快炮
    1120001: (12000, 14),   # 狙击枪T2：装填 2200，比任何 T1 都快
    2120001: (12000, 14),   # 华尔兹加农炮2：装填 1400，最快的加农炮
    1120101: (18000, 18),   # 狙击枪T2 白金版：24/39/2200，最强狙
    3120001: (18000, 18),   # 毁灭者：25/22/25/150，最强火箭炮
}
PART_BASE = {1: 500, 2: 400, 4: 400, 8: 300, 16: 300}   # 散装铠甲的部位底价
GOLD_PER_POINT = 450                                     # 散装铠甲每点加成的金币
DECOR_BASE = 800          # 头饰 / 尾饰 / 翅膀
GIFT_PRICE = 500          # 선물 礼物（一件没有加成的上衣）
COSMETIC_SET_PRICE = {539: 6000, 11: 2000, 9: 2000, 19: 2000}
SPRAY_PRICE = 1500
#: 突击技按 id 尾号：(价格, 等级)
DASH_PRICE = {2: (2000, 1), 3: (3000, 4), 4: (4500, 7), 5: (3500, 5), 6: (6000, 10)}
#: 强攻套装（+30% 攻击的全身套）：**商店卖**，放「装备 → 套装」（用户 2026-09-06：
#: 合成面板没有套装标签，所有套装改成商店上架）。纯金币价所以定得高。
ATTACK_SET_LEVEL, ATTACK_SET_PRICE = 24, 30000


def shop_level(w):
    """散装铠甲的穿上等级 = 1 + 1.2 × 加成总点数，封顶 18。"""
    if w <= 1:
        return 1
    return min(18, int(round(1 + w * 1.2)))


def default_level(item):
    """没上架也不合成的东西：武器按档、其余不限（和以前的模板一样）。"""
    if item.kind == "weapon" and item.series:
        return WEAPON_LEVEL.get(item.tier or 1, 5)
    return 1


# ---------------------------------------------------------------------------
# 合成：产线
# ---------------------------------------------------------------------------
#: ★ 配对口径（用户 2026-09-07，D49）：同风格的装备用同风格的材料；一条配方 =
#: **一种特殊材料**（只用一个难度档）+ **同一关的通用矿料** + **同一关简单难度
#: 掉的那色珠子**。这样一套装备的材料在一关里就打得齐：
#: 普通 / 困难打特殊材料 + 矿料，简单打珠子。
#:
#: 每点加成的个数：特殊 0.35 / 矿料 0.35 / 珠子 0.5 ——
#: 一件加成 9 点的上衣 = 3 + 3 + 5，即 3 局普通（或困难）+ 5 局简单；
#: 一套五件（加成合计 ≈ 28）≈ 10 局特殊难度 + 14 局简单。
#: 觉得快了慢了改这里的系数，或直接在管理页改 `recipe.json`。
#:
#: 尾号 → (穿上等级, 每点加成的金币, ((材料, 每点加成的个数), …))
SET_LINES = {
    # 四条原有产线
    67: (10, 260, ((40008, .35), (30018, .35), (10003, .5))),   # 佣兵铠甲（机械感的重甲）← 机械青蛙·普通：Z芯片 + 青铜管 + 绿珠
    64: (14, 320, ((50015, .35), (30015, .2), (10004, .5))),    # 大师铠甲 ← 太阳齿轮·困难：太阳结晶 + 制作书 (高级) + 蓝珠
    65: (18, 420, ((30016, .35), (30005, .35), (10001, .5))),   # 初阶凤凰铠甲 ← 秘密基地·普通：不死鸟之羽 + 橡皮管 + 黑珠
    66: (24, 560, ((30017, .35), (30005, .35), (10001, .5))),   # 高阶凤凰铠甲 ← 秘密基地·困难：不死鸟之泪 + 橡皮管 + 黑珠
    # 三个角色共用的
    60: (7, 200, ((40010, .35), (20007, .35), (10003, .5))),    # 加密尔军官服 ← 神秘岛·普通：士兵的破碎的衣服 + 铁矿石 + 绿珠
}
#: 尾号 61 / 62 / 63 三个角色**各不相同**（名字和外形都不一样），按 (角色, 尾号) 配。
#: 配对依据是名字和图标（`Images/Shop/<韩文名> 셋트.png` 逐张看过，§42）：
CHAR_SET_LINES = {
    # 泰尔
    (0, 61): (9, 240, ((40012, .35), (20007, .35), (10002, .5))),   # 舞动皇冠（王冠）← 黑骑士·普通：破碎的头盔 + 铁矿石 + 红珠
    (0, 62): (12, 300, ((50010, .35), (20007, .35), (10003, .5))),  # 收割锁链（灰绿军装 + 锁链）← 神秘岛·困难：破碎的军服 + 铁矿石 + 绿珠
    (0, 63): (16, 380, ((50012, .35), (20007, .35), (10002, .5))),  # 最后通牒（暗紫兜帽甲）← 黑骑士·困难：黑骑士的头盔 + 铁矿石 + 红珠
    # 卡希尔
    (1, 61): (9, 240, ((40015, .35), (30014, .2), (10004, .5))),    # 圣洁圣徒（金色圣袍）← 太阳齿轮·普通：太阳碎片 + 制作书 (中级) + 蓝珠
    (1, 62): (12, 300, ((50011, .35), (30019, .2), (10004, .5))),   # 蝴蝶（会飞的）← 鲸鱼战舰·困难：合金螺旋桨 + 浮游石 + 蓝珠
    (1, 63): (16, 380, ((50012, .35), (20007, .35), (10002, .5))),  # 掘墓人（黑色哥特裙）← 黑骑士·困难
    # 布洛克
    (2, 61): (9, 240, ((40015, .35), (30014, .2), (10004, .5))),    # 乱流（金色齿轮纹重甲）← 太阳齿轮·普通
    (2, 62): (12, 300, ((50008, .35), (30018, .35), (10003, .5))),  # 闪光渴求者（绿色机械重甲）← 机械青蛙·困难：Z晶片 + 青铜管 + 绿珠
    (2, 63): (16, 380, ((50012, .35), (20007, .35), (10002, .5))),  # 群魔殿（带角的骑士甲）← 黑骑士·困难
}


def set_line(item):
    """这件套装铠甲走哪条产线：61 / 62 / 63 按 (角色, 尾号)，其余按尾号；没有就 `None`。"""
    tail = item.id % 100
    return CHAR_SET_LINES.get((item.character, tail)) or SET_LINES.get(tail)


#: 金 / 银骑士甲产线（用户 2026-09-07 明说的）：银 ← 龙之血 + 熔岩碎片 + 红珠，
#: 金 ← 龙之精华 + **更多**熔岩碎片 + 红珠。形状和 `SET_LINES` 一样。
KNIGHT_LINES = {
    "0016": (8, 220, ((40009, .35), (30006, .35), (10002, .5))),    # 白银铠甲 ← 岩浆巨龙·普通
    "0015": (12, 300, ((50009, .35), (30006, .5), (10002, .5))),    # 黄金铠甲 ← 岩浆巨龙·困难
}
RING = {   # 尾号 → (等级, 花费, 材料)
    59: (6, 800, ((10003, 4), (10004, 3), (20007, 2))),      # 小胜利之戒：只要通用材料
    60: (11, 2000, ((40011, 2), (30019, 3), (10004, 3))),    # 祝福银戒 ← 鲸鱼战舰·普通：螺旋桨 + 浮游石 + 蓝珠
}
PET = {    # id → (等级, 花费, 材料)
    220007: (1, 1500, ((10003, 5), (10004, 3))),                # 熊猫（纯外观）：只要珠子
    220001: (6, 3000, ((40011, 2), (30019, 3), (10004, 4))),    # 青鸟 ← 鲸鱼战舰·普通：螺旋桨 + 浮游石 + 蓝珠
    220006: (8, 4000, ((40011, 3), (30019, 3), (10004, 3))),    # 防护装置·粉 ← 鲸鱼战舰·普通：螺旋桨 + 浮游石 + 蓝珠
    220004: (10, 5000, ((40008, 4), (30018, 3), (10003, 3))),   # 迷你机械青蛙 ← 机械青蛙·普通：Z芯片 + 青铜管 + 绿珠
    220003: (10, 5000, ((40009, 4), (30006, 3), (10002, 3))),   # 火焰蝙蝠 ← 岩浆巨龙·普通：龙之血 + 熔岩碎片 + 红珠
    220002: (12, 6000, ((50011, 3), (30019, 2), (10004, 4))),   # 防护装置 ← 鲸鱼战舰·困难：合金螺旋桨 + 浮游石 + 蓝珠
    220005: (12, 5000, ((40015, 3), (30013, 2), (10004, 3))),   # 萤火虫 ← 太阳齿轮：太阳碎片（普通）+ 制作书 (初级)（简单）+ 蓝珠
}
#: 称号：**暂不上架**（见文件头）。原版的对应关系留在这儿，等成就统计做好了
#: 再接回去 —— 卡片 60001 ~ 60008 对 560001 ~ 560008，武器卡片 110001 等对
#: 610001 等（卡片 id = weapon.ini 的 ROH，§37）。
TITLE = {}


def scaled(mats, w):
    return [{"id": mid, "count": max(1, int(round(w * ratio)))} for mid, ratio in mats]


def fixed(mats):
    return [{"id": mid, "count": int(n)} for mid, n in mats]


# ---------------------------------------------------------------------------
# 掉落 —— 原版口径（用户 2026-09-07 拍板，D49）
# ---------------------------------------------------------------------------
#: 关卡 → (普通掉的低档特殊材料, 困难掉的高档特殊材料, 关卡名)。
#: 1 ~ 6 关是 2007 新浪「合成系统」页写明的 boss 材料（`4xxxx` 普通 / `5xxxx`
#: 困难，同名同图两套，§37）。★ 第 7 关原版没写：不死鸟之羽 / 之泪没有对应的
#: boss（第 7 关的 boss 还是机械青蛙，§42），「最后一关掉最高档的材料、配等级最高的
#: 凤凰两套」是我们定的。
SPECIAL = {
    1: (40008, 50008, "机械青蛙"), 2: (40009, 50009, "岩浆巨龙"),
    3: (40010, 50010, "神秘岛"), 4: (40011, 50011, "鲸鱼战舰"),
    5: (40012, 50012, "黑骑士"), 6: (40015, 50015, "太阳齿轮"),
    7: (30016, 30017, "扎米洛秘密基地"),
}
SPECIAL_STAGE = {mat: stage for stage, (low, high, _) in SPECIAL.items()
                 for mat in (low, high)}
LOW_TIER = {low for low, _, _ in SPECIAL.values()}
HIGH_TIER = {high for _, high, _ in SPECIAL.values()}
#: 关卡 → {难度: 通用矿料}。每一档一样、100%。★ 标的三条是原版 `Promotion.ini`
#: 的基线（§12）。★ 用户 2026-09-07 第二轮：**每关一种独占的通用材料**，三档都一样 ——
#: 青铜管归机械青蛙、浮游石归鲸鱼战舰、橡皮管归秘密基地（原版基线 0114 里第 7 关
#: 简单给的青铜管因此让位）。铁矿石是神秘岛和黑骑士共用的。
GENERIC = {
    1: {1: 30018, 2: 30018, 3: 30018},   # 青铜管（普通 ★0118 / 困难 ★0125；简单是我们补齐的）—— 机械青蛙独占
    2: {1: 30006, 2: 30006, 3: 30006},   # 熔岩碎片 —— 岩浆巨龙独占
    3: {1: 20007, 2: 20007, 3: 20007},   # 铁矿石
    4: {1: 30019, 2: 30019, 3: 30019},   # 浮游石（困难 ★0123）—— 鲸鱼战舰独占
    5: {1: 20007, 2: 20007, 3: 20007},   # 铁矿石
    6: {1: 30013, 2: 30014, 3: 30015},   # 制作书 初 / 中 / 高 —— 太阳齿轮独占
    7: {1: 30005, 2: 30005, 3: 30005},   # 橡皮管 —— 秘密基地独占
}
BASELINE = {(1, 2): "0118", (1, 3): "0125", (4, 3): "0123"}
#: 简单难度的第二样：珠子，按关卡分色（100%）。每色至少两关有，黑珠只在最后一关。
EASY_BEAD = {1: 10003, 2: 10002, 3: 10003, 4: 10004, 5: 10002, 6: 10004, 7: 10001}
#: 对战也给珠子（新浪页：珠子「可在对战和任务模式中获得」）。原版 PVP 任务给的是
#: 羽翼套装散件（§42），珠子的概率是我们定的：(材料, 概率, 只有赢了才给)。
PVP_BEADS = ((10003, 30, False), (10004, 30, True), (10002, 30, True), (10001, 30, True))
#: ★ 对战也掉各关的独占通用材料（用户 2026-09-07 第三轮）：赢了每样 15%。
#: 制作书只给初级 —— 中 / 高级是太阳齿轮普通 / 困难的档位，对战没有难度。
#: 特殊材料**不**进对战（`check_tiering` 守着）。
PVP_GENERICS = ((30018, 15), (30006, 15), (20007, 15), (30019, 15), (30013, 15), (30005, 15))

#: 玩家选得到的难度：1 简单 / 2 普通 / 3 困难。**没有第 4 档**（§38）。
DIFFICULTIES = (1, 2, 3)


def rule(material, prob, mode="quest", stage=None, difficulty=None, count=1,
         cleared_only=True, note=""):
    r = {"mode": mode}
    if stage is not None:
        r["stage"] = stage
    if difficulty is not None:
        r["difficulty"] = difficulty
    r.update({"material": material, "count": count, "prob": prob,
              "cleared_only": cleared_only, "note": note})
    return r


def build_drops():
    """每一关每一档正好两样、全 100%：简单 = 矿料 + 珠子；普通 / 困难 = 特殊材料 + 矿料。

    ★ 表里没有的材料（测试用的小表）整条跳过，别生成一条校验不过的规则。
    """
    rules = []
    for stage in sorted(SPECIAL):
        low, high, name = SPECIAL[stage]
        for diff in DIFFICULTIES:
            tag = BASELINE.get((stage, diff))
            note = ("原版基线（Promotion.ini %s）" % tag if tag
                    else "%s·%s：通用材料" % (name, shopcfg.DIFFICULTY_ZH[diff]))
            rules.append(rule(GENERIC[stage][diff], 100, stage=stage, difficulty=diff, note=note))
        rules.append(rule(EASY_BEAD[stage], 100, stage=stage, difficulty=1, note="%s·简单：珠子" % name))
        rules.append(rule(low, 100, stage=stage, difficulty=2, note="%s·普通：特殊材料（低档）" % name))
        rules.append(rule(high, 100, stage=stage, difficulty=3, note="%s·困难：特殊材料（高档）" % name))
    for mat, prob, win_only in PVP_BEADS:
        rules.append(rule(mat, prob, mode="pvp", cleared_only=win_only,
                          note="珠子：对战获胜" if win_only else "珠子：对战参战就有机会"))
    for mat, prob in PVP_GENERICS:
        rules.append(rule(mat, prob, mode="pvp", note="各关的独占通用材料：对战获胜"))
    rules = [r for r in rules if shopdata.is_material(r["material"])]
    # 文件里就按 模式 → 关卡 → 难度 → 材料 排好（管理页显示时也这么排），
    # 闯关排在对战前面。
    rules.sort(key=lambda r: (0 if r["mode"] == "quest" else 1,
                              r.get("stage") or 0, r.get("difficulty") or 0,
                              r["material"]))
    return {"format": FORMAT, "rules": rules}


def expected_yield(rules, material, stage, difficulty, mode="quest"):
    """通关一局 `(关卡, 难度)` 这种材料的期望个数（`quest_materials` 的规则口径）。"""
    total = 0.0
    for r in rules:
        if r.get("mode", "quest") != mode or r["material"] != material:
            continue
        if r.get("stage") is not None and r["stage"] != stage:
            continue
        if r.get("difficulty") is not None and r["difficulty"] != difficulty:
            continue
        total += r["prob"] / 100.0 * r.get("count", 1)
    return total


def check_tiering(rules):
    """按 D49 的掉落口径逐条核对，返回违反说明的列表（空 = 全对）。"""
    bad = []
    kinds = collections.defaultdict(set)
    for r in rules:
        mat = r["material"]
        if r.get("mode", "quest") != "quest":
            if mat in SPECIAL_STAGE:
                bad.append("特殊材料 %s 不该在对战里掉" % zh(mat))
            continue
        stage, diff = r.get("stage"), r.get("difficulty")
        if stage is None or diff is None:
            bad.append("闯关规则都要写明关卡和难度：%s" % zh(mat))
            continue
        kinds[(stage, diff)].add(mat)
        if mat not in SPECIAL_STAGE:
            continue
        if SPECIAL_STAGE[mat] != stage:
            bad.append("%s 只在第 %d 关掉，却配在了第 %d 关" % (zh(mat), SPECIAL_STAGE[mat], stage))
        if diff == 1:
            bad.append("简单难度不掉特殊材料：%s" % zh(mat))
        elif diff == 2 and mat not in LOW_TIER:
            bad.append("普通难度只掉低档：%s" % zh(mat))
        elif diff == 3 and mat not in HIGH_TIER:
            bad.append("困难难度只掉高档：%s" % zh(mat))
        if r["prob"] != 100:
            bad.append("特殊材料必掉：%s 写了 %d%%" % (zh(mat), r["prob"]))
    for stage in sorted(SPECIAL):
        for diff in DIFFICULTIES:
            n = len(kinds[(stage, diff)])
            if n != 2:
                bad.append("第 %d 关难度 %d 掉 %d 种，口径是两种" % (stage, diff, n))
    return bad


def stages_of(rules, material):
    """这种材料在哪几关掉得出来（只看闯关规则）。"""
    return {r["stage"] for r in rules
            if r.get("mode", "quest") == "quest" and r["material"] == material}


def check_recipes(rules, recipes):
    """配方口径：一条配方不能同时用普通档和困难档的特殊材料；有名字的套装
    （铠甲）的材料必须在同一关打得齐。返回违反说明的列表。"""
    bad = []
    for recipe in recipes:
        mats = [m["id"] for m in recipe["materials"]]
        if any(m in LOW_TIER for m in mats) and any(m in HIGH_TIER for m in mats):
            bad.append("配方 #%d（%s）同时用了普通档和困难档的特殊材料"
                       % (recipe["id"], zh(recipe["result"])))
        if shopdata.kind(recipe["result"]) == "armor":
            common = None
            for m in mats:
                common = stages_of(rules, m) if common is None else common & stages_of(rules, m)
            if not common:
                bad.append("配方 #%d（%s）的材料在一关里凑不齐"
                           % (recipe["id"], zh(recipe["result"])))
    return bad


# ---------------------------------------------------------------------------
# 把四份算出来
# ---------------------------------------------------------------------------
def build_all():
    """算出四份默认配置。返回 `{"items", "shop", "recipes", "drops", "report"}`。

    `report` 给命令行壳打统计用：`skipped`（哪类没上架、几件）、`duplicates`
    （去重去掉的 id）、`where`（id → 商店 / 合成）、`levels`。
    """
    ownable = []
    for kind in shopdata.kinds():
        for item_id in shopdata.ids_of_kind(kind):
            item = shopdata.get(item_id)
            if item is not None and item.ownable:
                ownable.append(item)
    ownable.sort(key=lambda it: it.id)
    everything = list(ownable)                 # 物品库要收全部（真表 808 件）
    ownable, duplicates, twin_of = dedupe(ownable)   # 上架 / 合成只看去重后的

    items_out, shop_out, recipes_out = [], [], []
    levels = {}          # 只给要上架 / 合成的东西定等级
    where = {}           # id → "商店" / "合成"
    skipped = collections.Counter()

    def sell(item, price, level):
        shop_out.append({"id": item.id, "kind": item.kind, "listed": True,
                         "price": int(price)})
        levels[item.id] = int(level)
        where[item.id] = "商店"

    def craft(item, cost, materials, level):
        # 中文版没有的材料整格跳过（测试用的小表会撞上）；一格都不剩就不合成。
        materials = [m for m in materials if shopdata.is_material(m["id"])]
        if not materials:
            skipped["%s:no-material" % item.kind] += 1
            return
        recipes_out.append({"result": item.id, "listed": True,
                            "cost": int(cost), "materials": materials[:shopcfg.MAX_MATERIALS]})
        levels[item.id] = int(level)
        where[item.id] = "合成"

    for item in ownable:
        w = weight(item)
        if item.kind == "weapon":
            if item.series and item.tier and item.character is not None:
                sell(item, WEAPON_PRICE.get(item.tier, WEAPON_PRICE[1]),
                     WEAPON_LEVEL.get(item.tier, WEAPON_LEVEL[1]))
            elif item.id in SPECIAL_WEAPON:
                sell(item, *SPECIAL_WEAPON[item.id])
            else:
                skipped["weapon"] += 1
        elif item.kind == "armor":
            line = set_line(item) if is_named_set_piece(item) else None
            if line:
                level, gold, mats = line
                craft(item, w * gold, scaled(mats, w), level)
            elif is_attack_set(item):
                sell(item, ATTACK_SET_PRICE, ATTACK_SET_LEVEL)
            elif is_cosmetic_set(item):
                sell(item, COSMETIC_SET_PRICE[item.part_flag], 1)
            elif knight_color(item):
                level, gold, mats = KNIGHT_LINES[knight_color(item)]
                craft(item, w * gold, scaled(mats, w), level)
            elif item.part_flag in (128, 256, 512):
                sell(item, DECOR_BASE + w * 400, 1)
            elif (item.name_kr or "").endswith("선물"):
                sell(item, GIFT_PRICE, 1)
            elif part_single(item):
                sell(item, PART_BASE[item.part_flag] + w * GOLD_PER_POINT,
                     shop_level(w))
            else:
                skipped["armor?"] += 1
        elif item.kind == "spray":
            sell(item, SPRAY_PRICE, 1)
        elif item.kind == "dash" and item.id % 10 in DASH_PRICE:
            sell(item, *DASH_PRICE[item.id % 10])
        elif item.kind == "ring" and item.id % 100 in RING:
            level, cost, mats = RING[item.id % 100]
            craft(item, cost, fixed(mats), level)
        elif item.kind == "pet" and item.id in PET:
            level, cost, mats = PET[item.id]
            craft(item, cost, fixed(mats), level)
        elif item.kind == "title" and item.id in TITLE:
            cost, mats = TITLE[item.id]
            craft(item, cost, fixed(mats), 1)
        else:
            skipped[item.kind] += 1

    # 去重去掉的复制品跟它留下的那件同等级：同图同加成的两件东西门槛不该不一样
    # （管理页给玩家塞一件时会用到；它们不上架也不合成）。
    for dup in duplicates:
        if twin_of[dup.id] in levels:
            levels[dup.id] = levels[twin_of[dup.id]]

    # items.json：全部能进背包的东西，名字 + 等级 + 角色限定（D31）
    for item in everything:
        entry = {"id": item.id, "name": name_of(item)}
        if shopcfg.has_level_and_character(item):
            entry["level"] = levels.get(item.id, default_level(item))
            if item.character is not None:
                entry["character"] = int(item.character)
        items_out.append(entry)
    items_out.sort(key=lambda e: e["id"])

    # 配方号 = 按产物 id 排好后的行号（一个产物只能有一条配方，§27）
    recipes_out.sort(key=lambda r: r["result"])
    recipes_out = [{"id": index, "result": r["result"], "listed": True,
                    "cost": r["cost"], "materials": r["materials"]}
                   for index, r in enumerate(recipes_out, start=1)]
    shop_out.sort(key=lambda e: (e["kind"], e["id"]))

    return {
        "items": {"format": FORMAT, "items": items_out},
        "shop": {"format": FORMAT, "items": shop_out},
        "recipes": {"format": FORMAT, "recipes": recipes_out},
        "drops": build_drops(),
        "report": {"skipped": skipped, "duplicates": [it.id for it in duplicates],
                   "where": where, "levels": levels},
    }


def default_items():
    return build_all()["items"]


def default_shop():
    return build_all()["shop"]


def default_recipes():
    return build_all()["recipes"]


def default_drops():
    return build_all()["drops"]


def problems(built):
    """四份放在一起的自洽检查（不含「合成面板点不点得到」—— 那条要 `shop`，在
    `gen_listing.py` 和 `test_shopcfg` 里）。返回说明列表，空 = 全过。"""
    items_ok = shopcfg.validate_items(built["items"])
    shop_ok = shopcfg.validate_shop(built["shop"])
    recipes_ok = shopcfg.validate_recipes(built["recipes"])
    drops_ok = shopcfg.validate_drops(built["drops"])
    out = []
    listed_shop = {i for i, e in shop_ok.items() if e["listed"]}
    listed_craft = {r["result"] for r in recipes_ok if r["listed"]}
    both = listed_shop & listed_craft
    if both:
        out.append("商店和合成都上架了：%s" % sorted(both))
    droppable = {r["material"] for r in drops_ok}
    for recipe in recipes_ok:
        total = sum(m["count"] for m in recipe["materials"])
        if total > 30:
            out.append("配方 #%d（%s）要 %d 个材料" % (recipe["id"], zh(recipe["result"]), total))
        for m in recipe["materials"]:
            if m["id"] not in droppable:
                out.append("配方 #%d 的材料 %s 没有掉落规则" % (recipe["id"], zh(m["id"])))
    hangul = [(i, e["name"]) for i, e in items_ok.items()
              if any("가" <= ch <= "힣" for ch in e["name"])]
    if hangul:
        out.append("还有韩文名：%s" % hangul[:10])
    out.extend(check_tiering(drops_ok))
    out.extend(check_recipes(drops_ok, recipes_ok))
    for r in drops_ok:
        if r.get("difficulty") not in (None,) + DIFFICULTIES:
            out.append("掉落规则用了选不到的难度 %s" % r.get("difficulty"))
    return out
