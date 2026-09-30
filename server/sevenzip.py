#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 7z 写入器（X17，D101 / §142）：只写，而且只写我们要的那一小块格式。

两个用处：

* 管理页「待下载 7z 包」（`logpack.write_7z`）—— **固实**：要压的成员共用一个 LZMA2 流；
* 客户端崩溃包（`crashwatch`）—— **非固实**：一个成员一个 folder，每个成员 `end()` 之后
  `tell()` 就是精确体积（「加不下就跳过」的额度靠它），读坏的成员还能 `rollback()`。

## 为什么自己写

标准库没有 7z；py7zr 是第三方、还带一串要编译的依赖。7z 容器本身很简单：
开头 32 字节签名头 + 数据区 + 末尾一个目录头。压缩直接用标准库 `lzma` 的原始 LZMA2 流
（`FORMAT_RAW` + `FILTER_LZMA2`）—— 那正是 7z 里 LZMA2 编码器的数据，末尾自带 `00` 结束标记。

## 为什么 `fp` 必须可 seek（也是 7z 做不到边压边发的原因）

签名头里要写「尾部目录头的偏移 / 长度 / CRC」，全部写完才知道 ⇒ 先写 32 字节占位，
`close()` 时回头填。

## 只写这一小块（每条都是读取器的硬要求，§142）

* 每个 folder 只有 1 个编码器：Copy（原样存，`01 01 00`）或 LZMA2（`01 21 21 01 <字典码>`）；
  编码方式一换就开新 folder，所以对成员顺序没有要求。
* 一个字节都没有的成员 = kEmptyStream **加** kEmptyFile —— 少了后者，7-Zip 和 libarchive
  都会把它解成**文件夹**。是不是空以**实际写进来的字节**为准，不看 stat。
* SubStreamsInfo 按 `0D → 09 → 0A → 00` 的顺序写（libarchive 只认这个顺序）；
  只有某个 folder 的子流数不是 1 时才写 `0D`，有 folder 子流数 >1 时 `09` 必写；
  每个子流都写 CRC，folder 本身不写。
* FilesInfo 按 EmptyStream → EmptyFile → Name → MTime 的顺序写；不写属性、不写 kDummy 对齐。
* 名字 UTF-16LE、分隔符 `/`（7-Zip 在 Windows 上解开时换成 `\\`）。
* 目录头不压缩（kHeader，不是 kEncodedHeader）。

## 铁律：只用标准库，CPython 3.8（Win7 运行时）也要能跑

`lzma` 是可选模块（自己编译、缺 liblzma 的 Python 没有它）：import 失败时 `AVAILABLE = False`，
Copy 照样能写；要 LZMA2 才报错。`app.py` 顶层就间接 import 这里，所以**绝不能**让 import 本身炸掉。
"""
from __future__ import annotations

import struct
import zlib

try:
    import lzma
except ImportError:                 # 自己编译、没带 liblzma 的 Python（打包带的运行时都有）
    lzma = None

#: 这个 Python 能不能压 LZMA2。`False` 时由调用方说清楚「为什么打不了 7z」。
AVAILABLE = lzma is not None

SIGNATURE = b"7z\xbc\xaf\x27\x1c"
#: 版本 0.4（7-Zip 9.x 起一直写这个）。
VERSION = b"\x00\x04"
SIGNATURE_HEADER_SIZE = 32

#: LZMA2 默认档：-1（hc4 快档）。实测 -6 只再小 13%、慢 10 倍、编码器 94 MB 内存（§142）。
DEFAULT_PRESET = 1
#: 字典 1 MiB（-1 档自己的字典；显式写出来，好算属性字节）。解压端只要这么大。
DICT_SIZE = 1 << 20

COPY = "copy"
LZMA2 = "lzma2"

# 属性 ID（DOC/7zFormat.txt）
_K_END = 0x00
_K_HEADER = 0x01
_K_MAIN_STREAMS_INFO = 0x04
_K_FILES_INFO = 0x05
_K_PACK_INFO = 0x06
_K_UNPACK_INFO = 0x07
_K_SUBSTREAMS_INFO = 0x08
_K_SIZE = 0x09
_K_CRC = 0x0A
_K_FOLDER = 0x0B
_K_CODERS_UNPACK_SIZE = 0x0C
_K_NUM_UNPACK_STREAM = 0x0D
_K_EMPTY_STREAM = 0x0E
_K_EMPTY_FILE = 0x0F
_K_NAME = 0x11
_K_MTIME = 0x14

#: FILETIME 纪元（1601-01-01）到 Unix 纪元，单位 100 ns。
_FILETIME_EPOCH = 116444736000000000


# -------------------------------------------------------------------- 编码
def encode_number(value):
    """7z 的变长无符号数（7zFormat.txt 的 UINT64）：首字节前导 1 的个数 = 后面跟几个字节，
    后面那几个是低位（小端），首字节剩下的位是高位。照 7zOut.cpp `WriteNumber`。"""
    if value < 0 or value >= 1 << 64:
        raise ValueError("7z 的数只能是 0 ~ 2^64-1：%r" % (value,))
    first = 0
    mask = 0x80
    for extra in range(8):
        if value < 1 << (7 * (extra + 1)):
            first |= value >> (8 * extra)
            return bytes([first]) + (value & ((1 << (8 * extra)) - 1)).to_bytes(extra, "little")
        first |= mask
        mask >>= 1
    return b"\xff" + struct.pack("<Q", value)


def encode_bits(flags):
    """位向量：第 k 个标志放在第 k // 8 个字节的 `0x80 >> (k % 8)` 位（高位在前），末字节补 0。"""
    out = bytearray((len(flags) + 7) // 8)
    for index, flag in enumerate(flags):
        if flag:
            out[index >> 3] |= 0x80 >> (index & 7)
    return bytes(out)


def lzma2_dict_prop(dict_size):
    """LZMA2 那 1 字节属性：`(2 | (p & 1)) << (p // 2 + 11)` 够装下字典的最小 p（Lzma2Enc.c）。"""
    for prop in range(40):
        if dict_size <= (2 | (prop & 1)) << (prop // 2 + 11):
            return prop
    return 40


def filetime(mtime_ns):
    """Unix 纳秒 → Windows FILETIME（100 ns，1601 起）。7z 里存的是 UTC，解开来时区自动对。"""
    return min(max(0, int(mtime_ns) // 100 + _FILETIME_EPOCH), (1 << 64) - 1)


def check_name(name):
    """7z 里的路径：非空、`/` 分隔、不以 `/` 开头、不含 `\\` 和 NUL、没有空段 / `.` / `..`。

    返回 UTF-16LE 编码。不合法抛 `ValueError`（编不成 UTF-16 的 `UnicodeEncodeError` 也是它）
    —— 在 `begin()` 就查，调用方可以把这个文件当「跳过」处理，而不是到 `close()` 才整包作废。
    """
    if not isinstance(name, str) or not name:
        raise ValueError("7z 成员名是空的")
    if name.startswith("/") or "\\" in name or "\0" in name:
        raise ValueError("7z 成员名不合法：%r" % (name,))
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("7z 成员名里有空段或 . / ..：%r" % (name,))
    try:
        return name.encode("utf-16-le")
    except UnicodeEncodeError as error:         # 孤立代理（Linux 上解不开的文件名）
        raise ValueError("7z 成员名编不成 UTF-16：%r" % (name,)) from error


# -------------------------------------------------------------------- 写入器
class Member:
    """`Writer.begin()` 发出去的句柄。`size` / `crc` 是写进来的原始字节。"""

    __slots__ = ("name", "encoded", "method", "size", "crc", "folder", "start", "mtime_ns")

    def __init__(self, name, encoded, method):
        self.name = name
        self.encoded = encoded
        self.method = method
        self.size = 0
        self.crc = 0
        #: 数据落在哪个 folder；一个字节都没写 = `None` = 空文件。
        self.folder = None
        #: 这个成员的第一个字节在 `fp` 里的位置（`rollback` 截回这里）。
        self.start = None
        self.mtime_ns = 0


class _Folder:
    __slots__ = ("method", "packed", "unpacked", "members", "compressor")

    def __init__(self, method, compressor=None):
        self.method = method
        self.packed = 0
        self.unpacked = 0
        #: 子流 = 落在这里的成员，按顺序。
        self.members = []
        self.compressor = compressor

    def coder_bytes(self):
        """这个 folder 的编码器描述：NumCoders=1，然后 flag（ID 长度 | 0x20 有属性）/ ID / 属性。"""
        if self.method == COPY:
            return b"\x01\x01\x00"
        return b"\x01\x21\x21\x01" + bytes([lzma2_dict_prop(DICT_SIZE)])


class Writer:
    """往可 seek 的 `fp` 里写一个 7z。用法::

        w = Writer(fp)                       # 固实；崩溃包用 solid=False
        m = w.begin("logs/server.out")       # compress=False = 原样存
        w.write(m, chunk) ...                # 源文件由调用方自己读
        w.end(m, st.st_mtime_ns)
        size = w.close()                     # 写目录头、回填签名头；fp 不关

    `fp` 从当前位置起就是这个 7z（一般是新文件的 0）。同一时刻只能有一个成员在写。
    """

    def __init__(self, fp, preset=DEFAULT_PRESET, solid=True):
        self._fp = fp
        self._preset = preset
        self._solid = solid
        self._base = fp.tell()
        self._members = []
        self._folders = []
        #: 正在收数据的那个 folder（固实时同一种编码的成员接着往里写）。
        self._open = None
        self._current = None
        self._closed = False
        fp.write(b"\0" * SIGNATURE_HEADER_SIZE)       # 占位，close() 回填

    # ------------------------------------------------------------ 成员
    def begin(self, name, compress=True):
        """开始一个成员。名字不合法抛 `ValueError`（此时什么都没写，可以放心跳过）。"""
        self._check_open()
        if self._current is not None:
            raise RuntimeError("上一个成员还没 end() / rollback()：%s" % self._current.name)
        member = Member(name, check_name(name), LZMA2 if compress else COPY)
        self._current = member
        return member

    def write(self, member, data):
        """把一块原始字节写进 `member`。"""
        self._check_current(member)
        if not data:
            return
        if member.folder is None:
            member.folder = self._folder_for(member.method)
            # ★ 起点要在**选好 folder 之后**才记：换 folder 时会先把上一个 LZMA2 流的
            #   尾巴 flush 进 fp，早记的话 rollback 会把那截尾巴一起截掉。
            member.start = self._fp.tell()
        folder = member.folder
        member.size += len(data)
        member.crc = zlib.crc32(data, member.crc)
        folder.unpacked += len(data)
        if folder.method == COPY:
            self._fp.write(data)
            folder.packed += len(data)
        else:
            out = folder.compressor.compress(data)
            if out:
                self._fp.write(out)
                folder.packed += len(out)

    def end(self, member, mtime_ns):
        """这个成员写完了。非固实时顺手把它的 folder 收尾 ⇒ `tell()` 是精确值。"""
        self._check_current(member)
        member.mtime_ns = int(mtime_ns)
        if member.folder is not None:
            member.folder.members.append(member)
            if not self._solid:
                self._close_open_folder()
        self._members.append(member)
        self._current = None

    def rollback(self, member):
        """放弃这个成员：数据从 `fp` 里截掉，就当没 `begin` 过（名字也不进目录）。

        固实 LZMA2 退不回去（压缩器已经吃进去了）⇒ `RuntimeError`，调用方只能整包作废。
        Copy 成员和非固实成员都退得回去。
        """
        self._check_current(member)
        folder = member.folder
        if folder is not None:
            if folder.method == LZMA2 and self._solid:
                raise RuntimeError("固实的 LZMA2 流退不回去：%s" % member.name)
            self._fp.seek(member.start)
            self._fp.truncate()
            if folder.members:              # 和前面的成员共用这个 Copy folder
                folder.packed -= member.size
                folder.unpacked -= member.size
            else:                           # 为它新开的 folder，整个扔掉
                self._folders.remove(folder)
                if self._open is folder:
                    self._open = None
        self._current = None

    def add_bytes(self, name, data, mtime_ns, compress=True):
        """一次写完一个内存里的成员（`MANIFEST.txt` / `meta.json` 这种）。"""
        member = self.begin(name, compress)
        self.write(member, data)
        self.end(member, mtime_ns)
        return member

    def tell(self):
        """到目前为止写出去的字节（含签名头占位，不含还没写的目录头）。

        非固实：每个成员 `end()` 之后是精确值。固实：压缩器肚子里可能还压着一截没吐出来。
        """
        return self._fp.tell() - self._base

    # ------------------------------------------------------------ 收尾
    def close(self):
        """写目录头、回填签名头，返回整个 7z 的字节数。`fp` 留给调用方关。"""
        self._check_open()
        if self._current is not None:
            raise RuntimeError("还有成员没 end()：%s" % self._current.name)
        self._close_open_folder()
        packed = self._fp.tell() - self._base - SIGNATURE_HEADER_SIZE
        if packed != sum(folder.packed for folder in self._folders):
            raise RuntimeError("数据区长度和各 folder 之和对不上：%d" % packed)
        # 一个成员都没有：照 7-Zip 自己的写法，目录头长度 0（CRC 也是空串的 0）。
        header = self._header() if self._members else b""
        self._fp.write(header)
        total = self._fp.tell() - self._base
        start = struct.pack("<QQI", packed, len(header), zlib.crc32(header))
        self._fp.seek(self._base)
        self._fp.write(SIGNATURE + VERSION + struct.pack("<I", zlib.crc32(start)) + start)
        self._fp.seek(self._base + total)
        self._closed = True
        return total

    # ------------------------------------------------------------ 内部
    def _check_open(self):
        if self._closed:
            raise RuntimeError("这个 7z 已经 close() 了")

    def _check_current(self, member):
        self._check_open()
        if member is not self._current:
            raise RuntimeError("不是正在写的那个成员：%s" % getattr(member, "name", member))

    def _folder_for(self, method):
        if self._solid and self._open is not None and self._open.method == method:
            return self._open
        self._close_open_folder()
        compressor = None
        if method == LZMA2:
            if lzma is None:
                raise RuntimeError("这个 Python 没带 lzma 模块，压不了 LZMA2")
            compressor = lzma.LZMACompressor(
                format=lzma.FORMAT_RAW,
                filters=[{"id": lzma.FILTER_LZMA2, "preset": self._preset,
                          "dict_size": DICT_SIZE}])
        folder = _Folder(method, compressor)
        self._folders.append(folder)
        self._open = folder
        return folder

    def _close_open_folder(self):
        folder = self._open
        if folder is None:
            return
        if folder.compressor is not None:
            tail = folder.compressor.flush()            # 含 LZMA2 结束标记 00
            self._fp.write(tail)
            folder.packed += len(tail)
            folder.compressor = None
        self._open = None

    def _header(self):
        out = bytearray([_K_HEADER])
        folders = self._folders
        if folders:
            out.append(_K_MAIN_STREAMS_INFO)
            # PackInfo：PackPos=0，一个 folder 一个数据流。
            out += bytes([_K_PACK_INFO]) + encode_number(0) + encode_number(len(folders))
            out.append(_K_SIZE)
            for folder in folders:
                out += encode_number(folder.packed)
            out.append(_K_END)
            # UnpackInfo：folder 定义 + 每个 folder 解出来多大。
            out += bytes([_K_UNPACK_INFO, _K_FOLDER]) + encode_number(len(folders)) + b"\x00"
            for folder in folders:
                out += folder.coder_bytes()
            out.append(_K_CODERS_UNPACK_SIZE)
            for folder in folders:
                out += encode_number(folder.unpacked)
            out.append(_K_END)
            # SubStreamsInfo：★ 顺序 0D → 09 → 0A → 00（libarchive）。
            out.append(_K_SUBSTREAMS_INFO)
            counts = [len(folder.members) for folder in folders]
            if any(count != 1 for count in counts):
                out.append(_K_NUM_UNPACK_STREAM)
                for count in counts:
                    out += encode_number(count)
            if any(count > 1 for count in counts):
                out.append(_K_SIZE)
                for folder in folders:
                    for member in folder.members[:-1]:     # 最后一个 = folder 总长 − 其余
                        out += encode_number(member.size)
            out += bytes([_K_CRC, 1])                      # AllAreDefined
            for folder in folders:
                for member in folder.members:
                    out += struct.pack("<I", member.crc)
            out.append(_K_END)
            out.append(_K_END)                             # StreamsInfo 结束
        members = self._members
        if members:
            out.append(_K_FILES_INFO)
            out += encode_number(len(members))
            empty = [member.folder is None for member in members]
            if any(empty):
                _prop(out, _K_EMPTY_STREAM, encode_bits(empty))
                # ★ 必写：没有这一条，空流会被当成目录（我们这里空流全是文件）。
                _prop(out, _K_EMPTY_FILE, encode_bits([True] * sum(empty)))
            _prop(out, _K_NAME, b"\x00" + b"".join(member.encoded + b"\x00\x00"
                                                   for member in members))
            _prop(out, _K_MTIME, b"\x01\x00" + b"".join(
                struct.pack("<Q", filetime(member.mtime_ns)) for member in members))
            out.append(_K_END)
        out.append(_K_END)
        return bytes(out)


def _prop(out, prop_id, data):
    out.append(prop_id)
    out += encode_number(len(data))
    out += data
