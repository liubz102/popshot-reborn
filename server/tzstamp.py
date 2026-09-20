#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志时间戳的**时区后缀** —— 一个地方算，所有写日志的地方共用。

为什么要有这个文件（2026-09-20，bug调查/25）：

一次线上崩溃调查里，`meta.json` 的 `buildId`（**打包机**本地时间，UTC+9）被
直接拿去和崩溃时刻（**玩家机器**，UTC+8）、服务端日志（**云服务器**，UTC+8）
排同一根时间轴，差出来的那 1 小时把因果关系比反了 —— 结论从「崩溃发生在
发版之后」变成「发生在发版之前」。三类时间戳来自三台机器，却**一个都没写时区**。

⇒ 从此**凡是给人看的时间戳，后面都跟一个 `UTC+8` 这样的后缀**。

## ★★ 后缀在**进程启动时算一次**，之后一直沿用（用户 2026-09-20 第二轮拍板）

第一版每写一行日志就现算一遍偏移，实测 **1.35 µs/行**，把一个时间戳从
1.48 µs 抬到 3.38 µs（**2.3 倍**）。服务端一天几十万行、开调试还有逐弹体诊断，
纯属白烧。

第二版改成了「缓存 + 每次拿 `(timezone, altzone, daylight)` 比一次键」，
为的是**跨过夏令时切换时后缀还能跟着变**。用户当场否掉了这个理由：
**这个游戏的玩家和服务器全在中国（UTC+8，不用夏令时）**，为一个本项目里
不存在的场景留一条每行都要走的判断，是拿复杂度换零收益。

⇒ 现在是：模块 import 时算一次，写进 `ZONE_TEXT`，**之后没有任何生产代码
再算第二遍**。热路径上连那一次元组比较都没有了，只剩一次全局读。

* 代价写明白：进程跑着的时候真换了系统时区 / 跨过夏令时切换，后缀会停在
  启动时那个值。**本项目接受这个代价** —— 换时区本来就该重启服务端，
  而客户端那一侧（`hook/bshook.c`）也是同一套取舍。
* 启动那一刻仍然看一眼 `tm_isdst` 选 `altzone` 还是 `timezone`：不花任何
  运行时代价，而万一将来真有人在用夏令时的地方开服，至少**启动那刻是对的**。

其余规矩：

* 后缀**现算不写死** —— 玩家可能在任何时区，服务器也可能搬家；
* 半小时 / 三刻钟时区照样写得出来（印度 `UTC+5:30`、尼泊尔 `UTC+5:45`）；
* **文件名里的时间戳不加** —— 加了会把文件名弄脏、还会打乱既有的排序和匹配。
  文件名那一层靠文件内容的头一行说明时区。
"""
import time

__all__ = ["utc_offset_text", "stamp"]


def _format_offset(west_seconds):
    """`time.timezone` 那种「本地 + 它 = UTC」的秒数 -> `UTC+8` / `UTC-3:30`。"""
    offset = -west_seconds
    sign = "+" if offset >= 0 else "-"
    hours, rem = divmod(abs(offset) // 60, 60)
    return "UTC%s%d" % (sign, hours) if rem == 0 else \
           "UTC%s%d:%02d" % (sign, hours, rem)


def _zone_text():
    """本机**此刻**的 UTC 偏移后缀。

    ★★ **只在下面那一行 import 时调一次**。生产代码里不该有第二个调用方 ——
       整个文件的意思就是「启动时算一次、之后沿用」。
       （`test_tzstamp.py` 的假时区夹具会直接调它 + 改 `ZONE_TEXT`，
       那是测试为了把半小时时区、负偏移这些开发机跑不到的档钉死。）
    """
    lt = time.localtime()
    # `time.timezone` / `altzone` 是「本地时间要加上多少秒才等于 UTC」，
    # 符号和我们要印的正好相反。
    west = time.altzone if (lt.tm_isdst > 0 and time.daylight) else time.timezone
    return _format_offset(west)


#: 本进程的时区后缀。**算一次，之后一直沿用**（用户 2026-09-20 第二轮拍板）。
ZONE_TEXT = _zone_text()


def utc_offset_text(epoch=None):
    """本进程的 UTC 偏移，写成 `UTC+8` / `UTC-3` / `UTC+5:30`。

    ★ `epoch` **收下但不再使用**：偏移是进程启动时算死的，传哪一刻都一样。
      参数留着是为了调用方的签名不用动（`logpack.py` 传的是打包时刻、
      `crashwatch.py` 传的是会话开始时刻，两处都是「过去的某一刻」）。
    """
    return ZONE_TEXT


def stamp(epoch=None, fmt="%Y-%m-%d %H:%M:%S", millis=False):
    """给人看的时间戳，**带时区后缀**。

    `millis=True` 时在秒后面补 `.mmm`（各家 `ts()` 用的就是这一档）。

    ★ `localtime()` **只调一次** —— 它是服务端每一行日志都要走的路，
      调两次等于把这个函数的开销翻倍。
    """
    epoch = time.time() if epoch is None else epoch
    text = time.strftime(fmt, time.localtime(epoch))
    if millis:
        text += ".%03d" % int(epoch % 1 * 1000)
    return "%s %s" % (text, ZONE_TEXT)
