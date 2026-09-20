#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 `server/config.py` 生成 C 端的资源目录名头：`hook/pack.h`。

和 `gen_ports_h.py` 是同一个模式（同一个理由）：目录名只有一个源，
C 走生成的头文件，PowerShell 走 `python server/config.py --pack-dirs`，Python 直接 import。
`hook/build.bat` 每次编译前都会跑一遍；生成结果**提交进仓库**，没装 Python 也能编。
`test/test_packdirs.py` 盯着几边有没有分叉。

用法：

    python tools/gen_pack_h.py            # 生成 / 更新 hook/pack.h
    python tools/gen_pack_h.py --check    # 只检查是否最新（不写文件）
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# ★ 便携运行时带 python314._pth，不会自动把脚本所在目录放进 sys.path，tools/ 也要显式加。
for _p in (os.path.join(ROOT, "server"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as server_config          # noqa: E402
from gen_ports_h import write_if_changed   # noqa: E402  同一套「有变才落盘、CRLF」

HEADER_PATH = os.path.join(ROOT, "hook", "pack.h")

NOTES = {
    "PACK_LEGACY_DIR": "原版客户端写死的资源目录（镜像 0x6936b0 L\"Pack/\"），也是密钥前缀，永远不改",
    "PACK_DEVELOP_DIR": "明文资源树（hook 不用，只为三边同源）",
    "PACK_PUBLISH_DIR": "加密卷所在目录：hook 把 Pack\\*.pkn 的打开重定向到这里",
}


def render():
    """头文件的完整内容（行尾统一 LF，交给写入方决定落盘形态）。"""
    table = server_config.pack_dir_table()
    width = max(len(name) for name in table) + 2
    lines = [
        "/* ========================================================================",
        " *  pack.h —— 客户端资源目录名。",
        " *",
        " *  ★★ 【自动生成，不要手改】",
        " *      源头是 server/config.py，生成器是 tools/gen_pack_h.py。",
        " *      要改目录名只改 server/config.py 一处，重新编译即可",
        " *（build.bat 会自己重新生成）。",
        " *",
        " *  每个名字都给窄串和宽串两个宏；Python 那边有同名常量，两边分叉会被",
        " *  test/test_packdirs.py 当场抓住。",
        " * ====================================================================== */",
        "#ifndef POPSHOT_PACK_H",
        "#define POPSHOT_PACK_H",
        "",
    ]
    for name, value in table.items():
        note = NOTES.get(name, "")
        for suffix, prefix in (("", ""), ("_W", "L")):
            macro = "POPSHOT_%s%s" % (name, suffix)
            pad = " " * (width - len(name) - len(suffix))
            line = "#define %s%s %s\"%s\"" % (macro, pad, prefix, value)
            if note and suffix == "":
                line += "   /* %s */" % note
            lines.append(line)
    lines += ["", "#endif /* POPSHOT_PACK_H */", ""]
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    want = render()
    if "--check" in argv:
        try:
            with open(HEADER_PATH, "r", encoding="utf-8", newline="") as f:
                have = f.read().replace("\r\n", "\n")
        except OSError:
            have = None
        if have != want:
            print("[pack.h] !! %s 和 server/config.py 对不上，请跑一次 python tools/gen_pack_h.py"
                  % HEADER_PATH, file=sys.stderr)
            return 1
        print("[pack.h] 是最新的")
        return 0
    write_if_changed(HEADER_PATH, want, "已更新", tag="pack.h")
    return 0


if __name__ == "__main__":
    # ★ 和 `gen_ports_h.py` 同一个理由（那边写了完整版）：输出一被捕获，
    #   stdout 就退回系统 ANSI 代码页 —— 英文机上是 cp1252，`是最新的`
    #   这三个字直接 `UnicodeEncodeError`，退出码 1，而头文件其实没问题。
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
