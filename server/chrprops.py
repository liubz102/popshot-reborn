#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chrprops.py —— 读 `bot_chrprops.json` 里的**角色属性表**（V0.3 M5）。

和 `mapdata.py` / `weapondata.py` 同一套路数（D22 / D29 / D42）：产物是
`tools/chrprops.py` 从原版 `Pack_decrypt/Data/ChrProps.ini` 离线抽出来的，
**随代码走** —— 进 git、进两个发布包，不放 `data/`（那儿只装用户数据）。

## 为什么需要它：服务端得**自己判命中**

bot 没有本机，没有一台客户端会替它的弹体算爆炸（D28 / §42），所以
「这一发打没打中人」只能服务端自己判 —— 而判命中要知道**人有多大**。

原版把角色的碰撞体写成**三个圆**（`ChrProps.ini` 的韩文注释直译）：

    ChrSizeLegs       "다리 충돌체크사이즈"  = 腿的碰撞检测尺寸
    ChrSizeBody       "몸통 충돌체크사이즈"  = 身体的碰撞检测尺寸
    ChrSizeHead       "머리 충돌체크사이즈"  = 头的碰撞检测尺寸
    ChrSizeLegsCrouch "움추렸을때 …"        = 蹲下时腿那个圆变小后的尺寸

和它们一一对应的是 `weapon.ini` 的三档伤害
（`Damage` / `HeadDamage` / `LegsDamage`，见那个 ini 开头的说明）：
**打中哪个圆就吃哪一档**。收方不会重算伤害（§42），所以这一档由服务端挑。

客户端那边的命中判定是「一串碰撞形状 × 一串碰撞形状」的通用求交
（`0x50f410` 拿两边的 `[obj+0x140]..[+0x144]` 双重循环），形状本身是从
模型骨骼上取的，服务端拿不到。

## ★ 圆心在哪：这是**模型**，不是从客户端逆出来的

ini 只给了三个**半径**，没给圆心。这里用的模型是
**三个圆从脚底往上依次相切**：

    腿   圆心 = 脚 − r腿                    （下沿正好贴地）
    身体 圆心 = 脚 − 2·r腿 − r身体
    头   圆心 = 脚 − 2·r腿 − 2·r身体 − r头

依据是它算出来的**总高 `2(r腿+r身体+r头)` 和 ini 里的 `DisplayHeight`
（"HP/SP 바를 보여주기 위한 키 높이" = 画血条的那个身高）在 17 个角色上
全部对得上**，误差 2~8 个单位（血条本来就画在头顶上方一点）：

    角色 0    2(12+13+10) = 70   DisplayHeight 75
    角色 2    2(16+18+10) = 88   DisplayHeight 90
    角色 107  2(19+15+12) = 92   DisplayHeight 100

旁证：会话 18 在客户端里**实测**角色 0 的枪口在脚上 **57**（§62），
而这个模型给出的头圆心是 60、身体圆心是 37 —— 枪正好举在头 / 肩之间。

⚠ 所以命中判定会有几个单位的出入。**这没关系**：它决定的是「擦边的那一发
算不算中」，而不是「明明躲开了还掉血」那种量级的错（那个是 §63 / §65）。
真要更准，得把角色模型的骨骼碰撞体读出来，那是另一个量级的工程。

## 只用标准库

服务端的便携运行时里没有第三方包；CPython 3.8（Win7 运行时）也要能跑。
"""
import json
import math
import os

#: 认得的产物格式版本。对不上就当没有数据 —— 退回下面那组默认尺寸，
#: 而不是按错的布局解出一堆乱七八糟的圆。
#: ★ 2（会话 19）：加了 `game` 段（`GameProps.ini` 的体力常量）。
FORMAT = 2

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bot_chrprops.json")

#: 命中部位。和 `weapon.ini` 的三档伤害一一对应。
REGION_LEGS = "legs"
REGION_BODY = "body"
REGION_HEAD = "head"

#: 表里查不到这个角色时用的尺寸 —— 取角色 0（타이，基础角色）的那一组。
#: ★ 宁可用一组**真实存在过的**尺寸，也不要用 0（那样谁都打不中）
#: 或者一个大圆（那样谁都躲不开）。
DEFAULT_SIZES = {
    "size_head": 10.0,
    "size_body": 13.0,
    "size_legs": 12.0,
    "size_legs_crouch": 7.0,
    "display_height": 75.0,
    "hp": 100,
    "sp": 100,
    "speed": 7.0,
}


#: `GameProps.ini` 的体力常量，产物里没有时用的默认值（就是原版那几个数）。
DEFAULT_GAME = {
    "sp_max": 100.0,
    "sp_charging": 0.25,          # 每 tick 回多少（蹲下 ×2，§41）
    "fast_run_sp_cost": 1.5,      # 冲刺跑每 tick 花多少
    "guard_sp_cost": 0.5,
    "assault_sp_cost": 40.0,
}


class GameProps(object):
    """`GameProps.ini` 里和体力有关的几个数。"""

    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw or {}

    def _num(self, name):
        value = self.raw.get(name)
        return DEFAULT_GAME[name] if value is None else float(value)

    @property
    def sp_max(self):
        return self._num("sp_max")

    @property
    def sp_charging(self):
        """每个客户端 tick 回多少体力。★ 蹲下时 ×2（`0x507250`，§41）。"""
        return self._num("sp_charging")

    @property
    def fast_run_sp_cost(self):
        """按着右键冲刺跑时，每个 tick 花多少体力。"""
        return self._num("fast_run_sp_cost")


class Move(object):
    """一招（`DashNN` / `JabNN`）—— 冲刺攻击的全部参数（§64）。

    ## 伤害圈怎么走：**原版 ini 自己写了公式**

    `ChrProps.ini` 开头那段注释（韩文）逐字给出了算法：

        posDelta = (PosDeltaInitX, PosDeltaInitY)
        degree   = StartDegree + currTick * MaxDegree / (DamageEndFrame - 1)
        posDelta.x += cos(degree) * MultiForX
        posDelta.y += sin(degree) * MultiForY

    `posDelta` 是**相对角色中心**的偏移，`currTick` 是第几帧动画。
    伤害只在 `CastEndFrame` ~ `DamageEndFrame` 之间生效。

    ⚠ **半径是本工程的近似**：带 `DamagingObjBone` 的招式（角色 0 的
    `Dash00` 就是）伤害圈跟着**骨骼**走、半径写在 `DamagingObjSize`，
    而骨骼位置服务端拿不到。这里一律按上面那条 `posDelta` 公式定位置，
    半径取 `DamageSize`（没有就退 `DamagingObjSize`）。
    偏差的后果是「近身这一下偶尔擦不着 / 够得稍远一点」，
    不是协议层面的静默故障。
    """

    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw or {}

    def _num(self, name, default=0.0):
        value = self.raw.get(name)
        return default if value is None else float(value)

    @property
    def damage(self):
        return int(self._num("damage"))

    @property
    def sp_cost(self):
        return self._num("sp_cost")

    @property
    def radius(self):
        """伤害圈的半径。`DamageSize` 优先，没有就退 `DamagingObjSize`。"""
        return self._num("damage_size") or self._num("obj_size")

    @property
    def cast_end(self):
        """第几帧开始有伤害。"""
        return int(self._num("cast_end"))

    @property
    def damage_end(self):
        """第几帧伤害结束。"""
        return int(self._num("damage_end"))

    @property
    def total_frame(self):
        """整套动作一共几帧 —— 这一招占用角色多久。"""
        return int(self._num("total_frame"))

    def offset(self, frame):
        """第 `frame` 帧时伤害圈相对角色的偏移 `(dx, dy)`（朝右的那一版）。"""
        span = max(1, self.damage_end - 1)
        degree = self._num("start_degree") + frame * self._num("max_degree") / span
        radians = math.radians(degree)
        return (self._num("delta_x") + math.cos(radians) * self._num("multi_x"),
                self._num("delta_y") + math.sin(radians) * self._num("multi_y"))

    def frames(self):
        """有伤害的那几帧。"""
        return range(self.cast_end, self.damage_end + 1)

    def reach(self):
        """这一招最远够得着多少（水平方向，含伤害圈半径）。"""
        return max([abs(self.offset(f)[0]) for f in self.frames()] or [0.0]) \
            + self.radius

    def __repr__(self):
        return ("<Move 伤害%d 体力%.0f 半径%.0f 够到%.0f 帧%d-%d/%d>"
                % (self.damage, self.sp_cost, self.radius, self.reach(),
                   self.cast_end, self.damage_end, self.total_frame))


class Character(object):
    """一个角色的属性。字段直接对应 `ChrProps.ini` 里的键，缺的走默认值。"""

    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw or {}

    def _num(self, name, default=0.0):
        value = self.raw.get(name)
        return default if value is None else value

    @property
    def id(self):
        return int(self._num("id", -1))

    @property
    def hp(self):
        return int(self._num("hp", DEFAULT_SIZES["hp"]))

    @property
    def sp(self):
        """体力上限（`ChrSp`）。冲刺攻击要花掉 `DashNN-SpCost`。"""
        return int(self._num("sp", DEFAULT_SIZES["sp"]))

    @property
    def speed(self):
        """`ChrSpeed` —— 原版的移动速度参数。"""
        return float(self._num("speed", DEFAULT_SIZES["speed"]))

    @property
    def display_height(self):
        return float(self._num("display_height",
                               DEFAULT_SIZES["display_height"]))

    @property
    def size_head(self):
        return float(self._num("size_head", DEFAULT_SIZES["size_head"]))

    @property
    def size_body(self):
        return float(self._num("size_body", DEFAULT_SIZES["size_body"]))

    @property
    def size_legs(self):
        return float(self._num("size_legs", DEFAULT_SIZES["size_legs"]))

    @property
    def size_legs_crouch(self):
        return float(self._num("size_legs_crouch",
                               DEFAULT_SIZES["size_legs_crouch"]))

    def move(self, name):
        """一招的原始参数字典（`"dash0"` / `"jab0"` …）；没有返回 `None`。"""
        return (self.raw.get("moves") or {}).get(name)

    def dash(self, index=0):
        """冲刺攻击（双击左右方向键那一下，§64）；这个角色没有就返回 `None`。

        ★ 默认第 0 式：语料 4394 发 `rpDash` 的 `+2` **恒 0** ——
        真人打出来的就只有这一式。
        """
        raw = self.move("dash%d" % int(index))
        return None if raw is None else Move(raw)

    def circles(self, x, y, crouched=False):
        """站在落脚点 `(x, y)` 时的三个碰撞圆。

        返回 `[(圆心x, 圆心y, 半径, 部位), …]`，**从头往脚排** ——
        调用方按这个顺序取第一个命中的，头就优先于身体、身体优先于腿
        （圆之间相切不重叠，实际上只会中一个；排序只是让边界情形有定论）。

        `crouched` = 这一刻蹲着没有（`rpCrouch` 的状态，§41）：腿那个圆换成
        `ChrSizeLegsCrouch`，整个人跟着矮下去 —— 这正是原版「蹲下能躲子弹」
        的来源（`0x507607` 那一套的另一半）。
        """
        legs = self.size_legs_crouch if crouched else self.size_legs
        body = self.size_body
        head = self.size_head
        legs_y = y - legs
        body_y = legs_y - legs - body
        head_y = body_y - body - head
        return [
            (x, head_y, head, REGION_HEAD),
            (x, body_y, body, REGION_BODY),
            (x, legs_y, legs, REGION_LEGS),
        ]

    def center(self, x, y, crouched=False):
        """身体那个圆的圆心 —— **瞄这里**。

        它是三个圆里最大的一个，也是原版角色的重心位置。瞄头（脚上 57，
        §62 那个枪口高度）在两个人不同高时太容易擦过去。
        """
        legs = self.size_legs_crouch if crouched else self.size_legs
        return (x, y - 2.0 * legs - self.size_body)

    def hit_region(self, x, y, px, py, radius=0.0, crouched=False):
        """点 `(px, py)`（带 `radius` 的圆）打在这个人的哪个部位。

        没打中返回 `None`。`radius` 是**弹体自己的半径**
        （`weapon.ini` 的 `Size`，那个 ini 直译就是「데미지 사이즈」）。
        """
        for cx, cy, r, region in self.circles(x, y, crouched):
            reach = r + radius
            dx = px - cx
            dy = py - cy
            if dx * dx + dy * dy <= reach * reach:
                return region
        return None

    def __repr__(self):
        return ("<Character %d hp=%d 头%.0f 身%.0f 腿%.0f>"
                % (self.id, self.hp, self.size_head, self.size_body,
                   self.size_legs))


class _Store(object):
    """整张表 15 KB，一次读进来留着。"""

    def __init__(self, path=DATA_PATH):
        self.path = path
        self._table = None
        self._cache = {}
        self._fallback = Character(dict(DEFAULT_SIZES))

    def table(self):
        if self._table is None:
            self._table = self._read()
        return self._table

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                table = json.load(fp)
        except (IOError, OSError, ValueError):
            # 没有角色表不该让服务端起不来 —— 退回默认尺寸，bot 照样能打，
            # 只是所有角色一样大。
            return {"characters": {}}
        if table.get("format") != FORMAT:
            return {"characters": {}}
        return table

    def get(self, character_id):
        """按角色 id 取；表里没有就返回**默认尺寸**那一份（不返回 `None`）。

        ★ 故意不返回 `None`：命中判定这条路上多一个 `if x is None` 分支，
        就多一处「静默地谁都打不中」的可能。
        """
        key = str(int(character_id))
        if key in self._cache:
            return self._cache[key]
        raw = self.table().get("characters", {}).get(key)
        character = self._fallback if raw is None else Character(raw)
        self._cache[key] = character
        return character

    def game(self):
        """`GameProps.ini` 的体力常量；产物里没有就退默认（原版那几个数）。"""
        return GameProps(self.table().get("game") or {})

    def known(self, character_id):
        """这个角色在表里查得到吗（给日志 / 单测用）。"""
        return str(int(character_id)) in self.table().get("characters", {})

    def count(self):
        return len(self.table().get("characters", {}))


STORE = _Store()


def get(character_id):
    """按角色 id 取属性；查不到返回默认尺寸那一份。"""
    return STORE.get(character_id)


def game():
    """`GameProps.ini` 的体力常量。"""
    return STORE.game()


def known(character_id):
    return STORE.known(character_id)


def count():
    return STORE.count()
