#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""维护 `server/manifest-hook.json` —— 每个版本的 `bshook.dll` 应有的 SHA-256。

## 为什么要它

客户端握手时上报的那 4 个字节里，除了版本号还折进了**它自己那份
`bshook.dll` 的 SHA-256**（布局见 `server/versioning.py` 的 `WIRE_V2_*`）。
服务端拿这张表算出「这个版本应有的校验位」来比对：

* 对得上 -> 正版，放行；
* 对不上 / 压根没带校验位 / **表里根本没有这个版本** -> 一律按「版本过旧」
  拒绝，客户端自己拉起更新器换回正版那份。

起因：以前上报的数字**完全来自包根 `BUILD.ver` 这个明文 JSON**，把版本号
改大就绕过了版本门禁，连重编都不用 —— 倒卖者可以一直卖不带反倒卖公告的
旧客户端。

★★ **所以这张表必须始终跟得上实际发布的二进制**：发版人保证服务端先于
客户端更新，于是「表里没有」只可能是手改出来的版本号。唯一的安全阀是
`versioning.hook_check_disabled_reason()` —— 服务端**自己的版本**都不在表里
时整项校验自动关掉（否则被拒的客户端会去下载服务端那个版本，下完还是不在
表里，全服再也进不来）。

⚠ 客户端自校验**永远能被绕过**（把算 hash 那段 patch 掉即可），而且期望值
就明写在这个随包发出去的文件里。它挡的是「改个文本文件」，不是「会调试器」。

## 为什么放在 `server/` 里

两个打包脚本本来就**整个 `server/` 递归拷**（铁律 8：客户端包和服务端包共用
同一份服务端代码），放这儿零打包改动就同时进了两个包。

## 累积 + 幂等（同 `update_manifest.py`）

**全部历史版本都要留着** —— ★ 「表里没有就拒」之后这条更要紧了：删掉一条
历史条目 = 那一版的老玩家全部被强制更新。删之前先想清楚是不是真想这样。

同一个版本再打一次包时**原位刷新**，不新增条目：开发期同一个版本号会反复
重打包，母本必须是「一条命令重跑不坏」的状态。
⚠ 反过来说：**同一个版本号下重编过 hook，就等于作废了已经发出去的那一批**
（它们的 sha256 对不上了，会被强制更新）。重编就顺手抬版本号。

## 用法

    python tools/gen_hook_manifest.py                  # 版本号取 build-ver.config
    python tools/gen_hook_manifest.py --version V0.3.2
    python tools/gen_hook_manifest.py --check          # 只检查不写
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import versioning                                              # noqa: E402

#: 母本 = 服务端真正读的那一份，没有「另一边」。
MANIFEST_PATH = os.path.join(ROOT, "server", versioning.HOOK_MANIFEST_FILENAME)
DEFAULT_DLL = os.path.join(ROOT, "hook", "bin", versioning.HOOK_BINARY_NAME)
#: 版本号的唯一源（打包脚本读的也是它）。
VER_CONFIG = os.path.join(HERE, "build-ver.config")


def default_version():
    """`--version` 没给时取 `tools/build-ver.config`。

    ★ `hook/build.bat` 每次编译后都会跑一遍本脚本（不带 `--version`）——
    这样**开发机上重编 hook 之后，清单里那条立刻跟着换**，不然下一次启动
    就会被自己的服务端按「校验位对不上」拒掉。
    """
    version = versioning.read_version_file(VER_CONFIG)
    if version is None:
        raise SystemExit(f"[hookman] !! {VER_CONFIG} 里认不出版本号，"
                         f"请用 --version 明确指定")
    return versioning.format_version(version)

_NOTE = ("每个版本的 hook/bin/bshook.dll 的 SHA-256。服务端用它校验客户端"
         "上报的完整性校验位：对不上、没带、或表里根本没有这个版本，"
         "一律强制更新（见 server/versioning.py 的 verify_client_hook）。"
         "由 tools/gen_hook_manifest.py 维护（hook/build.bat 和打包脚本都会"
         "自动跑），不要手改。")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load(path=MANIFEST_PATH):
    """读母本 -> ``{"format":…, "hooks":[…]}``；不存在就给一张空表。"""
    try:
        with io.open(path, "r", encoding="utf-8-sig", newline="") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return {"format": 1, "note": _NOTE, "hooks": []}
    if not isinstance(obj, dict) or not isinstance(obj.get("hooks"), list):
        return {"format": 1, "note": _NOTE, "hooks": []}
    obj.setdefault("format", 1)
    obj["note"] = _NOTE
    return obj


def upsert(obj, version_text, digest, date=None):
    """同版本原位刷新，新版本前插。返回 ``(obj, 变了没有)``。"""
    version = versioning.parse_version_text(version_text)
    if version is None:
        raise SystemExit(f"[hookman] !! 认不出版本号 {version_text!r}")
    text = versioning.format_version(version)
    date = date or datetime.date.today().isoformat()

    for entry in obj["hooks"]:
        if versioning.parse_version_text(entry.get("version")) == version:
            if entry.get("sha256") == digest:
                return obj, False
            entry["version"] = text
            entry["sha256"] = digest
            entry["date"] = date
            return obj, True
    obj["hooks"].insert(0, {"version": text, "date": date, "sha256": digest})
    return obj, True


def dump(obj, path=MANIFEST_PATH):
    """LF、UTF-8 无 BOM（铁律 3：.json 要能在 Linux 上跑的服务端里读）。"""
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return text


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=None,
                    help="这一版的版本号（V0.3.2 / 0.3.2 都认）；"
                         "不给就读 tools/build-ver.config")
    ap.add_argument("--dll", default=DEFAULT_DLL,
                    help=f"bshook.dll 的路径（默认 {DEFAULT_DLL}）")
    ap.add_argument("--check", action="store_true",
                    help="只检查母本是不是最新的，不写文件")
    args = ap.parse_args(argv)
    version_text = args.version or default_version()

    if not os.path.exists(args.dll):
        raise SystemExit(f"[hookman] !! 找不到 {args.dll}，先跑 hook\\build.bat")
    digest = sha256_file(args.dll)
    obj, changed = upsert(load(), version_text, digest)

    if args.check:
        if changed:
            print(f"[hookman] !! {MANIFEST_PATH} 里 {version_text} 的 sha256 "
                  f"和 {args.dll} 对不上，请跑一次 "
                  f"python tools/gen_hook_manifest.py --version {version_text}",
                  file=sys.stderr)
            return 1
        print(f"[hookman] {version_text} 的条目是最新的（{digest[:16]}…）")
        return 0

    if not changed:
        print(f"[hookman] {version_text} 无变化（{digest[:16]}…）")
        return 0
    dump(obj)
    print(f"[hookman] 已更新 {MANIFEST_PATH}："
          f"{version_text} -> {digest[:16]}…（共 {len(obj['hooks'])} 个版本）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
