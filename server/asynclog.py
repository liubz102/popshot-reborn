#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""异步日志出口 —— **写盘绝不许发生在游戏逻辑线程上**。

需求（用户 2026-09-01）：「bot 多、子弹多的时候游戏突发性很卡……把客户端和
服务端里所有的 log 全都改成异步写入，不要阻塞影响游戏进程。」

为什么非改不可
--------------

服务端每一条日志最后都是 `print(..., flush=True)`，而 stdout 被启动脚本
重定向进了 `logs/server.out` —— 于是 `flush=True` 把块缓冲的好处全抵消掉，
**每行一次 `WriteFile` 系统调用**。一次 45 分钟的调试会话写了 24.9 万行。

要命的不是总量，是**它发生在谁身上**：

* `RoomLoop.run_tick()`（`gameserver.py`）**整格持有 `room.sim_lock`**，
  bot 的每一条 `machine.log()` 都在这把锁里面；
* 而真人的 `Conn.forward_peer_data()` 也要拿同一把 `sim_lock`，
  它进来的时候手上还攥着 `peer_lock`（锁序见 `lobby.py`）。

⇒ 房间线程一等磁盘，整个房间的 tick 和**所有真人的同步包转发**跟着一起等。
`logs/server.out` 里那几行 `[sim] ⚠ 房间 #3 落后 38 格（1216 ms）` 就是它。

设计
----

照抄仓库里已经在用的那套异步模式（`botplan.Planner` / `roomclock.Scheduler` /
`Conn._writer_loop`），**不引入 `logging`**：

1. 服务端一行 `logging` 都没用过，全是裸 `print` 和 `fh.write`。要接
   `QueueHandler` 得把几百个调用点改成 `logger.info(...)`，改动面大得多。
2. `logging` 每条记录都会 `findCaller`（`sys._getframe`），在 31 Hz × N 房间
   的量级上是白花的钱。
3. f-string 在调用前就求值了，`QueueHandler` 省不掉它 —— 逐包 dump 那种
   贵的格式化**必须**保留调用点上的 `if VERBOSE:` 前置门，换成 logging 反而
   容易让人以为「有 level 了就不用管」。

★ **没 `start()` 的时候就是同步写**，和改造前逐字一致。单测因此一行都不用改
（`test_logs.py` 用 `redirect_stdout` 断言输出的那几个照样绿），
只有 `app.py` 会调 `start()`。

顺序保证
--------

**只有一条队列、一条写线程** ⇒ 所有生产者之间的先后顺序和改造前完全一样。
写线程一次把队列取空，按目标分组，**每组一次 `write` + 一次 `flush`**。

丢弃
----

队列有上限，满了就丢并计数 —— 磁盘卡住时**绝不允许**把房间线程堵死。
恢复以后补一行「丢了几条」，**按状态翻转补报**（丢过 → 说一次 → 清零），
不按次数也不按时间（铁律 10）。
"""
from __future__ import annotations

import collections
import sys
import threading
import time

#: 队列上限。这是**内存预算**，不是判据 —— 超过它说明磁盘已经跟不上了，
#: 这时候「丢日志」比「卡住房间」正确。一条日志按 200 字节算，20 万条 = 40 MB。
MAX_PENDING = 200_000

_cv = threading.Condition()
_queue = collections.deque()
_thread = None
_running = False
_dropped = 0
#: 写线程正在干活（用来让 `drain()` 等到「队列空 **且** 没有正在写的」）。
_busy = False


class _Stdout:
    """`sys.stdout` 的占位目标。

    ★ 每次写的时候**现查** `sys.stdout`，不缓存 —— 单测里的
    `contextlib.redirect_stdout` 换的就是这个属性，缓存了就写错地方。
    """

    __slots__ = ()

    def __repr__(self):
        return "<stdout>"


#: 单例。`emit()` 不传 `target` 时用它。
STDOUT = _Stdout()


def _write_group(target, chunks):
    """把攒到一起的若干段写进同一个目标，**一次 write + 一次 flush**。"""
    try:
        if target is STDOUT:
            stream = sys.stdout
            if stream is None:
                return
            stream.write("".join(chunks))
            stream.flush()
        elif callable(target):
            # 回调式目标：`eventlog` 用它 —— 落盘路径要跨天切名，那段逻辑
            # 只有 eventlog 自己知道，所以让它给一个「拿到当前该写的句柄」的
            # 回调，在**写线程**上执行。
            fh = target()
            if fh is None:
                return
            fh.write("".join(chunks))
            fh.flush()
        elif isinstance(chunks[0], (bytes, bytearray)):
            target.write(b"".join(chunks))
            target.flush()
        else:
            target.write("".join(chunks))
            target.flush()
    except Exception:               # noqa: BLE001
        # 日志写不进去绝不能把服务端拖垮（磁盘满 / 目录只读 / 句柄已关）。
        pass


def _flush_batch(batch):
    """把一批 `(目标, 内容)` 按目标**连续分组**后写出去。

    ★ 是「连续分组」不是「按目标归并」：归并会打乱不同目标之间的先后关系，
      而 `server.out` 和逐连接的 `game_NNN.txt` 本来就是同一串事件的两份抄本，
      顺序不该对不上。
    """
    if not batch:
        return
    target = batch[0][0]
    chunks = [batch[0][1]]
    for tgt, payload in batch[1:]:
        if tgt is target:
            chunks.append(payload)
        else:
            _write_group(target, chunks)
            target = tgt
            chunks = [payload]
    _write_group(target, chunks)


def _run():
    global _busy, _dropped
    while True:
        with _cv:
            while not _queue and _running:
                _cv.wait()
            if not _queue and not _running:
                return
            batch = list(_queue)
            _queue.clear()
            dropped = _dropped
            _dropped = 0
            _busy = True
        try:
            _flush_batch(batch)
            if dropped:
                _write_group(STDOUT, [f"[asynclog] !! 日志队列满，丢了 {dropped} 条\n"])
        except Exception:           # noqa: BLE001
            pass
        finally:
            with _cv:
                _busy = False
                _cv.notify_all()


def start():
    """起写线程。**只有 `app.py` 调**；调之前 `emit()` 是同步的。

    重复调用无害（已经在跑就直接返回）。
    """
    global _thread, _running
    with _cv:
        if _running and _thread is not None and _thread.is_alive():
            return _thread
        _running = True
        _thread = threading.Thread(target=_run, name="asynclog", daemon=True)
        _thread.start()
        return _thread


def stop(timeout=5.0):
    """排空队列并停掉写线程。进程收尾时调，停完回落到同步写。"""
    global _thread, _running
    with _cv:
        if not _running:
            return
        thread = _thread
        _running = False
        _cv.notify_all()
    if thread is not None:
        thread.join(timeout)
    with _cv:
        if _thread is thread:
            _thread = None
        batch = list(_queue)
        _queue.clear()
    # 线程没在规定时间内退干净（磁盘挂住了）也要把剩下的写掉 —— 同步写，
    # 反正这时候已经在收尾了，多等一会儿比丢日志强。
    _flush_batch(batch)


def running():
    return _running


def drain(timeout=10.0):
    """等到队列空、也没有正在写的。**给单测和收工检查用**。

    ⚠ 战斗路径上一次都不许调它 —— 那就是把异步又变回同步。
    """
    if not _running:
        return True
    deadline = time.monotonic() + timeout
    with _cv:
        while _queue or _busy:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _cv.wait(left)
        return True


def _submit(target, payload):
    """入队；没起写线程就当场写。**绝不阻塞**。"""
    global _dropped
    if not _running:
        _write_group(target, [payload])
        return
    with _cv:
        if len(_queue) >= MAX_PENDING:
            _dropped += 1
            return
        _queue.append((target, payload))
        _cv.notify()


def emit(line, target=None):
    """记一行（不带换行的文本）。`target=None` = stdout。

    这是**唯一**该被业务代码调的入口：所有 `print(..., flush=True)` 都换成它。
    """
    _submit(STDOUT if target is None else target, line + "\n")


def emit_text(text, target=None):
    """记一段**已经带好换行**的文本。逐连接的 .txt 抄本用。"""
    _submit(STDOUT if target is None else target, text)


def emit_bytes(fh, data):
    """记一段二进制（`--verbose` 的 `.raw.bin` / `.dec.bin` 抓包流）。

    ★ 改造前这里是每次 `recv` 一次 `write` + 一次 `flush`；现在攒成一批
      再写，抓包内容一字节不差，只是不再让读线程等磁盘。
    """
    _submit(fh, bytes(data))
