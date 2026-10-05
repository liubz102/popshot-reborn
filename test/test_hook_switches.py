#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bshook 里不许有「退回原版行为」的开关（用户 2026-10-06，X_Mod D106）。

用户原话：「改了就是改了，加个开关把原版 bug 找回来有什么意义？这种永远用不着的
开关就是垃圾代码。」—— 以前 agent 擅自加过 26 个（`BSHOOK_KEEP_*` 23 个、
`BSHOOK_NO_*` 3 个），2026-10-06 全删了。这个文件钉住它们不回来。

hook 还允许读的环境变量：
* `BSHOOK_*` 只许诊断类 —— `BSHOOK_*_DIAG` / `BSHOOK_VERBOSE_LOG`（多打日志、多装探针），
  外加 `BSHOOK_WEAPON_MODE`（实机核对自定义武器两套表时强制模式）。都**不改游戏行为**。
* `POPSHOT_*` 是启动脚本把 `config/server.config` 传进来的配置通道（服务器地址 / 端口）。
bsloader ⇄ bshook 握手用的事件名走 `gg_bypass.h` 的宏，不是字面量，不在这里管。

要加新的环境变量：先想清楚它是不是「换个名字的退回开关」—— 是就别加。
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, "hook")

ALLOWED_BSHOOK = re.compile(r"^BSHOOK_(?:[A-Z]+_DIAG|VERBOSE_LOG|WEAPON_MODE)$")
ENV_READ = re.compile(r'(?:GetEnvironmentVariable[AW]?|env_uint)\(\s*L?"([^"]+)"')


def hook_sources():
    if not os.path.isdir(HOOK):
        raise unittest.SkipTest("不在源码仓库里（缺 hook/），跳过")
    out = {}
    for name in sorted(os.listdir(HOOK)):
        if name.endswith((".c", ".h", ".inc")):
            with open(os.path.join(HOOK, name), encoding="utf-8") as f:
                out[name] = f.read()
    return out


class NoRevertSwitchTest(unittest.TestCase):

    def test_no_keep_or_no_switch_anywhere_in_the_hook(self):
        for name, text in hook_sources().items():
            hits = sorted(set(re.findall(r"(?:BSHOOK|POPSHOT)_(?:KEEP|NO)_[A-Z0-9_]+", text)))
            self.assertEqual([], hits, "%s 里又出现了退回原版的开关：%s" % (name, hits))

    def test_every_env_var_the_hook_reads_is_a_diagnostic_or_config(self):
        names = {(name, m.group(1)) for name, text in hook_sources().items()
                 for m in ENV_READ.finditer(text)}
        self.assertTrue(names, "一个都没扫到 —— 正则坏了？")
        bad = sorted("%s: %s" % n for n in names
                     if not (ALLOWED_BSHOOK.match(n[1]) or n[1].startswith("POPSHOT_")))
        self.assertEqual([], bad, "hook 读了不在白名单里的环境变量（是不是换了名字的退回开关？）")


if __name__ == "__main__":
    unittest.main()
