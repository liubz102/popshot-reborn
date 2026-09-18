#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文版图集的**帧数下限** —— 少一帧就是 `C0000005`，而且不会提前报警。

exe 画图有两条路：

* 走 `ImageSet::Draw`（`0x5ccefc`）—— **带边界检查**，帧号越界只是不画；
* 直接从 `[图集+0x24]` 按**写死的常量下标**取帧指针 —— **没有检查**，
  取到数组外的堆垃圾，非空就当成对象用，下一条 `mov` 就炸。

只有第二条路会崩。全镜像扫下来（`mov r,[x+0x24]` 紧跟 `mov r,[x+r*4+K]`），
带高位常量的取帧点**只出现在两处**，就是下面这张表。

`Data/Chinese.ini` 把这些图集重定向到 `Images/Chinese/` 下的中文版，
而中文版的 `.smf` 被翻译组**截短过**：帧表少几条，图也没画。
少的那几帧平时走不到，一旦走到就是必崩 —— 所以只能在这里钉死下限。

⚠ 依赖仓库布局（`game_patched/Pack_develop/`），发布包里没有它，自动跳过。
"""
import os
import struct
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PACK = os.path.join(ROOT, "game_patched", "Pack_develop")

#: `(中文版图集, 最少帧数, exe 里那个取帧点, 走到会怎样)`。
#:
#: ★ 加条目之前先确认它真的是「直接取帧」—— 走 `0x5ccefc` 的那些帧数少了不会崩，
#:   别拿它们来污染这张表（`ShopSlotImg.smf` 中文版 20 帧 / 原版 22 帧就属于这类，
#:   而且中文版是**另画**的不是截断，照抄原版反而会错位）。
GUARDED = [
    (
        os.path.join("Images", "Chinese", "BigChrIcons_CN.smf"), 28,
        "0x4f676e",
        "爱琳（ChrIndex=3）的大头像是第 0x1a/0x1b 帧，进大厅就崩（FINDINGS §3）",
    ),
    (
        os.path.join("Images", "Chinese", "ClearResultSlotLadder.smf"), 24,
        "0x48e77e",
        "结算界面的「♥HEAL」标签是第 22/23 帧 —— 爱琳的回血图腾治到队友就会画它，"
        "一结算全房间同时崩（FINDINGS §34）",
    ),
]


def read_frame_count(path):
    """`.smf` 头 8 字节 = `u32 版本 + u32 帧数`；顺带核一遍文件大小自洽。"""
    with open(path, "rb") as f:
        blob = f.read()
    version, frames = struct.unpack_from("<II", blob, 0)
    return version, frames, len(blob)


def read_rgba8_png(path):
    """只读 8 位 RGBA、非隔行的 PNG，返回 `(宽, 高, 每像素 4 字节的 bytearray)`。

    ★ 故意**不用 Pillow**：两套便携运行时里都没装它（只有开发用的
    `C:\\Python314` 有），挂在 Pillow 上的断言在全量测试里会一路 skip ——
    那等于没有守卫。这里要读的就是本仓库自己的两张图，格式写死够用。
    """
    with open(path, "rb") as f:
        blob = f.read()
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} 不是 PNG")
    width = height = None
    data = bytearray()
    offset = 8
    while offset < len(blob):
        length, kind = struct.unpack_from(">I4s", blob, offset)
        body = blob[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if (depth, color, interlace) != (8, 6, 0):
                raise ValueError(f"{path} 不是 8 位 RGBA 非隔行 PNG")
        elif kind == b"IDAT":
            data += body
        elif kind == b"IEND":
            break
    raw = zlib.decompress(bytes(data))
    stride = width * 4
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        filt = raw[pos]
        row = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        for x in range(stride):
            a = row[x - 4] if x >= 4 else 0
            b = prev[x]
            c = prev[x - 4] if x >= 4 else 0
            if filt == 1:
                row[x] = (row[x] + a) & 0xFF
            elif filt == 2:
                row[x] = (row[x] + b) & 0xFF
            elif filt == 3:
                row[x] = (row[x] + (a + b) // 2) & 0xFF
            elif filt == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[x] = (row[x] + pred) & 0xFF
            elif filt:
                raise ValueError(f"{path} 第 {y} 行的过滤器 {filt} 不认识")
        out[y * stride:(y + 1) * stride] = row
        prev = row
    return width, height, out


class SmfFrameFloorTest(unittest.TestCase):
    """中文版图集的帧数不许低于 exe 写死的下标。"""

    def setUp(self):
        if not os.path.isdir(PACK):
            raise unittest.SkipTest(f"不在源码仓库里（缺 {PACK}），跳过")

    def test_guarded_atlases_have_enough_frames(self):
        for rel, need, site, hurt in GUARDED:
            with self.subTest(atlas=rel):
                path = os.path.join(PACK, rel)
                self.assertTrue(os.path.isfile(path), f"缺文件：{path}")
                version, frames, size = read_frame_count(path)
                self.assertEqual(2, version, f"{rel} 不是 .smf 版本 2")
                self.assertEqual(8 + frames * 32, size,
                                 f"{rel} 的帧数和文件大小对不上")
                self.assertGreaterEqual(
                    frames, need,
                    f"{rel} 只有 {frames} 帧（要 >= {need}）—— "
                    f"exe 在 {site} 直接按常量下标取帧，不做边界检查：{hurt}")


class LadderAtlasStillMatchesTheOriginalTest(unittest.TestCase):
    """`ClearResultSlotLadder`：补回来的那两帧得真有图，不能只有帧表。

    只补 `.smf` 不补 `.png` 也**不会崩**（矩形落在图里，画出来是一片透明），
    但标签就白补了 —— 玩家看到的还是「什么都没有」。所以这里连像素一起验。
    """

    CN = os.path.join(PACK, "Images", "Chinese", "ClearResultSlotLadder")
    KR = os.path.join(PACK, "Images", "NewUI2", "ClearResultSlotLadder")

    def setUp(self):
        if not os.path.isfile(self.CN + ".smf"):
            raise unittest.SkipTest("不在源码仓库里，跳过")

    def test_chinese_smf_is_byte_identical_to_the_original(self):
        # 中文版这份本来就是原版的**截断**（前 22 帧逐字节相同，FINDINGS §27），
        # 补法就是整份抄回来 —— 抄完两份应当完全一样。日后谁再动中文版这份，
        # 这条会先红，提醒他去看 §34。
        with open(self.CN + ".smf", "rb") as f:
            cn = f.read()
        with open(self.KR + ".smf", "rb") as f:
            kr = f.read()
        self.assertEqual(kr, cn, "中文版 ClearResultSlotLadder.smf 和原版不一致了")

    def test_heal_badge_pixels_are_not_blank(self):
        with open(self.KR + ".smf", "rb") as f:
            smf = f.read()
        frames = struct.unpack_from("<I", smf, 4)[0]
        width, height, pixels = read_rgba8_png(self.CN + ".png")
        for index in (22, 23):
            with self.subTest(frame=index):
                self.assertLess(index, frames)
                x1, y1, x2, y2 = struct.unpack_from("<4i", smf, 8 + index * 32)
                self.assertLessEqual(x2, width)
                self.assertLessEqual(y2, height)
                opaque = 0
                for y in range(y1, y2):
                    row = y * width * 4
                    opaque += sum(1 for x in range(x1, x2)
                                  if pixels[row + x * 4 + 3])
                self.assertGreater(
                    opaque, 0,
                    f"ClearResultSlotLadder.png 第 {index} 帧那块是全透明的 —— "
                    "「♥HEAL」标签没搬过来（FINDINGS §34）")


if __name__ == "__main__":
    unittest.main()
