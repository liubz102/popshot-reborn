#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""palette.py —— 自定义武器的配色规则：两套**方案**，每套「主色掩码 + 撞色掩码 + 若干变体」。

规则语言借 `tools/ch03_skin/recolor.py`（HSV 软掩码 + 按掩码混合），这里只定义**用哪几条**：

    方案 `gold`（X3，母本爆裂 3）  红 h∈[340°,20°] → 黄金 44°；橙/黄 h∈[20°,65°] → 黑曜/宝蓝/银白
    方案 `pink`（X6，母本复合 3）  紫 h∈[245°,320°] → 粉 ≥335°；橙/黄 **同一条掩码** → 绿

★ **撞色那条永远排在主色前面**：先把原来的橙 / 黄挪走，再换主色 —— 反过来的话
  新换上的主色有可能落回「原来的黄」那条掩码里，被再挪一次。

★★ 三个原版系列（D 爆裂 = 红 / R 极速 = 蓝 / F 复合 = 紫）**共用同一批橙黄撞色**，
  所以 `ACCENT_MASK` 两套方案通用，一个字都不用改（实测 F3 命中 8% ~ 17.6%）。

★★★ **不要把 `gold` 的红掩码带到 `pink` 上**。复合 3 的 efx 里确实还留着一批红
  （`#FFFF0000`×54、`#FFC00000`×36、`#FF800000`×17），但那是**三个系列共用的爆炸 / 命中闪光**：
  极速 3（蓝系）里这几个数一模一样（54 / 36 / 17），而爆裂 3 是它们的两倍（红在那儿既是闪光
  又是系列色）。原版做 R3 / F3 时就没动它 —— 我们也不动。

同一套规则用在三种东西上：
    recolor_rgba()   贴图 / 精灵 / 图标（ndarray RGBA）
    recolor_argb()   `.efx` 里的 `<ColorValue>`（ARGB 有符号 int，一个像素）
    has_hue()        某张贴图**值不值得**做副本（一个主色 / 撞色像素都没有的就共用原图）

变体名全局唯一：`A` `B` `C`（gold）· `P1` `P2` `P3`（pink）· `orig`（一个字节不改，只做搬运）。
"""
from __future__ import annotations

import collections
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
sys.path.insert(0, os.path.join(TOOLS, "ch03_skin"))
import recolor as _rc  # noqa: E402  —— ch03_skin/recolor.py 的 HSV 规则引擎

# ---------------------------------------------------------------------------
# 掩码
# ---------------------------------------------------------------------------

#: 红系掩码（爆裂 3 的主色）。`blur:1` 是因为老贴图是抖动过的（相邻像素在两个色相之间来回跳）。
RED_MASK = {"h": [340.0, 20.0], "h_soft": 8.0, "s": [0.35, 1.0], "s_soft": 0.08, "blur": 1}

#: 紫系掩码（复合 3 的主色）。实测手持贴图主峰在 **267°**，高光偏 280~305°、暗部偏 255~262°。
#: ★ 上界必须**过 300**：F3 的 efx 里最常见的紫是 `#FF800080`，色相正好 300.0°，出现 119 次，
#:   `[245,300)` 会整整漏掉它 —— 枪身改成粉了、爆炸还是紫的。
#: ★ 下界取 **235** 而不是 245：F3 的 efx 会引用几张**跨档共用**的蓝紫贴图
#:   （`CH00_Wp01F1_Damage01.dds` 有 46% 的像素在 230~240°、`CH00_Wp02R1_GatherEnergy02.dds` 82%）。
#:   实测 84 张特效贴图改色后的残留：下界 245 → 平均 0.32%（两张超 2%）；**235 → 0.06%（一张都不超）**；
#:   再放到 230 没有额外收益。手持贴图的紫 p5 在 250 以上，放宽这 10° 碰不到它们。
#: ★ `s` 下限取 0.22（红系是 0.35）：紫的暗部饱和度更低，0.35 会把枪身暗面留成紫的。
PURPLE_MASK = {"h": [235.0, 320.0], "h_soft": 8.0, "s": [0.22, 1.0], "s_soft": 0.08, "blur": 1}

#: 橙 / 黄掩码（三个系列共用的撞色）。下限 20° 和红系的上限相接，`h_soft` 让交界处渐变而不是一刀切。
ACCENT_MASK = {"h": [20.0, 65.0], "h_soft": 6.0, "s": [0.40, 1.0], "s_soft": 0.08, "blur": 1}

# ---------------------------------------------------------------------------
# 方案与变体
# ---------------------------------------------------------------------------

#: 黄金：色相 44°。饱和度 ×0.95 保留原来的明暗层次，明度 ×1.08 让金比红亮一点。
#: ★ 这是 **A / B / C 三个老变体**用的那版（`C` 就是 2026-09-19 实装在盘上那套）。
GOLD = {"hue": 44.0, "sat_mul": 0.95, "val_mul": 1.08}

#: 银白撞色（原来写在变体 `C` 里的那个字面量）。G 系三个新变体照用，一个字不改
#: —— 用户 2026-09-20 只说要改黄色。
SILVER = {"sat": 0.06, "val_mul": 1.12, "val_add": 0.08}

# ---------------------------------------------------------------------------
# G 系：真金属黄金（用户 2026-09-20「工地黄 → 闪闪发光的黄金」）
# ---------------------------------------------------------------------------
#
# 上面那条 `GOLD` 是「黑 → 纯黄」的**油漆色阶**。实测盘上那张 `ch00W0141.dds`：
# 色相全程钉在 44°、**62% 的像素 S ≥ 0.9**、最亮只到 `#FFD45C` —— 缺的不是某个色值，
# 是**明暗方向上的色相 + 饱和度变化**（金属的暗部偏红铜、高光塌到近白）。那是条**非线性**
# 曲线，`hue_by_val` / `sat_by_val` 的两端线性插值做不出来 ⇒ 改用 `ramp`（渐变映射，
# 算子在 `ch03_skin/recolor.py`）：按**亮度** Y 在多档色标上取色。
#
# ★★★ 三条硬约束，改下面这几个 dict 之前先看懂：
#
#   1. **键必须是亮度 Y，不能是 HSV 的 V。** 特效贴图的 V 是死的 ——
#      `CH00_Wp01D3_BulletTail00` 的 V 动态范围 literally 是 **0.00**（p5=p50=p95=1.00），
#      它们的形状是靠**饱和度**画的（白热核心 S≈0.25、边缘 S≈1.0）。Y 才有范围：
#      那张 0.30~0.83、`Fire01` 0.45~0.79、模型 `ch00W0131` 0.00~0.66。
#
#   2. **`ramp_range` 必须固定，不许逐图自适应。** 逐图拉伸会把亮度本来就集中在高段的
#      特效贴图整片映射到色标**底端** —— 实测 `Fire01` 一半像素落到 `#251703` / `#452B06`，
#      枪口火光变深褐。这就是 D37「不用 `sat_by_val`」那条坑换了个马甲。
#      钉成 `[0, 0.62]` 之后：枪身（Y 0~0.66）走完整条色阶、弹道 / 火光（Y 0.3~0.88）
#      落在上半段 —— 正好是「金属枪 + 金色光」。
#
#   3. **色标的 t 是按母本亮度的实际分位排的，不是均匀分的。** 爆裂 3 那张手持贴图的
#      Y 分位是 q30≈0.13 / q50≈0.20 / q70≈0.25 / q90≈0.42（Y 对高饱和暗红压得厉害），
#      换算成 t 就是 0.21 / 0.32 / 0.40 / 0.68 ⇒ **浓金那一档必须落在 t≈0.32**，
#      放到 0.5 会让整把枪偏暗发褐（第一版就是这么翻的）。
#
#   4. **`ramp_mix` 留 10%~20% 给上面那套 HSV 结果。** 纯渐变映射是「亮度 → 颜色」的
#      多对一映射，同亮度、不同色相 / 饱和度的像素会被压成同一个色，贴图细节会平掉。

#   5. ★ **饱和度要一路撑到 t≈0.7 才塌**，不能从中段就往下走。第一版在中段就把 S 压到
#      0.76 左右，渲出来是**米色 / 土黄**不是金 —— 在 64×64 的小图上，低饱和读作「脏」而不是
#      「金属」。真正让人读出金属的是**高光那一下的塌陷**（S 从 0.9 骤降到 0.05），
#      不是整条曲线都淡。

#: G1 赤铜金：色标压到 **33°~37°**（偏红铜），中段 S 仍有 0.9+。
#: ★ 顶档故意收在**金奶油** `#FFEFC0`（S≈0.25）而不是近白 —— 弹道 / 火光那些 Y 本来就高的
#:   像素会顶到色标末端，末端是金它们就还是金。这是三档里**特效最保金**的一版，代价是
#:   枪身高光没那么「白得发亮」。
GOLD_COPPER = dict(GOLD, ramp=[
    [0.00, "#150600"], [0.10, "#4A2403"], [0.21, "#8A4E06"], [0.32, "#BE750C"],
    [0.42, "#D88B15"], [0.56, "#EDAA2C"], [0.70, "#F8C64F"], [0.85, "#FFDF8C"],
    [1.00, "#FFEFC0"],
], ramp_range=[0.0, 0.62], ramp_mix=0.82)

#: G2 抛光黄金：色标 **42°~45°**，暗部红铜、中间调浓金（S≈0.96）、只在最上面两档塌成暖白。
GOLD_POLISHED = dict(GOLD, ramp=[
    [0.00, "#130A00"], [0.10, "#4A2D02"], [0.21, "#8F6205"], [0.32, "#C89108"],
    [0.42, "#E0A814"], [0.56, "#F2C62F"], [0.70, "#FCE072"], [0.85, "#FFF3BE"],
    [1.00, "#FFFDF2"],
], ramp_range=[0.0, 0.62], ramp_mix=0.82)

#: G3 镜面亮金：浓金档再往前提（t 0.30）、高光面积更大更白、`ramp_range` 收到 0.60。
#: ★ 代价：弹道 / 火光那些本来就亮的特效会**更偏奶白**，金味没有 G1 / G2 足
#:   —— 这是「枪身最闪」换来的，交给用户挑。
GOLD_MIRROR = dict(GOLD, ramp=[
    [0.00, "#0A0400"], [0.09, "#3D2301"], [0.19, "#9C6A03"], [0.30, "#DDA20A"],
    [0.40, "#F5BE17"], [0.52, "#FFD93E"], [0.64, "#FFEB8C"], [0.78, "#FFF7D2"],
    [1.00, "#FFFFFB"],
], ramp_range=[0.0, 0.60], ramp_mix=0.90)

#: G4 原版金铠：★ 用户 2026-09-20 指的那件 —— 物品 `3010015`「布洛克 黄金铠甲·上衣」，
#: 贴图 `Models/Characters/ch02/ch02B0015.dds`。**这是原版美术自己的金**，量出来的 profile
#: 和 G1~G3 收敛到的那套是一回事（§51 的判据因此得到旁证）：
#:
#:     分位   q10      q25      q50      q75      q90      q100
#:     色     #9C6100  #BD7D00  #CEB208  #FFD75A  #FFEF9C  #FFFFD6
#:     H      37.3     39.7     51.5     45.5     50.3     60.0    ← 随明度往**柠檬**走
#:     S      1.00     1.00     0.96     0.65     0.39     0.16    ← 撑到 q50 之后才塌
#:
#: 和 G1~G3 的两处差别（这就是「原版味」）：① 整体**更偏柠檬**（顶端到 60°，G2 只到 45°）；
#: ② **高光留黄** `#FFFFD6`，不塌成近白。
#: ★ 暗端是**补出来的**：那件铠甲穿在亮色角色身上，最暗只到 `#9C6100`（Y=0.41），
#:   直接拿来用枪身就没有暗部了 —— 按它 37° / S=1.0 的走向往下延了三档。
#: ★ 浓金 `#BD7D00` 落在 t=0.32 = 母本红像素亮度的中位（§51 第 3 条）。
GOLD_ARMOR = dict(GOLD, ramp=[
    [0.00, "#180F00"], [0.10, "#4A2E00"], [0.20, "#8C5C00"], [0.32, "#BD7D00"],
    [0.42, "#D99C00"], [0.55, "#EFBA00"], [0.70, "#F7D252"], [0.84, "#FDEB9C"],
    [0.94, "#FFFBB5"], [1.00, "#FFFFD6"],
], ramp_range=[0.0, 0.62], ramp_mix=0.82)

#: 用户 2026-09-19 第二轮选定的「樱花粉」主色（P5）。第三轮只换绿，粉固定引用它。
PINK_SAKURA = {"hue": 342.0, "sat_mul": 0.45, "val_pow": 0.45, "val_mul": 1.10}

Scheme = collections.namedtuple("Scheme", "zh main_mask accent_mask variants")

#: 变体 = `(中文名, 主色 op, 撞色 op)`。★ 全都渲给用户挑，别在这儿替他定。
#:
#: 粉色目标必须 **≥ 335°**：紫掩码上界 320 + `h_soft` 8 = 328，粉色落在 328 以下的话，
#: 叠规则或重跑时会被自己的主色掩码再吃一次。
SCHEMES = collections.OrderedDict((
    ("gold", Scheme("黄金", RED_MASK, ACCENT_MASK, collections.OrderedDict((
        ("A", ("黄金 + 黑曜", GOLD, {"hue": 220.0, "sat": 0.18, "val_mul": 0.38})),
        ("B", ("黄金 + 宝蓝", GOLD, {"hue": 222.0, "sat_mul": 1.05, "val_mul": 0.95})),
        # ★ `C` 是 2026-09-19 实装、也是用户嫌「像工地黄」的那版。**留着当对照列**
        #   （同 P 批留 `P1` 的做法），出效果图时和 G 系并排渲。
        ("C", ("黄金 + 银白", GOLD, SILVER)),
        # ★★ 2026-09-20 新增的三档真金属黄金，撞色一律沿用 C 的银白（只换黄色）。
        ("G1", ("赤铜金 + 银白", GOLD_COPPER, SILVER)),
        ("G2", ("抛光黄金 + 银白", GOLD_POLISHED, SILVER)),
        ("G3", ("镜面亮金 + 银白", GOLD_MIRROR, SILVER)),
        ("G4", ("原版金铠 + 银白", GOLD_ARMOR, SILVER)),
    )))),
    ("pink", Scheme("粉 + 绿", PURPLE_MASK, ACCENT_MASK, collections.OrderedDict((
        # ★★ **`P7` 是用户 2026-09-19 选定的那版**，`spec.BATCHES["P"].variant` 指着它。
        #    `P1` 留着当基准（Splatoon 2 官方 logo 色），三轮迭代的取舍见 DECISIONS D36。
        #
        # 最贴 Splatoon 2 官方 logo 色 —— **用户否掉：太艳，要「粉嫩、少女感」**。
        # ★ 复合 3 的紫**明度偏低**（v p50 ≈ 0.75），只换色相出来是绛红不是粉 ⇒ 靠 `val_pow`
        #   把中间调提上去（纯 `val_mul` 会把高光全挤到 1.0 压平）。
        #   实测：主色 h267 s.84 v.80 → #E93B6F（s.75 v.91），对上 #ff3f7a 的 h342 s.75 v1.0。
        ("P1", ("霓虹粉 + 荧光绿", {"hue": 342.0, "sat_mul": 0.89, "val_pow": 0.75, "val_mul": 1.08},
                                   {"hue": 88.0, "sat_mul": 0.93, "val_mul": 0.94})),
        # ★ **选定**。粉嫩的关键是**把饱和度压下来**（母本的紫 s≈0.84，P1 的 sat_mul 只压到 0.75，
        #   亮部仍然「艳」而不是「嫩」），同时靠 `val_pow` 把明度抬起来。
        # ★ 不用 `sat_by_val`（按明暗给饱和度）：特效贴图 95% 的像素 v≈1.0，按明度分档对它们
        #   等于全取「最亮那档」，会整体发白；`sat_mul` 保住各自的相对层次，枪身和特效同时成立。
        # ★ 色相 342 和爆裂系的红（h 0~15、s≈0.9）隔开 20° 以上，加上低饱和，一眼是粉不是红。
        #   实测：主色 #FE9EBB（s.38）/ 弹体 #FFA7C1（s.35），对照「少女粉」参考色 #FFB6C1 是 s.29。
        ("P7", ("樱花粉 + 抹茶绿", PINK_SAKURA,
                                   {"hue": 88.0, "sat_mul": 0.50, "val_pow": 0.55, "val_mul": 1.06})),
    )))),
))

#: `变体名 -> 方案名`。变体名全局唯一（`A/B/C` vs `P1/P2/P3`），命令行只报变体就够了。
SCHEME_OF = {v: name for name, s in SCHEMES.items() for v in s.variants}

VARIANTS = ("orig",) + tuple(SCHEME_OF)


def scheme_for(variant):
    """变体 -> `Scheme`；`orig` 返回 `None`。"""
    if variant == "orig":
        return None
    if variant not in SCHEME_OF:
        raise ValueError("没有这个变体：%r（可选 %s）" % (variant, ", ".join(VARIANTS)))
    return SCHEMES[SCHEME_OF[variant]]


def variants_of(scheme_name):
    """某个方案的变体名，按定义顺序。"""
    return tuple(SCHEMES[scheme_name].variants)


def rules_for(variant):
    """`[(规则名, {"mask":…, "op":…}), …]`，顺序就是施加顺序（**撞色在前**）。"""
    scheme = scheme_for(variant)
    if scheme is None:
        return []
    _zh, main_op, accent_op = scheme.variants[variant]
    return [("accent", {"mask": scheme.accent_mask, "op": accent_op}),
            ("main", {"mask": scheme.main_mask, "op": main_op})]


def recolor_rgba(rgba, variant):
    """RGBA uint8 ndarray → 改色后的副本。透明像素照常参与计算（alpha 原样保留）。"""
    rgba = np.asarray(rgba, dtype=np.uint8)
    rules = rules_for(variant)
    if not rules:
        return rgba.copy()
    out, _masks = _rc.recolor(rgba, rules)
    return out


def has_hue(rgba, variant, min_fraction=0.004):
    """这张图里**有没有**本方案要改的颜色（只数不透明的）—— 没有的贴图就不做副本。

    ★★ `variant` 不能省。X6 之前这个函数写死了红∪橙，而复合 3 的 96 张特效贴图里
       **只有 5 张**含红 / 橙 ⇒ 会静默地共用原版紫贴图、一个副本都不做，脚本还照常退出。

    `min_fraction` 是「多少比例的不透明像素命中掩码」的门槛：几个孤立的抖动像素
    不算。0.4% 对 64×64 的图是 16 个像素。
    """
    scheme = scheme_for(variant)
    if scheme is None:
        return False
    rgba = np.asarray(rgba, dtype=np.uint8)
    rgb = rgba[..., :3].astype(np.float64) / 255.0
    alpha = rgba[..., 3] if rgba.shape[-1] == 4 else np.full(rgba.shape[:2], 255, np.uint8)
    opaque = alpha > 16
    if not opaque.any():
        return False
    m = _rc.build_mask(scheme.main_mask, rgb) + _rc.build_mask(scheme.accent_mask, rgb)
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
    return scheme_for(variant).variants[variant][0]
