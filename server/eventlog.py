#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连接事件日志 —— 「谁连上来了 / 谁断开了 / 从哪个 IP」。

和普通的 `log()` 分开，理由有三条：

1. **它必须在精简模式下也照打。** 排查「玩家说他进不来」时要看的就是这几行，
   而 `--verbose` 的逐包 dump 一开就是几十 MB，没法长期开着。
2. **它要能一眼看完。** 全部前缀 ``[online]``，一条事件一行，
   `grep online logs/server.out` 就是一份完整的上下线流水。
3. **它另外落一份盘**（``logs/online.log``）。`server.out` 会被启动脚本覆盖，
   上下线记录不该跟着没。

★ **不打密码，也不打完整票据**（CLAUDE.md 铁律 9 / D067）。账号名和 IP 是要
记的 —— 那正是这份日志存在的意义。
"""
from __future__ import annotations

import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "logs", "online.log")

_lock = threading.Lock()
_path = DEFAULT_PATH
_fh = None
_to_file = True
_to_stdout = True


def ts():
    """和 `gameserver.ts()` 同一个格式，好让两边的行按时间对得上。"""
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"


def configure(path=None, to_file=True, to_stdout=True):
    """改落盘路径 / 开关。测试和「只要 stdout」的部署用得上。"""
    global _path, _fh, _to_file, _to_stdout
    with _lock:
        if _fh is not None:
            try:
                _fh.close()
            except Exception:
                pass
            _fh = None
        if path is not None:
            _path = path
        _to_file = bool(to_file)
        _to_stdout = bool(to_stdout)


def path():
    return _path


def _fh_unlocked():
    global _fh
    if _fh is None:
        os.makedirs(os.path.dirname(_path) or ".", exist_ok=True)
        # 追加打开：上下线流水跨重启也不该被截掉。
        _fh = open(_path, "a", encoding="utf-8", errors="backslashreplace")
    return _fh


def online(msg):
    """记一条连接事件。任何线程都能调，内部串行化。"""
    line = f"[{ts()}] [online] {msg}"
    with _lock:
        if _to_stdout:
            print(line, flush=True)
        if _to_file:
            try:
                fh = _fh_unlocked()
                fh.write(line + "\n")
                fh.flush()
            except Exception:
                # 日志写不进去绝不能把服务端拖垮（磁盘满 / 目录只读）。
                pass


def peer(addr):
    """`socket` 的 `addr` 元组 -> 人看的 ``ip:port``。

    `::` 双栈监听收到 IPv4 连接时，`getpeername` 给的是
    ``::ffff:192.168.11.79`` 这种 v4-mapped 形式（D063）。日志里直接写
    ``192.168.11.79`` —— 玩家报 IP 时说的就是这个，前缀留着只会对不上。
    """
    if not addr:
        return "?"
    host = str(addr[0])
    if host.lower().startswith("::ffff:") and "." in host:
        host = host[len("::ffff:"):]
    port = addr[1] if len(addr) > 1 else None
    if ":" in host:                       # 真 IPv6：加方括号才分得清端口
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None else host


def duration(seconds):
    """在线时长的人话形式。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分 {sec} 秒"
