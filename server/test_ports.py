#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端口号只有**一个**来源 —— 这个文件盯着别再长出第二个。

## 为什么值得一个专门的测试文件

同一个端口号曾经在四个地方各写一遍（`server/config.py`、`hook/bshook.c`、
`tools/launch.ps1`、`tools/server-package/serverctl.*`），四种语言，靠环境变量
在运行时对齐。那类「改一半」的故障有个共同的坏毛病：**症状不是报错，
而是某个功能悄悄不工作**。位置数据那条 UDP 通道尤其典型 —— 端口对不上时
它没有任何回执，服务端照常起、玩家照常玩，只是数据全投进黑洞。

所以现在：`server/config.py` 是唯一的源，`hook/ports.h` 和
`updater/src/ports.h` 由 `tools/gen_ports_h.py` 生成，PowerShell / sh 用
`python config.py --ports` 问。下面这几条把这个结构钉死。

⚠ 这些用例依赖仓库布局（`hook/`、`tools/`），发布包里没有它们 ——
   而发布包里也没有 `test_*.py`（`tools/build-common.ps1` 会剔掉），所以不冲突。
"""
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config as server_config                                  # noqa: E402

ROOT = os.path.dirname(HERE)
HEADER = os.path.join(ROOT, "hook", "ports.h")
BSHOOK = os.path.join(ROOT, "hook", "bshook.c")
UPDATER_HEADER = os.path.join(ROOT, "updater", "src", "ports.h")
GENERATOR = os.path.join(ROOT, "tools", "gen_ports_h.py")


def repo_file(path):
    if not os.path.exists(path):
        raise unittest.SkipTest(f"不在源码仓库里（缺 {path}），跳过")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


class GeneratedHeaderTests(unittest.TestCase):
    def test_the_header_matches_config_py(self):
        """★ `hook/ports.h` 必须和 `server/config.py` 一致。

        红了就是有人改了 `config.py` 没重新生成 —— 跑一次
        `python tools/gen_ports_h.py` 即可（`hook/build.bat` 每次编译也会跑）。
        """
        text = repo_file(HEADER)
        found = dict(re.findall(r"#define\s+POPSHOT_(\w+)\s+(\d+)", text))
        want = {k: str(v) for k, v in server_config.port_table().items()}
        self.assertEqual(found, want)

    def test_updater_header_matches_config_py(self):
        """★ `updater/src/ports.h`（更新器探针用的游戏服端口）也要一致。

        更新器只需要 GAME_PORT 一个号，但一样只认 server/config.py 这个源
        （分叉 = 探针连错端口 = 自动更新整条链失灵）。
        """
        text = repo_file(UPDATER_HEADER)
        found = dict(re.findall(r"#define\s+POPSHOT_(\w+)\s+(\d+)", text))
        self.assertEqual(found,
                         {"GAME_PORT": str(server_config.GAME_PORT)})

    def test_the_generator_reports_it_is_up_to_date(self):
        """生成器自己的 `--check` 也要说「最新」（它是 build.bat 的守门人）。"""
        if not os.path.exists(GENERATOR):
            self.skipTest("不在源码仓库里")
        result = subprocess.run([sys.executable, GENERATOR, "--check"],
                                capture_output=True)
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr).decode("utf-8", "replace"))


class NoSecondCopyTests(unittest.TestCase):
    """C 那边不许再出现端口字面量。"""

    def test_bshook_takes_every_port_from_the_header(self):
        text = repo_file(BSHOOK)
        self.assertIn('#include "ports.h"', text,
                      "bshook.c 必须包含生成的 ports.h")
        # 端口全局变量的初值必须是 POPSHOT_* 宏，不能是数字
        for line in text.splitlines():
            m = re.match(r"\s*static unsigned (g_\w*port\w*)\s*=\s*(.+?);", line)
            if not m:
                continue
            name, value = m.groups()
            self.assertTrue(value.startswith("POPSHOT_"),
                            f"{name} 又写死了字面量 {value!r} —— "
                            f"端口只能来自 ports.h（源头是 server/config.py）")

    def test_the_alignment_only_env_vars_are_gone(self):
        """★ 那批「只为把常量从 PowerShell 抄给 C」的环境变量已经删干净了。

        留着它们等于把「一个常量」变成「两处配置 + 一条传递链」，
        而玩家改错任何一处都不会得到报错。
        """
        text = repo_file(BSHOOK)
        for name in ("POPSHOT_RELAY_AUTH_PORT", "POPSHOT_RELAY_GAME_PORT",
                     "POPSHOT_RELAY_PEER_PORT", "POPSHOT_PEER_RELAY_PORT",
                     "POPSHOT_RELAY_UDP_SYNC_PORT", "POPSHOT_CLIENT_UDP_PORT"):
            self.assertNotIn(f'env_uint("{name}"', text,
                             f"{name} 不该再从环境变量读了")

    def test_the_two_configurable_ports_still_come_from_the_config_file(self):
        """⚠ 反过来：注册页那两个端口**真的**来自玩家的 server.config，
        它们必须**继续**走环境变量，不能被这一轮统一给误伤。"""
        text = repo_file(BSHOOK)
        self.assertIn('env_uint("POPSHOT_SERVER_REG_PORT"', text)
        self.assertIn('env_uint("POPSHOT_LOCAL_REG_PORT"', text)


class PortTableTests(unittest.TestCase):
    def test_the_cli_prints_every_exported_port(self):
        """`python config.py --ports` 是 PowerShell / sh 拿端口的唯一入口。"""
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "config.py"), "--ports"],
            capture_output=True)
        self.assertEqual(result.returncode, 0)
        printed = dict(
            line.split("=", 1)
            for line in result.stdout.decode("utf-8").splitlines() if "=" in line)
        self.assertEqual(printed,
                         {k: str(v) for k, v in server_config.port_table().items()})

    def test_the_udp_channel_shares_the_game_server_number(self):
        """位置数据的 UDP 口和游戏服 TCP **故意同号**（防火墙少记一个数）。

        这条钉住的是那个「同号」的承诺：谁要是把 UDP_SYNC_PORT 单独改掉，
        README 和启动横幅里那句话就不成立了。
        """
        self.assertEqual(server_config.UDP_SYNC_PORT, server_config.GAME_PORT)
        self.assertEqual(server_config.RELAY_UDP_SYNC_PORT,
                         server_config.RELAY_GAME_PORT)

    def test_the_client_udp_port_is_not_the_original_one(self):
        """★ 换掉原版那个 7788 正是这一轮的重点：换成我们自己的号之后，
        「这个口是不是游戏在听」才能由启动脚本 + bshook 一起给出确定答案。"""
        self.assertNotEqual(server_config.CLIENT_UDP_PORT,
                            server_config.GAME_ORIGINAL_UDP_PORT)

    def test_no_two_services_want_the_same_tcp_port(self):
        """所有 **TCP** 端口两两不同 —— 撞号会变成开机才发现的启动失败。"""
        table = server_config.port_table()
        tcp = {name: port for name, port in table.items()
               if name not in ("UDP_SYNC_PORT", "RELAY_UDP_SYNC_PORT",
                               "GAME_ORIGINAL_UDP_PORT", "CLIENT_UDP_PORT")}
        self.assertEqual(len(set(tcp.values())), len(tcp),
                         f"TCP 端口撞号了: {sorted(tcp.items(), key=lambda kv: kv[1])}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
