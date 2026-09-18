#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`Data/weapon.ini` 的守卫（X_Mod · X3）：**原版部分一个字节不许变**，自定义小节要齐、资源要在。

用户 2026-09-19 拍板：原版武器的属性全部恢复原样、以后也不再改 —— 前人 PR 改过泰尔的
四个左轮小节，这一条就是防止再有人「顺手」改一下。判据是最硬的那种：文件前 221519 字节
的 sha256 必须等于原版（`2245b8c7^` 那份 == `main/Pack_decrypt/Data/weapon.ini`）。

自定义武器那 11 个小节由 `tools/goldwp/build.py` 生成，追加在原版之后；这里核它们的
Id / 资源引用是不是都对得上磁盘上的文件（缺一个文件客户端不崩但会静默缺东西）。

⚠ 依赖明文资源树 `game_patched/Pack_develop`，发布包里没有，自动跳过。
"""
import hashlib
import importlib.util
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GOLDWP = os.path.join(ROOT, "tools", "goldwp")


def _load(name):
    spec = importlib.util.spec_from_file_location("test_" + name, os.path.join(GOLDWP, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WeaponIniTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.spec = _load("spec")
        cls.build = cls.spec          # 哈希 / 长度 / 标记都在 spec 里（build.py 要 numpy，便携运行时没有）
        cls.root = cls.spec.develop_root()
        cls.path = os.path.join(cls.root, "Data", "weapon.ini")
        if not os.path.isfile(cls.path):
            raise unittest.SkipTest("没有明文资源树，跳过")
        with open(cls.path, "rb") as fp:
            cls.raw = fp.read()
        cls.sections = cls.spec.read_weapon_ini(cls.path)

    def test_the_original_part_is_byte_for_byte_the_original(self):
        """★★ 前 221519 字节 == 原版。改了任何一个原版数值这里就红。"""
        head = self.raw[:self.build.ORIGINAL_SIZE]
        self.assertEqual(self.build.ORIGINAL_SIZE, len(head))
        self.assertEqual(self.build.ORIGINAL_SHA256, hashlib.sha256(head).hexdigest(),
                         "weapon.ini 的原版部分被改过 —— 原版武器的属性不许动（用户 2026-09-19）")

    def test_the_tail_is_only_our_generated_block(self):
        tail = self.raw[self.build.ORIGINAL_SIZE:]
        self.assertTrue(tail.startswith(b"\r\n" + self.build.INI_MARKER.encode("ascii")),
                        "原版之后只许接 build.py 生成的那一块")
        # 追加块是 CP949 / ASCII，不许有中文（铁律 3：客户端按 CP949 读）。
        tail.decode("cp949")

    def test_no_desc_keys_anywhere(self):
        """说明文改由管理页配置（`weaponcfg`），ini 里不许再有 `Desc=`。"""
        for name, fields in self.sections.items():
            self.assertNotIn("Desc", fields, name)

    def test_every_custom_section_exists_with_its_ids(self):
        for w in self.spec.WEAPONS:
            self.assertIn(w.custom_section, self.sections)
            self.assertEqual(str(w.ammo_id), self.sections[w.custom_section]["Id"])
            self.assertEqual(str(w.mesh_idx), self.sections[w.custom_section]["WMeshIdx"])
            self.assertEqual("%s%s,%d" % (w.icon_set, self.spec.SERIES, w.character),
                             self.sections[w.custom_section]["Icon"])
            if w.piece_section:
                self.assertIn(w.custom_piece_section, self.sections)
                self.assertEqual(str(w.piece_id), self.sections[w.custom_piece_section]["Id"])
                self.assertEqual(str(w.piece_id), self.sections[w.custom_section]["SliceId"])

    def test_custom_ids_do_not_collide_with_the_original_table(self):
        ids = {}
        for name, fields in self.sections.items():
            if "Id" in fields:
                self.assertNotIn(fields["Id"], ids, "%s 和 %s 撞 Id" % (name, ids.get(fields["Id"])))
                ids[fields["Id"]] = name

    def _exists(self, rel):
        return os.path.isfile(os.path.join(self.root, rel.replace("\\", "/")))

    def test_every_resource_the_custom_sections_reference_exists(self):
        """`Eff*` / `LineTexture` / `Image` / 手持网格 / 图标 / 音效，一个都不许缺
        （缺了客户端不崩，只是那一样静默没有）。"""
        spec = self.spec
        missing = []
        amf = _load_amf(self.root)
        for name in [w.custom_section for w in spec.WEAPONS] + \
                    [w.custom_piece_section for w in spec.WEAPONS if w.piece_section]:
            fields = self.sections[name]
            for key, value in fields.items():
                kk = key.lstrip("_")
                if kk.startswith("Eff") or (kk == "Image" and value.startswith("Effect,")):
                    for part in value.split(","):
                        part = part.strip()
                        if part.lower().endswith(".efx"):
                            rel = part if part.lower().startswith("effects/") else "Effects/" + part
                            if not self._exists(rel):
                                missing.append((name, key, part))
                elif kk == "LineTexture" and value:
                    if not self._exists(value):
                        missing.append((name, key, value))
                elif kk == "Image" and value.startswith("Anim,"):
                    entry = amf.get(value.split(",", 1)[1].strip().lower())
                    if entry is None:
                        missing.append((name, key, value))
                    elif not self._exists(entry.lstrip("/")):
                        missing.append((name, key, entry))
                elif kk == "Image" and value.startswith("Model,"):
                    stem = value.split(",", 1)[1].strip()
                    if not self._exists("Models/" + stem + ".msh") or not self._exists("Models/" + stem + ".dds"):
                        missing.append((name, key, value))
                elif kk == "Icon" and value:
                    icon = value.split(",")[0]
                    for ext in (".png", ".smf"):
                        if not self._exists("Images/Game/" + icon + ext):
                            missing.append((name, key, icon + ext))
                elif kk == "WMeshIdx":
                    m = re.match(r"ch(\d\d)-", name)
                    for ext in (".msh", ".dds"):
                        rel = "Models/Characters/ch%s/ch%sW%04d%s" % (m.group(1), m.group(1), int(value), ext)
                        if not self._exists(rel):
                            missing.append((name, key, rel))
                elif kk.startswith("Sound") and value:
                    rel = "Sounds/" + value
                    if not self._exists(rel):
                        missing.append((name, key, value))
        self.assertEqual([], missing)

    def test_shop_ini_has_the_nine_items(self):
        path = os.path.join(self.root, "Data", "ShopItem-Chn.ini")
        with open(path, "rb") as fp:
            raw = fp.read()
        self.assertEqual(b"\xff\xfe", raw[:2])
        text = raw[2:].decode("utf-16le")
        for w in self.spec.WEAPONS:
            self.assertEqual(1, text.count("[Item-%d]" % w.item_id), w.item_id)
            self.assertEqual(1, text.count("[Stock-%d]" % w.item_id), w.item_id)
            self.assertIn("Tag=%d.0" % w.ammo_id, text)
            icon = "Images/Shop/무기_%s %s.png" % (w.shop_icon_kr, self.spec.SERIES)
            self.assertIn("Image=" + icon, text)
            self.assertTrue(self._exists(icon), icon)


def _load_amf(root):
    """`{条目名(小写): 路径}`，只看 Game/Bullet 组。"""
    spec = importlib.util.spec_from_file_location("test_amftool_mod", os.path.join(ROOT, "tools", "amftool.py"))
    amftool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(amftool)
    _version, groups = amftool.load(os.path.join(root, "Data", "default.amf"))
    out = {}
    for group in groups:
        if group.name.lower() == "game/bullet":
            for entry in group.entries:
                out[entry.name.lower()] = entry.path
    return out


if __name__ == "__main__":
    unittest.main()
