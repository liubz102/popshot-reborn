#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务通关记录 —— 待机房间右侧「全体记录 / 个人记录」两个框的数据源。

落在 `server/data/quest_record.json`。目录**每次现取** `shopcfg.DATA_DIR`
（不在 import 时抓快照）—— 打包自检的 `--data-dir` 和测试的临时目录都靠这一条
把落脚点挪走。

★ 它和 `gifthistory.py` 同一档：是**记录**，不是运营配置。

* **不进 git、不进包**（`.gitignore` + 打包时 `server\\data\\` 是空目录，
  `Assert-PackageDataClean` 守着）；
* **启动路径上碰都不碰它** —— 缺文件就是「谁都没打通过」，不生成默认值。
  所以它不会像那几份配置一样被打包自检就地生成出来（D7 / 铁律 11 的坑）；
* 数据备份会连它一起备（`databackup.data_files` 认目录下所有 json）。

## 存什么

`{"关卡id:难度": {账号名: {name, seconds, at}}}`。

**桶内的键是账号名**，所以「一个人一条最好成绩」是**结构性**的不变量 ——
不是靠写入时去重的过程性约定。合作局四个人各更各的，全体榜天然就不会被
同一局的四个人占满。

`name` 是写入那一刻的昵称快照，`at` 是 `time.time()`。`at` 有两个用处：
同分排序的第二键，以及以后管理页要显示「什么时候打的」时不用改格式。

## 为什么判定破纪录也在这儿

`note_clear()` 一次调用同时做两件事：写盘、告诉调用方每个人**破没破纪录**。
两件事必须共用**同一份写前快照**（见函数里的注释），拆成「先查后写」两个
调用就会在合作局上出错，所以这个判定没法留给 `gameserver`。

## 和客户端的三个硬约束（改这个文件前先看一眼）

* 记录值的单位是**秒**，客户端拿它 `/60` 和 `%60` 拼 `"%02d:%02d"`；
* **`99999` 是「无历史战绩」的哨兵**（客户端 `0x466600: cmp eax, 0x1869f`）；
* ★★ **`0` 是禁用值** —— 客户端 `0x4665bc: test eax,eax` 把 0 当成
  「正在查询资料」那一态。所以再快的成绩也要夹到至少 1 秒，
  否则那一格会永远转圈，看起来和「服务端挂了」一模一样。
"""
from __future__ import annotations

import json
import os
import time

#: 顶上去那一句 `os.replace` 的重试外壳（见 `atomicfile.py` 文件头）。
import atomicfile
import shopcfg

#: 文件名。★ 加了新的 `server/data/*.json` 记得同时改 `.gitignore`。
FILENAME = "quest_record.json"

#: 文件格式版本。以后字段变了靠它分辨老文件。
FORMAT = 1

#: 全体榜一次发几条。
#:
#: ★ 这**不是**铁律 10 说的那种等待阈值，是「一个框里装多少」的取舍：
#:   客户端的 `RecordsCb` 是下拉框，刷新函数 `0x466594` 只把第一条设成
#:   收起来时显示的那行文本。10 条约 420 字节载荷，够用且不占地方。
TOP_N = 10

#: 一个桶（= 一张图的一个难度）最多留几个人，超出的把最慢的丢掉。
#:
#: ★ 同上，是「一个文件攒多大」的取舍，不是判据阈值。21 个桶 × 500 人
#:   ≈ 几百 KB。**代价要说清**：被裁掉的人连个人记录一起没了，所以这个
#:   数只能往大了设，正常玩家规模一辈子碰不到。
PLAYERS_MAX = 500

#: 客户端的「无历史战绩」哨兵（`0x1869F`）。只由组包层往线上填，盘上不存它。
NO_RECORD = 99999

#: 记录值的上下限。★ 下限是 1 不是 0，理由见文件头第三条。
SECONDS_MIN = 1
SECONDS_MAX = NO_RECORD - 1

#: 破纪录类型 —— 直接就是 `0x0411 gspEndGame` 业务值索引 9 的线上取值。
#: 客户端 `0x5527e3` 的 `dec/jz` 链只认 1 和 2，其余一概不播报。
RECORD_NONE = 0          #: 没破，不播报
RECORD_PERSONAL = 1      #: 破了自己的记录 → `Chinese.ini:1422`「您的新记录为…」
RECORD_GLOBAL = 2        #: 破了全服记录   → `Chinese.ini:1423`「您的新记录更新为…」

#: 昵称长度上限。和 `account_store.NICKNAME_MAX_LENGTH` 同一个数，但**不 import**
#: —— 这个模块被组包路径调用，不该为了一个常量把账号存档整个拉进来。
NAME_MAX_LENGTH = 16


def path(data_dir=None):
    return os.path.join(data_dir or shopcfg.DATA_DIR, FILENAME)


def key_of(quest_id, difficulty):
    """`(3, 1)` → `"3:1"`。JSON 的键只能是字符串，所以桶键得自己拼。"""
    return "%d:%d" % (int(quest_id), int(difficulty))


def _lock():
    """写锁。和运营配置共用 `shopcfg` 那套按文件名分的锁 —— 数据备份拷贝时
    要一次拿齐全部写锁（`all_write_locks`），住在那边才拿得到。"""
    return shopcfg.write_lock(FILENAME)


def _set_aside(target, why, log):
    """读不动的旧文件挪到一边，**不直接覆盖**（同 `gifthistory._set_aside`）。"""
    spare = "%s.bad-%s" % (target, time.strftime("%Y%m%d-%H%M%S"))
    try:
        atomicfile.replace(target, spare)
    except OSError:
        spare = None
    if log:
        log("⚠ %s 读不了（%s）%s" % (
            target, why,
            "，已挪到 %s" % spare if spare else "，而且挪不走，下一发会覆盖它"))


def _clean_name(raw):
    """盘上的昵称 → 能安全喂进 `w_wstr` 的昵称。

    ★ 注册那一侧已经挡过一遍（`account_store.check_nickname`：控制字符、
    落单代理项、补充平面字符全拒），但**这个文件是可以被人手改的**，
    所以读盘时必须再过一遍同样的规则 —— 补充平面字符会让 `w_wstr` 的
    u16 长度字段少算，直接把整个包的后半截错位。
    """
    text = str(raw or "")
    kept = []
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7f or code > 0xffff:
            continue
        if 0xd800 <= code <= 0xdfff:
            continue
        kept.append(ch)
        if len(kept) >= NAME_MAX_LENGTH:
            break
    return "".join(kept)


def _clean_seconds(raw):
    """盘上的成绩 → 合法秒数；不是正经数字就给 `None`（那一行整条丢掉）。

    ⚠ `bool` 是 `int` 的子类，`True` 会被 `isinstance(x, int)` 放行成 1 秒，
    所以先把它挡掉。
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw < SECONDS_MIN or raw > SECONDS_MAX:
        return None
    return raw


def _clean_row(raw):
    """盘上的一行 → 补齐默认值的一行；坏行给 `None`。

    ★ 铁律 11：**保留不认识的字段**。这一版不认得的键原样留着，以后谁加了
    新字段也不会被这一版的一次改写抹掉。
    """
    if not isinstance(raw, dict):
        return None
    seconds = _clean_seconds(raw.get("seconds"))
    if seconds is None:
        return None
    row = dict(raw)
    row["seconds"] = seconds
    row["name"] = _clean_name(raw.get("name"))
    try:
        row["at"] = float(raw.get("at") or 0.0)
    except (TypeError, ValueError):
        row["at"] = 0.0
    return row


def _buckets_of(raw):
    """文件内容 → `{桶键: {账号: 行}}`。顶层形状不对给 `None`。"""
    records = raw.get("records") if isinstance(raw, dict) else raw
    if not isinstance(records, dict):
        return None
    buckets = {}
    for bucket_key, players in records.items():
        if not isinstance(players, dict):
            continue
        parts = str(bucket_key).split(":")
        if len(parts) != 2:
            continue                      # 桶键解不动就整桶丢掉
        try:
            int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rows = {}
        for username, row in players.items():
            cleaned = _clean_row(row)
            if cleaned is not None and str(username):
                rows[str(username)] = cleaned
        if rows:
            buckets[str(bucket_key)] = rows
    return buckets


def check_document(document):
    """回滚一份备份之前：它是不是一份认得出来的记录表。不是就抛 `ValueError`。

    ★ 只验**顶层形状**，不验每一行 —— 坏行读盘时本来就会被 `_clean_row` 安静
    丢掉，那是设计好的容错。但**整个顶层不对**是另一回事：`load()` 会把文件
    挪成 `.bad-*` 再返回空，等于「回滚完榜就空了」，而且要等下一次读盘才发现。
    `databackup.restore()` 的规矩是「任何一份校验不过就一个字节都不写」，
    这一关就是为了让这种备份在写盘之前被拦下来。
    """
    if _buckets_of(document) is None:
        raise ValueError('顶层不是记录表（要 {"format": …, "records": {…}}）')


def load(data_dir=None, log=None):
    """全部记录。文件不在 = 谁都没打通过，给 `{}`。"""
    with _lock():
        return _load_unlocked(path(data_dir), log)


def _load_unlocked(target, log):
    try:
        with open(target, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
    except FileNotFoundError:
        return {}
    except (IOError, OSError, ValueError) as exc:
        _set_aside(target, exc, log)
        return {}
    buckets = _buckets_of(raw)
    if buckets is None:
        _set_aside(target, "顶层不是记录表", log)
        return {}
    return buckets


def _ranked(rows):
    """一个桶 → 按成绩排好的 `[(账号, 行), ...]`。

    排序键 `(seconds, at, username)`：
      1. 用时短的在前；
      2. 同分 → **先打出来的那个人在前**（`at` 小者胜）。这是体育比赛的通行
         口径，也是唯一不会因为改昵称而抖动的第二键；
      3. 再同 → 按账号名，保证**确定性**。并行测试要求两次读出来逐字节一样。

    ★ **不信任盘上的顺序，每次都重排** —— 手改过的文件照样出正确的榜。
    """
    return sorted(rows.items(), key=lambda kv: (kv[1]["seconds"], kv[1]["at"], kv[0]))


def board(quest_id, difficulty, usernames=(), limit=TOP_N, data_dir=None, log=None):
    """一次读盘同时回答两个问题 —— 组一发 `0x0310` 要的恰好就是这两样。

    返回 `(个人 {账号: 秒数}, 全体 [(秒数, 显示名), ...])`。

    ★ **别拆成「查某人」+「查前 N 名」两个入口**：那样一发包要读两次盘、
      拿两次锁，而且两次之间还可能被一次结算写盘插进来，六个座位的成绩和
      榜单就会来自两个不同的时刻。
    """
    rows = _load_unlocked(path(data_dir), log).get(key_of(quest_id, difficulty), {})
    wanted = set(str(name) for name in usernames if name)
    mine = {name: row["seconds"] for name, row in rows.items() if name in wanted}
    top = [(row["seconds"], row["name"] or name) for name, row in _ranked(rows)[:limit]]
    return mine, top


def note_clear(quest_id, difficulty, players, seconds, data_dir=None, log=None, now=None):
    """通关入账。`players = [(账号, 昵称), ...]`，`seconds` 是本局用时（整秒）。

    返回 `{账号: (破纪录类型, 旧成绩 or None)}` —— 类型直接就是 `0x0411`
    业务值索引 9 要填的那个数。

    ## ★★ 为什么判定必须和写盘挤在同一个函数里

    判定用的 `best_before` / `own_before` 全部取自**写之前**的那一份快照，
    而且**快照只取一次**。合作局四个人是同一个 `seconds`：要是边判边写，
    第一个人写进去之后就成了新的全服最好成绩，后三个人一比就变成「没破」——
    可四个人明明是一起打出来的，四个人都该收到「破全服记录」。

    同理，这也是它没法拆成「调用方先 `board()` 查一遍、再 `note_clear()` 写」
    的原因：那两步之间没有锁，另一局的结算插进来就会算错。

    ## 两条不变量（都有用例钉着）

    * `own_before >= best_before` 恒成立（后者是全桶最小）⇒ **类型 2 蕴含会写盘**；
    * `类型 0` ⇔ 不写盘。**并列不覆盖** —— 保住更早的那个 `at`，也避免
      「播报说破了纪录、榜上数字纹丝不动」。
    """
    seconds = max(SECONDS_MIN, min(SECONDS_MAX, int(seconds)))
    stamp = float(now if now is not None else time.time())
    verdicts = {}
    with _lock():
        target = path(data_dir)
        buckets = _load_unlocked(target, log)
        bucket_key = key_of(quest_id, difficulty)
        rows = buckets.get(bucket_key, {})

        # ---- 写前快照，只取这一次 ------------------------------------------
        ranked_before = _ranked(rows)
        best_before = ranked_before[0][1]["seconds"] if ranked_before else None
        own_before = {name: row["seconds"] for name, row in rows.items()}

        touched = False
        for username, nickname in players:
            username = str(username or "")
            if not username:
                continue
            mine = own_before.get(username)
            if best_before is None or seconds < best_before:
                kind = RECORD_GLOBAL
            elif mine is None or seconds < mine:
                kind = RECORD_PERSONAL
            else:
                kind = RECORD_NONE
            verdicts[username] = (kind, mine)
            if kind == RECORD_NONE:
                continue
            # 旧行里不认识的字段原样带过去（铁律 11）。
            row = dict(rows.get(username) or {})
            row["name"] = _clean_name(nickname)
            row["seconds"] = seconds
            row["at"] = stamp
            rows[username] = row
            touched = True

        if not touched:
            return verdicts

        if len(rows) > PLAYERS_MAX:
            rows = dict(_ranked(rows)[:PLAYERS_MAX])
        buckets[bucket_key] = rows
        shopcfg.write_json(target, {"format": FORMAT, "records": buckets})
    return verdicts


def clear(data_dir=None, log=None):
    """全删，返回删掉了几条。**把文件删掉**，不留一个空壳。"""
    with _lock():
        target = path(data_dir)
        buckets = _load_unlocked(target, log)
        count = sum(len(rows) for rows in buckets.values())
        try:
            os.remove(target)
        except FileNotFoundError:
            pass
        return count
