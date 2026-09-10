#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`server/data/*.json` 的备份 / 回滚 / 按天清理（管理页「数据备份」页，V0.3商店）。

需求（用户 2026-09-07）：有人在管理页里把商店 / 配方改坏了，要能回滚。

## 备份长什么样

`server/data/backups/<id>/`，一份备份一个子目录：里面是当时 `server/data/`
下**全部** json 的原样字节拷贝（含 `accounts.json`；以后新加的 json 自动纳入）
+ 一个 `manifest.json`（时刻 / 类型 / 说明 / 文件清单）。人能直接打开看，
单个文件也能手工捞回来。

`<id>` = `YYYYMMDD-HHMMSS-<kind>`，kind ∈ auto（每日定时）/ manual（手动）/
prerollback（回滚前自动留的那份）。先拷到 `.tmp-<id>/`，全拷完再改名到位
—— 列表里永远只有完整的备份。

## 三种写盘方要互斥

拷贝 / 覆盖期间要把所有写 `server/data/*.json` 的人挡在外面：
五份运营配置各一把写锁（`shopcfg.write_lock`，管理页保存拿的就是它们）+
存档锁（`AccountStore.lock`）。顺序固定：`_op_lock` → 配置锁（`shopcfg._SPECS`
的顺序）→ 存档锁（最热的锁最后拿、最先放）。现有代码没有任何一条路同时持
两套锁，所以只要这里顺序固定就不会死锁。

Windows 上这不是「保一致性」的可选项：Python `open()` 不带 FILE_SHARE_DELETE，
备份线程正读着的那一瞬，另一边的 `os.replace` 会直接 `PermissionError`，
结算奖励就丢了。

## 定时

一条守护线程，`threading.Event` 等到设定时刻（本地时间，`HH:MM`）。
设置一改（管理页保存 / 刷新时发现文件被手改）就 `set()` 叫醒它重排。
到点：开着就备份一份 auto，然后**不管开没开**都按保留天数清理一次
（关着自动备份的人手动备份也该按天数清）。
服务端在设定时刻没开着，那天就没有自动备份 —— **不补做**（用户 2026-09-07 拍板）。

★ 铁律 10（禁止固定时间阈值）在这里的豁免：等的就是「时钟走到设定时刻」，
物理上没有别的事件可等 —— 和日志清理的每天 4 点（`logcleanup.py`）是同一类。
保留天数也不是判据阈值，是用户配的策略；判据是 manifest 里的创建时刻。

## 回滚

先把当前状态留一份 prerollback，再逐文件 tmp + `os.replace` 覆盖回去。
写之前每一份都要过**今天的**校验器（旧备份可能不合现在的格式），任何一份
不过就一个字节都不写。五份运营配置互相关联（名字出处在物品库、商店 ⇄ 合成
互斥），**只能一起回滚**；`accounts.json` 是玩家存档，页面上默认不勾，勾了要过
「当前管理员在备份里仍是系统管理员」这一关（管理员账号也在那个文件里，
回错了会把自己锁在外面）。覆盖完 `shopcfg.invalidate()` —— 回滚一份大小相同的
旧文件会骗过 mtime 粒度的热重载。

## 铁律：只用标准库，CPython 3.8（Win7 运行时）也要能跑
"""
import contextlib
import json
import os
import re
import shutil
import threading
import time

import account_store
import config as server_config
import logcleanup
import shopcfg

BACKUP_DIRNAME = "backups"
MANIFEST_NAME = "manifest.json"
FORMAT = 1

KIND_AUTO = "auto"
KIND_MANUAL = "manual"
KIND_PREROLLBACK = "prerollback"
KINDS = (KIND_AUTO, KIND_MANUAL, KIND_PREROLLBACK)
KIND_ZH = {KIND_AUTO: "自动", KIND_MANUAL: "手动", KIND_PREROLLBACK: "回滚前"}
DEFAULT_LABELS = {KIND_AUTO: "每日自动备份", KIND_MANUAL: "手动备份",
                  KIND_PREROLLBACK: "回滚前自动备份"}

#: 说明文字的上限。任意字符串都收，但列表里一行放不下一篇文章。
LABEL_MAX = 200

#: 正在建 / 正在删的目录用这个前缀，列表和清理都认得它。
TMP_PREFIX = ".tmp-"

#: 备份编号的样子。★ 页面传上来的编号**必须**先过它 —— 编号直接拼进路径。
ID_RE = re.compile(r"^\d{8}-\d{6}-(auto|manual|prerollback)(-\d+)?$")

ACCOUNTS_FILENAME = "accounts.json"

#: 回滚对话框里复选框的分组键。
GROUP_CONFIG = "config"
GROUP_ACCOUNTS = "accounts"

SYSTEM_BY = "系统"


class BackupError(Exception):
    """给人看的失败原因（中文）。管理页直接把它回成 `ok: false`。"""


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------

def data_files(data_dir):
    """`data_dir` 里的全部 json 文件名（非递归，排好序）。

    非递归 ⇒ `backups/` 自己不会被卷进去；只认 `.json` 后缀 ⇒ `.bak-*` 和
    写盘中的 `*.tmp` 自然排除。以后新加的 json 自动纳入。
    """
    try:
        names = os.listdir(data_dir)
    except OSError:
        return []
    out = []
    for name in names:
        if name.startswith(".") or not name.endswith(".json"):
            continue
        if os.path.isfile(os.path.join(data_dir, name)):
            out.append(name)
    out.sort()
    return out


def make_id(kind, now, taken):
    """`YYYYMMDD-HHMMSS-<kind>`；同一秒里撞名就加 `-2`、`-3`。"""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    base = "%s-%s" % (stamp, kind)
    candidate = base
    n = 1
    while candidate in taken or (TMP_PREFIX + candidate) in taken:
        n += 1
        candidate = "%s-%d" % (base, n)
    return candidate


def check_id(backup_id):
    if not isinstance(backup_id, str) or not ID_RE.match(backup_id):
        raise BackupError("备份编号不对：%r" % (backup_id,))
    return backup_id


def clean_label(text, default=""):
    """说明文字：去掉控制字符（换行会把表格撑成两行）、掐头去尾、截到上限。"""
    text = "".join(ch if ch >= " " and ch != "\x7f" else " "
                   for ch in str(text or ""))
    text = text.strip()
    if len(text) > LABEL_MAX:
        text = text[:LABEL_MAX]
    return text or default


def next_run_at(now, hour, minute):
    """下一次「每天 hour:minute」的绝对时刻（本地时间，DST 逻辑复用日志清理的）。"""
    return now + logcleanup.seconds_until_daily(now, hour, minute)


def stale_ids(backups, keep_days, now):
    """该删的备份编号。`keep_days <= 0` = 永不自动删除。判据是 manifest 里的创建时刻。"""
    if keep_days <= 0:
        return []
    deadline = now - keep_days * 86400
    return [entry["id"] for entry in backups
            if entry.get("created_at", 0) < deadline]


def read_manifest(directory):
    """读一份备份的 manifest；缺 / 坏 / 不像我们写的 → `None`（不认也不删）。"""
    try:
        with open(os.path.join(directory, MANIFEST_NAME), "r",
                  encoding="utf-8") as fp:
            raw = json.load(fp)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
        return None
    if raw.get("kind") not in KINDS:
        return None
    try:
        raw["created_at"] = float(raw.get("created_at", 0))
    except (TypeError, ValueError):
        return None
    return raw


def format_size(size):
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.0f KB" % (size / 1024.0)
    return "%.1f MB" % (size / 1048576.0)


def format_time(epoch, seconds=True):
    pattern = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
    return time.strftime(pattern, time.localtime(epoch))


def write_bytes_atomic(path, data):
    """字节原样落盘：tmp → fsync → replace（和 `shopcfg.write_json` 同款）。"""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "wb") as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def settings_from_values(values):
    """`config.load()` 的结果 → 这里用的三个字段。"""
    return {
        "enabled": bool(values.get("backup_enabled",
                                   server_config.DEFAULT_BACKUP_ENABLED)),
        "time": str(values.get("backup_time")
                    or server_config.DEFAULT_BACKUP_TIME),
        "keep_days": int(values.get("backup_keep_days",
                                    server_config.DEFAULT_BACKUP_KEEP_DAYS)),
    }


def groups_of(names, current_files):
    """把备份里的文件分成回滚对话框上的几个复选框。

    * 运营配置（`shopcfg.config_filenames()` 登记的那几份）**一个格子**、默认勾
      —— 用户 2026-09-07：五份互相关联（名字出处在物品库、商店 ⇄ 合成互斥），
      不许拆开回滚；
    * `accounts.json` 单独一格、**默认不勾**：那是玩家存档（铁律 11）；
    * 其它 json（将来新加的）各自一格；备份里有、现在没有的默认不勾。
    """
    config_names = shopcfg.config_filenames()
    groups = []
    in_group = [name for name in config_names if name in names]
    if in_group:
        missing = [shopcfg.config_title(name) for name in config_names
                   if name not in names]
        label = "运营配置（%s）" % " · ".join(
            shopcfg.config_title(name) for name in in_group)
        groups.append({
            "key": GROUP_CONFIG, "label": label, "files": in_group,
            "checked": True,
            "warn": ("备份里没有 %s，它保持现状" % " / ".join(missing)
                     if missing else None),
        })
    for name in names:
        if name in config_names:
            continue
        if name == ACCOUNTS_FILENAME:
            groups.append({
                "key": GROUP_ACCOUNTS, "label": "玩家存档 accounts.json",
                "files": [name], "checked": False,
                "warn": "所有玩家的金币 / 装备 / 等级会回到备份那一刻，"
                        "管理员账号也在这份文件里 —— 一般不要勾。",
            })
            continue
        present = name in current_files
        groups.append({
            "key": name, "label": name, "files": [name], "checked": present,
            "warn": None if present else "现在的 server/data 里没有这个文件",
        })
    return groups


def check_selection(in_backup, wanted):
    """运营配置那一组要么全选要么全不选（前端画的是一个格子，这条是给直接
    POST 的人挡的）。"""
    group = [name for name in shopcfg.config_filenames() if name in in_backup]
    chosen = [name for name in group if name in wanted]
    if chosen and len(chosen) != len(group):
        raise BackupError(
            "%s 互相关联（名字出处在物品库、商店 ⇄ 合成互斥），要一起回滚；"
            "现在只选了 %s" % (" / ".join(shopcfg.config_title(n) for n in group),
                              "、".join(chosen)))


def admin_survives(document, name):
    """回滚一份存档之前：这个管理员在里面还是不是系统管理员。不是就抛 `ValueError`。

    管理员账号也在 `accounts.json` 里（`admin_accounts` 段）。回滚一份老存档
    可能让当前登录者不再是系统管理员 —— 每个请求都 403，把自己锁在外面。
    """
    table = account_store.admin_accounts(document)
    entry = table.get(str(name or "").strip())
    if entry is None:
        raise ValueError("这份存档里没有管理员「%s」，回滚后你会被锁在管理页外面"
                         % name)
    if account_store.admin_role_of(entry) != account_store.ADMIN_ROLE_SYSTEM:
        raise ValueError("这份存档里「%s」不是系统管理员，回滚后你会被锁在管理页外面"
                         % name)


def describe(manifest, current_files):
    """一份 manifest → 给页面的一行（时间 / 大小的文本在这儿就格式化好）。"""
    files = [entry for entry in manifest.get("files", [])
             if isinstance(entry, dict) and entry.get("name")]
    names = [entry["name"] for entry in files]
    total = 0
    for entry in files:
        try:
            total += int(entry.get("size") or 0)
        except (TypeError, ValueError):
            pass
    kind = manifest["kind"]
    return {
        "id": manifest["id"],
        "kind": kind,
        "kind_zh": KIND_ZH.get(kind, kind),
        "label": manifest.get("label") or DEFAULT_LABELS.get(kind, ""),
        "created_at": manifest["created_at"],
        "created_text": (manifest.get("created_text")
                         or format_time(manifest["created_at"])),
        "created_by": manifest.get("created_by") or "",
        "files": names,
        "file_count": len(names),
        "size": total,
        "size_text": format_size(total),
        "groups": groups_of(names, current_files),
    }


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------

class BackupService(object):
    """备份目录的门房 + 调度线程。`app.py` 建一个，管理页和线程共用。

    `data_dir=None` ⇒ 每次现取 `shopcfg.DATA_DIR`（测试会改它，别在 import 时
    抓快照）；`config_path=None` ⇒ `config.config_path()`。
    `accounts` 是 `AccountStore`（拿它的锁）；`log` 是 `f(str)`，不传就不吭声。
    """

    def __init__(self, data_dir=None, config_path=None, accounts=None, log=None):
        self._data_dir = data_dir
        self._config_path = config_path
        self.accounts = accounts
        self.log = log or (lambda msg: None)
        #: 备份 / 回滚 / 删除 / 清理 / 改设置串行（调度线程和网页线程不交错）。
        self._op_lock = threading.Lock()
        self._wake = threading.Event()
        self._settings = None
        self._settings_stamp = None
        self._last_auto = None
        self._thread = None
        self._stopping = False

    # ------------------------------------------------------------ 路径
    def data_dir(self):
        return self._data_dir or shopcfg.DATA_DIR

    def config_path(self):
        return self._config_path or server_config.config_path()

    def backup_dir(self):
        return os.path.join(self.data_dir(), BACKUP_DIRNAME)

    # ------------------------------------------------------------ 设置
    def settings(self):
        """当前设置 `{enabled, time, keep_days}`。

        按 `server.config` 的 `(mtime, size)` 变了才重读 —— 不轮询（那是铁律 10
        说的固定时间阈值）；调用时机是启动、页面刷新、改设置后、线程醒来重排前。
        发现文件被手改就叫醒调度线程重排。
        """
        path = self.config_path()
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if self._settings is None or stamp != self._settings_stamp:
            values, _warnings = server_config.load(path)
            fresh = settings_from_values(values)
            changed = self._settings is not None and fresh != self._settings
            self._settings = fresh
            self._settings_stamp = stamp
            if changed:
                self._wake.set()
        return dict(self._settings)

    def update_settings(self, enabled, time_text, keep_days):
        """校验 → 写回 `server.config` → 叫醒线程 → 立刻清理一次。

        返回 ``(新设置, 删掉的编号, 删不掉的)``。写不进文件时 `OSError` 原样抛
        （管理页会把它变成一句人话）。
        """
        clock = server_config.parse_clock(time_text)
        if clock is None:
            raise BackupError("备份时刻要写成 HH:MM（比如 04:00）")
        try:
            keep_days = int(keep_days)
        except (TypeError, ValueError):
            raise BackupError("保留天数要是一个整数（0 = 永不自动删除）")
        if not (0 <= keep_days <= server_config.MAX_LOG_RETENTION_DAYS):
            raise BackupError("保留天数要在 0 ~ %d 之间（0 = 永不自动删除）"
                              % server_config.MAX_LOG_RETENTION_DAYS)
        values = {"backup_enabled": 1 if enabled else 0,
                  "backup_time": server_config.format_clock(*clock),
                  "backup_keep_days": keep_days}
        with self._op_lock:
            server_config.save_keys(self.config_path(), values)
            self._settings_stamp = None          # 下一次 settings() 一定重读
            self._wake.set()
            removed, failed = self._cleanup_unlocked()
        return self.settings(), removed, failed

    def wake(self):
        self._wake.set()

    # ------------------------------------------------------------ 列表
    def list_backups(self):
        """全部备份的 manifest，新的在前。只认「编号合规 + manifest 读得懂」的目录。"""
        root = self.backup_dir()
        try:
            names = os.listdir(root)
        except OSError:
            return []
        out = []
        for name in names:
            if not ID_RE.match(name):
                continue
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            manifest = read_manifest(path)
            if manifest is None:
                continue
            manifest["id"] = name             # 目录名说了算，编号就是它
            out.append(manifest)
        out.sort(key=lambda entry: (entry["created_at"], entry["id"]),
                 reverse=True)
        return out

    def overview(self):
        """给 `GET /admin/api/backups` 的一整份：设置 + 状态 + 列表。"""
        settings = self.settings()
        current = data_files(self.data_dir())
        backups = self.list_backups()
        return {
            "settings": settings,
            "status": self.status(settings, backups),
            "backups": [describe(entry, current) for entry in backups],
            "current_files": current,
        }

    def status(self, settings=None, backups=None):
        settings = settings or self.settings()
        backups = self.list_backups() if backups is None else backups
        next_at = None
        if settings["enabled"]:
            clock = server_config.parse_clock(settings["time"]) or (4, 0)
            next_at = next_run_at(time.time(), clock[0], clock[1])
        newest_auto = None
        for entry in backups:
            if entry["kind"] == KIND_AUTO:
                newest_auto = {"at": entry["created_at"],
                               "text": format_time(entry["created_at"]),
                               "id": entry["id"]}
                break
        return {
            "enabled": settings["enabled"],
            "next_at": next_at,
            "next_text": format_time(next_at, seconds=False) if next_at else None,
            "last_auto": self._last_auto,            # 这次启动以来最近一次的结果
            "newest_auto": newest_auto,              # 磁盘上最新的一份自动备份
            "thread": ("running" if self._thread is not None
                       and self._thread.is_alive() else "stopped"),
            "dir": self.backup_dir(),
        }

    # ------------------------------------------------------------ 锁
    @contextlib.contextmanager
    def _hold_data(self):
        """把所有写 `server/data/*.json` 的人挡在外面：配置锁（固定顺序）→ 存档锁。"""
        with contextlib.ExitStack() as stack:
            for lock in shopcfg.all_write_locks():
                stack.enter_context(lock)
            if self.accounts is not None:
                stack.enter_context(self.accounts.lock)
            yield

    # ------------------------------------------------------------ 备份
    def create(self, kind, label=None, created_by=SYSTEM_BY):
        """备份一份，然后清理过期的。返回 manifest。"""
        with self._op_lock:
            with self._hold_data():
                manifest = self._create_unlocked(kind, label, created_by)
            self._cleanup_unlocked()
        return manifest

    def _create_unlocked(self, kind, label, created_by, now=None):
        if kind not in KINDS:
            raise ValueError("kind=%r" % (kind,))
        now = time.time() if now is None else now
        data_dir = self.data_dir()
        names = data_files(data_dir)
        if not names:
            raise BackupError("%s 里没有任何 json，没什么可备份的" % data_dir)
        root = self.backup_dir()
        os.makedirs(root, exist_ok=True)
        backup_id = make_id(kind, now, set(os.listdir(root)))
        final = os.path.join(root, backup_id)
        tmp = os.path.join(root, TMP_PREFIX + backup_id)
        os.makedirs(tmp)
        try:
            files = []
            for name in names:
                dst = os.path.join(tmp, name)
                shutil.copyfile(os.path.join(data_dir, name), dst)
                files.append({"name": name, "size": os.path.getsize(dst)})
            manifest = {
                "format": FORMAT,
                "id": backup_id,
                "kind": kind,
                "label": clean_label(label, DEFAULT_LABELS.get(kind, "")),
                "created_at": now,
                "created_text": format_time(now),
                "created_by": created_by or SYSTEM_BY,
                "files": files,
            }
            shopcfg.write_json(os.path.join(tmp, MANIFEST_NAME), manifest)
            os.rename(tmp, final)          # 到这一步之前列表里看不到它
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        self.log("数据备份 已备份 %s（%s，%d 个文件）"
                 % (backup_id, manifest["label"], len(files)))
        return manifest

    # ------------------------------------------------------------ 删除 / 清理
    def _remove_dir(self, backup_id):
        """先改名再删：改名是原子的，列表立刻看不见；删到一半崩了也只剩一个
        `.tmp-del-*`，下次清理顺手扫掉，不会留下一个没有 manifest 的孤儿。"""
        root = self.backup_dir()
        doomed = os.path.join(root, TMP_PREFIX + "del-" + backup_id)
        os.rename(os.path.join(root, backup_id), doomed)
        shutil.rmtree(doomed)

    def remove(self, backup_id):
        check_id(backup_id)
        with self._op_lock:
            if not os.path.isdir(os.path.join(self.backup_dir(), backup_id)):
                raise BackupError("没有这份备份（%s），可能已经被删掉了" % backup_id)
            try:
                self._remove_dir(backup_id)
            except OSError as error:
                raise BackupError(
                    "删不掉 %s（%s）—— 里面的文件多半正被别的程序占着"
                    "（编辑器打开了它？），关掉再试" % (backup_id, error.strerror or error))
        self.log("数据备份 删掉了 %s" % backup_id)

    def cleanup(self, now=None):
        """删掉早于保留天数的备份（三种类型一视同仁）。返回 ``(删了的, 删不掉的)``。"""
        with self._op_lock:
            return self._cleanup_unlocked(now)

    def _cleanup_unlocked(self, now=None):
        now = time.time() if now is None else now
        keep_days = self.settings()["keep_days"]
        removed, failed = [], []
        for backup_id in stale_ids(self.list_backups(), keep_days, now):
            try:
                self._remove_dir(backup_id)
            except OSError as error:
                # Windows 上被别的进程开着就是删不掉；下次再说，绝不喊冤。
                failed.append((backup_id, str(error)))
                continue
            removed.append(backup_id)
        self._sweep_tmp()
        # ★ 按状态翻转说话：什么都没删就一个字都不打。
        if removed or failed:
            note = "，另有 %d 份没删成" % len(failed) if failed else ""
            self.log("数据备份 清掉 %d 份早于 %d 天的备份%s"
                     % (len(removed), keep_days, note))
        return removed, failed

    def _sweep_tmp(self):
        """扫掉 `.tmp-*`（上次建到一半 / 删到一半的残留）。持 `_op_lock` 时才调，
        那时不可能有人正在建。"""
        root = self.backup_dir()
        try:
            names = os.listdir(root)
        except OSError:
            return
        for name in names:
            if name.startswith(TMP_PREFIX):
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)

    # ------------------------------------------------------------ 回滚
    def restore(self, backup_id, files, expect_admin=None, created_by="",
                after_write=None):
        """用一份备份覆盖 `server/data/` 里选中的那几份文件。

        * `files`：要回滚的文件名；运营配置那一组要么全要么全不要（`check_selection`）。
        * `expect_admin`：当前登录的管理员 —— 回滚 `accounts.json` 时他在备份里
          必须仍是系统管理员。
        * `after_write(restored)`：**还持着锁**时的回调（网页层用它让在线连接
          `reload_account()`，同线程拿存档锁是可重入的）。

        返回 ``{"restored", "failed", "pre_backup_id", "manifest"}``。
        任何一份校验不过 ⇒ 抛 `BackupError`，一个字节都没写。
        """
        check_id(backup_id)
        wanted = []
        for name in (files or []):
            name = str(name)
            if name not in wanted:
                wanted.append(name)
        with self._op_lock:
            src_dir = os.path.join(self.backup_dir(), backup_id)
            manifest = read_manifest(src_dir) if os.path.isdir(src_dir) else None
            if manifest is None:
                raise BackupError("没有这份备份（%s）" % backup_id)
            manifest["id"] = backup_id
            in_backup = [entry["name"] for entry in manifest["files"]
                         if isinstance(entry, dict) and entry.get("name")]
            if not wanted:
                raise BackupError("没有选中任何文件，没有回滚")
            unknown = [name for name in wanted if name not in in_backup]
            if unknown:
                raise BackupError("备份里没有 %s" % "、".join(unknown))
            check_selection(in_backup, wanted)
            data_dir = self.data_dir()
            with self._hold_data():
                # 1. 每一份都过今天的校验器：旧备份可能不合现在的格式，
                #    写下去 `shopcfg._load` 会退回缓存 —— 而缓存马上要被清掉，
                #    商店就直接空了。任何一份不过 ⇒ 一个字节都不写。
                payloads = {}
                for name in wanted:
                    path = os.path.join(src_dir, name)
                    try:
                        with open(path, "rb") as fp:
                            raw = fp.read()
                        parsed = json.loads(raw.decode("utf-8"))
                    except (OSError, ValueError) as error:
                        raise BackupError("备份里的 %s 读不了或不是合法 JSON（%s），"
                                          "没有回滚" % (name, error))
                    validator = shopcfg.validator_of(name)
                    try:
                        if validator is not None:
                            validator(parsed)
                        elif name == ACCOUNTS_FILENAME:
                            account_store.AccountStore.check_document(parsed)
                            if expect_admin:
                                admin_survives(parsed, expect_admin)
                    except (shopcfg.ConfigError, ValueError) as error:
                        raise BackupError("备份里的 %s 过不了现在的校验（%s），没有回滚"
                                          " —— 把它取消勾选再试" % (name, error))
                    payloads[name] = raw
                # 2. 目标可写探测：Windows 上编辑器独占打开会当场 PermissionError，
                #    能提前拦的提前拦，别写到一半才发现。
                for name in wanted:
                    target = os.path.join(data_dir, name)
                    if not os.path.exists(target):
                        continue
                    try:
                        with open(target, "r+b"):
                            pass
                    except OSError as error:
                        raise BackupError(
                            "写不进 %s（%s），没有回滚。这个文件多半正被别的程序占着"
                            "（编辑器打开了它？），关掉再试"
                            % (name, error.strerror or error))
                # 3. 回滚前先把现在的状态留一份。
                pre = self._create_unlocked(
                    KIND_PREROLLBACK,
                    "回滚前自动备份（→ %s · %s）"
                    % (manifest.get("created_text")
                       or format_time(manifest["created_at"]),
                       manifest.get("label") or ""),
                    created_by)
                # 4. 逐份覆盖。中途失败如实报告已覆盖 / 未覆盖的名单。
                restored, failed = [], []
                for name in wanted:
                    try:
                        write_bytes_atomic(os.path.join(data_dir, name),
                                           payloads[name])
                    except OSError as error:
                        failed.append((name, error.strerror or str(error)))
                        continue
                    restored.append(name)
                # ★ 热重载靠 mtime，但回滚回来的旧文件很可能和现在的大小一样、
                #   mtime 粒度又粗 —— 不清缓存就是「回滚了却没生效」。
                shopcfg.invalidate()
                if after_write is not None and restored:
                    after_write(restored)
            self._cleanup_unlocked()
        self.log("数据备份 回滚到 %s（%s），回滚前留了 %s%s"
                 % (backup_id, "、".join(restored), pre["id"],
                    ("；失败：" + "、".join(n for n, _e in failed)) if failed else ""))
        return {"restored": restored, "failed": failed,
                "pre_backup_id": pre["id"], "manifest": manifest}

    # ------------------------------------------------------------ 调度线程
    def run_due_once(self, now=None):
        """「到点后做的事」：开着就备份一份 auto，然后不管开没开都清理一次。
        抽出来是为了让测试直接调，不用起线程。"""
        now = time.time() if now is None else now
        settings = self.settings()
        if settings["enabled"]:
            try:
                manifest = self.create(KIND_AUTO, DEFAULT_LABELS[KIND_AUTO],
                                       SYSTEM_BY)          # create 自带清理
            except Exception as error:      # noqa: BLE001 —— 家务活出岔子不许死
                self._last_auto = {"at": now, "text": format_time(now),
                                   "ok": False, "message": str(error)}
                self.log("数据备份 自动备份失败：%s" % error)
            else:
                self._last_auto = {"at": now, "text": format_time(now),
                                   "ok": True, "message": "已备份 " + manifest["id"]}
                return manifest
        try:
            self.cleanup(now)
        except Exception as error:          # noqa: BLE001
            self.log("数据备份 清理出错（忽略）：%r" % (error,))
        return None

    def start(self):
        """起守护线程：先清一次过期的（不产生新备份），之后每天到点做一次。返回线程。"""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stopping = False
        thread = threading.Thread(target=self._run, daemon=True, name="databackup")
        self._thread = thread
        thread.start()
        return thread

    def stop(self):
        """让线程退出（只有测试用；生产里它是 daemon，随进程走）。"""
        self._stopping = True
        self._wake.set()

    def _run(self):
        try:
            self.cleanup()
        except Exception as error:          # noqa: BLE001
            self.log("数据备份 清理出错（忽略）：%r" % (error,))
        while not self._stopping:
            try:
                settings = self.settings()
                clock = server_config.parse_clock(settings["time"]) or (4, 0)
                due = next_run_at(time.time(), clock[0], clock[1])
                # ★ 这一等就是铁律 10 明说的那个例外：等的是时钟走到设定时刻，
                #   物理上没有别的事件可等。设置一改会被 `wake()` 提前叫醒重排。
                fired = self._wake.wait(max(0.0, due - time.time()))
                self._wake.clear()
                if self._stopping:
                    break
                if fired:
                    continue                # 设置变了 → 重新算下一次
                self.run_due_once()
            except Exception as error:      # noqa: BLE001
                # 不该走到这儿（`run_due_once` 自己都接住了）。真到了也别
                # 死循环刷日志：等到下一个「凌晨 4 点」再试，和上面同一个豁免。
                self.log("数据备份 调度出错（忽略）：%r" % (error,))
                self._wake.wait(logcleanup.seconds_until_daily())
                self._wake.clear()
