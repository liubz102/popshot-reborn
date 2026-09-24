#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""effects.py —— 庆典地图缺失的 28 个 `.efx` 特效：从通用特效库挑捐赠者复制、改色、改参数（X_Mod · X12）。

## 为什么是「复制 + 改」而不是从零写

原厂就是这么干的：`Maps/sea-/Effect/*.efx`、`Maps/TestIce/Effect/*.efx`、`Maps/new_fa/Effect/*.efx`
和 `Effects/<库>/Efx/` 里的同名文件**逐字节相同**；`Maps/Camel/Effect/Camel{Pink,Green}Light00.efx`
更是同一张白贴图只换 `<ColorValue>` 派生出来的。`Star00` / `Star01` / `Water00` 在库里有精确同名件。

## 改写方式

`.efx` 是 CP949 编码、CRLF、.NET XmlSerializer 吐出来的 XML（X_Mod §37）。这里**整份按 bytes 处理**，
只替换命中的标签内容，声明 / 换行 / 缩进一个字节不碰（和 `tools/custom-weapon/build.py` 的 `efx_copy` 同一手法）。
颜色是 `<ColorValue>` 的 ARGB 有符号 i32；要染色必须 `UseColorScale=true` + `ColorOperation=Modulate`，
且贴图得是白 / 中性的（`Modulate` 只会把通道往下乘）。

贴图路径相对 `Effects/`、反斜杠，和 efx 放在哪无关 ⇒ 这里**不新建任何 dds**。
"""
from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PACK = os.path.join(ROOT, "game_patched", "Pack_develop")
EFFECTS_DIR = os.path.join(PACK, "Effects")

_LAYER_RE = re.compile(rb"<EffLayer>.*?</EffLayer>", re.S)
_CRLF_LAYER_RE = re.compile(rb"\r\n\s*<EffLayer>.*?</EffLayer>", re.S)


def argb(hex8):
    """`'C8FF8080'` -> `.efx` 里的有符号 i32 字符串。"""
    v = int(hex8, 16) & 0xFFFFFFFF
    if v & 0x80000000:
        v -= 1 << 32
    return str(v)


def _num(v):
    """.NET 的 `R` 格式：整数不带小数点，小数最短表示。"""
    if isinstance(v, str):
        return v
    if float(v).is_integer():
        return str(int(v))
    return repr(float(v))


# ---------------------------------------------------------------------------
#  单层改写原语（都作用在一层的 bytes 上）
# ---------------------------------------------------------------------------

def set_scalar(layer, tag, value):
    pat = re.compile(rb"<%s>[^<]*</%s>|<%s />" % (tag.encode(), tag.encode(), tag.encode()))
    rep = b"<%s>%s</%s>" % (tag.encode(), _num(value).encode("cp949"), tag.encode())
    out, n = pat.subn(lambda m: rep, layer, count=1)
    if n != 1:
        raise KeyError("层里没有标签 <%s>" % tag)
    return out


def set_minmax(layer, tag, mn, mx):
    pat = re.compile(rb"(<%s>\s*<Min>)[^<]*(</Min>\s*<Max>)[^<]*(</Max>)" % tag.encode())
    out, n = pat.subn(lambda m: m.group(1) + _num(mn).encode() + m.group(2) + _num(mx).encode() + m.group(3), layer, count=1)
    if n != 1:
        raise KeyError("层里没有 Min/Max 标签 <%s>" % tag)
    return out


def set_axis_minmax(layer, tag, axis, mn, mx):
    """`<StartSizeRange><X><Min>..</Min><Max>..</Max></X>...` 这种三轴范围里改一轴。"""
    pat = re.compile(rb"(<%s>.*?<%s>\s*<Min>)[^<]*(</Min>\s*<Max>)[^<]*(</Max>\s*</%s>)" % (tag.encode(), axis.encode(), axis.encode()), re.S)
    out, n = pat.subn(lambda m: m.group(1) + _num(mn).encode() + m.group(2) + _num(mx).encode() + m.group(3), layer, count=1)
    if n != 1:
        raise KeyError("层里没有 <%s><%s>" % (tag, axis))
    return out


def set_size(layer, x_mn, x_mx, y_mn=None, y_mx=None):
    if y_mn is None:
        y_mn, y_mx = x_mn, x_mx
    layer = set_axis_minmax(layer, "StartSizeRange", "X", x_mn, x_mx)
    return set_axis_minmax(layer, "StartSizeRange", "Y", y_mn, y_mx)


def set_xyz(layer, tag, x, y, z=0):
    pat = re.compile(rb"(<%s>\s*<X>)[^<]*(</X>\s*<Y>)[^<]*(</Y>\s*<Z>)[^<]*(</Z>)" % tag.encode())
    out, n = pat.subn(lambda m: m.group(1) + _num(x).encode() + m.group(2) + _num(y).encode() + m.group(3) + _num(z).encode() + m.group(4), layer, count=1)
    if n != 1:
        raise KeyError("层里没有 XYZ 标签 <%s>" % tag)
    return out


def set_colors(layer, stops):
    """重写 `<ColorScales>`：stops = [('C8FF8080', 0), ('50FF2020', 1)]，顺带 UseColorScale=true。"""
    body = b"".join(
        b"\r\n        <ColorScale>\r\n          <ColorValue>%s</ColorValue>\r\n          <RelativeTime>%s</RelativeTime>\r\n        </ColorScale>"
        % (argb(h).encode(), _num(t).encode()) for h, t in stops)
    block = b"<ColorScales>" + body + b"\r\n      </ColorScales>"
    pat = re.compile(rb"<ColorScales>.*?</ColorScales>|<ColorScales />", re.S)
    out, n = pat.subn(lambda m: block, layer, count=1)
    if n != 1:
        raise KeyError("层里没有 <ColorScales>")
    return set_scalar(out, "UseColorScale", "true")


def set_sizes(layer, stops):
    """重写 `<SizeScales>`：stops = [(0.5, 0), (2, 1)]，顺带 UseSizeScale=true。"""
    body = b"".join(
        b"\r\n        <SizeScale>\r\n          <RelativeSize>%s</RelativeSize>\r\n          <RelativeTime>%s</RelativeTime>\r\n        </SizeScale>"
        % (_num(s).encode(), _num(t).encode()) for s, t in stops)
    block = b"<SizeScales>" + body + b"\r\n      </SizeScales>"
    pat = re.compile(rb"<SizeScales>.*?</SizeScales>|<SizeScales />", re.S)
    out, n = pat.subn(lambda m: block, layer, count=1)
    if n != 1:
        raise KeyError("层里没有 <SizeScales>")
    return set_scalar(out, "UseSizeScale", "true")


def set_texture(layer, rel_backslash):
    return set_scalar(layer, "TextureFileName", rel_backslash)


def set_cycle(layer, spawn, life, regen, delay=None):
    """一层的节奏：发射持续 / 粒子寿命 / 重启周期（秒），可选延迟。"""
    layer = set_minmax(layer, "SpawningTime", *spawn)
    layer = set_minmax(layer, "LifeTime", *life)
    layer = set_minmax(layer, "RegenTime", *regen)
    if delay is not None:
        layer = set_minmax(layer, "DelayTime", *delay)
    return layer


# ---------------------------------------------------------------------------
#  整份文件
# ---------------------------------------------------------------------------

def read_donor(rel):
    p = os.path.join(PACK, rel.replace("/", os.sep))
    with open(p, "rb") as fp:
        return fp.read()


def layers_of(blob):
    return [m.group(0) for m in _LAYER_RE.finditer(blob)]


def rebuild(blob, new_layers):
    """把文件里的层整体替换成 new_layers（保持首尾 / 缩进）。"""
    ms = list(_CRLF_LAYER_RE.finditer(blob))
    if not ms:
        raise ValueError("文件里找不到 <EffLayer>")
    head = blob[:ms[0].start()]
    tail = blob[ms[-1].end():]
    joined = b"".join(b"\r\n    " + L for L in new_layers)
    return head + joined + tail


def edit(blob, per_layer=None, keep=None, add=None):
    """per_layer: {层号: [fn, ...]} 或 {'*': [...]}；keep: 保留的层号列表（顺序即输出顺序）；
    add: 追加的层（bytes 列表）。返回新 bytes。"""
    layers = layers_of(blob)
    if keep is not None:
        layers = [layers[i] for i in keep]
    per_layer = per_layer or {}
    out = []
    for i, L in enumerate(layers):
        for fn in per_layer.get("*", []):
            L = fn(L)
        for fn in per_layer.get(i, []):
            L = fn(L)
        out.append(L)
    if add:
        out.extend(add)
    return rebuild(blob, out)


# ---------------------------------------------------------------------------
#  捐赠表：缺失名 -> (捐赠者, 改法)
# ---------------------------------------------------------------------------

def _colors(a, b):
    return lambda L: set_colors(L, [(a, 0), (b, 1)])


def _white_light(color_a, color_b):
    """彩灯泡：CamelWhiteLight00（白贴图 + Modulate）只换两站颜色。"""
    return ("Maps/Camel/Effect/CamelWhiteLight00.efx", dict(per_layer={"*": [_colors(color_a, color_b)]}))


def _firework(colors_burst, delay):
    """结算烟花 Win_FireWork00（38 层一次性）砍成 8 层循环版：两组爆闪 + 一层碎屑，每 3~4 秒一次。"""
    def burst(L):
        L = set_cycle(L, (0.3, 0.3), (0.25, 0.35), (3.2, 4.0), delay)
        return set_colors(L, [(colors_burst[0], 0), (colors_burst[1], 1)])

    def debris(L):
        return set_cycle(L, (0.5, 0.5), (1, 1.2), (3.2, 4.0), delay)

    return ("Effects/ClearResult/Efx/Win_FireWork00.efx",
            dict(keep=[0, 1, 2, 3, 5, 6, 29, 30], per_layer={0: [burst], 1: [burst], 2: [burst], 3: [debris], 4: [burst], 5: [burst], 6: [debris], 7: [burst]}))


def _glow(texture, size, color_a, color_b, life=(2, 2), offsets=((0, 0),)):
    """`<贴图名>_Glow00`：Quest07_TestTubeGlow_G00（单层常亮呼吸）换贴图 / 尺寸 / 颜色；多个 offsets = 多层。"""
    def one(dx, dy):
        def fn(L):
            L = set_texture(L, texture)
            L = set_size(L, size[0], size[0], size[1], size[1])
            L = set_xyz(L, "StartLocationOffset", dx, dy, 0)
            L = set_cycle(L, life, life, life)
            L = set_scalar(L, "FadeIn", "true")
            L = set_scalar(L, "FadeOut", "true")
            return set_colors(L, [(color_a, 0), (color_b, 0.5), (color_a, 1)])
        return fn

    donor = "Maps/quest07/Effect/Quest07_TestTubeGlow_G00.efx"
    blob = read_donor(donor)
    base = layers_of(blob)[0]
    layers = [one(dx, dy)(base) for dx, dy in offsets]
    return (donor, dict(_prebuilt=layers))


def _wave(donor, life, spawn, colors=None):
    def fn(L):
        L = set_minmax(L, "LifeTime", *life)
        L = set_minmax(L, "SpawningTime", *spawn)
        if colors:
            L = set_colors(L, [(colors[0], 0), (colors[1], 1)])
        return L
    return (donor, dict(per_layer={"*": [fn]}))


#: 缺失名 -> (捐赠者相对 Pack_develop 的路径, edit() 的参数)。顺序 = `--report` 里的顺序。
DONORS = {
    # 夜空
    "Star00": ("Effects/Environment/Efx/Star00.efx", {}),
    "Star01": ("Effects/Environment/Efx/Star01.efx", {}),
    # 月亮光晕（摆放和月亮图 la_08 重合、缩放 2.4~2.7 ⇒ 是光晕不是月亮本体）
    "Moon00": ("Maps/Camel/Effect/CamelLight00.efx", dict(per_layer={"*": [
        lambda L: set_size(L, 110, 110), lambda L: set_cycle(L, (4, 4), (4, 4), (4, 4)),
        _colors("64FFF6C8", "28FFF0B0")]})),
    # 远景柔光
    "Light00": ("Maps/Camel/Effect/CamelLight00.efx", {}),
    "Light01": ("Maps/Camel/Effect/CamelLight01.efx", {}),
    # 四色彩灯泡（白贴图 + Modulate）
    "Light_R01": _white_light("C8FF7070", "50FF2020"),
    "Light_G01": _white_light("C880FF80", "5000C000"),
    "Light_B01": _white_light("C88090FF", "500020C0"),
    "Light_Y01": _white_light("C8FFF080", "50FFC000"),
    "Light_Y00": ("Maps/Camel/Effect/CamelYellowLight00.efx", dict(per_layer={"*": [
        lambda L: set_cycle(L, (3600, 3600), (1.5, 1.5), (1.5, 1.5))]})),
    # 灯带（非等比缩放的大柔光）
    "Light_R00": ("Maps/Camel/Effect/CamelLight04.efx", dict(per_layer={"*": [_colors("50FF6060", "32FF3030")]})),
    # 金流苏彩带上的金闪（167 处，保持 1 层）
    # 实机看原尺寸的白星芒偏大偏白，缩一档、再暖一点
    "GoldSparkle00": ("Maps/Camel/Effect/CamelSpakle01.efx", dict(per_layer={"*": [
        lambda L: set_size(L, 10, 16, 20, 32), _colors("B4FFF0B0", "FFFFC060")]})),
    "GoldSparkle01": ("Maps/Camel/Effect/CamelWhiteSpakle00.efx", dict(per_layer={"*": [_colors("96FFF0B0", "FFFFC060")]})),
    "GoldSparkle02": ("Effects/QuestRecord/Efx/Small_1(Hard).efx", dict(keep=[0, 1], per_layer={
        0: [_colors("64FFF0C0", "32FFD080")], 1: [_colors("FFFFF0C0", "FFFFA000")]})),
    # 金环（F02 绳桥上 5 个）：宠物光环骨架，扩散 + 淡出
    "GoldRing00": ("Effects/Pet/Pet06/Efx/Pet06_Idle00.efx", dict(per_layer={"*": [
        lambda L: set_size(L, 22, 22), lambda L: set_sizes(L, [(0.6, 0), (1.3, 1)]),
        lambda L: set_cycle(L, (3600, 3600), (1.2, 1.2), (1.2, 1.2)),
        _colors("C8FFE080", "00FFC040")]})),
    # 光环（每图两个，背景层）
    "Aura00": ("Maps/fromhell-t/Effect/Fromhell_StoneGlow00.efx", dict(per_layer={
        "*": [lambda L: set_size(L, 260, 260, 160, 160)],
        0: [_colors("96FFB070", "96E08040")], 2: [_colors("96FFB070", "96E08040")],
        1: [_colors("32FFE0A0", "32FFC060")], 3: [_colors("32FFE0A0", "32FFC060")]})),
    # 烟花（背景夜空，循环）
    "FireWork01": _firework(("FFFFF0C0", "FFFF2020"), (0, 0)),
    "FireWork05": _firework(("FFE0FFE0", "FF20C0FF"), (1.3, 1.3)),
    "FireWork06": _firework(("FFFFE0FF", "FFE040FF"), (2.4, 2.4)),
    # 贴图光晕
    "tr_x_23_Glow00": _glow("Canal\\Texture\\Canal_TorchGlow00.dds", (70, 70), "8CFFC070", "C8FFE0A0", (2, 2), offsets=((-100, -34), (100, -34))),
    "tr_x_04_Glow00": ("Maps/Esperan/Effect/LavaStaticGlow00.efx", dict(per_layer={
        "*": [lambda L: set_size(L, 230, 230)], 0: [_colors("C8FF4020", "C8FF4020")]})),
    "tr_x_32_Glow00": _glow("Canal\\Texture\\Canal_TorchGlow00.dds", (60, 60), "8CFFC070", "C8FFE0A0", (2, 2)),
    # 水面
    "Water00": ("Effects/Water/Efx/Water00.efx", dict(per_layer={"*": [_colors("B480C0FF", "5040A0FF")]})),
    "Wave00": ("Maps/sea-/Effect/SeaWave00.efx", {}),
    "Wave01": ("Maps/sea-/Effect/SeaWave01.efx", {}),
    "Wave02": _wave("Maps/sea-/Effect/SeaWave00.efx", (1.0, 1.3), (2, 2), ("96C0E0FF", "64A0D0FF")),
    "Wave03": _wave("Maps/sea-/Effect/SeaWave01.efx", (1.8, 2.4), (4, 4), ("96BEDCF3", "64A0C8F0")),
    # 落水（map.ini 的 FallDownEffectFileName）：污水青 / 泥褐 -> 清水蓝白
    "FestivalWaterDamage00": ("Effects/Camel/Efx/WasteWaterDamage00.efx", dict(per_layer={
        1: [_colors("FF4080C0", "FF80C0FF")], 4: [_colors("FF6080A0", "FF80A0C0")],
        5: [_colors("FF6080A0", "FF80A0C0")], 6: [_colors("FF80C0FF", "FF4080C0")]})),
}


def build_one(name):
    donor, spec = DONORS[name]
    blob = read_donor(donor)
    if "_prebuilt" in spec:
        return rebuild(blob, spec["_prebuilt"])
    return edit(blob, per_layer=spec.get("per_layer"), keep=spec.get("keep"), add=spec.get("add"))


def check(blob, name):
    """自检：cp949 可解、层数 ≥ 1、贴图都在 `Effects/` 下。返回 (层数, 问题列表)。"""
    problems = []
    try:
        text = blob.decode("cp949")
    except UnicodeDecodeError as exc:
        return 0, ["%s: 不是 cp949：%s" % (name, exc)]
    if not text.startswith('<?xml version="1.0" encoding="ks_c_5601-1987"?>'):
        problems.append("%s: XML 声明不对" % name)
    n = text.count("<EffLayer>")
    if n == 0:
        problems.append("%s: 没有一层" % name)
    for tex in re.findall(r"<TextureFileName>([^<]+)</TextureFileName>", text):
        p = os.path.join(EFFECTS_DIR, tex.replace("\\", os.sep))
        if not os.path.isfile(p):
            problems.append("%s: 贴图不存在 %s" % (name, tex))
    return n, problems


def build_all(out_dir):
    """把 28 个 efx 写到 out_dir（`.../Maps/Festival/Effect/`）。返回 [(name, 层数, 字节数)]。"""
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for name in DONORS:
        blob = build_one(name)
        n, problems = check(blob, name)
        if problems:
            raise ValueError("\n".join(problems))
        with open(os.path.join(out_dir, name + ".efx"), "wb") as fp:
            fp.write(blob)
        rows.append((name, n, len(blob)))
    return rows


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "logs", "map_festival_fill", "out", "Effect")
    for name, n, size in build_all(out):
        print("%-24s %2d 层 %6d B  <- %s" % (name, n, size, DONORS[name][0]))
