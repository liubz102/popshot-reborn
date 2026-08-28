#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""weapondata.py —— 读 `bot_weapons.json` 里的**武器表**（V0.3 M3b）。

和 `mapdata.py` 同一套路数（D22 / D29）：产物是 `tools/weapondata.py` 从原版
`Pack_decrypt/Data/weapon.ini` 离线抽出来的，**随代码走** —— 进 git、进两个
发布包，不放 `data/`（那儿只装用户数据）。

## bot 开火要它干四件事（§43）

    Weapon.damage                填 `rpExplode +24`（★ 收方照抄，不重算，§42）
    Weapon.velocity/gravity      算弹道 —— 交给 `ballistics.solve()`（§47）
    Weapon.power_control/max_velocity  初速的三种模式
    Weapon.fire_interval_ms      两发之间隔多久 —— ★ 原版的 CoolingTime，不是我编的
    Weapon.shots                 `rpFire +22` 填几（= SpreadFrags，§46）
    Weapon.handle_step           ★★ 每发 rpFire 吃掉几个弹体句柄

最后一条最要命：句柄和收方的分配器对不上，`rpExplode` 会被**静默丢弃**
（`0x492750` 查不到弹体就整个 return），表现是「子弹飞过去不炸、一滴血不掉」，
一局之内不自愈。

## 铁律：只用标准库

服务端的便携运行时里没有第三方包。这里只有 `json` / `os`，
CPython 3.8（Win7 运行时）也能跑。
"""
import json
import os

#: 认得的产物格式版本。对不上就当没有数据 —— 宁可 bot 不开枪，
#: 也不要按错的布局解出一把参数乱七八糟的武器。
#: ★ 2（会话 14）：加了 `shots`，`handle_step` / `usable` 的口径也变了，
#:   跟 `tools/weapondata.py` 的 `FORMAT` 必须一起动。
#: ★ 3（会话 19）：`usable` 收紧成只放行 `CreatingClass=GeneralBullet`（§70）
#:   —— 分裂弹 / 火墙 / 炮台那几类会在收方**多吃句柄**，按老口径记账会永久错位。
#: ★ 4（会话 21）：**§70 那条口径是错的**（§72）：那些额外对象的创建点全在
#:   `IsMine` 门里，bot 的弹体在任何一台上都不是「自己的」⇒ 一个都不会造出来。
#:   `usable` 改成按 `SAFE_CLASSES` 白名单放行，新增 `slice_time` / `fuse_ticks`。
#: ★ 5（会话 22）：新增火墙那几格（`slice_id` / `spawn_count` / …，§75）。
#: ★ 6（会话 23）：新增 `homing_angle`（追踪弹的转向速率，§77）。
#: ★ 7（会话 24）：新增分裂弹的碎片那几格（`slice_count` / `slice_angle` /
#:   `slice_angle_base` / `slice_angle_random`，§81）。
FORMAT = 7

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bot_weapons.json")


class Weapon(object):
    """一把武器。字段直接对应 `weapon.ini` 里的键，缺的就是 `None`。"""

    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw

    def __getattr__(self, name):
        # `__slots__` 里没有的一律去产物字典里找 —— 加字段时这边不用改。
        try:
            return self.raw[name]
        except KeyError:
            raise AttributeError(name)

    def get(self, name, default=None):
        return self.raw.get(name, default)

    @property
    def id(self):
        return self.raw["id"]

    @property
    def damage(self):
        """`rpExplode +24` 要填的伤害。★ 收方原样拿去扣血，不会重算（§42）。"""
        return int(self.raw.get("damage", 0))

    @property
    def head_damage(self):
        """打在**头**那个圆上的伤害（`HeadDamage`）。没填就退回 `Damage`。"""
        value = self.raw.get("head_damage")
        return self.damage if value is None else int(value)

    @property
    def legs_damage(self):
        """打在**腿**那个圆上的伤害（`LegsDamage`）。没填就退回 `Damage`。"""
        value = self.raw.get("legs_damage")
        return self.damage if value is None else int(value)

    def damage_for(self, region):
        """按命中部位挑伤害档（`chrprops.REGION_*`）。

        ★ 这三档是原版的（`weapon.ini` 开头写着
        `Damage 상대방에게 가해지는 데미지값` / `HeadDamage 헤드샷일 경우` /
        `LegsDamage 다리에 맞았을 때`），**收方不重算**，填多少掉多少（§42）。
        """
        if region == "head":
            return self.head_damage
        if region == "legs":
            return self.legs_damage
        return self.damage

    @property
    def size(self):
        """弹体自己的碰撞半径（`weapon.ini` 的 `Size` = 「데미지 사이즈」）。

        判命中时和角色那三个圆的半径相加。没填按 0 算（当成一个点）。
        """
        return float(self.raw.get("size") or 0.0)

    @property
    def splash_damage(self):
        """溅射伤害（`SplashDamage`）。没有就退回 `Damage`。"""
        value = self.raw.get("splash_damage")
        return self.damage if value is None else int(value)

    @property
    def handle_step(self):
        """★★ 每发 `rpFire` 会让收方的弹体句柄计数器前进几格。

        `shots × (2 if SplashRange else 1)`（§46）。`None` / 0 = 算不出来，
        这种武器 bot 不许用（现在的表里没有这种）。

        ⚠ 它只在「**这一发的 `rpExplode` 全发完之前不开下一枪**」的前提下
        成立，闸门在 `bot.py`（`_pending_explosions`）。
        """
        return self.raw.get("handle_step")

    @property
    def shots(self):
        """`rpFire +22` 那一格要填几（= `SpreadFrags`，缺省 1）。

        ★ 填小了**一颗子弹都造不出来**：收侧 `OnFire` 的外层轮数是
        `count / SpreadFrags` 的整数除法（`0x491fa5`），填 1 打 3 散弹的枪
        就是 `1 / 3 = 0` 轮（§46）。
        """
        return int(self.raw.get("shots", 1) or 1)

    @property
    def fire_interval_ms(self):
        """两发之间至少隔多久（毫秒）。`CoolingTime`，没有就退 `ReloadTime`。

        ⚠ **有弹匣的武器不能只看它** —— 打空 `magazine` 发之后要停
        `reload_ms`。节奏在 `bot._reload_after_shot()`。
        """
        return self.raw.get("fire_interval_ms")

    @property
    def magazine(self):
        """`MagazineCount`：一个弹匣几发。没有这一格返回 `None`
        （榴弹 / 火箭那一类，打一发装一次）。"""
        value = self.raw.get("magazine")
        return None if not value else int(value)

    @property
    def cooling_ms(self):
        """`CoolingTime`：**弹匣里**两发之间隔多久。没有返回 `None`。"""
        return self.raw.get("cooling_ms")

    @property
    def reload_ms(self):
        """`ReloadTime`：换一个弹匣要多久。"""
        return self.raw.get("reload_ms")

    @property
    def velocity(self):
        return float(self.raw.get("velocity", 0.0))

    @property
    def max_velocity(self):
        """`MaxVelocity`：蓄力武器的初速上限。没有这一格返回 `None`。"""
        value = self.raw.get("max_velocity")
        return None if value is None else float(value)

    @property
    def power_control(self):
        """初速模式 0 / 1 / 2 —— `ballistics` 按它分流（§47）。"""
        return int(self.raw.get("power_control", 0) or 0)

    @property
    def gravity(self):
        return float(self.raw.get("gravity", 0.0))

    @property
    def acceleration(self):
        """每 tick 沿飞行方向加多少速（`Acceleration`，7 把武器有，§49）。"""
        return float(self.raw.get("acceleration", 0.0) or 0.0)

    @property
    def splash_range(self):
        """溅射半径。有它就意味着**每颗子弹多吃一个弹体句柄**（§43）。"""
        return self.raw.get("splash_range")

    @property
    def fuse_ticks(self):
        """★ **引信**：这颗弹体最多飞几个 tick，没有引信返回 `None`（§72）。

        `AppleGrenade` / `SeedBomb` / `SliceBullet` 的 `Tick` 里有一个从
        `SliceTime / 32` 倒数的计数器，数到 0 就**在每一台机器上自爆**
        （`0x47c952` / `0x48503f` / `0x4851d9`）—— 那一下**不带伤害**
        （伤害只来自射手发的 `rpExplode`），而且弹体从此不存在。

        ⇒ 服务端的 `rpExplode` 必须**赶在它前面**发出去，否则收方按句柄
        查不到弹体、整包静默丢弃（§42 第 4 条）。`bot._fire_target()` 因此
        把「飞不到就别开枪」这条按引信也判一遍。
        """
        value = self.raw.get("fuse_ticks")
        return None if not value else int(value)

    @property
    def homing_range(self):
        """追踪弹的**作用距离**（`HomingRange`）；没有返回 0。"""
        return float(self.raw.get("homing_range") or 0.0)

    @property
    def homing_angle(self):
        """★★ 追踪弹每 tick 最多转多少**度**的那个基数（`HomingAngle`，§77）。

        真正的转角是 `HomingAngle / 7`（`0x47e53a` 的 `fmul [0x693c34]`）。
        0 = 不追踪（`0x47e35a: cmp [weapondef+0x78], 0; je 出口`）。
        """
        return float(self.raw.get("homing_angle") or 0.0)

    #: ★★ 碎片扇形三个参数的**缺省值**（§81）。
    #:
    #: 出处是解析器里那三条设默认值的指令 —— `0x40b8c2` 读 ini 时
    #: `ebx` 就是缺省值：
    #:
    #:     0x48984d  mov ebx, 0xa0     -> [def+0xac] SliceAngle        = 160
    #:     0x489887  push 0x1e; pop ebx-> [def+0xb0] SliceAngleBase    = 30
    #:     0x4898c5  （ebx 仍是 0x1e） -> [def+0xb4] SliceAngleRandom  = 30
    #:
    #: 苹果雷的碎片节 `ch00-02a` 三格一个都没写 ⇒ 走的正是这组缺省值。
    #: ★ 语料交叉验证（1992 组碎片，每组 4 片）：四片的角度取值范围
    #: 分别是 `[15,44] [68,97] [121,150] [175,204]`，每档正好 30 个整数值
    #: —— 按上面的公式算出来一位不差。
    SLICE_ANGLE_DEFAULT = 160
    SLICE_ANGLE_BASE_DEFAULT = 30
    SLICE_ANGLE_RANDOM_DEFAULT = 30

    @property
    def slice_count(self):
        """★ 这颗弹体炸开会分成几片（`SliceCount`）；不分裂返回 0（§81）。

        只有**母弹**那一节有它（`ch00-02` = 4、`ch03-02` = 3），
        碎片那一节（`SliceId` 指向的 `ch00-02a`）一般不写。
        """
        return int(self.raw.get("slice_count") or 0)

    @property
    def slice_angle(self):
        """碎片扇形的**张角**（度），缺省 160 —— 见 `SLICE_ANGLE_DEFAULT`。"""
        value = self.raw.get("slice_angle")
        return self.SLICE_ANGLE_DEFAULT if value is None else int(value)

    @property
    def slice_angle_base(self):
        """碎片扇形的**起始角**（度），缺省 30。"""
        value = self.raw.get("slice_angle_base")
        return self.SLICE_ANGLE_BASE_DEFAULT if value is None else int(value)

    @property
    def slice_angle_random(self):
        """每片角度上叠的**随机幅度**（度，`[0, n)` 再减 `n/2`），缺省 30。"""
        value = self.raw.get("slice_angle_random")
        return (self.SLICE_ANGLE_RANDOM_DEFAULT if value is None
                else int(value))

    @property
    def lockon_range(self):
        """原版**自动瞄准**的作用距离（只有 1 号轻武器有）。

        ⚠ **它不是射程**（§44）。bot 的交战距离在 `bot.BOT_ENGAGE_RANGE`。
        """
        return self.raw.get("lockon_range")

    def __repr__(self):
        return ("<Weapon %s %s dmg=%d vel=%.0f step=%s>"
                % (self.raw.get("id"), self.raw.get("section", "?"),
                   self.damage, self.velocity, self.handle_step))


class _Store(object):
    """整张表就 75 KB，一次读进来留着。"""

    def __init__(self, path=DATA_PATH):
        self.path = path
        self._table = None
        self._cache = {}

    def table(self):
        if self._table is None:
            self._table = self._read()
        return self._table

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                table = json.load(fp)
        except (IOError, OSError, ValueError):
            # 没有武器表不该让服务端起不来：bot 照样会跑会跳（M3a），
            # 只是不开枪。
            return {"weapons": {}, "preferred": {}, "usable": []}
        if table.get("format") != FORMAT:
            return {"weapons": {}, "preferred": {}, "usable": []}
        return table

    def get(self, ammo_id):
        key = str(ammo_id)
        if key in self._cache:
            return self._cache[key]
        raw = self.table().get("weapons", {}).get(key)
        weapon = None if raw is None else Weapon(raw)
        self._cache[key] = weapon
        return weapon

    def preferred_for(self, character_id):
        """这个角色该给 bot 配哪把枪；没有可用的返回 `None`。

        排序口径在 `tools/weapondata.py` 的 `_preference()` 里：
        **先挑句柄步进小的**（1 最不容易错位），再挑槽位小的（基础枪）。
        """
        ammo = self.table().get("preferred", {}).get(str(int(character_id)))
        return None if ammo is None else self.get(ammo)

    def usable_for(self, character_id):
        """这个角色**所有** bot 能用的武器，按槽位（1/2/3）排好。

        `/gun N M` 就是按这张表挑的 —— 房主给的 M 是**槽位**，
        不是这张表里的下标（角色 1 只有 slot2 / slot3，没有 slot1）。
        """
        usable = set(self.table().get("usable", ()))
        out = []
        for ammo in self.table().get("by_character", {}).get(
                str(int(character_id)), ()):
            if ammo in usable:
                weapon = self.get(ammo)
                if weapon is not None:
                    out.append(weapon)
        return sorted(out, key=lambda w: w.raw.get("slot", 9))

    def slot_for(self, character_id, slot):
        """按**槽位**取这个角色的武器；没有 / 不可用返回 `None`。"""
        for weapon in self.usable_for(character_id):
            if weapon.raw.get("slot") == int(slot):
                return weapon
        return None

    def usable(self):
        return list(self.table().get("usable", ()))

    def count(self):
        return len(self.table().get("weapons", {}))


STORE = _Store()


def get(ammo_id):
    """按 ammo_id 取一把武器；表里没有返回 `None`（**调用方必须能接受**）。"""
    return STORE.get(ammo_id)


def preferred_for(character_id):
    """这个角色的 bot 该用哪把枪；没有返回 `None`（那它就不开火）。"""
    return STORE.preferred_for(character_id)


def usable_for(character_id):
    """这个角色所有能用的武器，按槽位排好（空表 = 它不开火）。"""
    return STORE.usable_for(character_id)


def slot_for(character_id, slot):
    """按槽位（1/2/3）取武器；没有返回 `None`。"""
    return STORE.slot_for(character_id, slot)


def usable():
    return STORE.usable()


def count():
    return STORE.count()
