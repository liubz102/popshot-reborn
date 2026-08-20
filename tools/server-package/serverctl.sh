#!/bin/sh
# ---------------------------------------------------------------------------
#  serverctl.sh —— 【服务端包】里 Linux 侧的启停实现。
#
#  被包根目录的 start.sh / start-debug.sh / stop.sh 调用：
#      sh tools/serverctl.sh start
#      sh tools/serverctl.sh start --verbose
#      sh tools/serverctl.sh stop
#
#  纯 POSIX sh，不依赖 bash。行尾必须是 LF —— 带上 CR 会直接
#  `bad interpreter: /bin/sh^M`（CLAUDE.md 铁律 3）。
# ---------------------------------------------------------------------------
set -u

ACTION="${1:-}"
VERBOSE="${2:-}"

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$HERE/.." && pwd)
cd "$ROOT" || exit 1

LOGDIR="$ROOT/logs"
PIDFILE="$LOGDIR/server.pid"
APP="$ROOT/server/app.py"
CONFIG="$ROOT/server.config"

# ★★ 端口号唯一的源是 server/config.py。这里先留占位，等下面确定了 $PY
#    之后再向它要（read_ports）—— 以前这三个数字在四个文件里各有一份，
#    那是一类「改了这边没改那边」的故障，而且症状通常不是报错。
AUTH_PORT=47611     # 占位，read_ports 会覆盖
GAME_PORT=27799     # 占位，read_ports 会覆盖
RELAY_PORT=27798    # 占位，read_ports 会覆盖

die() {
    echo ""
    echo "[失败] $*" >&2
    exit 1
}

# --- 挑一个 Python ---------------------------------------------------------
# 优先用包里自带的；没有就用系统的 python3（要求 3.10+）。
#
# ★ 包里的 Linux 运行时是**没有解开的 .tar.gz**，第一次启动时在这里解。
#   打包是在 Windows 上做的，那边解开会丢掉符号链接和可执行位
#   （python3 -> python3.14 这种），解出来根本跑不了。留着压缩包
#   到 Linux 上再解，是唯一能保住文件属性的做法。
pick_python() {
    bundled="$ROOT/runtime-linux/python/bin/python3"
    if [ ! -x "$bundled" ]; then
        archive=$(ls "$ROOT"/runtime-linux/*.tar.gz 2>/dev/null | head -n 1 || true)
        if [ -n "$archive" ]; then
            echo "[运行时] 第一次启动，正在解开包内的 Linux Python：$(basename "$archive")"
            if tar -xzf "$archive" -C "$ROOT/runtime-linux"; then
                echo "[运行时] 解开完成。"
            else
                echo "[运行时] 解压失败，改用系统的 python3。" >&2
            fi
        fi
    fi
    if [ -x "$bundled" ]; then
        PY="$bundled"
        PY_KIND="包内自带"
        return 0
    fi
    PY=$(command -v python3 2>/dev/null || true)
    [ -n "$PY" ] || die "这台机器上没有 python3，服务端包里也没有 runtime-linux/。
       两条路选一条：
         1) apt install python3   /   yum install python3   （3.10 或更新）
         2) 在打包的那台 Windows 上重新打一次带 Linux 运行时的服务端包"
    "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null \
        || die "系统的 python3 太老（需要 3.10+）：$("$PY" -V 2>&1)"
    PY_KIND="系统自带"
}

# 端口号向 server/config.py 要（唯一的源）。读不出来就保留上面的占位值 ——
# 那几个是客户端写死的常量，占位值和源里的一致，退一步也能跑。
read_ports() {
    table=$("$PY" "$ROOT/server/config.py" --ports 2>/dev/null) || return 0
    for line in $table; do
        key=${line%%=*}
        value=${line#*=}
        case "$key" in
            AUTH_PORT)       AUTH_PORT="$value" ;;
            GAME_PORT)       GAME_PORT="$value" ;;
            PEER_RELAY_PORT) RELAY_PORT="$value" ;;
        esac
    done
}

# 注册页端口写在 server.config 里，解析规则和 server/config.py 一致。
read_web_port() {
    WEB_PORT=27810
    [ -f "$CONFIG" ] || return 0
    value=$(sed -e 's/\r$//' "$CONFIG" \
        | grep -iE '^[[:space:]]*local_register_port[[:space:]]*=' \
        | tail -n 1 | cut -d= -f2 | tr -d '[:space:]')
    case "$value" in
        ''|*[!0-9]*) ;;
        *) WEB_PORT="$value" ;;
    esac
}

port_open() {
    "$PY" -c "import socket,sys
s = socket.socket(); s.settimeout(0.5)
sys.exit(0 if s.connect_ex(('127.0.0.1', $1)) == 0 else 1)" 2>/dev/null
}

# UDP 端口被占着没有？**只能用 bind 判**（UDP 没有连接，connect_ex 恒为 0）。
# 位置数据走 UDP 27799 —— 和游戏服 TCP 同号，但那是两套独立的端口空间。
udp_port_taken() {
    "$PY" -c "import socket,sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.bind(('0.0.0.0', $1))
except OSError:
    sys.exit(0)
sys.exit(1)" 2>/dev/null
}

running_pid() {
    [ -f "$PIDFILE" ] || return 1
    pid=$(cat "$PIDFILE" 2>/dev/null | tr -d '[:space:]')
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

# ---------------------------------------------------------------------------
#  stop
# ---------------------------------------------------------------------------
if [ "$ACTION" = "stop" ]; then
    echo ""
    echo "=== 炮炮火枪手服务端 —— 关闭 ==="
    echo ""
    pid=$(running_pid || true)
    if [ -z "$pid" ]; then
        # ★ 只按「命令行里带着本包的 app.py 绝对路径」找，不按 python 找 ——
        #   这台机器上可能还有别人的 Python 在跑。
        pid=$(pgrep -f "$APP" 2>/dev/null | head -n 1 || true)
    fi
    if [ -z "$pid" ]; then
        echo "本来就没在跑，无事可做。"
        rm -f "$PIDFILE"
        exit 0
    fi
    echo "[服务端] 停止 pid=$pid"
    kill "$pid" 2>/dev/null || true
    i=0
    while [ "$i" -lt 10 ]; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
        i=$((i + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "[服务端] 还没退，强制 kill -9"
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PIDFILE"
    if kill -0 "$pid" 2>/dev/null; then
        die "停不掉 pid=$pid，请手工处理"
    fi
    echo ""
    echo "已关闭。玩家的连接会一起断开。"
    exit 0
fi

if [ "$ACTION" != "start" ]; then
    die "用法：sh tools/serverctl.sh start|stop [--verbose]"
fi

# ---------------------------------------------------------------------------
#  start
# ---------------------------------------------------------------------------
MODE="精简"
[ "$VERBOSE" = "--verbose" ] && MODE="调试"

echo ""
echo "=== 炮炮火枪手服务端 —— 启动（日志模式：$MODE）==="
if [ "$VERBOSE" = "--verbose" ]; then
    echo "    调试模式：逐包 hexdump + 每条连接一对抓包文件，日志按 MB 涨。"
    echo "    排查完请换回 start.sh，别长期开着。"
fi
echo ""

[ -f "$APP" ] || die "找不到 $APP —— 服务端包不完整。"
pick_python
read_ports
read_web_port
mkdir -p "$LOGDIR"

pid=$(running_pid || true)
if [ -n "$pid" ]; then
    echo "[提示] 服务端已经在跑了（pid=$pid）。要重启请先 sh stop.sh。"
    exit 1
fi

# 启动前把每一个要绑的端口都查一遍（TCP + UDP），有一个被占着就不启动。
# ★ UDP 那条（位置数据）被占时不会有任何报错 —— 服务端照样起来、玩家照样
#   能玩，只是位置数据全部投进黑洞。所以宁可现在就硬失败。
for p in "$AUTH_PORT" "$GAME_PORT" "$RELAY_PORT" "$WEB_PORT"; do
    if port_open "$p"; then
        echo "!! 端口 TCP $p 被占用，服务端无法启动。"
        echo "   如果是上一次启动的服务端，先 sh stop.sh 再来；"
        echo "   如果是别的程序占了 $WEB_PORT，改 server.config 的 local_register_port。"
        exit 1
    fi
done
if udp_port_taken "$GAME_PORT"; then
    echo "!! 端口 UDP $GAME_PORT（位置同步）被占用，服务端无法启动。"
    echo "   它和游戏服 TCP $GAME_PORT 同号，但这是两套独立的端口空间。"
    echo "   查占用者： ss -lunp | grep $GAME_PORT   （或 lsof -iUDP:$GAME_PORT）"
    exit 1
fi

# ★ --no-control：调试控制通道（27800）在服务端包里默认关闭。
#   它能直接往任意连接推包，只该在开发机上开。
if [ "$VERBOSE" = "--verbose" ]; then
    nohup "$PY" "$APP" --no-control --verbose >"$LOGDIR/server.out" 2>"$LOGDIR/server.err" &
else
    nohup "$PY" "$APP" --no-control >"$LOGDIR/server.out" 2>"$LOGDIR/server.err" &
fi
NEWPID=$!
echo "$NEWPID" > "$PIDFILE"

ok=0
i=0
while [ "$i" -lt 30 ]; do
    if ! kill -0 "$NEWPID" 2>/dev/null; then
        break
    fi
    up=0
    for p in "$AUTH_PORT" "$GAME_PORT" "$RELAY_PORT" "$WEB_PORT"; do
        port_open "$p" && up=$((up + 1))
    done
    if [ "$up" -eq 4 ]; then ok=1; break; fi
    sleep 1
    i=$((i + 1))
done

if [ "$ok" -ne 1 ]; then
    echo "[启动失败] 端口没起全，下面是 logs/server.err 和 logs/server.out 的末尾："
    tail -n 20 "$LOGDIR/server.err" 2>/dev/null
    tail -n 20 "$LOGDIR/server.out" 2>/dev/null
    rm -f "$PIDFILE"
    exit 1
fi

echo "[服务端] 已启动（pid=$NEWPID，Python：$PY_KIND $("$PY" -V 2>&1)）"
echo ""
echo "  监听端口"
echo "    $AUTH_PORT   认证服（客户端写死，不可改）"
echo "    $GAME_PORT   游戏服（客户端写死，不可改）"
echo "    $RELAY_PORT   战斗同步中继"
echo "    $WEB_PORT   用户注册页  ->  http://127.0.0.1:$WEB_PORT/"
echo ""

ADDRS=$(ip -4 -o addr show scope global 2>/dev/null | awk '{split($4, a, "/"); print a[1]}')
[ -n "$ADDRS" ] || ADDRS=$(hostname -I 2>/dev/null || true)
if [ -n "$ADDRS" ]; then
    echo "  玩家那边 server.config 里填这个地址："
    for a in $ADDRS; do echo "    server_address = $a"; done
    echo "  云主机请填【公网】IP 或域名，上面列出的多半是内网地址。"
else
    echo "  没能自动识别本机地址，玩家那边填这台机器的实际 IP 或域名。"
fi
echo ""
echo "  ★ 云主机还要在【安全组/防火墙】里放行这四个 TCP 端口，"
echo "    只在系统里开是不够的："
echo "      ufw:       sudo ufw allow $AUTH_PORT,$GAME_PORT,$RELAY_PORT,$WEB_PORT/tcp"
echo "      firewalld: sudo firewall-cmd --add-port=$AUTH_PORT/tcp --add-port=$GAME_PORT/tcp --add-port=$RELAY_PORT/tcp --add-port=$WEB_PORT/tcp --permanent && sudo firewall-cmd --reload"
echo ""
echo "  日志"
echo "    logs/online.log   谁连上、谁断开、从哪个 IP、在线多久（精简模式也照记）"
echo "    logs/server.out   服务端全部输出（每次启动会被覆盖）"
echo "    ★ 玩家说进不去，先看 logs/online.log。"
echo ""
echo "  服务端在后台跑，关掉这个终端不影响它。要停请执行 sh stop.sh。"
exit 0
