#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``logs_client_crash/`` —— 收客户端传上来的崩溃现场压缩包。

需求（用户 2026-09-09）：商店 / 合成加了一大批新道具之后出现「某些新道具导致
客户端闪退」，而这种崩溃只有**真人在真机上打到那一下**才触发。以前靠玩家自己
把 `Dump\\` 和 `BigShot.rpt` 发回来，线上有真人在玩之后这条路指望不上，
所以让客户端崩完自己传上来。

对应的**发送**那一半在 `crashwatch.py`（只在客户端包里跑）。

落地布局::

    logs_client_crash/
        testuser1_a1b2c3d4_20260909-013642/
            testuser1_a1b2c3d4_20260909-013642.zip   <- 原样保存，**不解压**
            receipt.json                             <- 谁、什么时候、多大、校验过没有

目录名 = ``<账号名>_<安装码>_<崩溃时刻>``，账号在前、时刻在后 ——
按名称排序时同一个客户端的历次崩溃自然挨在一起（用户明确要的排序口径）。

★ 三条硬规矩，改这个文件之前先看一眼：

1. **目录名是客户端传上来的，直接拼进路径 ⇒ 必须先过 `ID_RE`，不过就拒**
   （和 `databackup.check_id` 同一条理由）。**绝不「清洗一下再用」** ——
   那等于把「校验」偷换成「推断」。
2. **撞名要按大小写不敏感比对，不能用 `os.path.exists`**：云服跑在 Linux 上，
   `Abc_..._013642` 和 `abc_..._013642` 是两个合法且互不冲突的目录，
   `exists` 一声不吭；等用户把目录拷到 Windows 才炸出来就晚了。见 `_pick_name`。
3. **落位、写 receipt、清理都在后台线程上做**（`Store.start()` 起的那条），
   请求线程只负责把字节收进临时文件。理由和日志异步写一样（V0.3bot D109 /
   §150）：auth / game / web 五个监听在**同一个进程**里，请求线程一等磁盘，
   同进程的战斗转发就跟着等。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 收到的崩溃包放在包根的这个目录下（和 `logs/` 平级）。
DIRNAME = "logs_client_crash"
DEFAULT_CRASH_DIR = os.path.join(ROOT, DIRNAME)

#: 半成品目录前缀。全部收完才 `os.rename` 到位，所以看到 `.tmp-` 开头的
#: 一定是**没收完就断了**的残骸，下次清理顺手扫掉。
#: （和 `databackup.TMP_PREFIX` 同一个套路。）
TMP_PREFIX = ".tmp-"

#: 待删目录前缀：先改名（原子）再 `rmtree`，删到一半崩了也不会留下半个目录。
DEL_PREFIX = ".tmp-del-"

#: 客户端可以传上来的目录名。**账号名 1~16 位**（和
#: `account_store.USERNAME_PATTERN` 的上限一致）+ **8 位十六进制安装码** +
#: **YYYYMMDD-HHMMSS**。
#:
#: ★ 白名单里**没有 `.` 也没有 `/`**，所以 `..` / `./` 这类路径段构造不出来。
#: ★ 撞名后缀 `-2` / `-3` 是**服务端自己加**的，所以这条正则里不含它 ——
#:   客户端传上来的必须是干净的原名。
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}_[0-9a-f]{8}_\d{8}-\d{6}$")

#: 落位队列的上限。满了就**丢并计数**，绝不反压到请求线程
#: （和 `asynclog.MAX_PENDING` 同一个取舍）。一份崩溃包 10 MB 上下，
#: 1024 份 = 10 GB，真排到这个数说明磁盘早就该满了。
MAX_PENDING = 1024

#: 每天几点做那次清理（本地时间，整点）。和日志清理同一个时刻。
DAILY_HOUR = 4


class CrashUploadError(Exception):
    """收包过程中「是客户端的问题」的失败。`status` 是要回的 HTTP 状态码。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def check_id(crash_id):
    """校验客户端传上来的目录名。不合规就抛 `CrashUploadError(400)`。

    ★ 这个字符串会直接拼进路径，所以这里**只做校验，不做清洗** ——
    清洗是客户端的活（`crashwatch.sanitize_account`），服务端这一侧
    「洗一洗再用」等于把校验变成推断，那正是目录穿越的经典入口。
    """
    if not isinstance(crash_id, str) or not ID_RE.match(crash_id):
        raise CrashUploadError(400, "崩溃包编号不合规")
    # 二次保险：即使正则将来被改松，也不该有任何东西能走出 crash 目录。
    if crash_id != os.path.basename(crash_id):
        raise CrashUploadError(400, "崩溃包编号不合规")
    return crash_id


def crash_dir(root: str | None = None) -> str:
    return os.path.join(root, DIRNAME) if root else DEFAULT_CRASH_DIR


def _lower_names(directory):
    """目录下现有条目名的**小写**集合。目录不存在就当空的。

    大小写撞名判定要的就是这个 —— Linux 上 `Abc` 和 `abc` 是两个目录，
    `os.path.exists` 判不出来。
    """
    try:
        return {name.lower() for name in os.listdir(directory)}
    except OSError:
        return set()


def split_id(crash_id):
    """`abc_a1b2c3d4_20260909-013642` -> `("abc", "a1b2c3d4_20260909-013642")`。

    撞名后缀要加在**账号名那一段后面**（撞名的成因就是账号名，标在那里
    一眼看得懂；排序时两条仍然挨着，不破坏「同一客户端聚在一起」）。
    """
    account, _, tail = crash_id.partition("_")
    return account, tail


def pick_name(directory, crash_id):
    """挑一个在 `directory` 里**大小写不敏感地**不撞名的目录名。

    `Abc_a1b2c3d4_20260909-013642` 已经在了，再来一份 `abc_...` 就变成
    `abc-2_a1b2c3d4_20260909-013642`。
    """
    taken = _lower_names(directory)
    account, tail = split_id(crash_id)
    candidate = crash_id
    n = 1
    while (candidate.lower() in taken
           or (TMP_PREFIX + candidate).lower() in taken):
        n += 1
        candidate = "%s-%d_%s" % (account, n, tail)
    return candidate


def format_time(epoch=None):
    epoch = time.time() if epoch is None else epoch
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


class Store:
    """崩溃包的落地 / 落位 / 清理。

    分成两半是**故意**的：

    * `receive()` 在**请求线程**上跑 —— 字节必须当场读走，不然 HTTP/1.1 的
      keep-alive 连接会错位（`web/server.py:_read_body` 那段注释）。
      它只把字节写进 `.tmp-<id>/`，边写边算 sha256。
    * `submit()` 之后的落位（`os.replace`）、写 `receipt.json`、触发清理
      都甩给**后台线程**，请求线程不等磁盘。
    """

    def __init__(self, directory=None, keep_days=0, log=None):
        self.dir = directory or DEFAULT_CRASH_DIR
        self.keep_days = int(keep_days or 0)
        self._log = log
        self._queue = queue.Queue(MAX_PENDING)
        self._thread = None
        self._cleanup_thread = None
        self._stopping = False
        #: 队列满时丢掉了几份。恢复之后补一行日志，**按状态翻转去重**，
        #: 不逐次刷屏（铁律 10）。
        self._dropped = 0

    # ------------------------------------------------------------------ 日志
    def log(self, message):
        if self._log:
            try:
                self._log(message)
            except Exception:                       # noqa: BLE001 —— 日志而已
                pass

    # ------------------------------------------------------------ 请求线程侧
    def begin(self, crash_id):
        """建 `.tmp-<id>/`，返回 `(临时目录, 最终用的目录名)`。

        ★ 目录名在这里就定下来（含撞名后缀），这样 `receipt.json` 里记的
        和磁盘上的一定是同一个。
        """
        check_id(crash_id)
        os.makedirs(self.dir, exist_ok=True)
        name = pick_name(self.dir, crash_id)
        tmp = os.path.join(self.dir, TMP_PREFIX + name)
        # 上一次收到一半就断了的残骸：直接扔掉重来，别在它上面接着写。
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        return tmp, name

    def abandon(self, tmp):
        """收包失败：把半成品扔掉，磁盘上不留任何痕迹。"""
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)

    def submit(self, tmp, name, receipt):
        """交给后台线程去落位。**立刻返回**，不等磁盘。

        队列满了就地丢掉半成品并计数 —— 反压到请求线程等于让上传把
        同进程的游戏拖住，那比丢一份崩溃包严重得多。
        """
        try:
            self._queue.put_nowait((tmp, name, receipt))
        except queue.Full:
            self._dropped += 1
            self.abandon(tmp)
            return False
        return True

    # ------------------------------------------------------------ 后台线程侧
    def place_one(self, tmp, name, receipt):
        """把一份半成品落位。**抽出来是为了让测试直接调，不用起线程。**"""
        target = os.path.join(self.dir, name)
        # receipt 先写进半成品里，这样 `os.rename` 之后目录一定是完整的：
        # 要么没有这个目录，要么它连回执一起齐全，没有中间态。
        with open(os.path.join(tmp, "receipt.json"), "w",
                  encoding="utf-8", newline="\n") as fp:
            json.dump(receipt, fp, ensure_ascii=False, indent=2, sort_keys=True)
            fp.write("\n")
        os.rename(tmp, target)
        return target

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:                        # stop() 放进来的哨兵
                return
            tmp, name, receipt = item
            try:
                self.place_one(tmp, name, receipt)
                dropped, self._dropped = self._dropped, 0
                size = receipt.get("bytes", 0)
                self.log("崩溃日志 ✓ 已保存 %s（%.1f MB，来自 %s）"
                         % (name, size / 1048576.0,
                            receipt.get("from", "?")))
                if dropped:
                    self.log("崩溃日志 ⚠ 刚才队列满，丢了 %d 份" % dropped)
            except Exception as error:              # noqa: BLE001
                # 落位是「顺手做的家务」，任何一步出岔子都不该让这条线程死掉。
                self.log("崩溃日志 ✗ 保存 %s 失败（忽略）：%r" % (name, error))
                self.abandon(tmp)

    def start(self):
        """起落位线程 + 清理线程。幂等：已经活着就直接返回。"""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stopping = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="crashstore")
        self._thread.start()
        # 清理线程只起一次：`stop()` + `start()` 是「把落位队列排空」的手法
        # （测试里用），不该每来一次就多攒一条清理线程。
        if self._cleanup_thread is None:
            self._cleanup_thread = start_cleanup(self, log=self._log)
        return self._thread

    def stop(self, timeout=5.0):
        """排空队列再收工。★ 生产里它是 daemon 线程，这个主要给测试和
        `atexit` 用 —— 和 `asynclog.stop()` 一个定位。"""
        self._stopping = True
        thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread.join(timeout=timeout)
        self._thread = None

    # ---------------------------------------------------------------- 清理
    def cleanup(self, now=None):
        """删掉超过 `keep_days` 天的崩溃包。返回 `(删了几个, 释放多少字节)`。

        判据是目录的 mtime（和日志清理同一个口径）。`keep_days <= 0` 时
        一个都不删，但 **`.tmp-` 残骸照删** —— 那是没收完的垃圾，
        和「保留几天证据」无关。
        """
        now = time.time() if now is None else now
        removed = freed = 0
        try:
            names = os.listdir(self.dir)
        except OSError:
            return 0, 0                             # 目录还没有 = 没收到过
        deadline = now - self.keep_days * 86400 if self.keep_days > 0 else None
        for name in sorted(names):
            path = os.path.join(self.dir, name)
            if not os.path.isdir(path):
                continue
            stale = name.startswith(TMP_PREFIX)     # 残骸：永远该删
            if not stale:
                if deadline is None:
                    continue
                try:
                    stale = os.stat(path).st_mtime < deadline
                except OSError:
                    continue
            if not stale:
                continue
            size = _dir_size(path)
            try:
                self._remove_dir(name)
            except OSError:
                continue                            # 删不掉就跳过，绝不喊冤
            removed += 1
            freed += size
        if removed:
            self.log("崩溃日志清理 删了 %d 份，释放 %.1f MB"
                     % (removed, freed / 1048576.0))
        return removed, freed

    def _remove_dir(self, name):
        """先改名再删：改名是原子的，列表立刻看不见它。"""
        root = self.dir
        doomed = os.path.join(root, DEL_PREFIX + str(int(time.time() * 1000)))
        os.rename(os.path.join(root, name), doomed)
        shutil.rmtree(doomed, ignore_errors=True)


def _dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def seconds_until_daily(now=None, hour=DAILY_HOUR, minute=0):
    """距离下一个「每天 hour:minute」还有几秒。转发给 `logcleanup` 的实现，
    免得同一段夏令时逻辑写两遍。"""
    import logcleanup

    return logcleanup.seconds_until_daily(now=now, hour=hour, minute=minute)


def start_cleanup(store, log=None, hour=DAILY_HOUR):
    """起一条守护线程：**先立刻清一次**，之后每天 `hour` 点再清一次。

    和 `logcleanup.start()` 同一个形状。注意**关掉保留期时线程照起** ——
    因为 `.tmp-` 残骸任何时候都该清，只有「按天数删证据」那一半被关掉。
    """

    def run():
        try:
            store.cleanup()
        except Exception as error:                  # noqa: BLE001
            if log:
                log("崩溃日志清理 出错（忽略）：%r" % (error,))
        while True:
            time.sleep(seconds_until_daily(hour=hour))
            try:
                store.cleanup()
            except Exception as error:              # noqa: BLE001
                if log:
                    log("崩溃日志清理 出错（忽略）：%r" % (error,))

    thread = threading.Thread(target=run, daemon=True, name="crashcleanup")
    thread.start()
    return thread
