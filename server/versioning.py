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

import json
import os
import re
import threading

import config

#: 原版客户端在握手里裸发的版本号。**也是「旧版客户端（没上报复活版本）」的
#: 判据**：bshook 补丁过的一定不等于它。和 `gameserver.CLIENT_VERSION`
#: 必须同值（test_versioning.py 钉住）。
LEGACY_WIRE_VERSION = 311

#: 编码里每一段的进制基数：major*WIRE_BASE^2 + minor*WIRE_BASE + patch。
#: 1000 -> minor/patch 各留 3 位，major 最大 2146（int32 装得下的极限）。
WIRE_BASE = 1_000

#: 「跟着 server-ClientFilter.config 走（每次握手热重载）」的哨兵值。
#: app.py（统一入口）用它；单跑 gameserver.py 或测试给具体值或 0。
FOLLOW_FILE = "auto"

#: 配置文件名。放在包根目录（= start.bat 同目录 = server/ 的上一级），
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


def decode_wire(wire):
    """握手收到的 int32 -> 版本元组；``None`` = 旧版客户端（没上报版本）。

    原版客户端永远裸发 311；bshook 补丁过的值一定 >= 1000。负数和超出
    编码上限的当乱值处理（按旧版对待，日志里会带着原始值）。
    """
    try:
        wire = int(wire)
    except (TypeError, ValueError):
        return None
    if wire == LEGACY_WIRE_VERSION:
        return None
    if not WIRE_BASE <= wire <= 2146 * WIRE_BASE * WIRE_BASE + 999 * WIRE_BASE + 999:
        return None
    major, rest = divmod(wire, WIRE_BASE * WIRE_BASE)
    minor, patch = divmod(rest, WIRE_BASE)
    return (major, minor, patch)


def client_filter_path(root=None):
    """``server-ClientFilter.config`` 的完整路径（包根目录下）。"""
    return os.path.join(os.path.abspath(root or config.PACKAGE_ROOT),
                        CLIENT_FILTER_FILENAME)


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
    （``tools/update_client.py`` 的探针）从文案里解析出该升到哪个版本，
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
