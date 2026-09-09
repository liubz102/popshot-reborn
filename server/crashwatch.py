#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端崩了就把现场打包传给远程服务器。**只在客户端包的中继进程里跑。**

需求（用户 2026-09-09）：商店 / 合成加了一大批新道具之后出现「某些新道具导致
客户端闪退」，而这种崩溃只有真人在真机上打到那一下才触发。收包那一半在
`crashstore.py`（跑在云服上）。

## 为什么宿主是**中继**而不是本机服务端

`tools/launch.ps1` 每次都同时起本机服务端 `app.py` 和本机中继 `relay.py`，
而「本机服务器 / 远程服务器」是玩家在**游戏登录界面点单选钮**选的（D066）：

* 选「本机服务器」→ 客户端直连 `127.0.0.1:47611/27799`，**中继一个字节都收不到**；
* 选「远程服务器」→ 客户端连 `127.0.0.1:47621/27809`，全部经中继出去。

所以「只在远程模式上传」**不是靠判断，是由构造保证的** —— 中继被调用过就说明
玩家选了远程。V0.2 **D079** 明确禁止「让服务端按连接是不是 loopback 来猜模式」，
这里不做任何推断。

## 判定链：全部事件驱动，一个 `sleep` 都没有（铁律 10）

1. **哪个进程是本次客户端** —— 中继 accept 到来自 `127.0.0.1:<peer>` 的连接后，
   `GetExtendedTcpTable` 按 (本地口 = peer, 远端口 = 中继监听口) 查 owning PID。
   这是 Windows 自己的连接表，**不是猜**。
2. **客户端死了** —— `WaitForSingleObject(hProcess, INFINITE)`。OS 事件，不轮询。
   ★ 顺带消灭了「读到半截文件」的竞态：进程对象被置位 ⇒ 进程已完全终止
   ⇒ 它所有文件句柄都被 OS 关闭 ⇒ `.mdmp` / `.rpt` / `LastCrashReport.txt`
   必定写完且没被占着。**所以这条链上不需要任何等待。**
3. **崩了还是正常退出** —— 看 `Dump/LastCrashReport.txt` 的 mtime 是不是落在
   本次会话之内（会话起点取自同一个进程句柄的 `GetProcessTimes`）。
   判据握在**原版自己的崩溃处理器**手里，不是我们从退出码猜的。
4. **哪个 `.mdmp` 属于这次** —— 崩溃报告里的 `Dump File Name:` 一行直接给出。

## 不阻塞

`note_client()` 由中继的**转发线程**调用，它后面马上要去跑 `_pump` 转发游戏
字节，所以那个函数**只入队就返回**，什么都不做 —— 尤其 `GetExtendedTcpTable`
要遍历整张 TCP 表，绝不能在那条线程上跑。查 PID、等退出、打包、上传全部在
本模块自己的后台线程上（V0.3bot D109 / §150 是同一条教训）。

## 先落盘，再上传

崩溃现场先打包写进 `logs/.crash_pending/`，**上传成功才删**。这样
「玩家崩完立刻 stop.bat」「崩的时候正好断网」都不会丢现场 ——
中继下次启动时先把队列里积压的补传掉。

非 Windows 平台上整个模块退化成 no-op（Linux 的服务端包里也有这个文件，
但那边永远不会有客户端崩溃）。
"""
from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_LOGDIR = os.path.join(ROOT, "logs")
DEFAULT_GAMEDIR = os.path.join(ROOT, "game_patched")

#: 攒着等上传的压缩包放这儿。★ 点开头 ⇒ `logcleanup.is_log_name()` 永不碰它，
#: 而 `logs/` 又在更新器的 `PROTECTED_PATHS` 里、打包时也不进 zip。
PENDING_DIRNAME = ".crash_pending"

#: 本机安装码存这儿（首次运行生成）。放 `logs/` 下的三个理由同上，
#: 其中最要紧的是**打包脚本只在包里建空 `logs/`** —— 否则所有玩家
#: 会共用同一个安装码，那这个 ID 就白加了。
CLIENT_ID_FILENAME = ".client_id"

#: 「上次已经处理过的那一次崩溃」记在这儿，防止重复上传同一份现场。
#: **按状态翻转去重，不按次数/ 时间窗**（铁律 10）。
STATE_FILENAME = ".crash_state.json"

#: 上传路径。收包那一头在 `web/server.py` 的 `CRASH_UPLOAD_PATH`。
UPLOAD_PATH = "/api/crash-report"

#: 重试：失败隔这么多秒再来一次，一共试这么多次。
#:
#: ★★ 这是铁律 10（「禁止固定次数 / 固定时间的阈值」）**唯一允许的例外**：
#:    连不上远端时物理上**没有事件可等** —— 网络恢复不会给我们发通知，
#:    对端也没法告诉我们「我起来了」。这正是铁律里写的那种「等不到事件」的
#:    地方，所以按注释要求在这里说明白。次数和间隔由用户 2026-09-09 指定。
RETRY_DELAY_SECONDS = 3.0
RETRY_ATTEMPTS = 3

#: 单次上传的网络超时（秒）。十几 MB 的包在慢线路上要走一会儿。
UPLOAD_TIMEOUT_SECONDS = 120.0

#: 中继转发线程往里塞连接的那个队列的上限。满了就丢 —— 同一次会话只要有
#: **一条**连接被认出来就够了，丢掉重复的不影响结果。
MAX_PENDING_NOTES = 64

#: 崩溃报告的分隔行。`BigShot.rpt` 是**追加式**的，一份文件里躺着历次崩溃，
#: 「只传最近一次」= 只截最后一块。
RECORD_HEAD = "==================   logged at "
_RECORD_RE = re.compile(r"^=+\s+logged at\s+(.+?)\s+=+\s*$")
_DUMPNAME_RE = re.compile(r"^Dump File Name:\s*(.+?)\s*$", re.MULTILINE)
_VERSION_RE = re.compile(r"^Version:\s*(.+?)\s*$", re.MULTILINE)
_EXCEPTION_RE = re.compile(r"^Exception code:\s*(.+?)\s*$", re.MULTILINE)
_FAULT_RE = re.compile(r"^Fault address:\s*(.+?)\s*$", re.MULTILINE)

#: 账号名白名单。和 `account_store.USERNAME_PATTERN` 允许的字符集一致 ——
#: 所以**能真正登录成功的账号名清洗后原样保留**，被洗掉的一定是
#: 「玩家在登录框里乱打、根本没登上去」的内容。
_ACCOUNT_ALLOWED = re.compile(r"[^A-Za-z0-9_-]")
_ACCOUNT_MAX = 16
ACCOUNT_FALLBACK = "unknown"


# =========================================================== 小工具（可单测）

def sanitize_account(raw):
    """把 `UserConfig.ini` 里的 `LastLoginId` 收拾成能安全拼进路径的一段。

    ★ 它**不是注册账号名**，是玩家在登录框里打进去的任意字符串 ——
    原版客户端登录失败也会写，内容可以是中文、空格、`../`、`*`、`:`、`|`。

    非白名单字符**直接丢弃**而不是替换成 `_`：「小明」变成 `______` 既没有
    信息量又更难认，不如让它落到 `unknown`。
    """
    text = _ACCOUNT_ALLOWED.sub("", str(raw or ""))
    text = text.strip("-_")[:_ACCOUNT_MAX].strip("-_")
    return text or ACCOUNT_FALLBACK


def read_last_login_id(path):
    """读 `game_patched/UserConfig.ini` 里的 `LastLoginId`。读不到回 `""`。

    ★ 用 `latin-1` 解码：这个文件是 2007 年的原版客户端按 **ANSI（中文机器上
    是 CP936）** 写的，不是 UTF-8。`latin-1` 对任何字节都不会抛异常，且
    0~127 与 ASCII 一一对应 —— 而我们只保留 ASCII 白名单里的字符，
    所以「用哪个 codec」这个问题在这里根本不存在。
    """
    try:
        with open(path, "r", encoding="latin-1") as fp:
            for line in fp:
                key, sep, value = line.partition("=")
                if sep and key.strip().lower() == "lastloginid":
                    return value.strip()
    except OSError:
        pass
    return ""


def install_id(logdir=None, generate=True):
    """本机安装码：8 位十六进制，首次运行生成后一直不变。

    `generate=False` 时只读不写（测试和「只想看看」的场合用）。
    """
    logdir = logdir or DEFAULT_LOGDIR
    path = os.path.join(logdir, CLIENT_ID_FILENAME)
    try:
        with open(path, "r", encoding="ascii") as fp:
            got = fp.read().strip().lower()
        if re.fullmatch(r"[0-9a-f]{8}", got):
            return got
    except (OSError, ValueError):
        pass
    if not generate:
        return "00000000"
    new = secrets.token_hex(4)
    try:
        os.makedirs(logdir, exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="ascii", newline="\n") as fp:
            fp.write(new + "\n")
        os.replace(tmp, path)
    except OSError:
        pass                                # 写不下就每次现生成，不值得报错
    return new


def parse_logged_at(text):
    """`"09/09/26, 01:36:42"` -> Unix 时刻。认不出回 `None`。

    格式是 `MM/DD/YY, HH:MM:SS`（原版按美式写法打的）。★ 认不出**绝不**拿这个
    字符串去拼路径 —— 调用方退回用文件 mtime。
    """
    try:
        tm = time.strptime(str(text).strip(), "%m/%d/%y, %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return time.mktime(tm)


def split_records(text):
    """把 `BigShot.rpt` 按 `==== logged at ... ====` 切成一块块，按原顺序返回。"""
    records = []
    current = []
    for line in text.splitlines(True):
        if _RECORD_RE.match(line.rstrip("\r\n")):
            if current:
                records.append("".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        records.append("".join(current))
    return records


def last_record(text):
    """`BigShot.rpt` 的**最后一块**。一块都没有就回 `""`。"""
    records = split_records(text)
    return records[-1] if records else ""


class CrashReport:
    """`Dump/LastCrashReport.txt` 解析出来的东西。"""

    def __init__(self, path, text, mtime):
        self.path = path
        self.text = text
        self.mtime = mtime
        head = _RECORD_RE.match(
            next((ln for ln in text.splitlines()
                  if _RECORD_RE.match(ln.rstrip())), "").rstrip())
        self.logged_at_text = head.group(1) if head else ""
        #: ★ 崩溃时刻优先用报告里写的；认不出（或那一行根本没有）就退回文件
        #:   mtime —— 绝不拿一个畸形字符串去拼目录名。
        self.epoch = parse_logged_at(self.logged_at_text) or mtime
        match = _DUMPNAME_RE.search(text)
        #: ★ 只取 basename：`BigShot.rpt` 里躺着历次崩溃留下的**别的机器、
        #:   别的目录**的绝对路径（`D:\work\popshot\...`），那些路径在这台
        #:   机器上要么不存在、要么指向不相干的东西。
        self.dump_name = os.path.basename(match.group(1)) if match else ""
        #: 原版客户端自己的版本号（报告第二行 `Version: 311`）。
        #: 和我们这一版的 `BUILD.ver` 是两回事，两个都要记。
        version = _VERSION_RE.search(text)
        self.client_version = version.group(1) if version else ""
        exc = _EXCEPTION_RE.search(text)
        self.exception = exc.group(1) if exc else ""
        fault = _FAULT_RE.search(text)
        self.fault = fault.group(1) if fault else ""

    @property
    def stamp(self):
        """目录名里那一段 `YYYYMMDD-HHMMSS`。"""
        return time.strftime("%Y%m%d-%H%M%S", time.localtime(self.epoch))


#: `BUILD.ver` 里对查崩溃真正有用的那几项。
#:
#: ★ **不要把整个文件原样塞进 `meta.json`**：客户端包里那份是一份富 JSON，
#: 带一大段中文 `notes`（几百字节，对查崩溃毫无用处）和打包机器名。
#: 这里只挑「是哪个 build、hook 和加载器是哪一版」——
#: `bshookHash` 尤其要紧：崩溃十有八九和注入层的补丁版本有关。
BUILD_FIELDS = ("version", "versionWire", "kind", "buildId", "time",
                "bshookHash", "bsloaderHash", "serverCodeHash")


def read_build_info(root):
    """`<包根>/BUILD.ver` -> 给崩溃报告用的那几项。

    三种形态都要认：

    * **客户端 / 服务端包**里的那份是富 JSON（`version` / `buildId` / 各种 hash）；
    * **仓库工作区**里那份只有 `{"version": "V0.3.0"}`；
    * 文件不在、或者根本不是 JSON。

    ★ 读不到时**明说读不到**，不要静默回一个空串 —— 看包的人得能分清
    「这个 build 没版本号」和「我们没读着」。
    """
    path = os.path.join(root, "BUILD.ver")
    try:
        with open(path, "r", encoding="utf-8-sig") as fp:
            text = fp.read()
    except OSError as error:
        return {"error": "读不到 BUILD.ver：%s" % (error,)}
    try:
        got = json.loads(text)
    except ValueError:
        # 不是 JSON 也别丢掉 —— 截一段原文，让人自己看。
        return {"error": "BUILD.ver 不是合法 JSON", "raw": text[:200]}
    if not isinstance(got, dict):
        return {"error": "BUILD.ver 不是一个对象", "raw": text[:200]}
    info = {key: got[key] for key in BUILD_FIELDS if key in got}
    return info or {"error": "BUILD.ver 里没有认得的字段"}


def read_crash_report(gamedir):
    """读 `<gamedir>/Dump/LastCrashReport.txt`。没有就回 `None`。"""
    path = os.path.join(gamedir, "Dump", "LastCrashReport.txt")
    try:
        mtime = os.path.getmtime(path)
        # ★ `newline=""`：包里那份要和玩家机器上那份**逐字节相同**，
        #   通用换行会把 `\r\n` 改成 `\n`。
        with open(path, "r", encoding="latin-1", newline="") as fp:
            text = fp.read()
    except OSError:
        return None
    return CrashReport(path, text, mtime)


# ================================================= Windows 专有（延迟 import）

def _kernel32():
    """非 Windows 上回 `None`，让调用方整条退化成 no-op。

    写法照 `roomclock.py` 那段：平台判断 + 延迟 import + 出错就当没有。
    """
    if sys.platform != "win32":
        return None
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def owning_pid(peer_ip, peer_port, local_port):
    """哪个进程开着 `peer_ip:peer_port -> 127.0.0.1:local_port` 这条连接。

    查的是 Windows 自己的 TCP 连接表（`GetExtendedTcpTable`），**不是按进程名
    猜**，所以「同时开着两个 BigShot」「别的程序也叫 BigShot.exe」都不会认错。
    查不到回 `None`。
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class _Row(ctypes.Structure):
        _fields_ = [("dwState", wintypes.DWORD),
                    ("dwLocalAddr", wintypes.DWORD),
                    ("dwLocalPort", wintypes.DWORD),
                    ("dwRemoteAddr", wintypes.DWORD),
                    ("dwRemotePort", wintypes.DWORD),
                    ("dwOwningPid", wintypes.DWORD)]

    def _port(value):
        # 表里的端口是网络字节序，塞在 DWORD 的低 16 位里。
        return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)

    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        want_addr = _ipv4_dword(peer_ip)
        if want_addr is None:
            return None
        size = wintypes.DWORD(0)
        # 第一发只为问「要多大」（必然回 ERROR_INSUFFICIENT_BUFFER）。
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False,
                                     2,      # AF_INET
                                     5,      # TCP_TABLE_OWNER_PID_ALL
                                     0)
        buf = ctypes.create_string_buffer(size.value)
        if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False,
                                        2, 5, 0) != 0:
            return None
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        rows = ctypes.cast(ctypes.byref(buf, ctypes.sizeof(wintypes.DWORD)),
                           ctypes.POINTER(_Row))
        for i in range(count):
            row = rows[i]
            if (_port(row.dwLocalPort) == peer_port
                    and _port(row.dwRemotePort) == local_port
                    and row.dwLocalAddr == want_addr):
                return int(row.dwOwningPid)
    except Exception:                               # noqa: BLE001
        return None
    return None


def _ipv4_dword(text):
    """`"127.0.0.1"` -> 连接表里那个 DWORD（网络字节序）。不像 IPv4 回 `None`。"""
    parts = str(text or "").split(".")
    if len(parts) != 4:
        return None
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return None
    if any(not 0 <= o <= 255 for o in octets):
        return None
    return (octets[3] << 24) | (octets[2] << 16) | (octets[1] << 8) | octets[0]


#: FILETIME（1601-01-01 起的 100 纳秒）换成 Unix 时刻要减掉的那一段。
_FILETIME_EPOCH_DELTA = 116444736000000000


def wait_process(pid):
    """等这个进程退出。回 `(会话起点, 退出码)`；打不开句柄就回 `None`。

    * 会话起点取自 `GetProcessTimes` 的创建时刻 —— 用来挑「本次运行的日志」，
      它是**真实的事实**，不是拍一个时间窗。
    * `WaitForSingleObject(INFINITE)` 是 OS 事件，不轮询；ctypes 调用期间
      会放开 GIL，所以这条线程干等着也不挡别人。
    """
    k32 = _kernel32()
    if k32 is None:
        return None
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                             False, int(pid))
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        start = None
        if k32.GetProcessTimes(handle, ctypes.byref(created),
                               ctypes.byref(exited), ctypes.byref(kernel),
                               ctypes.byref(user)):
            raw = (created.dwHighDateTime << 32) | created.dwLowDateTime
            start = (raw - _FILETIME_EPOCH_DELTA) / 10000000.0
        k32.WaitForSingleObject(handle, 0xFFFFFFFF)     # INFINITE
        code = wintypes.DWORD(0)
        k32.GetExitCodeProcess(handle, ctypes.byref(code))
        return start, int(code.value)
    finally:
        k32.CloseHandle(handle)


# ======================================================== 挑文件 / 打包

def pick_logs(logdir, session_start, pid, crash_epoch):
    """本次运行该传哪些 `logs/` 里的文件。返回绝对路径列表。

    用户拍板「logs/ 里本次运行的全都传」，落地成三条：

    1. **不带时刻后缀**的那几个 = 当前这次运行（`Move-LogAside` 定的语义：
       起进程前把上一份改名成 `xxx-<结束时刻>.out` 挪走）；
    2. `bshook_*_pid<本次PID>.log` —— **按 PID 精确匹配**，不按时间窗猜；
    3. 其余日志取 **mtime ≥ 本次客户端进程创建时刻** 的。
       ★ 这不是「拍一个时间窗」：边界是从进程句柄拿到的**真实创建时刻**，
       比它早的文件属于上一次运行，是确定的事实。归档的
       `*-<时刻>.out/err` 自然被这一条排除掉。

    只认 `logcleanup` 白名单里的名字 —— `logs/` 下还躺着逆向时手工留下的
    截图、探针输出、几十 MB 的中间产物，那些不是日志。
    """
    import logcleanup

    picked = []
    seen = set()

    def take(path):
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not os.path.isfile(path):
            return
        seen.add(key)
        picked.append(path)

    for name in ("server.out", "server.err", "relay.out", "relay.err",
                 "bsloader.out", "bsloader.err", "online.log"):
        take(os.path.join(logdir, name))
    take(os.path.join(logdir, "online-%s.log"
                      % time.strftime("%Y%m%d", time.localtime(crash_epoch))))
    if pid:
        for path in glob.glob(os.path.join(logdir, "bshook_*_pid%d.log" % pid)):
            take(path)
    try:
        names = os.listdir(logdir)
    except OSError:
        names = []
    for name in sorted(names):
        if not logcleanup.is_log_name(name):
            continue
        path = os.path.join(logdir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) >= session_start:
                take(path)
        except OSError:
            continue
    return picked


def pick_debug_log(gamedir, crash_epoch):
    """原版客户端自己的每日日志 `game_patched/Debug/YYYY-MM-DD.txt`。

    崩溃前最后几行常常就是线索。取**崩溃那天**那份；那天的不在就不取
    （别拿别的日子的顶上，那只会误导人）。
    """
    name = time.strftime("%Y-%m-%d.txt", time.localtime(crash_epoch))
    path = os.path.join(gamedir, "Debug", name)
    return path if os.path.isfile(path) else None


class Collector:
    """把一次崩溃的现场收集起来、压成一个 zip。

    ★ 抽成独立的类（不塞进 `CrashWatcher`）是为了让测试能拿磁盘上现成的
    那份真实崩溃直接跑「收集 → 打包」，不用起线程、不用真的崩一次。
    """

    def __init__(self, root=None, logdir=None, gamedir=None, max_bytes=0,
                 log=None):
        self.root = root or ROOT
        self.logdir = logdir or os.path.join(self.root, "logs")
        self.gamedir = gamedir or os.path.join(self.root, "game_patched")
        self.max_bytes = int(max_bytes or 0)
        self._log = log

    def log(self, message):
        if self._log:
            try:
                self._log(message)
            except Exception:                       # noqa: BLE001
                pass

    def crash_id(self, report, account=None, machine=None):
        """`<账号名>_<安装码>_<YYYYMMDD-HHMMSS>`。

        账号在前、时刻在后 —— 按名称排序时同一个客户端的历次崩溃挨在一起。
        """
        if account is None:
            account = sanitize_account(read_last_login_id(
                os.path.join(self.gamedir, "UserConfig.ini")))
        if machine is None:
            machine = install_id(self.logdir)
        return "%s_%s_%s" % (account, machine, report.stamp)

    def build(self, report, out_path, session_start=None, pid=None,
              exit_code=None, extra_meta=None):
        """打包。返回 `meta` 字典（里面记着裁掉了什么）。

        **必带的三样永远保留**：崩溃报告、`.mdmp`、`BigShot.rpt` 的最后一段。
        可选的（`Debug/` 和 `logs/`）按 size 从小到大加，加不下就跳过 ——
        ★ 判据是「**未压缩**的大小超不超得过剩余预算」：deflate 最坏情况也
        几乎不会让数据变大，所以这个保守判据永远不会超标，也不用先压一遍
        试试看。
        """
        session_start = report.mtime if session_start is None else session_start
        meta = {
            "format": 1,
            "crash_time": report.stamp,
            "crash_time_text": report.logged_at_text,
            "exception": report.exception,
            "fault": report.fault,
            "dump_name": report.dump_name,
            "pid": pid,
            "exit_code": exit_code,
            "session_start": time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(session_start)),
            "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            # 我们这一版的构建信息（版本号 / buildId / hook 和加载器的 hash）。
            "build": read_build_info(self.root),
            # 原版客户端自己的版本号，崩溃报告第一行就写着（现在是 311）。
            "client_version": report.client_version,
            "skipped": [],
        }
        if extra_meta:
            meta.update(extra_meta)

        # ★ 崩溃报告用**已经读进内存的那份文本**，不再去读一遍文件 ——
        #   原版客户端下次登录时会把 `LastCrashReport.txt` 当作 `0x0103` 的载荷
        #   发给服务端，**发完就把文件删掉**（2026-09-09 实测：12:09 那次登录
        #   的 0x0103 载荷 1890 字节 = int32(1) + int32(1878) + 报告正文，
        #   随后文件消失；其它几次登录都是 8 字节的 int32(0)+int32(0)）。
        #   我们在进程退出的那一刻就读到了内存里，从此和那条删除彻底无关。
        required = []
        if report.dump_name:
            dump = os.path.join(self.gamedir, "Dump", report.dump_name)
            if os.path.isfile(dump):
                required.append((dump, "Dump/" + report.dump_name))
            else:
                meta["skipped"].append(
                    {"name": report.dump_name, "why": "文件不在了"})

        optional = []
        debug = pick_debug_log(self.gamedir, report.epoch)
        if debug:
            optional.append((debug, "Debug/" + os.path.basename(debug)))
        for path in pick_logs(self.logdir, session_start, pid, report.epoch):
            optional.append((path, "logs/" + os.path.basename(path)))

        tmp = out_path + ".part"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zf:
            # `BigShot.rpt` 只截最后一段：整份文件是跨机器跨目录一路追加下来的
            # 历次崩溃，和这一次无关。
            # ★ 一律按 latin-1 编回**原始字节**再写进包里。这两份文本是原版
            #   按 ANSI 写的，读的时候用 latin-1 就是「字节原样搬进 str」，
            #   写的时候必须原路搬回去 —— 交给 `writestr` 去按 UTF-8 编码的话，
            #   包里那份和玩家机器上那份就不是同一串字节了。
            zf.writestr("Dump/LastCrashReport.txt",
                        report.text.encode("latin-1", "replace"))
            tail = self._rpt_tail(report)
            if tail:
                zf.writestr("BigShot.rpt.last.txt",
                            tail.encode("latin-1", "replace"))
                if report.logged_at_text and report.logged_at_text not in tail:
                    # 对不上不算错（玩家可能手工删改过 rpt），但要说清楚 ——
                    # 说在 meta.json 里，别把话混进那份要保持原样的正文。
                    meta["rpt_mismatch"] = True
            for path, arcname in required:
                self._add(zf, path, arcname, meta)
            for path, arcname in sorted(optional,
                                        key=lambda item: _size_of(item[0])):
                remain = self._remaining(zf)
                size = _size_of(path)
                if remain is not None and size > remain:
                    meta["skipped"].append(
                        {"name": arcname, "why": "包太大，放不下了",
                         "bytes": size})
                    continue
                self._add(zf, path, arcname, meta)
            zf.writestr("meta.json",
                        json.dumps(meta, ensure_ascii=False, indent=2,
                                   sort_keys=True) + "\n")
        os.replace(tmp, out_path)
        return meta

    def _remaining(self, zf):
        """还能再塞多少字节。没设上限时回 `None`。"""
        if self.max_bytes <= 0:
            return None
        # 留 64 KiB 给中央目录和 meta.json —— 它们在最后才写。
        return max(0, self.max_bytes - zf.fp.tell() - 65536)

    def _add(self, zf, path, arcname, meta):
        try:
            zf.write(path, arcname)
        except OSError as error:
            meta["skipped"].append({"name": arcname, "why": repr(error)})

    def _rpt_tail(self, report):
        """`BigShot.rpt` 的最后一段 —— 整份文件是历次崩溃一路追加下来的。"""
        text = self._read_text(os.path.join(self.gamedir, "BigShot.rpt"))
        return last_record(text) if text else ""

    @staticmethod
    def _read_text(path):
        # ★ `newline=""`：不加的话 Python 的通用换行会把 `\r\n` 悄悄变成 `\n`，
        #   包里那份就不再和玩家机器上那份逐字节相同了。
        try:
            with open(path, "r", encoding="latin-1", newline="") as fp:
                return fp.read()
        except OSError:
            return ""


def _size_of(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ================================================================== 上传

def upload(path, crash_id, host, port, connect, crash_time_text="",
           timeout=UPLOAD_TIMEOUT_SECONDS):
    """把一个压缩包 POST 上去。成功回 `(True, 说明)`，失败回 `(False, 原因)`。

    `connect(host, port)` 由调用方注入 —— 中继传的是自己的
    `relay.connect_remote`，于是 SOCKS5 / HTTP CONNECT 代理**自动生效**，
    崩溃包和游戏流量走同一条出口。注入进来还顺带让这个函数可以单测。

    元数据走 `X-Crash-*` 请求头而不是 multipart：全项目故意没有 multipart
    解析代码（`web/server.py` 开头那条注释），不为一个接口破例。
    """
    import http.client

    class _Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = connect(host, port)
            if timeout:
                self.sock.settimeout(timeout)

    size = os.path.getsize(path)
    conn = _Conn(host, port, timeout=timeout)
    try:
        with open(path, "rb") as body:
            conn.request("POST", UPLOAD_PATH, body=body, headers={
                "Content-Type": "application/zip",
                "Content-Length": str(size),
                "X-Crash-Client": crash_id,
                "X-Crash-Time": crash_time_text,
                "X-Crash-Sha256": sha256_of(path),
            })
        resp = conn.getresponse()
        payload = resp.read(65536)
        if resp.status != 200:
            return False, "HTTP %d %s" % (resp.status,
                                          _message_of(payload) or resp.reason)
        try:
            got = json.loads(payload.decode("utf-8"))
        except Exception:                           # noqa: BLE001
            return True, "已接收"
        if not got.get("ok"):
            return False, str(got.get("message") or "服务器拒收")
        return True, str(got.get("saved") or "已接收")
    except Exception as error:                      # noqa: BLE001
        return False, repr(error)
    finally:
        try:
            conn.close()
        except Exception:                           # noqa: BLE001
            pass


def _message_of(payload):
    try:
        return json.loads(payload.decode("utf-8")).get("message") or ""
    except Exception:                               # noqa: BLE001
        return ""


# ============================================================== 监控主体

class CrashWatcher:
    """中继进程里的那一份。用法见模块开头。"""

    def __init__(self, host, port, connect, root=None, logdir=None,
                 gamedir=None, enabled=True, max_bytes=0, keep_days=0,
                 log=None, sleep=time.sleep,
                 resolve_pid=owning_pid, wait=wait_process):
        self.host = host
        self.port = int(port)
        self.connect = connect
        self.root = root or ROOT
        self.logdir = logdir or os.path.join(self.root, "logs")
        self.gamedir = gamedir or os.path.join(self.root, "game_patched")
        self.enabled = bool(enabled)
        self.max_bytes = int(max_bytes or 0)
        self.keep_days = int(keep_days or 0)
        self._log = log
        #: ★ 注入点，全是给测试用的：`sleep` 让重试不用真等 3 秒，
        #:   `resolve_pid` / `wait` 让整条链在非 Windows 上也能验。
        self._sleep = sleep
        self._resolve_pid = resolve_pid
        self._wait = wait
        self._queue = queue.Queue(MAX_PENDING_NOTES)
        self._thread = None
        self._seen_pids = set()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 日志
    def log(self, message):
        if self._log:
            try:
                self._log(message)
            except Exception:                       # noqa: BLE001
                pass

    # ------------------------------------------------------------ 目录 / 状态
    @property
    def pending_dir(self):
        return os.path.join(self.logdir, PENDING_DIRNAME)

    def _state_path(self):
        return os.path.join(self.logdir, STATE_FILENAME)

    def _load_state(self):
        try:
            with open(self._state_path(), "r", encoding="utf-8") as fp:
                got = json.load(fp)
            return got if isinstance(got, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state):
        path = self._state_path()
        tmp = "%s.%d.tmp" % (path, os.getpid())
        try:
            os.makedirs(self.logdir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
                json.dump(state, fp, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, path)
        except OSError:
            pass

    @staticmethod
    def _fingerprint(report):
        """一次崩溃的身份。**按状态翻转去重**：同一次现场只处理一遍。"""
        return "%s|%d" % (report.logged_at_text, int(report.mtime))

    # ------------------------------------------------------ 中继转发线程调这个
    def note_client(self, peer_addr, local_port):
        """「有个客户端连上中继了」。★ **只入队，立刻返回。**

        调用它的是中继的转发线程，它后面马上要去跑 `_pump` 转发游戏字节 ——
        这里多花的每一微秒都直接压在游戏的延迟上。查 PID 那一步要遍历整张
        TCP 表，绝不能放在这儿。

        队列满了就丢：同一次会话只要有**一条**连接被认出来就够了。
        """
        if not self.enabled or self._thread is None:
            return
        try:
            self._queue.put_nowait((peer_addr, int(local_port)))
        except queue.Full:
            pass

    # ------------------------------------------------------------ 后台线程
    def start(self):
        """起后台线程。**先把上次没传成功的补传掉**，再开始盯新的会话。"""
        if not self.enabled:
            self.log("崩溃上传 已关闭（crash_upload = 0）")
            return None
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="crashwatch")
        self._thread.start()
        threading.Thread(target=self._resume, daemon=True,
                         name="crashwatch-resume").start()
        return self._thread

    def _resume(self):
        """补传：上次崩了但没传上去的包还在 `logs/.crash_pending/` 里。

        覆盖用户点名的两种丢失场景：崩的时候正好断网、玩家崩完立刻 stop.bat。
        """
        try:
            self.prune_pending()
            for path in sorted(glob.glob(os.path.join(self.pending_dir,
                                                      "*.zip"))):
                crash_id = os.path.basename(path)[:-4]
                self.log("崩溃上传 补传上次没传成功的 %s" % crash_id)
                self.deliver(path, crash_id, "")
        except Exception as error:                  # noqa: BLE001
            self.log("崩溃上传 补传出错（忽略）：%r" % (error,))

    def prune_pending(self):
        """待传队列里太老的包删掉，别让本机磁盘一直涨。`keep_days<=0` = 不删。"""
        if self.keep_days <= 0:
            return 0
        deadline = time.time() - self.keep_days * 86400
        removed = 0
        for path in glob.glob(os.path.join(self.pending_dir, "*")):
            try:
                if os.path.getmtime(path) < deadline:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
        return removed

    def _worker(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._on_note(*item)
            except Exception as error:              # noqa: BLE001
                # 认不出客户端不该让这条线程死掉 —— 下一条连接还有机会。
                self.log("崩溃上传 认客户端出错（忽略）：%r" % (error,))

    def _on_note(self, peer_addr, local_port):
        pid = self._resolve_pid(peer_addr[0], peer_addr[1], local_port)
        if not pid:
            return
        with self._lock:
            if pid in self._seen_pids:
                return
            self._seen_pids.add(pid)
        threading.Thread(target=self._session, args=(pid,), daemon=True,
                         name="crashwatch-%d" % pid).start()

    def _session(self, pid):
        """盯住一次游戏会话，从「认出来」到「它死了、现场传完」。

        ★ PID 要**一直占着**到这一整轮做完（包括收集和上传）才放开：中继有三个
        监听口，同一次会话会 accept 三条连接、排三次队；提前放开的话，
        后面那两条会又开一个 watcher 出来。
        """
        try:
            got = self._wait(pid)
            if not got:
                return
            session_start, exit_code = got
            self.handle_exit(pid, session_start, exit_code)
        except Exception as error:                  # noqa: BLE001
            # 一次会话出岔子不该影响下一次 —— 但要说出来，不能哑掉。
            self.log("崩溃上传 盯客户端出错（忽略）：%r" % (error,))
        finally:
            with self._lock:
                self._seen_pids.discard(pid)

    def handle_exit(self, pid, session_start, exit_code):
        """客户端退出了 —— 判断是不是崩溃，是就收集 + 上传。

        **抽出来是为了让测试直接调**，不用真起一个进程再杀掉。
        """
        report = read_crash_report(self.gamedir)
        if report is None:
            return None                     # 从来没崩过
        if session_start is not None and report.mtime < session_start:
            # 崩溃报告比本次会话还老 ⇒ 这次是正常退出，那份是上次留下的。
            return None
        state = self._load_state()
        mark = self._fingerprint(report)
        if state.get("last") == mark:
            return None                     # 这一份已经处理过了，不重复传
        state["last"] = mark
        self._save_state(state)

        collector = Collector(root=self.root, logdir=self.logdir,
                              gamedir=self.gamedir, max_bytes=self.max_bytes,
                              log=self._log)
        crash_id = collector.crash_id(report)
        os.makedirs(self.pending_dir, exist_ok=True)
        out = os.path.join(self.pending_dir, crash_id + ".zip")
        meta = collector.build(report, out, session_start=session_start,
                               pid=pid, exit_code=exit_code)
        size = os.path.getsize(out)
        # ★ 这一行在**上传之前**就写：后面网络全挂了也留得下痕迹。
        self.log("崩溃上传 捕获到一次崩溃 %s（%s @ %s，退出码 0x%08X），"
                 "已打包 %.1f MB%s"
                 % (crash_id, report.exception or "?", report.fault or "?",
                    (exit_code or 0) & 0xFFFFFFFF, size / 1048576.0,
                    "" if not meta["skipped"]
                    else "（有 %d 项没放进去）" % len(meta["skipped"])))
        if self.max_bytes and size > self.max_bytes:
            # 裁到最后还是超 —— 传上去也只会被 413，不如就地说清楚。
            keep = out[:-4] + ".toobig.zip"
            os.replace(out, keep)
            self.log("崩溃上传 ✗ 包 %.1f MB 超过上限 %.1f MB，没有上传；"
                     "现场留在 %s" % (size / 1048576.0,
                                     self.max_bytes / 1048576.0, keep))
            return crash_id
        self.deliver(out, crash_id, report.logged_at_text)
        return crash_id

    def deliver(self, path, crash_id, crash_time_text):
        """上传，失败就重试。**无论成败都写一行日志**（用户明确要求）。

        成功才删本机那份 —— 「先落盘再上传」的另一半。
        """
        last = ""
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            ok, detail = upload(path, crash_id, self.host, self.port,
                                self.connect, crash_time_text)
            if ok:
                self.log("崩溃上传 ✓ %s 已上传到 %s:%d（第 %d 次尝试，%s）"
                         % (crash_id, self.host, self.port, attempt, detail))
                try:
                    os.remove(path)
                except OSError:
                    pass
                return True
            last = detail
            self.log("崩溃上传 ✗ %s 第 %d/%d 次失败：%s"
                     % (crash_id, attempt, RETRY_ATTEMPTS, detail))
            if attempt < RETRY_ATTEMPTS:
                # ★ 铁律 10 的例外，理由见 RETRY_DELAY_SECONDS 上面那段注释。
                self._sleep(RETRY_DELAY_SECONDS)
        self.log("崩溃上传 ✗ %s 连续 %d 次失败（%s），先攒在 %s，"
                 "下次启动时再补传"
                 % (crash_id, RETRY_ATTEMPTS, last, self.pending_dir))
        return False
