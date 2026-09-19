#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py —— 把全部自定义武器的资源**从各自的母本生成出来**（X_Mod · X3 / X6，阶段 2）。

    C:\\Python314\\python.exe tools/custom-weapon/build.py                      # 全部批次一起，幂等
    C:\\Python314\\python.exe tools/custom-weapon/build.py --dry-run
    C:\\Python314\\python.exe tools/custom-weapon/build.py --variant P=orig     # 临时覆盖某一批（只搬运不改色）

★★ **一次跑全部批次**，不能只跑一批：`weapon.ini` 和 `ShopItem-Chn.ini` 都是
   「砍掉标记 / 锚点之后的一切再整块重写」，只跑一批会把另一批悄悄带走。

产出（全部落在 `game_patched/Pack_develop/`，之后跑 `tools\\build-pack.bat`）。`<字>` = 批次字母
（`C` 爆裂 3 → 黄金 + 银白；`P` 复合 3 → 粉 + 绿），`<档>` = 该系列的第 4 档：

    Models/Characters/chNN/chNNW0<系>4S.msh + .dds    手持件（等长改名，母本编号 = 目标 − 10，贴图改色）
    Models/Characters/chNN/chNND0<系>42.msh + .dds    3D 弹体（**按 ini 的 `Image=Model,` 判有没有**）
    Images/Game/CHnn_Wp0k<字>_*.png + .smf            2D 弹体 / 碎片精灵（改色）
    Images/Game/Wp0k<字>.png + .smf                   HUD 武器图标（3 帧 = 3 角色，改色；`.smf` 取本批母本那份）
    Images/Shop/무기_<韩文名> <字>.png                 商店图标（改色）
    Effects/CHnn/WP0k/Efx/*<字>*.efx                  特效副本：贴图指向改色副本，<ColorValue> 按同一套规则换色
    Effects/**/Texture/*_<字>.dds                     特效贴图的改色副本（本方案改得到的才做，其余共用原图）
    Data/default.amf                                  追加 CHnn_WPk_<字> 条目
    Data/weapon.ini                                   末尾一整块（每批 9 主 + 子弹药），**原版部分一个字节不动**
    Data/ShopItem-Chn.ini                             末尾每批 9 组 [Item-] + [Stock-]

## 铁律

* 母本只读：只读原版那一档的文件，一个字节不写回去；重跑就是重做一遍。
* `weapon.ini` 前 221519 字节必须等于原版（sha256 见 `ORIGINAL_SHA256`），追加块以标记行开头，
  重跑先把旧块砍掉再追加 —— `test/test_weaponini.py` 盯着这一条。
* `ShopItem-Chn.ini` 的切口是 `spec.WEAPONS[0].item_id`，`tools/openweapons.py` 的
  `CUSTOM_WEAPON_ANCHOR` 钉着同一个值（那 18 件原版 3 级武器插在本块之前）。
* `_` 前缀（全年龄版）的资源键**全部指向和普通键同一批资源**（`_Sound-*` 除外），
  不另做气泡版：不管客户端在哪种模式下都看到同一套颜色。
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
    "; Custom weapons + sub-ammo sections, one block per batch (see tools/custom-weapon/spec.BATCHES).",
    "; Numbers are copied from each batch's reference sections; the live values come from",
    "; the server (admin page -> 0x0F01 -> bshook), not from this file.",
)


class Plan(object):
    """所有要写的文件先攒在这里，最后统一落盘（`--dry-run` 只打印）。

    ★ `batch` 是「当前正在处理哪一批」—— 配色方案 / 母本系列 / 改名字母全从它取。
      每个入口（`build_meshes` / `build_icons` / `build_weapon_ini`）在切到一把武器时设置它。
    """

    def __init__(self, variants, dry_run):
        self.variants = dict(variants)           # 批次字母 -> 变体名
        self.batch = None                        # 当前批次（spec.Batch）
        self.dry_run = dry_run
        self.files = collections.OrderedDict()   # 目标路径 -> bytes
        self.notes = []
        #: `(批次字母, 源贴图路径)` -> efx 里该写的相对名。
        #: ★ 键里**必须带批次**：跨系列共用的贴图（名字里没有 D3/F3 的那些）两批都会引用，
        #:   只按路径缓存的话第二批会拿到第一批的副本，颜色串台。
        self.tex_cache = {}

    @property
    def variant(self):
        return self.variants[self.batch.letter]

    def use(self, weapon_or_batch):
        """切到某一批；返回它，方便 `plan.use(w).letter` 这样连写。"""
        self.batch = weapon_or_batch if isinstance(weapon_or_batch, spec.Batch) \
            else spec.batch_of(weapon_or_batch)
        return self.batch

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

_SECTIONS = []          # 懒加载的 weapon.ini（`spec.read_weapon_ini` 每次都重新解析整份）


def _sections():
    if not _SECTIONS:
        _SECTIONS.append(spec.read_weapon_ini())
    return _SECTIONS[0]


def _model_bullet(w):
    """这把武器的弹体是不是 3D 网格（`Image=Model,…`）。

    ★ 按 ini 里写的判，**不要按「2 号槽 + 角色 0/1」猜**：爆裂 3 恰好是那样，
      复合 3 的布洛克 2 号走精灵，而 `ch02D033x.msh` 在盘上却是有的（原版留着没用）。
    """
    return _sections()[w.section].get("Image", "").startswith("Model,")


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
        plan.use(w)
        d = spec.chars_dir(w.character)
        # 母本一律是各系列的第 3 档、产物是第 4 档 ⇒ 源编号 = 目标编号 − 10（见 spec 的注释）。
        pairs = [("ch%02dW%04d" % (w.character, w.mesh_idx - 10), "ch%02dW%04d" % (w.character, w.mesh_idx))]
        if _model_bullet(w):
            pairs.append(("ch%02dD0%03d" % (w.character, w.mesh_idx - 10),
                          "ch%02dD0%03d" % (w.character, w.mesh_idx)))
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
    key = (plan.batch.letter, src)
    if key in plan.tex_cache:
        return plan.tex_cache[key]
    if not os.path.isfile(src):
        plan.notes.append("⚠ 特效贴图不存在，原样引用：%s" % tex_rel)
        plan.tex_cache[key] = tex_rel
        return tex_rel
    _hd, rgba = dds_edit.load_rgba(src)
    if plan.variant == "orig" or not palette.has_hue(rgba, plan.variant):
        plan.tex_cache[key] = tex_rel          # 本方案改不到的颜色：共用原图
        return tex_rel
    stem, ext = os.path.splitext(os.path.basename(rel))
    new_rel = os.path.join(os.path.dirname(rel), spec.rename(stem, plan.batch) + ext).replace("/", "\\")
    dst = os.path.join(root, "Effects", new_rel.replace("\\", "/"))
    recolor_dds(src, dst, plan.variant, plan)
    plan.tex_cache[key] = new_rel
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
    new_rel = os.path.join(os.path.dirname(rel), spec.rename(stem, plan.batch) + ext).replace("\\", "/")
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
    new_rel = os.path.join(os.path.dirname(rel), spec.rename(stem, plan.batch) + ext).replace("\\", "/")
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
    new_name = spec.rename(entry_name, plan.batch)
    src_png = os.path.join(root, entry.path.lstrip("/").replace("\\", "/"))
    stem, ext = os.path.splitext(os.path.basename(src_png))
    new_base = spec.rename(stem, plan.batch)
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
    for batch in spec.BATCHES.values():
        plan.use(batch)
        for icon_set in sorted(set(w.icon_set for w in spec.weapons_of(batch.letter))):
            src = os.path.join(game, icon_set + batch.src_stem)
            dst = os.path.join(game, icon_set + batch.letter)
            recolor_png(src + ".png", dst + ".png", plan.variant, plan)
            # ★ `.smf` 取**本批母本自己那份**：`Wp01F3.smf` 和 `Wp01D3.smf` 的帧尺寸 / 偏移不一样
            #   （128×101 @55,20 vs 128×102 @57,16），照抄错了图标会偏。
            copy_file(src + ".smf", dst + ".smf", plan)
    for w in spec.WEAPONS:
        batch = plan.use(w)
        recolor_png(os.path.join(shop, "무기_%s %s.png" % (w.shop_icon_kr, batch.src_stem)),
                    os.path.join(shop, "무기_%s %s.png" % (w.shop_icon_kr, batch.letter)), plan.variant, plan)


# ---------------------------------------------------------------------------
# weapon.ini
# ---------------------------------------------------------------------------

_RESOURCE_KEYS = ("Image", "LineTexture")


def custom_section(plan, groups, w, fields, is_piece):
    """母本的一节 → 自定义小节的 `(键, 值)` 列表。返回 `(rows, groups)`。"""
    rows = []
    new_values = {}
    batch = plan.use(w)
    drop_stem = re.compile(r"\s*" + re.escape(batch.src_stem), re.IGNORECASE)

    def resolve(key, value):
        kk = key.lstrip("_")
        if kk == "Id":
            return str(w.piece_id if is_piece else w.ammo_id)
        if kk == "Name":
            # `리볼버 D3` → `리볼버 C`、`사과탄F3조각` → `사과탄조각 P`：去掉档位字样再挂批次字母
            return "%s %s" % (drop_stem.sub("", value).strip(), batch.letter)
        if kk == "WMeshIdx" and not is_piece:
            return str(w.mesh_idx)
        if kk == "Icon":
            # ★ **重算**，不照抄母本：原版 `CH01-01F3` 的 Icon 写的是 `Wp01F2,1`（笔误，R3 也一样）。
            return "%s%s,%d" % (w.icon_set, batch.letter, w.character)
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
                # `Characters/ch00/ch00D0332` → `…D0342`：源编号 = 目标编号 − 10（同手持件）。
                old = "D0%03d" % (w.mesh_idx - 10)
                new = "D0%03d" % w.mesh_idx
                rest = rest.strip()
                if not rest.endswith(old):
                    raise SystemExit("%s 的 Image=Model,%s 结尾不是 %s，编号口径对不上"
                                     % (w.section, rest, old))
                return "Model," + rest[:-len(old)] + new
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
        plan.use(w)
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
        image = "Images/Shop/무기_%s %s.png" % (w.shop_icon_kr, w.batch)
        lines += ["[Item-%d]" % w.item_id, "Image=" + image, "Tag=%d.0" % w.ammo_id,
                  "PartFlag=%d" % spec.WEAPON_SLOT_FLAG[w.slot], "",
                  "[Stock-%d]" % w.item_id, "Image=" + image, ""]
    plan.put(path, b"\xff\xfe" + (text + "\n".join(lines)).encode("utf-16le"))


# ---------------------------------------------------------------------------

def resolve_variants(overrides):
    """`{批次字母: 变体名}`。默认取 `spec.BATCHES[x].variant`，`--variant C=orig` 这样覆盖。

    ★ 变体**记在 spec 里**，不在命令行默认值里：不带参数重跑就能原样重现盘上那一套。
      （X6 之前 `--variant` 默认 `A`，而线上用的是 `C` —— 忘了带参数就把第一批刷成黑曜。）
    """
    out = {letter: b.variant for letter, b in spec.BATCHES.items()}
    for item in overrides or ():
        letter, _sep, variant = item.partition("=")
        if letter not in out:
            raise SystemExit("没有这个批次：%s（可选 %s）" % (letter, ", ".join(spec.BATCHES)))
        palette.scheme_for(variant) if variant != "orig" else None
        out[letter] = variant
    missing = [k for k, v in out.items() if v is None]
    if missing:
        raise SystemExit("批次 %s 还没定配色 —— 先跑 preview.py 出效果图让用户选，"
                         "再把变体填进 spec.BATCHES（或临时 --variant %s=<变体>）"
                         % ("/".join(missing), missing[0]))
    for letter, variant in out.items():
        if variant != "orig" and palette.SCHEME_OF[variant] != spec.BATCHES[letter].scheme:
            raise SystemExit("批次 %s 的方案是 %s，但变体 %s 属于 %s"
                             % (letter, spec.BATCHES[letter].scheme, variant, palette.SCHEME_OF[variant]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成全部自定义武器的资源（所有批次一起，幂等）")
    ap.add_argument("--variant", action="append", metavar="批次=变体",
                    help="临时覆盖某一批的配色，如 --variant P=P1；默认取 spec.BATCHES")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    plan = Plan(resolve_variants(args.variant), args.dry_run)
    root = spec.develop_root()
    amf_path = os.path.join(root, "Data", "default.amf")
    version, groups = amftool.load(amf_path)

    build_meshes(plan)
    build_icons(plan)
    groups = build_weapon_ini(plan, groups)
    build_shop_ini(plan)
    plan.put(amf_path, amftool.write(version, groups))

    wrote, same = plan.flush()
    for letter, variant in plan.variants.items():
        print("批次 %s（母本 %s）：%s（%s）"
              % (letter, spec.BATCHES[letter].src_stem, variant, palette.variant_label(variant)))
    print("%s%d 个文件，%d 个已是最新" % ("（dry-run）将写 " if args.dry_run else "写入 ", wrote, same))
    kinds = collections.Counter(os.path.splitext(p)[1].lower() for p in plan.files)
    print("  " + "  ".join("%s×%d" % (k or "?", n) for k, n in sorted(kinds.items())))
    for note in plan.notes:
        print("  " + note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
