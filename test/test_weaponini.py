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

    def _installed(self):
        """已经实装的那些批次的武器。

        ★ 判据是**状态翻转**：`BATCHES[x].variant` 一填上（= 用户选定了配色、该批该生成了），
          下面几条「资源必须在盘上」的守卫立刻开始管它。不是「跳过第 N 批」那种阈值。
        """
        return [w for w in self.spec.WEAPONS
                if self.spec.BATCHES[w.batch].variant is not None]

    def test_every_custom_section_exists_with_its_ids(self):
        for w in self._installed():
            self.assertIn(w.custom_section, self.sections)
            self.assertEqual(str(w.ammo_id), self.sections[w.custom_section]["Id"])
            self.assertEqual(str(w.mesh_idx), self.sections[w.custom_section]["WMeshIdx"])
            self.assertEqual("%s%s,%d" % (w.icon_set, w.batch, w.character),
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

    def test_every_custom_id_decodes_to_its_slot(self):
        """★★★ 武器 Id **不能随便编**（§41 / D31）。

        客户端 `0x40a138` 把 Id 拆数位算出一个索引，直接拿去下标**只有 4 格**的
        弹药数组，而开火 / 取弹药 / 换弹三个调用点**都只查 `>= 0`、不查上界**。
        索引必须正好等于槽位，否则两件事同时发生：
          ① 换弹是 `ammo[i] = maxAmmo[i]`，越界读到的是堆里的指针 → 永远减不到 0
             ⇒ 无限开火、准星外圈那圈弹格也不画；
          ② 每开一枪往越界处写一个 dword ⇒ 退出地图销毁 Surface 时撞上被踩坏的
             哈希节点，主线程死循环（结算界面「未响应」）。
        初版编号 `1 0 0C 9 S 0`（档 9、系列 0）正好两条都踩。
        """
        for w in self.spec.WEAPONS:
            series, index = self.spec.decode_weapon_id(w.ammo_id)
            self.assertTrue(3 <= series <= 5,
                            "%s 的 Id=%d 系列位是 %d，不在 [3,5]，索引不会被归一化成槽位"
                            % (w.custom_section, w.ammo_id, series))
            self.assertEqual(w.slot, index,
                             "%s 的 Id=%d 解出来的索引是 %d，不是槽位 %d —— 弹药数组会越界"
                             % (w.custom_section, w.ammo_id, index, w.slot))

    def test_the_decoder_model_matches_every_original_weapon(self):
        """上面那条守卫只在「解码器抄对了」的前提下才有意义 —— 拿 ini 里全部原版
        `chNN-0S…` 小节回归：Id 解出来的索引必须等于小节名里的槽位 S。
        子弹药（`…a` 结尾）不算：原版它们本来就落在 50+，不走玩家开火那条路。
        """
        checked = 0
        for name, fields in self.sections.items():
            m = re.match(r"^ch\d\d-0(\d)(.*)$", name, re.IGNORECASE)
            if not m or m.group(2).endswith("a") or "Id" not in fields:
                continue
            slot = int(m.group(1))
            index = self.spec.weapon_slot_index(int(fields["Id"]))
            self.assertEqual(slot, index,
                             "%s Id=%s 解出索引 %d，和小节名里的槽位 %d 对不上 —— "
                             "要么解码器抄错了，要么这条 Id 本来就特殊"
                             % (name, fields["Id"], index, slot))
            checked += 1
        self.assertGreater(checked, 100, "只核到 %d 个小节，正则八成没匹配上" % checked)

    def _exists(self, rel):
        return os.path.isfile(os.path.join(self.root, rel.replace("\\", "/")))

    def test_every_resource_the_custom_sections_reference_exists(self):
        """`Eff*` / `LineTexture` / `Image` / 手持网格 / 图标 / 音效，一个都不许缺
        （缺了客户端不崩，只是那一样静默没有）。"""
        spec = self.spec
        missing = []
        amf = _load_amf(self.root)
        installed = self._installed()
        for name in [w.custom_section for w in installed] + \
                    [w.custom_piece_section for w in installed if w.piece_section]:
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

    def test_shop_ini_has_every_custom_item(self):
        path = os.path.join(self.root, "Data", "ShopItem-Chn.ini")
        with open(path, "rb") as fp:
            raw = fp.read()
        self.assertEqual(b"\xff\xfe", raw[:2])
        text = raw[2:].decode("utf-16le")
        for w in self._installed():
            self.assertEqual(1, text.count("[Item-%d]" % w.item_id), w.item_id)
            self.assertEqual(1, text.count("[Stock-%d]" % w.item_id), w.item_id)
            self.assertIn("Tag=%d.0" % w.ammo_id, text)
            icon = "Images/Shop/무기_%s %s.png" % (w.shop_icon_kr, w.batch)
            self.assertIn("Image=" + icon, text)
            self.assertTrue(self._exists(icon), icon)

    def test_the_batches_do_not_collide_with_each_other(self):
        """两批（以及将来更多批）之间：武器 Id / 子弹药 Id / 物品 id / 网格号 / 小节名
        / 商店图标名一个都不许撞。★ 这条不看磁盘，**没实装的批次也管**。"""
        spec = self.spec
        self.assertEqual(1920001, spec.WEAPONS[0].item_id,
                         "openweapons.GOLDWP_ANCHOR 钉着 [Item-1920001]，第一条不能换")
        for field, label in (("ammo_id", "武器 Id"), ("item_id", "物品 id"),
                             ("custom_section", "小节名")):
            values = [getattr(w, field) for w in spec.WEAPONS]
            self.assertEqual(len(values), len(set(values)), "%s 有重复：%s" % (label, values))
        pieces = [w.piece_id for w in spec.WEAPONS if w.piece_id]
        self.assertEqual(len(pieces), len(set(pieces)), "子弹药 Id 有重复：%s" % pieces)
        meshes = [(w.character, w.mesh_idx) for w in spec.WEAPONS]
        self.assertEqual(len(meshes), len(set(meshes)), "手持网格号有重复：%s" % meshes)
        icons = [(w.icon_set, w.batch) for w in spec.WEAPONS]
        self.assertEqual(len(set(icons)), len(spec.BATCHES) * 3, "HUD 图集名有重复")
        for letter, batch in spec.BATCHES.items():
            self.assertEqual(9, len(spec.weapons_of(letter)), "批 %s 不是 9 把" % letter)
            self.assertIn(batch.scheme, ("gold", "pink"), batch.scheme)


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
