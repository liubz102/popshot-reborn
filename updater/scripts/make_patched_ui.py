"""从 ui\\orig\\ 原始素材生成工程使用的界面副本（updater\\ui\\ 下）。

原版模板（gb2312）在 NGMDll 里由 res:// 加载，按钮点击靠 NGMDll 的 COM 事件
下沉接收；我们不走 res://（2026-08-22 上一版真机翻车点），改为解到临时目录
用 file:// 加载 + 打过补丁的页面直接回调 window.external（IDispatch 由
ui_external.c 提供）。所以对模板做**最小**的、纯 ASCII 的注入式修改，
其余字节一律保持原样 —— 每处差异都在这里集中列出（也写进 ui\\README.md）。

用法：
    python updater\\scripts\\make_patched_ui.py

输出（updater\\ui\\）：
    TEMPLATE_PATCH.HTML / TEMPLATE_CONFIRMRUNADMIN.HTML / TEMPLATE_MESSAGEL.HTML
        打过补丁的模板（gb2312 字节，除注入点外与 orig\\ 逐字节一致）
    global.css                  原样拷贝（模板 <link> 引用的是小写文件名）
    BINARY\\*.GIF|*.JPG          原样拷贝（模板按 BINARY/xxx.gif 相对路径引用）
    ICON_109.ico                原版三尺寸图标（exe + 窗口图标用）
"""

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))       # updater\scripts
UI = os.path.join(os.path.dirname(HERE), "ui")          # updater\ui
ORIG = os.path.join(UI, "orig")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(UI)))   # 仓库根

SRC_DLL_DIR = os.path.join(ROOT, "game_patched")

# ---------------------------------------------------------------------------
#  对模板的修改清单。全部是「ASCII 串 -> ASCII 串」的字节级替换，避开任何
#  编码问题（模板正文是 gb2312，含中文；注入点都挑在纯 ASCII 的属性区）。
#  (needle, replacement, 期望出现次数)
# ---------------------------------------------------------------------------

ONCLICK = 'onclick="window.external.OnButton(\'{id}\');return false;"'

# 三张模板的公共处理：body 拖动 + 按钮回调 + IE11 行盒修正。
#   * 拖动：原版窗口由 NGMDll 的原生框架拖动；我们无标题栏，给 <body>
#     注入 onmousedown -> external.DragMove()。★ IE 传统事件模型里左键是
#     event.button==1（W3C 是 0）—— 两个都认。排掉交互元素（iframe 覆盖
#     公告区不冒泡、A/按钮图片会先吃掉点击），点标题、logo、边缘装饰图、
#     状态文字空白处都能拖。
#   * 按钮：原版页面没有任何 onclick（NGMDll 用 COM 事件下沉拿点击），
#     这里给每个 <img> 按钮注入 OnButton(id) 回调。
#   * PATCH 的公告 iframe：原版 src="" 等 NGMDll 运行时 ChangeContents()，
#     显式给 about:blank 防个别 IE 版本把空 src 解析成页面自身。
#   * ★ 行盒修正（真机踩坑 2026-08-22）：模板的装饰图（round*.gif 等）是
#     行内元素，IE11 的行盒比 IE6 高，每张小图多占几像素 -> 总高溢出
#     473px 客户区、底部圆角被裁（SCROLL_NO 挡住滚动直接切）。IE6 没有
#     这个空隙。img/iframe 改 display:block 精确还原 2007 年的高度。
#   * ★ 状态小字修正（真机踩坑 2026-08-22）：原版给底部状态行
#     （#BtmSec p.CurrentTxt）定的盒子 height:11px + overflow:hidden，
#     字号却是 12px —— IE11 行高约 14px，文字最底下几像素被裁。放宽到
#     14px 并上移 2px 补偿（容器 BtmSec 高 18px，top2+14=16 仍在内）。
#   * ★ 公告 iframe 加高（真机踩坑 2026-08-22，用户拍板）：原版 300 高装
#     下整张 546x300 海报就没地方放文字 -> 出滚动条。用户拍板**不缩图**，
#     iframe 加高到 332（海报 300 + 一行文字 ~28），窗体高度由 ui_window.c
#     的 scrollHeight 自适应跟着长。
DRAG = ('onmousedown="if((event.button==0||event.button==1)'
        '&&event.srcElement.tagName!=\'IFRAME\''
        '&&event.srcElement.tagName!=\'A\''
        '&&event.srcElement.id.substr(0,3)!=\'btn\')'
        'window.external.DragMove()"')

LINEBOX_FIX = ('<style>img{display:block}iframe{display:block}'
               '#BtmSec p.CurrentTxt{height:14px !important;'
               'top:2px !important}</style>')

PATCHES = {
    "TEMPLATE_PATCH.HTML": [
        ('ondrag="return false;">',
         'ondrag="return false;" %s>' % DRAG, 1),
        ('href="global.css">',
         'href="global.css">%s' % LINEBOX_FIX, 1),
        ('<img id="btnClose"', '<img id="btnClose" %s' % ONCLICK.format(id="btnClose"), 1),
        ('<img id="btnCancel"', '<img id="btnCancel" %s' % ONCLICK.format(id="btnCancel"), 1),
        ('<img id="btnConfirm"', '<img id="btnConfirm" %s' % ONCLICK.format(id="btnConfirm"), 1),
        # 公告 iframe：300 -> 332（不缩图，给文字留高度；见上面的拍板注释）。
        ('width="546" height="300"', 'width="546" height="332"', 1),
        ('src=""', 'src="about:blank"', 1),
    ],
    "TEMPLATE_CONFIRMRUNADMIN.HTML": [
        ('ondrag="return false;">',
         'ondrag="return false;" %s>' % DRAG, 1),
        ('href="global.css">',
         'href="global.css">%s' % LINEBOX_FIX, 1),
        ('<img id="btnClose"', '<img id="btnClose" %s' % ONCLICK.format(id="btnClose"), 1),
        ('<img id="btnContinue"', '<img id="btnContinue" %s' % ONCLICK.format(id="btnContinue"), 1),
        ('<img id="btnStop"', '<img id="btnStop" %s' % ONCLICK.format(id="btnStop"), 1),
    ],
    "TEMPLATE_MESSAGEL.HTML": [
        ('ondrag="return false;">',
         'ondrag="return false;" %s>' % DRAG, 1),
        ('href="global.css">',
         'href="global.css">%s' % LINEBOX_FIX, 1),
        ('<img id="btnClose"', '<img id="btnClose" %s' % ONCLICK.format(id="btnClose"), 1),
        ('<img id="btnConfirm"', '<img id="btnConfirm" %s' % ONCLICK.format(id="btnConfirm"), 1),
        ('<img id="btnCancel"', '<img id="btnCancel" %s' % ONCLICK.format(id="btnCancel"), 1),
    ],
}

#: 需要 BINARY\\ 拷贝的图片（= 全部 BINARY 资源；名字保持 DLL 里的原大写）。
def binary_names():
    names = []
    for fn in os.listdir(ORIG):
        if fn.startswith("BINARY__") and fn.endswith(".bin"):
            names.append(fn.split("__", 1)[1].rsplit("__lang", 1)[0])
    return sorted(names)


def patch_bytes(data, patches, title):
    for needle, repl, want in patches:
        count = data.count(needle.encode("ascii"))
        if count != want:
            print("[patch] !! %s 里 %r 出现 %d 次（期望 %d）—— orig 变了？"
                  % (title, needle, count, want))
            return None
        data = data.replace(needle.encode("ascii"), repl.encode("ascii"))
    return data


def main():
    ok = True

    for name, patches in PATCHES.items():
        src = os.path.join(ORIG, name)
        dst = os.path.join(UI, name)
        with open(src, "rb") as f:
            data = f.read()
        patched = patch_bytes(data, patches, name)
        if patched is None:
            ok = False
            continue
        with open(dst, "wb") as f:
            f.write(patched)
        print("[patch] %s（%d -> %d 字节）" % (name, len(data), len(patched)))

    # global.css：模板 <link href="global.css">，拷成同名（小写）。
    shutil.copyfile(os.path.join(ORIG, "GLOBAL.CSS"),
                    os.path.join(UI, "global.css"))
    print("[patch] global.css")

    # 图片：updater\\ui\\BINARY\\<原资源名>
    bindir = os.path.join(UI, "BINARY")
    os.makedirs(bindir, exist_ok=True)
    for name in binary_names():
        src = os.path.join(ORIG, "BINARY__%s__lang1033.bin" % name)
        shutil.copyfile(src, os.path.join(bindir, name))
    print("[patch] BINARY\\ %d 张图片" % len(binary_names()))

    # 图标：三尺寸的 109 号给 exe / 窗口。
    shutil.copyfile(os.path.join(ORIG, "ICON_109.ico"), os.path.join(UI, "ICON_109.ico"))
    print("[patch] ICON_109.ico")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
