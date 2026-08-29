#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""weapondata.py —— 从原版 `Data/weapon.ini` 离线提取**武器表**（V0.3 M3b）。

    python tools\\weapondata.py             # 提取到 server\\bot_weapons.json
    python tools\\weapondata.py --dump 1002010   # 顺便打印某几个武器，人工核对

## 为什么要提取（D29）

bot 开火要用到的四件事**全在这一张表里**（§43）：

    Velocity + GravityFactor   算弹道（1 号武器全是 GravityFactor=0 的直射弹）
    Damage                     填 rpExplode +24（收方照抄，不重算，§42）
    CoolingTime                开火间隔 —— ★ 原版数据，不是我拍脑袋的常量
    SplashRange                ★★ 决定**每发消耗几个弹体句柄**

最后一条最要命：句柄错位 = 「子弹飞过去不炸、一滴血不掉」，而且**静默**
（`0x492750` 查不到弹体就整个 return），一局之内不自愈。

## 服务端为什么不直接读 `weapon.ini`

服务端包里**没有** `Pack_decrypt/` —— 那是 368 MB 客户端安装包解出来的资源，
云端根本没有这个文件。和 M4 的地形数据同一个道理（D19 / D29）。

## 产物

`server/bot_weapons.json`（一个文件，约 100 KB，进 git、进两个发布包）：

    weapons        {ammo_id(str): {字段…}}
    by_character   {角色id(str): [ammo_id, …]}   按武器序号排序
    usable         [ammo_id, …]  ★ **句柄步进算得准**的那些，bot 只准用这些

读它的是 `server/weapondata.py`（只用标准库，CPython 3.8 也能跑）。
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

#: 产物格式版本。加/改字段时 +1，加载器拿它判「这份产物是不是我认识的」。
#:
#: 2（会话 14）：节名匹配改成**大小写不敏感**、加 `shots` 字段、
#: `handle_step` 改成按弹体个数算、`usable` 放宽到抛物线和散射武器。
#: ★ 3（会话 19）：`usable` **收紧**成只放行 `CreatingClass=GeneralBullet`（§70）。
#: ★ 4（会话 21）：`usable` 按 `SAFE_CLASSES` 白名单放行（§72 推翻了 §70 的
#:   收紧口径），新增 `slice_time` / `fuse_ticks` 两个字段。
#: ★ 5（会话 22）：新增火墙那几格（`slice_id` / `spawn_count` /
#:   `spawn_life` / `spawn_interval` / `spawn_distance`，§75）。
#: ★ 6（会话 23）：新增 `homing_angle`（追踪弹的转向速率，§77）。
#: ★ 7（会话 24）：新增分裂弹那几格（`slice_count` / `slice_angle` /
#:   `slice_angle_base` / `slice_angle_random`，§81）。
#: ★ 8（会话 37）：新增 `force_ms` / `force_count` —— 地上捡来那把枪
#:   **用多久就还原**（§115）。
FORMAT = 8

#: 节名 `chNNN-MM…`：NNN = 角色 id，MM = 武器序号。
#: ★ 后面还可能跟 `SE` / `D1` / `R1` / `F1` / `a` / `Classic` 之类的后缀 ——
#:   那些是同一把武器的**变体**（强化 / 换皮 / 分支弹药），不是玩家能选的槽位。
#:   只有**光秃秃的 `chNNN-MM`** 才是「角色 NNN 的第 MM 把武器」。
#:
#: ★★ **必须大小写不敏感**（会话 14 修的 bug，§45）：原版这个 ini 里
#: 角色 1 和角色 3 的 1 号武器写的是大写的 `[CH01-01]` / `[CH03-01]`，
#: 其余 46 个节都是小写。区分大小写的话这两把枪整个从表里消失 ——
#: 表现就是用户报的「2 号角色只有 3 号武器能用」（面板 2 = 角色 id 1）。
_SECTION = re.compile(r"^ch(\d+)-(\d+)$", re.IGNORECASE)

#: 只留 bot 用得着的字段：`名字 -> (产物里的键, 转换函数)`。
#: 贴图 / 音效 / 特效路径一律不要 —— 服务端一个都用不上，只会把产物撑大。
_FIELDS = (
    ("Name",            "name",            str),
    ("Type",            "type",            str),
    ("CreatingClass",   "creating_class",  str),
    ("Damage",          "damage",          int),
    ("HeadDamage",      "head_damage",     int),
    ("LegsDamage",      "legs_damage",     int),
    ("SplashDamage",    "splash_damage",   int),
    ("SplashRange",     "splash_range",    int),
    ("Velocity",        "velocity",        float),
    ("MaxVelocity",     "max_velocity",    float),
    ("GravityFactor",   "gravity",         float),
    # ★ `Acceleration`：每 tick 沿飞行方向加多少速，封顶 `MaxVelocity`
    #   （`0x47de6a` = `BulletObj` 的 `vft+0x24`，§49）。7 把武器有它，
    #   不建模的话 `ch100-03`（初速才 3）飞 600 单位要 6.4 秒。
    ("Acceleration",    "acceleration",    float),
    # ★★ `HomingRange` / `HomingAngle`：**追踪弹**（§77）。锁上
    #   `HomingRange` 之内的目标之后，速度矢量每 tick 朝它转
    #   `HomingAngle / 7` 度（`0x47e53a` 的 `fmul [0x693c34]` = 1/7）。
    #   服务端逐 tick 积分复现（`bot._homing_step`）。
    ("HomingRange",     "homing_range",    float),
    ("HomingAngle",     "homing_angle",    float),
    ("PowerControl",    "power_control",   int),
    ("CoolingTime",     "cooling_ms",      int),
    ("LoadingTime",     "loading_ms",      int),
    ("ReloadTime",      "reload_ms",       int),
    ("MagazineCount",   "magazine",        int),
    ("SpreadFrags",     "spread_frags",    int),
    ("SpreadAngle",     "spread_angle",    float),
    ("SpreadRandom",    "spread_random",   int),
    ("LockonRange",     "lockon_range",    float),
    ("LockonPrecision", "lockon_precision", float),
    ("Size",            "size",            float),
    # ★ `SliceTime`：**引信**（毫秒）。分裂类弹体（`AppleGrenade` /
    #   `SeedBomb` / `SliceBullet`）的 Tick 里有一个从 `SliceTime / 32`
    #   倒数的计数器，数到 0 就**在每一台机器上自爆**（§72）。
    ("SliceTime",       "slice_time",      int),
    # ★★ `SliceId`：爆炸之后生出来的那一节武器的 id。`FlamingBottle` 拿它
    #   当**火墙**（`rpSetOnFire` body `+10` 填的就是它，packet_api §5.4d）。
    ("SliceId",         "slice_id",        int),
    # ★★ `SpawnCount`：火墙铺几团火（`weapondef+0xb8`，`0x48990c`，默认 4）。
    #   ★ 它决定 `rpSetOnFire` **在收方吃掉几个弹体句柄**：`2n+1`
    #   （`0x4924a3` 读 `+0xb8`、`0x4924a9` 的 `lea eax,[eax+eax+1]`，§75）。
    ("SpawnCount",      "spawn_count",     int),
    # 火墙的一团火活多少个 tick / 隔几个 tick 生一团 / 每团间隔多少个单位。
    ("SpawnLifeTime",   "spawn_life",      int),
    ("SpawnInterval",   "spawn_interval",  int),
    ("SpawnDistance",   "spawn_distance",  float),
    # ★★ 分裂弹的碎片（§81）：`SliceCount` 是**母弹**那一节写的「炸成几片」
    #   （`[proj+0x33c]`），后面三格是**碎片**那一节的扇形参数
    #   （`weapondef+0xac/0xb0/0xb4`，解析在 `0x48984d` / `0x489887` /
    #   `0x4898c5`，缺省 **160 / 30 / 30** 就写在那三条 `mov ebx` 里）：
    #
    #       第 i 片的角度（度）= SliceAngleBase + SliceAngle × i / (n−1)
    #                            + rand() % SliceAngleRandom − SliceAngleRandom / 2
    #
    #   包里填的是它的**相反数**转弧度（`0x47ca03` 的 `fchs`）。
    ("SliceCount",        "slice_count",         int),
    ("SliceAngle",        "slice_angle",         int),
    ("SliceAngleBase",    "slice_angle_base",    int),
    ("SliceAngleRandom",  "slice_angle_random",  int),
    # ★★ `ForceTime`（毫秒）/ `ForceCount`（发数）：**捡来的枪能用多久**
    #   （V0.3 §115）。整个 ini 里只有四把有：`ch-nuke` 3 发、
    #   `ch-flamer` 15 秒、`ch-water` 10 秒、`ch-powershot`（教学）14 发。
    #   解析在 `0x4899bf` / `0x4899f6`，落进武器记录的 `+0x94` / `+0x98`；
    #   `0x48be09` 每次刷新武器时判「两个都到头了就换回自己那把」。
    ("ForceTime",         "force_ms",            int),
    ("ForceCount",        "force_count",         int),
)


class WeaponDataError(Exception):
    """`weapon.ini` 读不动 / 解不开。"""


# ---------------------------------------------------------------------------
#  解析
# ---------------------------------------------------------------------------

def read_ini(path):
    """读 `weapon.ini`，返回 `OrderedDict{节名: {键: 值}}`。

    ★ 这个文件是 **UTF-16LE 带 BOM** 的（第一行的注释就写着 "unicoded"）。
    没有 BOM 的话退回 CP936 —— 韩文原版和中文代理版的资源都出现过。
    ★ 用手写解析而不是 `configparser`：原版里有 `#CoolingTime=1000` 这种
    「注释掉的键」，也有重复节名，`configparser` 会直接抛异常。
    """
    with open(path, "rb") as fp:
        raw = fp.read()
    if raw[:2] == b"\xff\xfe":
        text = raw.decode("utf-16le", "replace")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw.decode("utf-8-sig", "replace")
    else:
        text = raw.decode("cp936", "replace")

    sections = collections.OrderedDict()
    current = None
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            # 重复节名：后面那份覆盖前面那份（和客户端逐行读进 map 的行为一致）。
            sections[current] = collections.OrderedDict()
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        # ★ `#Key=…` 是原版自己注释掉的键（比如 ch02-03 的 `#CoolingTime`），
        #   照客户端的口径当它不存在 —— 客户端查的是 `CoolingTime`。
        if key.startswith("#"):
            continue
        sections[current][key] = value.strip()
    if not sections:
        raise WeaponDataError("%s 里一个节都没有" % path)
    return sections


def _num(text, cast):
    """把 ini 里的值转成数字。转不动就返回 `None`（当这个字段不存在）。"""
    try:
        if cast is str:
            return text
        return cast(float(text))
    except (TypeError, ValueError):
        return None


def shots_of(fields):
    """★ bot 发这把枪的 `rpFire` 时，`+22` 那一格（`count`）要填几。

    收侧 `OnFire`（`0x491f12`）拿 `count` 这么用（会话 14 逐指令读完，§46）：

        外层轮数 = count / SpreadFrags      （`0x491fa5` 的 `idiv [weapon+0x80]`）
        每轮内层 = SpreadFrags 颗           （`0x491fbb` 的循环上界）

    ⇒ **`count` 小于 `SpreadFrags` 的话一颗子弹都造不出来**（整数除法得 0）。
    所以填 `SpreadFrags`（没有这个字段就是 1）—— 恰好一轮，最省句柄。

    ⚠ 上限：`0x491f41: cmp [ebp+0x20],0x1e; jge 退出` —— `count >= 30` 整包被丢。
    玩家武器最大的 `SpreadFrags` 是 5，离得很远。
    """
    frags = fields.get("spread_frags")
    return int(frags) if frags and frags > 1 else 1


def handle_step_of(fields):
    """★★ 这把武器**每发 `rpFire` 会吃掉几个弹体句柄**，不确定就返回 `None`。

    这是整个 M3b 里最要命的一个数：错了就是「子弹飞过去不炸、一滴血不掉」，
    而且收方**静默丢弃**（`0x492750`），一局之内不自愈（§42）。

    口径（§46 把 §43 的语料结论接上了机制）：

    * `rpFire` 一发造 **`shots_of()` 颗**弹体，每颗注册一次句柄
      （`0x49231e` 的 `call 0x473e7c` 在内层循环里）⇒ 先吃 `shots` 个；
    * **有 `SplashRange`** 的武器爆炸时额外再造一个溅射对象、多吃 **1 个**
      （语料实测「一发前进 2 格」，§43）⇒ 每颗子弹再 +1。

    ⇒ `handle_step = shots × (2 if SplashRange else 1)`。

    ★★ 这个数只有在**「这一发的 `rpExplode` 全部发完之前不发下一发
    `rpFire`」**的前提下才与收方一致 —— 因为溅射那一格到底是开火时分配的
    还是爆炸时分配的，语料分不出来。`bot.py` 的开火闸门保证了这个顺序
    （见那边 `_pending_explosions` 的注释）。
    """
    return shots_of(fields) * (2 if fields.get("splash_range") else 1)


def build_record(section, fields):
    """一节 ini -> 产物里的一条武器记录。不是「玩家能选的武器」就返回 `None`。"""
    ammo = _num(fields.get("Id"), int)
    if ammo is None:
        return None
    match = _SECTION.match(section)
    if match is None:
        # `chNN-01SE` / `chNN-02a` / `chNN-dash` 这类变体：id 照样有，
        # 但它不是「角色的第 N 把武器」。留在表里（万一实机看到了能查），
        # 但不挂进 `by_character`，也不进 `usable`。
        character, slot = None, None
    else:
        character, slot = int(match.group(1)), int(match.group(2))

    record = collections.OrderedDict()
    record["id"] = ammo
    record["section"] = section
    record["character"] = character
    record["slot"] = slot
    for key, name, cast in _FIELDS:
        if key in fields:
            value = _num(fields[key], cast)
            if value is not None:
                record[name] = value
    record["shots"] = shots_of(record)
    record["handle_step"] = handle_step_of(record)
    interval = fire_interval_of(record)
    if interval is not None:
        record["fire_interval_ms"] = interval
    fuse = fuse_ticks_of(record)
    if fuse is not None:
        record["fuse_ticks"] = fuse
    return record


def fire_interval_of(fields):
    """两发之间至少隔多久（毫秒）—— bot 的开火节奏直接用它（D29）。

    * 有 `CoolingTime`（连发武器）-> 就是它；
    * 没有 -> 用 `ReloadTime`。★ 缺 `CoolingTime` 的武器**同时也没有
      `MagazineCount`**（榴弹那一类，打一发装一次），所以「装填时间」
      就是它实际的发射间隔；
    * 两个都没有 -> `None`。

    ⚠ **这里还没有弹匣模型**：连发武器打完 `MagazineCount` 发之后真人要停
    `ReloadTime` 才能接着打，bot 现在不会停 —— 也就是说它的持续输出比真人高。
    M3b 只验句柄，先不管；调难度时（M5）再把弹匣补上。
    """
    cooling = fields.get("cooling_ms")
    if cooling:
        return int(cooling)
    reload_ms = fields.get("reload_ms")
    return int(reload_ms) if reload_ms else None


def build_table(sections):
    """全部节 -> `(weapons, by_character, usable)`。"""
    weapons = collections.OrderedDict()
    by_character = collections.OrderedDict()
    for section, fields in sections.items():
        record = build_record(section, fields)
        if record is None:
            continue
        weapons[str(record["id"])] = record
        if record["character"] is not None:
            by_character.setdefault(str(record["character"]), []).append(record)

    ordered = collections.OrderedDict()
    for character in sorted(by_character, key=int):
        rows = sorted(by_character[character], key=lambda r: r["slot"])
        ordered[character] = [r["id"] for r in rows]

    usable = sorted(r["id"] for r in weapons.values() if _is_usable(r))
    preferred = collections.OrderedDict()
    for character in ordered:
        rows = [r for r in by_character[character] if _is_usable(r)]
        if rows:
            preferred[character] = min(rows, key=_preference)["id"]
    return weapons, ordered, usable, preferred


#: `PowerControl` 的三种取值 = 收侧算初速的三种模式（`0x4920a1` 的三岔口，§47）。
#: 表里没有第四种，真出现了就说明这份 ini 不是我们逆过的那一版。
KNOWN_POWER_MODES = (0, 1, 2)

#: 客户端一个逻辑步长多少毫秒（`[0x6dc528]` = 32，和 §47 的 `TICK_MS` 同源）。
TICK_MS = 32

#: ★★★ **放行的 `CreatingClass` 白名单**（§72 定的口径，推翻了 §70 的
#: 「只放行 `GeneralBullet`」）。
#:
#: 判据是**「在别人那台机器上，它和 `GeneralBullet` 有没有区别」**：
#:
#: 1. 每个子类的 `Tick` 头一句都是 `call 0x47de6a`（基类 Tick）——
#:    **飞行本身完全一样**，子类只加特效位置 / 引信 / 分裂；
#: 2. 分裂弹 / 火墙 / 炮台那些**额外对象的创建点全都套在 `IsMine`
#:    （`0x50d294`）里面** —— 而 bot 的弹体在**任何**一台客户端上
#:    `IsMine` 都是假的（§42 第 3 条），所以那些对象**一个都不会造出来**，
#:    句柄一个都不会多吃。§70 量到的残差是**射手自己那台**的行为，
#:    对 bot 不成立。
#:
#: 剩下的都因为**飞行行为**被挡在外面，不是因为句柄：
#:
#: * `BounceBullet`（`ch106-02` / `ch110-02`）—— 改写了撞地形的响应
#:   （vft `+0x94` / `+0x98`），会**弹墙**接着飞，服务端的落点算不对；
#: * `RasTurret`（`ch107-02` / `ch108-02`）—— Tick 里有一段按
#:   `SliceId` 那把武器的 `SliceCount` 计数、数满就自爆的逻辑，没逆清；
#: * `PlasmaCannon`（`ch109-03`）—— 它的 `vft+0xa8`（`0x4848c9`）里
#:   **自己 `++` 了一次句柄计数器**（`0x484924`），调用路径没证明是
#:   `IsMine` 门内的，宁可不放；
#: * `TotemLauncher`（`ch103-03`）—— 它那把 `Damage=0`，本来也过不了第 3 条。
SAFE_CLASSES = (
    "GeneralBullet",
    # 带引信（`SliceTime`）：飞行和基类一样，但**到点会在每一台上自爆**，
    # 所以还要过 `fuse_ticks` 那一关（见 `_is_usable` 第 7 条）。
    "AppleGrenade",     # ch00-02 / ch98-02
    "SeedBomb",         # ch03-02
    "SliceBullet",      # ch110-03
    # 不带引信：`Tick` 只多设了个特效位置，其余一模一样。
    "FlamingBottle",    # ch01-02 / ch100-02 / ch103-02
    "TimeBomb",         # ch101-02
    "SpiralKnife",      # ch106-03
)

#: 有**引信**的那几类：`Tick` 里 `[obj+0x338]` 从 `SliceTime / 32` 倒数，
#: 数到 0 就调 `vft+0x15c` 自爆（`0x47c952` / `0x48503f` / `0x4851d9`）。
FUSE_CLASSES = ("AppleGrenade", "SeedBomb", "SliceBullet")


def _is_usable(record):
    """bot 允许用这把武器吗。

    ★★ 会话 14 放宽了两条（用户报「所有角色应该都有 3 把武器能用」）：
    抛物线（`GravityFactor > 0`）和散射（`SpreadFrags > 1`）从**排除**
    变成**支持** —— 前者是因为弹道模型逆清了（§47：`tick = 32 ms`、
    `v0` 三种模式、`vy += 1.2 × GravityFactor`），后者是因为 `count` 的
    语义读明白了（§46）。放宽之后 14 个玩家角色全都是三个槽位齐活。

    剩下的条件：

    1. **是角色的正式武器槽**（`chNNN-MM`，不是 `SE` / `D1` 那些变体）——
       玩家选得出来的才有对应的武器模型；
    2. **句柄步进算得出来** —— 否则服务端的记账会和收方的分配器对不上，
       一错全错且静默（D28 的硬约束 1）；
    3. **`Damage > 0`** —— 伤害 0 的武器（`1003030` 图腾发射器）打不动人；
    4. **算得出开火间隔** —— 没有 `CoolingTime` 也没有 `ReloadTime` 的话
       bot 不知道该隔多久打一发；
    5. **`PowerControl` 是我们逆过的三种模式之一** —— 初速公式按它分流，
       出现第四种就说明这份 ini 和逆向结论对不上，宁可不用；
    6. ★★★ **`CreatingClass` 在 `SAFE_CLASSES` 白名单里**（会话 21 改，§72）；
    7. ★ 白名单里**带引信**的那几类还得**算得出引信长度**（`fuse_ticks ≥ 2`）
       —— 引信是 0 的话弹体第一个 tick 就自爆，那把枪根本用不了。

    ## 第 6 条的来历（★ 会话 21 从「只放行 GeneralBullet」放宽，§72）

    会话 19 那一版（§70）是拿语料的**句柄残差**分桶定的：`AppleGrenade`
    残差主峰在 +3、`FlamingBottle` 在 +9/+17 ⇒ 判它们「在收方多吃句柄」。

    ★ **那份残差量的是「射手自己那台机器」**。逐指令读下来，分裂弹 /
    火墙 / 炮台的创建点**全都在 `IsMine`（`0x50d294`）门里面**
    （`0x47c96e` / `0x4829c3` / `0x48505b` / `0x4851f5` / `0x484d1d`
    / `0x4847de`）—— 而 bot 的弹体在**任何一台客户端**上 `IsMine` 都是假的
    （§42 第 3 条：没有一台机器认它是自己的）。⇒ **那些额外对象一个都不会
    被造出来，句柄一个都不会多吃**，`GeneralBullet` 的公式照样成立。

    挡在白名单外的那几类是因为**飞行**对不上，不是因为句柄，见 `SAFE_CLASSES`。
    """
    if not (record.get("character") is not None
            and record.get("handle_step")
            and record.get("damage", 0) > 0
            and record.get("fire_interval_ms")
            and (record.get("power_control") or 0) in KNOWN_POWER_MODES
            and record.get("creating_class") in SAFE_CLASSES):
        return False
    if record.get("creating_class") in FUSE_CLASSES:
        return (record.get("fuse_ticks") or 0) >= 2
    return True


def fuse_ticks_of(fields):
    """带引信的弹体**最多飞几个 tick**；没有引信返回 `None`。

    收方的算法（`0x47c8bd`..`0x47c8d3`，AppleGrenade 的初始化）：

        [obj+0x338] = SliceTime / [0x6dc528]      # 整数除，[0x6dc528] = 32

    然后每个 Tick `dec` 一次，`<= 0` 就调 `vft+0x15c` 自爆
    （`0x47c952`）。⇒ 它能**飞完** `N-1` 个 tick，第 `N` 个 tick 上炸掉，
    `N = SliceTime // 32`。

    ★ 这不是「等 XX 毫秒」的时序阈值（铁律 10）—— 它是**原版数据里写死的
    弹体寿命**，服务端只是照抄。
    """
    if fields.get("creating_class") not in FUSE_CLASSES:
        return None
    slice_time = fields.get("slice_time")
    if not slice_time:
        return None
    return int(slice_time) // TICK_MS


def _preference(record):
    """同一个角色的多把可用武器里，bot 该挑哪一把（越小越优先）。

    **按槽位**：1 号是基础枪 —— 冷却最短、直射、句柄步进最小，最像个陪练，
    也是玩家进游戏默认拿着的那一把。槽位一样（不会发生）再看句柄步进。

    ★ 会话 14 之前这里是「先挑步进小的」，那时抛物线武器还不可用，
    角色 1 / 100 / 103 因此默认拿 3 号重武器 —— 三个槽都能用之后就没必要了。
    """
    return (record.get("slot", 9), record.get("handle_step", 9))


# ---------------------------------------------------------------------------
#  入口
# ---------------------------------------------------------------------------

def find_weapon_ini(explicit=None):
    """找 `Pack_decrypt/Data/weapon.ini`（和 `mapdata.find_pack_root` 同一套口径）。"""
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append(os.path.join(ROOT, "Pack_decrypt", "Data", "weapon.ini"))
    candidates.append(os.path.abspath(os.path.join(
        ROOT, "..", "..", "main", "Pack_decrypt", "Data", "weapon.ini")))
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    raise SystemExit(
        "找不到 weapon.ini。试过：\n  " + "\n  ".join(candidates)
        + "\n用 --ini 指定，例如"
          " --ini D:\\git\\popshot-reborn\\main\\Pack_decrypt\\Data\\weapon.ini")


def main(argv=None):
    ap = argparse.ArgumentParser(description="从原版 weapon.ini 提取武器表")
    ap.add_argument("--ini", help="weapon.ini 的路径")
    ap.add_argument("--out", help="输出文件（默认 server\\bot_weapons.json）")
    ap.add_argument("--dump", nargs="*", metavar="ammo_id",
                    help="打印这几个武器；不给 id 就打印每个角色的 1 号武器")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    path = find_weapon_ini(args.ini)
    out_path = args.out or os.path.join(ROOT, "server", "bot_weapons.json")

    try:
        sections = read_ini(path)
    except WeaponDataError as exc:
        raise SystemExit("解析 %s 失败：%s" % (path, exc))
    weapons, by_character, usable, preferred = build_table(sections)
    if not weapons:
        raise SystemExit("%s 里一把带 Id 的武器都没有" % path)

    table = collections.OrderedDict((
        ("format", FORMAT),
        ("source", os.path.basename(path)),
        ("count", len(weapons)),
        ("by_character", by_character),
        ("preferred", preferred),
        ("usable", usable),
        ("weapons", weapons),
    ))
    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    # ★ `newline="\n"`：不写它的话 Windows 的文本模式会把 `\n` 转成 `\r\n`，
    #   而项目铁律 3 要求 `.json` 一律 **LF 无 BOM**（服务端包要在 Linux 上跑）。
    with open(out_path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(table, fp, ensure_ascii=False, separators=(",", ":"))
        fp.write("\n")

    if args.dump is not None:
        wanted = args.dump or [str(ids[0]) for ids in by_character.values()]
        for ammo in wanted:
            record = weapons.get(str(ammo))
            print("--- %s ---" % ammo)
            if record is None:
                print("    表里没有这个 id")
                continue
            for key, value in record.items():
                print("    %-18s %s" % (key, value))

    if not args.quiet:
        size = os.path.getsize(out_path)
        print("完成：%d 把武器（%d 个角色、%d 把可用）-> %s（%.1f KB）"
              % (len(weapons), len(by_character), len(usable),
                 out_path, size / 1024.0))
        missing = [c for c in by_character if c not in preferred]
        if missing:
            print("   ⚠ 这些角色一把可用武器都没有，它们的 bot 不会开火："
                  + "、".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
