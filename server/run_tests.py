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
           "test_lobby", "test_room", "test_battle", "test_relayserver",
           "test_proxy", "test_latency")


def main():
    # 测试不该往 `logs\online.log` 里写东西 —— 那是真实的上下线流水。
    import eventlog
    eventlog.configure(to_file=False)
    names = sys.argv[1:] or list(MODULES)
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
