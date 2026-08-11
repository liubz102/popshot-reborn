#!/bin/sh
# 炮炮火枪手服务端 —— 启动（日常开服，精简日志）。
#
#   sh start.sh          解压后没有执行权限时这样跑
#   ./start.sh           chmod +x *.sh 之后可以直接跑
#
# 排查问题请改用 start-debug.sh（逐包 hexdump，日志按 MB 涨）。
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec sh "$ROOT/tools/serverctl.sh" start
