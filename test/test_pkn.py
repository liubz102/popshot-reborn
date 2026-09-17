#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/pkn.py`（pkn 资源包读写工具）的用例。

三类：
1. **钉在真格式上**：密钥 / 卷头偏移的固定向量（V0.1 §29 实测值 + 本版推导），
   以及金样 `test/data/pkn/Effects0011.pkn`（最小的原版卷，307 KB）——
   读取器解它得到的文件表和每个文件的 sha256 必须等于 `Effects0011.expected.json`
   里记录的值，而那些 sha256 是从 `Pack_decrypt`（原版解包）算的，不是读取器自己说的。
2. **写入器和读取器对称**：合成小树 round-trip、确定性、增量、`--check`。
3. **规划**：一目录一卷、超限下钻、卷名校验、大小写重名。

全部在临时目录里做，不碰仓库（并行测试的规矩，见 run_tests.py）。
全量回读 `Pack_publish` 太慢（1~2 分钟），只在 `POPSHOT_PKN_FULL_VERIFY=1` 时跑。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")                    # 被测代码在隔壁
TOOLS = os.path.join(ROOT, "tools")
for p in (SERVER, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
# ★★ `tools/` 只能挂在**最后面**：它和 `server/` 有四个同名模块
#    （`mapdata` / `chrprops` / `shopdata` / `weapondata`），排到前面去的话，
#    同一个 worker 进程里跑在后面的分片 `import mapdata` 会拿到**离线提取器**
#    那一个，症状是 `test_mapdata` 整个跑偏（见 `test_chrprops.load_tool`
#    里同一件事的另一种解法）。这里要的只是 `pkn`，server 侧没这个名字。
if TOOLS not in sys.path:
    sys.path.append(TOOLS)

import pkn        # noqa: E402
import snow       # noqa: E402

FIXTURE_DIR = os.path.join(HERE, "data", "pkn")
FIXTURE_VOL = os.path.join(FIXTURE_DIR, "Effects0011.pkn")
FIXTURE_EXPECT = os.path.join(FIXTURE_DIR, "Effects0011.expected.json")


def sha256(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------------------
#  1. 真格式
# ---------------------------------------------------------------------------

class KeyVectorTests(unittest.TestCase):
    def test_key1_matches_the_client_observed_key(self):
        """V0.1 §29：客户端解 Pack\\Data0000.pkn 时 loadkey 收到的 16 字节。"""
        self.assertEqual(pkn.key1("Pack/Data0000.pkn").hex(),
                         "5062656e3349677b69393a3b3c3b7e7a")

    def test_key2_matches_the_second_observed_key(self):
        """V0.1 §29 观察到的「第二把密钥」= 路径串倒序派生（0x560920）。"""
        self.assertEqual(pkn.key2("Pack/Data0000.pkn").hex(),
                         "dc42c25f94c566978cf12d1b6a4e9ad1")

    def test_key1_agrees_with_snow_py(self):
        """`server/snow.py` 早就有同一条派生（`key_from_wstr`），两边不能分叉。"""
        self.assertEqual(pkn.key1("Pack/Maps0003.pkn"), snow.key_from_wstr("Pack/Maps0003.pkn"))

    def test_header_offsets_of_the_six_original_lead_volumes(self):
        """计划阶段在真卷上量出来的六组卷头偏移。"""
        for name, want in (("Data0000", 0x9D), ("Effects0000", 0xAB), ("Images0000", 0x41),
                           ("Maps0000", 0xB4), ("Models0000", 0x4F), ("Sounds0000", 0x67)):
            self.assertEqual(pkn.header_offset("Pack/%s.pkn" % name), want, name)

    def test_key3_uses_salt_and_the_full_name(self):
        salt = bytes(range(16))
        a = pkn.key3("Data/Chinese.ini", salt)
        b = pkn.key3("Chinese.ini", salt)
        c = pkn.key3("Data/Chinese.ini", bytes(16))
        self.assertEqual(len(a), 16)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        # 0x5600c0 的公式，手算第 0 字节：((0 + salt[0] + 2) * 'D') & 0xff
        self.assertEqual(a[0], ((0 + salt[0] + 2) * ord("D") + 0) & 0xFF)


class FlagsPolicyTests(unittest.TestCase):
    def test_original_policy_table(self):
        cases = {
            "a.dds": 1, "b.mtn": 1, "c.smf": 1, "d.txt": 1,
            "e.efx": 3, "f.evn": 3, "g.map": 3, "Chinese.ini": 3, "h.xml": 3,
            "i.png": 4, "j.ogg": 4, "k.jpg": 4, "l.tga": 4,
            "m.msh": 2, "n.ui": 2, "o.amf": 2,
            "p.uni": 0, "q.bmp": 0, "r.csv": 0, "s.zip": 0, "t.wav": 0, "noext": 0,
        }
        for name, want in cases.items():
            self.assertEqual(pkn.flags_for("Some/Dir/" + name), want, name)

    def test_svn_and_bak_suffixes_are_stripped_first(self):
        """原版就是这么定的：`lvl01Clear.txt.r19382` 按 .txt，`.uni.r19382` 按 .uni，`.map.bak` 按 .map。"""
        self.assertEqual(pkn.flags_for("Quest/lvl01Clear.txt.r19382"), 1)
        self.assertEqual(pkn.flags_for("Quest/lvl01Clear.uni.r19382"), 0)
        self.assertEqual(pkn.flags_for("brtest_01.map.bak"), 3)
        self.assertEqual(pkn.flags_for("Chinese.ini.r19467"), 3)

    def test_unknown_extension_defaults_to_compress_and_encrypt(self):
        self.assertEqual(pkn.flags_for("x.whatever"), 3)
        self.assertEqual(pkn.flags_for("x.PNG"), 4)      # 不分大小写


class GoldenVolumeTests(unittest.TestCase):
    """读取器钉在真格式上：原版最小卷 + 从 Pack_decrypt 算出来的期望值。

    ★★ 金样不在就**红**，不是跳过（2026-09-18）。它被当成「无用的测试数据」
      删过一次（`ad43f621`），当时之所以没人发现，正是因为「不在就跳过」——
      少了这 4 条，pkn 读取器就只剩「自己和自己对得上」的 round-trip，
      格式解错了也照样全绿。缺了要吵，别沉默。
    """

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(FIXTURE_VOL):
            raise AssertionError(
                "缺金样 %s —— 这是仓库里的夹具，不是可选项，"
                "别跳过它，把它找回来（见 test/README.md）。" % FIXTURE_VOL)
        with open(FIXTURE_EXPECT, "r", encoding="utf-8") as f:
            cls.expect = json.load(f)
        cls.vol = pkn.Volume(FIXTURE_VOL)

    def test_header(self):
        e = self.expect
        self.assertEqual(self.vol.root, e["root"])
        self.assertEqual(self.vol.count, e["count"])
        self.assertEqual(self.vol.stamp, e["stamp"])
        self.assertEqual(self.vol.gap, e["gap"])
        self.assertEqual(self.vol.header_offset, e["header_offset"])
        self.assertEqual(self.vol.data_offset, e["data_offset"])

    def test_table(self):
        got = [(x.name, x.flags, x.blk, x.size, x.stored) for x in self.vol.entries]
        want = [(x["name"], x["flags"], x["blk"], x["size"], x["stored"]) for x in self.expect["entries"]]
        self.assertEqual(got, want)

    def test_every_entry_decodes_to_the_original_bytes(self):
        for x, e in zip(self.expect["entries"], self.vol.entries):
            data = self.vol.read(e)
            self.assertEqual(len(data), x["size"], e.name)
            self.assertEqual(sha256(data), x["sha256"], e.name)

    def test_wrong_key_prefix_is_detected(self):
        with self.assertRaises(pkn.PknError):
            pkn.Volume(FIXTURE_VOL, key_path="Pack_publish/Effects0011.pkn")


# ---------------------------------------------------------------------------
#  2. 写入器 ⇄ 读取器
# ---------------------------------------------------------------------------

def make_tree(root, files):
    """files: {"Group/rel/name": bytes}"""
    for rel, data in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)


def sample_tree():
    rnd = bytes((i * 7 + 3) & 0xFF for i in range(5000))
    return {
        "Data/Chinese.ini": "﻿[Chinese]\r\n키=值\r\n".encode("utf-16-le"),
        "Data/empty.txt": b"",
        "Data/one.txt": b"x",
        "Data/three.csv": b"abc",
        "Data/Ui/Tip.ui": rnd[:1023],
        "Data/Ui/Tip2.ui": rnd[:1024],
        "Data/Ui/Tip3.ui": rnd[:1025],
        "Images/Shop/댄싱크라운 머리.png": rnd,          # 韩文名 + flag 4（只加密前 1KB）
        "Images/Shop/odd.png": rnd[:301],              # 奇数长内容，stored 要取整
        "Images/a/b/c/deep.dds": rnd[:2048],
        "Maps/m1/Terrain/t.png": rnd[:4000],
        "Maps/m1/m1.map": rnd[:777],
        "Maps/loose.map": rnd[:99],
        "Sounds/x.ogg": rnd,
        "Sounds/y.wav": rnd[:50],
        "Models/Characters/ch00/ch0000000.msh": rnd[:17422],   # flag 2，size 不是 4 的倍数
    }


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pkn-")
        self.src = os.path.join(self.tmp, "Pack_develop")
        self.out = os.path.join(self.tmp, "Pack_publish")
        self.files = sample_tree()
        make_tree(self.src, self.files)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pack_then_read_back_every_file(self):
        res = pkn.pack(self.src, self.out, log=lambda *a: None)
        self.assertTrue(res.changed)
        ok, problems = pkn.verify(self.src, self.out, log=lambda *a: None)
        self.assertEqual(problems, [])
        self.assertEqual(ok, len(self.files))
        # 每个条目单独核：flags 对、stored 规则对、能解回原文
        seen = 0
        for vp in pkn.iter_volume_files(self.out):
            v = pkn.Volume(vp)
            for e in v.entries:
                full = v.full_name(e)
                want = self.files[full]
                self.assertEqual(v.read(e), want, full)
                self.assertEqual(e.flags, pkn.flags_for(e.name))
                if e.flags & 6:
                    self.assertEqual(e.stored % 4, 0, full)
                if e.flags == 0:
                    self.assertEqual(e.stored, e.size, full)
                seen += 1
        self.assertEqual(seen, len(self.files))

    def test_volume_layout_looks_like_the_original(self):
        pkn.pack(self.src, self.out, log=lambda *a: None)
        for vp in pkn.iter_volume_files(self.out):
            v = pkn.Volume(vp)
            size = os.path.getsize(vp)
            self.assertEqual(size % pkn.BLOCK, 0, v.filename)          # 文件长补到 1KB
            self.assertEqual(v.data_offset % pkn.BLOCK, 0)
            self.assertEqual(v.stamp & 0xF, 0)
            self.assertTrue(0x30 <= v.gap)
            names = [e.name for e in v.entries]
            self.assertEqual(names, sorted(names))                       # 条目按名排
            blk = 0
            for e in v.entries:
                self.assertEqual(e.blk, blk, e.name)                     # 按表序连续
                blk += (e.stored + pkn.BLOCK - 1) // pkn.BLOCK
            self.assertEqual(size, v.data_offset + blk * pkn.BLOCK)

    def test_packing_twice_is_byte_identical(self):
        pkn.pack(self.src, self.out, log=lambda *a: None)
        first = {n: sha256(open(os.path.join(self.out, n), "rb").read())
                 for n in os.listdir(self.out)}
        pkn.pack(self.src, self.out, force=True, log=lambda *a: None)
        second = {n: sha256(open(os.path.join(self.out, n), "rb").read())
                  for n in os.listdir(self.out)}
        self.assertEqual(first, second)

    def test_second_pack_rewrites_nothing(self):
        pkn.pack(self.src, self.out, log=lambda *a: None)
        res = pkn.pack(self.src, self.out, log=lambda *a: None)
        self.assertFalse(res.changed)
        self.assertEqual(res.written, [])
        self.assertEqual(res.deleted, [])

    def test_changing_one_file_rewrites_only_its_volume(self):
        pkn.pack(self.src, self.out, log=lambda *a: None)
        before = {n: os.path.getmtime(os.path.join(self.out, n)) for n in os.listdir(self.out)}
        with open(os.path.join(self.src, "Data", "Ui", "Tip.ui"), "ab") as f:
            f.write(b"!")
        res = pkn.pack(self.src, self.out, log=lambda *a: None)
        self.assertEqual(res.written, ["Data~Ui.pkn"])
        self.assertEqual(res.deleted, [])
        v = pkn.Volume(os.path.join(self.out, "Data~Ui.pkn"))
        tip = [e for e in v.entries if e.name == "Ui/Tip.ui"][0]
        self.assertEqual(v.read(tip), self.files["Data/Ui/Tip.ui"] + b"!")
        # 没被点名的卷一个字节都没动（mtime 是「有没有重写」这个事实的记录，不是判据）
        for n, t in before.items():
            if n not in ("Data~Ui.pkn", pkn.INDEX_NAME):
                self.assertEqual(os.path.getmtime(os.path.join(self.out, n)), t, n)

    def test_deleting_a_directory_deletes_its_volume_and_stray_files_are_swept(self):
        pkn.pack(self.src, self.out, log=lambda *a: None)
        shutil.rmtree(os.path.join(self.src, "Maps", "m1"))
        with open(os.path.join(self.out, "Old0000.pkn"), "wb") as f:      # 别人留下的野卷
            f.write(b"junk")
        with open(os.path.join(self.out, "Sounds~.pkn.tmp"), "wb") as f:  # 上次被打断的残留
            f.write(b"junk")
        res = pkn.pack(self.src, self.out, log=lambda *a: None)
        self.assertEqual(sorted(res.deleted), ["Maps~m1.pkn", "Old0000.pkn", "Sounds~.pkn.tmp"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "Maps~m1.pkn")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "Old0000.pkn")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "Sounds~.pkn.tmp")))

    def test_check_mode_reports_staleness_without_writing(self):
        res = pkn.pack(self.src, self.out, check=True, log=lambda *a: None)
        self.assertTrue(res.changed)                       # 还没打过
        self.assertFalse(os.path.exists(self.out))
        pkn.pack(self.src, self.out, log=lambda *a: None)
        res = pkn.pack(self.src, self.out, check=True, log=lambda *a: None)
        self.assertFalse(res.changed)
        with open(os.path.join(self.src, "Sounds", "y.wav"), "wb") as f:
            f.write(b"new")
        snapshot = {n: sha256(open(os.path.join(self.out, n), "rb").read()) for n in os.listdir(self.out)}
        res = pkn.pack(self.src, self.out, check=True, log=lambda *a: None)
        self.assertEqual(res.written, ["Sounds~.pkn"])
        after = {n: sha256(open(os.path.join(self.out, n), "rb").read()) for n in os.listdir(self.out)}
        self.assertEqual(snapshot, after)                  # check 一个字节都没写

    def test_policy_change_rewrites_everything(self):
        pkn.pack(self.src, self.out, log=lambda *a: None)
        idx = pkn.load_index(self.out)
        idx["packer"]["zlib_level"] = 1                    # 假装策略变了
        pkn.dump_index(self.out, idx)
        res = pkn.pack(self.src, self.out, check=True, log=lambda *a: None)
        self.assertEqual(len(res.written), res.volumes)

    def test_junk_files_are_skipped_with_a_warning(self):
        make_tree(self.src, {"Data/Thumbs.db": b"x", "Data/.hidden": b"y", "Data/foo.tmp": b"z",
                             "loose_at_top.txt": b"w"})
        files, warnings = pkn.scan_tree(self.src)
        self.assertNotIn("Thumbs.db", files["Data"])
        self.assertNotIn(".hidden", files["Data"])
        self.assertNotIn("foo.tmp", files["Data"])
        self.assertEqual(len(warnings), 4)

    def test_case_insensitive_duplicate_is_rejected(self):
        """NTFS 上两种大小写根本放不进同一个目录，所以直接喂写入器（Linux 上打包就会碰到）。"""
        with self.assertRaises(pkn.PknError):
            pkn.build_volume("Data~.pkn", "Data", [("Chinese.ini", b"a"), ("chinese.INI", b"b")])

    def test_odd_length_name_makes_table_length_2_mod_4(self):
        """表长 mod 4 == 2 的情形（原版 47 卷都是），密文流要连续解才对。"""
        tree = {"Data/abc.ini": b"hello", "Data/de.ini": b"world"}   # 名长 7 / 6 字符
        src = os.path.join(self.tmp, "t2")
        out = os.path.join(self.tmp, "o2")
        make_tree(src, tree)
        pkn.pack(src, out, log=lambda *a: None)
        v = pkn.Volume(os.path.join(out, "Data~.pkn"))
        self.assertEqual(v._table_len % 4, 2)
        self.assertEqual({v.full_name(e): v.read(e) for e in v.entries}, tree)


# ---------------------------------------------------------------------------
#  3. 规划
# ---------------------------------------------------------------------------

class VolumePlanTests(unittest.TestCase):
    def _files(self, spec):
        """spec: {组: {相对名: 大小}} -> scan_tree 的返回形状（路径随便给）"""
        return {g: {r: ("/nowhere/" + r, s) for r, s in t.items()} for g, t in spec.items()}

    def test_one_directory_one_volume(self):
        files = self._files({"Maps": {"a.map": 1, "swamp/t.png": 5, "swamp/x/y.png": 5, "sea/s.map": 3}})
        plans = pkn.assign_volumes(files, cap=100)
        self.assertEqual([(p[0], p[3]) for p in plans], [
            ("Maps~.pkn", ["a.map"]),
            ("Maps~sea.pkn", ["sea/s.map"]),
            ("Maps~swamp.pkn", ["swamp/t.png", "swamp/x/y.png"]),
        ])

    def test_oversized_directory_with_children_is_split_one_level(self):
        files = self._files({"Models": {"Characters/own.txt": 1,
                                        "Characters/ch00/a.msh": 60, "Characters/ch01/b.msh": 60,
                                        "Monsters/m.msh": 10}})
        plans = pkn.assign_volumes(files, cap=100)
        self.assertEqual([p[0] for p in plans],
                         ["Models~Characters~.pkn", "Models~Characters~ch00.pkn",
                          "Models~Characters~ch01.pkn", "Models~Monsters.pkn"])

    def test_oversized_leaf_directory_is_not_split(self):
        files = self._files({"Sounds": {"FX/%d.ogg" % i: 50 for i in range(10)}})
        plans = pkn.assign_volumes(files, cap=100)
        self.assertEqual([p[0] for p in plans], ["Sounds~FX.pkn"])

    def test_bad_directory_name_is_rejected(self):
        with self.assertRaises(pkn.PknError):
            pkn.assign_volumes(self._files({"Maps": {"a~b/x.map": 1}}))
        with self.assertRaises(pkn.PknError):
            pkn.assign_volumes(self._files({"Maps": {"韩/x.map": 1}}))

    def test_volume_names_are_unique_ignoring_case(self):
        files = self._files({"Maps": {"Sea/a.map": 1, "sea/b.map": 1}})
        # 同一目录两种大小写在 NTFS 上本来就不可能并存；这里只保证规划器不会静默合并
        with self.assertRaises(pkn.PknError):
            pkn.assign_volumes(files)


class FullVerifyTests(unittest.TestCase):
    """全量回读仓库里的 Pack_publish（1~2 分钟），只在显式要求时跑。"""

    def test_publish_matches_develop(self):
        if os.environ.get("POPSHOT_PKN_FULL_VERIFY") != "1":
            raise unittest.SkipTest("设 POPSHOT_PKN_FULL_VERIFY=1 才跑全量回读")
        if not os.path.isdir(pkn.DEFAULT_SRC) or not os.path.isdir(pkn.DEFAULT_OUT):
            raise unittest.SkipTest("没有 Pack_develop / Pack_publish")
        ok, problems = pkn.verify(pkn.DEFAULT_SRC, pkn.DEFAULT_OUT, log=lambda *a: None)
        self.assertEqual(problems, [])
        self.assertGreater(ok, 0)


if __name__ == "__main__":
    unittest.main()
