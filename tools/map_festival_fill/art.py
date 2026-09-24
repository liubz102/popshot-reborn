#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""art.py —— `build.py` 用的像素工具：读庆典素材、缩放 / 平铺 / 叠加、几种程序化材质（X_Mod · X12）。

全部基于 numpy float32 的 RGBA（0..255，未预乘），最后 `to_png` 才转 uint8。
只在开发机跑（Pillow + numpy），不进发布包、不进服务端。
"""
from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PACK = os.path.join(ROOT, "game_patched", "Pack_develop")
FESTIVAL = os.path.join(PACK, "Maps", "Festival")

#: 庆典地图的几个基准色（从 tr_x_07 / tr_x_12 / tr_x_10 实测取平均得到）。
RED_LACQUER = (156.0, 47.0, 44.0)
RED_DARK = (96.0, 18.0, 18.0)
RED_LIGHT = (214.0, 78.0, 66.0)
GOLD = (222.0, 168.0, 62.0)
GOLD_LIGHT = (250.0, 222.0, 128.0)
GOLD_DARK = (128.0, 84.0, 24.0)


# ---------------------------------------------------------------------------
#  读 / 写
# ---------------------------------------------------------------------------

def load(rel):
    """`Terrain/tr_x_02.png` -> float32 RGBA (h, w, 4)。"""
    return np.array(Image.open(os.path.join(FESTIVAL, rel)).convert("RGBA")).astype(np.float32)


def new(w, h, color=None):
    img = np.zeros((h, w, 4), dtype=np.float32)
    if color is not None:
        img[:, :, :3] = color
    return img


def to_png(img, path):
    """存成 8-bit RGBA PNG（和庆典现有贴图同一种格式）。"""
    out = np.clip(np.rint(img), 0, 255).astype(np.uint8)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.fromarray(out, "RGBA").save(path, optimize=True)


def resize(img, w, h, smooth=True):
    im = Image.fromarray(np.clip(np.rint(img), 0, 255).astype(np.uint8), "RGBA")
    im = im.resize((max(1, int(w)), max(1, int(h))), Image.LANCZOS if smooth else Image.NEAREST)
    return np.array(im).astype(np.float32)


def scale(img, s):
    h, w = img.shape[:2]
    return resize(img, round(w * s), round(h * s))


def flip_h(img):
    return img[:, ::-1].copy()


def crop(img, x0, y0, x1, y1):
    return img[y0:y1, x0:x1].copy()


# ---------------------------------------------------------------------------
#  合成
# ---------------------------------------------------------------------------

def over(dst, src, x, y, opacity=1.0):
    """把 src 按 src-over 叠到 dst 的 (x, y)（左上角，可为负、可出界）。原地改 dst 并返回它。"""
    H, W = dst.shape[:2]
    h, w = src.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return dst
    s = src[y0 - y:y1 - y, x0 - x:x1 - x]
    d = dst[y0:y1, x0:x1]
    sa = (s[:, :, 3:4] / 255.0) * opacity
    da = d[:, :, 3:4] / 255.0
    oa = sa + da * (1 - sa)
    rgb = (s[:, :, :3] * sa + d[:, :, :3] * da * (1 - sa)) / np.maximum(oa, 1e-6)
    d[:, :, :3] = np.where(oa > 0, rgb, d[:, :, :3])
    d[:, :, 3:4] = oa * 255.0
    return dst


def apply_mask(img, mask, soften=1.0):
    """按 0/1 掩码裁 alpha（掩码之外全透明）；`soften` > 0 时给掩码边缘一点抗锯齿。"""
    m = mask.astype(np.float32)
    if soften > 0:
        m = np.array(Image.fromarray((m * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(soften))).astype(np.float32) / 255.0
        m = np.where(mask, np.maximum(m, 0.6), m * 0.9)
    img[:, :, 3] *= m
    return img


def open_mask(mask, r=1):
    """0/1 掩码的形态学开运算（先腐蚀再膨胀），用来抹掉孤立噪点。"""
    im = Image.fromarray((mask.astype(np.uint8)) * 255)
    k = 2 * r + 1
    im = im.filter(ImageFilter.MinFilter(k)).filter(ImageFilter.MaxFilter(k))
    return np.array(im) > 127


def tint(img, rgb_mul, alpha_mul=1.0):
    out = img.copy()
    out[:, :, :3] *= np.array(rgb_mul, dtype=np.float32)
    out[:, :, 3] *= alpha_mul
    return out


def fade_bottom(img, rows):
    """底部 rows 行 alpha 线性淡出（接水面 / 接空气用）。"""
    h = img.shape[0]
    ramp = np.linspace(1.0, 0.0, rows, dtype=np.float32)
    img[h - rows:, :, 3] *= ramp[:, None]
    return img


# ---------------------------------------------------------------------------
#  程序化材质（都从素材采样，不凭空造色）
# ---------------------------------------------------------------------------

def lacquer_panel(w, h, smooth=False, rows=(4, 94)):
    """红漆面板：取 tr_x_07 红鼓的鼓身平铺，再加一点纵向明暗，避免一整片平色。

    鼓身是竖向板条拼的桶，拉宽后板条缝会显出来；`smooth=True` 把每行横向抹平（只留纵向明暗），
    给坡道这种要自己画板缝的地方用。
    `rows` 取鼓身的哪几行：缺省 4..94 含底部那截青绿反光（方箱 / 浮箱当基座正好）；
    要**纯红**（船身）传 (4, 74) —— 2026-09-21 实机看船身发绿就是这截反光落进了船身。
    """
    drum = load("Terrain/tr_x_07.png")
    body = crop(drum, 60, rows[0], 200, rows[1])   # 鼓身的纯红漆区
    if smooth:
        flat = body.mean(axis=1, keepdims=True)  # 每行的平均色
        body = body * 0.15 + np.broadcast_to(flat, body.shape) * 0.85
    tile = resize(body, w, max(h, 8))
    yy = np.linspace(-1, 1, tile.shape[0], dtype=np.float32)[:, None, None]
    xx = np.linspace(-1, 1, tile.shape[1], dtype=np.float32)[None, :, None]
    shade = 1.0 + 0.14 * np.cos(yy * np.pi / 2) - 0.10 * (xx ** 2)
    tile[:, :, :3] = np.clip(tile[:, :, :3] * shade, 0, 255)
    tile[:, :, 3] = 255
    return tile[:h, :w]


def gold_band(w, h, rivets=True, rivet_step=18):
    """金色包边条（横向）：取 tr_x_07 鼓端那条金箍转 90 度平铺，再点铆钉。"""
    drum = load("Terrain/tr_x_07.png")
    band = crop(drum, 27, 6, 37, 92)            # 10x86 的竖向金箍
    band = np.transpose(band, (1, 0, 2))         # 转成横向 86x10
    band = resize(band, w, h)
    band[:, :, 3] = 255
    # 上下各一条暗边，让它像真的包边
    band[0, :, :3] *= 0.55
    band[-1, :, :3] *= 0.55
    if rivets and h >= 6:
        r = max(1.5, h * 0.22)
        cy = h / 2.0
        for cx in np.arange(rivet_step / 2.0, w, rivet_step):
            disc(band, cx, cy, r, GOLD_LIGHT, shadow=True)
    return band


def disc(img, cx, cy, r, color, shadow=False, alpha=1.0):
    """抗锯齿圆点（铆钉 / 灯笼挂环）。"""
    h, w = img.shape[:2]
    y0, y1 = max(0, int(cy - r - 2)), min(h, int(cy + r + 3))
    x0, x1 = max(0, int(cx - r - 2)), min(w, int(cx + r + 3))
    if x1 <= x0 or y1 <= y0:
        return img
    ys, xs = np.mgrid[y0:y1, x0:x1]
    d = np.sqrt((xs + 0.5 - cx) ** 2 + (ys + 0.5 - cy) ** 2)
    cov = np.clip(r + 0.5 - d, 0, 1) * alpha
    col = np.array(color, dtype=np.float32)
    if shadow:
        # 右下暗、左上亮，一点点体积感
        light = np.clip(1.0 + 0.35 * ((cx - xs) + (cy - ys)) / max(r, 1), 0.55, 1.35)
        col = col[None, None, :] * light[:, :, None]
    sub = img[y0:y1, x0:x1]
    a = cov[:, :, None]
    sub[:, :, :3] = sub[:, :, :3] * (1 - a) + col * a
    sub[:, :, 3] = np.maximum(sub[:, :, 3], cov * 255)
    return img


def hline(img, y, x0, x1, color, thickness=1, alpha=1.0):
    y0, y1 = max(0, int(y)), min(img.shape[0], int(y) + thickness)
    x0, x1 = max(0, int(x0)), min(img.shape[1], int(x1))
    if y1 > y0 and x1 > x0:
        sub = img[y0:y1, x0:x1]
        sub[:, :, :3] = sub[:, :, :3] * (1 - alpha) + np.array(color, dtype=np.float32) * alpha
        sub[:, :, 3] = np.maximum(sub[:, :, 3], alpha * 255)
    return img


def vline(img, x, y0, y1, color, thickness=1, alpha=1.0):
    x0, x1 = max(0, int(x)), min(img.shape[1], int(x) + thickness)
    y0, y1 = max(0, int(y0)), min(img.shape[0], int(y1))
    if y1 > y0 and x1 > x0:
        sub = img[y0:y1, x0:x1]
        sub[:, :, :3] = sub[:, :, :3] * (1 - alpha) + np.array(color, dtype=np.float32) * alpha
        sub[:, :, 3] = np.maximum(sub[:, :, 3], alpha * 255)
    return img


def deck_strip(w, deck_h, foam_h):
    """码头栈桥的一段横条：tr_x_02（红瓦 + 云边地面条）的「梁 + 瓦当」压到 deck_h 行，
    下面接 foam_h 行云边并淡出。**横向按 6 个周期（240 px）重采样到 w**，所以 w=246 时首尾无缝。"""
    src = load("Terrain/tr_x_02.png")
    six = crop(src, 0, 0, 240, src.shape[0])     # 6 个周期
    six = resize(six, w, src.shape[0])
    top = resize(crop(six, 0, 0, w, 72), w, deck_h)      # 梁 + 瓦当
    foam = resize(crop(six, 0, 72, w, 88), w, foam_h)    # 云边
    out = new(w, deck_h + foam_h)
    over(out, top, 0, 0)
    over(out, foam, 0, deck_h)
    fade_bottom(out, max(1, foam_h // 2))
    return out
