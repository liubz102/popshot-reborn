#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_listing.py —— 2026-09-06 全量上架：重新生成 server/data/ 四份运营配置。

设计口径（写在这里是为了让脚本自己就是说明书）：

* 商店卖「散件」：D/R/F + 特别版武器、没有名字的散装铠甲（#id / 模型编号）、
  装饰件（头饰 / 尾饰 / 翅膀 / 礼物）、染色剂、突击技、纯外观套装。
* 合成出「成套的、有名字的」东西：四条原有产线 + 四条按 boss 材料主题的
  新产线、四套 +30% 攻击的全身套、戒指、宠物、称号。
* 材料全靠打：珠子按难度、通用矿料按难度、boss 材料按关卡（普通 = 4xxxx，
  困难 = 5xxxx，照 2007 新浪「合成系统」页的口径）。
* ★ 称号和卡片**不上架**（用户 2026-09-06 第二轮）：卡片原版是按成就给的
  （击杀数 / 误伤次数…），本项目还没有成就统计，按概率掉不合理。
  等有了成就数据再把称号配方加回来。
* ★ 难度只有 1 / 2 / 3（中国区客户端选不到第 4 档「极限」，V0.3商店 §38），
  而且**同一种材料的期望产出随难度只增不减**（用户 2026-09-06：难度越高
  奖励应该越多）—— 脚本末尾有一道断言守着这条。

★ 只改 json，不动代码；跑完会把旧文件留成 `*.bak-<时刻>`。
"""
import collections
import json
import os
import shutil
import sys
import time

SERVER = r"D:\git\popshot-reborn\develop\3_Shop_Craft\server"
sys.path.insert(0, SERVER)
import shopdata      # noqa: E402
import shopcfg       # noqa: E402
import shop          # noqa: E402

DATA = os.path.join(SERVER, "data")
APPLY = "--apply" in sys.argv


def load(name):
    with open(os.path.join(DATA, name), "r", encoding="utf-8") as fp:
        return json.load(fp)


current_items = {e["id"]: e for e in load("items.json")["items"]}

# ---------------------------------------------------------------------------
# 中文名
# ---------------------------------------------------------------------------
#: 武器基础名 —— 2007 新浪官方资料页「称号系统」里的卡片名字就是武器名
#: （左轮手枪 / 苹果弹 / 狙击枪 / 复古短枪 / 火焰弹 / 华尔兹加农炮 / 重机枪 /
#: 榴弹发射器 / 火箭炮），照抄。
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
#: 按 id 定的名字（同名同图的两套材料只能按 id 分；称号卡片照新浪页）。
NAME_BY_ID = {
    102400001: "京",
    30006: "熔岩碎片",
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
#: 原版没给名字、但要摆上合成面板 / 货架当「一件东西」卖的整套装备：
#: `#1010049` 这种名字在配方列表里没法看，按「角色 + 描述」起个能认的名字
#: （不是原版名字，是复活工程自己起的描述性名字）。
SET_NAME_ZH = {
    49: "强攻套装 I", 50: "强攻套装 II", 51: "强攻套装 III", 52: "强攻套装 IV",   # 攻击 +30%
    22: "羽翼套装",                                                             # 带翅膀的外观全套
    59: "休闲套装",                                                             # 三件外观套
}
for _ch, _zh in shopcfg.CHARACTER_ZH.items():
    for _tail, _name in SET_NAME_ZH.items():
        NAME_BY_ID[(_ch + 1) * 1000000 + 10000 + _tail] = "%s %s" % (_zh, _name)

#: 金 / 银骑士甲（用户 2026-09-06：原版这两套整套都要合成，龙之血 → 银、
#: 龙之精华 → 金）。图标 `chNN?0015` = 金色、`chNN?0016` = 银色（看图确认过），
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
            return "%s %s%d" % (zh, shopcfg.SERIES_ZH[item.series], item.tier)
    return SPECIAL_WEAPON_ZH.get(name, current_items[item.id]["name"])


def name_of(item):
    if item.id in NAME_BY_ID:
        return NAME_BY_ID[item.id]
    if item.kind == "weapon":
        return weapon_name(item)
    if knight_color(item):
        return knight_name(item)
    return current_items[item.id]["name"]


def dedupe(ownable):
    """同类别 / 同角色 / 同槽位 / 同图标 / 同加成 / 同武器数值 = 同一件东西，
    只留 id 最小的（用户 2026-09-06：`CH01B0023` 上架了两件一模一样的）。
    材料不去重：`4xxxx` / `5xxxx` 同名同图是普通 / 困难两档，故意的。
    返回 `(留下的, 去掉的)`，两边都按 id 升序。"""
    seen = set()
    kept, dropped = [], []
    for item in sorted(ownable, key=lambda it: it.id):
        if item.kind == "material":
            kept.append(item)
            continue
        key = (item.kind, item.character, item.part_flag, item.icon,
               json.dumps(item.bonus or {}, sort_keys=True),
               json.dumps(item.weapon or {}, sort_keys=True))
        if key in seen:
            dropped.append(item)
            continue
        seen.add(key)
        kept.append(item)
    return kept, dropped


# ---------------------------------------------------------------------------
# 分类：每件东西走商店还是合成
# ---------------------------------------------------------------------------
def weight(item):
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


SET_INDEX_ZH = {60: "加密尔军官服", 61: "舞动皇冠 / 圣洁圣徒 / 乱流",
                62: "收割锁链 / 蝴蝶 / 闪光渴求者",
                63: "最后通牒 / 掘墓人 / 群魔殿",
                64: "大师铠甲", 65: "初阶凤凰铠甲", 66: "高阶凤凰铠甲",
                67: "佣兵铠甲"}

# ---------------------------------------------------------------------------
# 商店：价格 / 等级
# ---------------------------------------------------------------------------
WEAPON_PRICE = {1: 3000, 2: 8000, 3: 18000}
WEAPON_LEVEL = {1: 5, 2: 10, 3: 18}
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
PART_BASE = {1: 500, 2: 400, 4: 400, 8: 300, 16: 300}
GOLD_PER_POINT = 450
DECOR_BASE = 800          # 头饰 / 尾饰 / 翅膀
GIFT_PRICE = 500          # 선물 礼物（一件没有加成的上衣）
COSMETIC_SET_PRICE = {539: 6000, 11: 2000, 9: 2000, 19: 2000}
SPRAY_PRICE = 1500
#: 突击技按 id 尾号：(价格, 等级)
DASH_PRICE = {2: (2000, 1), 3: (3000, 4), 4: (4500, 7), 5: (3500, 5), 6: (6000, 10)}


def shop_level(w):
    if w <= 1:
        return 1
    return min(18, int(round(1 + w * 1.2)))


# ---------------------------------------------------------------------------
# 合成：产线
# ---------------------------------------------------------------------------
#: 尾号 → (穿上等级, 每点加成的金币, ((材料, 每点加成的个数), …))
SET_LINES = {
    67: (10, 260, ((20007, .40), (30018, .25), (10004, .60))),        # 佣兵：铁矿石 + 青铜管 + 蓝珠
    64: (14, 320, ((20007, .40), (30005, .25), (10002, .60))),        # 大师：铁矿石 + 水管 + 红珠
    65: (18, 420, ((30016, .30), (30006, .40), (10002, .55))),        # 初阶凤凰：羽 + 熔岩 + 红珠
    66: (24, 560, ((30016, .30), (30017, .25), (30006, .40), (10001, .55))),  # 高阶凤凰
    60: (7, 200, ((40012, .35), (20007, .35), (10003, .60))),         # 加密尔军官服：黑骑士(第 5 关) 的头盔
    61: (9, 240, ((40008, .30), (40009, .30), (10004, .60))),         # 第 1 / 2 关普通材料
    62: (12, 300, ((40010, .30), (40011, .30), (30013, .15), (10002, .55))),  # 第 3 / 4 关 + 初级制作书
    63: (16, 380, ((40015, .30), (50012, .25), (30014, .15), (10002, .55))),  # 第 6 关 + 黑骑士困难 + 中级制作书
}
#: 强攻套装（+30% 攻击的全身套）：**商店卖**，放「装备 → 套装」（用户 2026-09-06：
#: 合成面板没有套装标签，所有套装改成商店上架）。纯金币价所以定得高。
ATTACK_SET_LEVEL, ATTACK_SET_PRICE = 24, 30000
#: 金 / 银骑士甲产线：银 ← 龙之血（岩浆巨龙·普通），金 ← 龙之精华（岩浆巨龙·困难）。
#: 形状和 `SET_LINES` 一样：(穿上等级, 每点加成的金币, 材料表)。
KNIGHT_LINES = {
    "0016": (8, 220, ((40009, .35), (20007, .35), (10004, .55))),
    "0015": (12, 300, ((50009, .35), (20007, .35), (10002, .55))),
}
RING = {   # 尾号 → (等级, 花费, 材料)
    59: (6, 800, ((10003, 5), (10004, 3), (20007, 2))),
    60: (11, 2000, ((10002, 4), (20007, 4), (40011, 2))),
}
PET = {    # id → (等级, 花费, 材料)
    220007: (1, 1500, ((10003, 6), (10004, 4))),                          # 熊猫（纯外观）
    220001: (6, 3000, ((10004, 6), (30019, 1), (20007, 3))),              # 青鸟
    220006: (8, 4000, ((40011, 4), (30005, 3), (10004, 4))),              # 防护装置·粉
    220004: (10, 5000, ((40008, 4), (50008, 2), (20007, 3), (10002, 3))),  # 迷你机械青蛙 ← Z芯片
    220003: (10, 5000, ((40009, 4), (30006, 3), (10002, 3))),             # 火焰蝙蝠 ← 龙之血 + 熔岩
    220002: (12, 6000, ((50011, 3), (40011, 3), (20007, 4), (10002, 2))),  # 防护装置 ← 螺旋桨
    220005: (12, 5000, ((40015, 3), (30006, 3), (10002, 3))),             # 萤火虫 ← 太阳碎片
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
# 掉落
# ---------------------------------------------------------------------------
BOSS = {   # 关卡 → (普通材料, 困难材料, 关卡名)
    1: (40008, 50008, "机械青蛙"), 2: (40009, 50009, "岩浆巨龙"),
    3: (40010, 50010, "神秘岛"), 4: (40011, 50011, "鲸鱼战舰"),
    5: (40012, 50012, "黑骑士"), 6: (40015, 50015, "太阳齿轮"),
}


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


#: 玩家选得到的难度：1 简单 / 2 普通 / 3 困难。**没有第 4 档**（§38）。
DIFFICULTIES = (1, 2, 3)


def build_drops():
    """★ 口径：**同一种材料，难度越高期望产出只增不减**（`check_monotonic`
    守着）。所以「简单掉绿珠、普通掉蓝珠」这种按难度换颜色的写法不能用 ——
    那会让绿珠在普通难度反而变少。改成：低档材料每一档都掉、越高越多，
    高档材料从某一档起加进来。
    """
    rules = []
    # 原版基线（Promotion.ini 的 4 关，照抄）
    for stage, diff, mat, pid in ((1, 2, 30018, "0118"), (1, 3, 30018, "0125"),
                                  (4, 3, 30019, "0123"), (7, 1, 30018, "0114")):
        rules.append(rule(mat, 100, stage=stage, difficulty=diff,
                          note="原版基线（Promotion.ini %s）" % pid))
    # ★ 原版基线自己就不单调：第 7 关只有**简单**给青铜管，普通 / 困难什么都
    #   不给（Promotion.ini 0121 / 0128 只给经验和金币）。按「难度越高奖励
    #   不能更少」补上这两档。
    rules.append(rule(30018, 100, stage=7, difficulty=2, note="补基线：第 7 关普通难度也必掉青铜管"))
    rules.append(rule(30018, 100, stage=7, difficulty=3, note="补基线：第 7 关困难难度也必掉青铜管"))
    # 珠子：绿珠每一档都掉、越高越多；蓝珠从普通起；红珠 / 黑珠只有困难。
    # 对战也给（新浪页：可在对战和任务模式中获得）。
    for mat, table in ((10003, {1: 50, 2: 60, 3: 70}), (10004, {2: 40, 3: 60}),
                       (10002, {3: 50}), (10001, {3: 30})):
        for diff, prob in sorted(table.items()):
            rules.append(rule(mat, prob, difficulty=diff, note="珠子：按难度递增"))
    rules.append(rule(10003, 25, mode="pvp", cleared_only=False, note="珠子：对战参战就有机会"))
    rules.append(rule(10004, 20, mode="pvp", note="珠子：对战获胜"))
    rules.append(rule(10002, 12, mode="pvp", note="珠子：对战获胜"))
    rules.append(rule(10001, 8, mode="pvp", note="珠子：对战获胜"))
    rules.append(rule(10001, 40, stage=7, difficulty=3, note="黑珠：最后一关困难难度另加"))
    # 通用矿料
    for mat, table, label in ((20007, {1: 25, 2: 40, 3: 55}, "铁矿石"),
                              (30005, {2: 30, 3: 45}, "水管"),
                              (30018, {3: 30}, "青铜管（基线之外的补充）"),
                              (30006, {2: 20, 3: 40}, "熔岩碎片"),
                              (30016, {2: 15, 3: 35}, "不死鸟之羽"),
                              (30017, {3: 12}, "不死鸟之泪：只有困难难度")):
        for diff, prob in sorted(table.items()):
            rules.append(rule(mat, prob, difficulty=diff, note=label + "：按难度递增"))
    rules.append(rule(30006, 40, stage=2, difficulty=2, note="岩浆巨龙多掉熔岩碎片"))
    rules.append(rule(30006, 60, stage=2, difficulty=3, note="岩浆巨龙多掉熔岩碎片"))
    rules.append(rule(30019, 30, stage=4, difficulty=2, note="浮游石：鲸鱼战舰普通难度也有机会"))
    rules.append(rule(30017, 25, stage=7, difficulty=3, note="不死鸟之泪：最后一关困难难度另加"))
    # 制作书：三档。初级从普通起，中级只有困难，高级只有最后两关的困难。
    rules.append(rule(30013, 15, difficulty=2, note="制作书 (初级)"))
    rules.append(rule(30013, 25, difficulty=3, note="制作书 (初级)"))
    rules.append(rule(30014, 15, difficulty=3, note="制作书 (中级)：只有困难难度"))
    rules.append(rule(30015, 15, stage=6, difficulty=3, note="制作书 (高级)：太阳齿轮困难难度"))
    rules.append(rule(30015, 15, stage=7, difficulty=3, note="制作书 (高级)：扎米洛秘密基地困难难度"))
    # boss 材料：普通掉 4xxxx、困难掉 5xxxx（新浪「合成系统」页的口径）。
    # 简单难度给一点普通材料照顾新手；困难难度普通材料照给（不能比普通少）。
    for stage in sorted(BOSS):
        normal, hard, name = BOSS[stage]
        rules.append(rule(normal, 40, stage=stage, difficulty=1, note="%s·简单" % name))
        rules.append(rule(normal, 100, stage=stage, difficulty=2, note="%s·普通（原版口径：必得）" % name))
        rules.append(rule(normal, 100, stage=stage, difficulty=3, note="%s·困难（普通材料照给）" % name))
        rules.append(rule(hard, 100, stage=stage, difficulty=3, note="%s·困难（原版口径：必得）" % name))
    # ★ 称号卡片不掉（见文件头）：原版按成就给，成就统计还没做。
    # 文件里就按 模式 → 关卡 → 难度 → 材料 排好（管理页显示时也这么排），
    # 「不限」排在具体号码前面，闯关排在对战前面。
    rules.sort(key=lambda r: (0 if r["mode"] == "quest" else 1,
                              r.get("stage") or 0, r.get("difficulty") or 0,
                              r["material"]))
    return {"format": shopcfg.FORMAT, "rules": rules}


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


def check_monotonic(rules):
    """每一种材料、每一关：难度越高期望产出只增不减。返回违反的 `(材料, 关卡, 低档, 高档)`。"""
    bad = []
    materials = sorted({r["material"] for r in rules if r.get("mode", "quest") == "quest"})
    for material in materials:
        for stage in range(1, 8):
            prev = None
            for difficulty in DIFFICULTIES:
                now = expected_yield(rules, material, stage, difficulty)
                if prev is not None and now + 1e-9 < prev[1]:
                    bad.append((material, stage, prev[0], difficulty, prev[1], now))
                prev = (difficulty, now)
    return bad


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ownable = []
    for kind in shopdata.kinds():
        for item_id in shopdata.ids_of_kind(kind):
            item = shopdata.get(item_id)
            if item is not None and item.ownable:
                ownable.append(item)
    ownable.sort(key=lambda it: it.id)
    everything = list(ownable)                 # 物品库要收全部 808 件
    ownable, duplicates = dedupe(ownable)      # 上架 / 合成只看去重后的

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
        recipes_out.append({"result": item.id, "listed": True,
                            "cost": int(cost), "materials": materials})
        levels[item.id] = int(level)
        where[item.id] = "合成"

    for item in ownable:
        w = weight(item)
        if item.kind == "weapon":
            if item.series and item.tier and item.character is not None:
                sell(item, WEAPON_PRICE[item.tier], WEAPON_LEVEL[item.tier])
            elif item.id in SPECIAL_WEAPON:
                sell(item, *SPECIAL_WEAPON[item.id])
            else:
                skipped["weapon"] += 1
        elif item.kind == "armor":
            if is_named_set_piece(item):
                level, gold, mats = SET_LINES[item.id % 100]
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
        elif item.kind == "dash":
            sell(item, *DASH_PRICE[item.id % 10])
        elif item.kind == "ring":
            level, cost, mats = RING[item.id % 100]
            craft(item, cost, fixed(mats), level)
        elif item.kind == "pet":
            level, cost, mats = PET[item.id]
            craft(item, cost, fixed(mats), level)
        elif item.kind == "title" and item.id in TITLE:
            cost, mats = TITLE[item.id]
            craft(item, cost, fixed(mats), 1)
        else:
            skipped[item.kind] += 1

    # items.json：全部 808 件，名字 + 等级
    for item in everything:
        entry = {"id": item.id, "name": name_of(item)}
        if shopcfg.has_level_and_character(item):
            entry["level"] = levels.get(item.id, int(current_items[item.id].get("level", 1)))
            if item.character is not None:
                entry["character"] = int(item.character)
        items_out.append(entry)

    recipes_out.sort(key=lambda r: r["result"])
    for index, recipe in enumerate(recipes_out, start=1):
        recipe["id"] = index
        recipes_out[index - 1] = {"id": index, "result": recipe["result"],
                                  "listed": True, "cost": recipe["cost"],
                                  "materials": recipe["materials"]}
    shop_out.sort(key=lambda e: (e["kind"], e["id"]))

    items_doc = {"format": shopcfg.FORMAT, "items": items_out}
    shop_doc = {"format": shopcfg.FORMAT, "items": shop_out}
    recipe_doc = {"format": shopcfg.FORMAT, "recipes": recipes_out}
    drops_doc = build_drops()

    # ---- 校验：服务端那四个校验器 + 自洽检查 ----
    items_ok = shopcfg.validate_items(items_doc)
    shop_ok = shopcfg.validate_shop(shop_doc)
    recipes_ok = shopcfg.validate_recipes(recipe_doc)
    drops_ok = shopcfg.validate_drops(drops_doc)
    problems = []
    listed_shop = {i for i, e in shop_ok.items() if e["listed"]}
    listed_craft = {r["result"] for r in recipes_ok if r["listed"]}
    both = listed_shop & listed_craft
    if both:
        problems.append("商店和合成都上架了：%s" % sorted(both))
    droppable = {r["material"] for r in drops_ok}
    for recipe in recipes_ok:
        total = sum(m["count"] for m in recipe["materials"])
        if total > 30:
            problems.append("配方 #%d（%d）要 %d 个材料" % (recipe["id"], recipe["result"], total))
        for m in recipe["materials"]:
            if m["id"] not in droppable:
                problems.append("配方 #%d 的材料 %d 没有掉落规则" % (recipe["id"], m["id"]))
        cat = shop.category_of(recipe["result"])
        if cat not in shop.COMPOSITION_CATEGORIES:
            problems.append("配方 #%d 的产物 %d 归在 %#x，合成面板点不到" % (recipe["id"], recipe["result"], cat))
    for material in droppable:
        if not shopdata.is_material(material):
            problems.append("掉落 %d 不是材料" % material)
    hangul = [(e["id"], e["name"]) for e in items_out if any("가" <= ch <= "힣" for ch in e["name"])]
    if hangul:
        problems.append("还有韩文名：%s" % hangul[:10])
    for material, stage, low, high, before, after in check_monotonic(drops_ok):
        problems.append("材料 %d 在第 %d 关：难度 %d 期望 %.2f > 难度 %d 期望 %.2f（难度越高奖励反而少）"
                        % (material, stage, low, before, high, after))
    for r in drops_ok:
        if r.get("difficulty") not in (None,) + DIFFICULTIES:
            problems.append("掉落规则用了选不到的难度 %s" % r.get("difficulty"))
    # 每种要用到的材料，掉落概率总和（粗略）—— 看有没有几乎掉不出来的
    used = collections.Counter()
    for recipe in recipes_ok:
        for m in recipe["materials"]:
            used[m["id"]] += 1

    # ---- 汇总 ----
    print("上架统计：商店 %d 件、合成 %d 条、掉落规则 %d 条、物品库 %d 件"
          % (len(listed_shop), len(listed_craft), len(drops_ok), len(items_ok)))
    by_kind = collections.Counter()
    for item in ownable:
        by_kind[(item.kind, where.get(item.id, "未上架"))] += 1
    for (kind, w_), n in sorted(by_kind.items()):
        print("  %-11s %-4s %d" % (kind, w_, n))
    print("跳过（不上架）：", dict(skipped))
    print("去重去掉的 %d 件：%s" % (len(duplicates), [it.id for it in duplicates]))
    print("合成产物按类别：", collections.Counter(shop.category_of(r["result"]) & 0xFFFFF for r in recipes_ok))
    print("每种材料被几条配方用到：")
    for mid, n in sorted(used.items()):
        probs = ["%s%s%s%d%%" % (r["mode"], "/s%d" % r["stage"] if r.get("stage") else "",
                                 "/d%d" % r["difficulty"] if r.get("difficulty") else "", r["prob"])
                 for r in drops_ok if r["material"] == mid]
        print("  %-8d %-12s 用于 %2d 条配方  掉落：%s" % (mid, NAME_BY_ID.get(mid) or current_items[mid]["name"], n, " ".join(probs)))
    prices = sorted(e["price"] for e in shop_ok.values())
    print("商店价格区间：%d ~ %d，中位 %d" % (prices[0], prices[-1], prices[len(prices) // 2]))
    print("等级分布（上架/合成的）：", sorted(collections.Counter(levels.values()).items()))
    if problems:
        print("\n★ 自洽检查有问题：")
        for p in problems:
            print("  -", p)
        return 1
    print("自洽检查全部通过。")

    if not APPLY:
        print("\n（试算模式，没有写文件。加 --apply 才写。）")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for name, doc in (("items.json", items_doc), ("shop.json", shop_doc),
                      ("recipe.json", recipe_doc), ("drops.json", drops_doc)):
        path = os.path.join(DATA, name)
        if os.path.exists(path):
            shutil.copyfile(path, "%s.bak-%s" % (path, stamp))
        shopcfg.write_json(path, doc)
        print("已写", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
