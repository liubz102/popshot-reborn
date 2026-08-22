"""一次性提取原版更新器 UI 素材：game_patched\\NGMResource.dll -> updater\\ui\\orig\\

为什么有这个脚本：自研更新器（updater\\src\\）要复用原版 BsPatcherChn /
NGMDll 的更新界面。那套界面是 HTML 模板 + 图片，全部以 RCDATA 资源的形式
存在 NGMResource.dll 里（纯资源 DLL，2007 年原版文件，包里一直没删）。
本脚本把它们原样 dump 出来，作为工程资源进 git（用户拍板：素材以副本
形式存进我们自己的工程，运行时不依赖这个 DLL）。

用法（在仓库根目录）：
    python updater\\scripts\\extract_updater_ui.py

输出：
    updater\\ui\\orig\\            原始字节，永不改动（重提取 / 比对基准）
    updater\\ui\\extract-report.txt  资源目录清单 + HTML 模板的解码预览

注意：
    * NGMResource.dll 是 32 位 DLL，开发机 python 是 64 位也没关系 ——
      用 LOAD_LIBRARY_AS_DATAFILE 只当数据文件读，不执行代码。
    * HTML 模板是 gb2312 编码，保持字节原样 dump；预览只是给人看的。
    * 资源名/类型要么是整数 id（< 0x10000，MAKEINTRESOURCE 语义），
      要么是宽字符串。枚举回调按 c_void_p 收，回传 FindResourceW 时
      不设 argtypes、原样传回（int/str 都合法），最省事也最不容易错。
"""

import ctypes
import os
import struct
import sys
import time
from ctypes import wintypes

k32 = ctypes.windll.kernel32

LOAD_LIBRARY_AS_DATAFILE = 0x00000002
LOAD_LIBRARY_AS_IMAGE_RESOURCE = 0x00000020

# 标准资源类型 id -> 名字（只用于目录清单的显示）。
RT_NAMES = {
    1: "CURSOR", 2: "BITMAP", 3: "ICON", 4: "MENU", 5: "DIALOG",
    6: "STRING", 7: "FONTDIR", 8: "FONT", 9: "ACCELERATOR",
    10: "RCDATA", 11: "MESSAGETABLE", 12: "GROUP_CURSOR", 14: "GROUP_ICON",
    16: "VERSION", 23: "HTML", 24: "MANIFEST",
}

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DLL_PATH = os.path.join(ROOT, "game_patched", "NGMResource.dll")
OUT_DIR = os.path.join(ROOT, "updater", "ui", "orig")
REPORT = os.path.join(ROOT, "updater", "ui", "extract-report.txt")

TEXT_EXTS = (".html", ".htm", ".txt", ".js", ".css")


def res_id_or_str(raw):
    """枚举回调里的类型/名字参数：值 < 0x10000 是整数 id，否则是字符串指针。"""
    if not raw:
        return None
    v = raw if isinstance(raw, int) else ctypes.cast(raw, ctypes.c_void_p).value
    if not v:
        return None
    if v < 0x10000:
        return v
    return ctypes.wstring_at(v)


def safe_name(part):
    return str(part).replace("/", "_").replace("\\", "_").replace(":", "_")


def sniff_text(data):
    """HTML/文本资源给个粗略判断：没有大量 NUL 且看起来像标签/文字。"""
    if not data:
        return False
    if data.count(0) > len(data) // 16:
        return False
    head = data[:512].decode("gbk", errors="ignore").lower()
    return ("<html" in head or "<body" in head or "<td" in head
            or "<script" in head or "<meta" in head)


def main():
    if not os.path.isfile(DLL_PATH):
        print("[extract] missing:", DLL_PATH)
        return 1

    # 只当数据加载（不执行 DllMain，也绕开 32/64 位不匹配）。
    hmod = k32.LoadLibraryExW(DLL_PATH, None,
                              LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE)
    if not hmod:
        print("[extract] LoadLibraryExW failed err=%u" % k32.GetLastError())
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    entries = []  # (type_disp, name_disp, lang, size, filename, data)

    def load_resource(rtype, rname):
        hrsrc = k32.FindResourceW(hmod, rname, rtype)
        if not hrsrc:
            return None, 0
        hglob = k32.LoadResource(hmod, hrsrc)
        if not hglob:
            return None, 0
        size = k32.SizeofResource(hmod, hrsrc)
        ptr = k32.LockResource(hglob)
        if not ptr or not size:
            return None, 0
        return ctypes.string_at(ptr, size), size

    EnumResTypeProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p)
    EnumResNameProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p)
    EnumResLangProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.WORD, ctypes.c_void_p)

    def on_language(_h, rtype, rname, lang, _lp):
        t = held_type[0]
        n = held_name[0]
        data, size = load_resource(t, n)
        if data is None:
            return True
        t_disp = RT_NAMES.get(t, str(t)) if isinstance(t, int) else str(t)
        if str(n).lower().endswith(TEXT_EXTS):
            fname = safe_name(n)               # 模板/文本直接用资源名当文件名
        else:
            fname = "%s__%s__lang%d.bin" % (safe_name(t_disp), safe_name(n), lang)
        out = os.path.join(OUT_DIR, fname)
        if not os.path.exists(out):            # 同名多语言只留第一份
            with open(out, "wb") as f:
                f.write(data)
        entries.append((t_disp, n, lang, size, fname, data))
        return True

    lang_cb = EnumResLangProc(on_language)

    def on_name(_h, rtype, rname, _lp):
        t = res_id_or_str(rtype)
        n = res_id_or_str(rname)
        if t is None or n is None:
            return True
        held_type[0] = t
        held_name[0] = n
        k32.EnumResourceLanguagesW(hmod, t, n, lang_cb, 0)
        return True

    name_cb = EnumResNameProc(on_name)
    held_type = [None]
    held_name = [None]

    def on_type(_h, rtype, _lp):
        t = res_id_or_str(rtype)
        if t is None:
            return True
        k32.EnumResourceNamesW(hmod, t, name_cb, 0)
        return True

    type_cb = EnumResTypeProc(on_type)
    if not k32.EnumResourceTypesW(hmod, type_cb, 0):
        print("[extract] EnumResourceTypesW failed err=%u" % k32.GetLastError())
        return 1

    # —— 目录清单 ------------------------------------------------------------
    entries.sort(key=lambda e: (str(e[0]), str(e[1]), e[2]))
    lines = []
    lines.append("NGMResource.dll resource catalog")
    lines.append("source : %s" % DLL_PATH)
    lines.append("dumped : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("output : %s" % OUT_DIR)
    lines.append("")
    for t_disp, n, lang, size, fname, _ in entries:
        lines.append("%-8s %-40s lang=%-5d %9d B  -> %s"
                     % (t_disp, n, lang, size, fname))
    lines.append("")
    lines.append("total %d resources" % len(entries))

    # —— HTML 模板解码预览（只给人看，orig\\ 里仍是原始字节） ------------------
    for t_disp, n, lang, size, fname, data in entries:
        if not str(n).lower().endswith(TEXT_EXTS) and not sniff_text(data):
            continue
        text = data.decode("gbk", errors="replace")
        lines.append("")
        lines.append("=" * 78)
        lines.append("decoded preview: %s  (gbk, %d bytes)" % (fname, size))
        lines.append("=" * 78)
        lines.append(text)

    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    print("[extract] %d resources -> %s" % (len(entries), OUT_DIR))
    print("[extract] report -> %s" % REPORT)
    k32.FreeLibrary(hmod)

    # —— 顺手把 GROUP_ICON + RT_ICON 组装成可以直接用的 .ico ------------------
    # RT_GROUP_ICON 是目录（GRPICONDIRENTRY 数组，成员以 RT_ICON 的数字 id
    # 引用位图数据）；RT_ICON 的裸数据不带 .ico 文件头。拼一个标准 .ico 出来，
    # 我们的 exe 图标 / 窗口图标就能直接用原版的。
    icons = {}
    for t_disp, n, _lang, _size, fname, data in entries:
        if t_disp == "ICON" and isinstance(n, int):
            icons[n] = data
    for t_disp, n, _lang, _size, fname, data in entries:
        if t_disp != "GROUP_ICON" or not isinstance(n, int):
            continue
        if data[:4] != b"\x00\x00\x01\x00" or n not in (106, 109):
            continue
        count = struct.unpack_from("<H", data, 4)[0]
        blobs, dirents = [], []
        for i in range(count):
            w, h, _c, _r, planes, bpp, csize, rid = \
                struct.unpack_from("<BBBBHHIH", data, 6 + i * 14)
            blob = icons.get(rid)
            if blob is None or len(blob) != csize:
                break
            offset = 6 + count * 16 + sum(len(b) for b in blobs)
            dirents.append(struct.pack("<BBBBHHII", w, h, _c, _r,
                                       planes, bpp, csize, offset))
            blobs.append(blob)
        if len(blobs) != count:
            print("[extract] group %d 引用的 RT_ICON 凑不齐，跳过" % n)
            continue
        out = os.path.join(OUT_DIR, "ICON_%d.ico" % n)
        with open(out, "wb") as f:
            f.write(struct.pack("<HHH", 0, 1, count))
            for d in dirents:
                f.write(d)
            for b in blobs:
                f.write(b)
        print("[extract] assembled ICON_%d.ico (%d 张)" % (n, count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
