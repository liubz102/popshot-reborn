#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""庆典三张图的资源守卫（X_Mod · X12）：`.map` 引用的贴图 / 特效必须都在盘上，尺寸要对得上掩码。

纯标准库，两套运行时都跑。它钉的是「以后别再把这批当无用文件删掉」——
这些文件是 `tools/map_festival_fill/build.py` 生成的，磁盘上看不出谁在引用它们。
"""
import importlib.util
import os
import struct
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_tools_mapdata():
    """按文件路径加载 `tools/mapdata.py`（离线 .map 解析器）。

    ★ 不能 `import mapdata`：`server/mapdata.py`（运行时加载器）同名，全量并行跑时谁先进
    `sys.modules` 谁赢，拿到 server 那份就没有 `parse_map`（2026-09-21 踩过）。
    """
    spec = importlib.util.spec_from_file_location("tools_mapdata_x12", os.path.join(ROOT, "tools", "mapdata.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mapdata = _load_tools_mapdata()

PACK = os.path.join(ROOT, "game_patched", "Pack_develop")
MAPS = ("Festival00", "Festival01", "Festival02")
FILE_TYPES = (200, 201, 202, 203, 209)
FALLDOWN_EFFECT = "Maps/Festival/Effect/FestivalWaterDamage00.efx"
#: 原版就是 Terrain 同名逐字节复制的 Cover 副本（`tr_x_09` 原版就不同，不在名单里）。
#: ★ `tr_x_12/13/14/15` 也**不是**副本 —— 它们是绳上的「炮炮火枪手」圆牌（§71），另见 `MEDALLIONS`。
COVER_COPIES = ("tr_x_08", "tr_x_10", "tr_x_16", "tr_x_17")

#: 绳上五个 `HidingObj` 用的四张圆牌：同尺寸方画布，前两个位置共用 `tr_x_15`（都是「炮」）。
MEDALLIONS = ("tr_x_12", "tr_x_13", "tr_x_14", "tr_x_15")


def real_path(rel):
    cur = PACK
    for part in rel.replace("\\", "/").split("/"):
        if not part:
            continue
        if not os.path.isdir(cur):
            return None
        names = {n.lower(): n for n in os.listdir(cur)}
        if part.lower() not in names:
            return None
        cur = os.path.join(cur, names[part.lower()])
    return cur if os.path.isfile(cur) else None


def png_header(path):
    """(宽, 高, 位深, 颜色类型)。"""
    with open(path, "rb") as fp:
        head = fp.read(29)
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise ValueError("%s 不是 PNG" % path)
    w, h, depth, ctype = struct.unpack(">IIBB", head[16:26])
    return w, h, depth, ctype


def _parsed():
    out = {}
    for name in MAPS:
        out[name] = mapdata.parse_map(os.path.join(PACK, "Maps", name + ".map"))
    return out


class FestivalAssetsPresent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.maps = _parsed()

    def test_every_referenced_file_exists(self):
        missing = []
        for name, (_ver, _w, _h, objs, _terrain, _masks) in self.maps.items():
            for o in objs:
                p = o.get("path", "")
                if p and o["type"] in FILE_TYPES and real_path(p) is None:
                    missing.append("%s: %s" % (name, p))
        self.assertEqual([], sorted(set(missing)))

    def test_falldown_effect_exists(self):
        self.assertIsNotNone(real_path(FALLDOWN_EFFECT), FALLDOWN_EFFECT)

    def test_mask_backed_sizes_match_png(self):
        """有掩码的 5 张（tr_x_32 / co_07 / co_08 / co_12 / co_13）PNG 尺寸必须等于掩码画布。"""
        seen = {}
        for name, (_ver, _w, _h, _objs, _terrain, masks) in self.maps.items():
            for rel, td in masks.items():
                if not os.path.basename(rel).startswith(("tr_x_32", "co_07", "co_08", "co_12", "co_13")):
                    continue
                rp = real_path(rel)
                self.assertIsNotNone(rp, rel)
                w, h, depth, ctype = png_header(rp)
                self.assertEqual((td["width"], td["height"]), (w, h), rel)
                seen[rel] = (w, h)
        self.assertEqual(5, len(seen), sorted(seen))

    def test_generated_pngs_are_8bit_rgba(self):
        rels = ["Terrain/tr_x_28", "Terrain/tr_x_29", "Terrain/tr_x_30", "Terrain/tr_x_31", "Terrain/tr_x_32",
                "Terrain/tr_x_33", "Terrain/tr_x_34", "Terrain/tr_x_35", "Terrain/tr_x_37",
                "Layer/la_19", "Layer/la_20", "Breakable/co_07", "Breakable/co_08", "Breakable/co_12", "Breakable/co_13"]
        rels += ["Cover/%s" % b for b in MEDALLIONS]
        for rel in rels:
            rp = real_path("Maps/Festival/%s.png" % rel)
            self.assertIsNotNone(rp, rel)
            w, h, depth, ctype = png_header(rp)
            self.assertEqual((8, 6), (depth, ctype), rel)

    def test_medallions_are_one_square_size(self):
        """绳上五块牌子必须同尺寸方画布 —— 原版靠对象缩放（0.4 / 0.5）分大小，不是靠贴图。"""
        sizes = set()
        for base in MEDALLIONS:
            rp = real_path("Maps/Festival/Cover/%s.png" % base)
            self.assertIsNotNone(rp, base)
            w, h, _depth, _ctype = png_header(rp)
            self.assertEqual(w, h, base)
            sizes.add(w)
        self.assertEqual(1, len(sizes), sorted(sizes))

    def test_cover_medallions_are_not_terrain_copies(self):
        """守住 2026-09-21 那次返工：这四张一旦又被当成 Terrain 副本抄回去，绳上就变回柱子和墙块。"""
        for base in MEDALLIONS:
            a = real_path("Maps/Festival/Cover/%s.png" % base)
            b = real_path("Maps/Festival/Terrain/%s.png" % base)
            self.assertIsNotNone(a, base)
            self.assertIsNotNone(b, base)
            with open(a, "rb") as fa, open(b, "rb") as fb:
                self.assertNotEqual(fa.read(), fb.read(), base)

    def test_cover_copies_are_byte_identical_to_terrain(self):
        for base in COVER_COPIES:
            a = real_path("Maps/Festival/Cover/%s.png" % base)
            b = real_path("Maps/Festival/Terrain/%s.png" % base)
            self.assertIsNotNone(a, base)
            self.assertIsNotNone(b, base)
            with open(a, "rb") as fa, open(b, "rb") as fb:
                self.assertEqual(fa.read(), fb.read(), base)


class FestivalEffectsWellFormed(unittest.TestCase):
    def _all_efx(self):
        maps = _parsed()
        rels = set()
        for _name, (_ver, _w, _h, objs, _terrain, _masks) in maps.items():
            for o in objs:
                if o["type"] == 209 and o["path"].startswith("Maps/Festival/"):
                    rels.add(o["path"])
        rels.add(FALLDOWN_EFFECT)
        return sorted(rels)

    def test_count_and_parse(self):
        rels = self._all_efx()
        self.assertEqual(28, len(rels), rels)
        for rel in rels:
            rp = real_path(rel)
            self.assertIsNotNone(rp, rel)
            with open(rp, "rb") as fp:
                blob = fp.read()
            self.assertTrue(blob.startswith(b'<?xml version="1.0" encoding="ks_c_5601-1987"?>'), rel)
            text = blob.decode("cp949")
            root = ET.fromstring(text.split("?>", 1)[1])
            layers = root.findall("./EffLayers/EffLayer")
            self.assertGreaterEqual(len(layers), 1, rel)
            for layer in layers:
                tex = layer.findtext("TextureFileName") or ""
                if tex:
                    self.assertTrue(os.path.isfile(os.path.join(PACK, "Effects", tex.replace("\\", os.sep))), "%s -> %s" % (rel, tex))
                self.assertIn(layer.findtext("DrawStyle"), ("Brighten", "Normal", "Darken"), rel)


if __name__ == "__main__":
    unittest.main()
