#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自定义武器的数值与全武器的说明文 —— 管理页「自定义属性」弹窗改的那份配置（X_Mod · X3）。

落在 `server/data/weapons.json`。★ **它是第七份运营配置**（照 `sellprice.py` 那一档，D95 的范式）：
出厂值 = 空覆盖（`default_table()`）、开服由 `shopcfg.ensure_files()` 生成、读盘走 `shopcfg` 那套
热重载 + 「坏文件保留上一份好的」、数据备份回滚和它们同一个勾选项、写锁排在
`shopcfg.all_write_locks()` 那条唯一的加锁顺序里。**没有配置标签页**：编辑入口是物品库卡片上的
「自定义属性」按钮。

    {"format": 1, "serial": 3,
     "custom": {"1920001": {"pve": {"damage": 6, …}, "pvp": {…}}},   ← 只认 9 把自定义武器
     "desc":   {"1120011": "说明文…"}}                                 ← 任何武器都行

## 两条链

* **数值**：`effective(item_id, mode)` = 该模式的覆盖 ∪ 资源包参考值（`weapondata`，即 `weapon.ini`
  里爆裂 3 那一节抄来的数）。它有两个消费者：`shopcfg.item_desc_zh()` 画提示框（只画 PVP 那套），
  和 `build_hook_frame()` 组成 `0x0F01` 推给 bshook 写进客户端内存（PVE / PVP 两套一起下发，
  客户端按「这一局是闯关还是对战」自己挑）。原版武器**不在**这条链上，服务端根本不发它们的 id。
* **说明文**：`desc_of(item_id)` 是提示框第 2 段的覆盖，`item_desc_zh()` 优先用它。

★ 铁律 11：读盘按缺省补齐、幂等；写盘只在校验通过之后（`save_item()`），一个字节不写坏。
"""
from __future__ import annotations

import struct

import shopcfg
import shopdata
import weapondata

FILENAME = shopcfg.WEAPONS_FILENAME
FORMAT = shopcfg.FORMAT

MODE_PVE = "pve"
MODE_PVP = "pvp"
MODES = (MODE_PVE, MODE_PVP)
MODE_ZH = {MODE_PVE: "任务模式（PVE）", MODE_PVP: "对战模式（PVP）"}

#: 可调字段：键 → (中文名, 单位, `weapondata` 记录里的键, 类型, 下限, 上限)。
#: ★ 顺序 = 下发给 bshook 的顺序 = 弹窗里从上到下的顺序，三处共用这一张表。
#: 类型 `int` 落到客户端武器记录的 int32 格，`float` 落到 f32 格（偏移见 hook 侧 `WTAB_FIELDS`）。
#:
#: ★★ 上下限是**管理页能填的范围**，2026-09-19 按原版 `weapon.ini` 的实际分布
#: 收紧（用户原话「不能过于大的离谱」）。右边注释里的「原版 x..y」是全表 228 节
#: 量出来的真实区间，留的余量大致是原版上限的 4~6 倍 —— 够任何合理 Mod 用，
#: 又不至于填出让这一局没法玩的数。**改这里要同步 `test_weaponcfg` 的那条守卫。**
FIELDS = (
    #  键              中文名       单位    weapondata 键     类型   下限     上限
    ("damage",        "身体伤害",   "",     "damage",        int,   0,      500),      # 原版 0..80（ch-nuke）
    ("head_damage",   "爆头伤害",   "",     "head_damage",   int,   0,      500),      # 原版 4..39
    ("legs_damage",   "腿部伤害",   "",     "legs_damage",   int,   0,      500),      # 原版 3..23
    ("splash_damage", "溅射伤害",   "",     "splash_damage", int,   0,      500),      # 原版 0..80（ch-nuke）
    ("splash_range",  "溅射范围",   "",     "splash_range",  int,   0,      1000),     # 原版 0..250（ch-nuke）
    ("magazine",      "弹匣",       "发",   "magazine",      int,   1,      100),      # 原版 1..18；用户指定 1~100
    ("cooling_ms",    "射速间隔",   "毫秒", "cooling_ms",    int,   1,      10000),    # 原版 0..5000
    ("reload_ms",     "换弹",       "毫秒", "reload_ms",     int,   0,      10000),    # 原版 0..5300（ch03-03）
    ("loading_ms",    "切换",       "毫秒", "loading_ms",    int,   0,      10000),    # 原版 0..2000
    ("velocity",      "初速",       "",     "velocity",      float, 0.0,    1000.0),   # 原版 0..230（ch98-03）
    ("max_velocity",  "最大初速",   "",     "max_velocity",  float, 0.0,    1000.0),   # 原版 20..300
    ("gravity",       "重力系数",   "",     "gravity",       float, -20.0,  20.0),     # 原版 0..8；负数 = 上飘
)
FIELD_KEYS = tuple(f[0] for f in FIELDS)

#: ★★ **读盘**放行的范围（比 `FIELDS` 宽），只用于已经躺在 `weapons.json` 里的值。
#:
#: 铁律 11。`shopcfg._load()` 对校验不过的文件是「**整份退回出厂值**」——
#: 要是收紧输入范围的同时也收紧读盘，那么以前在旧范围里存下的一格离谱数值，
#: 会让**整份自定义武器数值一起消失**，而且只在日志里留一行 warning。
#: ⇒ 读盘按这套（= 收紧之前那套）放行，管理页按 `FIELDS` 拦。
#: 存量值照旧能用、能读、能在弹窗里看到；一旦 GM 在弹窗里**保存**，
#: 就必须落进 `FIELDS` 的范围 —— 旧值自然被换掉，不需要迁移脚本。
LEGACY_LIMITS = {
    "damage":        (0, 9999),
    "head_damage":   (0, 9999),
    "legs_damage":   (0, 9999),
    "splash_damage": (0, 9999),
    "splash_range":  (0, 9999),
    "magazine":      (1, 999),
    "cooling_ms":    (1, 60000),
    "reload_ms":     (0, 60000),
    "loading_ms":    (0, 60000),
    "velocity":      (0.0, 10000.0),
    "max_velocity":  (0.0, 10000.0),
    "gravity":       (-100.0, 100.0),
}

#: 弹窗上的分组（只影响画面）。
FIELD_GROUPS = (
    ("伤害", ("damage", "head_damage", "legs_damage")),
    ("溅射", ("splash_damage", "splash_range")),
    ("射击", ("magazine", "cooling_ms", "reload_ms", "loading_ms")),
    ("弹道", ("velocity", "max_velocity", "gravity")),
)

#: 提示框第 1 段的首行（用户 2026-09-19 原话；第二版缩短 —— 第一版「…PVE属性请看GM管理页：」
#: 在 234 px 宽的框里折行）。自定义武器只画 PVP 那套数值。
PVP_ONLY_NOTE = "仅显示PVP属性，PVE的请看管理页"

#: 说明文的上限：提示框第 2 段只有 3 行、232 px 宽（`shopcfg.ITEM_DESC_MAX_LINES_2`）。
DESC_MAX_LINES = shopcfg.ITEM_DESC_MAX_LINES_2
DESC_MAX_CHARS = 90

#: `0x0F01` 载荷的格式号。改布局就 +1，hook 侧 `WTAB_FORMAT` 同步。
#: 2 = 头上多一个「这一局是闯关还是对战」的字节（§42 / D32）。
WIRE_FORMAT = 2

#: `0x0F01` 头里那一格模式。★ `NONE` = 「只更新数据、别施加」——
#: 数值改动**下一局才生效**，不捅正在进行的那一局（用户 2026-09-19 拍板）。
HOOK_MODE_PVE = 0
HOOK_MODE_PVP = 1
HOOK_MODE_NONE = 0xFF
HOOK_MODE_ZH = {HOOK_MODE_PVE: "任务", HOOK_MODE_PVP: "对战", HOOK_MODE_NONE: "不施加"}


class ConfigError(shopcfg.ConfigError):
    pass


def custom_item_ids():
    """9 把自定义武器的物品 id（`shop_items.json` 里 `custom: true` 的），升序。"""
    return sorted(int(item_id) for item_id in shopdata.ids_of_kind("weapon") if is_custom(item_id))


def is_custom(item_id):
    item = shopdata.get(item_id)
    return bool(item and item.custom)


# ---------------------------------------------------------------------------
# 校验 / 默认
# ---------------------------------------------------------------------------

def default_table():
    """出厂值：一条覆盖都没有。"""
    return {"format": FORMAT, "serial": 0, "custom": {}, "desc": {}}


def _as_number(raw, spec, where, strict=True):
    """一格数值。`strict` 决定用哪套范围 —— `FIELDS`（管理页）还是 `LEGACY_LIMITS`（读盘）。"""
    key, label, _unit, _src, cast, low, high = spec
    if not strict:
        low, high = LEGACY_LIMITS[key]
    if isinstance(raw, bool):
        raise ConfigError("%s.%s（%s）要是数字" % (where, key, label))
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise ConfigError("%s.%s（%s）要是数字，不是 %r" % (where, key, label, raw)) from None
    if cast is float and value != value:
        raise ConfigError("%s.%s（%s）不能是 NaN" % (where, key, label))
    if value < low or value > high:
        raise ConfigError("%s.%s（%s）要在 %s ~ %s 之间" % (where, key, label, low, high))
    return value


def validate_params(raw, where="params", strict=True):
    """一套数值（某个模式的覆盖）→ 只留认得的键、类型和范围都对的 dict。空 = 全用参考值。

    `strict=True`（管理页保存）按 `FIELDS` 的范围拦；
    `strict=False`（读盘）按 `LEGACY_LIMITS` 放行 —— 见那张表上面写的理由。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("%s 要是一个对象" % where)
    out = {}
    for spec in FIELDS:
        key = spec[0]
        if key not in raw or raw[key] is None or raw[key] == "":
            continue
        out[key] = _as_number(raw[key], spec, where, strict=strict)
    return out


def validate_desc(raw, where="desc"):
    """一条说明文 → 规范化的字符串（去首尾空白、`|` 换成 `/`、限行数 / 字数）。"""
    if raw is None:
        return ""
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n").strip()
    text = text.replace(shopcfg.DESC_SEPARATOR, "/")
    lines = [line.rstrip() for line in text.split("\n")]
    if len(lines) > DESC_MAX_LINES:
        raise ConfigError("%s 最多 %d 行（提示框下半段只画得下 %d 行）"
                          % (where, DESC_MAX_LINES, DESC_MAX_LINES))
    if len(text) > DESC_MAX_CHARS:
        raise ConfigError("%s 最多 %d 个字" % (where, DESC_MAX_CHARS))
    return "\n".join(lines)


def validate(raw):
    """整份 `weapons.json` → 补齐并校验过的 dict。不合法抛 `ConfigError`。

    ★ 读盘按缺省补齐（缺 `custom` / `desc` / 某个模式都当空），多出来的键丢掉；
    自定义数值只认 `shop_items.json` 里 `custom: true` 的物品 id，说明文认任何武器 id。
    """
    if not isinstance(raw, dict):
        raise ConfigError("weapons.json 的最外层必须是一个对象")
    out = default_table()
    try:
        out["serial"] = max(0, int(raw.get("serial", 0) or 0))
    except (TypeError, ValueError):
        raise ConfigError("serial 要是整数") from None
    custom = raw.get("custom") or {}
    if not isinstance(custom, dict):
        raise ConfigError("custom 要是一个对象（物品 id → 两套数值）")
    for key, value in custom.items():
        try:
            item_id = int(key)
        except (TypeError, ValueError):
            raise ConfigError("custom 里的键 %r 不是物品 id" % (key,)) from None
        if not is_custom(item_id):
            raise ConfigError("custom.%d：不是自定义武器，原版武器的数值不能改" % item_id)
        if not isinstance(value, dict):
            raise ConfigError("custom.%d 要是一个对象" % item_id)
        modes = {}
        for mode in MODES:
            # ★ 读盘一律宽容（`LEGACY_LIMITS`）：存量值不能因为我们后来收紧了
            #   输入范围就让整份配置退回出厂值。管理页那条路走 `save_item()`，
            #   它另外用 `strict=True` 再校一遍。
            modes[mode] = validate_params(value.get(mode), "custom.%d.%s" % (item_id, mode),
                                          strict=False)
        out["custom"][str(item_id)] = modes
    desc = raw.get("desc") or {}
    if not isinstance(desc, dict):
        raise ConfigError("desc 要是一个对象（物品 id → 说明文）")
    for key, value in desc.items():
        try:
            item_id = int(key)
        except (TypeError, ValueError):
            raise ConfigError("desc 里的键 %r 不是物品 id" % (key,)) from None
        item = shopdata.get(item_id)
        if item is None or item.kind != "weapon":
            raise ConfigError("desc.%d：不是武器" % item_id)
        text = validate_desc(value, "desc.%d" % item_id)
        if text:
            out["desc"][str(item_id)] = text
    return out


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

def load(data_dir=None, log=None):
    table, warnings = shopcfg.weapons(data_dir)
    if log:
        for line in warnings:
            log("⚠ [weapons] %s" % line)
    return table


def reference(item_id):
    """资源包里的参考值 `{字段: 数值}`（= `weapon.ini` 里那一节，经 `bot_weapons.json`）。
    ini 里没写的字段就没有这个键。不是武器 / 查不到就空表。"""
    item = shopdata.get(item_id)
    if item is None or not item.ammo_id:
        return {}
    weapon = weapondata.get(item.ammo_id)
    record = weapon.raw if weapon is not None else {}
    out = {}
    for key, _label, _unit, src, cast, _low, _high in FIELDS:
        if src in record and record[src] is not None:
            out[key] = cast(record[src])
    return out


def overrides_of(item_id, mode, table=None, data_dir=None):
    """某模式下管理员填的那几格（没填的键不在里面）。"""
    if table is None:
        table = load(data_dir)
    entry = table.get("custom", {}).get(str(int(item_id))) or {}
    return dict(entry.get(mode) or {})


def effective(item_id, mode, table=None, data_dir=None):
    """某模式下的**有效值** = 覆盖 ∪ 参考。只对自定义武器有意义；原版武器直接回参考值。"""
    values = reference(item_id)
    if is_custom(item_id):
        values.update(overrides_of(item_id, mode, table, data_dir))
    return values


def desc_of(item_id, table=None, data_dir=None):
    """说明文覆盖；没有就空串。"""
    if table is None:
        table = load(data_dir)
    return table.get("desc", {}).get(str(int(item_id)), "")


def effective_weapon_dict(item, mode, table=None, data_dir=None):
    """给 `shopcfg._weapon_lines()` 用的 dict：以 `shop_items.json` 的 `weapon` 为底、盖上该模式的有效值。"""
    base = dict(item.weapon or {})
    for key, value in effective(item.id, mode, table, data_dir).items():
        base[key] = value
    return base


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------

def _lock():
    return shopcfg.write_lock(FILENAME)


def save_item(item_id, params=None, desc=None, data_dir=None, log=None):
    """存一件：`params` = `{"pve": {...}, "pvp": {...}}`（原版武器必须是 `None`），`desc` = 说明文。

    ★ 读 → 改 → 校验 → 原子写，全程持写锁；不合法抛 `ConfigError`，一个字节不写。
    `serial` 每次 +1，是发给客户端的「表变过了」的版本号。返回落盘后的整份表。
    """
    item_id = int(item_id)
    item = shopdata.get(item_id)
    if item is None or item.kind != "weapon":
        raise ConfigError("物品 %d 不是武器" % item_id)
    with _lock():
        table = load(data_dir)
        new = {"format": FORMAT, "serial": int(table.get("serial", 0)) + 1,
               "custom": {k: {m: dict(v.get(m) or {}) for m in MODES} for k, v in table.get("custom", {}).items()},
               "desc": dict(table.get("desc", {}))}
        if params is not None:
            if not item.custom:
                raise ConfigError("原版武器的数值不能改，只能改说明文")
            # ★ **管理页填进来的这一件按 `FIELDS` 的范围严格拦**（下面那发
            #   `validate(new)` 是宽容的 —— 它要同时吃下表里其它件的存量值）。
            #   ⇒ 新填的必须落在合理范围里，旧的照样读得出来、改一次就换掉。
            for mode in MODES:
                validate_params((params or {}).get(mode),
                                "custom.%d.%s" % (item_id, mode), strict=True)
            new["custom"][str(item_id)] = {mode: (params or {}).get(mode) for mode in MODES}
        if desc is not None:
            text = validate_desc(desc, "说明文")
            if text:
                new["desc"][str(item_id)] = text
            else:
                new["desc"].pop(str(item_id), None)
        checked = validate(new)
        shopcfg.write_json(shopcfg.path_of(FILENAME, data_dir), checked)
    shopcfg.invalidate(data_dir)
    if log:
        log("[weapons] 物品 %d 的自定义属性 / 说明文已更新（serial=%d）" % (item_id, checked["serial"]))
    return checked


# ---------------------------------------------------------------------------
# 0x0F01 —— 推给 bshook 的「自定义武器表」
# ---------------------------------------------------------------------------

def _pack_block(values):
    """一个模式的 12 格：`u32 mask + 12 × 4 B`（按 FIELDS 顺序；int32 / float32）。"""
    mask = 0
    body = b""
    for bit, spec in enumerate(FIELDS):
        key, cast = spec[0], spec[4]
        if key in values and values[key] is not None:
            mask |= 1 << bit
            body += struct.pack("<i", int(values[key])) if cast is int else struct.pack("<f", float(values[key]))
        else:
            body += b"\0\0\0\0"
    return struct.pack("<I", mask) + body


def build_hook_frame(table=None, data_dir=None, hook_mode=HOOK_MODE_NONE):
    """`0x0F01` 的载荷：`u16 format, u32 serial, u16 n, u8 模式, n × { i32 武器Id, [PVE] 块, [PVP] 块 }`。

    ★ 每条都把两套**有效值**整个发下去（参考值也带 mask 位）：hook 那边不用知道 ini 里写了什么，
    没在 mask 里的格它写回自己保存的原值（管理员清掉某一格 = 回到参考值）。

    ★★ `hook_mode`（格式 2 新增，§42 / D32）——「这一局是闯关还是对战」：

    * `HOOK_MODE_NONE`（0xFF）：**只送数据，不要施加**。登录后那一发、管理页保存后广播的那一发
      都用它 ⇒ 改了数值不会捅进正在进行的那一局，**下一局才生效**（用户 2026-09-19 拍板）。
    * `HOOK_MODE_PVE` / `HOOK_MODE_PVP`：开局握手 `0x0400` **之前**那一发用它。
      房间类型是服务端自己建的房，它最清楚（铁律 10：让掌握事实的那一方明说）——
      客户端那边 `WeaponTable::Load` 的调用时机根本不等于「开局」，靠它推断模式会慢一拍
      甚至不动（§42）。
    """
    if table is None:
        table = load(data_dir)
    records = []
    for item_id in custom_item_ids():
        item = shopdata.get(item_id)
        if item is None or not item.ammo_id:
            continue
        rec = struct.pack("<i", int(item.ammo_id))
        for mode in MODES:
            rec += _pack_block(effective(item_id, mode, table, data_dir))
        records.append(rec)
    return (struct.pack("<HIHB", WIRE_FORMAT, int(table.get("serial", 0)),
                        len(records), int(hook_mode) & 0xFF)
            + b"".join(records))


RECORD_SIZE = 4 + 2 * (4 + 4 * len(FIELDS))

#: 载荷头的字节数（`u16 format + u32 serial + u16 n + u8 模式`）。hook 侧 `WTAB_HEADER_BYTES` 同步。
HEADER_SIZE = 9


def parse_hook_frame(payload):
    """`build_hook_frame` 的逆（测试和日志用）→ `(format, serial, 模式, [(武器Id, {mode: {字段: 值}})])`。"""
    fmt, serial, count, hook_mode = struct.unpack_from("<HIHB", payload, 0)
    off = HEADER_SIZE
    out = []
    for _ in range(count):
        ammo = struct.unpack_from("<i", payload, off)[0]
        off += 4
        modes = {}
        for mode in MODES:
            mask = struct.unpack_from("<I", payload, off)[0]
            off += 4
            values = {}
            for bit, spec in enumerate(FIELDS):
                key, cast = spec[0], spec[4]
                if mask & (1 << bit):
                    values[key] = (struct.unpack_from("<i", payload, off)[0] if cast is int
                                   else struct.unpack_from("<f", payload, off)[0])
                off += 4
            modes[mode] = values
        out.append((ammo, modes))
    if off != len(payload):
        raise ValueError("0x0F01 载荷长度不对：解到 %d，共 %d" % (off, len(payload)))
    return fmt, serial, hook_mode, out


# ---------------------------------------------------------------------------
# 管理页要的那份视图
# ---------------------------------------------------------------------------

MODE_HEADING = {MODE_PVP: "【对战模式 PVP】", MODE_PVE: "【任务模式 PVE】"}


def mode_lines(item, mode, table=None, data_dir=None):
    """某模式下提示框会画的那几行数值（`shopcfg._weapon_lines` 的口径）。"""
    return shopcfg._weapon_lines(effective_weapon_dict(item, mode, table, data_dir))


def admin_desc(item, table=None, data_dir=None):
    """管理页浮窗 / 弹窗要的说明：自定义武器把 **PVE 和 PVP 两套都列出来**（用户 2026-09-19：
    管理页空间够，两种都显示；游戏内提示框装不下才只画 PVP）。原版武器和游戏里一样。

    ★ 顺序 = `MODES`（**PVE 在前**），和弹窗里两栏「PVE 在左、PVP 在右」同一个方向
    —— 用户 2026-09-19 第三轮：一处左右、一处上下反着来看着别扭。
    `admin.js` 的预览区是同一个顺序，`test_weaponcfg` 钉着两边。"""
    if item is None:
        return ""
    if not getattr(item, "custom", False):
        return shopcfg.item_desc_zh(item, weapons_table=table)
    if table is None:
        table = load(data_dir)
    blocks = []
    for mode in MODES:
        blocks.append(MODE_HEADING[mode])
        blocks.extend(mode_lines(item, mode, table))
    text = "\n".join(blocks)
    note = desc_of(item.id, table)
    return text + shopcfg.DESC_SEPARATOR + note if note else text

def admin_view(item_id, table=None, data_dir=None):
    """弹窗要的一切：字段表（含参考值 / 两套当前值）、说明文、游戏里会显示的预览。"""
    item_id = int(item_id)
    item = shopdata.get(item_id)
    if item is None or item.kind != "weapon":
        raise ConfigError("物品 %d 不是武器" % item_id)
    if table is None:
        table = load(data_dir)
    ref = reference(item_id)
    fields = []
    for key, label, unit, _src, cast, low, high in FIELDS:
        row = {"key": key, "label": label, "unit": unit,
               "type": "float" if cast is float else "int", "min": low, "max": high,
               "reference": ref.get(key)}
        for mode in MODES:
            row[mode] = overrides_of(item_id, mode, table).get(key)
        fields.append(row)
    return {
        "id": item_id,
        "custom": bool(item.custom),
        "name": shopcfg.name_of(shopcfg.items(data_dir)[0], item_id),
        "modes": [{"key": m, "label": MODE_ZH[m]} for m in MODES],
        "groups": [{"label": label, "keys": list(keys)} for label, keys in FIELD_GROUPS],
        "fields": fields,
        "desc": desc_of(item_id, table),
        "desc_max_lines": DESC_MAX_LINES,
        "desc_max_chars": DESC_MAX_CHARS,
        "pvp_only_note": PVP_ONLY_NOTE if item.custom else "",
        "preview": shopcfg.item_desc_zh(item, weapons_table=table),
        # ★ 两套数值行分开给（用户 2026-09-19）：弹窗里 PVP / PVE 各画一块，游戏内那段只有 PVP。
        "lines": {mode: mode_lines(item, mode, table) for mode in MODES} if item.custom else {},
        "serial": int(table.get("serial", 0)),
    }
