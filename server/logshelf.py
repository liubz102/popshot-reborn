#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「待下载 7z 包」（X17，D101 / §142）：后台线程把日志压成 7z 放进 `logs_7z/`，
管理页「下载日志」右栏看进度、下载、删除。

## 为什么是「后台压好、落成文件、再下载」（D101，推翻 V0.3商店 D131 的那一条）

7z 开头 32 字节要写尾部目录的偏移 / CRC ⇒ 全部压完才有第一个字节，做不到边压边发；
用户要看实时进度条 ⇒ 在服务器上压好、落成文件，压完才出现「下载 / 删除」。
zip 照旧边压边发（`logpack.write_zip`），两条路在弹窗里二选一。

## 不影响对局

一条后台线程（第一次提交时才起），一次只压一份、其余排队；`lzma` 压缩时让出 GIL，
最多吃满一个核。提交接口只排个队就回，压缩不占 HTTP 请求线程。

## 目录第一次压的时候才建（和 `crashstore` 同一条规矩）

打包自检会把包里的服务端真跑一遍 —— 启动时建目录就会带一个空 `logs_7z/` 进发布包。
上次进程死在压缩中途留下的 `.<名字>.part`，第一次被用到时顺手清掉（目录不存在就不去建）。

## 进度：版本号 + 条件变量（铁律 10）

状态变了（排队 → 压缩中 → 压好 / 失败、删掉一个）或者百分比整数变了，`version` +1 并
`notify_all`。`wait(since)` 只在「版本没变、而且还有活」时挂着 —— 没活就没有东西会自己变，
立刻返回。不设超时：唤醒它的就是事件本身（下一格进度 / 压完 / 失败 / `stop()`）。

## 名字：只校验不清洗

下载 / 删除传上来的名字必须和 `logs_7z/` 里列出来的某一个**一模一样**
（`crashstore.check_id` / `logpack.plan` 的 `sub` 同一条理由）。
"""
from __future__ import annotations

import os
import re
import stat as statmod
import threading
import time

import atomicfile
import logpack
import sevenzip
from databackup import format_size, format_time

#: 目录名（在服务端根目录下，和 `logs/`、`logs_client_crash/` 并排）。
DIRNAME = "logs_7z"
SUFFIX = ".7z"
#: 压缩中的半成品：`.<名字>.part`（点开头 ⇒ 不进清单）。
PART_PREFIX = "."
PART_SUFFIX = ".part"

QUEUED = "queued"
PACKING = "packing"
FAILED = "failed"

#: 从包名认出它是哪一类（给右栏写标签）。`logpack.plan` 造的 stem + 撞名时的 `-N`。
_STAMP = r"\d{8}-\d{6}(?:-\d+)?"
_LABELS = (
    (re.compile(r"logs_server_(\d+)h_" + _STAMP + r"\.7z\Z"),
     lambda match: "服务端日志（最近 %s 小时）" % match.group(1)),
    (re.compile(r"logs_server_" + _STAMP + r"\.7z\Z"), lambda match: "服务端日志（全量）"),
    (re.compile(r"logs_client_crash_" + _STAMP + r"\.7z\Z"),
     lambda match: "客户端崩溃包（全部）"),
    (re.compile(r"logs_client_crash_(.+)_" + _STAMP + r"\.7z\Z"),
     lambda match: "崩溃包 %s" % match.group(1)),
)


class ShelfError(logpack.LogPackError):
    """请求不合法。`status` 是 HTTP 该回的状态码。"""


class NotFound(ShelfError):
    status = 404


class Busy(ShelfError):
    status = 409


def label_of(name):
    """包名 → 给人看的标签；认不出（手放进去的）就用名字本身。"""
    for pattern, make in _LABELS:
        match = pattern.match(name)
        if match:
            return make(match)
    return name


class Job:
    """一次「压缩」请求。压好之后就不在这张表里了（变成 `logs_7z/` 里的一个文件）。"""

    def __init__(self, job_id, plan, name, meta, who, created):
        self.id = job_id
        self.plan = plan
        #: 压好之后的文件名（提交时就定了，同一秒撞名加 `-2`）。
        self.name = name
        self.meta = meta
        #: 审计日志里的「谁」（`'admin'（昵称）` 那种）。
        self.who = who
        self.created = created
        self.state = QUEUED
        self.percent = 0
        self.done = 0
        self.error = ""


class LogShelf:
    """`logs_7z/` + 一条后台压缩线程。`audit(msg)` 写审计日志（`app.py` 传 `eventlog.online`）。"""

    def __init__(self, packer, directory, audit=None, clock=time.time):
        self.packer = packer
        self.dir = directory
        self._audit = audit
        self._clock = clock
        self._cond = threading.Condition()
        #: 排队 / 压缩中 / 失败的任务，按提交顺序。
        self._jobs = []
        self._version = 0
        #: 这一次进程的编号：服务端重启后版本号从 0 重数，页面靠它认出「不是同一串」。
        self._epoch = os.urandom(4).hex()
        self._next_id = 1
        self._thread = None
        self._stopping = False
        self._swept = False

    # ------------------------------------------------------------ 对外
    def submit(self, kind, scope="", sub="", meta=None, who=""):
        """排一个压缩任务，返回 `(Job, 是不是新排的)`。

        参数不合法 / 没东西可压：`logpack.LogPackError` / `NothingToPack`（带 HTTP 状态码）；
        这个 Python 没有 lzma：`logpack.SevenZipUnavailable`（503）。
        同一份（kind / scope / sub）已经在排队或正在压 ⇒ 不再排，把那一个还回去。
        """
        if not sevenzip.AVAILABLE:
            raise logpack.SevenZipUnavailable("这台服务器的 Python 没带 lzma 模块（自己编译时缺 "
                                              "liblzma），打不了 7z；zip 照常能用")
        plan = self.packer.plan(kind, scope or logpack.SCOPE_ALL, sub)     # 读盘：锁外做
        with self._cond:
            if self._stopping:
                raise Busy("服务端正在关闭，没法再排压缩任务")
            self._sweep_once()
            for job in self._jobs:
                if job.state != FAILED and (job.plan.kind, job.plan.scope, job.plan.sub) == \
                        (plan.kind, plan.scope, plan.sub):
                    return job, False
            job = Job("j%d" % self._next_id, plan, self._unique_name(plan.stem),
                      dict(meta or {}), who, self._clock())
            self._next_id += 1
            self._jobs.append(job)
            self._bump()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="logshelf",
                                                daemon=True)
                self._thread.start()
        return job, True

    def snapshot(self):
        """右栏要的全部东西（见 `_snapshot_locked`）。"""
        with self._cond:
            self._sweep_once()
            return self._snapshot_locked()

    def wait(self, since, timeout=None):
        """长轮询：`since` 就是现在的版本、而且还有活 ⇒ 等到版本变了再回。否则立刻回。

        ★ 没有超时（铁律 10）：有活就一定会有下一次变化（进度 / 压完 / 失败）；
          没活就不挂（没有东西会自己变）。`timeout` 只给测试当**保险丝**（HTTP 那条路不传）。
        """
        with self._cond:
            self._sweep_once()
            self._cond.wait_for(lambda: not (since == self._version and self._busy()
                                             and not self._stopping), timeout)
            return self._snapshot_locked()

    def remove(self, name="", job=""):
        """删一个压好的包（`name`），或清掉一条失败记录（`job`）。返回给审计日志用的标签。"""
        with self._cond:
            if job:
                for item in self._jobs:
                    if item.id == job:
                        if item.state != FAILED:
                            raise Busy("这一份还在压，压完才能删")
                        self._jobs.remove(item)
                        self._bump()
                        return item.plan.label
                raise NotFound("没有这条记录（可能已经被清掉了，刷新一下）")
            path = self._check_name(name)
            try:
                os.remove(path)
            except FileNotFoundError:
                raise NotFound("没有这个 7z 包（可能刚被删了，刷新一下）")
            except PermissionError:
                # Windows：正被下载（我们自己开着读）的文件删不掉。
                raise Busy("这个 7z 包正在被下载，下载完再删")
            self._bump()
            return label_of(name)

    def open_file(self, name):
        """打开一个压好的包去下载：`(文件对象, 字节数, 标签)`。名字对不上抛 `NotFound`。"""
        with self._cond:
            path = self._check_name(name)
        try:
            fp = open(path, "rb")
        except FileNotFoundError:
            raise NotFound("没有这个 7z 包（可能刚被删了，刷新一下）")
        return fp, os.fstat(fp.fileno()).st_size, label_of(name)

    def stop(self, timeout=None):
        """停掉后台线程、唤醒所有长轮询（测试用；`timeout` 是等线程退出的保险丝，不是判据）。"""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    @property
    def version(self):
        with self._cond:
            return self._version

    # ------------------------------------------------------------ 后台线程
    def _run(self):
        while True:
            with self._cond:
                while not self._stopping and not self._queued():
                    self._cond.wait()
                if self._stopping:
                    return
                job = self._queued()[0]
                job.state = PACKING
                self._bump()
            self._pack(job)

    def _pack(self, job):
        started = time.monotonic()
        final = os.path.join(self.dir, job.name)
        part = os.path.join(self.dir, PART_PREFIX + job.name + PART_SUFFIX)
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(part, "wb") as fp:
                stats = self.packer.write_7z(fp, job.plan, meta=job.meta,
                                             progress=lambda done: self._progress(job, done))
            note = ("，跳过 %d 个（打包时已被清理或读不到）" % len(stats["skipped"])
                    if stats["skipped"] else "")
            with self._cond:
                # ★ 改名和「任务出表」在同一把锁里：快照不会同时看到它既是文件又是任务。
                atomicfile.replace(part, final)
                self._jobs.remove(job)
                # ★ 审计也在锁里、叫醒之前写：长轮询一醒来，这一行已经在了（测试靠它）。
                self._say("[admin] %s 压缩好了%s：%d 个文件 %s → 7z %s，用时 %.1f 秒%s"
                          % (job.who or "?", job.plan.label, stats["files"],
                             format_size(stats["bytes"]), format_size(stats["size"]),
                             time.monotonic() - started, note))
                self._bump()
        except Exception as error:              # noqa: BLE001 —— 后台线程，什么错都要落到任务上
            _discard(part)
            message = str(error) or type(error).__name__
            with self._cond:
                job.state = FAILED
                job.error = message
                self._say("⚠ [admin] %s 压缩%s失败（%s）" % (job.who or "?", job.plan.label, message))
                self._bump()

    def _progress(self, job, done):
        total = max(job.plan.total_bytes, 1)
        # 封顶 99：文件在压的时候还可能在长，100 只留给「真压好了」。
        percent = min(99, done * 100 // total)
        with self._cond:
            job.done = done
            if percent != job.percent:
                job.percent = percent
                self._bump()

    # ------------------------------------------------------------ 内部（持锁调用）
    def _bump(self):
        self._version += 1
        self._cond.notify_all()

    def _queued(self):
        return [job for job in self._jobs if job.state == QUEUED]

    def _busy(self):
        return any(job.state in (QUEUED, PACKING) for job in self._jobs)

    def _files(self):
        """`logs_7z/` 里压好的包：`[(名字, 字节数, mtime), ...]`。目录不存在 = 还没压过，不去建它。"""
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        out = []
        for name in names:
            if name.startswith(PART_PREFIX) or not name.endswith(SUFFIX):
                continue
            try:
                st = os.stat(os.path.join(self.dir, name))
            except OSError:
                continue
            if statmod.S_ISREG(st.st_mode):
                out.append((name, st.st_size, st.st_mtime))
        return out

    def _check_name(self, name):
        """★ 只校验不清洗：必须和列出来的某一个一模一样。"""
        name = str(name or "")
        if name != os.path.basename(name) or name not in {item[0] for item in self._files()}:
            raise NotFound("没有这个 7z 包（可能刚被删了，刷新一下）")
        return os.path.join(self.dir, name)

    def _unique_name(self, stem):
        taken = {item[0].lower() for item in self._files()}
        taken.update(job.name.lower() for job in self._jobs)
        name = stem + SUFFIX
        number = 2
        while name.lower() in taken:
            name = "%s-%d%s" % (stem, number, SUFFIX)
            number += 1
        return name

    def _sweep_once(self):
        """上次进程死在压缩中途留下的 `.part`：第一次被用到时清掉（这时本进程还没有任务在压）。"""
        if self._swept:
            return
        self._swept = True
        try:
            names = os.listdir(self.dir)
        except OSError:
            return
        for name in names:
            if name.startswith(PART_PREFIX) and name.endswith(PART_SUFFIX):
                _discard(os.path.join(self.dir, name))

    def _snapshot_locked(self):
        files = sorted(self._files(), key=lambda item: (-item[2], item[0]))
        active = [job for job in self._jobs if job.state != FAILED]
        jobs = []
        # 新的在上 = 提交顺序倒过来（别按 created 排：Windows 时钟一格十几毫秒，连着交的会撞）。
        for job in reversed(self._jobs):
            total = job.plan.total_bytes
            jobs.append({
                "id": job.id, "name": job.name, "label": job.plan.label,
                "state": job.state, "percent": job.percent,
                "done": job.done, "done_text": format_size(job.done),
                "total": total, "total_text": format_size(total),
                # 前面还有几份在排 / 在压（只对排队的有意义）。
                "ahead": active.index(job) if job in active else 0,
                "error": job.error,
                "created": job.created,
                "created_text": format_time(job.created, seconds=False),
            })
        return {
            "version": self._version,
            "epoch": self._epoch,
            "busy": self._busy(),
            "available": sevenzip.AVAILABLE,
            "dir": self.dir,
            "dirname": DIRNAME,
            "jobs": jobs,
            "files": [{"name": name, "label": label_of(name), "size": size,
                       "size_text": format_size(size), "mtime": mtime,
                       "mtime_text": format_time(mtime, seconds=False)}
                      for name, size, mtime in files],
            "total": {"count": len(files), "size": sum(item[1] for item in files),
                      "size_text": format_size(sum(item[1] for item in files))},
        }

    def _say(self, message):
        if self._audit is not None:
            try:
                self._audit(message)
            except Exception:                   # noqa: BLE001 —— 写日志失败不能拖垮压缩线程
                pass


def _discard(path):
    try:
        os.remove(path)
    except OSError:
        pass
