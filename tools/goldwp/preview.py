#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preview.py —— 自定义武器配色的**效果图**（X_Mod · X3，阶段 1：出图给用户确认，不写任何资源）。

    C:\\Python314\\python.exe tools/goldwp/preview.py            # 三张图：模型 / 精灵与图标 / 特效贴图
    C:\\Python314\\python.exe tools/goldwp/preview.py --variants A,B

产物（都在 `tools/goldwp/`）：
    preview_models.png    9 把手持件 × {原版 D3, 金+A, 金+B, 金+C}，每格 front + rot35 两个视角
    preview_sprites.png   弹体精灵 / HUD 图标 / 商店图标 的 原版 → 三个变体
    preview_effects.png   特效里含红 / 橙的贴图 的 原版 → 三个变体

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
import dds_edit  # noqa: E402
import mshtool  # noqa: E402
import palette  # noqa: E402
import spec  # noqa: E402

CELL = 150          # 模型格：每个视角 150 px
BG = (58, 58, 66)
CHAR_ZH = {0: "泰尔", 1: "卡希尔", 2: "布洛克"}
SLOT_ZH = {1: "1号", 2: "2号", 3: "3号"}


def _font(size=14):
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
        path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _label(img, xy, text, size=14, fill=(240, 240, 240)):
    ImageDraw.Draw(img).text(xy, text, font=_font(size), fill=fill)


def render_mesh(msh_path, rgba, views=("front", "rot35"), size=CELL):
    m = mshtool.load(msh_path)
    tiles = [Image.fromarray(mshtool.rasterize([(m, rgba)], view=v, size=size, bg=BG))
             for v in views]
    sheet = Image.new("RGB", (size * len(tiles), size), BG)
    for i, tile in enumerate(tiles):
        sheet.paste(tile, (i * size, 0))
    return sheet


def models_sheet(variants):
    cols = 1 + len(variants)
    cell_w = CELL * 2 + 6
    cell_h = CELL + 24
    img = Image.new("RGB", (120 + cols * cell_w, 30 + len(spec.WEAPONS) * cell_h), (30, 30, 34))
    _label(img, (8, 6), "手持件（front / rot35）", 15)
    for c, name in enumerate(["原版 爆裂3"] + ["金 + " + palette.CONTRAST[v][0] + "（%s）" % v for v in variants]):
        _label(img, (120 + c * cell_w + 4, 6), name, 15, (255, 215, 120))
    for r, w in enumerate(spec.WEAPONS):
        d3_idx = w.mesh_idx - 10
        msh = os.path.join(spec.chars_dir(w.character), "ch%02dW%04d.msh" % (w.character, d3_idx))
        dds = os.path.join(spec.chars_dir(w.character), "ch%02dW%04d.dds" % (w.character, d3_idx))
        _hd, rgba = dds_edit.load_rgba(dds)
        y = 30 + r * cell_h
        _label(img, (8, y + CELL // 2 - 8), "%s %s" % (CHAR_ZH[w.character], SLOT_ZH[w.slot]), 15)
        for c, variant in enumerate(["orig"] + list(variants)):
            tex = palette.recolor_rgba(rgba, variant)
            tile = render_mesh(msh, tex)
            img.paste(tile, (120 + c * cell_w, y + 20))
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


def _sprite_sources():
    """(标题, PIL RGBA) —— 弹体精灵、HUD 图标、商店图标。"""
    root = spec.develop_root()
    game = os.path.join(root, "Images", "Game")
    shop = os.path.join(root, "Images", "Shop")
    out = []
    sections = spec.read_weapon_ini()
    for w in spec.WEAPONS:
        fields = sections[w.section]
        image = fields.get("Image", "")
        if image.startswith("Anim,"):
            entry = image.split(",", 1)[1].strip()
            import amftool
            _v, groups = amftool.load(os.path.join(root, "Data", "default.amf"))
            e = amftool.find(groups, "Game/Bullet", entry)
            if e:
                png = os.path.join(root, e.path.lstrip("/"))
                out.append(("弹体 %s" % os.path.basename(png), Image.open(png).convert("RGBA")))
        if w.piece_section:
            pimage = sections[w.piece_section].get("Image", "")
            if pimage.startswith("Anim,"):
                import amftool
                _v, groups = amftool.load(os.path.join(root, "Data", "default.amf"))
                e = amftool.find(groups, "Game/Bullet", pimage.split(",", 1)[1].strip())
                if e:
                    png = os.path.join(root, e.path.lstrip("/"))
                    out.append(("碎片 %s" % os.path.basename(png), Image.open(png).convert("RGBA")))
        icon_png = os.path.join(game, "%sD3.png" % w.icon_set)
        icon_smf = os.path.join(game, "%sD3.smf" % w.icon_set)
        frame = _frames_of(icon_png, icon_smf)[w.character]
        out.append(("HUD 图标 %s帧%d" % (w.icon_set, w.character), frame))
        shop_png = os.path.join(shop, "무기_%s D3.png" % w.shop_icon_kr)
        out.append(("商店图标 %s" % CHAR_ZH[w.character] + SLOT_ZH[w.slot], Image.open(shop_png).convert("RGBA")))
    # 2 号的 3D 弹体贴图
    for w in spec.WEAPONS:
        if w.slot == 2 and w.character in (0, 1):
            dds = os.path.join(spec.chars_dir(w.character), "ch%02dD0132.dds" % w.character)
            _hd, rgba = dds_edit.load_rgba(dds)
            out.append(("3D 弹体贴图 ch%02dD0132" % w.character, Image.fromarray(rgba, "RGBA")))
    return out


def tiles_sheet(title, sources, variants, tile=112, columns=1, label_w=230):
    """一行一个素材：名字 + 原版 + 各变体。`columns` > 1 时把行折成几大列并排（素材多时省高度）。"""
    cols = 1 + len(variants)
    block_w = label_w + cols * (tile + 8)
    per_col = (len(sources) + columns - 1) // columns
    img = Image.new("RGB", (block_w * columns, 30 + per_col * (tile + 8)), (30, 30, 34))
    _label(img, (8, 6), title, 15)
    for b in range(columns):
        for c, name in enumerate(["原版"] + [palette.CONTRAST[v][0] + "（%s）" % v for v in variants]):
            _label(img, (b * block_w + label_w + c * (tile + 8) + 4, 6), name, 15, (255, 215, 120))
    for r, (name, src) in enumerate(sources):
        b, rr = divmod(r, per_col)
        x0 = b * block_w
        y = 30 + rr * (tile + 8)
        _label(img, (x0 + 8, y + tile // 2 - 8), name, 11)
        rgba = np.asarray(src.convert("RGBA"))
        for c, variant in enumerate(["orig"] + list(variants)):
            out = palette.recolor_rgba(rgba, variant)
            pic = Image.fromarray(out, "RGBA")
            # 透明底铺一层深灰，看得清半透明特效
            back = Image.new("RGBA", pic.size, (70, 70, 78, 255))
            back.alpha_composite(pic)
            back.thumbnail((tile, tile))
            img.paste(back.convert("RGB"), (x0 + label_w + c * (tile + 8), y + 4))
    return img


def _effect_textures():
    """9 把武器（含子弹药）的特效贴图里**含红 / 橙**的那些。"""
    import re
    root = spec.develop_root()
    sections = spec.read_weapon_ini()
    efx = []
    for name, fields in spec.d3_sections(sections).items():
        for key, value in fields.items():
            kk = key.lstrip("_")
            if kk.startswith("Eff") or (kk == "Image" and value.startswith("Effect,")):
                for part in value.split(","):
                    part = part.strip()
                    if part.lower().endswith(".efx"):
                        p = part if part.lower().startswith("effects/") else "Effects/" + part
                        efx.append(p)
    seen = set()
    textures = []
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
        if palette.has_hue(rgba):
            out.append((os.path.basename(path), Image.fromarray(rgba, "RGBA")))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="自定义武器配色效果图")
    ap.add_argument("--variants", default="A,B,C")
    ap.add_argument("--only", choices=("models", "sprites", "effects"))
    args = ap.parse_args(argv)
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    if not args.only or args.only == "models":
        models_sheet(variants).save(os.path.join(HERE, "preview_models.png"))
        print("-> preview_models.png")
    if not args.only or args.only == "sprites":
        tiles_sheet("弹体精灵 / HUD 图标 / 商店图标 / 3D 弹体贴图", _sprite_sources(), variants) \
            .save(os.path.join(HERE, "preview_sprites.png"))
        print("-> preview_sprites.png")
    if not args.only or args.only == "effects":
        tex = _effect_textures()
        print("  特效里含红 / 橙的贴图 %d 张" % len(tex))
        tiles_sheet("特效贴图（只列含红 / 橙的）", tex, variants, tile=80, columns=3, label_w=200) \
            .save(os.path.join(HERE, "preview_effects.png"))
        print("-> preview_effects.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
