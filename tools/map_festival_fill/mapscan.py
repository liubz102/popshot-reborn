#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mapscan.py —— 庆典地图（Festival00/01/02）缺失资源扫描：哪些贴图 / 特效被 `.map` 引用却不在盘上，
以及每个缺失地形精灵在碰撞位图里留下的**真实轮廓**（X_Mod · X12）。

    python tools\\map_festival_fill\\mapscan.py --report            # 死引用表（PNG + efx + map.ini 落水特效）
    python tools\\map_festival_fill\\mapscan.py --report --all      # 全 174 张地图一起扫
    python tools\\map_festival_fill\\mapscan.py --diag              # 诊断图 + 轮廓图到 logs\\map_festival_fill\\diag\\

## 判据

`.map` 尾部的碰撞位图 `TerrainData`（V0.3bot §27）是编辑器按精灵 alpha 烘好的，贴图缺了碰撞还在。
把现存 `TerrainObj` 按 (中心 x,y · 缩放 · 镜像) 光栅化成覆盖图，**碰撞里有、覆盖图里没有的像素 = 缺失精灵的轮廓**。
庆典三张图所有 TerrainObj 旋转都是 0，所以不用处理旋转。

尾部那张「贴图路径 → 掩码」表（V0.3bot §27，可破坏物的形状）里还躺着 `tr_x_32` 和四个 `co_*` 的
逐像素轮廓和**画布尺寸** —— 有掩码的一律以掩码为准。

## 和 `tools/mapdata.py` 的关系

只复用它的读流原语和碰撞位图解码（`Reader` / `_read_terrain_data` / `unpack_cells`），
19 类循环自己再走一遍 —— `mapdata._read_obj_blob` 故意把缩放 / 镜像丢了（bot 用不着），这里要留下。
**不改 `mapdata.py`**：它的产物格式有 `server/mapdata.py` 和两个发布包盯着。
"""
from __future__ import annotations

import argparse
import collections
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
import mapdata  # noqa: E402

PACK = os.path.join(ROOT, "game_patched", "Pack_develop")
MAPS_DIR = os.path.join(PACK, "Maps")
FESTIVAL_MAPS = ("Festival00", "Festival01", "Festival02")
LOG_DIR = os.path.join(ROOT, "logs", "map_festival_fill")

#: 有贴图 / 特效文件的对象类型（V0.3bot §17）。
TERRAIN, HIDING, LAYER, BREAKABLE, EFFECT = 200, 201, 202, 203, 209
FILE_TYPES = frozenset((TERRAIN, HIDING, LAYER, BREAKABLE, EFFECT))
TYPE_NAME = {TERRAIN: "Terrain", HIDING: "Cover", LAYER: "Layer", BREAKABLE: "Breakable", EFFECT: "Effect"}

#: `Data/map.ini` 里 Festival02 的落水特效（UTF-16LE 文件，键 `FallDownEffectFileName`）。
MAP_INI = os.path.join(PACK, "Data", "map.ini")

#: ★ 本工程**自己画出来**的地形贴图。算覆盖图 / 残差时它们永远按「缺失」处理 ——
#: 否则 `build.py --install` 一装上去，残差就被自己盖没了，第二次跑什么轮廓都推不出来（2026-09-21 踩过）。
GENERATED_TERRAIN = frozenset(("tr_x_28", "tr_x_29", "tr_x_30", "tr_x_31", "tr_x_33", "tr_x_34", "tr_x_35", "tr_x_37"))


# ---------------------------------------------------------------------------
#  解析：带缩放 / 镜像 / 尾部字段的完整对象表
# ---------------------------------------------------------------------------

def _read_blob(blob, ver):
    """`MapObj::Deserialize`（0x511da0）—— 同 `mapdata._read_obj_blob`，但把缩放 / 旋转 / 附加值都留下。"""
    r = mapdata.Reader(blob)
    obj = {"v17": (0, 0, 0), "sx": 1.0, "sy": 1.0, "rot": 0.0, "name": "", "path": ""}
    if ver >= 17:
        obj["v17"] = (r.i32(), r.i32(), r.i32())
    if ver >= 12:
        obj["x"] = r.f32(); obj["y"] = r.f32()
        obj["sx"] = r.f32(); obj["sy"] = r.f32()
        r.f32(); r.f32()
        obj["rot"] = r.f32()
        obj["name"] = r.wstr()
        path = r.wstr()
        obj["path"] = path.replace("//", "/") if ver >= 13 else path
    if ver >= 10:
        obj["props"] = [(r.wstr(), r.wstr()) for _ in range(r.i32())]
    obj["tail"] = r.raw(r.left()) if r.left() else b""
    return obj


def parse_full(path):
    """解一个 `.map`：返回 dict(ver, width, height, objects, terrain, masks)。

    `objects` 里每条：type / handle / cat / x / y / sx / sy / rot / path / v17 / tail。
    容器格式照 `mapdata.parse_map`（V0.3bot §17 / §28）。
    """
    with open(path, "rb") as fp:
        r = mapdata.Reader(fp.read())
    ver = r.u16()
    width = r.i32()
    height = r.i32()
    r.raw(r.i32())
    if ver >= 10:
        r.wstr()
    objects = []
    for cat in range(19):
        for _ in range(r.i32()):
            if ver >= 13:
                otype = r.i32()
                handle = r.i32()
                obj = _read_blob(r.raw(r.i32()), ver)
            else:
                handle = r.i32()
                if ver >= 9:
                    r.wstr()
                else:
                    r.i32()
                pos = [r.f32() for _ in range(7)]
                tex = r.wstr()
                obj = _read_blob(r.raw(r.i32()), ver)
                obj.update(x=pos[0], y=pos[1], sx=pos[2], sy=pos[3], rot=pos[6], path=tex)
                otype = mapdata._type_from_path(tex)
            obj["type"] = otype
            obj["handle"] = handle
            obj["cat"] = cat
            objects.append(obj)
    terrain = mapdata._read_terrain_data(r)
    masks = {}
    for _ in range(r.i32()):
        mask_path = r.wstr()
        masks[mask_path.replace("//", "/")] = mapdata._read_terrain_data(r)
    if r.left() > 1:
        raise mapdata.MapFormatError("文件尾还剩 %d 字节没解释" % r.left())
    return {"name": os.path.splitext(os.path.basename(path))[0], "ver": ver, "width": width,
            "height": height, "objects": objects, "terrain": terrain, "masks": masks}


def cells_of(td):
    """TerrainData -> numpy uint8 (h, w)，值 0..3。"""
    import numpy as np
    flat = mapdata.unpack_cells(td["cells"], td["width"], td["height"])
    return np.frombuffer(flat, dtype=np.uint8).reshape(td["height"], td["width"])


# ---------------------------------------------------------------------------
#  文件存在性（大小写不敏感，路径相对 Pack_develop）
# ---------------------------------------------------------------------------

_LISTING_CACHE = {}


def real_path(rel):
    """`Maps/Festival/Terrain/tr_x_01.png` -> 磁盘真实路径；不存在返回 None。"""
    cur = PACK
    for part in rel.replace("\\", "/").split("/"):
        if not part:
            continue
        names = _LISTING_CACHE.get(cur)
        if names is None:
            names = {n.lower(): n for n in os.listdir(cur)} if os.path.isdir(cur) else {}
            _LISTING_CACHE[cur] = names
        hit = names.get(part.lower())
        if hit is None:
            return None
        cur = os.path.join(cur, hit)
    return cur if os.path.isfile(cur) else None


def falldown_effect(map_name):
    """`map.ini` 里这张图（常规模式那条）的 `FallDownEffectFileName`，没有返回 None。"""
    with open(MAP_INI, "rb") as fp:
        text = fp.read().decode("utf-16")
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = {}
            continue
        if section is None or "=" not in line:
            continue
        key, _, val = line.partition("=")
        section[key.strip()] = val.strip()
        if key.strip() == "FallDownEffectFileName" and section.get("MapFileName") == map_name:
            return val.strip()
    return None


def dead_refs(info):
    """一张图里「被引用但盘上没有」的资源：Counter{(type, path): 次数}。"""
    dead = collections.Counter()
    for o in info["objects"]:
        p = o.get("path", "")
        if p and o["type"] in FILE_TYPES and real_path(p) is None:
            dead[(o["type"], p)] += 1
    return dead


# ---------------------------------------------------------------------------
#  覆盖图 / 残差 / 轮廓
# ---------------------------------------------------------------------------

def sprite_window(o, w, h):
    """精灵在世界里的整数窗口 (left, top, W, H)：中心锚点，`floor(x - W/2)`。"""
    import numpy as np
    W = max(1, int(round(w * abs(o["sx"]))))
    H = max(1, int(round(h * abs(o["sy"]))))
    return int(np.floor(o["x"] - W / 2.0)), int(np.floor(o["y"] - H / 2.0)), W, H


def place(canvas, sprite, o, value=1):
    """把 (h, w) 的 0/1 掩码按 o 的摆放贴进 canvas（取 max）。返回世界窗口或 None（全出界）。"""
    import numpy as np
    from PIL import Image
    h, w = sprite.shape
    left, top, W, H = sprite_window(o, w, h)
    m = sprite.astype(np.uint8)
    if (W, H) != (w, h):
        m = np.array(Image.fromarray(m * 255).resize((W, H), Image.NEAREST)) // 255
    if o["sx"] < 0:
        m = m[:, ::-1]
    if o["sy"] < 0:
        m = m[::-1, :]
    Hc, Wc = canvas.shape
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(Wc, left + W), min(Hc, top + H)
    if x1 <= x0 or y1 <= y0:
        return None
    sub = m[y0 - top:y1 - top, x0 - left:x1 - left].astype(np.uint8) * value
    canvas[y0:y1, x0:x1] = np.maximum(canvas[y0:y1, x0:x1], sub)
    return (x0, y0, x1, y1)


def load_alpha_mask(rel):
    """贴图的 alpha>0 掩码（bool ndarray），文件不存在返回 None。"""
    import numpy as np
    from PIL import Image
    rp = real_path(rel)
    if rp is None:
        return None
    return np.array(Image.open(rp).convert("RGBA"))[:, :, 3] > 0


def coverage(info):
    """现存 TerrainObj 的覆盖图（uint8：0 没盖、1 平台掩码、2 实心）+ 缺失 TerrainObj 列表。

    有掩码表的精灵用掩码（tr_x_21 / tr_x_23 就是这么烘的），其余用 alpha>0。
    """
    import numpy as np
    w, h = info["width"], info["height"]
    cover = np.zeros((h, w), dtype=np.uint8)
    missing = []
    for o in info["objects"]:
        if o["type"] != TERRAIN:
            continue
        mk = info["masks"].get(o["path"])
        if mk is not None:
            mc = cells_of(mk)
            place(cover, mc > 0, o, value=int(mc.max()))
            continue
        if os.path.basename(o["path"])[:-4] in GENERATED_TERRAIN:
            missing.append(o)
            continue
        a = load_alpha_mask(o["path"])
        if a is None:
            missing.append(o)
            continue
        place(cover, a, o, value=2)
    return cover, missing


def residual(info):
    """(cells, cover, resid, missing)：resid = 碰撞非零且没被任何现存精灵盖到。"""
    cells = cells_of(info["terrain"])
    cover, missing = coverage(info)
    return cells, cover, (cells > 0) & (cover == 0), missing


def local_silhouette(info, o, resid, cells, radius=720):
    """把一处摆放周围的残差反变换到精灵局部坐标（以中心为原点，未缩放、未镜像）。

    返回 (sil, known, origin)：`sil[v, u]` = 残差值（0 / 1 平台 / 2 实心），`known` = 这个局部像素
    在世界里是否可见（没被现存精灵盖住、没出界），`origin` = (RAD, RAD) 即中心在数组里的下标。

    ★ 用**正向采样**：对局部每个像素算它落在世界哪一格再取值。反过来（世界像素往局部投）在 |缩放| < 1
    时每隔几列就有局部像素没人写到，画出来是一排竖缝（2026-09-21 踩过）。
    """
    import numpy as np
    h, w = cells.shape
    R = radius
    vs, us = np.mgrid[-R:R, -R:R]
    xs = np.rint(o["x"] + us * o["sx"]).astype(int)
    ys = np.rint(o["y"] + vs * o["sy"]).astype(int)
    inside = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
    xi = np.clip(xs, 0, w - 1)
    yi = np.clip(ys, 0, h - 1)
    covered = info["_cover"][yi, xi] > 0
    known = inside & ~covered
    sil = np.where(known & resid[yi, xi], cells[yi, xi], 0).astype(np.uint8)
    return sil, known, (R, R)


def analyse(name):
    """一张图的全部分析结果（dict），给 build / preview / --diag 共用。"""
    info = parse_full(os.path.join(MAPS_DIR, name + ".map"))
    cells, cover, resid, missing = residual(info)
    info["_cells"], info["_cover"], info["_resid"], info["_missing"] = cells, cover, resid, missing
    return info


# ---------------------------------------------------------------------------
#  --report / --diag
# ---------------------------------------------------------------------------

def report(names):
    total = collections.Counter()
    for name in names:
        path = os.path.join(MAPS_DIR, name + ".map")
        try:
            info = parse_full(path)
        except Exception as exc:  # noqa: BLE001 —— 报表要把坏图列出来而不是中止
            print("%-20s !! 解析失败：%s" % (name, exc))
            continue
        dead = dead_refs(info)
        fall = falldown_effect(name) if name in FESTIVAL_MAPS else None
        fall_missing = fall is not None and real_path(fall) is None
        if not dead and not fall_missing:
            continue
        print("== %s  ver %d  %dx%d  对象 %d  死引用 %d 条 / %d 处" % (
            name, info["ver"], info["width"], info["height"], len(info["objects"]), len(dead), sum(dead.values())))
        for (t, p), n in sorted(dead.items()):
            print("   %-9s x%-3d %s" % (TYPE_NAME.get(t, t), n, p))
            total[TYPE_NAME.get(t, t)] += 1
        if fall_missing:
            print("   %-9s x1   %s   (map.ini FallDownEffectFileName)" % ("Effect", fall))
            total["Effect"] += 1
    print("合计（去重按图）:", dict(total) or "无死引用")


def diag(names):
    """诊断图：现存精灵原色压暗 + 残差实心红 / 平台黄；每张缺失精灵的局部轮廓图。"""
    import numpy as np
    from PIL import Image
    out = os.path.join(LOG_DIR, "diag")
    os.makedirs(out, exist_ok=True)
    for name in names:
        info = analyse(name)
        cells, cover, resid = info["_cells"], info["_cover"], info["_resid"]
        h, w = cells.shape
        img = np.zeros((h, w, 3), dtype=np.uint8) + np.array([36, 28, 28], dtype=np.uint8)
        for o in sorted([o for o in info["objects"] if o["type"] in (LAYER, TERRAIN, BREAKABLE, HIDING)],
                        key=lambda o: {LAYER: 0, TERRAIN: 1, BREAKABLE: 2, HIDING: 3}[o["type"]]):
            rp = real_path(o["path"])
            if rp is None:
                continue
            a = np.array(Image.open(rp).convert("RGBA"))
            left, top, W, H = sprite_window(o, a.shape[1], a.shape[0])
            im = np.array(Image.fromarray(a).resize((W, H), Image.BILINEAR))
            if o["sx"] < 0:
                im = im[:, ::-1]
            if o["sy"] < 0:
                im = im[::-1, :]
            x0, y0 = max(0, left), max(0, top); x1, y1 = min(w, left + W), min(h, top + H)
            if x1 <= x0 or y1 <= y0:
                continue
            sub = im[y0 - top:y1 - top, x0 - left:x1 - left]
            al = sub[:, :, 3:4].astype(np.float32) / 255.0 * (0.75 if o["type"] == TERRAIN else 0.5)
            img[y0:y1, x0:x1] = (img[y0:y1, x0:x1] * (1 - al) + sub[:, :, :3] * al).astype(np.uint8)
        img[resid & (cells == 2)] = (255, 40, 40)
        img[resid & (cells == 1)] = (255, 230, 40)
        Image.fromarray(img).save(os.path.join(out, name + "_diag.png"))
        n_missing = len(info["_missing"])
        print("%s: 残差 %d 像素（实心 %d / 平台 %d），缺失地形精灵 %d 处 -> %s" % (
            name, int(resid.sum()), int((resid & (cells == 2)).sum()), int((resid & (cells == 1)).sum()),
            n_missing, os.path.join(out, name + "_diag.png")))
        for o in info["_missing"]:
            sil, known, (R, _) = local_silhouette(info, o, resid, cells, radius=400)
            ys, xs = np.nonzero(sil)
            if len(xs) == 0:
                continue
            u0, u1 = max(0, xs.min() - 20), min(2 * R, xs.max() + 21)
            v0, v1 = max(0, ys.min() - 20), min(2 * R, ys.max() + 21)
            crop = np.zeros((v1 - v0, u1 - u0, 3), dtype=np.uint8) + 60
            crop[~known[v0:v1, u0:u1]] = (30, 30, 30)
            crop[sil[v0:v1, u0:u1] == 2] = (255, 255, 255)
            crop[sil[v0:v1, u0:u1] == 1] = (255, 230, 40)
            base = os.path.basename(o["path"])[:-4]
            Image.fromarray(crop).save(os.path.join(out, "%s_%s_h%d_sil.png" % (name, base, o["handle"])))
            print("   %-9s h=%-3d xy=(%.1f,%.1f) s=(%.2f,%.2f) 局部 u[%d..%d] v[%d..%d] 像素 %d" % (
                base, o["handle"], o["x"], o["y"], o["sx"], o["sy"], xs.min() - R, xs.max() - R, ys.min() - R, ys.max() - R, len(xs)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--report", action="store_true", help="死引用表")
    ap.add_argument("--diag", action="store_true", help="诊断图 + 轮廓图")
    ap.add_argument("--all", action="store_true", help="扫 Maps/ 顶层全部 .map（缺省只扫庆典三张）")
    args = ap.parse_args(argv)
    if args.all:
        names = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(MAPS_DIR, "*.map")))
    else:
        names = list(FESTIVAL_MAPS)
    if not (args.report or args.diag):
        args.report = True
    if args.report:
        report(names)
    if args.diag:
        diag(names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
