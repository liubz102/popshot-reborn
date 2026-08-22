#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""update_client.py —— 客户端自动更新主逻辑（V0.2 版本管理的收尾一环）

**谁拉起它**：game_patched\\BsPatcherChn.exe（被 tools\\updater\\updater.c
替换掉的引导器）。玩家用旧客户端连新服务器 → 服务器版本门禁回 0xFE
拒绝帧 → 客户端走原版自带的升级分支拉起「BsPatcherChn.exe」→ 引导器按
launch.ps1 同一套规则挑包内 Python，把本脚本带一个新控制台窗口拉起来，
然后立刻退出 —— 等游戏退出、下载、校验、应用全在这里做。

流程（细节见 develop_history 对应 session / DECISIONS）：

    1. 单实例锁；读包根 BUILD.ver 的本地版本
    2. 探测游戏服（server.config 的 server_address : 27799）：重演一次
       握手（裸发版本号），从拒绝文案里解析「该升到哪个版本」——
       成对发布（D079）的客户端 / 服务端靠这句话对上批次
    3. 取 GitHub 上的 manifest.json（MANIFEST_URLS 按顺序尝试）
    4. 选定目标版本：探针说的版本优先，manifest 里没有再用最新版
    5. 下载全量 zip（进度/速度/ETA）→ sha256 校验
    6. 等待 BigShot.exe 退出（15 秒还没退就提示后 taskkill）
    7. 包根写权限试探：写不进去 → runas 自提权重跑（唯一一次 UAC；
       已下载的 zip 用 --zip 传给提权后的自己，不重复下 400MB）
    8. 解压到临时目录 → 逐文件覆盖（PROTECTED_PATHS 跳过玩家数据，
       文件被占重试）→ BUILD.ver 最后写（= 成功提交点，失败重跑幂等）
    9. 「更新完成，是否重启游戏？」

也支持玩家/维护者手动跑：python tools\\update_client.py（效果一样，
--elevated/--zip 是提权重跑的内部参数，不用手填）。
"""
from __future__ import annotations

import argparse
import ctypes
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

#: 本脚本所在 = <包根>\\tools，上跳一级是包根。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    sys.path.insert(0, os.path.join(ROOT, "server"))
    import config        # noqa: E402  （server/config.py：server_address / 端口）
    import simple        # noqa: E402  （server/simple.py：握手用的 SimpleCipher）
    import versioning    # noqa: E402  （server/versioning.py：版本号语义）
    SERVER_MODULES_ERROR = None
except Exception as _import_error:            # noqa: BLE001 —— 包残缺也要能开口说话
    config = simple = versioning = None
    SERVER_MODULES_ERROR = _import_error

# ---------------------------------------------------------------------------
#  常量（★ 按发版人交代：地址直接写在代码里，不新增配置文件）
# ---------------------------------------------------------------------------

#: manifest 的取用地址，按顺序尝试。第一个是 GitHub「最新 Release」的
#: 固定 URL（releases/latest/download/<资产名> 永远指向最新正式 Release，
#: 不用知道版本号）。要加镜像前缀（ghproxy 之类）就往列表后面加。
MANIFEST_URLS = (
    "https://github.com/liubz102/popshot-reborn/releases/latest/download/manifest.json",
)

#: 下载/manifest 全失败时给玩家看的手动下载页（必须和 updater.c 的
#: MANUAL_URL 保持一致）。
RELEASES_PAGE = "https://github.com/liubz102/popshot-reborn/releases"

#: 玩家数据排除清单 —— 应用更新时**永不覆盖**（打包侧本来就不把这些打进
#: zip，这里是第二道纵深防御：防「未来某次打包误带」和「-IncludeSave 自用
#: 包」两类事故）。注意：执行更新的是**旧包里**的这份脚本，改这张表要到
#: 「包含它的那次更新」之后的下一次更新才生效，所以主保护必须在打包侧。
#:   - server.config           玩家自己填的服务器地址（模板进包，更新不覆盖）
#:   - game_patched/UserConfig.ini  玩家的画面设置 / 登录名
#:   - 其余                     账号、日志、崩溃转储（zip 里本来就没有）
PROTECTED_PATHS = (
    "server/data/accounts.json",
    "server.config",
    "game_patched/UserConfig.ini",
    "game_patched/BigShot.rpt",
    "logs",                       # 目录：logs/ 下任何东西都不碰
    "game_patched/Dump",
    "game_patched/Debug",
)

#: 探针从拒绝文案里抠版本号的模式（`versioning.format_version` 的输出
#: 是 "V主.次.修订"；gameserver.version_reject_message 的注释钉住了这个格式）。
WANTED_VERSION_RE = re.compile(r"[vV](\d+(?:\.\d+){1,2})")

#: 各类等待的节奏。
PROBE_TIMEOUT_S = 3.0
GAME_EXIT_WAIT_S = 15.0
FILE_RETRY_COUNT = 20
FILE_RETRY_SLEEP_S = 0.25

#: 覆盖「正在运行的 exe」时的让位后缀（见 copy_with_retry 的改名大法）。
#: 下次更新开始时 sweep_update_old 会把不再被锁的旧文件扫掉。
UPDATE_OLD_SUFFIX = ".update_old"

#: 单实例锁。stale 判定：锁文件比这还旧就直接抢（上次更新器崩了没清）。
LOCK_STALE_S = 600.0


# ---------------------------------------------------------------------------
#  基础小件
# ---------------------------------------------------------------------------

def log_to_file(line):
    """关键节点追加进 logs\\update.log（引导器写的 updater.log 旁边，
    排查「更新到一半没反应」的现场问题用）。失败就算了，不影响主流程。"""
    try:
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(ROOT, "logs", "update.log"), "a",
                  encoding="utf-8", errors="replace") as f:
            f.write(f"[{stamp}] {line}\n")
    except OSError:
        pass


def hold(prompt="按回车键关闭…"):
    """停住窗口：更新器由引导器带控制台拉起，main 一返回窗口就没了 ——
    ★ 用户要求：无论正常结束还是出错，窗口都必须留住让人看清结果，
    绝不闪退。（引导器侧还有 cmd & pause 的第二道保险，见 updater.c。）"""
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        pass


def read_local_version(root=ROOT):
    """包根 BUILD.ver -> 版本元组；认不出返回 None（走「请手动下载」）。"""
    try:
        with open(os.path.join(root, "BUILD.ver"), "rb") as f:
            text = f.read().decode("utf-8-sig", errors="replace")
    except OSError:
        return None
    try:
        value = json.loads(text).get("version")
    except ValueError:
        m = re.search(r'"version"\s*:\s*"([^"]*)"', text)
        value = m.group(1) if m else None
    if value is None:
        return None
    return versioning.parse_version_text(value)


def is_protected(relpath):
    """zip 里的相对路径（\\ 或 / 都收）是否命中排除清单。

    目录条目按前缀匹配：``logs`` 命中 ``logs/xxx``，也命中 ``logs``
    本身；文件条目按全路径等值比较。
    """
    rel = relpath.replace("\\", "/").strip("/")
    if not rel:
        return True
    for entry in PROTECTED_PATHS:
        if rel == entry or rel.startswith(entry.rstrip("/") + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
#  探针：向游戏服重演一次握手，问「我该升到哪个版本」
# ---------------------------------------------------------------------------

def parse_wanted_version(message):
    """从拒绝文案里抠版本号 -> 元组；抠不到返回 None。

    文案是 gameserver.version_reject_message() 生成的，里面带着服务器
    自己的版本号（format_version 的 ``V主.次.修订``）。这句话是**给机器
    读的协议**：探针拿它对准「成对发布」的批次。
    """
    m = WANTED_VERSION_RE.search(message or "")
    if not m:
        return None
    return versioning.parse_version_text(m.group(1))


def parse_handshake_response(plain):
    """解密后的明文流 -> ``(结果码, 文案或 None)``。

    明文流开头是 4 字节头的 0xFE 控制帧：``[0xFE][未用][u16 LE 载荷长]``，
    载荷 = ``int32 结果码`` + 可选 ``u16 字符数 + UTF-16LE``（客户端
    0x54dbf6 的读法，gameserver.py 的 build_ctrl/w_i32/w_wstr 的镜像）。
    """
    if len(plain) < 4 or plain[0] != 0xFE:
        raise ValueError("回应不是 0xFE 控制帧")
    payload_len = struct.unpack_from("<H", plain, 2)[0]
    if len(plain) < 4 + payload_len:
        raise ValueError("0xFE 帧不完整")
    payload = plain[4:4 + payload_len]
    result = struct.unpack_from("<i", payload, 0)[0]
    message = None
    if len(payload) >= 8:
        chars = struct.unpack_from("<H", payload, 4)[0]
        if 4 + 2 + chars * 2 <= len(payload):
            message = payload[6:6 + chars * 2].decode("utf-16-le",
                                                      errors="replace")
    return result, message


def probe_server(host, local_version, timeout=PROBE_TIMEOUT_S, port=None):
    """连游戏服 27799 重演握手 -> ``(状态, 该升到的版本元组或 None, 文案)``。

    状态三种：
        ``"ok"``        结果码 0 —— 服务器认这个版本，不用更新（门禁
                        刚被放宽、或探针前一刻刚好放版的窗口期）
        ``"rejected"``  非零结果码 —— 正常的「要更新」
        ``"unreachable"`` 连不上 / 说了听不懂的话 —— 单机玩家或网络问题，
                        由调用方退回「升 GitHub 最新版」

    握手细节：整条流被 SimpleCipher 逐字节加密（连最前面那 4 个版本号
    字节也是流的一部分）；客户端->服务端方向初态 (i1=0, i2=1)，服务端
    ->客户端方向 (i1=5, i2=3)（server/simple.py）。**探针连接会被服务器
    正常记账进 online.log**，和真实客户端一模一样。
    """
    port = port if port is not None else config.GAME_PORT
    if not host:
        return "unreachable", None, None
    wire = versioning.encode_wire(local_version) if local_version \
        else versioning.LEGACY_WIRE_VERSION
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return "unreachable", None, None
    try:
        sock.settimeout(timeout)
        out = simple.SimpleCipher.client_to_server()
        sock.sendall(out.encrypt(struct.pack("<i", wire)))
        dec = simple.SimpleCipher.server_to_client()
        plain = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            plain += dec.decrypt(chunk)
            if len(plain) >= 4 and plain[0] == 0xFE:
                need = 4 + struct.unpack_from("<H", plain, 2)[0]
                if len(plain) >= need:
                    break
        result, message = parse_handshake_response(plain)
    except (OSError, ValueError):
        return "unreachable", None, None
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if result == 0:
        return "ok", None, message
    return "rejected", parse_wanted_version(message), message


# ---------------------------------------------------------------------------
#  manifest 与下载
# ---------------------------------------------------------------------------

def validate_manifest(obj):
    """manifest.json 的结构闸：认不出直接当「取不到」处理（fail-open 到
    下一个 URL / 手动下载提示），绝不在玩家机器上抛栈。"""
    if not isinstance(obj, dict):
        return None
    releases = obj.get("releases")
    if not isinstance(releases, list) or not releases:
        return None
    for entry in releases:
        if (not isinstance(entry, dict)
                or not entry.get("url")
                or not entry.get("sha256")
                or versioning.parse_version_text(entry.get("version", ""))
                is None):
            return None
    return obj


def fetch_manifest(urls=MANIFEST_URLS, timeout=15.0):
    """按顺序取 manifest.json -> 校验过的 dict；全失败返回 None。"""
    for url in urls:
        print(f"· 正在获取更新清单：{url}")
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                obj = json.loads(resp.read().decode("utf-8", errors="replace"))
            good = validate_manifest(obj)
            if good is not None:
                return good
            print(f"× manifest 内容认不出：{url}")
        except (OSError, ValueError) as error:
            print(f"× 取 manifest 失败（{error}）：{url}")
    return None


def pick_target(manifest, local_version, wanted):
    """选更新目标 -> manifest 条目或 None（None = 不用更新）。

    优先级：探针点名要的版本（对准服务器批次）> manifest 最新版。
    本地已不低于目标就返回 None。
    """
    entries = []
    for entry in manifest["releases"]:
        version = versioning.parse_version_text(entry["version"])
        entries.append((version, entry))
    if wanted is not None:
        for version, entry in entries:
            if version == wanted:
                if local_version is not None and local_version >= version:
                    return None          # 服务器要的版本本地已有（或更高）
                return entry
        print(f"⚠ 更新源里没有服务器要的版本 "
              f"{versioning.format_version(wanted)}，改用最新版")
    version, entry = entries[0]
    if local_version is not None and local_version >= version:
        return None
    return entry


def download_file(url, dest, expected_sha256=None, expected_size=None,
                  chunk=1 << 20):
    """带进度条下载 + 边下边算哈希。校验不过删文件、抛异常。"""
    started = time.monotonic()

    def fmt_progress(done, total):
        done_mb = done / (1 << 20)
        speed = done / max(time.monotonic() - started, 0.001)
        if total:
            eta = (total - done) / max(speed, 1)
            return (f"\r{done_mb:7.1f} / {total / (1 << 20):.1f} MiB"
                    f"  {speed / (1 << 20):5.2f} MiB/s  剩余 {eta:4.0f} 秒 ")
        return f"\r{done_mb:7.1f} MiB  {speed / (1 << 20):5.2f} MiB/s "

    with urllib.request.urlopen(url, timeout=60) as resp:
        total = expected_size
        if total is None:
            try:
                total = int(resp.headers.get("Content-Length", ""))
            except ValueError:
                total = None
        hasher = hashlib.sha256()
        done = 0
        with open(dest, "wb") as f:
            while True:
                block = resp.read(chunk)
                if not block:
                    break
                f.write(block)
                hasher.update(block)
                done += len(block)
                print(fmt_progress(done, total), end="", flush=True)
    print()
    if expected_size is not None and done != expected_size:
        os.unlink(dest)
        raise IOError(f"下载不完整：{done} 字节，应为 {expected_size}")
    digest = hasher.hexdigest()
    if expected_sha256 and digest.lower() != str(expected_sha256).lower():
        os.unlink(dest)
        raise IOError(f"sha256 校验不过（下载到 {digest}，manifest 说 "
                      f"{expected_sha256}）—— 更新源文件损坏或被篡改")
    return digest


def fetch_zip_cached(entry, cache_dir):
    """下载（或复用上次下到一半校验通过的）更新包 -> zip 路径。

    复用判据是 sha256：同一个 zip 下到 99% 断掉、提权重跑、玩家手滑再点
    一次，都接着用已完整的那份，不重复下 400MB。
    """
    name = "popshot-update-" + str(entry["version"]).strip() + ".zip"
    dest = os.path.join(cache_dir, name)
    if os.path.exists(dest):
        try:
            print(f"· 用已下载的 {dest}（校验中…）")
            download_file_verify_local(dest, entry)
            return dest
        except (IOError, OSError) as error:
            print(f"× 缓存的包不能用（{error}），重新下载")
            try:
                os.unlink(dest)
            except OSError:
                pass
    print(f"↓ 正在下载客户端 V{entry['version']} 的完整包（约 410 MiB）…")
    print(f"  {entry['url']}")
    download_file(entry["url"], dest,
                  expected_sha256=entry.get("sha256"),
                  expected_size=entry.get("size"))
    return dest


def download_file_verify_local(path, entry):
    size = os.path.getsize(path)
    expected = entry.get("size")
    if expected is not None and size != expected:
        raise IOError(f"大小不符（{size}，应为 {expected}）")
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            hasher.update(block)
    if hasher.hexdigest().lower() != str(entry["sha256"]).lower():
        raise IOError("sha256 不符")
    return True


# ---------------------------------------------------------------------------
#  等游戏退出 / 提权
# ---------------------------------------------------------------------------

def _pids_by_name(name, extra_pid=0):
    """按进程名找 PID（ctypes Toolhelp32 快照，不引第三方库）+ 一个额外的
    PID（-procid 传来的那个，进程名对不上也认）。"""
    k32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x2

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong),
                    ("cntUsage", ctypes.c_ulong),
                    ("th32ProcessID", ctypes.c_ulong),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", ctypes.c_ulong),
                    ("cntThreads", ctypes.c_ulong),
                    ("th32ParentProcessID", ctypes.c_ulong),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", ctypes.c_ulong),
                    ("szExeFile", ctypes.c_char * 260)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1 or snap == 0xFFFFFFFF:
        return [extra_pid] if extra_pid else []
    pids = []
    try:
        entry = PE32()
        entry.dwSize = ctypes.sizeof(PE32)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.decode("mbcs", errors="replace").lower() == name.lower():
                pids.append(entry.th32ProcessID)
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    if extra_pid and extra_pid not in pids:
        pids.append(extra_pid)
    return pids


def pid_alive(pid):
    """PID 还在不在（探询权限不够也算「在」—— 宁可多等一眼）。"""
    if not pid:
        return False
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return True
    try:
        WAIT_OBJECT_0 = 0
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _process_image_path(pid):
    """PID -> 它的 exe 完整路径（QueryFullProcessImageNameW）；拿不到返回 None。"""
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong)]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


def is_package_python_image(image_path, root=ROOT):
    """这个 python.exe 是不是**从本包的 runtime 里**跑起来的（= 该停的那种）。

    本机服务端（server\\app.py）和本机中继（server\\relay.py）都是启动脚本用
    ``<root>\\runtime*\\python\\python.exe`` 拉起来的 —— 它们不锁游戏文件，
    但锁着 runtime 里的 python.exe 自己（更新要覆盖 runtime 时会被卡死）。
    按 exe 完整路径精确匹配，**别的 python（其他游戏副本 / 开发环境）一个
    都不碰** —— 和 launch.ps1「绝不 Get-Process python 乱杀」是同一条纪律。
    """
    if not image_path:
        return False
    image = os.path.normcase(os.path.abspath(image_path))
    for rel in (os.path.join("runtime", "python", "python.exe"),
                os.path.join("runtime-win7", "python", "python.exe")):
        if image == os.path.normcase(os.path.join(os.path.abspath(root), rel)):
            return True
    return False


def stop_package_pythons(root=ROOT, on_progress=print):
    """停掉从本包 runtime 跑起来的 python（本机服务端 / 本机中继）-> 停了几个。

    它们锁着 runtime\\python\\python.exe，不解掉更新没法覆盖 runtime。
    更新器**自己**也是从这份 python 跑的 —— 排除自己的 PID；自己脚下这个
    exe 的占用由 copy_with_retry 的「改名大法」解决（见那边注释）。
    """
    victims = []
    for pid in _pids_by_name("python.exe"):
        if pid == os.getpid():
            continue
        if is_package_python_image(_process_image_path(pid), root):
            victims.append(pid)
    if not victims:
        return 0
    on_progress(f"· 停止本机服务端/中继（PID {', '.join(str(p) for p in victims)}）"
                f"以解除文件占用…")
    stopped = len(victims)
    for pid in victims:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    for _ in range(int(FILE_RETRY_COUNT)):
        victims = [pid for pid in victims if pid_alive(pid)]
        if not victims:
            return stopped
        time.sleep(FILE_RETRY_SLEEP_S)
    raise OSError("本机服务端进程结束不了（runtime 仍被占用）。"
                  "请手动关掉它的窗口后重试更新。")


def wait_game_exit(procid=0, patience_s=GAME_EXIT_WAIT_S):
    """等游戏退出：BigShot.exe（按进程名找）+ -procid 那个 PID。

    更新器是被「客户端的升级分支」拉起来的，此刻游戏多半还开着 —— 而
    game_patched 里的 exe/dll 全被它锁着。先安静等（引导器已经退出，
    游戏自己也会退）；超时还没退就明说，玩家点回车后 taskkill。
    """
    pids = [pid for pid in _pids_by_name("BigShot.exe", procid) if pid_alive(pid)]
    if not pids:
        return
    print(f"· 等待游戏退出（PID {', '.join(str(p) for p in pids)}）…")
    deadline = time.monotonic() + patience_s
    while time.monotonic() < deadline:
        pids = [pid for pid in pids if pid_alive(pid)]
        if not pids:
            return
        time.sleep(0.5)
    pids = [pid for pid in pids if pid_alive(pid)]
    if not pids:
        return
    try:
        input("更新需要关闭游戏。关闭后按回车继续（自动结束游戏进程）…")
    except EOFError:
        pass
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    for _ in range(20):                      # 给句柄释放留点时间
        pids = [pid for pid in pids if pid_alive(pid)]
        if not pids:
            return
        time.sleep(0.25)
    raise OSError("游戏进程结束不了，文件仍被占用。请手动关闭游戏后重试。")


def root_is_writable(root=ROOT):
    """包根能不能写（建了就删的试探文件）。"""
    probe = os.path.join(root, ".update-write-test")
    try:
        with open(probe, "w") as f:
            f.write("t")
        os.unlink(probe)
        return True
    except OSError:
        return False


def elevate_and_rerun(zip_path, procid):
    """以管理员身份重跑自己（ShellExecuteW 的 runas = 标准的一次 UAC）。

    把已下载的 zip 用 --zip 传过去，提权后的进程不重复下 400MB。
    返回 False = 玩家拒绝了 UAC / 提权失败（调用方转手动下载提示）。
    本进程随后退出，一切由提权后的那个接着干。
    """
    params = build_elevate_params(zip_path, procid)
    SW_SHOWNORMAL = 1
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, ROOT, SW_SHOWNORMAL)
    return rc > 32


def build_elevate_params(zip_path, procid, script=None):
    """提权重跑的 ShellExecuteW 参数行（纯函数，测试钉住格式）。"""
    args = [f'"{script or os.path.abspath(__file__)}"', "--elevated"]
    if zip_path:
        args += ["--zip", f'"{zip_path}"']
    if procid:
        args += ["--procid", str(procid)]
    return " ".join(args)


# ---------------------------------------------------------------------------
#  应用更新
# ---------------------------------------------------------------------------

def copy_with_retry(src, dst):
    """覆盖一个文件，被占（sharing violation / 权限抖动）就重试。

    bsloader / bshook 在游戏退出后的零点几秒里可能还占着句柄；20 次 x
    0.25 秒足够它们退干净。目标只读时先去掉只读位（zip 解出来不带属性，
    这里是防玩家手贱把文件设了只读）。

    ★ 改名大法：目标若是**正在运行的 exe**（最典型：更新器自己脚下的
    ``runtime\\python\\python.exe`` —— 这个进程要跑完更新才能退，靠等是等
    不到的），Windows 不许删/覆盖它，但**允许改名**。把旧文件挪到旁边
    （``xxx.update_old``）让位，新文件落原名 —— 旧进程继续从改名后的映像
    跑，互不影响。遗留的 ``.update_old`` 由下次更新开头的 sweep 扫掉。
    """
    last_error = None
    for attempt in range(FILE_RETRY_COUNT):
        try:
            os.replace(src, dst)
            return
        except PermissionError as error:
            last_error = error
            try:
                os.chmod(dst, stat_write_mode(dst))
            except OSError:
                pass
            if attempt >= 3:
                try:
                    os.rename(dst, dst + UPDATE_OLD_SUFFIX)
                    os.replace(src, dst)
                    return
                except OSError:
                    pass
            time.sleep(FILE_RETRY_SLEEP_S)
        except OSError as error:
            last_error = error
            time.sleep(FILE_RETRY_SLEEP_S)
    raise OSError(f"覆盖 {dst} 失败（重试 {FILE_RETRY_COUNT} 次）：{last_error}")


def sweep_update_old(root, on_progress=print):
    """清掉历次更新「改名让位」留下的 ``*.update_old``，返回清了几个。

    这些文件被改名时正被某个进程占用（多半是上一次的更新器自己），那时删
    不了；下次更新时锁早没了。删不掉的（还有进程占着）跳过，下次再试。
    """
    removed = 0
    for base, dirs, files in os.walk(root):
        rel_base = os.path.relpath(base, root).replace(os.sep, "/")
        # 玩家数据目录（logs/ 等）里的东西一律不碰，哪怕名字像
        if rel_base != "." and is_protected(rel_base):
            dirs[:] = []
            continue
        for name in files:
            if name.endswith(UPDATE_OLD_SUFFIX):
                try:
                    os.unlink(os.path.join(base, name))
                    removed += 1
                except OSError:
                    pass
    if removed:
        on_progress(f"· 清掉上次更新遗留的旧文件 {removed} 个")
    return removed


def stat_write_mode(path):
    import stat as stat_module
    try:
        return stat_module.S_IMODE(os.stat(path).st_mode) | 0o200
    except OSError:
        return 0o666


def apply_update(zip_path, root=ROOT, on_progress=print):
    """把更新 zip 应用到包根。**BUILD.ver 最后写** —— 它是提交点。

    zip 里的路径都带一层顶层目录（PopShot-portable-win64_V0-2-7\\...，
    New-PackageZip 按「目录整体进 zip」打的），应用时剥掉这一层。
    不删除 zip 里没有的文件：玩家数据（accounts.json / 日志 / 设置）
    天然保留，PROTECTED_PATHS 再拦一道。

    ★ staging 建在包根（同盘）而不是 %TEMP%：搬运用 os.replace，跨盘
      会直接失败（WinError 17），顺带让「盘空间够不够」在正确的盘上暴露。
    """
    moved = 0
    skipped = []
    sweep_update_old(root, on_progress=on_progress)   # 上次改名让位的遗留
    staging = tempfile.mkdtemp(prefix=".popshot-apply-", dir=root)
    try:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                top = names[0].split("/")[0] if names else ""
                if not any(n.split("/", 1)[1:] == ["BUILD.ver"] for n in names):
                    raise IOError("更新包里没有 BUILD.ver —— 不是完整的客户端包")
                # 先整包解到临时目录，再逐文件搬。
                zf.extractall(staging)
        except zipfile.BadZipFile as error:
            raise IOError(f"解压更新包失败（zip 损坏）：{error}")

        buildver_src = None
        for name in names:
            rel = name.split("/", 1)[1] if "/" in name else ""
            if not rel:
                continue
            if is_protected(rel):
                skipped.append(rel)
                continue
            src = os.path.join(staging, top, rel.replace("/", os.sep))
            if rel == "BUILD.ver":
                buildver_src = src           # 提交点，最后写
                continue
            dst = os.path.join(root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            copy_with_retry(src, dst)
            moved += 1
            if moved % 200 == 0:
                on_progress(f"  …已写入 {moved} 个文件")
        if buildver_src is not None:
            copy_with_retry(buildver_src, os.path.join(root, "BUILD.ver"))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if skipped:
        on_progress(f"· 保留玩家本地文件 {len(skipped)} 项（不覆盖）")
    on_progress(f"√ 共写入 {moved} 个文件")


# ---------------------------------------------------------------------------
#  单实例锁
# ---------------------------------------------------------------------------

class SingleInstance:
    """logs\\update.lock 的 O_CREAT|O_EXCL 锁 + stale 抢占。

    玩家手快双击两次、引导器和手动各起一份，都会走到这里 —— 第二份直接
    退出，别两个人同时往包根搬文件。
    """

    def __init__(self, root=ROOT):
        os.makedirs(os.path.join(root, "logs"), exist_ok=True)
        self.path = os.path.join(root, "logs", "update.lock")
        self.handle = None

    def acquire(self):
        try:
            age = time.time() - os.path.getmtime(self.path)
            if age > LOCK_STALE_S:
                os.unlink(self.path)          # 上一次崩了没清，抢过来
        except OSError:
            pass
        try:
            self.handle = os.open(self.path,
                                  os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.handle, str(os.getpid()).encode("ascii"))
            return True
        except FileExistsError:
            return False
        except OSError:
            return True                       # 锁不上（权限等）就放行

    def release(self):
        if self.handle is not None:
            try:
                os.close(self.handle)
                os.unlink(self.path)
            except OSError:
                pass
            self.handle = None


def restart_game():
    """重启游戏。★ 我们此刻多半是管理员（引导器 runas 拉起来的）—— 直接
    启动 start.bat 会把管理员身份传染给整个游戏。经 explorer.exe 转一手
    拿回普通权限（explorer 以用户身份运行）；转不动就照原版 NGM 的做法
    直接启动（原版那些年就是提权重启的，行为等价不算回退）。"""
    start = os.path.join(ROOT, "start.bat")
    SW_SHOWNORMAL = 1
    try:
        if ctypes.windll.shell32.ShellExecuteW(
                None, None, "explorer.exe", f'"{start}"', None,
                SW_SHOWNORMAL) > 32:
            print(f"√ 已重启游戏：{start}")
            return
    except Exception:
        pass
    os.startfile(start)


# ---------------------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------------------

def manual_fallback(reason):
    print()
    print("==========================================")
    print("× 自动更新没能完成：" + reason)
    print("  请手动下载完整客户端：")
    print("  " + RELEASES_PAGE)
    print("==========================================")
    log_to_file("FAIL " + reason)
    hold()
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="炮炮火枪手复活版 客户端自动更新")
    parser.add_argument("--procid", type=int, default=0,
                        help="客户端升级分支经引导器透传的游戏进程号")
    parser.add_argument("--elevated", action="store_true",
                        help="（内部）本次已是管理员身份运行")
    parser.add_argument("--zip", help="（内部）复用已下载好的更新包")
    parser.add_argument("--target-version", help="（内部）跳过探针直接指定目标")
    args = parser.parse_args(argv)

    # 控制台编码兜底：非中文 Windows 的控制台（cp437 之类）打中文会炸，
    # errors="replace" 保证最多变问号、绝不中断更新。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass

    local = read_local_version()
    print("=== 炮炮火枪手 复活版 自动更新 ===")
    print(f"本地版本：{versioning.format_version(local) if local else '认不出'}")
    log_to_file(f"run local={versioning.format_version(local) if local else '?'} "
                f"elevated={int(args.elevated)} zip={args.zip}")

    if SERVER_MODULES_ERROR is not None:
        # server\ 里的模块起不来 = 包残缺。此时也得开口说话，不能闪退。
        return manual_fallback(
            f"程序包不完整（{SERVER_MODULES_ERROR}）—— 请重新下载完整客户端")

    lock = SingleInstance()
    if not lock.acquire():
        print("× 已经有一个更新程序在跑了（logs\\update.lock），这次退出。")
        hold()
        return 0
    try:
        return _run(args, local)
    finally:
        lock.release()


def _run(args, local):
    zip_path = args.zip

    # --- 提权重跑的直通路径：包已下好，只剩应用 ----------------------------
    if not zip_path:
        if local is None:
            return manual_fallback("读不出本地版本（BUILD.ver 认不出）")

        # --- 探针：问服务器「该升到哪版」 ----------------------------------
        cfg, _ = config.load(root=ROOT)
        host = cfg.get("server_address")
        print(f"· 正在探测服务器 {host}:{config.GAME_PORT}（确认需要的版本）…")
        status, wanted, message = probe_server(host, local)
        if status == "ok":
            print("√ 服务器已接受当前版本，无需更新。")
            log_to_file("probe ok, no update needed")
            hold()
            return 0
        if status == "rejected":
            print(f"· 服务器要求版本："
                  f"{versioning.format_version(wanted) if wanted else '?'}"
                  f"（{message or '文案没带上版本号，将改用最新版'}）")
        else:
            print("· 连不上服务器（单机模式或网络问题），按更新源最新版处理")
            wanted = None
        log_to_file(f"probe {status} wanted="
                    f"{versioning.format_version(wanted) if wanted else '?'}")

        # --- manifest 与目标版本 -------------------------------------------
        manifest = fetch_manifest()
        if manifest is None:
            return manual_fallback("取不到更新清单（manifest.json）")
        if args.target_version:
            wanted = versioning.parse_version_text(args.target_version)
        target = pick_target(manifest, local, wanted)
        if target is None:
            print("√ 已是最新版本，无需更新。")
            log_to_file("up to date")
            hold()
            return 0
        target_version = versioning.parse_version_text(target["version"])
        print(f"· 目标版本：{versioning.format_version(target_version)}")

        # --- 下载 + 校验（在提权之前做：临时目录不需要管理员） --------------
        try:
            zip_path = fetch_zip_cached(target, tempfile.gettempdir())
        except (IOError, OSError) as error:
            return manual_fallback(f"下载或校验失败（{error}）")
        log_to_file(f"zip ready {zip_path}")

    # --- 等游戏退出（游戏文件都被它锁着） -----------------------------------
    try:
        wait_game_exit(args.procid)
        # 本机服务端 / 中继锁着 runtime\python\python.exe —— 更新要覆盖
        # runtime，先按「exe 路径 == 本包 runtime」精确停掉它们。
        stopped = stop_package_pythons()
        if stopped:
            log_to_file(f"stopped local pythons={stopped}")
    except OSError as error:
        return manual_fallback(str(error))

    # --- 写权限：平时零提权，写不进才弹一次 UAC ------------------------------
    if not args.elevated and not root_is_writable():
        print("· 目录没有写权限，请求管理员身份继续（只会弹这一次 UAC）…")
        if elevate_and_rerun(zip_path, args.procid):
            print("√ 已把更新交给管理员窗口，本窗口可以关闭。")
            log_to_file("elevated rerun spawned")
            return 0
        return manual_fallback("没有管理员权限，写不了游戏目录")

    # --- 应用 + 收尾 ---------------------------------------------------------
    try:
        apply_update(zip_path)
    except (IOError, OSError) as error:
        return manual_fallback(f"应用更新失败（{error}）—— 已写入的文件"
                               f"下次重跑会补齐，BUILD.ver 没动，不算坏包")

    new_local = read_local_version()
    print(f"√ 更新完成，现在是 "
          f"{versioning.format_version(new_local) if new_local else '?'}")
    log_to_file(f"applied -> "
                f"{versioning.format_version(new_local) if new_local else '?'}")

    try:
        os.unlink(zip_path)
    except OSError:
        pass                                  # 留着也无妨，下次按哈希复用

    try:
        answer = input("是否立刻重启游戏？(Y=重启 / 回车=退出)：").strip()
    except EOFError:
        answer = ""
    if answer.lower() in ("y", "yes", "是"):
        restart_game()
    return 0


if __name__ == "__main__":
    # ★ 任何未捕获异常都必须「留窗 + 写日志」再退，绝不能让窗口一闪而过
    #   （用户明确要求：出错也要看得到错在哪）。正常路径的留窗在各出口的
    #   hold()；这里兜住所有意外，包括 main() 里没料到的炸弹。
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        try:
            log_to_file("CRASH " + traceback.format_exc(limit=20))
        except Exception:
            pass
        print()
        print("× 更新程序遇到未预期的错误（上方是详情）。")
        hold()
        raise SystemExit(1)
