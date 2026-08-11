#!/bin/sh
# 炮炮火枪手服务端 —— 关闭。
#
# 按 logs/server.pid 停；pid 文件丢了就按「命令行里带着本包 server/app.py
# 绝对路径」找，不会误伤这台机器上别人的 Python 进程。
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec sh "$ROOT/tools/serverctl.sh" stop
