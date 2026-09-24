#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`logs/` 和 `logs_client_crash/` 的打包下载（管理页「数据管理」→「下载日志」，V0.3商店）。

需求（用户 2026-09-17）：游戏部署到云服务器之后，服务端日志和玩家客户端自动传上来的
崩溃包只能远程登录上去取，麻烦。管理页里加一个入口：看大小、选范围、打成 zip 下载。

## 只做「清单 + 流式写 zip」，HTTP 那一半在 `web/admin.py`

这个模块**不 import `web/` 和 `gameserver`**（和 `databackup` 同一条规矩）。两件事分开：

* `LogPacker.overview()` —— 弹窗上那几个数（多少文件、多大、崩溃包有哪几份）；
* `LogPacker.plan()` + `write_zip()` —— 按请求列出文件、往一个「只有 `write()`」的对象
  里流式写 zip。写到哪里去（HTTP 响应体、内存、文件）它不关心。

## 口径

* `logs/` = **第一层的普通文件，含点文件，不递归**。和 `logcleanup` 同一个口径；
  开发机的 `logs/investigation-deps/` 是逆向工具不是日志，递归会把它卷进去。
* 「最近 N 小时」= 文件的**最后修改时间**在 `now - N 小时` 之后（`RECENT_HOURS`）。
  ★ 这个 N 是用户定的产品参数（「12 小时内的 log」），不是铁律 10 说的那种时序阈值
    —— 它不用来判断任何事件的先后，只是「要哪一段」。
* `logs_client_crash/` = 第一层**目录**各算一份（跳过点开头的 `.tmp-*` 半成品 /
  `.tmp-del-*` 待删），下载一份 = 那个目录下**全部**文件（递归）。
* zip 里本身已经是压缩包的成员（崩溃包就是一个 `.zip`）原样存（`ZIP_STORED`），
  其余走最快的 deflate（level 1）—— 用户要的是「快速压缩」，不是「压得小」。
* zip 根上放一份 `MANIFEST.txt`：什么时候、从哪台机器、打了哪些、跳过了哪些。
  几天后回头看一个 zip 时，这几行比文件名可靠。

## ★ 大小和时间用 `os.stat`，不用 `os.scandir().stat()`

Windows 上 `scandir` 的 `DirEntry.stat()` 直接用目录枚举带回来的数据，而 NTFS 对
**正开着写**的文件只在句柄关闭时才把大小 / mtime 写回目录项 —— `server.out` 被
`daylog` 长期以追加模式持有，服务端开了一天后 `scandir` 看到的可能还是启动那一刻
的数值，「最近 12 小时」会把最重要的那个文件漏掉。`os.stat(path)` 按句柄查，是新鲜的。
`test_logpack` 有一条「打开着写、不关」的用例钉这件事。

## 和 `logcleanup` / `crashstore.cleanup` 并发：不加锁

两边各自本来就容忍：清理删不掉正被读的文件会跳过、下次再删（Windows 上 `open()`
不带 FILE_SHARE_DELETE）；这里遇到刚被删掉的文件就跳过，并把它记进 `MANIFEST.txt`。
加一把锁只会让「凌晨 4 点那一下」和「有人正在下载」互相等，而两边都不需要对方等。

## 写 zip 的输出对象只要 `write()` 和 `flush()`

`zipfile` 发现它没有 `tell()` 会自己包一层 `_Tellable` 并改用 data descriptor
（每个成员的大小 / CRC 写在数据**后面**），所以能往 HTTP 响应体这种不能回头的流里写。
★ `write()` **必须返回写了多少字节**（`_Tellable` 拿它累加偏移，返回 None 直接 TypeError）。

## 铁律：只用标准库，CPython 3.8（Win7 运行时）也要能跑
"""
from __future__ import annotations

import os
import socket
import stat as statmod
import time
import zipfile

from databackup import format_size, format_time
import tzstamp

#: 「最近多少小时」（用户 2026-09-17 定的 12）。前端的标签从 overview 里现取，别两头写死。
RECENT_HOURS = 12

KIND_SERVER = "server"
KIND_CLIENT_CRASH = "client_crash"
KINDS = (KIND_SERVER, KIND_CLIENT_CRASH)

SCOPE_ALL = "all"
SCOPE_RECENT = "recent"
SCOPES = (SCOPE_ALL, SCOPE_RECENT)

#: 两个目录在 zip 里的顶层名字 —— 解开来和服务器上看到的一样。
SERVER_ARCDIR = "logs"
CRASH_ARCDIR = "logs_client_crash"

#: 本身已经压缩过的东西，原样存（`ZIP_STORED`）：再压一遍只费 CPU 不省字节。
STORED_SUFFIXES = (".zip", ".7z", ".gz", ".png", ".jpg", ".dll", ".exe")

#: 最快的 deflate 档。
COMPRESS_LEVEL = 1

MANIFEST_NAME = "MANIFEST.txt"

#: zip 文件名里的时间戳（服务器本地时间）。
STAMP_FORMAT = "%Y%m%d-%H%M%S"


class LogPackError(ValueError):
    """请求本身不合法（kind / scope / sub 不认识）。`status` 是 HTTP 该回的状态码。"""

    status = 400


class NothingToPack(LogPackError):
    """没有可打包的东西：目录空 / 最近 N 小时没有文件 / 没有这一份崩溃包。"""

    status = 404


class PackAborted(Exception):
    """一个成员拷到一半读不下去了（磁盘坏块这种极罕见的事）：zip 已经不完整。

    和「输出那头断了」（`write_zip` 原样抛出 sink 的异常）分开 —— 前者是服务器
    自己的问题，后者多半是浏览器取消了下载，审计日志里要说清楚是哪一种。
    """


class Plan:
    """一次下载要打什么。`entries` 是 `[(磁盘绝对路径, zip 里的名字), ...]`。"""

    def __init__(self, kind, scope, sub, label, filename, stamp, entries,
                 total_bytes, now):
        self.kind = kind
        self.scope = scope
        self.sub = sub
        #: 给人看的中文（审计日志 / 浮条用）：「服务端日志（最近 12 小时）」。
        self.label = label
        #: 浏览器存成什么名字。**全 ASCII** —— HTTP 头是 latin-1，放中文当场炸。
        self.filename = filename
        self.stamp = stamp
        self.entries = entries
        #: 清单时的原始字节数之和（打包时文件还可能在长，只当参考）。
        self.total_bytes = total_bytes
        self.now = now


class LogPacker:
    """两个目录的清单 + 打包。`clock` 只给测试换时钟用。"""

    def __init__(self, logdir, crash_dir, clock=time.time):
        self.logdir = logdir
        self.crash_dir = crash_dir
        self._clock = clock

    # ---------------------------------------------------------------- 清单
    def server_files(self):
        """`logs/` 第一层的普通文件：`[(名字, 字节数, mtime), ...]`，按名字排。

        目录不存在 = 没有日志，不是错误。刚被清理线程删掉的文件跳过。
        """
        try:
            names = os.listdir(self.logdir)
        except OSError:
            return []
        out = []
        for name in sorted(names):
            path = os.path.join(self.logdir, name)
            try:
                st = os.stat(path)          # ★ 不用 scandir，见文件头
            except OSError:
                continue
            if not statmod.S_ISREG(st.st_mode):
                continue                    # 子目录 / 别的东西不算
            out.append((name, st.st_size, st.st_mtime))
        return out

    def recent_files(self, now=None):
        """最近 `RECENT_HOURS` 小时内写过的那些（同 `server_files` 的形状）。"""
        now = self._clock() if now is None else now
        since = now - RECENT_HOURS * 3600
        return [item for item in self.server_files() if item[2] >= since]

    def crash_dirs(self):
        """`logs_client_crash/` 第一层的目录：`[(名字, 文件数, 字节数, mtime), ...]`，按名字排。

        按名字排是 `crashstore` 文件头定的口径：同一个客户端的历次崩溃挨在一起。
        点开头的一律不算（`.tmp-*` 是没收完的半成品，`.tmp-del-*` 是正在删的）。
        """
        try:
            names = os.listdir(self.crash_dir)
        except OSError:
            return []
        out = []
        for name in sorted(names):
            if name.startswith("."):
                continue
            path = os.path.join(self.crash_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not statmod.S_ISDIR(st.st_mode):
                continue
            files = _walk_files(path)
            out.append((name, len(files), sum(size for _p, _rel, size in files),
                        st.st_mtime))
        return out

    def overview(self, now=None):
        """弹窗要的全部数字。每次现算（stat 几百个文件是毫秒级的事）。"""
        now = self._clock() if now is None else now
        files = self.server_files()
        recent = self.recent_files(now)
        dirs = self.crash_dirs()
        return {
            "recent_hours": RECENT_HOURS,
            "server": {
                "dir": self.logdir,
                "dirname": SERVER_ARCDIR,
                "total": _tally(len(files), sum(size for _n, size, _m in files)),
                "recent": _tally(len(recent), sum(size for _n, size, _m in recent)),
            },
            "crash": {
                "dir": self.crash_dir,
                "dirname": CRASH_ARCDIR,
                "total": dict(_tally(sum(count for _n, count, _s, _m in dirs),
                                     sum(size for _n, _c, size, _m in dirs)),
                              dirs=len(dirs)),
                "dirs": [{"name": name, "files": count, "size": size,
                          "size_text": format_size(size), "mtime": mtime,
                          "mtime_text": format_time(mtime, seconds=False)}
                         for name, count, size, mtime in dirs],
            },
            "generated_at": now,
            "generated_text": format_time(now),
        }

    # ---------------------------------------------------------------- 计划
    def plan(self, kind, scope=SCOPE_ALL, sub="", now=None):
        """把一次下载请求变成 `Plan`。参数不合法抛 `LogPackError`，没东西抛 `NothingToPack`。

        ★ `sub` 是浏览器传上来的、会拼进路径的字符串 —— **只校验不清洗**
          （和 `crashstore.check_id` 同一条理由）：必须和 `crash_dirs()` 列出来的
          某个名字**一模一样**。`..`、带斜杠的、大小写不同的、点开头的全都对不上。
        """
        now = self._clock() if now is None else now
        stamp = time.strftime(STAMP_FORMAT, time.localtime(now))
        kind = str(kind or "")
        scope = str(scope or SCOPE_ALL)
        sub = str(sub or "")
        if kind == KIND_SERVER:
            if scope not in SCOPES:
                raise LogPackError("范围只能是 all（全量）或 recent（最近 %d 小时）"
                                   % RECENT_HOURS)
            if sub:
                raise LogPackError("服务端日志没有子目录可选")
            if scope == SCOPE_RECENT:
                files = self.recent_files(now)
                label = "服务端日志（最近 %d 小时）" % RECENT_HOURS
                filename = "logs_server_%dh_%s.zip" % (RECENT_HOURS, stamp)
                if not files:
                    raise NothingToPack("最近 %d 小时内没有写过的日志" % RECENT_HOURS)
            else:
                files = self.server_files()
                label = "服务端日志（全量）"
                filename = "logs_server_%s.zip" % stamp
                if not files:
                    raise NothingToPack("logs/ 里没有可打包的文件")
            entries = [(os.path.join(self.logdir, name), SERVER_ARCDIR + "/" + name)
                       for name, _size, _mtime in files]
            total = sum(size for _name, size, _mtime in files)
        elif kind == KIND_CLIENT_CRASH:
            if scope != SCOPE_ALL:
                raise LogPackError("崩溃包只有全量，没有「最近」这个范围")
            dirs = self.crash_dirs()
            names = [name for name, _c, _s, _m in dirs]
            if sub:
                if (sub != os.path.basename(sub) or sub.startswith(".")
                        or sub not in names):
                    raise NothingToPack("没有这一份崩溃包（可能刚被清理掉了，刷新一下）")
                picked = [sub]
                label = "崩溃包 %s" % sub
                filename = "logs_client_crash_%s_%s.zip" % (_ascii_name(sub), stamp)
            else:
                picked = names
                label = "客户端崩溃包（全部 %d 份）" % len(picked)
                filename = "logs_client_crash_%s.zip" % stamp
                if not picked:
                    raise NothingToPack("还没有收到过崩溃包")
            entries = []
            total = 0
            for name in picked:
                for path, rel, size in _walk_files(os.path.join(self.crash_dir, name)):
                    entries.append((path, CRASH_ARCDIR + "/" + name + "/" + rel))
                    total += size
            if not entries:
                raise NothingToPack("这一份崩溃包里是空的" if sub else "崩溃包目录里没有文件")
        else:
            raise LogPackError("kind 只能是 server（服务端日志）或 client_crash（崩溃包）")
        assert _is_ascii(filename), filename
        return Plan(kind, scope, sub, label, filename, stamp, entries, total, now)

    # ---------------------------------------------------------------- 打包
    def write_zip(self, sink, plan, meta=None):
        """把 `plan` 里的文件流式写成 zip 到 `sink`（只要 `write()` 返回字节数 + `flush()`）。

        返回 `{"files": 进了 zip 的文件数, "bytes": 它们的原始字节数,
              "skipped": [(zip 里的名字, 原因), ...]}`。

        ★ 「跳过」的判据是**这个成员一个字节都还没进流**：`zf.write()` 的顺序是
          stat → open → 写本地文件头 → 拷贝，stat / open 失败（刚被清理线程删了、
          权限不够）时流里什么都没动，可以安心跳过；拷到一半才失败就是半个成员
          已经进流了，这时抛 `PackAborted` 让上层把连接断掉 —— 绝不交付一个自称
          完整、其实缺了半截的 zip。
        ★ 输出那头写不动了（浏览器取消下载）：`sink` 的异常**原样往上抛**
          （类型不变，审计日志要写是哪一种断法），而且**不再碰 sink**。
        """
        meta = dict(meta or {})
        counter = _Counting(sink)
        zf = zipfile.ZipFile(counter, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=COMPRESS_LEVEL, strict_timestamps=False)
        written = []            # [(arcname, 原始字节, date_time), ...]
        skipped = []
        try:
            for path, arcname in plan.entries:
                before = counter.total
                try:
                    zf.write(path, arcname, compress_type=compress_type_for(arcname),
                             compresslevel=COMPRESS_LEVEL)
                except (OSError, ValueError) as error:
                    if counter.broken:
                        raise                       # 输出断了，和这个文件无关
                    if counter.total != before:
                        raise PackAborted("%s：%s" % (arcname, error)) from error
                    skipped.append((arcname, _describe(error)))
                    continue
                info = zf.getinfo(arcname)
                written.append((arcname, info.file_size, info.date_time))
            zf.writestr(MANIFEST_NAME, manifest_text(plan, written, skipped, meta))
        except BaseException:
            # ★ 这份 zip 已经完不成了。**不要** close()：它会往已经坏掉的输出里再写
            #   一段中央目录 —— 要么再抛一次，要么（源文件那种情况）交付一个假的
            #   「完整」zip。`fp = None` 让 close() 和 __del__ 都当它已经关过，
            #   否则垃圾回收时 __del__ 里那一下会把 "Exception ignored" 打进 server.err。
            zf.fp = None
            raise
        zf.close()              # 写中央目录 + 叫一次 sink.flush()
        return {"files": len(written),
                "bytes": sum(size for _name, size, _when in written),
                "skipped": skipped}


# -------------------------------------------------------------------- 工具
class _Counting:
    """数一数交给 `sink` 多少字节，并记住它有没有坏掉（`write_zip` 靠这两个数判「跳过还是作废」）。"""

    def __init__(self, sink):
        self._sink = sink
        self.total = 0
        self.broken = False

    def write(self, data):
        try:
            count = self._sink.write(data)
        except BaseException:
            self.broken = True
            raise
        # ★ sink 没返回长度就让它在这儿炸，别让 zipfile 里那个 `_Tellable` 抛一个
        #   看不出来源的 TypeError。
        self.total += count
        return count

    def flush(self):
        self._sink.flush()


def compress_type_for(name):
    """崩溃包这种本来就是压缩包的原样存，其余 deflate。"""
    if name.lower().endswith(STORED_SUFFIXES):
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def manifest_text(plan, written, skipped, meta):
    """zip 根上那份 `MANIFEST.txt` 的内容。"""
    lines = [
        "炮炮火枪手 服务端 —— 日志打包清单",
        "类别: %s" % plan.label,
        "打包时刻: %s（服务器本地时间）" % format_time(plan.now),
        "主机: %s" % _hostname(),
    ]
    if meta.get("version"):
        lines.append("服务器版本: %s" % meta["version"])
    if meta.get("by"):
        lines.append("下载者: %s" % meta["by"])
    total = sum(size for _name, size, _when in written)
    lines.append("文件: %d 个，原始大小共 %s" % (len(written), format_size(total)))
    if skipped:
        lines.append("跳过: %d 个（打包时已被清理或读不到）" % len(skipped))
        for arcname, why in skipped:
            lines.append("  %s —— %s" % (arcname, why))
    lines.append("")
    # ★ 逐行的时间不重复写时区（太吵），在表头说一次 —— 它们和「打包时刻」
    #   同一台机器、同一个时区。（bug调查/25：三台机器三个时区，比反过一次。）
    lines.append("--- 文件清单（zip 内路径 / 原始字节 / 最后修改时间，均为 %s）---"
                 % tzstamp.utc_offset_text(plan.now))
    for arcname, size, when in written:
        lines.append("%s\t%d\t%04d-%02d-%02d %02d:%02d:%02d" % ((arcname, size) + tuple(when)))
    lines.append("")
    return "\n".join(lines)


def _walk_files(root):
    """`root` 下全部普通文件（递归）：`[(绝对路径, 相对路径（/ 分隔）, 字节数), ...]`，按路径排。"""
    out = []
    for base, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(base, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not statmod.S_ISREG(st.st_mode):
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            out.append((path, rel, st.st_size))
    return out


def _tally(files, size):
    return {"files": files, "size": size, "size_text": format_size(size)}


def _ascii_name(text):
    """崩溃包目录名进 zip 文件名：只留 `[A-Za-z0-9_.-]`，别的换成 `_`（HTTP 头只能是 ASCII）。"""
    return "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "_.-")) else "_"
                   for ch in text) or "x"


def _is_ascii(text):
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _describe(error):
    return getattr(error, "strerror", None) or str(error) or type(error).__name__


def _hostname():
    try:
        return socket.gethostname()
    except OSError:
        return "?"
