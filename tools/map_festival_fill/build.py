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
- 无碰撞的装饰（`tr_x_34/35/37`、`la_19/20`）：保守猜测，用现有素材拼；`tr_x_35` 的**摆位**
  是从 11 个实例量出来的常数定死的（`TR35_PAD`），不是猜的。
- `Cover/` 是**独立编号**，不是 Terrain 的副本（原版 `Cover/tr_x_09` 就和 `Terrain/tr_x_09` 完全不同）：
  `Cover/tr_x_12/13/14/15` 是绳上的「炮炮火枪手」圆牌（§71）；只有 `tr_x_10/16/17` 仍按副本处理（未证实）。

## 美术怎么来

全部从 `Maps/Festival/` 现有素材采样 / 缩放 / 拼贴（铁律 13：先搜再画），程序化只用在没有素材的地方
（绳桥、坡道面板）。基准色和材质函数在 `art.py`。
★ `tr_x_32`（鲤鱼）是从 `ref/festival02_carp.png` **抠**出来的，不是画的 —— 那是用户找到的原版截图，
比任何手画都准。以后再遇到「有原版图能对」的情况，优先抠图。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import art  # noqa: E402
import effects  # noqa: E402
import mapscan  # noqa: E402

ROOT = art.ROOT
OUT_DEFAULT = os.path.join(ROOT, "logs", "map_festival_fill", "out")
INSTALL_DIR = art.FESTIVAL

#: Cover 副本：原版 `Cover/tr_x_08.png` 和 `Terrain/tr_x_08.png` 逐字节相同，照抄。
#: ★ `tr_x_12/13/14/15` **已经不在这张表里** —— 它们是绳上的「炮炮火枪手」圆牌，另有画法（见 RECIPES）。
#: ⚠ 剩下这三张只是「原版 tr_x_08 是副本」推出来的**未经证实**的假设：原版 `Cover/tr_x_09.png`
#: 就和 `Terrain/tr_x_09.png` 完全不同（前者是 logo、后者是托架灯）⇒ Cover 是独立编号，
#: 这三张多半也不是副本。缺参考图，先保持现状（§71）。
COVER_COPIES = ("tr_x_10", "tr_x_16", "tr_x_17")


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


# ---------------------------------------------------------------------------
#  tr_x_32：吊在彩带上的红鲤鱼灯 —— 从原版截图里抠
# ---------------------------------------------------------------------------

#: 用户 2026-09-21 找到的原版清晰截图（「云之桥」里那条鱼）。抠图的唯一来源，别删。
CARP_REF = os.path.join(HERE, "ref", "festival02_carp.png")

#: 截图 → 画布的仿射：`shot_x = AX·u + BX`、`shot_y = AY·v + BY`。
#: 网格搜「截图里的鱼形 vs `.map` 掩码」的 IoU 得到（最优 0.86）；
#: `AX/AY = 1.093 ≈ 对象的 sx/sy = 1.1/1.0`，两条独立的量法对上了，说明配准是对的。
CARP_FIT = (2.110, 66.0, 1.930, -241.5)

#: 吊链：竖段在画布正中，到 v≈298 分成 Y 形两叉搭在鱼背上（都按上面的仿射从截图量的）。
CARP_CHAIN_U = 124.0
CARP_CHAIN_FORK = (124.0, 298.0)
CARP_CHAIN_ENDS = ((92.0, 329.0), (142.0, 326.0))
#: 竖链往上循环贴的取样段（截图只拍到 v≈125 以下）；长度 68 = 链节周期的整数倍。
CARP_CHAIN_TILE = (176, 244)

CARP_EYE = (189.0, 346.0)          # 青色的鱼眼，不红，抠图时要单独保住
CARP_FEET = (318, 335, 170, 215)   # 截图里站在鱼背上那个角色的鞋，要擦掉重补


def _carp_resample(shot, Hc, Wc):
    """截图 → 画布：反向遍历截图像素做面积平均（缩小约 2 倍）。返回 (rgb, 采到没有)。"""
    ax, bx, ay, by = CARP_FIT
    Hs, Ws = shot.shape[:2]
    acc = np.zeros((Hc, Wc, 3), dtype=np.float32)
    cnt = np.zeros((Hc, Wc), dtype=np.float32)
    sy, sx = np.mgrid[0:Hs, 0:Ws]
    u = np.rint((sx - bx) / ax).astype(int)
    v = np.rint((sy - by) / ay).astype(int)
    ok = (u >= 0) & (u < Wc) & (v >= 0) & (v < Hc)
    np.add.at(acc, (v[ok], u[ok]), shot[ok])
    np.add.at(cnt, (v[ok], u[ok]), 1.0)
    got = cnt > 0
    acc[got] /= cnt[got][:, None]
    return acc, got


def _inpaint(rgb, keep, need, rounds=40):
    """把 `need` 里、`keep` 外的像素用邻居的 keep 像素反复平均补上。

    ★ 求和时必须先乘 keep 掩码 —— 只除以「好邻居个数」却把坏邻居的颜色也加进去，
    结果会被放大到溢出，补出一片纯白（2026-09-21 踩过）。
    """
    out = rgb.copy()
    good = keep.copy()
    H, W = good.shape
    for _ in range(rounds):
        todo = need & ~good
        if not todo.any():
            break
        pv = np.pad(out * good[:, :, None], ((1, 1), (1, 1), (0, 0)))
        pg = np.pad(good.astype(np.float32), ((1, 1), (1, 1)))
        s = sum(pv[1 + dy:1 + dy + H, 1 + dx:1 + dx + W] for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        n = sum(pg[1 + dy:1 + dy + H, 1 + dx:1 + dx + W] for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        fill = todo & (n > 0)
        out[fill] = s[fill] / n[fill][:, None]
        good |= fill
    return out


def _seg_band(Hc, Wc, p0, p1, half):
    """到线段 p0-p1 的距离 ≤ half 的那条带。"""
    ys, xs = np.mgrid[0:Hc, 0:Wc]
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    t = np.clip(((xs - x0) * dx + (ys - y0) * dy) / (dx * dx + dy * dy), 0, 1)
    return np.hypot(xs - (x0 + t * dx), ys - (y0 + t * dy)) <= half


def _smooth_in(img, mask, box, rounds):
    """只在掩码内、只在 box 这一块做几遍 3×3 平均（抹掉按列补出来的竖条纹）。"""
    v0, v1, u0, u1 = box
    sl = (slice(v0, v1), slice(u0, u1))
    for _ in range(rounds):
        win = img[sl].copy()
        wm = mask[sl].astype(np.float32)[:, :, None]
        pv = np.pad(win * wm, ((1, 1), (1, 1), (0, 0)))
        pg = np.pad(wm, ((1, 1), (1, 1), (0, 0)))
        h, w = win.shape[:2]
        acc = sum(pv[1 + dy:1 + dy + h, 1 + dx:1 + dx + w] for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        num = sum(pg[1 + dy:1 + dy + h, 1 + dx:1 + dx + w] for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        img[sl] = np.where(num > 0, acc / np.maximum(num, 1e-6), win)
    return img


def tr_x_32(ctx):
    """吊在彩带上的**红鲤鱼灯** 252×435（掩码尺寸）：上面是金吊链，下面（掩码 rows 321..418）是鱼。

    ★ 2026-09-21：用户先指出这是鱼不是船，又找来一张清晰的原版截图 ⇒ **直接从截图抠**，不再手画。
    几何自洽：对象挂在 type 111 路径上（y≈735）⇒ 画布顶 = 735−217.5 = 517.3，正好是绳子那一行；
    鱼挂在画布最下面，中间那 321 行就是链子。

    透明度怎么定：
    - **鱼身 alpha = `.map` 掩码 × 「像不像鱼」**。掩码是编辑器按列从最上到最下填出来的实心块
      （252 列里只有 6 列有洞），所以尾巴和肚子之间那个缺口在掩码里是实的、在原版贴图里是透的 ——
      只按掩码裁会糊上一块背景云。判据：`r − max(g, b)`（鱼身 / 金云纹 ≥96，背景亮云 ≤54），
      外加「掩码内够暗的一律算鱼」（背景是亮云，不会暗）。青色的鱼眼两条都不满足，单独圈出来保住。
    - 链子 alpha = 「金色 ∧ 贴着那三条线段」，这样不会把背景里的金云一起抠进来。
    """
    m = ctx.mask("Festival02", "Maps/Festival/Terrain/tr_x_32.png") > 0
    Hc, Wc = m.shape
    shot = np.array(Image.open(CARP_REF).convert("RGB")).astype(np.float32)
    rgb, got = _carp_resample(shot, Hc, Wc)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]

    conf = np.clip((r - np.maximum(g, b) - 62.0) / 26.0, 0, 1)
    conf = np.maximum(conf, np.clip((120.0 - rgb.max(axis=2)) / 30.0, 0, 1))
    conf[~got] = 0
    ys, xs = np.mgrid[0:Hc, 0:Wc]
    conf[np.hypot(xs - CARP_EYE[0], ys - CARP_EYE[1]) <= 8.0] = 1.0
    v0, v1, u0, u1 = CARP_FEET
    conf[v0:v1, u0:u1] = 1.0

    keep = m & (conf > 0.85)
    keep[v0:v1, u0:u1] = False
    body = _inpaint(rgb, keep, m)
    # 鞋那一块：按列把下面第一行干净的鱼背色往上拉，再抹几遍。
    # 用通用 `_inpaint` 会把鞋边缘的棕色卷进来，补出一道棕带。
    for u in range(u0, u1):
        col = np.nonzero(keep[v1:, u])[0]
        if not len(col):
            continue
        src = body[v1 + col[0], u].copy()
        for v in range(v0, v1):
            if m[v, u]:
                body[v, u] = src
    _smooth_in(body, m, (v0 - 2, v1 + 3, u0 - 2, u1 + 3), 5)

    img = art.new(Wc, Hc)
    img[:, :, :3] = body
    img[:, :, 3] = m.astype(np.float32) * conf * 255.0

    # 吊链
    goldish = got & (r > 130) & (g > 88) & (r - b > 55) & (g - b > 30)
    band = _seg_band(Hc, Wc, (CARP_CHAIN_U, 0.0), CARP_CHAIN_FORK, 7.0)
    for end in CARP_CHAIN_ENDS:
        band |= _seg_band(Hc, Wc, CARP_CHAIN_FORK, end, 5.0)
    chain = art.new(Wc, Hc)
    chain[:, :, :3] = rgb
    chain[:, :, 3] = (goldish & band).astype(np.float32) * 255.0
    p0, p1 = CARP_CHAIN_TILE
    tile = chain[p0:p1].copy()
    y = p0
    while y > 0:
        y -= (p1 - p0)
        chain[max(0, y):y + (p1 - p0)] = tile[max(0, y) - y:]
    art.over(img, chain, 0, 0)
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


#: `tr_x_35` 画布右边留出的空白宽度。精灵按**中心**锚点摆，右边多留 2×`TR35_PAD` 就等于
#: 把彩带整体往左推 `TR35_PAD` px。定成 30 的根据：11 个实例实测「右挂点 − 红瓦平台可见右沿」
#: 一律是 +27..+33（中位 +30、σ=2），是个常数 ⇒ 原版就是按「右挂点贴平台右沿」定的位
#: （2026-09-21 用户报「几乎所有跳跃平台上的这个都错位了」）。平台宽度 113..236 不等，
#: 只有右端对得死，左端只能跟着走。★ 这个常数是在「主层按文件逆序画」的前提下量的（§70）。
TR35_PAD = 30


def tr_x_35(ctx):
    """红瓦墙上的挂饰 220×130：一段金流苏彩带 + 两只小红灯笼，画在画布左侧（见 `TR35_PAD`）。"""
    img = art.new(160 + 2 * TR35_PAD, 130)
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
#  Cover：绳上那五块「炮炮火枪手」圆牌（★ 不是 Terrain 同名件的副本）
# ---------------------------------------------------------------------------

#: `Cover/tr_x_1{5,4,3,2}.png` —— Festival02 的绳子上挂着五个 `HidingObj`，按世界 x 排是
#: 667.9 / 737.2 / 819.3 / 903.9 / 973.6，用的贴图依次是 `tr_x_15`、`tr_x_15`、`tr_x_14`、
#: `tr_x_13`、`tr_x_12`：**前两个是同一张** ⇒ 读作「炮 炮 火 枪 手」（用户 2026-09-21 找到原版截图佐证）。
#: 第三块（火）的对象缩放是 0.5、其余 0.4，原版截图里它也正好大一圈 ⇒ 四张画布同尺寸。
#: 屏上直径实测约 64 px（0.4 缩放）⇒ 画布 160×160。
MEDALLION_SIZE = 160

#: 圆牌配色，从原版截图上采的（火那块，按到中心的距离分环取均值）。
MED_DISC_HI = (245.0, 136.0, 152.0)
MED_DISC_LO = (186.0, 58.0, 72.0)
MED_RING_HI = (252.0, 198.0, 140.0)
MED_RING_LO = (196.0, 104.0, 58.0)
MED_GLYPH = (56.0, 6.0, 10.0)


def _logo_glyphs():
    """把 `Terrain/tr_x_08.png`（617×195 的「炮炮火枪手」logo）切成 5 个字芯掩码。

    字芯 = 那圈粉色描边**里面**的暗红填充（不含描边和外发光），判据是「够不透明 + 够暗 + 够红」。
    五个字在列方向天然分开（各约 98 px 宽），按列直接切。铁律 13：字不自己造，从原版 logo 抠。
    """
    a = art.load("Terrain/tr_x_08.png")
    r, g, b, al = a[:, :, 0], a[:, :, 1], a[:, :, 2], a[:, :, 3]
    core = (al > 150) & (r < 150) & (r > g + 18) & (r > b + 10)
    core = art.open_mask(core, 2)        # logo 底边有几粒噪点，开运算抹掉
    inside = core.sum(axis=0) > 1
    runs, start = [], None
    for i, v in enumerate(inside):
        if v and start is None:
            start = i
        elif (not v) and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(inside) - 1))
    runs = [(x0, x1) for x0, x1 in runs if x1 - x0 >= 12]
    if len(runs) != 5:
        raise RuntimeError("tr_x_08 应该切出 5 个字，切出了 %d 个：%s" % (len(runs), runs))
    out = []
    for x0, x1 in runs:
        sub = core[:, x0:x1 + 1]
        rows = np.nonzero(sub.any(axis=1))[0]
        out.append(sub[rows.min():rows.max() + 1])
    return out


def _medallion(glyph):
    """一块圆牌 160×160：暖金圈 + 中心亮的粉红盘面 + 暗红字。"""
    S = MEDALLION_SIZE
    c = (S - 1) / 2.0
    r_in, r_out = 63.0, 79.0      # 外径 158 ⇒ 0.4 缩放后屏上 63 px，和原版截图量到的 64 对得上
    ys, xs = np.mgrid[0:S, 0:S]
    d = np.sqrt((xs - c) ** 2 + (ys - c) ** 2)
    img = art.new(S, S)
    # 盘面：中心亮外缘深，再往左上压一点高光
    t = np.clip(d / r_in, 0, 1)[:, :, None]
    img[:, :, :3] = np.array(MED_DISC_HI) * (1 - t) + np.array(MED_DISC_LO) * t
    lit = np.clip(1.0 + 0.16 * ((c - xs) + (c - ys)) / r_in, 0.86, 1.14)
    img[:, :, :3] = np.clip(img[:, :, :3] * lit[:, :, None], 0, 255)
    # 金圈：内亮外暗的一道斜面
    k = np.clip((d - r_in) / (r_out - r_in), 0, 1)[:, :, None]
    gold = np.array(MED_RING_HI) * (1 - k) + np.array(MED_RING_LO) * k
    img[:, :, :3] = np.where(((d >= r_in) & (d <= r_out))[:, :, None], gold, img[:, :, :3])
    img[:, :, 3] = np.clip(r_out + 0.5 - d, 0, 1) * 255
    # 字：按 logo 原始字宽等比缩（98 -> 96），各自按外接框居中
    gh, gw = glyph.shape
    k2 = 88.0 / 98.0
    tw, th = max(1, int(round(gw * k2))), max(1, int(round(gh * k2)))
    stamp = art.new(gw, gh)
    stamp[:, :, :3] = MED_GLYPH
    stamp[:, :, 3] = glyph.astype(np.float32) * 255.0
    stamp = art.resize(stamp, tw, th)
    stamp[:, :, :3] = MED_GLYPH                      # 缩放会把颜色和透明边混在一起，重新压回字色
    art.over(img, stamp, int(round(c - tw / 2.0)), int(round(c - th / 2.0)))
    return img


_GLYPHS = []


def _glyph(i):
    if not _GLYPHS:
        _GLYPHS.extend(_logo_glyphs())
    return _GLYPHS[i]


def cover_tr_x_15(ctx):
    """绳上第 1、2 块圆牌（同一张贴图用两次）：**炮**。"""
    return _medallion(_glyph(0))


def cover_tr_x_14(ctx):
    """绳上第 3 块圆牌（对象缩放 0.5，比别的大一圈）：**火**。"""
    return _medallion(_glyph(2))


def cover_tr_x_13(ctx):
    """绳上第 4 块圆牌：**枪**。"""
    return _medallion(_glyph(3))


def cover_tr_x_12(ctx):
    """绳上第 5 块圆牌：**手**。"""
    return _medallion(_glyph(4))


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
    "Cover/tr_x_12.png": cover_tr_x_12, "Cover/tr_x_13.png": cover_tr_x_13,
    "Cover/tr_x_14.png": cover_tr_x_14, "Cover/tr_x_15.png": cover_tr_x_15,
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
