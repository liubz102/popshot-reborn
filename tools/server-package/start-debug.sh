#!/bin/sh
# 炮炮火枪手服务端 —— 启动（排查用，逐包 hexdump）。
#
# 和 start.sh 只差一个 --verbose。日志按 MB 涨，别长期开着。
# 连接流水 logs/online.log 两种模式下都记，和 --verbose 无关。
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec sh "$ROOT/tools/serverctl.sh" start --verbose
