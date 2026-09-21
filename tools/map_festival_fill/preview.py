#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preview.py —— 把庆典三张图离线合成出来看：「补前 / 补后」整图 + 每处缺口的 2 倍局部对照（X_Mod · X12）。

    python tools\\map_festival_fill\\preview.py                       # 读 logs\\map_festival_fill\\out\\ 里的新图
    python tools\\map_festival_fill\\preview.py --src <目录>          # 换一个来源（比如实装后的 Pack_develop）

输出到 logs\\map_festival_fill\\preview\\：
    <图>_before.png / <图>_after.png       整图
    <图>_<区域>.png                        局部 2 倍，左 before 右 after

## 只是示意，不是引擎渲染

- 绘制顺序按「背景层 → 主层（**按句柄升序**）→ 前景层」合成；主层里谁盖谁引擎怎么排离线定不了
  （见 FINDINGS），实机第一次看图时核对。
- 背景视差层（cat4~7）按原坐标直接贴（引擎有视差缩放，这里没有），出界的跳过。
- 特效只画一个按首层颜色 / 尺寸的柔光点当位置标记（仅主层 / 前景层的，它们是世界坐标），
  背景层的特效坐标是层内坐标，不画。动态效果以实机为准。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import art  # noqa: E402
import mapscan  # noqa: E402

ROOT = art.ROOT
OUT_DIR = os.path.join(ROOT, "logs", "map_festival_fill", "preview")
SRC_DEFAULT = os.path.join(ROOT, "logs", "map_festival_fill", "out")

#: 每张图要放大看的区域（世界坐标 x0, y0, x1, y1）。
REGIONS = {
    "Festival00": {"tower": (480, 220, 1340, 660), "leftwall": (60, 520, 540, 800), "rightwall": (1420, 520, 1900, 800),
                   "top": (0, 0, 1900, 260)},
    "Festival01": {"leftwalls": (0, 480, 700, 1000), "rightwalls": (1250, 420, 2150, 1000), "dragon": (60, 980, 480, 1300),
                   "top": (0, 0, 2300, 300)},
    "Festival02": {"bottom": (0, 1040, 1800, 1370), "rope": (300, 330, 1420, 640), "boat": (620, 740, 1090, 1260),
                   "pierL": (440, 1090, 780, 1300), "pierR": (1000, 1090, 1340, 1300),
                   "rightbuilding": (1220, 340, 1800, 720), "leftwall": (0, 840, 380, 1010), "top": (0, 0, 1800, 260)},
}


def _bg(w, h):
    """夜空底色：上深下略亮的藏青，接近原版 night_10 背景的观感。"""
    img = np.zeros((h, w, 4), dtype=np.float32)
    g = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    img[:, :, 0] = 14 + 10 * g
    img[:, :, 1] = 12 + 12 * g
    img[:, :, 2] = 30 + 26 * g
    img[:, :, 3] = 255
    return img


def _load_sprite(rel, src_dir, allow_new):
    rp = mapscan.real_path(rel)
    if rp is None and allow_new:
        cand = os.path.join(src_dir, rel.replace("Maps/Festival/", "").replace("/", os.sep))
        rp = cand if os.path.isfile(cand) else None
    if rp is None:
        return None
    return np.array(Image.open(rp).convert("RGBA")).astype(np.float32)


def _effect_marker(rel, src_dir):
    """读生成的 efx：首层的首个 ColorValue 和 StartSizeRange，给示意用。"""
    p = os.path.join(src_dir, "Effect", os.path.basename(rel))
    if not os.path.isfile(p):
        return None
    t = open(p, "rb").read().decode("cp949", "replace")
    layer = re.search(r"<EffLayer>(.*?)</EffLayer>", t, re.S)
    if not layer:
        return None
    L = layer.group(1)
    m = re.search(r"<ColorValue>(-?\d+)</ColorValue>", L)
    argb = (int(m.group(1)) & 0xFFFFFFFF) if m else 0xC8FFFFFF
    sz = re.search(r"<StartSizeRange>\s*<X>\s*<Min>([^<]*)</Min>\s*<Max>([^<]*)</Max>", L)
    size = (float(sz.group(1)) + float(sz.group(2))) / 2.0 if sz else 30.0
    return ((argb >> 16) & 255, (argb >> 8) & 255, argb & 255), size


def _blob(img, cx, cy, r, color, alpha=0.55):
    h, w = img.shape[:2]
    y0, y1 = max(0, int(cy - r)), min(h, int(cy + r) + 1)
    x0, x1 = max(0, int(cx - r)), min(w, int(cx + r) + 1)
    if x1 <= x0 or y1 <= y0:
        return
    ys, xs = np.mgrid[y0:y1, x0:x1]
    d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2) / max(r, 1)
    a = np.clip(1 - d, 0, 1) ** 2 * alpha
    sub = img[y0:y1, x0:x1]
    sub[:, :, :3] = np.clip(sub[:, :, :3] + np.array(color, dtype=np.float32) * a[:, :, None], 0, 255)


def compose(name, src_dir, allow_new):
    info = mapscan.parse_full(os.path.join(mapscan.MAPS_DIR, name + ".map"))
    w, h = info["width"], info["height"]
    img = _bg(w, h)
    objs = info["objects"]
    order = [o for o in objs if o["cat"] in (4, 5, 6, 7) and o["type"] == mapscan.LAYER]
    order.sort(key=lambda o: (o["cat"], o["handle"]))
    main = [o for o in objs if o["cat"] == 8 and o["type"] in (mapscan.TERRAIN, mapscan.BREAKABLE)]
    main.sort(key=lambda o: o["handle"])
    front = [o for o in objs if o["cat"] == 10 and o["type"] == mapscan.HIDING]
    front.sort(key=lambda o: o["handle"])
    missing_marks = []
    for o in order + main + front:
        spr = _load_sprite(o["path"], src_dir, allow_new)
        if spr is None:
            if mapscan.real_path(o["path"]) is None:
                missing_marks.append(o)
            continue
        left, top, W, H = mapscan.sprite_window(o, spr.shape[1], spr.shape[0])
        im = art.resize(spr, W, H)
        if o["sx"] < 0:
            im = im[:, ::-1]
        if o["sy"] < 0:
            im = im[::-1, :]
        art.over(img, im, left, top, opacity=0.9 if o["type"] == mapscan.LAYER else 1.0)
    if allow_new:
        for o in objs:
            if o["type"] != mapscan.EFFECT or o["cat"] not in (8, 10) or mapscan.real_path(o["path"]) is not None:
                continue
            mk = _effect_marker(o["path"], src_dir)
            if mk is None:
                continue
            color, size = mk
            _blob(img, o["x"], o["y"], min(60, max(6, size * abs(o["sx"]) * 0.5)), color)
    out = Image.fromarray(np.clip(np.rint(img), 0, 255).astype(np.uint8), "RGBA").convert("RGB")
    if not allow_new:
        d = ImageDraw.Draw(out)
        for o in missing_marks:
            x, y = int(o["x"]), int(o["y"])
            d.line([(x - 8, y), (x + 8, y)], fill=(255, 80, 80), width=2)
            d.line([(x, y - 8), (x, y + 8)], fill=(255, 80, 80), width=2)
            d.text((x + 10, y - 14), os.path.basename(o["path"])[:-4], fill=(255, 120, 120))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--src", default=SRC_DEFAULT, help="新贴图 / 特效所在目录（含 Terrain/ Layer/ Cover/ Breakable/ Effect/）")
    ap.add_argument("--maps", nargs="*", default=list(mapscan.FESTIVAL_MAPS))
    args = ap.parse_args(argv)
    os.makedirs(OUT_DIR, exist_ok=True)
    for name in args.maps:
        before = compose(name, args.src, False)
        after = compose(name, args.src, True)
        before.save(os.path.join(OUT_DIR, name + "_before.png"))
        after.save(os.path.join(OUT_DIR, name + "_after.png"))
        for tag, (x0, y0, x1, y1) in REGIONS.get(name, {}).items():
            a = before.crop((x0, y0, x1, y1)); b = after.crop((x0, y0, x1, y1))
            s = 2 if (x1 - x0) <= 1000 else 1
            a = a.resize((a.width * s, a.height * s), Image.NEAREST); b = b.resize((b.width * s, b.height * s), Image.NEAREST)
            sheet = Image.new("RGB", (a.width + b.width + 12, a.height + 22), (40, 40, 48))
            sheet.paste(a, (0, 22)); sheet.paste(b, (a.width + 12, 22))
            d = ImageDraw.Draw(sheet)
            d.text((4, 4), "%s / %s  before" % (name, tag), fill=(230, 230, 230))
            d.text((a.width + 16, 4), "after", fill=(230, 230, 230))
            sheet.save(os.path.join(OUT_DIR, "%s_%s.png" % (name, tag)))
        print(name, "->", os.path.join(OUT_DIR, name + "_{before,after}.png"), "+", len(REGIONS.get(name, {})), "个局部")
    return 0


if __name__ == "__main__":
    sys.exit(main())
