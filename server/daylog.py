#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按天切分的日志出口 —— 让 `server.out` / `server.err` 也会老、也会被清掉。

## 为什么要有它（用户 2026-09-14）

云主机上取回来的 `logs/server.out` **一个文件 940 MB**。原因不是日志写得多，
是它**永远不切**：

* 这两个文件是**启动脚本的重定向产物**（`Start-Process -RedirectStandardOutput`
  / `nohup … > server.out`），进程活多久它们就长多久；
* `logcleanup` 按 **mtime** 判老，而正在写的文件 mtime 永远是刚才
  ⇒ 保留天数对它**一天都不起作用**。云主机一开几个月，就只有这一个文件。

`online.log` 早就没这个毛病（`eventlog.py` 文件头讲的就是这件事）：跨零点时把
昨天那份改名成 `online-YYYYMMDD.log`，它就此不再被写、mtime 冻在昨天、到期被
`logcleanup` 删掉。**这里对 `server.out` 做同一件事。**

## 做法：把 `sys.stdout` / `sys.stderr` 换成一个自己管文件的流

关键是**谁持有那个文件句柄**。重定向出来的句柄是启动脚本给的，Windows 上
拿着它没法改名；所以只能让 Python **自己开**这个文件：

    logs/server.out            今天的（追加打开，跨重启不截断）
    logs/server-20260913.out   昨天的，切出来的；mtime 冻在昨天 ⇒ 到期被清掉
    logs/server-boot.out/.err  启动脚本的重定向，只兜住 install() 之前那一小段
                               （解释器的 SyntaxWarning、import 崩了的 traceback）

`asynclog._Stdout` 每次写的时候**现查** `sys.stdout`（见那边的注释），所以换掉
它就等于把服务端**所有**日志、连同任何一句裸 `print()` 一起接管过来，
几百个调用点一个都不用改。

## 切分时机：第一次写，不是定时器

跨天判断放在**每次写之前**（`_fh_unlocked`）—— 零点没人说话就不切，等第一行
真的要写的时候再切，那一行本来就属于新的一天。**没有任何定时器和计数器**
（铁律 10）；服务端空转两天再写一行，也照样切得对，因为判据是「当前打开的
这个句柄属于哪一天」这个事实本身。

## 改名失败就原样接着写

和 `eventlog._rotate_unlocked` 同一个取舍：切分是为了让清理够得着它，
不值得为它冒「日志写不进去」的险。`atomicfile.replace` 内部已经替我们扛过
「杀软/索引器开了一瞬」那一档（见 `atomicfile.py`），它还失败就是真有人
长期占着，这时候重试多少次都没用。
"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import time

import atomicfile
import tzstamp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOGDIR = os.path.join(ROOT, "logs")

#: 切出来的那份叫什么。`server.out` -> `server-20260913.out`。
#: ★ 和启动脚本的重启归档名（`server-20260913-101500.out`，`Move-LogAside` /
#:   `rotate_log`）**不会撞** —— 那边多一截 `-HHMMSS`。
DATE_SUFFIX = "%04d%02d%02d"


def dated_name(path, day):
    """`logs/server.out` + (2026, 9, 13) -> `logs/server-20260913.out`。"""
    stem, ext = os.path.splitext(path)
    return "%s-%s%s" % (stem, DATE_SUFFIX % day, ext)


def rotate_to_dated(path, day):
    """把 `path` 上那份**属于 `day` 那天的**日志改名成带日期的，让它开始变老。

    `day` 是 `time.localtime()[:3]` 那种 `(年, 月, 日)`。

    改名失败（目标已存在 / Windows 上正被别人开着 / 权限不够）一律**当没发生**
    —— 见文件头。返回 True 表示真的改成了，只给测试和日志用。

    ★ `eventlog` 切 `online.log` 走的也是这一句，两处的命名和取舍必须一样：
      运维看 `logs/` 时不该看到两种切法。
    """
    if not day:
        return False
    target = dated_name(path, day)
    if os.path.exists(target):
        return False
    try:
        atomicfile.replace(path, target)
    except OSError:
        return False
    return True


class DaySink:
    """顶替 `sys.stdout` / `sys.stderr` 的文本流：自己开文件、自己跨天切名。

    只实现日志真正用得上的那几个方法。`fileno()` 转交给**原来那个流**
    （启动脚本的重定向），这样 `faulthandler` 一类要真 fd 的东西不会当场炸
    —— 它们写到 `*-boot.err` 里去，那正是「Python 自己的底层输出」该去的地方。
    """

    def __init__(self, path, fallback=None, autoflush=False,
                 _localtime=time.localtime):
        self.path = path
        #: 开不出文件时的退路（原来那个流）。磁盘满 / 目录只读时日志还有地方去。
        self._fallback = fallback
        #: stderr 要每写必刷 —— traceback 攒在缓冲区里等于没记。
        self._autoflush = autoflush
        #: 钟。**单测的注入点**（和 `atomicfile` / `crashwatch` 同一套做法），
        #: 业务代码别传 —— 「一个不重启的进程过零点会不会切」只有把钟拨过去
        #: 才验得了，而那正是云主机上唯一会发生的情况。
        self._localtime = _localtime
        self._lock = threading.Lock()
        self._fh = None
        #: 当前这个句柄属于哪一天（`time.localtime()[:3]`）。
        self._day = None

    # --- 流接口（够 print / asynclog / traceback 用就行） ------------------

    encoding = "utf-8"
    errors = "backslashreplace"

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    def isatty(self):
        return False

    def fileno(self):
        if self._fallback is None or self._fallback is self:
            raise OSError("这个流没有 fd（按天切分的日志出口）")
        return self._fallback.fileno()

    def write(self, text):
        if not text:
            return 0
        with self._lock:
            fh = self._fh_unlocked()
            if fh is None:
                self._to_fallback(text)
                return len(text)
            try:
                fh.write(text)
                if self._autoflush:
                    fh.flush()
            except Exception:               # noqa: BLE001
                # 写盘失败绝不能把服务端拖垮，也不能把这一行弄丢。
                self._to_fallback(text)
        return len(text)

    def flush(self):
        # ★ 不顺手刷退路那个流：走退路的那几行在 `_to_fallback()` 里已经当场
        #   刷过了。`asynclog` 每批都会调一次 flush，这里少一次系统调用。
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except Exception:           # noqa: BLE001
                    pass

    def close(self):
        """把自己这份日志文件刷出去、放掉句柄。**再写一次会自己重新开。**

        两条都重要：

        * **放掉句柄** —— Windows 上句柄不放，别人连删都删不掉这个文件
          （单测的临时目录就是这么清不掉的）；
        * **还能再写** —— `sys.stdout` 被谁 `close()` 掉一次就再也写不了
          是灾难（解释器收尾、`atexit` 里都还要用它）。

        ★ **绝不碰退路那个流**（启动脚本给的原始 stdout/stderr）—— 它不归
          我们所有，关掉它等于把解释器最后那几行报错也弄没。
        """
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                except Exception:           # noqa: BLE001
                    pass
            self._fh = None
            self._day = None

    # --- 切分 -------------------------------------------------------------

    def _to_fallback(self, text):
        if self._fallback is None:
            return
        try:
            self._fallback.write(text)
            self._fallback.flush()
        except Exception:                   # noqa: BLE001
            pass

    def _fh_unlocked(self):
        today = self._localtime()[:3]
        if self._fh is not None and self._day != today:
            # 跨天了：关掉、改名成 server-<那天>.out，下面再新开一份。
            try:
                self._fh.close()
            except Exception:               # noqa: BLE001
                pass
            self._fh = None
            rotate_to_dated(self.path, self._day)
        if self._fh is None:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                # 重启后接着写的那份可能是好几天前留下的，同样先切掉，
                # 否则一个「上个月开始的」server.out 会永远新鲜、永远清不掉。
                try:
                    stat = os.stat(self.path)
                except OSError:
                    stat = None
                if stat is not None:
                    old_day = self._localtime(stat.st_mtime)[:3]
                    if old_day != today:
                        rotate_to_dated(self.path, old_day)
                # 追加打开：同一天里跨重启也不该被截掉（用户 2026-09-01
                # 「日志不要被覆盖、只清理过期的」）。
                self._fh = open(self.path, "a", encoding="utf-8",
                                errors="backslashreplace")
                self._day = today
            except Exception:               # noqa: BLE001
                self._fh = None
                self._day = None
        return self._fh


#: 已经装过了没有。`install()` 只该发生一次。
_installed = None


def install(stem="server", logdir=None, banner=None):
    """把 `sys.stdout` / `sys.stderr` 换成按天切分的 `<stem>.out` / `<stem>.err`。

    **只有真进程入口才调**（`app.py` / `relay.py` 的 `main`）—— 单测里 stdout
    必须保持原样，`contextlib.redirect_stdout` 那些断言才成立。

    `banner` 是启动分隔线上要写的那句话（「服务端启动」）。文件现在跨重启
    **追加**，没有这条线就分不清「这一段是哪一次运行写的」。

    返回 `(out 的路径, err 的路径)`。
    """
    global _installed
    if _installed is not None:
        return _installed
    logdir = logdir or DEFAULT_LOGDIR
    out = DaySink(os.path.join(logdir, stem + ".out"), fallback=sys.stdout)
    err = DaySink(os.path.join(logdir, stem + ".err"), fallback=sys.stderr,
                  autoflush=True)
    sys.stdout = out
    sys.stderr = err
    _installed = (out.path, err.path)
    # 进程收尾时把缓冲刷出去。`stop.bat` 是强杀（拿不到 atexit），所以别指望
    # 它兜底 —— 真正保证不丢的是 asynclog 每批一次的 flush。
    atexit.register(out.flush)
    atexit.register(err.flush)
    if banner:
        bar = "=" * 20
        out.write("\n%s %s %s pid=%d %s\n"
                  % (bar, banner, tzstamp.stamp(), os.getpid(), bar))
        out.flush()
    return _installed
