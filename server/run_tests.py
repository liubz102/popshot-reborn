#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑全部服务端测试。

    python server/run_tests.py

★ 为什么要有这个文件：内置的便携 Python（`runtime\\python`）带着
`python314._pth`，里面的 `.` 指的是 **python.exe 所在目录**，不是当前工作目录，
所以 `..\\runtime\\python\\python.exe -m unittest test_gameserver` 会
`ModuleNotFoundError`。这里显式把 `server/` 放进 `sys.path` 再跑，
系统 Python 和便携 Python 就用同一条命令了。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MODULES = ("test_account_store", "test_gameserver", "test_online",
           "test_lobby", "test_room", "test_battle", "test_bot",
           "test_botsync", "test_botmove", "test_botnav", "test_botcombat",
           "test_botbreak",
           "test_mapdata",
           "test_weapondata",
           "test_chrprops",
           "test_shopdata", "test_shopcfg", "test_web_admin",
           "test_ballistics",
           "test_relayserver", "test_proxy",
           "test_latency", "test_logs", "test_asynclog",
           "test_udpsync", "test_ports",
           "test_versioning", "test_roomclock")


#: 测试期间 `shopcfg` 指向的空目录。★ 必须一直活着（`TemporaryDirectory`
#: 一被回收就把目录删了），所以挂在模块上，不放局部变量里。
_EMPTY_DATA_DIR = None


def main():
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

    names = sys.argv[1:] or list(MODULES)
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
