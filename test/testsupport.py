#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试共用的小工具。

★ 它**不是**测试模块：全量跑的是 `test/test_*.py`（`run_tests.all_modules()`
现扫目录），这个文件**故意不叫 `test_*`**，所以不会被当成用例收集。
"""
from __future__ import annotations

import struct
import zlib

try:
    import lzma as _lzma
except ImportError:                 # 和 server/sevenzip.py 一样：缺了也不能让 import 崩
    _lzma = None

#: 等发送线程把队列写完的**保险丝**（秒）。
#:
#: ★★ 它**不是判据**。判据是 `Conn.flush_outbox()` 里那个条件变量
#: （「队列排空」或「发送流已废」）—— 那一路本来就是事件驱动的、一被
#: `notify` 就醒，正常情况下这个数一秒都用不上。它只防一件事：真出毛病时
#: （发送线程没起来、死锁）别让测试永久挂住。所以取得**足够长**，
#: 长到正常机器上永远走不到。
#:
#: ⚠ 以前各处写的是 `timeout=5.0`，**而且返回值被丢掉**。全量并行跑
#: （16 个进程，后台还跑着真服务端）时偶尔等不满 5 秒，测试却接着往下走、
#: 在帧里找不到包，报出来的却是「没有发出 gspRepLogin」——
#: **把一次时序超时说成了功能 bug**，2026-09-17 就这么误导过一次排查。
#:
#: 铁律 10 说的「禁止固定时间阈值」防的正是这个：固定阈值当判据用，
#: 掩盖竞态而不是消除竞态。这里的写法是把它从**判据**降级成**保险丝** ——
#: 判据交还给条件变量，超时只在「已经出事了」时兜底，而且兜到了要**说清楚**。
OUTBOX_FUSE_S = 60.0


def drain(case, conn, what="发送队列"):
    """等 `conn` 的发送队列排空；没排空就**当场失败、并说清是超时**。

    ★★ 关键不在超时值有多大，在于**不许把「还没写完」和「根本没发」
    混成同一个报错**。`flush_outbox()` 的返回值恰好就是区分这两者的那个
    事实（`True` = 真排空了），把它丢掉，后面任何「找不到某个包」的断言
    都会变成一句误导人的错话。

    返回 `conn`，方便串起来写。
    """
    if conn.flush_outbox(timeout=OUTBOX_FUSE_S):
        return conn
    broken = getattr(conn, "send_broken", False)
    case.fail("%s 在 %g 秒内没排空（还剩 %d 个包没写出去，发送流%s）—— "
              "这是时序或死锁，**不是**「服务端没发这个包」，"
              "别照着这条错话去查协议。"
              % (what, OUTBOX_FUSE_S, len(conn.outbox),
                 "已废" if broken else "还活着"))


# ====================================================================== 7z 读取器
# X17（D101 / §142）：`server/sevenzip.py` 只有写，没有读；标准库也没有 7z。
# 这是给测试用的**严格**读取器：只认 sevenzip.py 写的那一小块，外加 7-Zip 自己会写的
# kDummy / 属性 / 时间 —— 碰到别的字段、字段顺序不对、记录没用完、CRC 不对，一律
# `ValueError`。宽松的读取器会把写入器的格式错误放过去（7-Zip / libarchive 可不放过）。
#
# ★ 「写入器和读取器都是自己写的」会互相对得上而一起错（test/README.md 里 pkn 的教训），
#   所以 `test_sevenzip` 另外拿**真 7-Zip 打的夹具**核这个读取器，拿**真 7-Zip** 核写入器。

SEVENZIP_SIGNATURE = b"7z\xbc\xaf\x27\x1c"
_FILETIME_EPOCH = 116444736000000000


class SevenZipEntry:
    """7z 里的一项。`method` 是 `"copy"` / `"lzma2"`，空文件和目录是 `None`。"""

    __slots__ = ("name", "data", "is_dir", "method", "folder", "mtime_ns", "attributes")

    def __init__(self, name):
        self.name = name
        self.data = b""
        self.is_dir = False
        self.method = None
        self.folder = None
        self.mtime_ns = None
        self.attributes = None


class SevenZipArchive:
    """`read_7z()` 的结果。`entries` 按目录头里的顺序；`folders` 是
    `[(method, 属性字节, 压缩后字节, 解开后字节, 子流数), ...]`；`header` 是目录头原文。"""

    def __init__(self, entries, folders, header):
        self.entries = entries
        self.folders = folders
        self.header = header

    @property
    def names(self):
        return [entry.name for entry in self.entries]

    def entry(self, name):
        for entry in self.entries:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def read(self, name):
        return self.entry(name).data


class _Cursor:
    def __init__(self, data, what="目录头"):
        self.data = data
        self.pos = 0
        self.what = what

    def byte(self):
        if self.pos >= len(self.data):
            raise ValueError("%s提前结束（第 %d 字节）" % (self.what, self.pos))
        value = self.data[self.pos]
        self.pos += 1
        return value

    def take(self, count):
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError("%s里要读 %d 字节，只剩 %d" % (self.what, count,
                                                       len(self.data) - self.pos))
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def number(self):
        """7zIn.cpp `ReadNumber`。"""
        first = self.byte()
        mask = 0x80
        value = 0
        for index in range(8):
            if not first & mask:
                return value | ((first & (mask - 1)) << (8 * index))
            value |= self.byte() << (8 * index)
            mask >>= 1
        return value

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.take(8))[0]

    def bits(self, count):
        raw = self.take((count + 7) // 8)
        flags = [bool(raw[index >> 3] & (0x80 >> (index & 7))) for index in range(count)]
        if count % 8 and raw[-1] & (0xFF >> (count % 8)):
            raise ValueError("%s的位向量末尾补位不是 0" % self.what)
        return flags

    def defined(self, count):
        """「AllAreDefined 字节，不是 1 就再跟一串位向量」。"""
        return [True] * count if self.byte() else self.bits(count)

    def expect(self, value, what):
        got = self.byte()
        if got != value:
            raise ValueError("%s：期望 0x%02x，读到 0x%02x（第 %d 字节）"
                             % (what, value, got, self.pos - 1))

    def done(self):
        if self.pos != len(self.data):
            raise ValueError("%s没用完：还剩 %d 字节" % (self.what, len(self.data) - self.pos))


def read_7z(data):
    """解析并**校验**一个 7z（`bytes`），返回 `SevenZipArchive`。任何不对都抛 `ValueError`。"""
    data = bytes(data)
    if len(data) < 32 or data[:6] != SEVENZIP_SIGNATURE:
        raise ValueError("不是 7z：签名不对")
    if data[6] != 0:
        raise ValueError("7z 主版本不是 0：%d" % data[6])
    start_crc, = struct.unpack("<I", data[8:12])
    if zlib.crc32(data[12:32]) != start_crc:
        raise ValueError("StartHeaderCRC 不对")
    next_offset, next_size, next_crc = struct.unpack("<QQI", data[12:32])
    if 32 + next_offset + next_size != len(data):
        raise ValueError("文件长度 %d ≠ 32 + 目录头偏移 %d + 长度 %d"
                         % (len(data), next_offset, next_size))
    if next_size == 0:
        if next_offset or next_crc:
            raise ValueError("空 7z 的目录头偏移 / CRC 应该都是 0")
        return SevenZipArchive([], [], b"")
    header = data[32 + next_offset:]
    if zlib.crc32(header) != next_crc:
        raise ValueError("NextHeaderCRC 不对")
    cur = _Cursor(header)
    kind = cur.byte()
    if kind == 0x17:
        raise ValueError("目录头是压缩过的（kEncodedHeader）—— 夹具要用 7z a -mhc=off 打")
    if kind != 0x01:
        raise ValueError("目录头第一个字节不是 kHeader：0x%02x" % kind)
    kind = cur.byte()
    streams = None
    if kind == 0x04:
        streams = _read_streams_info(cur)
        kind = cur.byte()
    entries = []
    if kind == 0x05:
        entries = _read_files_info(cur)
        kind = cur.byte()
    if kind != 0x00:
        raise ValueError("目录头里不认识的段：0x%02x" % kind)
    cur.done()

    folders = []
    substreams = []
    if streams is not None:
        pack_pos, pack_sizes, coders, unpack_sizes, folder_crcs, counts, sizes, crcs = streams
        offset = 32 + pack_pos
        crc_iter = iter(crcs)
        for index, (method, props) in enumerate(coders):
            packed = data[offset:offset + pack_sizes[index]]
            offset += pack_sizes[index]
            raw = _decode_folder(method, props, packed)
            if len(raw) != unpack_sizes[index]:
                raise ValueError("folder %d 解出 %d 字节，目录头说 %d"
                                 % (index, len(raw), unpack_sizes[index]))
            if folder_crcs[index] is not None and zlib.crc32(raw) != folder_crcs[index]:
                raise ValueError("folder %d 的 CRC 不对" % index)
            folders.append((method, props, pack_sizes[index], unpack_sizes[index],
                            counts[index]))
            pos = 0
            for size in sizes[index]:
                chunk = raw[pos:pos + size]
                pos += size
                if counts[index] == 1 and folder_crcs[index] is not None:
                    want = folder_crcs[index]
                else:
                    want = next(crc_iter, None)
                if want is None:
                    raise ValueError("folder %d 有子流没有 CRC" % index)
                if zlib.crc32(chunk) != want:
                    raise ValueError("folder %d 的子流 CRC 不对" % index)
                substreams.append((chunk, method, index))
        if offset != 32 + next_offset:
            raise ValueError("数据区没有正好接到目录头：%d ≠ %d" % (offset, 32 + next_offset))
        if next(crc_iter, None) is not None:
            raise ValueError("SubStreamsInfo 的 CRC 比子流多")
    elif next_offset:
        raise ValueError("没有 MainStreamsInfo，却有 %d 字节数据区" % next_offset)

    with_data = [entry for entry in entries if entry.method == "?"]
    if len(with_data) != len(substreams):
        raise ValueError("有数据的项 %d 个，子流 %d 个" % (len(with_data), len(substreams)))
    for entry, (chunk, method, index) in zip(with_data, substreams):
        entry.data = chunk
        entry.method = method
        entry.folder = index
    return SevenZipArchive(entries, folders, header)


def _read_streams_info(cur):
    cur.expect(0x06, "kPackInfo")
    pack_pos = cur.number()
    pack_count = cur.number()
    cur.expect(0x09, "PackInfo 的 kSize")
    pack_sizes = [cur.number() for _ in range(pack_count)]
    kind = cur.byte()
    if kind == 0x0A:                            # 数据流的 CRC（7-Zip 不写，读到就跳过）
        for flag in cur.defined(pack_count):
            if flag:
                cur.u32()
        kind = cur.byte()
    if kind != 0x00:
        raise ValueError("PackInfo 里不认识的字段：0x%02x" % kind)

    cur.expect(0x07, "kUnpackInfo")
    cur.expect(0x0B, "kFolder")
    folder_count = cur.number()
    if cur.byte() != 0:
        raise ValueError("folder 定义放在外部数据流里（External）—— 不认")
    coders = []
    for _ in range(folder_count):
        if cur.number() != 1:
            raise ValueError("只认一个编码器的 folder")
        flag = cur.byte()
        if flag & 0xC0 or flag & 0x10:
            raise ValueError("编码器标志不认：0x%02x" % flag)
        coder_id = cur.take(flag & 0x0F)
        props = cur.take(cur.number()) if flag & 0x20 else b""
        if coder_id == b"\x00" and not props:
            coders.append(("copy", props))
        elif coder_id == b"\x21" and len(props) == 1:
            coders.append(("lzma2", props))
        else:
            raise ValueError("不认识的编码器：%s / %s" % (coder_id.hex(), props.hex()))
    if pack_count != folder_count:
        raise ValueError("数据流 %d 个、folder %d 个（一个编码器的 folder 应该一一对应）"
                         % (pack_count, folder_count))
    cur.expect(0x0C, "kCodersUnpackSize")
    unpack_sizes = [cur.number() for _ in range(folder_count)]
    folder_crcs = [None] * folder_count
    kind = cur.byte()
    if kind == 0x0A:
        for index, flag in enumerate(cur.defined(folder_count)):
            if flag:
                folder_crcs[index] = cur.u32()
        kind = cur.byte()
    if kind != 0x00:
        raise ValueError("UnpackInfo 里不认识的字段：0x%02x" % kind)

    counts = [1] * folder_count
    sizes = None
    crcs = []
    kind = cur.byte()
    if kind == 0x08:
        # ★ 顺序只能是 0D → 09 → 0A → 00（libarchive 的要求），乱序在这里就报。
        kind = cur.byte()
        if kind == 0x0D:
            counts = [cur.number() for _ in range(folder_count)]
            kind = cur.byte()
        if kind == 0x09:
            sizes = []
            for index, count in enumerate(counts):
                part = [cur.number() for _ in range(max(0, count - 1))]
                if count:
                    rest = unpack_sizes[index] - sum(part)
                    if rest < 0:
                        raise ValueError("folder %d 的子流长度加起来超了" % index)
                    part.append(rest)
                sizes.append(part)
            kind = cur.byte()
        if kind == 0x0A:
            unknown = sum(count for index, count in enumerate(counts)
                          if not (count == 1 and folder_crcs[index] is not None))
            crcs = [cur.u32() if flag else None for flag in cur.defined(unknown)]
            if None in crcs:
                raise ValueError("SubStreamsInfo 有没定义的 CRC")
            kind = cur.byte()
        if kind != 0x00:
            raise ValueError("SubStreamsInfo 顺序不对或有不认识的字段：0x%02x" % kind)
        kind = cur.byte()
    if sizes is None:
        if any(count > 1 for count in counts):
            raise ValueError("有 folder 子流数 >1，却没写 kSize")
        sizes = [[unpack_sizes[index]] if count == 1 else []
                 for index, count in enumerate(counts)]
    if kind != 0x00:
        raise ValueError("StreamsInfo 没有以 kEnd 结束：0x%02x" % kind)
    return (pack_pos, pack_sizes, coders, unpack_sizes, folder_crcs, counts, sizes, crcs)


def _read_files_info(cur):
    count = cur.number()
    empty_stream = [False] * count
    empty_file = None
    seen = []
    entries = None
    mtimes = [None] * count
    attributes = [None] * count
    while True:
        prop = cur.byte()
        if prop == 0x00:
            break
        body = _Cursor(cur.take(cur.number()), "FilesInfo 属性 0x%02x " % prop)
        seen.append(prop)
        if prop == 0x0E:
            empty_stream = body.bits(count)
        elif prop == 0x0F:
            if 0x0E not in seen:
                raise ValueError("kEmptyFile 必须排在 kEmptyStream 后面")
            empty_file = body.bits(sum(empty_stream))
        elif prop == 0x11:
            if body.byte() != 0:
                raise ValueError("名字放在外部数据流里（External）—— 不认")
            raw = body.take(len(body.data) - body.pos)
            if len(raw) % 2:
                raise ValueError("名字区长度不是偶数")
            names = []
            start = 0
            for pos in range(0, len(raw), 2):
                if raw[pos:pos + 2] == b"\x00\x00":
                    names.append(raw[start:pos].decode("utf-16-le"))
                    start = pos + 2
            if start != len(raw) or len(names) != count or not all(names):
                raise ValueError("名字区不是正好 %d 个非空名字" % count)
            entries = [SevenZipEntry(name) for name in names]
        elif prop in (0x12, 0x13, 0x14):
            flags = body.defined(count)
            if body.byte() != 0:
                raise ValueError("时间放在外部数据流里（External）—— 不认")
            values = [body.u64() if flag else None for flag in flags]
            if prop == 0x14:
                mtimes = values
        elif prop == 0x15:
            flags = body.defined(count)
            if body.byte() != 0:
                raise ValueError("属性放在外部数据流里（External）—— 不认")
            attributes = [body.u32() if flag else None for flag in flags]
        elif prop == 0x19:
            body.take(len(body.data))           # kDummy：只是对齐用的填充
        else:
            raise ValueError("FilesInfo 里不认识的属性：0x%02x" % prop)
        body.done()
    if count and entries is None:
        raise ValueError("FilesInfo 没有名字")
    entries = entries or []
    empty_index = 0
    for index, entry in enumerate(entries):
        if empty_stream[index]:
            is_file = empty_file[empty_index] if empty_file is not None else False
            entry.is_dir = not is_file
            empty_index += 1
        else:
            entry.method = "?"                  # read_7z 按顺序填上真正的数据
        if mtimes[index] is not None:
            entry.mtime_ns = (mtimes[index] - _FILETIME_EPOCH) * 100
        entry.attributes = attributes[index]
    return entries


def _decode_folder(method, props, packed):
    if method == "copy":
        if len(packed) == 0:
            raise ValueError("Copy folder 是空的（写入器不该写出 0 字节的数据流）")
        return packed
    if _lzma is None:
        raise ValueError("这个 Python 没带 lzma，解不了 LZMA2")
    prop = props[0]
    if prop > 40:
        raise ValueError("LZMA2 字典属性不合法：%d" % prop)
    dict_size = 0xFFFFFFFF if prop == 40 else (2 | (prop & 1)) << (prop // 2 + 11)
    decoder = _lzma.LZMADecompressor(format=_lzma.FORMAT_RAW, filters=[
        {"id": _lzma.FILTER_LZMA2, "dict_size": max(dict_size, 4096)}])
    raw = decoder.decompress(packed)
    if not decoder.eof or decoder.unused_data:
        raise ValueError("LZMA2 流没有正好在结束标记处结束")
    return raw
