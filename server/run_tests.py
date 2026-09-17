#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑全部服务端测试。

    python server/run_tests.py              # 默认**并行**，按 CPU 数开子进程
    python server/run_tests.py -j1          # 串行，和并行化之前逐字一致
    python server/run_tests.py test_shop    # 只跑这几个模块（也能写 模块.类）

★ 为什么要有这个文件：内置的便携 Python（`runtime\\python`）带着
`python314._pth`，里面的 `.` 指的是 **python.exe 所在目录**，不是当前工作目录，
所以 `..\\runtime\\python\\python.exe -m unittest test_gameserver` 会
`ModuleNotFoundError`。这里显式把 `server/` 放进 `sys.path` 再跑，
系统 Python 和便携 Python 就用同一条命令了。

## 为什么默认并行（用户 2026-09-14）

全量已经三千五百多条。串行实测：3.14 **154 秒**、Win7 那套 3.8 运行时
**279 秒**（3.8 没有卡死，就是慢 1.8 倍）。两套各跑一遍七八分钟 ——
改一行前端也要等这么久，一天下来全耗在等上。

按 **TestCase 类**切片丢给子进程跑，3.14 实测 **40 秒**（3.9×）。
粒度取「类」而不是「模块」的理由：`test_botsync` 一个模块就占了全量的
三分之一，按模块切它一个人就是地板。

进程之间天然隔离，靠的是本来就有的三件事，**不是**为并行开的后门：

* `shopcfg.DATA_DIR` 每个进程指到自己的临时目录（见 `_prepare`）；
* 测试开的 socket 一律 `bind((host, 0))` 要临时端口，没有端口字面量；
* 没有任何测试往仓库里写文件（读 `hook/*.c` 那几条是只读的）。

★ **加新测试时这三条要继续成立**，否则并行会随机红 —— 而随机红比慢更贵。
真有非独占不可的测试，把它单独留在一个类里，那个类整个是一片。

## 分片顺序是上一轮自己量出来的，不是写死的常量

最慢的那一片要**最先发**，否则它排到末尾就成了纯拖尾。谁最慢这件事
每次跑完都是已知的，所以跑完把每片耗时写进 `logs/.test_shards.json`，
下一轮照它从慢到快发。第一次跑（或者换了机器）没有这份账就按原顺序发，
结果一样对，只是排布差一点。**不写死任何「某某模块很慢」的表。**

## 子进程的输出

并行时每个分片的 stdout 单独收着，**只有这一片红了才打出来**。理由：
全量一轮本来就有五千行 `[online]` / `[web]` 日志，八个进程交织在一起
谁也读不了；而真正要看的是失败那条的上下文。
串行（`-j1`）照旧原样往 stdout 写，一个字节都没变。
"""
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MODULES = ("test_atomicfile",
           "test_account_store", "test_savecrypt", "test_gameserver", "test_online",
           "test_lobby", "test_room", "test_battle", "test_bot",
           "test_botsync", "test_botmove", "test_botmotion", "test_botnav", "test_botcombat",
           "test_botbreak",
           "test_mapdata",
           "test_weapondata",
           "test_chrprops",
           "test_shopdata", "test_shopcfg", "test_shop", "test_cfgmerge",
           "test_sellprice", "test_cards", "test_cardtext", "test_cardfair",
           "test_web_admin", "test_admindirty", "test_backup", "test_logpack",
           "test_crashwatch", "test_crashstore",
           "test_ballistics",
           "test_relayserver", "test_proxy",
           "test_latency", "test_logs", "test_asynclog",
           "test_udpsync", "test_ports", "test_notice",
           "test_versioning", "test_update", "test_hookintegrity",
           "test_roomclock")


#: 测试期间 `shopcfg` 指向的空目录。★ 必须一直活着（`TemporaryDirectory`
#: 一被回收就把目录删了），所以挂在模块上，不放局部变量里。
_EMPTY_DATA_DIR = None

#: 上一轮每个分片跑了多久。只用来排发送顺序，丢了不影响正确性。
_SHARD_TIMES = os.path.join(os.path.dirname(HERE), "logs", ".test_shards.json")

#: 子进程模式的内部开关。命令行上别手敲。
_WORKER_FLAG = "--worker"


def _prepare():
    """每个真正跑测试的进程都要先做这一套（串行的主进程 / 并行的每个子进程）。"""
    # 测试不该往 `logs\online.log` 里写东西 —— 那是真实的上下线流水。
    import eventlog
    eventlog.configure(to_file=False)

    # ★★ 同一个道理：测试也不该**读**开发机上那份真的 `server/data/*.json`
    #    —— 那是用户随时在改的运营配置。读它的话，「这一局掉不掉材料」
    #    就取决于跑测试的人昨天把掉率调成了多少，测试当场变成随机的
    #    （踩过：`send_end_game` 里加了掉落之后，全量测试时红时绿）。
    #    指到一个**空目录**上 ⇒ 默认「什么都没配」，确定；要具体规则的用例
    #    自己临时改 `shopcfg.DATA_DIR`（`test_gameserver.MaterialDropTests`
    #    和 `test_web_admin` 是样板）。
    global _EMPTY_DATA_DIR
    import shopcfg
    import tempfile
    _EMPTY_DATA_DIR = tempfile.TemporaryDirectory()
    shopcfg.DATA_DIR = _EMPTY_DATA_DIR.name
    shopcfg.invalidate()


# ---------------------------------------------------------------------------
# 分片
# ---------------------------------------------------------------------------
def _shards(names):
    """把模块名摊成 `模块.类` 的分片。已经带点的名字原样放行。"""
    import importlib
    out = []
    for name in names:
        if "." in name:
            out.append(name)
            continue
        try:
            module = importlib.import_module(name)
        except Exception:
            # ★ import 不动（改坏了 / 名字打错了）**不许把父进程带走** ——
            #   整片原样交给 worker，让 `loadTestsFromNames` 照串行那样
            #   把它报成一条错误用例，报告的形状两种跑法就一致了。
            out.append(name)
            continue
        classes = sorted(
            attr for attr, value in vars(module).items()
            if isinstance(value, type) and issubclass(value, unittest.TestCase)
            # ★ `__module__` 这一条不是多余的：`test_botbreak` 从
            #   `test_mapdata` import 了 `BreakableTerrainTests`，不判它
            #   那 5 条会被算成两片跑两遍（串行至今就是跑两遍的）。
            and value.__module__ == name)
        if classes:
            out.extend("%s.%s" % (name, cls) for cls in classes)
        else:
            out.append(name)
    return out


def _load_shard_times():
    try:
        with open(_SHARD_TIMES, "r", encoding="utf-8") as fp:
            times = json.load(fp)
    except (IOError, OSError, ValueError):
        return {}
    return times if isinstance(times, dict) else {}


def _save_shard_times(times, complete):
    """跑完把这一轮的耗时记下来，给下一轮排顺序用。写不进去就算了。

    `complete` = 这一轮跑的是不是**全部**分片：

    * 是 ⇒ 直接覆盖。这一轮的名单就是完整名单，改过名 / 删掉的类
      正好在这里被清出去，否则旧名字会在账本里永远躺着。
    * 否（只跑了几个模块）⇒ **并进**旧账，别把没跑的那些抹掉，
      不然下一次全量又退回没有顺序的状态。
    """
    merged = times if complete else dict(_load_shard_times(), **times)
    try:
        os.makedirs(os.path.dirname(_SHARD_TIMES), exist_ok=True)
        with open(_SHARD_TIMES, "w", encoding="utf-8") as fp:
            json.dump(merged, fp, indent=1, sort_keys=True)
    except (IOError, OSError):
        pass


# ---------------------------------------------------------------------------
# 子进程
# ---------------------------------------------------------------------------
def _worker():
    """stdin 一行一个分片名，跑完往**协议管道**回一行 JSON。

    ★ 协议为什么不直接写 stdout：测试自己就在往 stdout 写日志
    （`eventlog` / `[web]` 那些）。所以开工第一件事是把 fd 1 复制一份留给
    协议，再把 fd 1 指到黑洞 —— 底下怎么写都污染不到协议。
    JSON 走 `ensure_ascii`（默认），中文全转义成 `\\uXXXX`，
    这样管道是什么代码页都无所谓。
    """
    import contextlib
    protocol = os.fdopen(os.dup(1), "w", encoding="ascii", newline="\n")
    sink = open(os.devnull, "w")
    os.dup2(sink.fileno(), 1)
    _prepare()
    loader = unittest.defaultTestLoader
    while True:
        # ★ 一行一取，不用 `for line in sys.stdin` —— 那是带预读的迭代协议，
        #   两边都在等对方先说话时最容易在这里对死。
        line = sys.stdin.readline()
        shard = line.strip()
        if not line or not shard:
            break
        captured = io.StringIO()
        began = time.time()
        with contextlib.redirect_stdout(captured):
            # ★ 两个流分开：`captured` 只装**被测代码自己**打的东西
            #   （`[online]` / `[web]` 那些），runner 的「Ran N tests / OK」
            #   丢掉 —— 汇总由父进程统一打一份，每片再打一份只会自相矛盾。
            result = unittest.TextTestRunner(
                verbosity=0, stream=io.StringIO()).run(
                    loader.loadTestsFromNames([shard]))
        bad = ([["FAIL", str(c), t] for c, t in result.failures]
               + [["ERROR", str(c), t] for c, t in result.errors])
        protocol.write(json.dumps({
            "shard": shard,
            "seconds": time.time() - began,
            "ran": result.testsRun,
            "skipped": len(result.skipped),
            "bad": bad,
            # 只有红了才把这一片的日志带回去 —— 绿的那些没人会看。
            "log": captured.getvalue() if bad else "",
        }) + "\n")
        protocol.flush()
    return 0


# ---------------------------------------------------------------------------
# 两条跑法
# ---------------------------------------------------------------------------
def _run_serial(names):
    """并行化之前那条路，逐字未变。"""
    _prepare()
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def _run_parallel(names, jobs, complete):
    # ★ 父进程只负责数分片、发分片，自己一条测试都不跑；但**摊分片要 import
    #   测试模块**，所以同样得先把 `shopcfg` 指到空目录上再 import。
    _prepare()
    shards = _shards(names)
    # 上一轮慢的先发。没有账（第一次跑 / 换了机器）就按原顺序。
    known = _load_shard_times()
    shards.sort(key=lambda s: -known.get(s, 0.0))
    jobs = max(1, min(jobs, len(shards)))

    pending = queue.Queue()
    for shard in shards:
        pending.put(shard)

    results = []
    collected = threading.Lock()
    began = time.time()

    def pump():
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), _WORKER_FLAG],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            universal_newlines=True, encoding="ascii", errors="replace",
            cwd=HERE)
        try:
            while True:
                try:
                    shard = pending.get_nowait()
                except queue.Empty:
                    return
                child.stdin.write(shard + "\n")
                child.stdin.flush()
                line = child.stdout.readline()
                if not line:
                    # 子进程死在这一片上（解释器崩了之类）。剩下的分片留给
                    # 别的 worker，这一片记成错误 —— 绝不能当没发生过。
                    with collected:
                        results.append({
                            "shard": shard, "seconds": 0.0, "ran": 0,
                            "skipped": 0, "log": "",
                            "bad": [["ERROR", shard,
                                     "子进程没有回结果就退出了（返回码 %s）"
                                     % child.poll()]]})
                    return
                with collected:
                    results.append(json.loads(line))
        finally:
            try:
                child.stdin.close()
            except (IOError, OSError, ValueError):
                pass
            child.wait()

    pumps = [threading.Thread(target=pump, name="shard-%d" % i)
             for i in range(jobs)]
    for thread in pumps:
        thread.start()
    for thread in pumps:
        thread.join()
    elapsed = time.time() - began

    # 没被任何 worker 领走的分片（所有 worker 都提前死了才会发生）。
    missed = []
    while True:
        try:
            missed.append(pending.get_nowait())
        except queue.Empty:
            break

    _save_shard_times({r["shard"]: r["seconds"] for r in results}, complete)

    ran = sum(r["ran"] for r in results)
    skipped = sum(r["skipped"] for r in results)
    bad = [entry for r in sorted(results, key=lambda r: r["shard"])
           for entry in r["bad"]]
    for result in sorted(results, key=lambda r: r["shard"]):
        if result["bad"] and result["log"]:
            sys.stdout.write("\n===== %s 的输出 =====\n%s"
                             % (result["shard"], result["log"]))
    for kind, name, text in bad:
        sys.stdout.write("\n" + "=" * 70 + "\n%s: %s\n%s\n%s"
                         % (kind, name, "-" * 70, text))
    for shard in missed:
        sys.stdout.write("\n没跑成的分片：%s\n" % shard)

    # ★ 末尾四行的形状和 `unittest` 逐字一致（`Ran … / 空行 / OK`）——
    #   `... | tail -4` 是现成的验收姿势，别把并行的细节掺进那几行。
    sys.stdout.write("\n（并行：%d 个进程，%d 片）\n" % (jobs, len(shards)))
    sys.stdout.write("-" * 70 + "\n")
    sys.stdout.write("Ran %d test%s in %.3fs\n"
                     % (ran, "" if ran == 1 else "s", elapsed))
    sys.stdout.write("\n")
    if bad or missed:
        failures = sum(1 for kind, _, _ in bad if kind == "FAIL")
        errors = len(bad) - failures + len(missed)
        tally = ["failures=%d" % failures] if failures else []
        if errors:
            tally.append("errors=%d" % errors)
        sys.stdout.write("FAILED (%s)\n" % ", ".join(tally))
        return 1
    sys.stdout.write("OK%s\n" % (" (skipped=%d)" % skipped if skipped else ""))
    return 0


def main():
    argv = sys.argv[1:]
    if argv[:1] == [_WORKER_FLAG]:
        return _worker()

    jobs = os.cpu_count() or 4
    names = []
    for arg in argv:
        if arg.startswith("-j"):
            jobs = int(arg[2:] or "0") or (os.cpu_count() or 4)
        elif arg.startswith("--jobs="):
            jobs = int(arg[len("--jobs="):])
        else:
            names.append(arg)
    complete = not names          # 没点名 = 跑全部，账本可以整份覆盖
    names = names or list(MODULES)

    if jobs <= 1:
        return _run_serial(names)
    return _run_parallel(names, jobs, complete)


if __name__ == "__main__":
    sys.exit(main())
