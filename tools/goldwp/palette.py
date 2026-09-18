#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""palette.py —— 自定义武器的配色规则：爆裂系列的**红 → 黄金**，橙 / 黄撞色 → 另一种撞色（X_Mod · X3）。

规则语言借 `tools/ch03_skin/recolor.py`（HSV 软掩码 + 按掩码混合），这里只定义**用哪几条**：

    红系   h∈[340°, 20°]、s ≥ 0.35  → 黄金（h 44°，饱和度略降、明度略提）
    撞色   h∈[20°, 65°]、s ≥ 0.40   → 三个候选之一（A 黑曜 / B 宝蓝 / C 银白）
    灰色金属、白色高光不动（饱和度低，两条掩码都碰不到）。

★ 撞色那条排在**前面**：先把原来的橙 / 黄挪走，再把红换成金 —— 反过来的话
  新换上的金（44°）会被当成「原来的黄」再挪一次，整把枪就没有金了。

同一套规则用在三种东西上：
    recolor_rgba()   贴图 / 精灵 / 图标（ndarray RGBA）
    recolor_argb()   `.efx` 里的 `<ColorValue>`（ARGB 有符号 int，一个像素）
    has_hue()        某张贴图**值不值得**做副本（一个红 / 橙像素都没有的就共用原图）

变体名：`A` 黑曜 · `B` 宝蓝 · `C` 银白 · `orig`（一个字节不改，只做搬运的占位版）。
"""
from __future__ import annotations

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
sys.path.insert(0, os.path.join(TOOLS, "ch03_skin"))
import recolor as _rc  # noqa: E402  —— ch03_skin/recolor.py 的 HSV 规则引擎

#: 黄金：色相 44°。饱和度 ×0.95 保留原来的明暗层次，明度 ×1.08 让金比红亮一点。
GOLD = {"hue": 44.0, "sat_mul": 0.95, "val_mul": 1.08}

#: 撞色候选。键 = 变体名，值 = (中文名, 对「原橙 / 黄」那一条掩码施加的 op)。
#: ★ 三个都渲给用户挑，别在这儿替他定。
CONTRAST = {
    "A": ("黑曜", {"hue": 220.0, "sat": 0.18, "val_mul": 0.38}),
    "B": ("宝蓝", {"hue": 222.0, "sat_mul": 1.05, "val_mul": 0.95}),
    "C": ("银白", {"sat": 0.06, "val_mul": 1.12, "val_add": 0.08}),
}

VARIANTS = ("orig",) + tuple(CONTRAST)

#: 红系掩码。`blur:1` 是因为老贴图是抖动过的（相邻像素在两个色相之间来回跳）。
RED_MASK = {"h": [340.0, 20.0], "h_soft": 8.0, "s": [0.35, 1.0], "s_soft": 0.08, "blur": 1}
#: 橙 / 黄掩码。下限 20° 和红系的上限相接，`h_soft` 让交界处渐变而不是一刀切。
ACCENT_MASK = {"h": [20.0, 65.0], "h_soft": 6.0, "s": [0.40, 1.0], "s_soft": 0.08, "blur": 1}


def rules_for(variant):
    """`[(规则名, {"mask":…, "op":…}), …]`，顺序就是施加顺序（撞色在前）。"""
    if variant == "orig":
        return []
    if variant not in CONTRAST:
        raise ValueError("没有这个变体：%r（可选 %s）" % (variant, ", ".join(VARIANTS)))
    _zh, op = CONTRAST[variant]
    return [("accent", {"mask": ACCENT_MASK, "op": op}),
            ("gold", {"mask": RED_MASK, "op": GOLD})]


def recolor_rgba(rgba, variant):
    """RGBA uint8 ndarray → 改色后的副本。透明像素照常参与计算（alpha 原样保留）。"""
    rgba = np.asarray(rgba, dtype=np.uint8)
    rules = rules_for(variant)
    if not rules:
        return rgba.copy()
    out, _masks = _rc.recolor(rgba, rules)
    return out


def has_hue(rgba, min_fraction=0.004):
    """这张图里**有没有**红 / 橙像素（只数不透明的）—— 没有的贴图就不做副本。

    `min_fraction` 是「多少比例的不透明像素命中掩码」的门槛：几个孤立的抖动像素
    不算。0.4% 对 64×64 的图是 16 个像素。
    """
    rgba = np.asarray(rgba, dtype=np.uint8)
    rgb = rgba[..., :3].astype(np.float64) / 255.0
    alpha = rgba[..., 3] if rgba.shape[-1] == 4 else np.full(rgba.shape[:2], 255, np.uint8)
    opaque = alpha > 16
    if not opaque.any():
        return False
    m = _rc.build_mask(RED_MASK, rgb) + _rc.build_mask(ACCENT_MASK, rgb)
    hit = ((m > 0.5) & opaque).sum()
    return hit >= max(1, int(opaque.sum() * min_fraction))


def recolor_argb(value, variant):
    """`.efx` 的 `<ColorValue>`：ARGB 有符号 32 位 int → 改色后的 int。

    颜色是单个像素，掩码的 `blur` 在 1×1 上没意义，这里直接按纯色相判：
    同一条规则、同样的阈值，只是不模糊。
    """
    rules = rules_for(variant)
    if not rules:
        return int(value)
    argb = int(value) & 0xFFFFFFFF
    a = (argb >> 24) & 0xFF
    r, g, b = (argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF
    pixel = np.array([[[r, g, b, a]]], dtype=np.uint8)
    rgb = pixel[..., :3].astype(np.float64) / 255.0
    for _name, rule in rules:
        mask = dict(rule["mask"])
        mask.pop("blur", None)
        m = _rc.build_mask(mask, rgb)
        if not (m > 1e-3).any():
            continue
        h, s, v = _rc.rgb_to_hsv(rgb)
        new = _rc.apply_op(rule["op"], h, s, v, m)
        rgb = rgb * (1 - m[..., None]) + new * m[..., None]
    r, g, b = (int(x) for x in np.clip(np.rint(rgb[0, 0] * 255.0), 0, 255))
    out = (a << 24) | (r << 16) | (g << 8) | b
    return out - (1 << 32) if out & 0x80000000 else out


def variant_label(variant):
    if variant == "orig":
        return "原色搬运"
    return "黄金 + %s" % CONTRAST[variant][0]
