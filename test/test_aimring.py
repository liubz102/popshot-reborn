#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""准星弹格盘（X_Mod · X4，FINDINGS §43）—— 资源、生成器、hook 那张表三边对账。

病根：客户端 `0x48ca0d` 按弹匣容量**精确匹配**挑刻度盘，只认 2/3/6/10/14/18，
其余一律返回帧 4（一张没有刻度的光滑圆环）。自定义武器配成 15 / 20 发就撞上它。

这里能离线钉死的四件事：

1. **图集和表不许分叉** —— `aimring.py` 说有几帧，`AimPoint.smf` 就得真有几帧，
   且每个帧号都落在图集里面（越界帧号 = 客户端拿野指针画图，§3 那类崩法）；
2. **原版资源一个字节不动** —— 前 19 帧的 `.smf` 记录、以及它们覆盖到的像素区域；
3. **`hook/aimring.h` 是生成物** —— 和 `tools/aimring.py` 分叉当场抓住（手改头文件没用）；
4. **补丁站点** —— `0x48ca0d` 的特征字节拿脱壳镜像验，并确认原版那 6 个容量
   在我们的表里仍然返回原版帧号。
"""
import os
import re
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for sub in ("tools", "test"):
    path = os.path.join(ROOT, sub)
    if path not in sys.path:
        sys.path.insert(0, path)

import aimring  # noqa: E402

PNG = aimring.PNG_PATH
SMF = aimring.SMF_PATH
HEADER = aimring.HEADER_PATH


def _skip_unless(*paths):
    for p in paths:
        if not os.path.isfile(p):
            raise unittest.SkipTest("不在源码仓库里（缺 %s），跳过" % p)


class DialTableTest(unittest.TestCase):
    """那张 `容量 → 帧号` 的表本身。"""

    def test_every_capacity_from_2_to_20_has_a_dial(self):
        for cap in range(aimring.DIAL_MIN, aimring.DIAL_MAX + 1):
            self.assertIn(cap, aimring.DIAL, "容量 %d 没有刻度盘" % cap)

    def test_the_six_original_capacities_still_map_to_the_original_frames(self):
        """原版武器的观感不许变 —— 2/3/6/10/14/18 必须还是原来那几帧。"""
        self.assertEqual({2: 6, 3: 5, 6: 6, 10: 8, 14: 7, 18: 9}, aimring.ORIG_DIAL)
        for cap, frame in aimring.ORIG_DIAL.items():
            self.assertEqual(frame, aimring.DIAL[cap])
            self.assertLess(frame, aimring.ORIG_FRAMES, "原版容量不该指向新帧")

    def test_new_frames_are_distinct_and_start_after_the_original_ones(self):
        frames = [aimring.NEW_DIAL[c] for c in aimring.NEW_CAPS]
        self.assertEqual(len(frames), len(set(frames)), "补画的帧号有重复")
        self.assertEqual(list(range(aimring.ORIG_FRAMES, aimring.TOTAL_FRAMES)), sorted(frames))
        for f in frames:
            self.assertGreaterEqual(f, aimring.ORIG_FRAMES)

    def test_capacities_outside_the_range_have_no_dial(self):
        """`< 2` 客户端压根不画外圈；`> 20` 是用户拍板走光滑圆环。"""
        for cap in (0, 1, aimring.DIAL_MAX + 1, 100, 999):
            self.assertNotIn(cap, aimring.DIAL, "容量 %d 不该有刻度盘" % cap)


class AtlasTest(unittest.TestCase):
    """资源侧：`.smf` 的帧表、以及原版那 19 帧没被动过。"""

    @classmethod
    def setUpClass(cls):
        _skip_unless(PNG, SMF)
        cls.recs = aimring.read_smf(SMF)

    def test_the_atlas_has_exactly_the_frames_the_generator_claims(self):
        self.assertEqual(aimring.TOTAL_FRAMES, len(self.recs),
                         "AimPoint.smf 的帧数和 aimring.py 对不上 —— 跑一次 tools/aimring.py")

    def test_the_original_nineteen_frames_are_untouched(self):
        import hashlib
        blob = aimring.orig_smf_blob(self.recs)
        self.assertEqual(aimring.ORIG_SMF_SHA256, hashlib.sha256(blob).hexdigest(),
                         "AimPoint.smf 的前 19 帧被改过了")

    def test_every_frame_number_in_the_table_exists_in_the_atlas(self):
        """★ 越界帧号 = 客户端 `[数组 + 帧号*4]` 拿野指针去画（§3 那类崩法）。"""
        for cap, frame in sorted(aimring.DIAL.items()):
            self.assertLess(frame, len(self.recs), "容量 %d 指到第 %d 帧，图集里没有" % (cap, frame))
        self.assertLess(aimring.DEFAULT_FRAME, len(self.recs))

    def test_new_frames_sit_inside_the_image_and_do_not_overlap_the_old_ones(self):
        # 尺寸直接从 PNG 的 IHDR 读 —— 内置运行时没有 Pillow，这条覆盖不能因此丢掉。
        head = open(PNG, "rb").read(24)
        self.assertEqual(b"\x89PNG\r\n\x1a\n", head[:8], "不是 PNG")
        w, h = struct.unpack(">II", head[16:24])
        old = self.recs[:aimring.ORIG_FRAMES]
        for idx in range(aimring.ORIG_FRAMES, aimring.TOTAL_FRAMES):
            x0, y0, x1, y1 = self.recs[idx][:4]
            self.assertTrue(0 <= x0 < x1 < w and 0 <= y0 < y1 < h,
                            "第 %d 帧 (%d,%d)-(%d,%d) 超出 %dx%d" % (idx, x0, y0, x1, y1, w, h))
            self.assertEqual((aimring.FRAME_W, aimring.FRAME_H), (x1 - x0 + 1, y1 - y0 + 1))
            for o in old:
                overlap = not (x1 < o[0] or o[2] < x0 or y1 < o[1] or o[3] < y0)
                self.assertFalse(overlap, "第 %d 帧压在原版帧 %s 上" % (idx, o[:4]))

    def test_new_frames_actually_have_that_many_slots(self):
        """画出来的格子数真的等于容量 —— 沿白带中线数一圈亮段。

        ★ 要解 PNG，只能在装了 Pillow 的开发 Python（`C:\\Python314`）下跑；
        内置运行时没有它，那边跳过（上面几条不依赖 Pillow 的仍然全跑）。
        """
        import math
        try:
            from PIL import Image
        except ImportError:
            raise unittest.SkipTest("没有 Pillow（内置运行时），跳过像素级核对")
        img = Image.open(PNG).convert("RGBA")
        for cap in aimring.NEW_CAPS:
            x0, y0, x1, y1 = self.recs[aimring.NEW_DIAL[cap]][:4]
            cell = img.crop((x0, y0, x1 + 1, y1 + 1)).load()
            lit = []
            steps = 3600
            for i in range(steps):
                a = math.radians(i * 360.0 / steps - 90.0)
                # 半径取白带正中，避开内外黑边
                x = int(round(aimring.CENTER + 23.5 * math.cos(a)))
                y = int(round(aimring.CENTER + 23.5 * math.sin(a)))
                r, g, b, alpha = cell[x, y]
                lit.append(r > 150 and alpha > 100)
            runs = sum(1 for i in range(steps) if lit[i] and not lit[i - 1])
            self.assertEqual(cap, runs, "容量 %d 的盘上数出来 %d 格" % (cap, runs))


class GeneratedHeaderTest(unittest.TestCase):
    """`hook/aimring.h` 是生成物 —— 手改它没用，这里当场抓分叉。"""

    @classmethod
    def setUpClass(cls):
        _skip_unless(HEADER)
        with open(HEADER, "r", encoding="utf-8", newline="") as fp:
            cls.text = fp.read().replace("\r\n", "\n")

    def test_the_header_is_exactly_what_the_generator_renders(self):
        self.assertEqual(aimring.render_header(), self.text,
                         "hook/aimring.h 和 tools/aimring.py 对不上 —— "
                         "跑一次 python tools/aimring.py --header-only")

    def test_the_header_table_agrees_with_python(self):
        """不只比字符串 —— 把 C 数组解出来逐格对，防止渲染函数自己写错。"""
        body = re.search(r"POPSHOT_AIM_FRAME\[[^\]]*\]\s*=\s*\{(.*?)\};", self.text, re.S)
        self.assertIsNotNone(body, "头文件里找不到 POPSHOT_AIM_FRAME")
        values = [int(v) for v in re.findall(r"^\s*/\*[^*]*\*/\s*(\d+),", body.group(1), re.M)]
        self.assertEqual(aimring.DIAL_MAX + 1, len(values))
        for cap, got in enumerate(values):
            want = aimring.DIAL.get(cap, aimring.DEFAULT_FRAME)
            self.assertEqual(want, got, "容量 %d：C 表是 %d，Python 表是 %d" % (cap, got, want))

    def test_the_header_is_crlf_without_bom(self):
        """仓库里的 .c/.h 都是 CRLF（铁律 3）。"""
        raw = open(HEADER, "rb").read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "不该有 BOM")
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), "有 LF-only 的行")


class PatchSiteTest(unittest.TestCase):
    """补丁站点：`0x48ca0d` 的特征字节拿脱壳镜像验一遍。"""

    @classmethod
    def setUpClass(cls):
        try:
            import test_patchsites as base
        except ImportError:
            raise unittest.SkipTest("没有 test_patchsites")
        _skip_unless(base.BSHOOK, base.IMG)
        cls.base = base
        cls.img = base.load_image()
        cls.src = base.c_source()

    def test_the_signature_is_what_the_image_has(self):
        va = self.base.c_define(self.src, "AIM_RING_VA")
        sig = self.base.c_byte_array(self.src, "AIM_RING_SIG")
        self.assertEqual(sig, self.base.read_va(self.img, va, len(sig)),
                         "0x48ca0d 的特征串和镜像对不上")

    def test_we_overwrite_five_whole_bytes_that_do_not_split_an_instruction(self):
        """`test ecx,ecx`(2) + `push 4`(2) + `pop eax`(1) = 5 字节，E9 正好盖住这三条。"""
        sig = self.base.c_byte_array(self.src, "AIM_RING_SIG")
        self.assertEqual(bytes([0x85, 0xC9]), sig[0:2])   # test ecx, ecx
        self.assertEqual(bytes([0x6A, 0x04]), sig[2:4])   # push 4
        self.assertEqual(0x58, sig[4])                     # pop  eax

    def test_the_site_is_unique_in_the_image(self):
        sig = self.base.c_byte_array(self.src, "AIM_RING_SIG")
        self.assertEqual(1, self.img.count(sig), "特征串不唯一，镜像不是这一版？")

    def test_the_original_switch_really_only_knows_six_capacities(self):
        """把原版那串 `dec/je/sub` 解出来，确认我们接管的就是这 6 个值（§43）。

        `mov ecx,[ecx+0x60]` 之后：`dec,dec,je→6` `dec,je→5` `sub 3,je→6`
        `sub eax(=4),je→8` `sub eax,je→7` `sub eax,jne→默认` `push 9`。
        """
        va = self.base.c_define(self.src, "AIM_RING_VA")
        code = self.base.read_va(self.img, va, 0x36)
        self.assertEqual(bytes([0x8B, 0x49, 0x60]), code[7:10])   # mov ecx,[ecx+0x60]
        # 累计减掉的量 = 命中的容量：2,3,6,10,14,18
        self.assertEqual(bytes([0x49, 0x49]), code[10:12])        # dec ecx ×2 -> cap==2
        self.assertEqual(0x74, code[12])                          # je
        self.assertEqual(0x49, code[14])                          # dec ecx -> cap==3
        self.assertEqual(bytes([0x83, 0xE9, 0x03]), code[17:20])  # sub ecx,3 -> cap==6
        for off in (0x16, 0x1A, 0x1E):                            # sub ecx,eax ×3 -> 10/14/18
            self.assertEqual(bytes([0x2B, 0xC8]), code[off:off + 2])
        # 进函数时 push 4 / pop eax：默认帧就是 4，和我们的 DEFAULT_FRAME 一致
        self.assertEqual(aimring.DEFAULT_FRAME, code[3])

    def test_bshook_includes_the_generated_header_and_hardcodes_no_frame_number(self):
        with open(self.base.BSHOOK, "r", encoding="utf-8", errors="replace") as fp:
            src = fp.read()
        self.assertIn('#include "aimring.h"', src)
        # 帧号只许从生成的宏 / 表里来
        chunk = src[src.index("AIM_RING_VA"):src.index("CODE_PATCH_DELAY_MS")]
        self.assertIn("POPSHOT_AIM_FRAME[cap]", chunk)
        self.assertIn("POPSHOT_AIM_DEFAULT_FRAME", chunk)


if __name__ == "__main__":
    unittest.main()
