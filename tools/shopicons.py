#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shopicons.py —— 把原版物品图标拼成**一张图集**给管理页用（V0.3 合成与商店 M8）。

    python tools\\shopicons.py                        # 默认找 main worktree 的素材
    python tools\\shopicons.py --src <Images\\Shop 路径>
    python tools\\shopicons.py --check                # 只检查，不写产物

## 为什么是图集，不是 664 个小文件

管理页要给每一件东西画图标。可入包物品有 808 件、去重后 **664 个图标**：

| 方案 | 体积 | 请求数 |
|---|---|---|
| 664 个 64px PNG8 | 3.07 MB | 每屏几十~上百 |
| **一张图集 PNG8** | **0.62 MB** | **1** |

图集里几百个小图共用一条 zlib 窗口和一张调色板，压得比单文件小 5 倍，
而且管理页一次请求就拿全。⇒ 走图集。

## 为什么在这里离线做，而不是服务端现场缩图

和 `tools/shopdata.py` / `tools/mapdata.py` 同一个道理：**服务端包里没有
`Pack_decrypt/`**（368 MB 客户端资源，云端根本没有），而且服务端的便携运行时
里没有 Pillow（`server/` 只用标准库）。所以：**这里离线生成，产物进 git、进包，
服务端只负责把 PNG 字节原样吐出去。**

## ★ 本脚本要 Pillow

`C:\\Python314` 里有；`runtime\\python` 里**没有**（那是给服务端跑的便携运行时）。
`tools\\update-shopicons.bat` 会先探测再决定用谁。

## 产物（都进 git、进服务端包）

    server/web/itemicons.png    1536x1792 PNG8，约 0.62 MB
    server/web/itemicons.json   {"format":1,"size":64,"cols":24,
                                 "count":N,"cells":{"<icon 名>": 格号}}

管理页按 `格号` 算 `background-position`：
`x = -(cell % cols) * size`，`y = -(cell // cols) * size`。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 产物格式版本。管理页读到对不上的版本就当没有图标（画问号占位），
#: 而不是按错的行列切出一堆张冠李戴的图。
FORMAT = 1

#: 图集单元格边长。原版图标是 128x128；管理页最大也就画到 48px，
#: 64 足够清晰（HiDPI 下 48px 显示点也够用），再大就是白扔体积。
CELL = 64

#: 每行几格。24 x 64 = 1536px 宽 —— 浏览器纹理上限之内，行数也不至于太多。
COLS = 24

#: 调色板颜色数。255 而不是 256：留一格给全透明像素。
#: 实测平均色差 8.3/255 ≈ 3%，64px 上看不出来。
PALETTE_COLORS = 255


class IconError(Exception):
    """素材找不到 / 读不了。让 main 转成一句人话，不要甩栈。"""


def _say(text):
    """打印一行。★ 图标名是韩文、提示里有 `⚠`，而这台机器的控制台是 CP936
    —— 直接 `print()` 会 `UnicodeEncodeError` 把整个脚本带走（产物已经写完了，
    死在最后一句上最冤）。转不出去的字符换成 `?`，**产物不受影响**
    （那是 UTF-8 的 json）。和 `tools/chrprops.py._say` 同一套。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, "replace").decode(encoding, "replace"))


def load_pillow():
    """导入 Pillow；没装就给一句**能照着做**的话，而不是 ImportError 栈。"""
    try:
        from PIL import Image
    except ImportError:
        raise IconError(
            "这个脚本要 Pillow，当前解释器没装：\n"
            "  %s\n"
            "★ 便携运行时 runtime\\python 里本来就没有（服务端只用标准库）。\n"
            "  请改用开发用的 Python：\n"
            "    C:\\Python314\\python.exe tools\\shopicons.py\n"
            "  或者给它装上：<那个 python> -m pip install pillow"
            % sys.executable)
    return Image


def find_source_dir(explicit=None):
    """找 `Pack_decrypt/Images/Shop`（和 `shopdata.find_data_file` 同一套口径）。"""
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append(os.path.join(ROOT, "Pack_decrypt", "Images", "Shop"))
    candidates.append(os.path.abspath(os.path.join(
        ROOT, "..", "..", "main", "Pack_decrypt", "Images", "Shop")))
    for cand in candidates:
        if os.path.isdir(cand):
            return cand
    raise IconError(
        "找不到原版图标目录 Images\\Shop。试过：\n  %s\n"
        "★ 素材太大没进本工作副本，只在 main worktree 里。用参数指路，例如\n"
        "  tools\\shopicons.py --src "
        "D:\\git\\popshot-reborn\\main\\Pack_decrypt\\Images\\Shop"
        % "\n  ".join(candidates))


def wanted_icons(items_path):
    """`shop_items.json` 里全部 **`ownable`** 物品的图标名（去重 + 排序）。

    ★ 只收 `ownable` 的：`shopcfg._check_item_id()` 也只放行这一批
    （纯期限售卖形态只有货架条目，进不了背包，§11）。管理页里根本选不到别的，
    收进来就是白占体积。

    ★ **排序是产物稳定的前提**：格号由顺序决定，顺序一抖动，
    整张图和整份索引都变，git 里就是一次无意义的大 diff。
    """
    try:
        with open(items_path, "r", encoding="utf-8") as fp:
            table = json.load(fp)
    except (IOError, OSError, ValueError) as exc:
        raise IconError("读不了物品表 %s：%s" % (items_path, exc))
    names = set()
    for raw in table.get("items", {}).values():
        if not raw.get("ownable"):
            continue
        icon = (raw.get("icon") or "").strip()
        if icon:
            names.add(icon)
    return sorted(names)


def index_source(src_dir):
    """`{小写文件名: 真实文件名}`。

    ★ 大小写要兜住：物品表里写的是 `ch00B0001`，磁盘上有 `ch00B0001.png`
    也有 `CH00B0023.png` —— 原版自己就不一致，Windows 不在乎，
    但产物要能在 Linux 上再生成一次，所以这里自己做不敏感匹配。
    """
    try:
        entries = os.listdir(src_dir)
    except OSError as exc:
        raise IconError("列不了 %s：%s" % (src_dir, exc))
    return dict((name.lower(), name) for name in entries
                if name.lower().endswith(".png"))


def build_atlas(Image, src_dir, names, cell=CELL, cols=COLS):
    """拼图集。返回 `(RGBA 大图, {icon 名: 格号}, [找不到的 icon 名])`。"""
    by_lower = index_source(src_dir)
    placed = collections.OrderedDict()
    missing = []
    for name in names:
        if (name + ".png").lower() in by_lower:
            placed[name] = len(placed)
        else:
            missing.append(name)

    rows = max(1, -(-len(placed) // cols))          # 向上取整
    sheet = Image.new("RGBA", (cols * cell, rows * cell), (0, 0, 0, 0))
    for name, cell_no in placed.items():
        path = os.path.join(src_dir, by_lower[(name + ".png").lower()])
        with Image.open(path) as raw:
            icon = raw.convert("RGBA").resize((cell, cell), Image.LANCZOS)
        sheet.paste(icon, ((cell_no % cols) * cell, (cell_no // cols) * cell))
    return sheet, placed, missing


def write_outputs(Image, sheet, placed, png_path, json_path,
                  cell=CELL, cols=COLS):
    """存 PNG8 + 索引。**先写临时文件再 replace**，中途崩了不留半张图。"""
    directory = os.path.dirname(png_path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)

    palette = sheet.quantize(colors=PALETTE_COLORS, method=Image.FASTOCTREE)
    tmp_png = "%s.%d.tmp" % (png_path, os.getpid())
    try:
        palette.save(tmp_png, "PNG", optimize=True)
        os.replace(tmp_png, png_path)
    finally:
        if os.path.exists(tmp_png):
            try:
                os.remove(tmp_png)
            except OSError:
                pass

    index = collections.OrderedDict((
        ("format", FORMAT),
        ("size", cell),
        ("cols", cols),
        ("width", sheet.width),
        ("height", sheet.height),
        ("count", len(placed)),
        ("cells", collections.OrderedDict(placed.items())),
    ))
    # 铁律 3：`.json` 一律 LF 无 BOM（服务端包要在 Linux 上跑）。
    tmp_json = "%s.%d.tmp" % (json_path, os.getpid())
    try:
        with open(tmp_json, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(index, fp, ensure_ascii=False, indent=1)
            fp.write("\n")
        os.replace(tmp_json, json_path)
    finally:
        if os.path.exists(tmp_json):
            try:
                os.remove(tmp_json)
            except OSError:
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="把原版物品图标拼成一张图集")
    ap.add_argument("--src", help="原版 Images\\Shop 目录")
    ap.add_argument("--items", help="物品表（默认 server\\shop_items.json）")
    ap.add_argument("--out-png", help="默认 server\\web\\itemicons.png")
    ap.add_argument("--out-json", help="默认 server\\web\\itemicons.json")
    ap.add_argument("--check", action="store_true",
                    help="只检查素材齐不齐，不写产物")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        Image = load_pillow()
        src_dir = find_source_dir(args.src)
        items_path = args.items or os.path.join(ROOT, "server",
                                                "shop_items.json")
        names = wanted_icons(items_path)
        if not names:
            raise IconError("%s 里一个 ownable 物品都没有 —— 先跑"
                            " tools\\update-shopdata.bat" % items_path)
        sheet, placed, missing = build_atlas(Image, src_dir, names)
    except IconError as exc:
        raise SystemExit("[x] %s" % exc)

    png_path = args.out_png or os.path.join(ROOT, "server", "web",
                                            "itemicons.png")
    json_path = args.out_json or os.path.join(ROOT, "server", "web",
                                              "itemicons.json")

    if not args.check:
        write_outputs(Image, sheet, placed, png_path, json_path)

    if not args.quiet:
        _say("素材目录：%s" % src_dir)
        _say("图标：%d 个（物品表要 %d 个）" % (len(placed), len(names)))
        if missing:
            # ★ 只警告不报错：中文版砍掉过一批东西，个别图标缺了是正常的，
            #   管理页对没有格号的物品画问号占位。
            _say("⚠ 素材里找不到 %d 个图标，管理页会画占位：" % len(missing))
            for name in missing[:10]:
                _say("    %s" % name)
            if len(missing) > 10:
                _say("    …… 还有 %d 个" % (len(missing) - 10))
        if args.check:
            _say("（--check：没有写产物）")
        else:
            size = os.path.getsize(png_path)
            _say("图集：%s  %dx%d  %.2f MB"
                  % (png_path, sheet.width, sheet.height, size / 1048576.0))
            _say("索引：%s" % json_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
