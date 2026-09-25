#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""equipbonus.py —— 客户端局内 `GetEquipBonus` 的服务端复刻（X_Mod §91）。

## 为什么服务端要自己算

装备加成本来是**纯客户端**的事（V0.3商店 §1）：服务端只用 `0x030b` 告诉每台
机器「这个座位穿了哪些 itemId」，各机查自己本地的 `EquipBonus.ini`。
可受害者一侧的两条 —— 防御加成那道 15%、`[幸运幸存者]` 要看的满血 —— 是在
**射手那台**的伤害函数 `0x4806bf` 里用的，而 bot 没有本机，射手就是服务端
（D28）⇒ 服务端得按同一套规矩把**受害者**的加成算出来。

## 客户端怎么算（`0x407014` → `0x41543e`）

* **分桶**：`桶 = itemId / 1e6 − 1`；`itemId < 1e6`（称号、宠物）是 `−1` 通用桶。
  取值时 **`−1` 桶 + 「座位角色 id」那个桶** 相加（V0.3商店 §1 / §16 / §118）——
  泰尔的铠甲在卡希尔上场时不算。
* **静态值**：`EquipBonus-Chn.ini` 里的数字（`shopdata.bonus()`，已按 `_wtoi` 取整）。
* **Lua**：`0x4133f2` 再加上脚本返回值（`CallEquipBonusFunction`，结果 `_ftol` 截断）。
  中文表里带脚本的只有 12 格（V0.3商店 §53④），这里认受害者一侧用得到的两种形状：

      if session:GetGameType() == G and session:GetPvpMode() OP M then return V else return E end
      return - GetDefaultMaxHp(mychr:GetChrIdx()) * F

  ★ `!=` 能过：这份 Lua 5.0.2 的词法器把 `!` 和 `~` 放在同一个 case 里
    （`0x5b3dce`，后面跟 `=` 就是 `TK_NE`）。
  ★ `mychr` 是**本机**玩家（`0x4133f2` 传的是 `0x409f39()`），不是被算的那个人。
    服务端没有本机 ⇒ 一律按「被算的人自己那台」算：他自己角色的满血。
  认不出的脚本按 0 —— 武器称号那 9 格（`GetLastBulletROHIdx`）是射手一侧的事，
  而 bot 身上什么都不穿。

只用标准库；发布运行时是 CPython 3.8。
"""
from __future__ import annotations

import re

import shopdata

#: `shop_items.json` 里加成的键（`bonus_index` 那张表，idx 2 / 5）。
DEFENSE = "defense"
HP = "hp"

#: 通用桶：`itemId < 1e6` 的物品（称号、宠物）加进每一个角色的桶。
COMMON_BUCKET = -1

#: 房间描述符类型 1 = 普通对战、5 = 天梯（`lobby.SESSION_TYPE_GAME_TYPES`）。
SESSION_TYPE_NORMAL = 1
SESSION_TYPE_LADDER = 5

_CONDITION = re.compile(
    r"^\s*if\s+session:GetGameType\(\)\s*==\s*(-?\d+)\s+and\s+"
    r"session:GetPvpMode\(\)\s*(==|!=|~=)\s*(-?\d+)\s+then\s+return\s+(-?\d+)"
    r"\s+else\s+return\s+(-?\d+)\s+end\s*$")
_MAX_HP_SHARE = re.compile(
    r"^\s*return\s*-\s*GetDefaultMaxHp\(\s*mychr:GetChrIdx\(\)\s*\)\s*\*\s*"
    r"(\d+(?:\.\d+)?|\.\d+)\s*$")


def bucket_of(item_id):
    """这件东西落在哪个桶（`0x414e43` 的 `idiv 0xf4240` 再减 1）。"""
    item_id = int(item_id)
    if item_id < 1000000:
        return COMMON_BUCKET
    return item_id // 1000000 - 1


def counts_for(item_id, character_id):
    """这件东西算不算进 `character_id` 这个角色的加成。"""
    bucket = bucket_of(item_id)
    return bucket == COMMON_BUCKET or bucket == int(character_id)


def pvp_mode(session_type, arguments):
    """`session:GetPvpMode()`（`0x409e0a`）。

    类型 5 返回 5；类型 1 返回 `arguments[1]`（描述符 `+0xc`）；其余 −1。
    """
    kind = int(session_type)
    if kind == SESSION_TYPE_LADDER:
        return SESSION_TYPE_LADDER
    if kind != SESSION_TYPE_NORMAL:
        return -1
    try:
        return int(tuple(arguments)[1])
    except (IndexError, TypeError, ValueError):
        return -1


def lua_value(code, session_type, arguments, own_max_hp):
    """一格 Lua 加成在这一局里返回几；认不出的形状返回 0。"""
    text = str(code or "")
    matched = _CONDITION.match(text)
    if matched:
        game_type, op, mode, then, otherwise = matched.groups()
        mode_now = pvp_mode(session_type, arguments)
        same = mode_now == int(mode)
        hit = (int(session_type) == int(game_type)
               and (same if op == "==" else not same))
        return int(then) if hit else int(otherwise)
    matched = _MAX_HP_SHARE.match(text)
    if matched:
        # `pop<int>` 走 `_ftol`：朝零截断。
        return int(-float(own_max_hp) * float(matched.group(1)))
    return 0


def seat_bonus(item_ids, character_id, key, session_type=SESSION_TYPE_NORMAL,
               arguments=(), own_max_hp=0):
    """`GetEquipBonus(座位, key)`：静态值 + Lua，按桶过滤。

    `item_ids` = 这个座位 `0x030b` 里的装备（`account_store.equipped_items`）。
    `own_max_hp` = 这个座位角色的基础满血（`ChrProps.ini` 的 `ChrHp`），
    只有 `560001` 那一格 Lua 用得到。
    """
    total = 0
    for item_id in item_ids or ():
        if not counts_for(item_id, character_id):
            continue
        item = shopdata.get(item_id)
        if item is None:
            continue
        total += int(item.bonus.get(key, 0) or 0)
        code = item.bonus_lua.get(key)
        if code:
            total += lua_value(code, session_type, arguments, own_max_hp)
    return total
