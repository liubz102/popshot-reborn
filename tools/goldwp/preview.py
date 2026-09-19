#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preview.py —— 自定义武器配色的**效果图**（阶段 1：出图给用户确认，不写任何游戏资源）。

    C:\\Python314\\python.exe tools/goldwp/preview.py --batch P
    C:\\Python314\\python.exe tools/goldwp/preview.py --batch C --variants A,B,C --only models

产物（都在 `tools/goldwp/`，文件名带批次后缀，两批并存互不覆盖）：
    preview_models_<批>.png    9 把手持件 × {原版, 各变体}，每格 front + rot35 两个视角
    preview_sprites_<批>.png   弹体精灵 / HUD 图标 / 商店图标 / 3D 弹体贴图 的 原版 → 各变体
    preview_effects_<批>.png   特效贴图里**本方案会改到**的那些 的 原版 → 各变体

★ 母本、配色方案、变体全部由 `spec.BATCHES` / `palette.SCHEMES` 决定，这里一个 `D3` / `F3`
  字面量都不许写死（X6 之前写死过，换母本时是最难查的一类 bug）。

渲染用 `tools/mshtool.rasterize`（带贴图的软光栅，只为看配色和形状，不等于游戏画面）。
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import amftool  # noqa: E402
import dds_edit  # noqa: E402
import mshtool  # noqa: E402
import palette  # noqa: E402
import spec  # noqa: E402

CELL = 150          # 模型格：每个视角 150 px
BG = (58, 58, 66)
PAPER = (30, 30, 34)
HEAD = 52           # 表头带高：标题一行 + 列名一行（★ 挤在一行会重叠，X6 之前就是这毛病）
LABEL_W = 132       # 左边那列行名的宽度
CHAR_ZH = {0: "泰尔", 1: "卡希尔", 2: "布洛克"}
SLOT_ZH = {1: "1号", 2: "2号", 3: "3号"}
#: 母本系列字母 → 中文名（和 `tools/shopdata.SERIES_NAMES` 同一套叫法）。
SRC_ZH = {"D3": "爆裂3", "R3": "极速3", "F3": "复合3"}


def _font(size=14):
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
        path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _label(img, xy, text, size=14, fill=(240, 240, 240)):
    ImageDraw.Draw(img).text(xy, text, font=_font(size), fill=fill)


def _column_titles(batch, variants):
    """表头那一行：原版 + 各变体。"""
    return ["原版 " + SRC_ZH.get(batch.src_stem, batch.src_stem)] + \
           ["%s（%s）" % (palette.variant_label(v), v) for v in variants]


def render_mesh(msh_path, rgba, views=("front", "rot35"), size=CELL):
    m = mshtool.load(msh_path)
    tiles = [Image.fromarray(mshtool.rasterize([(m, rgba)], view=v, size=size, bg=BG))
             for v in views]
    sheet = Image.new("RGB", (size * len(tiles), size), BG)
    for i, tile in enumerate(tiles):
        sheet.paste(tile, (i * size, 0))
    return sheet


def _src_mesh(w):
    """母本手持件的 `(msh, dds)` 绝对路径。`mesh_idx - 10` 对每一批都成立（见 spec 的注释）。"""
    d = spec.chars_dir(w.character)
    stem = "ch%02dW%04d" % (w.character, w.mesh_idx - 10)
    return os.path.join(d, stem + ".msh"), os.path.join(d, stem + ".dds")


def models_sheet(batch, variants):
    weapons = spec.weapons_of(batch.letter)
    titles = _column_titles(batch, variants)
    cell_w = CELL * 2 + 6
    cell_h = CELL + 24
    img = Image.new("RGB", (LABEL_W + len(titles) * cell_w, HEAD + len(weapons) * cell_h), PAPER)
    _label(img, (8, 6), "手持件（front / rot35）· 母本 %s → 自定义 %s"
           % (SRC_ZH.get(batch.src_stem, batch.src_stem), batch.letter), 15)
    for c, name in enumerate(titles):
        _label(img, (LABEL_W + c * cell_w + 4, HEAD - 22), name, 15, (255, 215, 120))
    for r, w in enumerate(weapons):
        msh, dds = _src_mesh(w)
        _hd, rgba = dds_edit.load_rgba(dds)
        y = HEAD + r * cell_h
        _label(img, (8, y + CELL // 2 - 8), "%s %s" % (CHAR_ZH[w.character], SLOT_ZH[w.slot]), 15)
        for c, variant in enumerate(["orig"] + list(variants)):
            img.paste(render_mesh(msh, palette.recolor_rgba(rgba, variant)), (LABEL_W + c * cell_w, y))
        print("  模型 %s 渲完" % w.section)
    return img


def _frames_of(png_path, smf_path):
    """按 .smf 的帧表把图集切成小图（left, top, right, bottom 前四个 int）。"""
    import struct
    blob = open(smf_path, "rb").read()
    count = struct.unpack_from("<I", blob, 4)[0]
    img = Image.open(png_path).convert("RGBA")
    out = []
    for i in range(count):
        x0, y0, x1, y1 = struct.unpack_from("<4i", blob, 8 + i * 32)
        out.append(img.crop((x0, y0, x1, y1)))
    return out


def _sprite_sources(batch):
    """(标题, PIL RGBA) —— 弹体精灵、碎片、HUD 图标、商店图标、3D 弹体贴图。"""
    root = spec.develop_root()
    game = os.path.join(root, "Images", "Game")
    shop = os.path.join(root, "Images", "Shop")
    sections = spec.read_weapon_ini()
    _v, groups = amftool.load(os.path.join(root, "Data", "default.amf"))
    weapons = spec.weapons_of(batch.letter)
    out = []

    def anim(entry_name, title):
        e = amftool.find(groups, "Game/Bullet", entry_name)
        if e is None:
            return
        png = os.path.join(root, e.path.lstrip("/"))
        if os.path.isfile(png):
            out.append((title % os.path.basename(png), Image.open(png).convert("RGBA")))

    for w in weapons:
        image = sections[w.section].get("Image", "")
        if image.startswith("Anim,"):
            anim(image.split(",", 1)[1].strip(), "弹体 %s")
        if w.piece_section:
            pimage = sections[w.piece_section].get("Image", "")
            if pimage.startswith("Anim,"):
                anim(pimage.split(",", 1)[1].strip(), "碎片 %s")
        icon_png = os.path.join(game, w.icon_set + batch.src_stem + ".png")
        icon_smf = os.path.join(game, w.icon_set + batch.src_stem + ".smf")
        out.append(("HUD 图标 %s帧%d" % (w.icon_set, w.character),
                    _frames_of(icon_png, icon_smf)[w.character]))
        shop_png = os.path.join(shop, "무기_%s %s.png" % (w.shop_icon_kr, batch.src_stem))
        out.append(("商店图标 " + CHAR_ZH[w.character] + SLOT_ZH[w.slot],
                    Image.open(shop_png).convert("RGBA")))

    for w in weapons:
        # 3D 弹体贴图。★ 按 ini 里的 `Image=Model,<路径>` 取，**不要按文件名猜**：
        #   `ch02D033x.dds` 在盘上是有的，但复合 3 的布洛克 2 号走精灵、根本没引用它。
        image = sections[w.section].get("Image", "")
        if not image.startswith("Model,"):
            continue
        rel = image.split(",", 1)[1].strip().replace("\\", "/")
        dds = os.path.join(root, "Models", rel + ".dds")
        if os.path.isfile(dds):
            _hd, rgba = dds_edit.load_rgba(dds)
            out.append(("3D 弹体贴图 " + os.path.basename(rel), Image.fromarray(rgba, "RGBA")))
    return out


def tiles_sheet(title, sources, batch, variants, tile=112, columns=1, label_w=230):
    """一行一个素材：名字 + 原版 + 各变体。`columns` > 1 时把行折成几大列并排（素材多时省高度）。"""
    titles = _column_titles(batch, variants)
    # ★ 列宽取「图块」和「表头文字」里宽的那个 —— 只按 tile 算的话，两列并排时
    #   变体名（「霓虹粉 + 荧光绿（P1）」）会糊成一片（X6 第一版就是这样）。
    font = _font(13)
    step = max(tile + 8, max(int(font.getlength(t)) for t in titles) + 10)
    block_w = label_w + len(titles) * step
    per_col = (len(sources) + columns - 1) // columns
    img = Image.new("RGB", (block_w * columns, HEAD + per_col * (tile + 8)), PAPER)
    _label(img, (8, 6), title, 15)
    for b in range(columns):
        for c, name in enumerate(titles):
            _label(img, (b * block_w + label_w + c * step + 4, HEAD - 22), name, 13, (255, 215, 120))
    for r, (name, src) in enumerate(sources):
        b, rr = divmod(r, per_col)
        x0 = b * block_w
        y = HEAD + rr * (tile + 8)
        _label(img, (x0 + 8, y + tile // 2 - 8), name, 11)
        rgba = np.asarray(src.convert("RGBA"))
        for c, variant in enumerate(["orig"] + list(variants)):
            pic = Image.fromarray(palette.recolor_rgba(rgba, variant), "RGBA")
            back = Image.new("RGBA", pic.size, (70, 70, 78, 255))   # 透明底铺深灰，看得清半透明特效
            back.alpha_composite(pic)
            back.thumbnail((tile, tile))
            img.paste(back.convert("RGB"), (x0 + label_w + c * step, y + 4))
    return img


def _effect_textures(batch, variant):
    """这一批的特效贴图里**本方案会改到**的那些（判据和 `build.py` 的 `texture_copy` 一致）。"""
    import re
    root = spec.develop_root()
    sections = spec.read_weapon_ini()
    efx = []
    for _name, fields in spec.src_sections(sections, spec.weapons_of(batch.letter)).items():
        for key, value in fields.items():
            if key.startswith("_"):       # 全年龄键走镜像，build.py 不解析
                continue
            if key.startswith("Eff") or (key == "Image" and value.startswith("Effect,")):
                for part in value.split(","):
                    part = part.strip()
                    if part.lower().endswith(".efx"):
                        efx.append(part if part.lower().startswith("effects/") else "Effects/" + part)
    seen, textures = set(), []
    for rel in efx:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        text = open(path, "rb").read().decode("cp949", "replace")
        for m in re.finditer(r"<TextureFileName>([^<]+)</TextureFileName>", text):
            t = m.group(1).strip().replace("\\", "/")
            if t.lower() in seen:
                continue
            seen.add(t.lower())
            textures.append(os.path.join(root, "Effects", t))
    out = []
    for path in textures:
        if not os.path.isfile(path):
            continue
        try:
            _hd, rgba = dds_edit.load_rgba(path)
        except Exception as exc:  # noqa: BLE001
            print("  跳过 %s：%s" % (path, exc))
            continue
        if palette.has_hue(rgba, variant):
            out.append((os.path.basename(path), Image.fromarray(rgba, "RGBA")))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="自定义武器配色效果图")
    ap.add_argument("--batch", default="P", choices=tuple(spec.BATCHES))
    ap.add_argument("--variants", default=None,
                    help="逗号分隔；缺省 = 这一批所属方案的全部变体")
    ap.add_argument("--only", choices=("models", "sprites", "effects"))
    args = ap.parse_args(argv)
    batch = spec.BATCHES[args.batch]
    variants = [v.strip() for v in args.variants.split(",")] if args.variants \
        else list(palette.variants_of(batch.scheme))
    for v in variants:
        palette.scheme_for(v)                      # 变体名打错就当场报，别渲到一半才发现
    print("批次 %s（母本 %s，配色方案 %s）：变体 %s"
          % (batch.letter, batch.src_stem, batch.scheme, ", ".join(variants)))
    tag = "_" + batch.letter

    if not args.only or args.only == "models":
        models_sheet(batch, variants).save(os.path.join(HERE, "preview_models%s.png" % tag))
        print("-> preview_models%s.png" % tag)
    if not args.only or args.only == "sprites":
        tiles_sheet("弹体精灵 / 碎片 / HUD 图标 / 商店图标 / 3D 弹体贴图",
                    _sprite_sources(batch), batch, variants, columns=2) \
            .save(os.path.join(HERE, "preview_sprites%s.png" % tag))
        print("-> preview_sprites%s.png" % tag)
    if not args.only or args.only == "effects":
        tex = _effect_textures(batch, variants[0])
        print("  特效贴图里本方案会改到的 %d 张" % len(tex))
        tiles_sheet("特效贴图（只列本方案会改到的）", tex, batch, variants,
                    tile=80, columns=3, label_w=200) \
            .save(os.path.join(HERE, "preview_effects%s.png" % tag))
        print("-> preview_effects%s.png" % tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
