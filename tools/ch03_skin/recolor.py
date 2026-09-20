#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recolor.py —— 按 `params.json` 把卡希尔（ch01）的默认贴图重着色成爱琳（ch03）（X_Mod · X1 M7 阶段 A）。

    C:\\Python314\\python.exe tools/ch03_skin/recolor.py            # 全部重做（幂等：源永远是 ch01）
    C:\\Python314\\python.exe tools/ch03_skin/recolor.py --only H   # 只做脸（按贴图类字母）
    C:\\Python314\\python.exe tools/ch03_skin/recolor.py --masks    # 额外把每条规则的掩码存成 PNG 供检查

## 为什么源是 ch01 而不是 ch03

ch03 的贴图本来就是 ch01 逐字节克隆的（`mkchar.py`）。每次都从**只读的母本** ch01 出发，
再怎么重跑都不会「在改过的图上再改一遍」；参数改了重跑一次就是新结果。
（本脚本只**读** ch01，一个字节都不写回去。）

## 产物

- `game_patched/Pack_develop/Models/Characters/ch03/<名字>.dds` —— 头 128 字节照抄 ch01 那张，
  只换像素区，长度断言相同（`dds_edit.save_like`）。
- `tools/ch03_skin/src/*.png`、`tools/ch03_skin/out/*.png` —— 改前 / 改后的中间产物，给人 review 用。
- `tools/ch03_skin/masks/*.png`（`--masks` 时）—— 每条规则命中了哪些像素。

## params.json 的写法

```
"textures": { "<ch03 文件名去掉 .dds，可用 * 通配>": ["<规则名>", ...] }
"rules":    { "<规则名>": { "mask": {...}, "op": {...} } }
```
mask（软阈值，各项取乘积）：
  "h": [lo, hi]   色相区间（度，可跨 360，如 [340, 20]）      "h_soft": 过渡宽度（度，默认 6）
  "s": [lo, hi]   饱和度 0..1                                  "s_soft": 默认 0.06
  "v": [lo, hi]   明度 0..1                                    "v_soft": 默认 0.05
  "rect": [x0, y0, x1, y1]  只在这块纹素矩形里生效（含 x0,y0 不含 x1,y1）
  "rect_out": [[...], ...]  这些矩形里不生效
  "blur": n                 算掩码时先把图做 (2n+1)² 均值模糊 —— 这些老贴图是抖动过的
                            （相邻像素在两个色相之间来回跳），不模糊掩码就是筛子，渲出来一身麻点
  "zone":     {"bones": ["Forearm", "Hand"], "grow": 1}
                            只在「由主骨骼名含这些子串的三角形」画到的纹素上生效（按同名 .msh 的
                            UV 光栅化出来，见 mshtool.uv_zone_mask）。★ 这是区分皮肤和布料最可靠的办法：
                            手套贴图里皮肤和米白布的色相只差 5 度，靠颜色分不开，靠骨骼一刀切干净
  "zone_out": {...}         同上，反选
op（作用在 HSV 上，然后按掩码和原图混合）：
  "hue": 目标色相 | "hue_shift": 加多少度 | "hue_by_val": [亮处色相, 暗处色相]
  "sat": 固定 | "sat_mul" | "sat_add" | "sat_by_val": [亮处饱和度, 暗处饱和度]
  "val_mul" | "val_add" | "val_pow"（伽马）| "val_by_val": [亮处明度, 暗处明度]
  "val_range": [v_lo, v_hi]  *_by_val 里「亮/暗」按这个区间归一化（默认取掩码内像素的实际范围）
  "ramp": [[t, "#RRGGBB"], ...]  **渐变映射**：按「进函数时的亮度」Y = .299R+.587G+.114B
                            在色标表上取色（t 递增，首尾建议 0.0 / 1.0）。金属那种「暗部偏红铜
                            → 中间调浓色 → 高光塌到近白」是条**非线性**曲线，`*_by_val` 的
                            两端线性插值做不出来，得靠多档色标
  "ramp_range": [lo, hi]     ★ **必填**，Y 的归一化区间。**故意不给「逐图自适应」的缺省** ——
                            特效贴图的 Y 本来就集中在高段（实测弹道 0.30~0.83、火光 0.45~0.79），
                            逐图拉伸会把它们整片映射到色标底端、枪口火光发褐（同 D37 那条坑）
  "ramp_mix": 0..1           渐变映射和上面 HSV 那套结果混多少（1.0 = 纯映射，默认 1.0）
  "mix": ["#RRGGBB", 比例]   最后在 RGB 里再往某个颜色靠
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
import dds_edit  # noqa: E402
import mshtool  # noqa: E402

CHARS = os.path.join(ROOT, "game_patched", "Pack_develop", "Models", "Characters")
SRC_CH, DST_CH = "ch01", "ch03"

_MESH_CACHE = {}


def zone_mask(zone, tex_name, shape):
    """按骨骼名把贴图分区。`tex_name` 形如 ch03H0000Angry -> 网格 ch01H0000.msh（前 9 个字符）。"""
    mesh_name = tex_name[:2] + SRC_CH[2:] + tex_name[4:9] + ".msh"
    path = os.path.join(CHARS, SRC_CH, mesh_name)
    if path not in _MESH_CACHE:
        _MESH_CACHE[path] = mshtool.load(path)
    m = _MESH_CACHE[path]
    subs = zone["bones"]
    pred = lambda name: any(s in name for s in subs)  # noqa: E731
    return mshtool.uv_zone_mask(m, shape[1], shape[0], pred, grow=int(zone.get("grow", 1)))


# ---------------------------------------------------------------------------
# 颜色空间
# ---------------------------------------------------------------------------

def rgb_to_hsv(rgb):
    """rgb float 0..1 (...,3) -> h(度) s v，各 (...)"""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = rgb.max(-1)
    mn = rgb.min(-1)
    d = mx - mn
    safe = np.where(d > 1e-12, d, 1.0)
    rc, gc, bc = (mx - r) / safe, (mx - g) / safe, (mx - b) / safe
    h = np.where(mx == r, bc - gc, np.where(mx == g, 2.0 + rc - bc, 4.0 + gc - rc))
    h = np.where(d > 1e-12, (h / 6.0) % 1.0, 0.0) * 360.0
    s = np.where(mx > 1e-12, d / np.where(mx > 1e-12, mx, 1.0), 0.0)
    return h, s, mx


def hsv_to_rgb(h, s, v):
    h6 = (h % 360.0) / 60.0
    i = np.floor(h6).astype(int) % 6
    f = h6 - np.floor(h6)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], -1)


def smoothstep(e0, e1, x):
    if e1 <= e0:
        return (x >= e0).astype(np.float64)
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def band(x, lo, hi, soft):
    return smoothstep(lo - soft, lo, x) * (1.0 - smoothstep(hi, hi + soft, x))


def hue_band(h, lo, hi, soft):
    """跨 360 的色相区间。把色相平移到以 lo 为 0 的坐标系里再做普通区间。"""
    width = (hi - lo) % 360.0
    x = (h - lo) % 360.0
    x = np.where(x > width + (360.0 - width) / 2, x - 360.0, x)
    return band(x, 0.0, width, soft)


def hex_rgb(s):
    s = s.lstrip("#")
    return np.array([int(s[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64) / 255.0


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------

def box_blur(rgb, n):
    """(2n+1)² 均值模糊，边缘用镜像补。"""
    if n <= 0:
        return rgb
    pad = np.pad(rgb, ((n, n), (n, n), (0, 0)), mode="reflect")
    out = np.zeros_like(rgb)
    h, w = rgb.shape[:2]
    for dy in range(2 * n + 1):
        for dx in range(2 * n + 1):
            out += pad[dy:dy + h, dx:dx + w]
    return out / float((2 * n + 1) ** 2)


def build_mask(spec, rgb, tex_name=None):
    h, s, v = rgb_to_hsv(box_blur(rgb, int(spec.get("blur", 0))))
    m = np.ones_like(v)
    if "zone" in spec:
        m *= zone_mask(spec["zone"], tex_name, v.shape)
    if "zone_out" in spec:
        m *= ~zone_mask(spec["zone_out"], tex_name, v.shape)
    if "h" in spec:
        m *= hue_band(h, spec["h"][0], spec["h"][1], spec.get("h_soft", 6.0))
    if "s" in spec:
        m *= band(s, spec["s"][0], spec["s"][1], spec.get("s_soft", 0.06))
    if "v" in spec:
        m *= band(v, spec["v"][0], spec["v"][1], spec.get("v_soft", 0.05))
    if "rect" in spec:
        x0, y0, x1, y1 = spec["rect"]
        z = np.zeros_like(m)
        z[y0:y1, x0:x1] = 1.0
        m *= z
    for x0, y0, x1, y1 in spec.get("rect_out", []):
        m[y0:y1, x0:x1] = 0.0
    return m


def ramp_map(stops, y, lo, hi):
    """渐变映射：按亮度 `y` 在色标表 `[[t, "#RRGGBB"], …]` 上线性取色（RGB 空间插值）。"""
    t = np.clip((y - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    ts = np.asarray([float(a) for a, _c in stops], dtype=np.float64)
    cs = np.asarray([hex_rgb(c) for _a, c in stops], dtype=np.float64)
    return np.stack([np.interp(t, ts, cs[:, i]) for i in range(3)], -1)


def apply_op(op, h, s, v, m):
    """返回变换后的 rgb（float 0..1）。`m` 只用来决定 *_by_val 的归一化范围。"""
    h, s, v = h.copy(), s.copy(), v.copy()
    # ★ 渐变映射的键必须取**进函数时**的亮度 —— 在下面任何算子改写 h/s/v 之前算掉。
    #   HSV 往返是精确的，所以 hsv_to_rgb(h,s,v) 就是原像素。
    ramp_luma = None
    if "ramp" in op:
        if "ramp_range" not in op:
            raise ValueError("`ramp` 必须显式给 `ramp_range`：缺省逐图自适应会把亮度本来"
                             "就集中在高段的特效贴图整片映射到色标底端（发褐），见 recolor.py 文件头")
        rgb0 = hsv_to_rgb(h, np.clip(s, 0, 1), np.clip(v, 0, 1))
        ramp_luma = 0.299 * rgb0[..., 0] + 0.587 * rgb0[..., 1] + 0.114 * rgb0[..., 2]
    if "val_range" in op:
        v_lo, v_hi = op["val_range"]
    else:
        sel = m > 0.5
        v_lo, v_hi = (float(v[sel].min()), float(v[sel].max())) if sel.any() else (0.0, 1.0)
    t_dark = np.clip((v_hi - v) / max(v_hi - v_lo, 1e-6), 0.0, 1.0)  # 0=最亮 1=最暗

    if "hue" in op:
        h[:] = op["hue"]
    if "hue_shift" in op:
        h = (h + op["hue_shift"]) % 360.0
    if "hue_by_val" in op:
        a, b = op["hue_by_val"]
        h = a + (b - a) * t_dark
    if "sat" in op:
        s[:] = op["sat"]
    if "sat_mul" in op:
        s = s * op["sat_mul"]
    if "sat_add" in op:
        s = s + op["sat_add"]
    if "sat_by_val" in op:
        a, b = op["sat_by_val"]
        s = a + (b - a) * t_dark
    if "val_pow" in op:
        v = np.power(np.clip(v, 0, 1), op["val_pow"])
    if "val_mul" in op:
        v = v * op["val_mul"]
    if "val_add" in op:
        v = v + op["val_add"]
    if "val_by_val" in op:
        a, b = op["val_by_val"]
        v = a + (b - a) * t_dark
    rgb = hsv_to_rgb(h % 360.0, np.clip(s, 0, 1), np.clip(v, 0, 1))
    if ramp_luma is not None:
        lo, hi = op["ramp_range"]
        amount = float(op.get("ramp_mix", 1.0))
        rgb = rgb * (1 - amount) + ramp_map(op["ramp"], ramp_luma, lo, hi) * amount
    if "mix" in op:
        color, amount = op["mix"]
        rgb = rgb * (1 - amount) + hex_rgb(color) * amount
    return rgb


def recolor(rgba, rules, want_masks=False, tex_name=None):
    rgb = rgba[..., :3].astype(np.float64) / 255.0
    masks = {}
    for name, rule in rules:
        m = build_mask(rule["mask"], rgb, tex_name)
        if want_masks:
            masks[name] = m
        if not (m > 1e-3).any():
            continue
        h, s, v = rgb_to_hsv(rgb)
        new = apply_op(rule["op"], h, s, v, m)
        rgb = rgb * (1 - m[..., None]) + new * m[..., None]
    out = rgba.copy()
    out[..., :3] = np.clip(np.rint(rgb * 255.0), 0, 255).astype(np.uint8)
    return out, masks


# ---------------------------------------------------------------------------

def main(argv=None):
    from PIL import Image

    ap = argparse.ArgumentParser()
    ap.add_argument("--params", default=os.path.join(HERE, "params.json"))
    ap.add_argument("--only", help="只处理文件名里含这个子串的贴图，如 H0000")
    ap.add_argument("--masks", action="store_true", help="把每条规则的掩码存成 PNG")
    ap.add_argument("--dry-run", action="store_true", help="只写 PNG 中间产物，不写 .dds")
    args = ap.parse_args(argv)

    params = json.load(open(args.params, encoding="utf-8"))
    rules_all = params["rules"]
    src_dir = os.path.join(CHARS, SRC_CH)
    dst_dir = os.path.join(CHARS, DST_CH)
    for sub in ("src", "out", "masks"):
        os.makedirs(os.path.join(HERE, sub), exist_ok=True)

    # 展开通配：ch03 目录里所有 .dds，匹配 params 里的键
    names = sorted(n[:-4] for n in os.listdir(dst_dir) if n.lower().endswith(".dds"))
    plan = []
    for pattern, rule_names in params["textures"].items():
        hits = [n for n in names if fnmatch.fnmatchcase(n, pattern)]
        if not hits:
            raise SystemExit("params 里的 %r 在 %s 没匹配到任何贴图" % (pattern, dst_dir))
        for n in hits:
            plan.append((n, rule_names))
    if args.only:
        plan = [(n, r) for n, r in plan if args.only in n]

    written = unchanged = 0
    for name, rule_names in plan:
        src_name = name[:2] + SRC_CH[2:] + name[4:]  # ch03X.. -> ch01X..（只改第 3、4 位）
        src_path = os.path.join(src_dir, src_name + ".dds")
        dst_path = os.path.join(dst_dir, name + ".dds")
        _, rgba = dds_edit.load_rgba(src_path)
        rules = [(rn, rules_all[rn]) for rn in rule_names]
        out, masks = recolor(rgba, rules, want_masks=args.masks, tex_name=name)
        Image.fromarray(rgba, "RGBA").save(os.path.join(HERE, "src", name + ".png"))
        Image.fromarray(out, "RGBA").save(os.path.join(HERE, "out", name + ".png"))
        for rn, m in masks.items():
            Image.fromarray((m * 255).astype(np.uint8), "L").save(
                os.path.join(HERE, "masks", "%s.%s.png" % (name, rn)))
        if args.dry_run:
            continue
        blob = dds_edit.encode(open(src_path, "rb").read(), out)
        try:
            same = open(dst_path, "rb").read() == blob
        except OSError:
            same = False
        if same:
            unchanged += 1
            continue
        with open(dst_path, "wb") as fh:
            fh.write(blob)
        written += 1
        print("  %-24s %s -> %d B" % (name + ".dds", ",".join(rule_names), len(blob)))
    print("完成：%d 张写入 / %d 张未变（共 %d 张）" % (written, unchanged, len(plan)))
    return 0


if __name__ == "__main__":
    # ★ 把 stdout / stderr 钉成 utf-8。调用方一**捕获**输出（管道 / 赋值给变量），
    #   CPython 就发现 stdout 不是控制台、改用 `GetACP()` = cp936 —— 中文按 GBK 落进
    #   管道而上游按 utf-8 解（满屏乱码），`✓` 这种 cp936 编不出来的字符更是直接
    #   `UnicodeEncodeError` 把进程带崩。完整来龙去脉见 `tools/pkn.py` 的 main()。
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
