#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发奖记录 —— 管理页「批量发送奖励」每按一次留一条（用户 2026-09-10）。

落在 `server/data/gift_history.json`，和账号存档 / 运营配置同一个目录。
目录**每次现取** `shopcfg.DATA_DIR`（不在 import 时抓快照）—— 打包自检的
`--data-dir` 和测试的临时目录都靠这一条把落脚点挪走。

★ 它是**记录**，不是运营配置，三点不一样：

* **不进 git、不进包**（`.gitignore` + 打包时 `server\\data\\` 是空目录，
  `Assert-PackageDataClean` 守着）；
* **启动路径上碰都不碰它** —— 缺文件就是「一次都没发过」，不生成默认值。
  所以它不会像那四份配置一样被打包自检就地生成出来（D7 / 铁律 11 的坑）；
* 数据备份会连它一起备（`databackup.data_files` 认目录下所有 json），
  这是顺带的，不是为它设计的。

一条记录 = 管理页上按一次「确认发送奖励」：

    {id, time, sender, message, summary, players: [...], rewards: [...]}

`players` 的昵称和 `rewards` 的物品名存的是**发送那一刻**的值 —— 物品以后
改了名或从物品库里删掉，记录里仍旧是当时发出去的那个名字。翻旧账要的是
「当时发的是什么」，不是「那个 id 今天叫什么」。
"""
from __future__ import annotations

import json
import os
import time

import shopcfg

#: 文件名。★ 加了新的 `server/data/*.json` 记得同时改 `.gitignore`。
FILENAME = "gift_history.json"

#: 文件格式版本。以后字段变了靠它分辨老文件。
FORMAT = 1

#: 最多留多少条，超出的从最老的一头丢。
#:
#: ★ 这**不是**铁律 10 说的那种等待阈值，是「一个文件攒多大」的取舍：
#:   一条记录含一份玩家名单，几十人的名单约 2 KB，200 条 ≈ 0.4 MB ——
#:   管理页一次全读回来还不至于卡，再多就该分页了。用户自己按「清空记录」
#:   随时能清干净。
HISTORY_MAX = 200


def path(data_dir=None):
    return os.path.join(data_dir or shopcfg.DATA_DIR, FILENAME)


def _lock():
    """写锁。和运营配置共用 `shopcfg` 那套按文件名分的锁 —— 数据备份拷贝时
    要一次拿齐全部写锁（`all_write_locks`），住在那边才拿得到。"""
    return shopcfg.write_lock(FILENAME)


def _set_aside(target, why, log):
    """读不动的旧文件挪到一边，**不直接覆盖**。

    记录的意义就是「以后翻得到」，解析失败就当它不存在、下一发写盘顺手抹掉，
    等于悄悄销毁证据。挪成 `gift_history.json.bad-<时刻>` 放着，人自己看。
    """
    spare = "%s.bad-%s" % (target, time.strftime("%Y%m%d-%H%M%S"))
    try:
        os.replace(target, spare)
    except OSError:
        spare = None
    if log:
        log("⚠ %s 读不了（%s）%s" % (
            target, why,
            "，已挪到 %s" % spare if spare else "，而且挪不走，下一发会覆盖它"))


def _records_of(raw):
    """文件内容 → 记录列表。认 `{format, records}` 和裸列表两种。"""
    records = raw.get("records") if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        return None
    return [row for row in records if isinstance(row, dict)]


def load(data_dir=None, log=None):
    """全部记录，**新的在前**。文件不在 = 一次都没发过，给 `[]`。"""
    with _lock():
        return _load_unlocked(path(data_dir), log)


def _load_unlocked(target, log):
    try:
        with open(target, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
    except FileNotFoundError:
        return []
    except (IOError, OSError, ValueError) as exc:
        _set_aside(target, exc, log)
        return []
    records = _records_of(raw)
    if records is None:
        _set_aside(target, "顶层不是记录列表", log)
        return []
    return records


def _make_id(stamp, taken):
    """`YYYYMMDD-HHMMSS`；同一秒里发两次就加 `-2`、`-3`（照 `databackup.make_id`）。"""
    base = time.strftime("%Y%m%d-%H%M%S", time.localtime(stamp))
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = "%s-%d" % (base, n)
    return candidate


def append(record, data_dir=None, log=None):
    """写一条，返回补好 `id` / `time` 的那一条。

    读盘 → 插到最前 → 截断 → 原子写，整段在写锁里 —— 两个管理员同时按
    「确认发送奖励」时，后写的不会把先写的那一条吃掉。
    """
    with _lock():
        target = path(data_dir)
        records = _load_unlocked(target, log)
        row = dict(record)
        row["time"] = float(row.get("time") or time.time())
        row["id"] = _make_id(row["time"], set(r.get("id") for r in records))
        records.insert(0, row)
        del records[HISTORY_MAX:]
        shopcfg.write_json(target, {"format": FORMAT, "records": records})
        return row


def clear(data_dir=None, log=None):
    """全删，返回删掉了几条。**把文件删掉**，不留一个空壳。"""
    with _lock():
        target = path(data_dir)
        count = len(_load_unlocked(target, log))
        try:
            os.remove(target)
        except FileNotFoundError:
            pass
        return count
