#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""amftool.py —— `Data/default.amf`（2D 精灵动画表）的解析 / 写出 / 追加（X_Mod · X3）。

    C:\\Python314\\python.exe tools/amftool.py list   [default.amf] [--group Game/Bullet]
    C:\\Python314\\python.exe tools/amftool.py roundtrip [default.amf]     # parse→write 逐字节一致
    C:\\Python314\\python.exe tools/amftool.py add    default.amf 组名 条目名 /Images/Game/x.png 帧号:毫秒[,帧号:毫秒…]

## 为什么要有它

`weapon.ini` 的 `Image=Anim,<条目名>` 指的是这张表里的条目（爆裂 3 的弹体 `CH00_WP1_D3`
就在这里），条目再指向 `Images/Game/*.png` + 同名 `.smf` 帧表。给自定义武器造一颗
金色子弹 = 往表里**追加**一条指向新精灵的条目。仓库里原先没有任何读写这张表的代码。

## 格式（✅ 整份 233792 B 逐字节走通，X_Mod §37）

    "ANIM"  u32 版本(=1)  u32 组数
    组 ×N：  wstr 名[256 B]  wstr 目录[256 B]  u32 条目数
             条目 ×n：wstr 名[256 B]  wstr 路径[512 B]  i32 a  i32 b  i32 帧数  帧数×(i32 帧号, i32 毫秒)

`wstr[固定长]` = UTF-16LE + 零填充（原文件里名字后面全是零，长度固定）。
`a` / `b` 全表恒 0，语义未查，原样保留。
"""
from __future__ import annotations

import argparse
import collections
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MAGIC = b"ANIM"
NAME_BYTES = 256
PATH_BYTES = 512


class AmfError(Exception):
    pass


Entry = collections.namedtuple("Entry", "name path a b frames")   # frames: [(帧号, 毫秒), …]
Group = collections.namedtuple("Group", "name folder entries")


def _wstr(raw):
    return raw.decode("utf-16le").split("\0", 1)[0]


def _fixed(text, nbytes):
    data = text.encode("utf-16le")
    if len(data) + 2 > nbytes:
        raise AmfError("字符串太长（%d 字节 > %d）：%r" % (len(data), nbytes - 2, text))
    return data + b"\0" * (nbytes - len(data))


def parse(blob):
    """字节 → `(版本, [Group, …])`。"""
    if blob[:4] != MAGIC:
        raise AmfError("不是 amf（magic 不对）")
    version, ngroups = struct.unpack_from("<II", blob, 4)
    off = 12
    groups = []
    for _ in range(ngroups):
        name = _wstr(blob[off:off + NAME_BYTES])
        folder = _wstr(blob[off + NAME_BYTES:off + 2 * NAME_BYTES])
        count = struct.unpack_from("<I", blob, off + 2 * NAME_BYTES)[0]
        off += 2 * NAME_BYTES + 4
        entries = []
        for _ in range(count):
            ename = _wstr(blob[off:off + NAME_BYTES])
            epath = _wstr(blob[off + NAME_BYTES:off + NAME_BYTES + PATH_BYTES])
            a, b, nframes = struct.unpack_from("<iii", blob, off + NAME_BYTES + PATH_BYTES)
            off += NAME_BYTES + PATH_BYTES + 12
            frames = []
            for _ in range(nframes):
                frames.append(struct.unpack_from("<ii", blob, off))
                off += 8
            entries.append(Entry(ename, epath, a, b, frames))
        groups.append(Group(name, folder, entries))
    if off != len(blob):
        raise AmfError("解析停在 %d，文件长 %d —— 格式和预期不符" % (off, len(blob)))
    return version, groups


def write(version, groups):
    out = [MAGIC, struct.pack("<II", version, len(groups))]
    for group in groups:
        out.append(_fixed(group.name, NAME_BYTES))
        out.append(_fixed(group.folder, NAME_BYTES))
        out.append(struct.pack("<I", len(group.entries)))
        for entry in group.entries:
            out.append(_fixed(entry.name, NAME_BYTES))
            out.append(_fixed(entry.path, PATH_BYTES))
            out.append(struct.pack("<iii", entry.a, entry.b, len(entry.frames)))
            for frame, ms in entry.frames:
                out.append(struct.pack("<ii", frame, ms))
    return b"".join(out)


def load(path):
    with open(path, "rb") as fp:
        return parse(fp.read())


def save(path, version, groups):
    with open(path, "wb") as fp:
        fp.write(write(version, groups))


def find(groups, group_name, entry_name):
    """按名找条目（名字大小写不敏感，和客户端的 ini 口径一致）。找不到 `None`。"""
    for group in groups:
        if group.name.lower() != group_name.lower():
            continue
        for entry in group.entries:
            if entry.name.lower() == entry_name.lower():
                return entry
    return None


def upsert(groups, group_name, entry):
    """追加或替换一条（同名就替换）。返回 `(新的 groups, 是不是新增)`。"""
    out = []
    done = False
    added = False
    for group in groups:
        if group.name.lower() != group_name.lower():
            out.append(group)
            continue
        entries = []
        for old in group.entries:
            if old.name.lower() == entry.name.lower():
                entries.append(entry)
                done = True
            else:
                entries.append(old)
        if not done:
            entries.append(entry)
            done = added = True
        out.append(Group(group.name, group.folder, entries))
    if not done:
        raise AmfError("没有这个组：%s" % group_name)
    return out, added


def default_path():
    server_dir = os.path.join(ROOT, "server")
    if server_dir not in sys.path:
        sys.path.append(server_dir)
    import config
    return os.path.join(ROOT, "game_patched", config.PACK_DEVELOP_DIR, "Data", "default.amf")


def main(argv=None):
    ap = argparse.ArgumentParser(description="default.amf 解析 / 写出 / 追加")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("amf", nargs="?"); p.add_argument("--group")
    p = sub.add_parser("roundtrip"); p.add_argument("amf", nargs="?")
    p = sub.add_parser("add")
    p.add_argument("amf"); p.add_argument("group"); p.add_argument("name"); p.add_argument("path")
    p.add_argument("frames", help="帧号:毫秒，逗号分隔，如 0:50,1:50")
    args = ap.parse_args(argv)

    path = getattr(args, "amf", None) or default_path()
    if args.cmd == "roundtrip":
        blob = open(path, "rb").read()
        version, groups = parse(blob)
        again = write(version, groups)
        print("%s: %d 组 %d 条，写回%s" % (path, len(groups), sum(len(g.entries) for g in groups),
                                        "逐字节一致" if again == blob else "**不一致**"))
        return 0 if again == blob else 1
    if args.cmd == "list":
        _version, groups = load(path)
        for group in groups:
            if args.group and group.name.lower() != args.group.lower():
                continue
            print("[%s] (%s) %d 条" % (group.name, group.folder, len(group.entries)))
            for entry in group.entries:
                print("  %-24s %-48s %s" % (entry.name, entry.path,
                                            " ".join("%d:%d" % f for f in entry.frames)))
        return 0
    if args.cmd == "add":
        frames = []
        for token in args.frames.split(","):
            frame, ms = token.split(":")
            frames.append((int(frame), int(ms)))
        version, groups = load(path)
        groups, added = upsert(groups, args.group, Entry(args.name, args.path, 0, 0, frames))
        save(path, version, groups)
        print("%s：%s %s" % (path, "新增" if added else "替换", args.name))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
