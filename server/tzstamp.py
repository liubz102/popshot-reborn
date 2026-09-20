#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志时间戳的**时区后缀** —— 一个地方算，所有写日志的地方共用。

为什么要有这个文件（2026-09-20，bug调查/25）：

一次线上崩溃调查里，`meta.json` 的 `buildId`（**打包机**本地时间，UTC+9）被
直接拿去和崩溃时刻（**玩家机器**，UTC+8）、服务端日志（**云服务器**，UTC+8）
排同一根时间轴，差出来的那 1 小时把因果关系比反了 —— 结论从「崩溃发生在
发版之后」变成「发生在发版之前」。三类时间戳来自三台机器，却**一个都没写时区**。

⇒ 从此**凡是给人看的时间戳，后面都跟一个 `UTC+8` 这样的后缀**。

规矩：

* 后缀**现算**，不写死 —— 玩家可能在任何时区，服务器也可能搬家；
* 跟着 DST 走：夏令时会让同一台机器的偏移变，所以按**那一刻**的偏移算，
  不用进程启动时缓存的值（`time.localtime(epoch).tm_isdst` 已经替我们分好了）；
* 半小时 / 三刻钟时区照样写得出来（印度 `UTC+5:30`、尼泊尔 `UTC+5:45`）；
* **文件名里的时间戳不加** —— 加了会把文件名弄脏、还会打乱既有的排序和匹配。
  文件名那一层靠文件内容的头一行说明时区。
"""
import time

__all__ = ["utc_offset_text", "stamp"]


def utc_offset_text(epoch=None):
    """那一刻本机的 UTC 偏移，写成 `UTC+8` / `UTC-3` / `UTC+5:30`。

    ★ 用 `time.localtime().tm_isdst` 选 `altzone` 还是 `timezone`，
      所以夏令时切换前后给出的是**各自正确**的偏移，不是进程启动时的那个。
    """
    epoch = time.time() if epoch is None else epoch
    lt = time.localtime(epoch)
    # `time.timezone` / `altzone` 是「本地时间要加上多少秒才等于 UTC」，符号和
    # 我们要印的正好相反。
    west = time.altzone if (lt.tm_isdst > 0 and time.daylight) else time.timezone
    offset = -west
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    hours, rem = divmod(offset // 60, 60)
    return "UTC%s%d" % (sign, hours) if rem == 0 else \
           "UTC%s%d:%02d" % (sign, hours, rem)


def stamp(epoch=None, fmt="%Y-%m-%d %H:%M:%S", millis=False):
    """给人看的时间戳，**带时区后缀**。

    `millis=True` 时在秒后面补 `.mmm`（各家 `ts()` 用的就是这一档）。
    """
    epoch = time.time() if epoch is None else epoch
    text = time.strftime(fmt, time.localtime(epoch))
    if millis:
        text += ".%03d" % int(epoch % 1 * 1000)
    return "%s %s" % (text, utc_offset_text(epoch))
