#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py —— 生成庆典地图缺失的 22 张贴图 + 28 个特效（X_Mod · X12）。**幂等**，跑几遍结果一样。

    python tools\\map_festival_fill\\build.py              # 写到 logs\\map_festival_fill\\out\\（看效果用，不进游戏）
    python tools\\map_festival_fill\\build.py --install    # 写进 game_patched\\Pack_develop\\Maps\\Festival\\，然后跑 tools\\build-pack.bat

## 形状从哪来

- 有碰撞的地形（`tr_x_28/29/30/31/33`）：`mapscan.py` 从 `.map` 的碰撞位图减去现存精灵得到的**残差**，
  反变换到精灵局部坐标（以中心为原点）—— 画出来的东西和空气墙严丝合缝。
- 有掩码的（`tr_x_32`、`co_07/08/12/13`）：`.map` 尾部掩码表里的逐像素轮廓和画布尺寸，**尺寸必须一模一样**
  （`BreakableObj` 命中判定按半尺寸换局部坐标）。
- 无碰撞的装饰（`tr_x_34/35/37`、`la_19/20`）：保守猜测，用现有素材拼。
- `Cover/tr_x_*`：原版就是 Terrain 同名逐字节复制。

## 美术怎么来

全部从 `Maps/Festival/` 现有素材采样 / 缩放 / 拼贴（铁律 13：先搜再画），程序化只用在没有素材的地方
（绳桥、船身、坡道面板）。基准色和材质函数在 `art.py`。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import art  # noqa: E402
import effects  # noqa: E402
import mapscan  # noqa: E402

ROOT = art.ROOT
OUT_DEFAULT = os.path.join(ROOT, "logs", "map_festival_fill", "out")
INSTALL_DIR = art.FESTIVAL

#: Cover 副本：原版 `Cover/tr_x_08.png` 和 `Terrain/tr_x_08.png` 逐字节相同，缺的 7 张照抄。
COVER_COPIES = ("tr_x_10", "tr_x_12", "tr_x_13", "tr_x_14", "tr_x_15", "tr_x_16", "tr_x_17")


class Ctx:
    """三张图的分析结果 + 按需取的局部轮廓。"""

    def __init__(self):
        self.maps = {n: mapscan.analyse(n) for n in mapscan.FESTIVAL_MAPS}

    def obj(self, map_name, base, handle=None):
        for o in self.maps[map_name]["objects"]:
            if o["type"] == mapscan.TERRAIN and os.path.basename(o["path"])[:-4] == base and (handle is None or o["handle"] == handle):
                return o
        raise KeyError("%s 里没有 %s" % (map_name, base))

    def mask(self, map_name, rel):
        return mapscan.cells_of(self.maps[map_name]["masks"][rel])

    def local(self, map_name, o, u0, u1, v0, v1):
        """残差反变换到局部坐标（以中心为原点），返回 (sil, known)，数组下标 = (v - v0, u - u0)。"""
        info = self.maps[map_name]
        sil, known, (R, _) = mapscan.local_silhouette(info, o, info["_resid"], info["_cells"], radius=760)
        return sil[R + v0:R + v1, R + u0:R + u1], known[R + v0:R + v1, R + u0:R + u1]


# ---------------------------------------------------------------------------
#  地形：有碰撞的
# ---------------------------------------------------------------------------

def tr_x_28(ctx):
    """码头栈桥地板块 246×100：中心行 50；残差实心 v -16..+41 ⇒ 甲板 rows 34..91，其下 8 行云边淡出。"""
    o = ctx.obj("Festival02", "tr_x_28", 320)
    sil, _ = ctx.local("Festival02", o, -123, 123, -50, 50)
    rows = np.nonzero(sil.any(axis=1))[0]
    top, bottom = int(rows.min()), int(rows.max())          # 期望 34 / 91
    deck_h = bottom - top + 1
    img = art.new(246, 100)
    strip = art.deck_strip(246, deck_h, 8)
    art.over(img, strip, 0, top)
    return img


def deck_phase_strip(world_x0, world_x1, deck_h, foam_h, sx=1.0):
    """按世界坐标取一段码头条纹（周期 246，相位以 tr_x_28@439.2 的左边 316.2 为 0），
    横向预拉伸 1/sx 以便被引擎按 sx 缩回去后和相邻地板块接上。"""
    base = art.deck_strip(246, deck_h, foam_h)
    n = int(round((world_x1 - world_x0) / sx))
    xs = (np.arange(n) * sx + world_x0 - 316.2) % 246.0
    idx = np.clip(np.rint(xs).astype(int), 0, 245)
    return base[:, idx]


def tr_x_29(ctx):
    """码头端头坡道（左实例 s=(0.8,1)，右实例镜像）：画布 170×124，中心 (85, 62)。"""
    o = ctx.obj("Festival02", "tr_x_29", 300)
    W, H = 170, 124
    sil, known = ctx.local("Festival02", o, -85, 85, -62, 62)
    solid = sil == 2
    # 未知区（被现存精灵盖住）按上下邻居补：坡道是从上沿到底一整块实心
    top = np.full(W, -1)
    for u in range(W):
        col = np.nonzero(solid[:, u])[0]
        if len(col):
            top[u] = col.min()
    # 平滑上沿（残差是 1 px 台阶）并把没测到的列按邻居插值
    valid = top >= 0
    if valid.sum() < 10:
        raise RuntimeError("tr_x_29 残差太少")
    xs = np.arange(W)
    top_f = np.interp(xs, xs[valid], top[valid]).astype(np.float32)
    k = 5
    top_s = np.convolve(np.pad(top_f, (k, k), mode="edge"), np.ones(2 * k + 1) / (2 * k + 1), mode="valid")
    deck_row = 62 + int(round(1206 - o["y"]))              # 世界 y=1206 是地板面
    bottom_row = 62 + int(round(1264 - o["y"]))            # 地板下沿
    img = art.new(W, H)
    # 1) 地板部分（deck_row .. bottom_row）：和地板块同一条纹，按世界相位取
    deck_h = bottom_row - deck_row
    strip = deck_phase_strip(o["x"] - 85 * abs(o["sx"]), o["x"] + 85 * abs(o["sx"]), deck_h, 8, sx=abs(o["sx"]))
    strip = art.resize(strip, W, strip.shape[0])
    art.over(img, strip, 0, deck_row)
    # 2) 坡道体：红漆面板（抹平桶纹）贴到上沿曲线下面直到地板面，木板缝顺着弧线走
    panel = art.lacquer_panel(W, H, smooth=True)
    mask = np.zeros((H, W), dtype=bool)
    for u in range(W):
        if solid[:, u].any():
            t = top_s[u]
            mask[int(np.floor(t)):deck_row + 2, u] = True
            for r in range(int(np.floor(t)) + 7, deck_row, 14):
                if 0 <= r < H:
                    panel[r, u, :3] *= 0.68
                    if r + 1 < H:
                        panel[r + 1, u, :3] *= 0.88
    body = panel.copy()
    body[:, :, 3] = 255
    art.apply_mask(body, mask, soften=0.8)
    # 上沿抗锯齿：曲线上方一行按小数覆盖
    for u in range(W):
        t = top_s[u]
        r = int(np.floor(t))
        if 0 <= r < H and mask[:, u].any():
            body[r, u, 3] *= 1.0 - (t - r)
    art.over(img, body, 0, 0)
    # 3) 金边沿曲线 + 顶面一条亮红
    gold = np.array(art.GOLD, dtype=np.float32)
    gold_l = np.array(art.GOLD_LIGHT, dtype=np.float32)
    for u in range(W):
        if not mask[:, u].any():
            continue
        t = top_s[u]
        r0 = int(np.floor(t)) + 1
        for dr, col, a in ((0, gold_l, 0.95), (1, gold, 0.95), (2, gold, 0.8), (3, art.RED_LIGHT, 0.6)):
            r = r0 + dr
            if 0 <= r < deck_row:
                img[r, u, :3] = img[r, u, :3] * (1 - a) + np.array(col, dtype=np.float32) * a
                img[r, u, 3] = max(img[r, u, 3], 255 * a)
    # 4) 端头竖面（最右一列到 deck_row）压暗，读作侧面
    right = np.nonzero(mask.any(axis=0))[0]
    if len(right):
        x1 = right.max()
        for u in range(max(0, x1 - 5), x1 + 1):
            f = 0.55 + 0.08 * (x1 - u)
            img[:deck_row, u, :3] *= f
    return img


def tr_x_30(ctx):
    """阁楼方箱 133×162：上 143 行实心（红漆 + 上下金箍），底 19 行是不挡人的裙边。"""
    W, H, SOLID = 133, 162, 143
    img = art.new(W, H)
    panel = art.lacquer_panel(W, SOLID)
    art.over(img, panel, 0, 0)
    # 内框：暗红细线 + 一圈更亮的内板
    inset = 9
    inner = art.lacquer_panel(W - 2 * inset - 2, SOLID - 34 - 2)
    inner[:, :, :3] *= 1.08
    art.over(img, inner, inset + 1, 14 + 1)
    for r in (14, SOLID - 20):
        art.hline(img, r, inset, W - inset, art.RED_DARK, 1, 0.85)
    art.vline(img, inset, 14, SOLID - 20, art.RED_DARK, 1, 0.85)
    art.vline(img, W - inset - 1, 14, SOLID - 20, art.RED_DARK, 1, 0.85)
    art.over(img, art.gold_band(W, 12, rivets=True, rivet_step=22), 0, 0)
    art.over(img, art.gold_band(W, 16, rivets=True, rivet_step=22), 0, SOLID - 16)
    # 角饰：四角小金片
    for cx in (7, W - 8):
        for cy in (20, SOLID - 26):
            art.disc(img, cx, cy, 3.5, art.GOLD_LIGHT, shadow=True)
    # 裙边（无碰撞）：暗红布幔 + 金流苏点，向下淡出
    skirt = art.new(W, H - SOLID, art.RED_DARK)
    skirt[:, :, 3] = 255
    for x in range(6, W, 12):
        art.vline(skirt, x, 0, H - SOLID, art.GOLD, 1, 0.55)
    art.fade_bottom(skirt, 8)
    art.over(img, skirt, 0, SOLID)
    return img


def _rope(ctx, map_name, base, W, H, cx_col, cy_row, thickness, hooks=(), mirrored_handle=None):
    """程序化绳桥：沿残差平台线画一条红绞绳（上沿压在平台线上）。

    hooks：挂灯串的局部 u 坐标（画小金环）。mirrored_handle：镜像的第二个实例，把它的平台线也并进来，
    绳带按两条线的包络画，保证两处摆放踩的都是绳子而不是空气。
    """
    o = ctx.obj(map_name, base)
    u0, u1, v0, v1 = -cx_col, W - cx_col, -cy_row, H - cy_row
    sil, _ = ctx.local(map_name, o, u0, u1, v0, v1)
    plat = sil == 1
    lines = [plat]
    if mirrored_handle is not None:
        # local_silhouette 已经按 sx 的符号把镜像实例反变换回精灵自身坐标了，这里**不要再翻一次**
        o2 = ctx.obj(map_name, base, mirrored_handle)
        sil2, _ = ctx.local(map_name, o2, u0, u1, v0, v1)
        lines.append(sil2 == 1)
    top = np.full(W, np.nan); bot = np.full(W, np.nan)
    for L in lines:
        for u in range(W):
            rows = np.nonzero(L[:, u])[0]
            if len(rows):
                top[u] = np.nanmin([top[u], rows.min()]); bot[u] = np.nanmax([bot[u], rows.max()])
    valid = ~np.isnan(top)
    xs = np.arange(W)
    umin, umax = int(xs[valid].min()), int(xs[valid].max())
    # 平滑 + 插值（平台线是 1 px 台阶）
    k = 4
    def smooth(a):
        f = np.interp(xs, xs[valid], a[valid])
        return np.convolve(np.pad(f, (k, k), mode="edge"), np.ones(2 * k + 1) / (2 * k + 1), mode="valid")
    top_s, bot_s = smooth(top), smooth(bot)
    img = art.new(W, H)
    red, dark, light = np.array(art.RED_LACQUER), np.array(art.RED_DARK), np.array(art.RED_LIGHT)
    gold = np.array(art.GOLD_LIGHT)
    for u in range(umin, umax + 1):
        t = top_s[u] - 0.5                       # 上沿略高于平台线，脚踩在绳面上
        b = max(bot_s[u], t + thickness) + 1.0
        r0, r1 = int(np.floor(t)), int(np.ceil(b))
        for r in range(max(0, r0), min(H, r1 + 1)):
            cov = min(r + 1, b) - max(r, t)
            if cov <= 0:
                continue
            f = (r + 0.5 - t) / max(b - t, 1e-3)      # 0 顶 .. 1 底
            col = light * (1 - f) * 0.9 + red * f * 0.7 + dark * (f ** 2) * 0.6
            col = np.clip(col + light * 0.35 * np.exp(-((f - 0.22) / 0.16) ** 2), 0, 255)
            # 绞绳斜纹：沿绳长每 13 px 一道暗纹
            twist = ((u * 1.0 + (r - t) * 1.6) % 13.0)
            if twist < 3.2:
                col = col * 0.72
            # 金线：更稀的斜纹
            if ((u * 1.0 + (r - t) * 1.6 + 6.0) % 26.0) < 1.6 and 0.15 < f < 0.85:
                col = gold
            img[r, u, :3] = col
            img[r, u, 3] = max(img[r, u, 3], min(1.0, cov) * 255)
    # 两端金环 + 挂钩
    for u in (umin + 4, umax - 4):
        art.disc(img, u, (top_s[u] + bot_s[u]) / 2 + thickness / 2, thickness * 0.7 + 2, art.GOLD, shadow=True)
        art.disc(img, u, (top_s[u] + bot_s[u]) / 2 + thickness / 2, thickness * 0.35, art.RED_DARK)
    for hu in hooks:
        col = int(round(hu + cx_col))
        if umin <= col <= umax:
            cy = max(bot_s[col], top_s[col] + thickness) + 4
            art.disc(img, col, cy, 4.5, art.GOLD, shadow=True)
            art.disc(img, col, cy, 2.0, art.RED_DARK)
    return img


def tr_x_31(ctx):
    """F02 灯笼绳桥 954×228，中心 (477, 114)；灯串挂在世界 x=546/824/1063（局部 -258/+20/+259）。"""
    return _rope(ctx, "Festival02", "tr_x_31", 954, 228, 477, 114, 12, hooks=(-258, 20, 259))


def tr_x_33(ctx):
    """F00 两条镜像绳桥 320×380，中心 (160, 190)；把镜像实例的平台线并进来。"""
    return _rope(ctx, "Festival00", "tr_x_33", 320, 380, 160, 190, 9, mirrored_handle=345)


def tr_x_32(ctx):
    """水面画舫 252×435（掩码尺寸）：船身按掩码，甲板小亭 + 灯杆 + 灯笼（灯笼落在 Glow 特效的位置，列≈167 行≈217）。"""
    m = ctx.mask("Festival02", "Maps/Festival/Terrain/tr_x_32.png") > 0
    H, W = m.shape
    img = art.new(W, H)
    # 船身：红漆（只取鼓身纯红行，别把青绿反光带进来）+ 木板缝，上沿金边，底部吃水线压暗
    hull = art.lacquer_panel(W, H, rows=(4, 74))
    art.plank_lines(hull, 0, W, 0, H, step=18, phase=4)
    hull[:, :, 3] = 255
    art.apply_mask(hull, m, soften=0.8)
    top = np.full(W, -1)
    for u in range(W):
        rows = np.nonzero(m[:, u])[0]
        if len(rows):
            top[u] = rows.min()
    for u in range(W):
        if top[u] < 0:
            continue
        rows = np.nonzero(m[:, u])[0]
        b = rows.max()
        for r in range(max(top[u], b - 8), b + 1):
            hull[r, u, :3] *= 0.55 + 0.05 * (b - r)
        for dr, col, a in ((0, art.GOLD_LIGHT, 1.0), (1, art.GOLD, 1.0), (2, art.GOLD_DARK, 0.7)):
            r = top[u] + dr
            if r <= b:
                hull[r, u, :3] = hull[r, u, :3] * (1 - a) + np.array(col, dtype=np.float32) * a
    art.over(img, hull, 0, 0)
    # 甲板：船身上沿再往上 4 px 的一条暗红甲板线（只在船身中段）
    mid = [u for u in range(W) if top[u] >= 0]
    u_lo, u_hi = min(mid) + 18, max(mid) - 40
    # 小亭：tr_x_12 红瓦墙块缩到 96×64，坐在船身中段
    cabin = art.resize(art.load("Terrain/tr_x_12.png"), 96, 64)
    cab_x = 62
    cab_y = int(np.median([top[u] for u in range(cab_x, cab_x + 96) if top[u] >= 0])) - 60
    art.over(img, cabin, cab_x, cab_y)
    # 灯杆：tr_x_13 柱子缩到 20×92，杆顶挂 co_06 小灯笼，灯笼中心对准 (167, 217)
    pole = art.resize(art.load("Terrain/tr_x_13.png"), 20, 92)
    pole_x, pole_top = 167 - 10, 235
    art.over(img, pole, pole_x, pole_top)
    art.hline(img, pole_top, pole_x - 14, pole_x + 34, art.GOLD_DARK, 3, 0.95)   # 横臂
    art.vline(img, 167, pole_top - 26, pole_top, art.RED_DARK, 2, 0.9)          # 吊绳
    lantern = art.scale(art.load("Breakable/co_06.png"), 0.55)
    lh, lw = lantern.shape[:2]
    art.over(img, lantern, 167 - lw // 2, 217 - lh // 2)
    # 船头小旗：三角红旗
    flag = art.new(26, 18)
    for r in range(18):
        w_ = int(26 * (1 - abs(r - 9) / 9.0))
        flag[r, :w_, :3] = art.RED_LIGHT; flag[r, :w_, 3] = 255
    art.over(img, flag, pole_x + 20, pole_top - 2)
    return img


# ---------------------------------------------------------------------------
#  地形：无碰撞的装饰（保守猜）
# ---------------------------------------------------------------------------

def tr_x_34(ctx):
    """中央柱右侧的托架灯 200×280：tr_x_09 缩 0.68，托架竖板落在柱面（世界 x 894..944 → 画布列 42..92）。"""
    img = art.new(200, 280)
    src = art.scale(art.load("Terrain/tr_x_09.png"), 0.68)
    art.over(img, src, 62, 22)
    return img


def tr_x_35(ctx):
    """红瓦墙上的挂饰 160×130：一段金流苏彩带 + 两只小红灯笼。"""
    img = art.new(160, 130)
    garland = art.resize(art.load("Terrain/tr_x_16.png"), 156, 32)
    art.over(img, garland, 2, 2)
    lantern = art.scale(art.load("Breakable/co_06.png"), 0.52)
    lh, lw = lantern.shape[:2]
    for cx in (40, 120):
        art.vline(img, cx, 26, 48, art.RED_DARK, 2, 0.9)
        art.disc(img, cx + 1, 26, 2.5, art.GOLD, shadow=True)
        art.over(img, lantern, cx - lw // 2 + 1, 46)
    return img


def tr_x_37(ctx):
    """龙柱柱础 240×70：三层须弥座（金 / 红漆 / 金），下宽上窄。"""
    W, H = 240, 70
    img = art.new(W, H)
    tiers = ((16, 0, 14, True), (10, 14, 40, False), (0, 54, 16, True))
    for inset, y, h, gold in tiers:
        if gold:
            band = art.gold_band(W - 2 * inset, h, rivets=True, rivet_step=24)
        else:
            band = art.lacquer_panel(W - 2 * inset, h)
            art.hline(band, h // 2 - 1, 0, W, art.GOLD, 2, 0.8)
        art.over(img, band, inset, y)
    # 顶面一条更亮的线，读作有厚度
    art.hline(img, 0, 16, W - 16, art.GOLD_LIGHT, 2, 0.9)
    return img


def _far_pagoda(pieces, w=400, h=300):
    """远景楼阁剪影：几张现有楼阁图缩放拼贴，压暗偏蓝（和最远视差层的 la_07 同一档观感）。"""
    img = art.new(w, h)
    for rel, s, x, y in pieces:
        src = art.scale(art.load(rel), s)
        src = art.tint(src, (0.55, 0.62, 0.90), 0.92)
        art.over(img, src, x, y)
    haze = art.new(w, 70, (30, 40, 80))
    haze[:, :, 3] = np.linspace(0, 150, 70, dtype=np.float32)[:, None]
    art.over(img, haze, 0, h - 70)
    return img


def la_19(ctx):
    return _far_pagoda((("Layer/la_17.png", 0.55, 20, 55), ("Layer/la_03.png", 0.50, 205, 65)))


def la_20(ctx):
    return _far_pagoda((("Layer/la_18.png", 0.70, 10, 90), ("Layer/la_15.png", 0.45, 210, 150)))


# ---------------------------------------------------------------------------
#  可破坏物：掩码定死尺寸和轮廓
# ---------------------------------------------------------------------------

def _body_bbox(img, thr=200):
    ys, xs = np.nonzero(img[:, :, 3] >= thr)
    return xs.min(), ys.min(), xs.max(), ys.max()


def _fit_lantern(src_rel, mask, body_rows=None):
    """把一张灯笼素材缩放平移，使其「灯身」（alpha≥200 的椭圆）落在掩码的实心椭圆上。"""
    H, W = mask.shape
    src = art.load(src_rel)
    ys, xs = np.nonzero(mask)
    mx0, my0, mx1, my1 = xs.min(), ys.min(), xs.max(), ys.max()
    if body_rows is not None:
        sub = src[body_rows[0]:body_rows[1]]
        bx0, by0, bx1, by1 = _body_bbox(sub)
        by0 += body_rows[0]; by1 += body_rows[0]
    else:
        bx0, by0, bx1, by1 = _body_bbox(src)
    sx = (mx1 - mx0 + 1) / float(bx1 - bx0 + 1)
    sy = (my1 - my0 + 1) / float(by1 - by0 + 1)
    s = (sx + sy) / 2.0
    scaled = art.scale(src, s)
    # 灯身中心对齐掩码中心
    cx_src, cy_src = (bx0 + bx1 + 1) / 2.0 * s, (by0 + by1 + 1) / 2.0 * s
    cx_m, cy_m = (mx0 + mx1 + 1) / 2.0, (my0 + my1 + 1) / 2.0
    img = art.new(W, H)
    art.over(img, scaled, int(round(cx_m - cx_src)), int(round(cy_m - cy_src)))
    return img


def co_07(ctx):
    """竖长宫灯 120×172：Cover/co_05 整体缩到掩码的 39×92（连灯帽带流苏正好）。"""
    m = ctx.mask("Festival02", "Maps/Festival/Breakable/co_07.png") > 0
    H, W = m.shape
    ys, xs = np.nonzero(m)
    src = art.load("Cover/co_05.png")
    ox0, oy0, ox1, oy1 = _body_bbox(src, 40)
    s = (ys.max() - ys.min() + 1) / float(oy1 - oy0 + 1)
    scaled = art.scale(src, s)
    img = art.new(W, H)
    cx_m, cy_m = (xs.min() + xs.max() + 1) / 2.0, (ys.min() + ys.max() + 1) / 2.0
    art.over(img, scaled, int(round(cx_m - (ox0 + ox1 + 1) / 2.0 * s)), int(round(cy_m - (oy0 + oy1 + 1) / 2.0 * s)))
    return img


def co_08(ctx):
    """水面浮箱 90×80：红漆箱 + 上下金箍 + 金角 + 中心金环，alpha 按掩码。"""
    m = ctx.mask("Festival02", "Maps/Festival/Breakable/co_08.png") > 0
    H, W = m.shape
    img = art.lacquer_panel(W, H)
    img[:, :, 3] = 255
    art.over(img, art.gold_band(W, 9, rivets=True, rivet_step=16), 0, 1)
    art.over(img, art.gold_band(W, 9, rivets=True, rivet_step=16), 0, H - 11)
    art.vline(img, 3, 10, H - 10, art.GOLD_DARK, 3, 0.9)
    art.vline(img, W - 6, 10, H - 10, art.GOLD_DARK, 3, 0.9)
    art.disc(img, W / 2, H / 2, 14, art.GOLD, shadow=True)
    art.disc(img, W / 2, H / 2, 9.5, art.RED_LACQUER)
    art.disc(img, W / 2, H / 2, 4, art.GOLD_LIGHT, shadow=True)
    art.apply_mask(img, m, soften=0.6)
    return img


def co_12(ctx):
    """橙金圆灯笼 227×227：Breakable/co_04 缩到掩码椭圆 97×75。"""
    m = ctx.mask("Festival02", "Maps/Festival/Breakable/co_12.png") > 0
    return _fit_lantern("Breakable/co_04.png", m)


def co_13(ctx):
    """红圆灯笼 218×230：Breakable/co_01 缩到掩码椭圆 125×91（只量灯身那一段，不含流苏）。"""
    m = ctx.mask("Festival02", "Maps/Festival/Breakable/co_13.png") > 0
    src = art.load("Breakable/co_01.png")
    # 灯身 = alpha≥200 里最宽那一段行（流苏窄得多）
    widths = (src[:, :, 3] >= 200).sum(axis=1)
    body = np.nonzero(widths >= widths.max() * 0.7)[0]
    return _fit_lantern("Breakable/co_01.png", m, body_rows=(int(body.min()), int(body.max()) + 1))


# ---------------------------------------------------------------------------
#  清单 / 入口
# ---------------------------------------------------------------------------

RECIPES = {
    "Terrain/tr_x_28.png": tr_x_28, "Terrain/tr_x_29.png": tr_x_29, "Terrain/tr_x_30.png": tr_x_30,
    "Terrain/tr_x_31.png": tr_x_31, "Terrain/tr_x_32.png": tr_x_32, "Terrain/tr_x_33.png": tr_x_33,
    "Terrain/tr_x_34.png": tr_x_34, "Terrain/tr_x_35.png": tr_x_35, "Terrain/tr_x_37.png": tr_x_37,
    "Layer/la_19.png": la_19, "Layer/la_20.png": la_20,
    "Breakable/co_07.png": co_07, "Breakable/co_08.png": co_08, "Breakable/co_12.png": co_12, "Breakable/co_13.png": co_13,
}


def build(out_dir, only=None):
    ctx = Ctx()
    made = []
    for rel, fn in RECIPES.items():
        if only and os.path.basename(rel)[:-4] not in only:
            continue
        img = fn(ctx)
        path = os.path.join(out_dir, rel.replace("/", os.sep))
        art.to_png(img, path)
        made.append((rel, img.shape[1], img.shape[0]))
        print("  %-26s %4dx%-4d" % (rel, img.shape[1], img.shape[0]))
    if not only:
        os.makedirs(os.path.join(out_dir, "Cover"), exist_ok=True)
        for base in COVER_COPIES:
            src = os.path.join(art.FESTIVAL, "Terrain", base + ".png")
            dst = os.path.join(out_dir, "Cover", base + ".png")
            shutil.copyfile(src, dst)
            made.append(("Cover/%s.png" % base, 0, 0))
            print("  %-26s <- Terrain 逐字节复制" % ("Cover/%s.png" % base))
        for name, n, size in effects.build_all(os.path.join(out_dir, "Effect")):
            made.append(("Effect/%s.efx" % name, n, size))
        print("  Effect/*.efx  %d 个" % len(effects.DONORS))
    return made


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--install", action="store_true", help="写进 Pack_develop/Maps/Festival/（缺省写到 logs/）")
    ap.add_argument("--only", nargs="*", help="只生成这几张（按 basename，如 tr_x_31 co_08）")
    args = ap.parse_args(argv)
    out = INSTALL_DIR if args.install else OUT_DEFAULT
    print("输出到", out)
    build(out, set(args.only) if args.only else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
