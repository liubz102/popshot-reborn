#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""openweapons.py —— 把**韩版有、中文版没抄**的原版武器补进 `ShopItem-Chn.ini`（X_Mod · X5）。

    python tools\\openweapons.py            # 只看要补什么，不写文件
    python tools\\openweapons.py --apply     # 写回 Pack_develop 的 ShopItem-Chn.ini

## 这是在解决什么

中文版客户端读的物品表是 `Data/ShopItem-Chn.ini`（`Chinese.ini` 里
`Data/ShopItem.ini=Data/ShopItem-Chn.ini` 那条重定向），它比韩版 `ShopItem.ini`
**少 622 条**。少掉的那批里有 **18 把货真价实的 3 级武器** —— 1 号槽和 3 号槽的
`D3 / R3 / F3`（韩版 2432 节 vs 中文版 1879 节，武器差 72 节 = 18 件 + 54 条期限售卖形态）。

**它们的数据和美术在中文版客户端里一个都不缺**：`weapon.ini` **不在重定向表里**
（全世界同一份），18 个小节原样都在；手持网格 `chNNW0<系><档><槽>.msh`、弹体图集、
特效 `.efx`、贴图、音效、HUD 图标（3 帧 = 3 个角色）、商店图标 `무기_<韩文名> <系>3.png`
全部就位。⇒ 中文版**唯一**缺的就是物品表里那 18 组 `[Item-]` / `[Stock-]` 条目，
少了它客户端查不到图标和 `Tag`，服务端这边 `shopdata.exists()` 也判它不存在，
于是管理页物品库里根本没有这一行（用户 2026-09-19 报的「重机枪 爆裂3 看不见」）。

⇒ 按铁律 12「打开原版埋好的优先于自己造一个」：**把韩版那 18 组条目原样抄回来**，
不新编 id、不动 `weapon.ini`、不造任何资源。

## 为什么**不**连 54 条期限售卖形态一起抄

`162xxxx / 262xxxx / 362xxxx`（部位码 62 = 12 + 50）是同一件武器的「期限制售卖形态」，
**只有 `[Stock-]` 没有 `[Item-]`** ⇒ 没有 `Tag` / `PartFlag`、`shopdata` 判 `ownable` 为假，
物品库（只收 `ownable`）和我们自己的商店都收不了它。抄过来是 54 条谁也用不上的死条目。

## 幂等

「要补什么」是**每次现算的**：中文版里已经有这条 id 就不算缺口，第二遍跑下来是空的、
一个字节都不写。真要重写时也先把这些 id 从文件里**整段摘掉**再插回去，不会插重。

插入位置是 自定义武器生成器的自定义武器块（`[Item-1920001]` 起）**之前** —— 两个工具因此可以
任意顺序重跑：`custom-weapon/build.py` 的 `build_shop_ini()` 是「砍掉 `[Item-1920001]` 之后的
一切再追加」，本工具的块在那一刀**上游**，不会被它带走。

## 跑完还要做什么

1. `tools\\update-gamedata.bat` —— 重抽 `server/shop_items.json` + 重拼 `server/web/itemicons.*`；
2. `tools\\build-pack.bat` —— 重打资源卷（客户端读的是 `Pack_publish`，不是明文树）；
3. 已经开过服的机器：控制通道 `shop-backfill apply`（或 `tools/gen_listing.py --apply --all`）
   把这 18 件补进已有的 `server/data/items.json` / `shop.json` —— `ensure_files` 对**已存在**的
   配置一律不覆盖（D7 / 铁律 11），新物品只能靠补齐进去。

铁律：本模块只用标准库（`test_openweapons` 要在两套运行时里跑）。
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 自定义武器块的第一节。本工具的块插在它**之前**（见文件头「幂等」）。
CUSTOM_WEAPON_ANCHOR = "[Item-1920001]"

#: 物品 id 的部位码 12 = 武器（`tools/shopdata.PART_KIND`）。
WEAPON_PART = 12

#: 一行 `[Item-1120013]`（读原文时用）。
_SECTION_HEAD = re.compile(r"^\[(Item|Stock)-(\d+)\]$", re.IGNORECASE)
#: `read_ini` 给出来的节名不带方括号（`Item-1120013`）。
_SECTION_NAME = re.compile(r"^(Item|Stock)-(\d+)$", re.IGNORECASE)


def _load_sibling(name):
    """按路径加载 `tools/` 下的同伴模块 —— `server/` 下有同名的另一份（见 shopdata.py 的注释）。"""
    path = os.path.join(HERE, name + ".py")
    spec = importlib.util.spec_from_file_location("openweapons_" + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def develop_root():
    server_dir = os.path.join(ROOT, "server")
    if server_dir not in sys.path:
        sys.path.append(server_dir)
    import config
    return os.path.join(ROOT, "game_patched", config.PACK_DEVELOP_DIR)


# ---------------------------------------------------------------------------
# ini 读写（UTF-16LE + BOM + 裸 LF —— 三份 ShopItem 都是这个形状）
# ---------------------------------------------------------------------------

def read_text(path):
    raw = open(path, "rb").read()
    if raw[:2] != b"\xff\xfe":
        raise SystemExit("%s 不是 UTF-16LE 带 BOM，不敢动" % path)
    return raw[2:].decode("utf-16le")


def encode_text(text):
    return b"\xff\xfe" + text.encode("utf-16le")


def split_sections(text):
    """`[{"kind","id","head","body"}]` —— `body` 含节内全部原文（连尾部空行）。

    只认 `[Item-<数字>]` / `[Stock-<数字>]`；别的节名原样当成前一节的一部分带走，
    所以重新拼起来必定逐字节等于原文。
    """
    out = []
    lines = text.split("\n")
    current = None
    for line in lines:
        m = _SECTION_HEAD.match(line.strip())
        if m:
            if current is not None:
                out.append(current)
            current = {"kind": m.group(1).lower(), "id": int(m.group(2)),
                       "head": line, "body": []}
        elif current is not None:
            current["body"].append(line)
        else:
            out.append({"kind": None, "id": None, "head": line, "body": []})
    if current is not None:
        out.append(current)
    return out


def join_sections(sections):
    parts = []
    for s in sections:
        parts.append(s["head"])
        parts.extend(s["body"])
    return "\n".join(parts)


def section_text(sections, kind, item_id):
    """一节的原文（含节头和尾部空行）；没有就 `None`。"""
    for s in sections:
        if s["kind"] == kind and s["id"] == item_id:
            return "\n".join([s["head"]] + s["body"])
    return None


# ---------------------------------------------------------------------------
# 「该补哪些」——**算出来的，不是手抄的一张表**
# ---------------------------------------------------------------------------

Candidate = collections.namedtuple("Candidate", "item_id ammo_id section name_kr icon missing")


def find_candidates(root=None):
    """韩版有 `[Item-]`、中文版没有的**武器**，按 id 排序。

    判据四条，缺一不可 —— 这样将来原版表里再出现别的漏网武器，跑一遍就自己冒出来：

    1. 部位码 12（武器）且韩版有 `[Item-]` 节（**能进背包**，纯 `[Stock-]` 的期限形态不算）；
    2. 中文版 `ShopItem-Chn.ini` 里没有这条 id；
    3. `Tag` 指得到 `weapon.ini` 里一个真实小节（不然客户端拿不到武器数值）；
    4. 商店图标文件在明文资源树里真的存在（不然仓库里是个空格子）。

    `missing` 是第 3、4 条里没过的说明，非空 = 这条**不能**补，只报告不写。
    """
    root = root or develop_root()
    data = os.path.join(root, "Data")
    weapondata = _load_sibling("weapondata")
    shopdata = _load_sibling("shopdata")

    kr = weapondata.read_ini(os.path.join(data, "ShopItem.ini"))
    cn = weapondata.read_ini(os.path.join(data, "ShopItem-Chn.ini"))
    weapons = weapondata.read_ini(os.path.join(data, "weapon.ini"))
    by_ammo = {}
    for name, fields in weapons.items():
        ammo = shopdata.tag_ammo_id(fields.get("Id"))
        if ammo is not None:
            by_ammo.setdefault(ammo, name)
    # 中文版已有的 id（节名大小写在原版里并不统一，按小写比）
    cn_ids = set()
    for section in cn:
        m = _SECTION_NAME.match(section.strip())
        if m:
            cn_ids.add(int(m.group(2)))

    out = []
    for section, fields in kr.items():
        m = _SECTION_NAME.match(section.strip())
        if not m or m.group(1).lower() != "item":
            continue
        item_id = int(m.group(2))
        text = str(item_id)
        if len(text) != 7 or int(text[1:3]) != WEAPON_PART:
            continue
        if item_id in cn_ids:
            continue
        ammo = shopdata.tag_ammo_id(fields.get("Tag"))
        icon = (fields.get("Image") or "").replace("\\", "/")
        why = []
        if ammo is None or ammo not in by_ammo:
            why.append("Tag=%s 在 weapon.ini 里找不到对应小节" % fields.get("Tag"))
        if not icon:
            why.append("没有 Image=")
        elif not os.path.exists(os.path.join(root, *icon.split("/"))):
            why.append("商店图标缺失：" + icon)
        _stem, name_kr = shopdata.icon_name(icon)
        out.append(Candidate(item_id, ammo, by_ammo.get(ammo), name_kr, icon, why))
    out.sort(key=lambda c: c.item_id)
    return out


# ---------------------------------------------------------------------------
# 写回
# ---------------------------------------------------------------------------

def rebuild(root=None, candidates=None):
    """算出**新的** `ShopItem-Chn.ini` 全文和实际补进去的那几条。

    返回 `(路径, 新全文, [Candidate])`。不写盘 —— 写不写由调用方决定。
    """
    root = root or develop_root()
    path = os.path.join(root, "Data", "ShopItem-Chn.ini")
    kr_sections = split_sections(read_text(os.path.join(root, "Data", "ShopItem.ini")))
    cn_text = read_text(path)

    if candidates is None:
        candidates = find_candidates(root)
    usable = [c for c in candidates if not c.missing]
    ids = set(c.item_id for c in usable)

    # ① 先整段摘掉自己上次插的（幂等；顺便容忍有人手工挪过位置）
    kept = [s for s in split_sections(cn_text)
            if not (s["id"] in ids and s["kind"] in ("item", "stock"))]
    text = join_sections(kept)

    # ② 韩版原文逐节抄过来。★ **`[Item-]` 必须排在 `[Stock-]` 前面** ——
    #   原版两份表里 841 / 753 组都是这个顺序，`tools/shopdata.py` 也按它取字段
    #   （`Tag` / `PartFlag` 只写在 `[Item-]` 里）。反过来写会丢弹药 id 和装备槽。
    block = []
    for c in usable:
        for kind in ("item", "stock"):
            piece = section_text(kr_sections, kind, c.item_id)
            if piece is not None:
                block.append(piece.rstrip("\n"))
    if not block:
        return path, text, []
    chunk = "\n\n".join(block) + "\n"

    # ③ 插在 自定义武器生成器的自定义武器块之前（没有那个块就追加到末尾）
    anchor = text.find(CUSTOM_WEAPON_ANCHOR)
    if anchor >= 0:
        head, tail = text[:anchor], text[anchor:]
        if not head.endswith("\n\n"):
            head = head.rstrip("\n") + "\n\n"
        text = head + chunk + "\n" + tail
    else:
        text = text.rstrip("\n") + "\n\n" + chunk
    return path, text, usable


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把韩版有、中文版没抄的原版武器补进 ShopItem-Chn.ini")
    ap.add_argument("--apply", action="store_true", help="真的写回文件（默认只看）")
    ap.add_argument("--pack", help="明文资源树（默认 game_patched/Pack_develop）")
    args = ap.parse_args(argv)

    root = args.pack or develop_root()
    candidates = find_candidates(root)
    if not candidates:
        print("中文版物品表里一件原版武器都不缺 —— 没有要补的。")
        return 0

    usable = [c for c in candidates if not c.missing]
    blocked = [c for c in candidates if c.missing]
    print("韩版有、中文版没有的武器条目：%d 件（可补 %d，资源不全 %d）"
          % (len(candidates), len(usable), len(blocked)))
    for c in usable:
        print("  %-8d %-16s 弹药 %-9s weapon.ini[%s]" % (c.item_id, c.name_kr, c.ammo_id, c.section))
    for c in blocked:
        print("  %-8d %-16s ✗ %s" % (c.item_id, c.name_kr, "；".join(c.missing)))

    path, text, added = rebuild(root, candidates)
    data = encode_text(text)
    if data == open(path, "rb").read():
        print("\n%s 已经是最新的，没动。" % os.path.basename(path))
        return 0
    if not args.apply:
        print("\n（没写）加 --apply 才会写回 %s" % path)
        return 0
    with open(path, "wb") as fp:
        fp.write(data)
    print("\n已写回 %s（补了 %d 件，%d 组 [Item-]/[Stock-]）"
          % (path, len(added), len(added) * 2))
    print("接着要做：tools\\update-gamedata.bat → tools\\build-pack.bat；"
          "已开过服的机器再跑一次 `shop-backfill apply`。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
