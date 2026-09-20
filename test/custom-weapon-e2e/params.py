#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""params.py —— 四套测试参数集的**生成器**。

    python params.py A          # 看 SET-A 长什么样
    python params.py --check    # 自检：范围 / 互异性 / 跨格规则

## 为什么是「生成器」而不是一张表

范围（`min`/`max`）、字段个数、类型全部**现读 `server/weaponcfg.py` 的 `FIELDS`**。
以后加第 15 格或者改某一格的上下界，这里自动跟着走；写死一张表的话，
加格之后测试会**静默少测一格**，而那正是最需要它说话的时候。

## 四套各自要验什么

| 集 | 取值 | 验什么 |
|---|---|---|
| A | 18 把 × 14 格 × 两模式**全填**，36 个取值**互不相同** | 主力：每格都生效；任何串扰（写错记录 / 写错偏移 / PVE 串进 PVP）当场暴露 |
| B | 同样全填，但每格都和 A 不同 | 「局内改了不生效、下一局才生效」的对照 |
| C | 每把**一半格留空**，PVE 留空的格 PVP 填、反之 | 留空回退参考值（mask=0 那条分支）|
| D | **PVE 块全取下界、PVP 块全取上界** | 边界 / 负重力 / `homing_angle=0` 关掉追踪 |

## 取值怎么铺

`值 = 下界 + (序号 + 1) × 步长`，序号 = `武器序号 × 2 + 模式`（0..35），
步长 = `跨度 // 38`（整数）或 `跨度 / 38` 量化到 **0.25 的整数倍**（浮点）。

★ 浮点特意只取 **0.25 的整数倍** —— 这些数在 binary32 里**精确可表示**，
于是「设定值 → JSON → `<f` 打包 → 内存」全程零误差，比对可以用 `==` 而不是容差。
容差会把真的小偏差盖过去。

## 跨格硬规则

`homing_angle > 0` 时 `homing_range` 必须 > 0（`weaponcfg.homing_error()`，
否则管理页 400）。所以 C 集里这两格**一起留空、一起填**，不拆开。
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if os.path.join(ROOT, "server") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "server"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

import weaponcfg  # noqa: E402

MODES = ("pve", "pvp")
#: 浮点取值的量化步长。0.25 的整数倍在 binary32 里精确可表示。
FLOAT_QUANTUM = 0.25
#: 追踪那一对必须同进同退（跨格校验）。
HOMING_PAIR = ("homing_angle", "homing_range")


def custom_ids():
    """18 把自定义武器的物品 id，升序。现读 `shop_items.json`，不写死。"""
    return sorted(weaponcfg.custom_item_ids())


def f32(value):
    """把一个 Python float 压成 float32 再读回来 —— 和内存里那 4 字节同一口径。"""
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def _quantize(value, quantum=FLOAT_QUANTUM):
    return round(value / quantum) * quantum


def _spread(spec, ordinal, total):
    """把第 `ordinal` 个取值铺在 `[low, high]` 里。`total` = 一共要铺几个。"""
    _key, _label, _unit, _src, cast, low, high = spec
    span = high - low
    if cast is int:
        step = max(1, int(span) // (total + 2))
        return min(int(high), int(low) + (ordinal + 1) * step)
    step = _quantize(span / float(total + 2))
    if step < FLOAT_QUANTUM:
        step = FLOAT_QUANTUM
    return f32(min(high, _quantize(low + (ordinal + 1) * step)))


#: 每个字段在序号上再错开这么多。★ 这一格不是装饰：`damage` / `head_damage` /
#: `legs_damage` / `splash_damage` 的范围**一模一样**（0..500），不错开的话同一把
#: 武器这四格会取到同一个值 —— 于是「hook 把爆头伤害写进了身体伤害那个偏移」
#: 这类**字段之间写串**的错误就查不出来了。`velocity` / `max_velocity` /
#: `homing_range`（都是 0..1000 的 float）和 `reload_ms` / `loading_ms` 同理。
#: 取 5：`5 × i mod 36`（i = 0..13）两两不同，所以互异性一点没丢。
FIELD_STRIDE = 5


def _ordinal(weapon_index, mode_index, field_index, shift, total):
    base = weapon_index * len(MODES) + mode_index
    return (base + shift + field_index * FIELD_STRIDE) % total


def _full_set(shift):
    """全填的一套：{物品id: {"pve": {...14格...}, "pvp": {...}}}。"""
    ids = custom_ids()
    total = len(ids) * len(MODES)
    out = {}
    for wi, item_id in enumerate(ids):
        block = {}
        for mi, mode in enumerate(MODES):
            block[mode] = {
                spec[0]: _spread(spec, _ordinal(wi, mi, fi, shift, total), total)
                for fi, spec in enumerate(weaponcfg.FIELDS)
            }
        out[item_id] = block
    return out


def set_a():
    """主力集：全填，36 个取值互异。"""
    return _full_set(0)


def set_b():
    """对照集：同样全填，但每一格都和 A 不同（序号整体挪半圈）。"""
    ids = custom_ids()
    return _full_set(len(ids))          # 挪 18 = 半圈，和 A 逐格不等


def set_c():
    """留空集：每把一半格留空，PVE / PVP 互补。留空用 `None` 表示。

    ★ 追踪那一对同进同退 —— 只填转向不填距离会被跨格校验拒掉（400）。
    """
    base = set_a()
    out = {}
    for wi, item_id in enumerate(sorted(base)):
        block = {}
        for mi, mode in enumerate(MODES):
            values = {}
            for fi, spec in enumerate(weaponcfg.FIELDS):
                key = spec[0]
                # 判据只看「字段位置 + 武器序号」的奇偶，两个模式互为补集。
                pair_index = fi if key not in HOMING_PAIR else weaponcfg.FIELDS.index(
                    next(s for s in weaponcfg.FIELDS if s[0] == HOMING_PAIR[0]))
                keep = ((wi + pair_index) % 2 == mi)
                values[key] = base[item_id][mode][key] if keep else None
            block[mode] = values
        out[item_id] = block
    return out


def set_d():
    """边界集：PVE 块全取下界、PVP 块全取上界。

    下界那套里 `homing_angle = 0` ⇒ 跨格校验不触发（`0` 就是「不追踪」）。
    ⚠ 这一套 18 把的值**全一样**，串扰查不出来 —— 那是 A 集的活儿，别混。
    """
    out = {}
    for item_id in custom_ids():
        low = {}
        high = {}
        for spec in weaponcfg.FIELDS:
            key, _label, _unit, _src, cast, lo, hi = spec
            low[key] = int(lo) if cast is int else f32(lo)
            high[key] = int(hi) if cast is int else f32(hi)
        out[item_id] = {"pve": low, "pvp": high}
    return out


def set_empty():
    """基线集：全部留空 ⇒ 每一格都走参考值 / ini 原值。"""
    return {item_id: {mode: {} for mode in MODES} for item_id in custom_ids()}


SETS = {
    "EMPTY": ("基线（全部留空）", set_empty),
    "A": ("主力（全填，逐格互异）", set_a),
    "B": ("对照（全填，逐格与 A 不同）", set_b),
    "C": ("留空（一半格留空，两模式互补）", set_c),
    "D": ("边界（PVE 下界 / PVP 上界）", set_d),
}


def build(name):
    if name not in SETS:
        raise KeyError("没有这套参数集：%s（有 %s）" % (name, "/".join(SETS)))
    return SETS[name][1]()


# --------------------------------------------------------------------------
# 自检 —— 这一份也是 `run.py --selftest` 的一部分
# --------------------------------------------------------------------------

def check(report=print):
    """把四套集逐条过一遍范围 / 互异性 / 跨格规则。返回问题列表（空 = 全过）。"""
    problems = []
    ids = custom_ids()
    if len(ids) != 18:
        problems.append("自定义武器有 %d 把，不是 18 把 —— 数据变了就先确认是不是预期"
                        % len(ids))

    for name in ("EMPTY", "A", "B", "C", "D"):
        table = build(name)
        if set(table) != set(ids):
            problems.append("%s：覆盖的武器和 custom_item_ids() 对不上" % name)
        for item_id, block in table.items():
            for mode in MODES:
                values = block[mode]
                for spec in weaponcfg.FIELDS:
                    key, _l, _u, _s, cast, lo, hi = spec
                    if key not in values or values[key] is None:
                        continue
                    v = values[key]
                    if cast is int and not isinstance(v, int):
                        problems.append("%s %s/%s %s：应为 int，实为 %r"
                                        % (name, item_id, mode, key, v))
                    if not (lo <= v <= hi):
                        problems.append("%s %s/%s %s=%r 越界 [%s, %s]"
                                        % (name, item_id, mode, key, v, lo, hi))
                    if cast is float and f32(v) != v:
                        problems.append("%s %s/%s %s=%r 不是 binary32 精确值"
                                        % (name, item_id, mode, key, v))
                # 跨格：homing_angle > 0 必须有 homing_range > 0
                angle = values.get(HOMING_PAIR[0])
                reach = values.get(HOMING_PAIR[1])
                if angle:
                    ref = weaponcfg.reference(item_id)
                    eff_reach = reach if reach is not None else ref.get(HOMING_PAIR[1])
                    if not eff_reach:
                        problems.append("%s %s/%s：转向 %s > 0 但距离为空/0，会被 400 拒"
                                        % (name, item_id, mode, angle))

    # A / B 必须逐格不同；A 自己必须逐格互异
    a, b = build("A"), build("B")
    for item_id in ids:
        for mode in MODES:
            for spec in weaponcfg.FIELDS:
                key = spec[0]
                if a[item_id][mode][key] == b[item_id][mode][key]:
                    problems.append("A 和 B 在 %s/%s %s 上撞了值" % (item_id, mode, key))
    for spec in weaponcfg.FIELDS:
        key = spec[0]
        seen = {}
        for item_id in ids:
            for mode in MODES:
                v = a[item_id][mode][key]
                if v in seen:
                    problems.append("A 集 %s：%s/%s 和 %s 撞了值 %r"
                                    % (key, item_id, mode, seen[v], v))
                seen[v] = "%s/%s" % (item_id, mode)

    # ★ 同范围的字段在同一把武器 / 同一模式下也必须取到不同的值 ——
    #   否则「爆头伤害被写进了身体伤害那个偏移」这类**字段之间写串**查不出来。
    same_range = {}
    for spec in weaponcfg.FIELDS:
        same_range.setdefault((spec[4], spec[5], spec[6]), []).append(spec[0])
    for group in same_range.values():
        if len(group) < 2:
            continue
        for item_id in ids:
            for mode in MODES:
                seen = {}
                for key in group:
                    v = a[item_id][mode][key]
                    if v in seen:
                        problems.append("A 集 %s/%s：同范围的 %s 和 %s 取到同一个值 %r"
                                        % (item_id, mode, seen[v], key, v))
                    seen[v] = key

    # C 集：两个模式必须互补，且都不是全空
    c = build("C")
    for item_id in ids:
        pve = {k for k, v in c[item_id]["pve"].items() if v is not None}
        pvp = {k for k, v in c[item_id]["pvp"].items() if v is not None}
        if pve & pvp:
            problems.append("C 集 %s：两个模式都填了 %s" % (item_id, sorted(pve & pvp)))
        if not pve or not pvp:
            problems.append("C 集 %s：有一侧一格都没填" % item_id)

    report("参数集自检：%d 把武器 × %d 格 × %d 模式，问题 %d 条"
           % (len(ids), len(weaponcfg.FIELDS), len(MODES), len(problems)))
    for p in problems:
        report("  ✗ " + p)
    return problems


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "--check":
        return 1 if check() else 0
    name = argv[0].upper()
    table = build(name)
    print("== SET-%s %s ==" % (name, SETS[name][0]))
    keys = [s[0] for s in weaponcfg.FIELDS]
    print("%-9s %-4s %s" % ("物品id", "模式", " ".join("%-9s" % k[:9] for k in keys)))
    for item_id in sorted(table):
        for mode in MODES:
            row = table[item_id][mode]
            print("%-9s %-4s %s" % (item_id, mode,
                                    " ".join("%-9s" % ("·" if row.get(k) is None else row[k])
                                             for k in keys)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
