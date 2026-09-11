#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复活项目自己的版本号：解析、线上编码、最低版本门禁。

背景：排查玩家问题时最大的痛点是「拿到 log 却不知道对方跑的是哪个版本」。
原版客户端只有一个写死的 311（BigShot.exe ``0x54d98f``，连游戏服时裸发
int32，见 `gameserver.CLIENT_VERSION` / re/packet_api.md §1.2），所有复活
版本在服务端看来一模一样。本模块给复活项目立一套自己的 ``V主.次.修订``：

* **打包侧**（tools/build-*.ps1）：``tools/build-ver.config`` 里的版本号
  写进包根 ``BUILD.ver``（JSON），成果物文件夹名带上版本（点转横杠）。
* **客户端侧**（hook/bshook.c）：每次启动读包根 ``BUILD.ver``，把版本号
  **编码成 int32 补丁进 ``0x54d98f``** —— 连接时裸发的还是那 4 个字节，
  协议流布局一个位都不动（SimpleCipher 是有状态流密码，多插一个字节都会
  让后面全部错位），中继（relay.py 纯字节转发）也天然透明。
* **服务端侧**（gameserver.py）：握手收到 int32 后解码。仍等于 311 的
  = 旧版客户端（没上报版本）；其他值解码出 ``V主.次.修订``，写进
  online.log，并和 ``server-ClientFilter.config`` 里的最低版本比较，
  不达标就按「版本过旧」拒绝。

**线上编码**：``major*1_000_000 + minor*1_000 + patch``（0.2.7 -> 2007）。
约束 major<=2146、minor/patch<=999（再大装不进 int32 / 解不开）；
**311 是保留值**（= 0.0.311，打包校验时直接拒绝，避免和原版混淆）。

解析刻意宽容 —— ``tools/build-ver.config`` 和 ``server-ClientFilter.config``
都是给人用记事本手改的：前后空格 / CRLF / UTF-8 BOM / UTF-16 BOM /
有无 v 或 V 前缀 / 大小写，一律要能吃下（server.config 的同一套哲学）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import threading

import config

#: 原版客户端在握手里裸发的版本号。**也是「旧版客户端（没上报复活版本）」的
#: 判据**：bshook 补丁过的一定不等于它。和 `gameserver.CLIENT_VERSION`
#: 必须同值（test_versioning.py 钉住）。
LEGACY_WIRE_VERSION = 311

#: 编码里每一段的进制基数：major*WIRE_BASE^2 + minor*WIRE_BASE + patch。
#: 1000 -> minor/patch 各留 3 位，major 最大 2146（int32 装得下的极限）。
WIRE_BASE = 1_000

# --------------------------------------------------------------------------
#  ★ V0.3.2 起的「带校验位」编码（§89 / D85）
#
#  为什么要它：旧编码里上报的数字**完全来自包根 BUILD.ver 这个明文 JSON**，
#  倒卖者把版本号改大就绕过了版本门禁，连重编都不用 —— 于是他可以继续卖
#  不带反倒卖公告的旧客户端。
#
#  修法：把 `bshook.dll` 自己的 SHA-256 折进上报值。仍然是那 4 个字节
#  （SimpleCipher 是有状态流密码，多插一个字节后面全错位，见本文件开头），
#  布局改成：
#
#      bit31      = 1        新编码的标记（旧客户端裸发的是小正数，恒为 0）
#      bit30..24  = 校验位   由 sha256(bshook.dll) 和版本号一起算出来
#      bit23..0   = 版本号   ★ 明着放
#
#  ★★ 版本号**必须明着放**，不能连它一起加密：否则服务端的
#  `manifest-hook.json` 一旦比客户端旧（发了新客户端、云服还没换包），
#  新客户端一条都匹配不上 -> 被判成「改过」-> 强制更新到它**已经是**的
#  版本 -> **死循环**。明着放，服务端才分得清「这个版本我不认识」和
#  「这个版本我认识、但校验位不对」。
#
#  ⚠ 客户端自校验**永远能被绕过**（把算 hash 那段 patch 掉就行），而且
#  期望值就明写在随包发出去的 manifest-hook.json 里。这一步的意义只是把
#  门槛从「改个 JSON 文件」抬到「真的会用调试器」，别当成不可破。
# --------------------------------------------------------------------------

#: 新编码的标记位。
WIRE_V2_FLAG = 0x8000_0000
#: 低 24 位放版本号。
WIRE_V2_VERSION_MASK = 0x00FF_FFFF
#: ★ 但**合法值只到 15.999.999**（major <= 15），不是整个 24 位都算数 ——
#: 否则 `0xFFFFFFFF`（-1，典型的乱值）会被解成一个「合法」的 V16.777.215。
#: 乱值必须当乱值，别让它混成一个看起来像版本号的东西。
WIRE_V2_VERSION_MAX = 15 * WIRE_BASE * WIRE_BASE + 999 * WIRE_BASE + 999
WIRE_V2_TAG_SHIFT = 24
#: 7 位校验位。位数不是安全强度 —— 期望值本来就是公开的（见上面那条 ⚠），
#: 它挡的是「随手改了字节」，不是「知道算法的人」。
WIRE_V2_TAG_MASK = 0x7F

#: 客户端包里那份 hook 的文件名（`manifest-hook.json` 记的就是它的 SHA-256）。
HOOK_BINARY_NAME = "bshook.dll"

#: 「跟着 server-ClientFilter.config 走（每次握手热重载）」的哨兵值。
#: app.py（统一入口）用它；单跑 gameserver.py 或测试给具体值或 0。
FOLLOW_FILE = "auto"

#: 配置文件名。放在包根的 `config\` 子目录里（和 server.config 集中管理），
#: 客户端包和服务端包里都要有（两边的服务端行为必须一致）。
CLIENT_FILTER_FILENAME = "server-ClientFilter.config"

_lock = threading.Lock()
#: ``{路径: (mtime, size, (min_version, warnings))}`` —— 热重载缓存。
#: 改配置不用重启服务器：每次握手来查一眼 mtime，变了才重读。
_filter_cache = {}


def parse_version_text(text):
    """版本号文本 -> ``(major, minor, patch)`` 元组；认不出返回 ``None``。

    认得的写法（前后空格随意）::

        0.2.7   v0.2.7   V0.2.7   0.2   5.12.23   0
        # 行首 # 或 ; 的行当注释跳过，取第一行认得出的
    """
    if text is None:
        return None
    for raw in str(text).splitlines():
        line = raw.strip().lstrip("﻿").rstrip("\r")
        if not line or line[0] in "#;":
            continue
        if line[0] in "vV":
            line = line[1:].strip()
        parts = line.split(".")
        if not 1 <= len(parts) <= 3:
            return None
        numbers = []
        for part in parts:
            if not part.isdigit():       # 空段 / 负号 / 空格 / 全角都挡在这
                return None
            numbers.append(int(part))
        while len(numbers) < 3:
            numbers.append(0)
        major, minor, patch = numbers
        if major > 2146 or minor > 999 or patch > 999:
            return None
        return (major, minor, patch)
    return None


def read_version_file(path):
    """读一个只写版本号的配置文件 -> 元组或 ``None``。

    记事本「另存为」可能存成 UTF-8 with BOM 甚至 UTF-16，按 BOM 认一下。
    文件不存在 / 读不了 / 内容认不出，一律返回 ``None``，原因由调用方
    （`load_client_filter` / 打包脚本）自己说清楚。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    else:
        text = data.decode("utf-8-sig", errors="replace")
    return parse_version_text(text)


def format_version(version):
    """``(0, 2, 7)`` -> ``"V0.2.7"``。日志里固定这个格式（大写 V）。"""
    if not version:
        return "?"
    return "V" + ".".join(str(part) for part in version)


def encode_wire(version):
    """``(0, 2, 7)`` -> ``2007``（bshook 补丁进 0x54d98f 的那个 int32）。

    不合法（段超限 / 等于保留值 311 / 编出来还落在原版客户端的小数字
    区间里）抛 ``ValueError`` —— 打包脚本靠它把写错的版本号拦在打包之前。
    """
    if not version or len(version) != 3:
        raise ValueError(f"版本号不是三段数字: {version!r}")
    major, minor, patch = version
    if major > 2146 or minor > 999 or patch > 999:
        raise ValueError(
            f"版本号 {format_version(version)} 段值超限"
            f"（要求 major<=2146、minor/patch<=999）")
    wire = major * WIRE_BASE * WIRE_BASE + minor * WIRE_BASE + patch
    if wire == LEGACY_WIRE_VERSION:
        # 0.0.311 编出来正好是原版的 311，服务端会把它当「没上报版本的
        # 旧版客户端」。这种巧合宁可打包时报错，不许悄悄上线。
        raise ValueError(
            f"版本号 {format_version(version)} 编码后等于原版保留值 311，换一个")
    if wire < WIRE_BASE:
        # 0.0.x 这种编码落在原版客户端版本号（310/311/312…）的区间里，
        # 和真正的老客户端分不开。复活项目版本至少从 0.1.0 起步。
        raise ValueError(
            f"版本号 {format_version(version)} 太低（< 0.1.0），"
            f"编码后会与原版客户端版本号混淆")
    return wire


def hook_tag(sha256_hex, wire_version):
    """由 hook 的 SHA-256 和版本号算出 7 位校验位。

    ★ `hook/bshook.c` 的 `notice_hook_tag()` 必须**逐位同算法**：
    对「32 个原始 hash 字节 ‖ 版本号的 4 字节小端」再做一次 SHA-256，
    取结果第一个字节的低 7 位。两边分叉的症状是「谁都登不上」，
    `server/test_versioning.py` 用固定向量钉着。
    """
    raw = bytes.fromhex(str(sha256_hex).strip())
    if len(raw) != 32:
        raise ValueError(f"不是 SHA-256：{sha256_hex!r}")
    digest = hashlib.sha256(raw + struct.pack("<I", int(wire_version))).digest()
    return digest[0] & WIRE_V2_TAG_MASK


def encode_wire_v2(version, sha256_hex):
    """``(0, 3, 2)`` + hook 的 SHA-256 -> 带校验位的上报值（无符号 int32）。

    打包脚本用它算出「这一版应该上报什么」，测试拿它和 C 侧对答案。
    """
    wire = encode_wire(version)
    if wire > WIRE_V2_VERSION_MAX:
        raise ValueError(
            f"版本号 {format_version(version)} 装不进新编码的低 24 位"
            f"（要求 major <= 15）")
    return WIRE_V2_FLAG | (hook_tag(sha256_hex, wire) << WIRE_V2_TAG_SHIFT) | wire


def split_wire(wire):
    """编码后的整数 -> 版本元组；认不出返回 ``None``（不判新旧编码，只拆数字）。"""
    if not WIRE_BASE <= wire <= 2146 * WIRE_BASE * WIRE_BASE + 999 * WIRE_BASE + 999:
        return None
    major, rest = divmod(wire, WIRE_BASE * WIRE_BASE)
    minor, patch = divmod(rest, WIRE_BASE)
    return (major, minor, patch)


def decode_wire(wire):
    """握手收到的 int32 -> 版本元组；``None`` = 旧版客户端（没上报版本）。

    原版客户端永远裸发 311；bshook 补丁过的值一定 >= 1000。负数和超出
    编码上限的当乱值处理（按旧版对待，日志里会带着原始值）。

    ★ 新编码（带校验位）也认 —— 只想要版本号时用它就够了；要连校验位
    一起拿走 `decode_wire_ex`。
    """
    version, _tag = decode_wire_ex(wire)
    return version


def decode_wire_ex(wire):
    """握手收到的 int32 -> ``(版本元组或 None, 校验位或 None)``。

    ``校验位 is None`` 表示对方用的是**旧编码**（没有完整性校验）——
    调用方据此决定要不要按「这个版本本该带校验位」处理。
    """
    try:
        wire = int(wire)
    except (TypeError, ValueError):
        return None, None
    unsigned = wire & 0xFFFF_FFFF          # 握手是按 int32 解的，可能是负数

    if unsigned & WIRE_V2_FLAG:
        code = unsigned & WIRE_V2_VERSION_MASK
        if code > WIRE_V2_VERSION_MAX:
            return None, None                  # 乱值（比如 -1 = 0xFFFFFFFF）
        tag = (unsigned >> WIRE_V2_TAG_SHIFT) & WIRE_V2_TAG_MASK
        version = split_wire(code)
        return (version, tag) if version is not None else (None, None)

    if unsigned == LEGACY_WIRE_VERSION:
        return None, None
    return split_wire(unsigned), None


def client_filter_path(root=None):
    """``server-ClientFilter.config`` 的完整路径（包根的 ``config`` 子目录下）。"""
    return os.path.join(os.path.abspath(root or config.PACKAGE_ROOT),
                        config.CONFIG_DIR, CLIENT_FILTER_FILENAME)


def load_client_filter(path=None, _reload=False):
    """读最低客户端版本 -> ``(min_version_or_None, warnings)``。

    * ``None`` = **不限制**：填 0 / 0.0.0、文件不存在、内容认不出，都是它
      （宁可放行不可停服 —— server.config 的 fail-open 哲学，D 一脉相承）；
      识别失败的具体原因放进 warnings，由调用方打到日志里。
    * 结果按 mtime+size 缓存：改完配置**不用重启服务器**，下一条连接就按
      新值判。``_reload`` 只给测试用（改文件不动 mtime 粒度的极端情况）。
    """
    path = path or client_filter_path()
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _lock:
            _filter_cache.pop(path, None)
        return None, [f"没有找到 {path}，不限制客户端版本（旧版客户端也可以连）"]

    with _lock:
        cached = _filter_cache.get(path)
    if cached and cached[0] == stamp and not _reload:
        return cached[1]

    version = read_version_file(path)
    if version is None:
        result = (None, [f"{path} 的内容认不出是版本号"
                         f"（要形如 0.2.7 / v0.2.7），不限制客户端版本"])
    elif version == (0, 0, 0):
        result = (None, [])
    else:
        result = (version, [])
    with _lock:
        _filter_cache[path] = (stamp, result)
    return result


#: 包根 ``BUILD.ver`` 的文件名（客户端包和服务端包都有，打包脚本写入）。
BUILD_VER_FILENAME = "BUILD.ver"

#: ``{路径: (mtime, size, (版本元组或 None, 警告列表))}`` —— 同
#: `load_client_filter` 的 mtime 热重载缓存。BUILD.ver 只在换包时变，
#: 换包必然重启，但照抄同一套缓存模式最省心，测试也好写。
_own_cache = {}

#: ``"version"`` 键的兜底扫描模式（完整 JSON 解析失败时用，同 bshook 的
#: ``read_build_ver``：只认第一个 ``"version"`` 键，不做完整解析）。
_VERSION_KEY_RE = re.compile(r'"version"\s*:\s*"([^"]*)"')


def load_own_version(root=None, _reload=False):
    """读包根 ``BUILD.ver`` 的 ``version`` 字段 -> ``(版本元组或 None, 警告列表)``。

    「这台服务器自己是哪个批次」——版本门禁的拒绝文案带上它，客户端更新器
    （``updater\src\probe.c`` 的探针）从文案里解析出该升到哪个版本，
    成对发布（D079）的客户端 / 服务端靠这句话对上批次。

    BUILD.ver 是我们自己脚本写的 JSON（``version`` 键永远第一个）。先做
    完整 JSON 解析，失败再退回 bshook 同款的「扫第一个 ``version`` 键」，
    两套都认不出才返回 ``None``（调用方有兜底文案，绝不因为这句话让
    服务器起不来 —— fail-open，server.config 哲学）。
    """
    path = os.path.join(os.path.abspath(root or config.PACKAGE_ROOT),
                        BUILD_VER_FILENAME)
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _lock:
            _own_cache.pop(path, None)
        return None, [f"没有找到 {path}，读不出服务器自己的版本号"]

    with _lock:
        cached = _own_cache.get(path)
    if cached and cached[0] == stamp and not _reload:
        return cached[1]

    version = None
    warnings = []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as error:
        return None, [f"读不了 {path}（{error}）"]
    text = data.decode("utf-8-sig", errors="replace")
    value = None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            value = obj.get("version")
    except ValueError:
        m = _VERSION_KEY_RE.search(text)     # bshook 同款兜底扫描
        value = m.group(1) if m else None
    if value is None:
        warnings.append(f"{path} 里找不到 version 字段")
    else:
        version = parse_version_text(value)
        if version is None:
            warnings.append(f"{path} 的 version 值 {value!r} 认不出是版本号")
    result = (version, warnings)
    with _lock:
        _own_cache[path] = (stamp, result)
    return result


#: 读不出自己的版本号时，页面上顶替版本号的那几个字。★ 不是空字符串 ——
#: 徽标整个消失的话，看的人只会以为「这一版页面没有版本号」；真正的事实是
#: 「这台服务器的包根没有 BUILD.ver」，那是一条要去查的线索，别把它藏起来。
UNKNOWN_VERSION_TEXT = "版本未知"


def own_version_text(root=None):
    """这台服务器自己的版本号，**给人看的一行字**：``"V0.3.2"``。

    注册页和管理页的标题旁边都挂着它（用户 2026-09-11）——「我现在连的这台
    是哪一批」是玩家报错、运营换包时第一个要对的东西，而这两页是**唯一两个
    不用进游戏就能看到**的地方。

    读不出来既不抛也不回空，回 `UNKNOWN_VERSION_TEXT`：这两页是渲染时把
    版本号**填死进 HTML** 的，为了一个 BUILD.ver 读不到就让整页 500 毫无
    道理（fail-open，同 `load_own_version`）。
    ★ 警告不在这里打印 —— 这是**每次请求**都要走的路径，打出来就是刷屏；
      同一批警告版本门禁那条路（`gameserver.version_reject_message`）
      已经在打了。
    """
    version, _warnings = load_own_version(root)
    if version is None:
        return UNKNOWN_VERSION_TEXT
    return format_version(version)


# --------------------------------------------------------------------------
#  客户端 hook 完整性清单（manifest-hook.json）
# --------------------------------------------------------------------------

#: 清单文件名。**就放在 `server/` 目录里**，两个打包脚本都经 `Copy-ServerCode`
#: -> `Copy-HookManifest`（tools/build-common.ps1）把它带进包。★ 不是「递归拷
#: server/ 顺带的」—— 打包只拷 *.py，JSON 要显式拷；改名的话那边要跟着改。
HOOK_MANIFEST_FILENAME = "manifest-hook.json"

#: 同 `_filter_cache` 的 mtime 热重载：加一版不用重启服务器。
_hook_manifest_cache = {}

#: `verify_client_hook` 的三种结论。
HOOK_OK = "ok"
HOOK_MISMATCH = "mismatch"
HOOK_UNKNOWN = "unknown"


def hook_manifest_path():
    """`server/manifest-hook.json` 的完整路径。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        HOOK_MANIFEST_FILENAME)


def load_hook_manifest(path=None, _reload=False):
    """读清单 -> ``({版本元组: sha256hex}, warnings)``。

    认不出的条目**跳过并记一条 warning**，不让一条坏数据毁掉整张表。
    文件不存在 / 整个读不了 -> 空表（fail-open：空表 = 所有版本都
    「不认识」= 一律放行，见 `verify_client_hook`）。
    """
    path = path or hook_manifest_path()
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _lock:
            _hook_manifest_cache.pop(path, None)
        return {}, [f"没有找到 {path}，不校验客户端 hook 的完整性"]

    with _lock:
        cached = _hook_manifest_cache.get(path)
    if cached and cached[0] == stamp and not _reload:
        return cached[1]

    table = {}
    warnings = []
    try:
        with open(path, "rb") as f:
            obj = json.loads(f.read().decode("utf-8-sig", errors="replace"))
    except (OSError, ValueError) as error:
        result = ({}, [f"{path} 读不了或不是合法 JSON（{error}），"
                       f"不校验客户端 hook 的完整性"])
        with _lock:
            _hook_manifest_cache[path] = (stamp, result)
        return result

    entries = obj.get("hooks") if isinstance(obj, dict) else None
    if not isinstance(entries, list):
        result = ({}, [f"{path} 里没有 hooks 数组，不校验客户端 hook 的完整性"])
        with _lock:
            _hook_manifest_cache[path] = (stamp, result)
        return result

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        version = parse_version_text(entry.get("version"))
        digest = str(entry.get("sha256", "")).strip().lower()
        if version is None or len(digest) != 64:
            warnings.append(f"{path} 里有条目认不出，跳过：{entry!r}")
            continue
        try:
            bytes.fromhex(digest)
        except ValueError:
            warnings.append(f"{path} 里 {format_version(version)} 的 sha256 不是十六进制，跳过")
            continue
        table[version] = digest

    result = (table, warnings)
    with _lock:
        _hook_manifest_cache[path] = (stamp, result)
    return result


def hook_check_disabled_reason(manifest=None, root=None):
    """这项校验现在该不该生效？返回不生效的原因；``None`` = 正常生效。

    ★★ **安全阀，别拿掉。** 被拒的客户端会去下载**服务端自己那个版本**
    （拒绝文案里写的就是它，`version_reject_message`）。所以一旦服务端
    **自己的版本都不在清单里**，客户端下完还是不在清单里 —— **死循环，
    而且解不开**，全服没人进得来。

    出现这种状态只有一个原因：打包时清单没跟上（比如只打了服务端包）。
    那不是玩家的错，宁可这一版不校验，也不能把所有人锁在门外
    （fail-open，和 `server.config` / 版本门禁一脉相承）。
    """
    if manifest is None:
        manifest, _warnings = load_hook_manifest()
    if not manifest:
        return "清单是空的（还没打过带完整性校验的包）"
    own, _warnings = load_own_version(root)
    if own is None:
        return "读不出服务端自己的版本号（BUILD.ver）"
    if own not in manifest:
        return (f"清单里没有服务端自己的版本 {format_version(own)} "
                f"—— 打包时清单没跟上，这一版不校验（否则被拒的客户端会去下载"
                f"这个版本，下完还是不在清单里，就再也进不来了）")
    return None


def verify_client_hook(version, tag, manifest=None, root=None):
    """客户端那份 `bshook.dll` 是不是我们发的那一份？返回 ``(结论, 说明)``。

    * **OK** —— 版本在清单里，校验位对得上；或者这项校验现在不生效
      （见 `hook_check_disabled_reason`）。
    * **MISMATCH（拒，走强制更新）** —— 三种：校验位对不上 / 本该带却没带
      （算 hash 那段被拿掉了）/ **清单里根本没有这个版本**。
      ★ 最后这条按用户 2026-09-11 的要求改成拒：服务端由发版人自己管、
      **保证先于客户端更新**，所以「清单里没有」只可能是手改出来的版本号。
    * **UNKNOWN（放行）** —— 对方压根没上报版本号（原版 / 很老的客户端）。
      这类交给**版本门禁**去管，不归完整性校验 —— 不然
      `server-ClientFilter.config` 填 `0`（不限制）就失去意义了。
    """
    if version is None:
        return HOOK_UNKNOWN, "对方没上报版本号，交给版本门禁判"
    if manifest is None:
        manifest, _warnings = load_hook_manifest()

    disabled = hook_check_disabled_reason(manifest, root)
    if disabled is not None:
        return HOOK_UNKNOWN, f"这一版不校验完整性：{disabled}"

    digest = manifest.get(version)
    if digest is None:
        # ★ 比清单里最老的那条还老 = 那个版本**发布时还没有这项功能**，
        #   「清单里没有」证明不了它被改过 —— 交给版本门禁去判。
        #   不这么分的话，`server-ClientFilter.config` 填 0（不限制）就失去
        #   意义了：老客户端会被完整性校验挡住，而那不是运营者的本意。
        #   （想拦老客户端就抬门禁，那才是干这件事的地方。）
        oldest = min(manifest)
        if version < oldest:
            return HOOK_UNKNOWN, (
                f"{format_version(version)} 比清单里最老的 "
                f"{format_version(oldest)} 还老（那时还没有完整性校验），"
                f"交给版本门禁判")
        return HOOK_MISMATCH, (
            f"清单里没有 {format_version(version)}"
            f"（服务端一定先于客户端更新，所以这个版本号是手改出来的）")
    if tag is None:
        return HOOK_MISMATCH, (
            f"{format_version(version)} 本该带校验位却没带"
            f"（bshook.dll 里算 hash 那段被动过？）")
    want = hook_tag(digest, encode_wire(version))
    if tag != want:
        return HOOK_MISMATCH, (
            f"{format_version(version)} 的校验位 {tag} != 应有的 {want}"
            f"（bshook.dll 被改过）")
    return HOOK_OK, f"{format_version(version)} 的 bshook.dll 校验通过"
