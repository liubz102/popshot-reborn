#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``logs/`` 的自动清理 —— 把太老的日志文件删掉。

需求（用户 2026-08-14）：日志只增不减，本机和云主机上的垃圾越攒越多。
两个触发时机，**两个都要**，因为它们覆盖的是两种完全不同的用法：

1. **服务端每次真正启动时**一次 —— 本机游玩是「开一局关一局」，
   进程反复启停，靠启动那一下就够了。
   ★ 启动脚本发现服务端已经在跑而**跳过启动**时，清理自然也跟着跳过 ——
   这条不用额外写代码，因为清理挂在 `app.py` 的启动路径上，
   而那种情况下 `app.py` 压根没有被执行（`launch.ps1` 第 2 步）。
2. **每天凌晨 4 点**一次 —— 云主机一开几个月，永远等不到第 1 条。

**都在后台线程里做**：一次清理要 stat 几千个文件，同步做会让开局那一下卡住。

保留天数来自 `server.config` 的 `log_retention_days`（默认 3 天，0 = 不清理）。

判据是**文件的最后修改时间**，不是文件名里的日期：

* 正在写的日志（`server.out` / 今天的 `online.log` / 活着的连接那份
  `game_NNN_*.txt`）mtime 就是刚才，**永远不会被选中**；
* 客户端那些 `bshook_20260813_142534_pid24332.log` 文件名里虽然带日期，
  但没有任何东西保证别的写日志的人也这么命名。mtime 是所有文件都有的。

删不掉的文件（Windows 上另一个进程正开着它、权限不够）**只跳过，不报错** ——
清垃圾绝不能把服务端弄挂。
"""
from __future__ import annotations

import fnmatch
import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOGDIR = os.path.join(ROOT, "logs")

#: ★ **这次启动的时刻**，进程一起就定死，用来给逐连接的抓包文件起名
#:   （`auth_<RUN_STAMP>_001_47611.txt` / `game_<RUN_STAMP>_001_27799.dec.bin`）。
#:
#: 为什么要有它（用户 2026-09-01）：那两处的序号是**进程级全局**、从 1 开始，
#: 文件又是 `"w"` 打开的 —— 于是同一天第二次启动的第 1 条连接直接把上一次的
#: `auth_001_47611.txt` 冲掉。磁盘上有实证：`auth_016` 的 mtime 是 8/31 02:53，
#: 而 `auth_001`~`015` 全是当天 23:39 之后，016 就是被覆盖剩下的孤儿。
#:
#: 放在这个模块里：它是 `logs/` 的门房，且**不 import 任何别的 server 模块**，
#: `authserver` 和 `gameserver` 都能安全地拿它，不会绕出循环导入。
#:
#: ★ 新名字仍然落在 `auth_*` / `game_*` 白名单里（见 LOG_PATTERNS），
#:   到期照样被清掉。
RUN_STAMP = time.strftime("%Y%m%d-%H%M%S")

#: 每天几点做那次定时清理（本地时间，整点）。需求指定凌晨 4 点。
DAILY_HOUR = 4

#: 认得的日志文件名。**白名单**而不是「删掉所有文件」：
#: `logs/` 里还躺着 `.server_mode` / `.relay_target` 这类状态文件
#: （启动脚本靠它们判断要不要重启中继），删了会让下次启动多做一次无谓的重启；
#: 逆向时手工留下的截图 / 探针输出也不该被自动清掉。
#:
#: 覆盖的是**所有**真正会自动长出来的东西：
#:   server.out / server.err / relay.out / relay.err / bsloader.out / bsloader.err
#:   bshook_*.log            客户端注入 DLL 的日志
#:   online.log / online-*.log  连接事件流水（后者是 eventlog 按天切出来的）
#:   game_* / auth_* / conn_*   逐连接的抓包落盘（只有 --verbose 才产生）
LOG_PATTERNS = (
    "*.log",
    "*.out",
    "*.err",
    "game_*.txt", "game_*.bin",
    "auth_*.txt", "auth_*.bin",
    "conn_*.txt", "conn_*.bin",
)


def is_log_name(name: str) -> bool:
    """这个文件名算不算「可以自动清理的日志」。"""
    # 以点开头的一律不碰：`.server_mode` / `.relay_target` 是状态文件不是日志。
    if not name or name.startswith("."):
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in LOG_PATTERNS)


def find_stale(logdir: str, days: int, now: float | None = None):
    """列出该删的文件（绝对路径）。`days <= 0` 一律返回空。

    分出这个函数是为了让测试能只验「挑得对不对」，不用真的删东西。
    """
    if days <= 0:
        return []
    now = time.time() if now is None else now
    deadline = now - days * 86400
    try:
        names = os.listdir(logdir)
    except OSError:
        return []                      # 目录还不存在 = 没有日志，不是错误
    stale = []
    for name in names:
        if not is_log_name(name):
            continue
        path = os.path.join(logdir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue                   # 刚被别人删掉了，正常
        if not os.path.isfile(path):
            continue
        if st.st_mtime < deadline:
            stale.append(path)
    stale.sort()
    return stale


def cleanup(logdir: str = None, days: int = 3, now: float | None = None,
            log=None):
    """删掉 `logdir` 里超过 `days` 天没动过的日志，返回 ``(删了几个, 删了多少字节)``。

    `log` 是一个 `f(str)`；不传就不吭声（测试用）。
    """
    logdir = logdir or DEFAULT_LOGDIR
    if days <= 0:
        if log:
            log("日志清理 已关闭（log_retention_days = 0）")
        return 0, 0
    removed = 0
    freed = 0
    failed = 0
    for path in find_stale(logdir, days, now):
        try:
            size = os.path.getsize(path)
            os.remove(path)
        except OSError:
            # Windows 上文件被别的进程开着就是删不掉；下次再说，绝不喊冤。
            failed += 1
            continue
        removed += 1
        freed += size
    if log:
        if removed:
            note = f"，另有 {failed} 个正被占用没删成" if failed else ""
            log(f"日志清理 删掉 {removed} 个超过 {days} 天的日志文件，"
                f"腾出 {freed / 1048576:.1f} MiB{note}")
        else:
            log(f"日志清理 没有超过 {days} 天的日志文件")
    return removed, freed


def seconds_until_daily(now: float | None = None, hour: int = DAILY_HOUR) -> float:
    """距离下一个「凌晨 `hour` 点」还有几秒。

    用 `time.localtime` 而不是自己算 86400 的倍数：这样夏令时 / 手工改时区
    之后也仍然落在当地的 4 点，而不是慢慢漂走。
    """
    now = time.time() if now is None else now
    tm = time.localtime(now)
    target = time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, hour, 0, 0,
                          0, 0, -1))
    if target <= now:
        target = time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday + 1, hour,
                              0, 0, 0, 0, -1))
    # `mktime` 允许 mday 溢出（32 号自动进位到下个月），所以上面那句是安全的。
    return max(1.0, target - now)


def start(logdir: str = None, days: int = 3, log=None, hour: int = DAILY_HOUR):
    """起一条守护线程：**先立刻清一次**，之后每天 `hour` 点再清一次。

    返回那条线程（测试里用得上）。`days <= 0` 时只打一行说明就退出 ——
    连线程都不起，免得一个关掉的功能还占着一个线程。
    """
    logdir = logdir or DEFAULT_LOGDIR
    if days <= 0:
        if log:
            log("日志清理 已关闭（log_retention_days = 0）")
        return None

    def run():
        # ★ 整个循环体套 try：清理是「顺手做的家务」，
        #   任何一步出岔子都不该让这条线程死掉，更不该影响游戏。
        try:
            cleanup(logdir, days, log=log)
        except Exception as error:
            if log:
                log(f"日志清理 出错（忽略）：{error!r}")
        while True:
            wait = seconds_until_daily(hour=hour)
            time.sleep(wait)
            try:
                cleanup(logdir, days, log=log)
            except Exception as error:
                if log:
                    log(f"日志清理 出错（忽略）：{error!r}")

    thread = threading.Thread(target=run, daemon=True, name="logcleanup")
    thread.start()
    return thread
