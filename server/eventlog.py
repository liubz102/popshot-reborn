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

两个级别（用户 2026-08-14 的要求，D112）
-----------------------------------------

* `online()` —— **运营事件**。`start.bat` / `start-debug.bat` 都写。
  判据：「几个月后回头查一次事故，缺了它就查不动」。上下线、登录失败、
  被顶号、建/进/离房间、注册。**频率由玩家的动作决定**，一局下来几十行。
* `debug()`  —— **性能遥测和客户端噪声**。只有 `start-debug.bat`
  （`app.py --verbose`）才写。判据：「只在专门查某个问题的那几天有用，
  平时纯属占地方」。转发耗时、中继 RTT、客户端自己的异常上报。
  **频率由代码里的定时器决定**，一局下来几百上千行。

按天切分
--------

`online.log` 是**追加**打开的，跨重启不截断 —— 于是它永远不会「超过 3 天没动」，
`logcleanup` 也就永远清不到它。所以跨过零点时把昨天那份改名成
``online-YYYYMMDD.log``，让它自己老去、到期被清掉。当天那份始终叫
``online.log``，文档和 ⏳ 里那些「把 logs\\online.log 发回来」不用改。
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
#: `debug()` 写不写。由 `app.py` 按 `--verbose` 设置。
_verbose = False
#: 当前打开的那个文件属于哪一天（`time.localtime` 的 `(年, 月, 日)`）。
_fh_day = None


def ts():
    """和 `gameserver.ts()` 同一个格式，好让两边的行按时间对得上。"""
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"


def configure(path=None, to_file=True, to_stdout=True, verbose=None):
    """改落盘路径 / 开关。测试和「只要 stdout」的部署用得上。

    `verbose=True` 让 `debug()` 也开始写；不传就保持原样。
    """
    global _path, _fh, _fh_day, _to_file, _to_stdout, _verbose
    with _lock:
        if _fh is not None:
            try:
                _fh.close()
            except Exception:
                pass
            _fh = None
            _fh_day = None
        if path is not None:
            _path = path
        _to_file = bool(to_file)
        _to_stdout = bool(to_stdout)
        if verbose is not None:
            _verbose = bool(verbose)


def path():
    return _path


def verbose():
    return _verbose


def _rotate_unlocked(day):
    """把 `_path` 上那份**昨天（或更早）的** `online.log` 改名成带日期的。

    `day` 是它自己的 `(年, 月, 日)`。改名失败（Windows 上正被别人开着、
    同名文件已存在）就**原样接着写** —— 切分是为了让清理够得着它，
    不值得为它冒「日志写不进去」的险。
    """
    stem, ext = os.path.splitext(_path)
    target = f"{stem}-{day[0]:04d}{day[1]:02d}{day[2]:02d}{ext}"
    if os.path.exists(target):
        return
    try:
        os.replace(_path, target)
    except OSError:
        pass


def _fh_unlocked():
    global _fh, _fh_day
    today = time.localtime()[:3]
    if _fh is not None and _fh_day != today:
        # 跨天了：关掉、把它改名成 online-<那天>.log，下面再新开一份。
        try:
            _fh.close()
        except Exception:
            pass
        _fh = None
        _rotate_unlocked(_fh_day)
    if _fh is None:
        os.makedirs(os.path.dirname(_path) or ".", exist_ok=True)
        # 服务端重启后接着写的那份可能是好几天前留下的，同样要先切掉，
        # 否则一个「上个月开始的」online.log 会永远新鲜、永远清不掉。
        try:
            stat = os.stat(_path)
        except OSError:
            stat = None
        if stat is not None:
            old_day = time.localtime(stat.st_mtime)[:3]
            if old_day != today:
                _rotate_unlocked(old_day)
        # 追加打开：同一天里跨重启也不该被截掉。
        _fh = open(_path, "a", encoding="utf-8", errors="backslashreplace")
        _fh_day = today
    return _fh


def _write(line):
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


def online(msg):
    """记一条**运营**事件。任何线程都能调，内部串行化。精简模式下也照记。"""
    _write(f"[{ts()}] [online] {msg}")


def debug(msg):
    """记一条**调试/遥测**事件。只有 `--verbose`（`start-debug.bat`）才写。

    前缀是 `[online-debug]`，好让 `grep '\\[online\\]'` 仍然只捞到运营那一档。
    """
    if not _verbose:
        return
    _write(f"[{ts()}] [online-debug] {msg}")


def host(addr):
    """`socket` 的 `addr` 元组 -> 裸 IP（不带端口、不带方括号）。

    `::` 双栈监听收到 IPv4 连接时，`getpeername` 给的是
    ``::ffff:192.168.11.79`` 这种 v4-mapped 形式（D063）。这里一律剥成
    ``192.168.11.79`` —— 同一台机器**必须**收敛成同一个键，
    否则「按 IP 限流」会因为写法不同而漏掉（注册页限流用的就是这个）。
    """
    if not addr:
        return "?"
    value = str(addr[0])
    if value.lower().startswith("::ffff:") and "." in value:
        value = value[len("::ffff:"):]
    return value


def peer(addr):
    """`socket` 的 `addr` 元组 -> 人看的 ``ip:port``。"""
    if not addr:
        return "?"
    text = host(addr)
    port = addr[1] if len(addr) > 1 else None
    if ":" in text:                       # 真 IPv6：加方括号才分得清端口
        text = f"[{text}]"
    return f"{text}:{port}" if port is not None else text


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
