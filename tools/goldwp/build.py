#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py —— 把 9 把「自定义」黄金武器的全部资源**从爆裂 3 生成出来**（X_Mod · X3，阶段 2）。

    C:\\Python314\\python.exe tools/goldwp/build.py --variant A          # 全部生成（幂等，母本永远是爆裂 3）
    C:\\Python314\\python.exe tools/goldwp/build.py --variant A --dry-run
    C:\\Python314\\python.exe tools/goldwp/build.py --variant orig       # 只搬运不改色（占位版，先把链路跑通）

产出（全部落在 `game_patched/Pack_develop/`，之后跑 `tools\\build-pack.bat`）：

    Models/Characters/chNN/chNNW014S.msh + .dds      手持件（等长改名 013S→014S，贴图改色）
    Models/Characters/chNN/chNND0142.msh + .dds      泰尔 / 卡希尔 2 号的 3D 弹体
    Images/Game/CHnn_Wp0kC_*.png + .smf              2D 弹体 / 碎片精灵（改色）
    Images/Game/Wp0kC.png + .smf                     HUD 武器图标（3 帧 = 3 角色，改色）
    Images/Shop/무기_<韩文名> C.png                    商店图标（改色）
    Effects/CHnn/WP0k/Efx/*C*.efx                    特效副本：贴图指向改色副本，<ColorValue> 按同一套规则换色
    Effects/**/Texture/*_C.dds                       特效贴图的改色副本（含红 / 橙的才做，其余共用原图）
    Data/default.amf                                 追加 CHnn_WPk_C 条目
    Data/weapon.ini                                  末尾追加 11 个小节（9 主 + 2 子弹药），**原版部分一个字节不动**
    Data/ShopItem-Chn.ini                            末尾追加 9 组 [Item-] + [Stock-]

## 铁律

* 母本只读：只读爆裂 3 的文件，一个字节不写回去；重跑就是重做一遍。
* `weapon.ini` 前 221519 字节必须等于原版（sha256 见 `ORIGINAL_SHA256`），追加块以标记行开头，
  重跑先把旧块砍掉再追加 —— `test/test_weaponini.py` 盯着这一条。
* `_` 前缀（全年龄版）的资源键**全部指向和普通键同一批黄金资源**（`_Sound-*` 除外），
  不另做气泡版：不管客户端在哪种模式下都看到黄金。
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import os
import re
import shutil
import struct
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOLS)
for p in (TOOLS, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
import amftool  # noqa: E402
import dds_edit  # noqa: E402
import palette  # noqa: E402
import spec  # noqa: E402

#: 原版 `weapon.ini` 的哈希 / 长度 / 追加块标记 —— 住在 `spec.py`（测试也要用，那边不依赖 numpy）。
ORIGINAL_SHA256 = spec.ORIGINAL_SHA256
ORIGINAL_SIZE = spec.ORIGINAL_SIZE
INI_MARKER = spec.INI_MARKER

#: 自定义武器的说明（写进 ini 注释，纯 ASCII，铁律 3）。
INI_BANNER = (
    "; 9 custom weapons + 2 sub-ammo sections. Numbers are copied from the D3 reference sections;",
    "; the live values come from the server (admin page -> 0x0F01 -> bshook), not from this file.",
)


class Plan(object):
    """所有要写的文件先攒在这里，最后统一落盘（`--dry-run` 只打印）。"""

    def __init__(self, variant, dry_run):
        self.variant = variant
        self.dry_run = dry_run
        self.files = collections.OrderedDict()   # 目标路径 -> bytes
        self.notes = []
        self.tex_cache = {}                      # 源贴图路径 -> (需要副本?, 副本相对路径)

    def put(self, path, data, note=None):
        self.files[path] = data
        if note:
            self.notes.append(note)

    def flush(self):
        wrote = same = 0
        for path, data in self.files.items():
            if os.path.isfile(path) and open(path, "rb").read() == data:
                same += 1
                continue
            if not self.dry_run:
                d = os.path.dirname(path)
                if d and not os.path.isdir(d):
                    os.makedirs(d)
                with open(path, "wb") as fp:
                    fp.write(data)
            wrote += 1
        return wrote, same


# ---------------------------------------------------------------------------
# 贴图 / 图片
# ---------------------------------------------------------------------------

def recolor_dds(src, dst, variant, plan):
    _hd, rgba = dds_edit.load_rgba(src)
    out = palette.recolor_rgba(rgba, variant)
    template = open(src, "rb").read()
    plan.put(dst, dds_edit.encode(template, out))


def recolor_png(src, dst, variant, plan):
    img = Image.open(src).convert("RGBA")
    out = palette.recolor_rgba(np.asarray(img), variant)
    import io
    buf = io.BytesIO()
    Image.fromarray(out, "RGBA").save(buf, format="PNG")
    plan.put(dst, buf.getvalue())


def copy_file(src, dst, plan):
    plan.put(dst, open(src, "rb").read())


# ---------------------------------------------------------------------------
# 网格
# ---------------------------------------------------------------------------

def rename_texture_in_msh(blob, old, new):
    """`mkchar.patch_msh` 的手法：只替换「u32 长度前缀 + 恰好等于旧贴图名」的串，等长。"""
    assert len(old) == len(new), (old, new)
    needle = struct.pack("<I", len(old)) + old.encode("utf-16le")
    repl = struct.pack("<I", len(new)) + new.encode("utf-16le")
    if needle not in blob:
        raise RuntimeError("网格里找不到贴图名 %s" % old)
    return blob.replace(needle, repl)


def build_meshes(plan):
    for w in spec.WEAPONS:
        d = spec.chars_dir(w.character)
        pairs = [("ch%02dW%04d" % (w.character, w.mesh_idx - 10), "ch%02dW%04d" % (w.character, w.mesh_idx))]
        if w.slot == 2 and w.character in (0, 1):
            pairs.append(("ch%02dD0132" % w.character, "ch%02dD0142" % w.character))
        for old, new in pairs:
            blob = open(os.path.join(d, old + ".msh"), "rb").read()
            plan.put(os.path.join(d, new + ".msh"), rename_texture_in_msh(blob, old + ".dds", new + ".dds"))
            recolor_dds(os.path.join(d, old + ".dds"), os.path.join(d, new + ".dds"), plan.variant, plan)


# ---------------------------------------------------------------------------
# 特效
# ---------------------------------------------------------------------------

_TEX_RE = re.compile(rb"<TextureFileName>([^<]+)</TextureFileName>")
_COLOR_RE = re.compile(rb"<ColorValue>(-?\d+)</ColorValue>")


def _efx_rel(value):
    """ini 里的特效路径 → 相对明文树的路径（有的写 `Effects/…`，有的没有前缀）。"""
    value = value.strip().replace("\\", "/")
    if not value.lower().startswith("effects/"):
        value = "Effects/" + value
    return value


def _real_case(root, rel):
    """`Effects/Ch01/…` 这种大小写不一致的路径按磁盘上的真实名字修正（打包器按真实目录名装卷）。"""
    parts = rel.replace("\\", "/").split("/")
    cur = root
    fixed = []
    for part in parts:
        if not os.path.isdir(cur):
            fixed.append(part)
            cur = os.path.join(cur, part)
            continue
        match = None
        for name in os.listdir(cur):
            if name.lower() == part.lower():
                match = name
                break
        fixed.append(match or part)
        cur = os.path.join(cur, match or part)
    return "/".join(fixed)


def texture_copy(plan, tex_rel):
    """特效贴图 `CH00\\WP01\\Texture\\X.dds` → 需要的话生成改色副本，返回 efx 里该写的相对名。"""
    root = spec.develop_root()
    rel = _real_case(os.path.join(root, "Effects"), tex_rel.replace("\\", "/"))
    src = os.path.join(root, "Effects", rel)
    if src in plan.tex_cache:
        return plan.tex_cache[src]
    if not os.path.isfile(src):
        plan.notes.append("⚠ 特效贴图不存在，原样引用：%s" % tex_rel)
        plan.tex_cache[src] = tex_rel
        return tex_rel
    _hd, rgba = dds_edit.load_rgba(src)
    if plan.variant == "orig" or not palette.has_hue(rgba):
        plan.tex_cache[src] = tex_rel          # 没有红 / 橙：共用原图
        return tex_rel
    stem, ext = os.path.splitext(os.path.basename(rel))
    new_rel = os.path.join(os.path.dirname(rel), spec.rename(stem) + ext).replace("/", "\\")
    dst = os.path.join(root, "Effects", new_rel.replace("\\", "/"))
    recolor_dds(src, dst, plan.variant, plan)
    plan.tex_cache[src] = new_rel
    return new_rel


def efx_copy(plan, efx_value):
    """一个特效文件 → 生成副本，返回 ini 里该写的路径（保持原来有没有 `Effects/` 前缀的写法）。"""
    root = spec.develop_root()
    rel = _real_case(root, _efx_rel(efx_value))
    src = os.path.join(root, rel)
    if not os.path.isfile(src):
        plan.notes.append("⚠ 特效文件不存在，原样引用：%s" % efx_value)
        return efx_value.strip()
    blob = open(src, "rb").read()

    def tex_sub(m):
        return b"<TextureFileName>" + texture_copy(plan, m.group(1).decode("cp949", "replace")).encode("cp949") + b"</TextureFileName>"

    def color_sub(m):
        return b"<ColorValue>" + str(palette.recolor_argb(int(m.group(1)), plan.variant)).encode("ascii") + b"</ColorValue>"

    out = _TEX_RE.sub(tex_sub, blob)
    out = _COLOR_RE.sub(color_sub, out)
    stem, ext = os.path.splitext(os.path.basename(rel))
    new_rel = os.path.join(os.path.dirname(rel), spec.rename(stem) + ext).replace("\\", "/")
    plan.put(os.path.join(root, new_rel), out)
    return new_rel


def line_texture_copy(plan, value):
    """`LineTexture=Effects/…/X.dds` → 改色副本的路径。"""
    root = spec.develop_root()
    rel = _real_case(root, value.strip().replace("\\", "/"))
    src = os.path.join(root, rel)
    if not os.path.isfile(src):
        plan.notes.append("⚠ 拖尾贴图不存在，原样引用：%s" % value)
        return value.strip()
    stem, ext = os.path.splitext(os.path.basename(rel))
    new_rel = os.path.join(os.path.dirname(rel), spec.rename(stem) + ext).replace("\\", "/")
    recolor_dds(src, os.path.join(root, new_rel), plan.variant, plan)
    return new_rel


# ---------------------------------------------------------------------------
# 精灵 / 图标 / amf
# ---------------------------------------------------------------------------

def anim_copy(plan, groups, entry_name):
    """`Image=Anim,X` → 生成 `Anim,<X 改名>`：amf 条目 + 精灵 PNG（改色）+ .smf（原样）。返回新条目名。"""
    root = spec.develop_root()
    entry = amftool.find(groups, "Game/Bullet", entry_name)
    if entry is None:
        plan.notes.append("⚠ amf 里没有条目 %s，原样引用" % entry_name)
        return entry_name, groups
    new_name = spec.rename(entry_name)
    src_png = os.path.join(root, entry.path.lstrip("/").replace("\\", "/"))
    stem, ext = os.path.splitext(os.path.basename(src_png))
    new_base = spec.rename(stem)
    dst_png = os.path.join(os.path.dirname(src_png), new_base + ext)
    if os.path.isfile(src_png):
        recolor_png(src_png, dst_png, plan.variant, plan)
        smf = os.path.splitext(src_png)[0] + ".smf"
        if os.path.isfile(smf):
            copy_file(smf, os.path.join(os.path.dirname(src_png), new_base + ".smf"), plan)
    else:
        plan.notes.append("⚠ 精灵不存在：%s" % src_png)
    new_path = os.path.dirname(entry.path).replace("\\", "/") + "/" + new_base + ext
    groups, _added = amftool.upsert(groups, "Game/Bullet",
                                    amftool.Entry(new_name, new_path, entry.a, entry.b, list(entry.frames)))
    return new_name, groups


def resolve_sound(plan, w, value):
    """音效路径照抄；原版有一处死引用（`[ch01-02D3a] Sound-Fire=Weapon-Hit-Bottle_D3.ogg`
    少写了 `FX/wp/ch01/` 前缀，`Sounds/` 根下没有那个文件）—— 能在本角色的 `FX/wp/chNN/`
    下找到同名文件就补上前缀，让自定义那把真的有声。找不到的原样带走。"""
    root = spec.develop_root()
    if os.path.isfile(os.path.join(root, "Sounds", value.replace("\\", "/"))):
        return value
    candidate = "FX/wp/ch%02d/%s" % (w.character, os.path.basename(value.replace("\\", "/")))
    if os.path.isfile(os.path.join(root, "Sounds", candidate)):
        plan.notes.append("音效死引用已修正：%s -> %s" % (value, candidate))
        return candidate
    plan.notes.append("⚠ 音效不存在，原样引用：%s" % value)
    return value


def build_icons(plan):
    root = spec.develop_root()
    game = os.path.join(root, "Images", "Game")
    shop = os.path.join(root, "Images", "Shop")
    for icon_set in sorted(set(w.icon_set for w in spec.WEAPONS)):
        recolor_png(os.path.join(game, icon_set + "D3.png"), os.path.join(game, icon_set + spec.SERIES + ".png"),
                    plan.variant, plan)
        copy_file(os.path.join(game, icon_set + "D3.smf"), os.path.join(game, icon_set + spec.SERIES + ".smf"), plan)
    for w in spec.WEAPONS:
        recolor_png(os.path.join(shop, "무기_%s D3.png" % w.shop_icon_kr),
                    os.path.join(shop, "무기_%s %s.png" % (w.shop_icon_kr, spec.SERIES)), plan.variant, plan)


# ---------------------------------------------------------------------------
# weapon.ini
# ---------------------------------------------------------------------------

_RESOURCE_KEYS = ("Image", "LineTexture")


def custom_section(plan, groups, w, fields, is_piece):
    """爆裂 3 的一节 → 自定义小节的 `(键, 值)` 列表。返回 `(rows, groups)`。"""
    rows = []
    new_values = {}

    def resolve(key, value):
        kk = key.lstrip("_")
        if kk == "Id":
            return str(w.piece_id if is_piece else w.ammo_id)
        if kk == "Name":
            # `리볼버 D3` → `리볼버 C`、`사과탄D3조각` → `사과탄조각 C`：把档位字样去掉再挂系列字母
            return "%s %s" % (re.sub(r"\s*D3", "", value).strip(), spec.SERIES)
        if kk == "WMeshIdx" and not is_piece:
            return str(w.mesh_idx)
        if kk == "Icon":
            return "%s%s,%d" % (w.icon_set, spec.SERIES, w.character)
        if kk == "SliceId" and w.piece_id:
            return str(w.piece_id)
        if kk == "Image":
            kind, _sep, rest = value.partition(",")
            kind = kind.strip()
            if kind == "Anim":
                name, new_groups = anim_copy(plan, groups[0], rest.strip())
                groups[0] = new_groups
                return "Anim," + name
            if kind == "Model":
                return "Model," + re.sub(r"D0132$", "D0142", rest.strip())
            if kind == "Effect":
                return "Effect," + efx_copy(plan, rest)
            return value
        if kk.startswith("Eff"):
            return ",".join(efx_copy(plan, part) for part in value.split(",") if part.strip())
        if kk == "LineTexture" and value.strip():
            return line_texture_copy(plan, value)
        if kk.startswith("Sound") and value.strip():
            return resolve_sound(plan, w, value.strip())
        return value

    # 先算普通键，再让 `_` 键镜像普通键（`_Sound-*` 保留原来的低龄音）。
    for key, value in fields.items():
        if key.startswith("_") or key.startswith("#"):
            continue
        new_values[key] = resolve(key, value)
        rows.append((key, new_values[key]))
    for key, value in fields.items():
        if key.startswith("#"):
            rows.append((key, value))      # 原版注释掉的键原样带走
            continue
        if not key.startswith("_"):
            continue
        base = key[1:]
        if base.startswith("Sound"):
            rows.append((key, value))
        elif base in new_values:
            rows.append((key, new_values[base]))
        # 普通键里没有的 `_X`（比如只有低龄版才有的键）：丢掉 —— 低龄版和普通版指同一批资源。
    return rows, groups


def build_weapon_ini(plan, groups):
    root = spec.develop_root()
    path = os.path.join(root, "Data", "weapon.ini")
    raw = open(path, "rb").read()
    head = raw[:ORIGINAL_SIZE]
    if hashlib.sha256(head).hexdigest() != ORIGINAL_SHA256:
        raise SystemExit("weapon.ini 前 %d 字节不是原版（sha256 不对）—— 先 git 恢复原版再跑" % ORIGINAL_SIZE)
    tail = raw[ORIGINAL_SIZE:]
    marker = INI_MARKER.encode("ascii")
    if tail and marker not in tail:
        raise SystemExit("weapon.ini 原版后面有不认识的内容（不是本脚本追加的块），不敢动")
    sections = spec.read_weapon_ini(path)
    lines = ["", INI_MARKER] + list(INI_BANNER) + [""]
    holder = [groups]
    for w in spec.WEAPONS:
        for name, ref, is_piece in ((w.custom_section, w.section, False),
                                    (w.custom_piece_section, w.piece_section, True)):
            if not name:
                continue
            rows, _g = custom_section(plan, holder, w, sections[ref], is_piece)
            lines.append("[%s]" % name)
            lines.extend("%s=%s" % (k, v) for k, v in rows)
            lines.append("")
    block = ("\r\n".join(lines) + "\r\n").encode("cp949")
    plan.put(path, head + block)
    return holder[0]


# ---------------------------------------------------------------------------
# ShopItem-Chn.ini
# ---------------------------------------------------------------------------

def build_shop_ini(plan):
    root = spec.develop_root()
    path = os.path.join(root, "Data", "ShopItem-Chn.ini")
    raw = open(path, "rb").read()
    if raw[:2] != b"\xff\xfe":
        raise SystemExit("ShopItem-Chn.ini 不是 UTF-16LE 带 BOM，不敢动")
    text = raw[2:].decode("utf-16le")
    first = "[Item-%d]" % spec.WEAPONS[0].item_id
    cut = text.find(first)
    if cut >= 0:
        text = text[:cut]
    if not text.endswith("\n"):
        text += "\n"
    lines = []
    for w in spec.WEAPONS:
        image = "Images/Shop/무기_%s %s.png" % (w.shop_icon_kr, spec.SERIES)
        lines += ["[Item-%d]" % w.item_id, "Image=" + image, "Tag=%d.0" % w.ammo_id,
                  "PartFlag=%d" % spec.WEAPON_SLOT_FLAG[w.slot], "",
                  "[Stock-%d]" % w.item_id, "Image=" + image, ""]
    plan.put(path, b"\xff\xfe" + (text + "\n".join(lines)).encode("utf-16le"))


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="生成 9 把自定义黄金武器的全部资源")
    ap.add_argument("--variant", default="A", choices=palette.VARIANTS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    plan = Plan(args.variant, args.dry_run)
    root = spec.develop_root()
    amf_path = os.path.join(root, "Data", "default.amf")
    version, groups = amftool.load(amf_path)

    build_meshes(plan)
    build_icons(plan)
    groups = build_weapon_ini(plan, groups)
    build_shop_ini(plan)
    plan.put(amf_path, amftool.write(version, groups))

    wrote, same = plan.flush()
    print("配色：%s（%s）" % (args.variant, palette.variant_label(args.variant)))
    print("%s%d 个文件，%d 个已是最新" % ("（dry-run）将写 " if args.dry_run else "写入 ", wrote, same))
    kinds = collections.Counter(os.path.splitext(p)[1].lower() for p in plan.files)
    print("  " + "  ".join("%s×%d" % (k or "?", n) for k, n in sorted(kinds.items())))
    for note in plan.notes:
        print("  " + note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
