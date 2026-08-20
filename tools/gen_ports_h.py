#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 `server/config.py` 生成 `hook/ports.h`。

## 为什么要它

同一个端口号以前在四个地方各写一遍：`server/config.py`、`hook/bshook.c`、
`tools/launch.ps1`、`tools/server-package/serverctl.*`。四种语言，靠环境变量
在运行时对齐。那不只是重复劳动，更是一类**改一半**的故障 —— 改了这边没改
那边，症状通常不是报错，而是「某个功能悄悄不工作」（位置数据投进黑洞那种）。

现在 `server/config.py` 是唯一的源：

    Python      直接 import
    C           本脚本生成的 hook/ports.h
    PowerShell  python server/config.py --ports
    sh          同上

`hook/build.bat` 每次编译前都会跑一遍本脚本，所以「改了 config.py 忘了重新
生成」这件事在正常流程里发生不了；生成结果也**提交进仓库**，这样没装 Python
的人拿到源码也能直接编译。`server/test_ports.py` 会盯着两边有没有分叉。

用法：

    python tools/gen_ports_h.py            # 生成 / 更新 hook/ports.h
    python tools/gen_ports_h.py --check    # 只检查是否最新（不写文件）
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import config as server_config          # noqa: E402

#: 生成物的落点。
HEADER_PATH = os.path.join(ROOT, "hook", "ports.h")

#: 每个端口在头文件里的一行说明。没写的用常量名兜底。
NOTES = {
    "AUTH_PORT": "认证服（客户端写死，V0.1 §24）",
    "GAME_PORT": "游戏服（客户端写死，V0.1 §40）",
    "CONTROL_PORT": "调试控制通道（只绑 127.0.0.1）",
    "PEER_RELAY_PORT": "原版 TCP 中继（服务端侧，D078/D079）",
    "UDP_SYNC_PORT": "位置数据的 UDP 通道（和游戏服 TCP 同号）",
    "RELAY_AUTH_PORT": "本机中继：认证",
    "RELAY_GAME_PORT": "本机中继：游戏",
    "RELAY_PEER_PORT": "本机中继：战斗中继",
    "RELAY_UDP_SYNC_PORT": "本机中继：位置数据（UDP）",
    "GAME_ORIGINAL_UDP_PORT": "原版 UDPBinder 写死要 bind 的口（§153），要改写掉",
    "CLIENT_UDP_PORT": "改写成这个号：游戏【接收】位置数据的 UDP 口",
    "DEFAULT_REGISTER_PORT": "注册页默认端口（server.config 可改，这里只是缺省）",
}


def render():
    """头文件的完整内容（**行尾统一 LF**，交给写入方决定落盘形态）。"""
    lines = [
        "/* ========================================================================",
        " *  ports.h —— 端口号常量。",
        " *",
        " *  ★★ 【自动生成，不要手改】",
        " *      源头是 server/config.py，生成器是 tools/gen_ports_h.py。",
        " *      要改端口只改 server/config.py 一处，重新编译即可"
        "（build.bat 会自己重新生成）。",
        " *",
        " *  这里的每一个号在 Python 那边都有同名常量，两边分叉会被",
        " *  server/test_ports.py 当场抓住。",
        " * ====================================================================== */",
        "#ifndef POPSHOT_PORTS_H",
        "#define POPSHOT_PORTS_H",
        "",
    ]
    table = server_config.port_table()
    width = max(len(name) for name in table)
    for name, port in table.items():
        note = NOTES.get(name, "")
        pad = " " * (width - len(name))
        lines.append(f"#define POPSHOT_{name}{pad} {port}"
                     + (f"   /* {note} */" if note else ""))
    lines += ["", "#endif /* POPSHOT_PORTS_H */", ""]
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    want = render()
    try:
        with open(HEADER_PATH, "r", encoding="utf-8", newline="") as f:
            have = f.read().replace("\r\n", "\n")
    except OSError:
        have = None

    if "--check" in argv:
        if have == want:
            print(f"[ports] {HEADER_PATH} 是最新的")
            return 0
        print(f"[ports] !! {HEADER_PATH} 和 server/config.py 对不上，"
              f"请跑一次 python tools/gen_ports_h.py", file=sys.stderr)
        return 1

    if have == want:
        print(f"[ports] {HEADER_PATH} 无变化")
        return 0
    os.makedirs(os.path.dirname(HEADER_PATH), exist_ok=True)
    # ★ 落盘用 CRLF：这是个 Windows C 工程，仓库里的 .c/.h 都是这样。
    with open(HEADER_PATH, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(want)
    print(f"[ports] 已更新 {HEADER_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
