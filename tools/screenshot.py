#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
screenshot.py —— 抓某个进程主窗口（或整屏）的截图，存 PNG

用法：
    python tools/screenshot.py <pid> <输出png>       # 抓该进程的可见顶层窗口
    python tools/screenshot.py screen <输出png>      # 抓整个屏幕
    python tools/screenshot.py dlg <pid> <输出png>   # 抓 #32770 对话框（登录框）

实现：BitBlt 屏幕 DC 到内存位图，再手工写最小 PNG（zlib + struct，无外部依赖）。
不用 PrintWindow —— D3D9 独占绘制的窗口 PrintWindow 会抓到全黑。

★ `dlg` 模式走的是 **`GetWindowDC(那个窗口)`**，不碰屏幕（§174）：
  串流 / 远程桌面会话里桌面根本不渲染，屏幕 DC 抓出来是**全黑**的
  （V0.2 会话 12 就是这么卡住的），而 DWM 给每个窗口留着自己的重定向表面，
  普通 GDI 对话框从那里照样读得到真实像素。
  也**不会触发重绘** —— 所以「被兄弟控件擦掉的字」在这里也是缺的，
  正好用来验登录框的布局问题（§173）。
  ⚠ 只对 GDI 窗口有效；游戏主窗口那种 D3D9 独占绘制的照样是黑的。
"""
import ctypes as C
import struct
import sys
import time
import zlib
from ctypes import wintypes as W

u32 = C.WinDLL("user32", use_last_error=True)
gdi = C.WinDLL("gdi32", use_last_error=True)

EnumWindowsProc = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
u32.EnumWindows.argtypes = [EnumWindowsProc, W.LPARAM]
u32.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
u32.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
u32.IsWindowVisible.argtypes = [W.HWND]
u32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]


def find_windows(pid):
    out = []

    def cb(h, _):
        p = W.DWORD()
        u32.GetWindowThreadProcessId(h, C.byref(p))
        if p.value == pid:
            cn = C.create_unicode_buffer(256)
            u32.GetClassNameW(h, cn, 256)
            visible = bool(u32.IsWindowVisible(h))
            # BigShot keeps its real D3D window hidden during parts of startup.
            # Keep it as a candidate so a stale SplashWindow is not captured.
            if not visible and cn.value not in ("MoleWnd", "D3DProxyWindow"):
                return True
            r = W.RECT()
            u32.GetWindowRect(h, C.byref(r))
            w, ht = r.right - r.left, r.bottom - r.top
            if u32.IsIconic(h) or not visible:
                u32.ShowWindow(h, 9)   # SW_RESTORE
                time.sleep(0.6)
                u32.GetWindowRect(h, C.byref(r))
                w, ht = r.right - r.left, r.bottom - r.top
            if w > 100 and ht > 100:
                buf = C.create_unicode_buffer(256)
                u32.GetWindowTextW(h, buf, 256)
                out.append((h, r.left, r.top, w, ht, buf.value, cn.value))
        return True

    u32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def bring_to_front(hwnd):
    """把别的进程的窗口拉到前台。

    单纯 SetForegroundWindow 在调用方不是前台进程时会被系统拒掉（只闪任务栏），
    所以叠加 SwitchToThisWindow（未文档化但一直有效）+ SetWindowPos 置顶再取消置顶。
    """
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    try:
        u32.SwitchToThisWindow(W.HWND(hwnd), True)
    except Exception:
        pass
    u32.SetWindowPos(W.HWND(hwnd), W.HWND(HWND_TOPMOST), 0, 0, 0, 0, flags)
    u32.BringWindowToTop(W.HWND(hwnd))
    u32.SetForegroundWindow(W.HWND(hwnd))
    time.sleep(0.7)
    u32.SetWindowPos(W.HWND(hwnd), W.HWND(HWND_NOTOPMOST), 0, 0, 0, 0, flags)
    time.sleep(0.3)


def grab(x, y, w, h):
    """BitBlt 屏幕区域，返回 (w, h, BGRA bytes)"""
    SRCCOPY = 0x00CC0020
    scr = u32.GetDC(0)
    mem = gdi.CreateCompatibleDC(scr)
    bmp = gdi.CreateCompatibleBitmap(scr, w, h)
    gdi.SelectObject(mem, bmp)
    gdi.BitBlt(mem, 0, 0, w, h, scr, x, y, SRCCOPY)

    class BITMAPINFOHEADER(C.Structure):
        _fields_ = [("biSize", W.DWORD), ("biWidth", C.c_long), ("biHeight", C.c_long),
                    ("biPlanes", W.WORD), ("biBitCount", W.WORD),
                    ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                    ("biXPelsPerMeter", C.c_long), ("biYPelsPerMeter", C.c_long),
                    ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]

    bi = BITMAPINFOHEADER()
    bi.biSize = C.sizeof(bi)
    bi.biWidth = w
    bi.biHeight = -h          # 负数 = 自上而下
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = C.create_string_buffer(w * h * 4)
    gdi.GetDIBits(mem, bmp, 0, h, buf, C.byref(bi), 0)
    gdi.DeleteObject(bmp)
    gdi.DeleteDC(mem)
    u32.ReleaseDC(0, scr)
    return bytes(buf)


def grab_window(hwnd):
    """读某个窗口自己的 DC（不经过屏幕），返回 ``(w, h, BGRA bytes)``。

    见文件开头 `dlg` 模式那段说明：串流会话下屏幕 DC 全黑，这条路照样有内容。
    """
    SRCCOPY = 0x00CC0020
    r = W.RECT()
    u32.GetWindowRect(W.HWND(hwnd), C.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    src = u32.GetWindowDC(W.HWND(hwnd))
    mem = gdi.CreateCompatibleDC(src)
    bmp = gdi.CreateCompatibleBitmap(src, w, h)
    gdi.SelectObject(mem, bmp)
    gdi.BitBlt(mem, 0, 0, w, h, src, 0, 0, SRCCOPY)

    class BITMAPINFOHEADER(C.Structure):
        _fields_ = [("biSize", W.DWORD), ("biWidth", C.c_long), ("biHeight", C.c_long),
                    ("biPlanes", W.WORD), ("biBitCount", W.WORD),
                    ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                    ("biXPelsPerMeter", C.c_long), ("biYPelsPerMeter", C.c_long),
                    ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]

    bi = BITMAPINFOHEADER()
    bi.biSize = C.sizeof(bi)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = C.create_string_buffer(w * h * 4)
    gdi.GetDIBits(mem, bmp, 0, h, buf, C.byref(bi), 0)
    gdi.DeleteObject(bmp)
    gdi.DeleteDC(mem)
    u32.ReleaseDC(W.HWND(hwnd), src)
    return w, h, bytes(buf)


def find_dialogs(pid, title="PopShot"):
    """该进程里可见的 `#32770` 对话框。标题传 None 就不挑标题。"""
    out = []

    def cb(h, _):
        p = W.DWORD()
        u32.GetWindowThreadProcessId(h, C.byref(p))
        if p.value != pid or not u32.IsWindowVisible(h):
            return True
        cn = C.create_unicode_buffer(64)
        u32.GetClassNameW(h, cn, 64)
        tb = C.create_unicode_buffer(256)
        u32.GetWindowTextW(h, tb, 256)
        if cn.value == "#32770" and (title is None or tb.value == title):
            out.append((h, tb.value))
        return True

    u32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def write_png(path, w, h, bgra):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * w * 4:(y + 1) * w * 4]
        raw += bytes(b for i in range(0, len(row), 4)
                     for b in (row[i + 2], row[i + 1], row[i]))
    comp = zlib.compress(bytes(raw), 6)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", comp) + chunk(b"IEND", b""))
    open(path, "wb").write(png)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    target, out = sys.argv[1], sys.argv[2]
    if target == "dlg":
        if len(sys.argv) < 4:
            print(__doc__)
            return
        pid, out = int(sys.argv[2]), sys.argv[3]
        found = find_dialogs(pid) or find_dialogs(pid, None)
        if not found:
            print(f"pid {pid} 没有可见的 #32770 对话框")
            return
        hwnd, title = found[0]
        w, h, bgra = grab_window(hwnd)
        write_png(out, w, h, bgra)
        print(f"已抓对话框 hwnd={hwnd:#x} {title!r} {w}x{h} -> {out}")
        return
    if target == "screen":
        w = u32.GetSystemMetrics(0)
        h = u32.GetSystemMetrics(1)
        write_png(out, w, h, grab(0, 0, w, h))
        print(f"整屏 {w}x{h} -> {out}")
        return
    pid = int(target)
    wins = find_windows(pid)
    if not wins:
        print(f"pid {pid} 没有可见窗口")
        return
    for hwnd, x, y, w, h, title, cn in wins:
        print(f"窗口 hwnd={hwnd:#x} {x},{y} {w}x{h} class=[{cn}] 标题={title!r}")
    # D3DProxyWindow 是 d3d9 自己建的代理窗，画面在游戏本体的窗口上，优先选后者
    real = [t for t in wins if t[6] != "D3DProxyWindow"] or wins
    hwnd, x, y, w, h, title, cn = max(
        real,
        key=lambda t: (t[6] == "MoleWnd", t[3] * t[4]),
    )
    bring_to_front(hwnd)
    write_png(out, w, h, grab(x, y, w, h))
    print(f"已抓 [{cn}] {w}x{h} -> {out}")


if __name__ == "__main__":
    main()
