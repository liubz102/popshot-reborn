#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mapdata.py —— 从原版 `.map` 里离线提取**地图地形数据**（V0.3 M4）。

    python tools\\mapdata.py                    # 全量提取到 server\\bot_mapdata\\
    python tools\\mapdata.py --verify Camel00   # 顺便导出可视化 PNG 人工核对

## 数据从哪来：**不是**从 `.map` 的对象列表里拼出来的

`.map` 的对象表（`TerrainObj` 等）只有「位置 + 缩放 + 旋转 + 贴图路径」，
形状在那张 PNG 的 alpha 里 —— 照那条路走要装 Pillow、要处理镜像和弧度旋转，
还得自己决定「多大的 alpha 才算实心」。

**但客户端根本不这么干。** `.map` 文件的**尾部**烘着一份 `TerrainData`：
和地图**同尺寸**、**每像素 2 bit** 的碰撞位图，RLE 压缩。客户端自己的碰撞
查询 `TerrainData::Get(x, y)`（`0x472fe0`）读的就是它。所以本工具直接搬这一份 ——
**和客户端逐像素一致**，而且整个提取过程只用标准库（`--verify` 才要 Pillow）。

格值的含义（`0x472fe0` 返回 0..3，越大越「硬」，`0x51a9e0` 对邻域取 max）：

    0  空
    1  薄的可站立面（细绳桥 / 藤蔓 / 窄檐）—— 实测有 77 个出生点直接落在它上面
    2  实心
    3  原版 174 张图里**一个都没有**；只在运行时出现（`0x4fa844` 拿它当哨兵）

    ★ **出界返回 2**（不是 0）—— 照抄，别改成「出界算空」。

## 产物：纯数据，服务端运行时不许依赖 Pillow

★ 放 `server/bot_mapdata/`，**不放 `server/data/`** —— 那个目录按约定
只装**用户数据**（`accounts.json` / `tickets.json`，都是 `.gitignore` 掉的
运行时状态）。地形数据是**随代码走的产物**：进 git、进两个发布包。

`server/bot_mapdata/index.json` + 每张图一个 `<名字>.json`：

    cells    zlib+base64 的 2bit/像素位图（原始 132 MB -> 全 174 张合计约 1 MB）
    ground   每一列 x 的**站立面** y 列表（实心区的上沿），zlib+base64 的 uint16
    points   出生点 / 重生点 / 刷怪区等玩法坐标

读它的是 `server/mapdata.py`（只用标准库，CPython 3.8 也能跑）。

## 一次跑完要多久

开发机上约 40 秒 / 174 张。打包脚本会先调它一次，产物缺失或解析失败就中止打包。
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: `.map` 的 19 次分类循环里，这些 type 是纯装饰/地形，不进 `points`。
DECOR_TYPES = frozenset((200, 201, 202, 203, 209))

#: v<13 的 type 不在文件里，靠贴图路径的第 3 段目录名判（客户端 `0x5130a8`）。
DIR_TYPE = {"TERRAIN": 200, "LAYER": 202, "COVER": 201,
            "EFFECT": 209, "BREAKABLE": 203}

#: 产物格式版本。改了布局就 +1，`server/mapdata.py` 会拒绝不认识的版本。
FORMAT = 1


class MapFormatError(Exception):
    """`.map` 没按预期的样子长 —— 宁可炸，也不要产出半张地图。"""


# ---------------------------------------------------------------------------
#  读流原语：和客户端那个序列化器一一对应
# ---------------------------------------------------------------------------

class Reader(object):
    """按客户端 `0x5d59xx` 那套原语读字节流。"""

    def __init__(self, buf, pos=0):
        self.buf = buf
        self.pos = pos

    def _take(self, n):
        end = self.pos + n
        if end > len(self.buf):
            raise MapFormatError(
                "读越界：要 %d 字节，只剩 %d" % (n, len(self.buf) - self.pos))
        out = self.buf[self.pos:end]
        self.pos = end
        return out

    def u8(self):
        return self._take(1)[0]

    def u16(self):
        return struct.unpack("<H", self._take(2))[0]

    def i32(self):
        return struct.unpack("<i", self._take(4))[0]

    def f32(self):
        return struct.unpack("<f", self._take(4))[0]

    def raw(self, n):
        return self._take(n)

    def wstr(self):
        """u16 字符数 + UTF-16LE（`0x5d5b3a`）。"""
        return self._take(self.u16() * 2).decode("utf-16le")

    def left(self):
        return len(self.buf) - self.pos


# ---------------------------------------------------------------------------
#  `.map` 容器：World::LoadMapData（0x47429e）
# ---------------------------------------------------------------------------

def _type_from_path(path):
    """v<13 没有 type 字段，客户端拿贴图路径去判（`0x5130a8`）。"""
    parts = path.split("/")
    if len(parts) >= 4:
        found = DIR_TYPE.get(parts[2].upper(), 0)
        if found:
            return found
    name = parts[-1] if parts else ""
    # `0x513051`：文件名形如 `108-xxx.png` 时，头三位数字就是 type。
    if len(name) >= 4 and name[:3].isdigit() and name[3] == "-":
        return int(name[:3])
    return 0


def _read_obj_blob(blob, ver):
    """MapObj::Deserialize（`0x511da0`）—— `TerrainObj` 是纯 `jmp`，没有自己的字段。"""
    r = Reader(blob)
    obj = {}
    if ver >= 17:
        r.i32(); r.i32(); r.i32()
    if ver >= 12:
        obj["x"] = r.f32()
        obj["y"] = r.f32()
        r.f32(); r.f32()          # 缩放（负数 = 镜像）
        r.f32(); r.f32(); r.f32()  # 后两个没用上，最后一个是弧度旋转
        r.wstr()                   # 名字
        path = r.wstr()
        obj["path"] = path.replace("//", "/") if ver >= 13 else path
    if ver >= 10:
        for _ in range(r.i32()):
            r.wstr(); r.wstr()     # 属性表（key/value）
    return obj, r.left()


def _read_terrain_data(r):
    """TerrainData::Load（`0x47c4a4`）—— 2bit/像素的碰撞位图 + RLE。"""
    r.i32()                        # 恒 1，像是子格式版本
    width = r.i32()
    height = r.i32()
    if width <= 0 or height <= 0:
        raise MapFormatError("TerrainData 尺寸不合法：%dx%d" % (width, height))
    # 客户端按 ((w*h+15)/16)*4 字节分配 —— 每字节 4 格、每格 2 bit。
    need = ((width * height + 15) // 16) * 4
    packed = r.raw(r.i32())
    out = bytearray()
    # RLE：(count u8, value u8) 一对一对地 memset（`0x5f47e0`）。
    for i in range(0, len(packed) - 1, 2):
        out += bytes((packed[i + 1],)) * packed[i]
    if len(out) != need:
        raise MapFormatError(
            "TerrainData 解压后 %d 字节，应为 %d" % (len(out), need))
    return {"width": width, "height": height, "cells": bytes(out)}


def parse_map(path):
    """解一个 `.map`，返回 (版本, 宽, 高, 对象表, TerrainData)。"""
    with open(path, "rb") as fp:
        r = Reader(fp.read())
    ver = r.u16()
    width = r.i32()
    height = r.i32()
    r.raw(r.i32())                 # ini 文本，全是背景渲染参数，和地形无关
    if ver >= 10:
        r.wstr()
    objects = []
    for _cat in range(19):         # 0x47453b 写死 0x13 类
        for _ in range(r.i32()):
            if ver >= 13:
                otype = r.i32()
                r.i32()
                obj, _left = _read_obj_blob(r.raw(r.i32()), ver)
                obj["type"] = otype
            else:
                r.i32()
                if ver >= 9:
                    r.wstr()       # 名字
                else:
                    r.i32()
                pos = [r.f32() for _ in range(7)]
                tex = r.wstr()
                _read_obj_blob(r.raw(r.i32()), ver)
                obj = {"type": _type_from_path(tex), "path": tex,
                       "x": pos[0], "y": pos[1]}
            objects.append(obj)
    terrain = _read_terrain_data(r)
    # 尾部还有一张「贴图路径 -> 该精灵的掩码」的表，破坏物碎掉时拿它抠格子用。
    # bot 用不上，跳过即可 —— 但要把它读完，读不完说明前面的字段错位了。
    for _ in range(r.i32()):
        r.wstr()
        _read_terrain_data(r)
    if r.left() > 1:               # testipkn.map 尾部多 1 字节填充
        raise MapFormatError("文件尾还剩 %d 字节没解释" % r.left())
    if (width, height) != (terrain["width"], terrain["height"]):
        raise MapFormatError(
            "地图 %dx%d 和 TerrainData %dx%d 不一致"
            % (width, height, terrain["width"], terrain["height"]))
    return ver, width, height, objects, terrain


# ---------------------------------------------------------------------------
#  从位图里抽站立面
# ---------------------------------------------------------------------------

#: 一字节 4 格 -> 4 个字节，`bytes.join` 展开时用。
_UNPACK = tuple(bytes(((b >> 0) & 3, (b >> 2) & 3, (b >> 4) & 3, (b >> 6) & 3))
                for b in range(256))
#: 展开后再 translate 成「非空 = 1」，走 C 层，比逐格 if 快两个数量级。
_SOLID = bytes((0, 1, 1, 1)) + bytes(252)


def unpack_cells(packed, width, height):
    """2bit/格 -> 1 字节/格（值仍是 0..3），行优先。"""
    flat = b"".join(map(_UNPACK.__getitem__, packed))
    return flat[:width * height]


def extract_ground(cells, width, height):
    """每一列的**站立面**：实心区的上沿 y（`cells[x,y]` 非空且正上方是空）。

    ★ 出界照客户端算**实心**（`0x472fe0` 越界返回 2），所以 y=0 那一行
    即使实心也不是站立面 —— 上方是「墙」不是天空。
    """
    solid = cells.translate(_SOLID)
    per_col = [[] for _ in range(width)]
    # ★ 逐格 if 在这里要跑几百万次 Python 循环。改成把整行当一个大整数：
    # 「这一行实心 且 上一行空」= `cur & ~prev`，一次位运算出一整行的上沿，
    # 剩下的 find 只在真有上沿的地方走（全图也就几千个）。
    prev = int.from_bytes(solid[0:width], "big")
    for y in range(1, height):
        cur = int.from_bytes(solid[y * width:(y + 1) * width], "big")
        edge = cur & ~prev
        prev = cur
        if not edge:
            continue
        row = edge.to_bytes(width, "big")
        start = 0
        while True:
            idx = row.find(1, start)
            if idx < 0:
                break
            per_col[idx].append(y)
            start = idx + 1
    counts = [len(col) for col in per_col]
    ys = [y for col in per_col for y in col]
    return counts, ys


# ---------------------------------------------------------------------------
#  产物编码
# ---------------------------------------------------------------------------

def _blob(raw):
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def build_record(name, ver, width, height, objects, terrain):
    cells = unpack_cells(terrain["cells"], width, height)
    counts, ys = extract_ground(cells, width, height)
    if height > 0xFFFF or (counts and max(counts) > 0xFFFF):
        raise MapFormatError("%s 的站立面超出 uint16 能表达的范围" % name)
    points = collections.OrderedDict()
    for obj in objects:
        otype = obj.get("type", 0)
        if otype in DECOR_TYPES or not otype or "x" not in obj:
            continue
        points.setdefault(str(otype), []).append(
            [int(obj["x"]), int(obj["y"])])
    return collections.OrderedDict((
        ("format", FORMAT),
        ("name", name),
        ("version", ver),
        ("width", width),
        ("height", height),
        # 原样搬客户端的位图：2 bit/像素，行优先，低位在前。
        ("cells", _blob(terrain["cells"])),
        ("ground_counts", _blob(struct.pack("<%dH" % width, *counts))),
        ("ground_ys", _blob(struct.pack("<%dH" % len(ys), *ys))),
        ("points", points),
    ))


# ---------------------------------------------------------------------------
#  --verify 的可视化（**只在开发机跑**，服务端不依赖 Pillow）
# ---------------------------------------------------------------------------

def write_preview(record, cells, out_path, scale=4):
    """画一张缩略图：实心 / 薄板 / 站立面 / 出生点各一个颜色。

    ★ 没装 Pillow **不算失败** —— 提取本身一点都不依赖它，只是画不出图。
    便携运行时里就没有 Pillow，那条路上跑到这里直接返回 False。
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    width, height = record["width"], record["height"]
    ow, oh = max(1, width // scale), max(1, height // scale)
    palette = {0: (16, 16, 20), 1: (255, 92, 92), 2: (86, 200, 110),
               3: (90, 150, 255)}
    img = Image.new("RGB", (ow, oh))
    px = img.load()
    for y in range(oh):
        base = (y * scale) * width
        for x in range(ow):
            px[x, y] = palette[cells[base + x * scale]]
    # 站立面画成白点，肉眼核对「地面线」对不对。
    counts = struct.unpack("<%dH" % width,
                           zlib.decompress(base64.b64decode(record["ground_counts"])))
    ys_raw = zlib.decompress(base64.b64decode(record["ground_ys"]))
    ys = struct.unpack("<%dH" % (len(ys_raw) // 2), ys_raw)
    pos = 0
    for x in range(width):
        n = counts[x]
        sx = x // scale
        if sx < ow:
            for i in range(n):
                sy = ys[pos + i] // scale
                if sy < oh:
                    px[sx, sy] = (255, 255, 255)
        pos += n
    # 出生点（101/102）和重生点（108）画成十字：核对「点是不是悬在地面上方」。
    marks = {"101": (90, 150, 255), "102": (255, 90, 90), "108": (255, 220, 60)}
    for otype, color in marks.items():
        for (mx, my) in record["points"].get(otype, ()):
            cx, cy = mx // scale, my // scale
            for dx in range(-2, 3):
                if 0 <= cx + dx < ow and 0 <= cy < oh:
                    px[cx + dx, cy] = color
            for dy in range(-2, 3):
                if 0 <= cx < ow and 0 <= cy + dy < oh:
                    px[cx, cy + dy] = color
    img.save(out_path)
    return True


# ---------------------------------------------------------------------------
#  入口
# ---------------------------------------------------------------------------

def find_pack_root(explicit=None):
    """找 `Pack_decrypt/`。它太大没进本工作副本，只在 `main` worktree 里。"""
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append(os.path.join(ROOT, "Pack_decrypt"))
    candidates.append(os.path.abspath(
        os.path.join(ROOT, "..", "..", "main", "Pack_decrypt")))
    for cand in candidates:
        if os.path.isdir(os.path.join(cand, "Maps")):
            return cand
    raise SystemExit(
        "找不到 Pack_decrypt\\Maps。试过：\n  " + "\n  ".join(candidates)
        + "\n用 --pack 指定，例如 --pack D:\\git\\popshot-reborn\\main\\Pack_decrypt")


def main(argv=None):
    ap = argparse.ArgumentParser(description="从原版 .map 提取地形数据")
    ap.add_argument("--pack", help="Pack_decrypt 目录")
    ap.add_argument("--out", help="输出目录（默认 server\\bot_mapdata）")
    ap.add_argument("--verify", nargs="*", metavar="地图名",
                    help="额外导出可视化 PNG；不给名字就挑 5 张有代表性的")
    ap.add_argument("--preview-dir",
                    help="可视化 PNG 放哪（默认 logs\\mapdata-preview）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    pack = find_pack_root(args.pack)
    maps_dir = os.path.join(pack, "Maps")
    out_dir = args.out or os.path.join(ROOT, "server", "bot_mapdata")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    names = sorted(f[:-4] for f in os.listdir(maps_dir)
                   if f.lower().endswith(".map"))
    if not names:
        raise SystemExit("%s 里一个 .map 都没有" % maps_dir)

    verify = args.verify
    if verify is not None and not verify:
        verify = ["Camel00", "Beginner", "Megatron_b", "Quest02_2#Normal",
                  "Forest00"]
    verify = set(verify or ())
    # ★ 可视化 PNG **不放进产物目录** —— 那个目录整个会被打进服务端包，
    # 而预览图是开发机上给人看的，进了包只是白占体积。
    preview_dir = args.preview_dir or os.path.join(ROOT, "logs",
                                                   "mapdata-preview")
    if verify and not os.path.isdir(preview_dir):
        os.makedirs(preview_dir)

    index = collections.OrderedDict()
    bases = collections.OrderedDict()
    written = set()
    for name in names:
        try:
            ver, width, height, objects, terrain = parse_map(
                os.path.join(maps_dir, name + ".map"))
            record = build_record(name, ver, width, height, objects, terrain)
        except MapFormatError as exc:
            raise SystemExit("解析 %s.map 失败：%s" % (name, exc))
        fname = name + ".json"
        # ★ `newline="\n"`：不写它的话 Windows 的文本模式会把 `\n` 转成
        #   `\r\n`，而项目铁律 3 要求 `.json` 一律 **LF 无 BOM**。
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8",
                  newline="\n") as fp:
            json.dump(record, fp, ensure_ascii=False, separators=(",", ":"))
            fp.write("\n")
        written.add(fname)
        nsurf = len(zlib.decompress(base64.b64decode(record["ground_ys"]))) // 2
        index[name] = collections.OrderedDict((
            ("file", fname), ("version", ver),
            ("width", width), ("height", height), ("surfaces", nsurf)))
        base = name.split("#", 1)[0]
        if base != name:
            bases.setdefault(base, []).append(name)
        if name in verify:
            png = os.path.join(preview_dir, "%s.png" % name.replace("#", "_"))
            drawn = write_preview(
                record, unpack_cells(terrain["cells"], width, height), png)
            if not args.quiet:
                print("   可视化 -> %s" % png if drawn
                      else "   没装 Pillow，跳过可视化（提取本身不受影响）")
        if not args.quiet:
            print("%-28s v%-2d %5dx%-5d 站立面 %6d" % (name, ver, width, height, nsurf))

    idx = collections.OrderedDict((
        ("format", FORMAT), ("count", len(index)),
        ("maps", index), ("bases", bases)))
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8",
              newline="\n") as fp:
        json.dump(idx, fp, ensure_ascii=False, indent=1, sort_keys=False)
        fp.write("\n")
    written.add("index.json")

    # 上一次跑剩下的、这次没再生成的 .json 全删掉 —— 免得改了名字之后
    # 包里同时带着新旧两份，服务端按索引找不到、按目录扫又扫得到。
    stale = 0
    for fname in os.listdir(out_dir):
        if fname.endswith(".json") and fname not in written:
            os.remove(os.path.join(out_dir, fname))
            stale += 1

    if not args.quiet:
        total = sum(os.path.getsize(os.path.join(out_dir, f))
                    for f in os.listdir(out_dir) if f.endswith(".json"))
        print("\n完成：%d 张地图 -> %s（合计 %.1f MB%s）"
              % (len(index), out_dir, total / 1048576.0,
                 "，清掉 %d 个过期文件" % stale if stale else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
