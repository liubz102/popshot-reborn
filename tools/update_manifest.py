#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""update_manifest.py —— 打包时生成 / 更新自动更新的 manifest.json

**manifest.json 是什么**：挂在每个 GitHub Release 上的发版清单，客户端
更新器（updater/src/manifest.c）从 ``releases/latest/download/manifest.json``
这个固定 URL 取它，按里面的 sha256/url 下载对应版本的客户端整包。
全部历史版本都留在列表里 —— 更新器优先装「服务器点名要的版本」（成对
发布，D079），不是无脑最新，所以旧版本的条目不能丢。

**幂等是硬要求**：开发期同一个版本号会反复重打包实验。同一个版本再来
一次时**原位替换**（刷新 url/size/sha256/date，保留手写的 notes），只有
新版本才前插 —— 母本 tools/update-manifest.json 始终是「一条命令重跑
不坏」的状态。

**母本与副本**：``tools/update-manifest.json``（进 git，累积母本）由本
脚本维护；同内容再写一份到 ``dist/manifest.json`` 供发版人直接上传到
GitHub Release。

用法（由 tools/build-portable.ps1 在 -Zip 打完包后调用）::

    python tools/update_manifest.py --version 0.2.7 \
        --zip dist/PopShot-portable-win64_V0-2-7.zip

也可以单独重跑（比如只想重建 dist 副本）。URL 规则：
tag 保留点号（``V0.2.7``），文件名点转横杠（``V0-2-7``）—— 和仓库
Release 的实际命名一致。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

#: 与 updater/src/main.c 的 MANIFEST_URL / RELEASES_PAGE 同源。
REPO_URL = "https://github.com/liubz102/popshot-reborn"

#: 客户端整包的文件名前缀（dist 里的成果物名，打包脚本定的）。
PACKAGE_STEM = "PopShot-portable-win64"

FORMAT_VERSION = 1


def normalize_version(version_text):
    """"0.2.7" / "V0.2.7" -> ("V0.2.7", "V0-2-7")；认不出抛 ValueError。"""
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
    import versioning
    parts = versioning.parse_version_text(version_text)
    if parts is None:
        raise ValueError(f"版本号认不出：{version_text!r}（要形如 0.2.7）")
    return ("V" + ".".join(str(p) for p in parts),
            "V" + "-".join(str(p) for p in parts))


def release_url(tag, filename):
    """GitHub Release 资产的直链。tag 带点、文件名点转横杠。"""
    return f"{REPO_URL}/releases/download/{tag}/{filename}"


def file_sha256(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def merge_manifest(existing, version_text, url, size, sha256,
                   date=None, repo=REPO_URL):
    """把一个版本条目并进 manifest（幂等）。

    * existing：已有的 manifest dict（None = 从零开始）
    * 已有同版本 -> **原位替换**，保留旧条目里的 notes（手写的发版说明
      比时间戳金贵）；新版本 -> 前插到最前
    * 返回新的 dict（不改动传入对象）
    """
    tag, _suffix = normalize_version(version_text)
    entry = {
        "version": tag,
        "date": date or datetime.date.today().isoformat(),
        "url": url,
        "size": int(size),
        "sha256": sha256,
    }
    if not existing or not isinstance(existing.get("releases"), list):
        existing = None
    if existing is None:
        return {"format": FORMAT_VERSION, "repo": repo, "releases": [entry]}

    releases = list(existing["releases"])
    kept_notes = None
    for index, old in enumerate(releases):
        if isinstance(old, dict) and old.get("version", "").lstrip("vV") \
                == tag.lstrip("vV"):
            kept_notes = old.get("notes")
            releases[index] = entry
            break
    else:
        releases.insert(0, entry)
    if kept_notes:
        for index, item in enumerate(releases):
            if item is entry:
                releases[index] = dict(entry, notes=kept_notes)
                break
    out = dict(existing)
    out["format"] = FORMAT_VERSION
    out["repo"] = repo
    out["releases"] = releases
    return out


def write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="生成/更新自动更新 manifest（同版本原位替换，幂等）")
    parser.add_argument("--version", required=True,
                        help="本批版本号，如 0.2.7")
    parser.add_argument("--zip", required=True,
                        help="刚打出来的客户端整包 zip 路径（算 sha256/size）")
    parser.add_argument("--root",
                        default=os.path.dirname(os.path.dirname(
                            os.path.abspath(__file__))),
                        help="仓库根（默认：本脚本的上一级）")
    args = parser.parse_args(argv)

    tag, suffix = normalize_version(args.version)
    zip_path = os.path.abspath(args.zip)
    if not os.path.isfile(zip_path):
        raise SystemExit(f"找不到 zip：{zip_path}")
    filename = os.path.basename(zip_path)
    if filename != f"{PACKAGE_STEM}_{suffix}.zip":
        print(f"⚠ zip 名字 {filename!r} 和版本号对不上"
              f"（预期 {PACKAGE_STEM}_{suffix}.zip），URL 仍按预期名字生成")
    url = release_url(tag, f"{PACKAGE_STEM}_{suffix}.zip")

    master = os.path.join(args.root, "tools", "update-manifest.json")
    existing = None
    if os.path.isfile(master):
        with open(master, "r", encoding="utf-8-sig") as f:
            try:
                existing = json.load(f)
            except ValueError:
                print(f"⚠ 母本 {master} 不是合法 JSON，按从零开始重建")
                existing = None

    merged = merge_manifest(existing, args.version, url,
                            size=os.path.getsize(zip_path),
                            sha256=file_sha256(zip_path))
    write_json(master, merged)
    dist_copy = os.path.join(args.root, "dist", "manifest.json")
    try:
        os.makedirs(os.path.dirname(dist_copy), exist_ok=True)
        write_json(dist_copy, merged)
    except OSError as error:
        # dist 不在 / 写不了（比如只重跑本脚本）：母本已更新，别当失败。
        print(f"⚠ dist 副本没写成（{error}）—— 母本 {master} 已更新")
    print(f"√ manifest 已更新：{master}")
    print(f"  {tag}  {os.path.getsize(zip_path)} 字节  {url}")
    if os.path.isfile(dist_copy):
        print(f"  dist 副本：{dist_copy}（发 Release 时把它挂上去）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
