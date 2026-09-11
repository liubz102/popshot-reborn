#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 `hook/notice.zh.txt` 编成 HTML、混淆后生成 `hook/notice_blob.h`。

## 为什么要它

登录界面顶部那个 533×260 的公告框（`MPlay.Control.MiniBrowser`，原本指向
早已注销的 `gamepopshot.tiancity.com`）现在用来放反倒卖声明。**声明必须让
倒卖者改不掉** —— 放成一个 txt / html 文件的话，记事本打开就能删。

所以：明文原稿只留在仓库里（`hook/notice.zh.txt`，**不进任何发布包**），
本脚本把它编译成一份完整的 HTML 文档，**混淆**成字节数组写进
`hook/notice_blob.h`，由 `hook/bshook.c` 编译进 `bshook.dll`。
运行时解出来落成临时文件再让 IE 控件加载；临时文件每次启动重生成，
改它没有意义。**发布包里不存在任何一份明文。**

混淆强度按用户要求只到「记事本 / 十六进制编辑器 / strings 搜中文找不到」
这一档，不追求抗逆向 —— 真要逆的人拦不住，但那不是威胁模型。

## 管线

    hook/notice.zh.txt  --(本脚本)-->  hook/notice_blob.h  --(cl)-->  bshook.dll
                                                                          |
                                                  运行时解混淆 -> %TEMP%\\psnotice_*.htm
                                                                          |
                                     patch 0x42408A 的立即数 -> MiniBrowser::Navigate

`hook/build.bat` 每次编译前都会跑一遍本脚本（和 `gen_ports_h.py` 同一个位置），
所以「改了文案忘了重新生成」在正常流程里发生不了。生成结果**提交进仓库**，
没装 Python 的人拿到源码也能直接编译（`hook/ports.h` 同样待遇）。

## 用法

    python tools/gen_notice_h.py                      # 生成 / 更新 notice_blob.h
    python tools/gen_notice_h.py --check              # 只检查是否最新（不写文件）
    python tools/gen_notice_h.py --html out.html      # 导出 HTML，浏览器里看排版
    python tools/gen_notice_h.py --verify-dll <dll>   # ★ 验收：DLL 里搜得到明文就失败
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 明文原稿（**不进发布包**）。
SOURCE_PATH = os.path.join(ROOT, "hook", "notice.zh.txt")
#: 生成物（提交进仓库，和 hook/ports.h 同规格）。
HEADER_PATH = os.path.join(ROOT, "hook", "notice_blob.h")

#: 混淆种子的基底。改它会让整份 blob 变样（没别的作用，不是密钥管理）。
_SEED_BASE = 0x1F35A2C7
#: FNV-1a 的质数，用来把长度混进种子 —— 让种子不是一眼能认出的常量。
_FNV_PRIME = 0x01000193
_MASK32 = 0xFFFFFFFF


# --------------------------------------------------------------------------
#  原稿 -> HTML
# --------------------------------------------------------------------------

def parse_notice(text):
    """原稿文本 -> `[(kind, payload), ...]`。格式说明写在原稿文件开头。

    kind 取值：``title`` / ``warn`` / ``kv`` / ``gap`` / ``text``。
    """
    blocks = []
    for raw in text.splitlines():
        line = raw.lstrip("﻿").rstrip("\r").rstrip()
        if line.startswith(";"):
            continue
        if not line.strip():
            blocks.append(("gap", ""))
            continue
        # ★ 只 rstrip：行首的空格（半角或全角）是原稿作者有意排的缩进，
        #   `esc()` 会把它们转成 &nbsp; 保住 —— HTML 默认会把它们吞掉。
        if line.startswith("# "):
            blocks.append(("title", line[2:].rstrip()))
        elif line.startswith("! "):
            blocks.append(("warn", line[2:].rstrip()))
        elif line.startswith("@ "):
            left, _, right = line[2:].partition("|")
            blocks.append(("kv", (left.strip(), right.strip())))
        else:
            blocks.append(("text", line.rstrip()))
    # 掐掉首尾的空行，免得白白吃掉本来就不够的 260 像素
    while blocks and blocks[0][0] == "gap":
        blocks.pop(0)
    while blocks and blocks[-1][0] == "gap":
        blocks.pop()
    return blocks


def esc(s):
    """HTML 转义 + 保住行首缩进。公告是纯文本，不允许原稿里夹标签。

    行首的空格（半角 `' '` 或全角 `'\\u3000'`）转成 `&nbsp;` —— 否则 HTML
    会把它们折叠掉，原稿里排好的缩进就没了。行内的空格不动（要正常折行）。
    """
    body = s.lstrip(" 　")
    indent = s[:len(s) - len(body)]
    body = (body.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))
    return "".join("&nbsp;&nbsp;" if ch == "　" else "&nbsp;"
                   for ch in indent) + body


#: ★ 内嵌 WebBrowser 默认跑 **IE7 文档模式**（改不了 BigShot.exe 的
#: FEATURE_BROWSER_EMULATION 注册项），所以这里只用 IE7 吃得下的东西：
#: 简单 block + table，没有 flex / grid / border-radius / rgba。
_HTML_HEAD = """<html><head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<title>PopShot</title>
<style type="text/css">
body{margin:0;padding:14px 12px;background:#fbfbfb;color:#333;
 font-family:"Microsoft YaHei","微软雅黑",SimSun,sans-serif;
 font-size:14px;line-height:1.55;}
p{margin:0;}
/* ★ 标题保持 16px：再大一号，第一行在 533-32=501px 里就放不下要折行了
   （25 个全角 × 16 + "Github" ≈ 476px，已经贴边）。改文案后请重看排版。 */
.t{font-size:16px;font-weight:bold;color:#12386b;line-height:1.42;}
.w{font-size:15px;font-weight:bold;color:#c00000;}
.gap{height:12px;font-size:1px;line-height:1px;}
hr{border:0;border-top:1px solid #dcdcdc;margin:12px 0;}
table.k{border-collapse:collapse;}
/* ★★ `font-size` 这里**必须显式写**，不能靠从 body 继承（实机 2026-09-11 踩过）：
   内嵌 WebBrowser 跑的是 **quirks 模式**，而 quirks 下**表格单元格不继承
   字体大小**，会退回默认的 medium(16px)。后果不是报错，是网址和 QQ 那两行
   各自从中间断开（`popsho` / `t-reborn`）—— 只有实机看得见。 */
table.k td{padding:1px 0;vertical-align:top;font-size:13px;line-height:1.5;}
td.l{color:#666;white-space:nowrap;padding-right:12px;}
td.v{color:#12386b;font-weight:bold;word-break:break-all;}
/* 值末尾那个「（补充说明）」降成小字：它是补充信息，不该和 QQ 号、网址抢
   宽度 —— 不降的话这一行会挤到折行（实机 533px 里差 2 个字）。 */
span.sub{font-size:11px;font-weight:normal;color:#666;margin-left:5px;}
/* 链接：点了开系统浏览器（bshook 接管了 BeforeNavigate2）。
   `cursor:pointer` IE6+ 就认；IE5.5 才需要写 `hand`，这里用不着。 */
a{color:#0b53c0;text-decoration:underline;cursor:pointer;}
a:hover{color:#c00000;}
</style></head><body>
"""

_HTML_TAIL = "</body></html>\r\n"

#: `@ 左|右` 里右半边**末尾**那个整括号 —— 渲染成小字（见 span.sub 的说明）。
_TRAILING_PAREN = re.compile(r"^(.+?)\s*([（(][^（）()]*[）)])\s*$")

#: 网址 —— 渲染成真链接。
#: ★ 点了会开**系统浏览器**，靠的是 bshook 接管了 MiniBrowserEvent 的
#: `BeforeNavigate2`（0x44335f）：`http(s)://` 一律 ShellExecuteW 并取消导航。
#: **原版不会这么干**（它是「弹窗取消、框内导航」），所以这个 <a> 和那处
#: patch 是绑死的 —— 那处 patch 没打上的话，点一下会把公告页顶掉。
_URL = re.compile(r"(https?://[^\s<>\"）)]+)")


def linkify(html_fragment_text):
    """把已转义文本里的网址换成 <a>。输入必须**已经转义过**。"""
    return _URL.sub(r'<a href="\1">\1</a>', html_fragment_text)


def render_value(text):
    """kv 行的右半边：末尾的「（补充说明）」降成小字，网址变成可点链接。"""
    m = _TRAILING_PAREN.match(text)
    if m:
        return (linkify(esc(m.group(1)))
                + '<span class="sub">%s</span>' % esc(m.group(2)))
    return linkify(esc(text))


def render_html(blocks):
    """`parse_notice` 的结果 -> 一份完整的 HTML 文档（str）。

    连续的 ``kv`` 行合并成一张两列表格，这样左列标签能对齐 —— 原稿里那些
    对齐用的空格只是给人看的示意，排版由这张表负责。
    """
    out = [_HTML_HEAD]
    i = 0
    seen_kv = False
    while i < len(blocks):
        kind, payload = blocks[i]
        if kind == "kv":
            if not seen_kv:
                out.append("<hr>\n")
                seen_kv = True
            out.append('<table class="k">\n')
            while i < len(blocks) and blocks[i][0] == "kv":
                left, right = blocks[i][1]
                out.append('<tr><td class="l">%s</td><td class="v">%s</td></tr>\n'
                           % (esc(left), render_value(right)))
                i += 1
            out.append("</table>\n")
            continue
        if kind == "gap":
            out.append('<div class="gap"></div>\n')
        elif kind == "title":
            out.append('<p class="t">%s</p>\n' % esc(payload))
        elif kind == "warn":
            out.append('<p class="w">%s</p>\n' % esc(payload))
        else:
            out.append("<p>%s</p>\n" % esc(payload))
        i += 1
    out.append(_HTML_TAIL)
    return "".join(out)


# --------------------------------------------------------------------------
#  混淆
# --------------------------------------------------------------------------

def _seed_for(length):
    """种子由长度算出来，不是写死的常量（改一个字整份 blob 就换样）。"""
    seed = (_SEED_BASE ^ ((length * _FNV_PRIME) & _MASK32)) & _MASK32
    return seed or 0x2545F491          # xorshift 的状态不能是 0


def _iv_for(seed):
    """IV 也从种子派生。刻意避开 0x00 / 0xFF / 可打印区间。"""
    iv = ((seed >> 24) ^ (seed >> 8) ^ 0x6D) & 0xFF
    if iv < 0x80 or iv == 0xFF:
        iv = (iv | 0x80) & 0xFE
    return iv


def _keystream(seed):
    """xorshift32，每步取低字节。"""
    x = seed & _MASK32
    while True:
        x ^= (x << 13) & _MASK32
        x ^= x >> 17
        x ^= (x << 5) & _MASK32
        x &= _MASK32
        yield x & 0xFF


def obfuscate(data):
    """明文字节 -> `(seed, iv, 密文字节)`。

    `c[i] = p[i] ^ ks[i] ^ c[i-1]`（c[-1] = IV）。密钥随位置变、又和前一个
    **密文**字节链起来 ⇒ 同一个汉字在密文里不会重复出现相同字节，
    strings / 十六进制编辑器里看不出任何结构。解密在 `bshook.c` 的
    `notice_decode()`，两边必须同算法（种子和 IV 由本脚本写进头文件，
    是唯一真源，不会漂）。
    """
    seed = _seed_for(len(data))
    iv = _iv_for(seed)
    ks = _keystream(seed)
    out = bytearray()
    prev = iv
    for p in data:
        c = p ^ next(ks) ^ prev
        out.append(c)
        prev = c
    return seed, iv, bytes(out)


def deobfuscate(seed, iv, blob):
    """`obfuscate` 的逆（自检用 —— 生成时当场解一遍对比，不让错的 blob 出门）。"""
    ks = _keystream(seed)
    out = bytearray()
    prev = iv
    for c in blob:
        out.append(c ^ next(ks) ^ prev)
        prev = c
    return bytes(out)


# --------------------------------------------------------------------------
#  头文件
# --------------------------------------------------------------------------

def render_header(seed, iv, blob):
    """`hook/notice_blob.h` 的完整内容（行尾统一 LF，写入方决定落盘形态）。"""
    lines = [
        "/* ========================================================================",
        " *  notice_blob.h —— 登录界面公告框的文案（已混淆）。",
        " *",
        " *  ★★ 【自动生成，不要手改】",
        " *      原稿是 hook/notice.zh.txt，生成器是 tools/gen_notice_h.py。",
        " *      要改文案只改原稿一处，重新编译即可（build.bat 会自己重新生成）。",
        " *",
        " *  下面这坨字节是一份完整 HTML 文档的密文。解密在 bshook.c 的",
        " *  notice_decode()，算法和种子必须和生成器一致 —— 种子/IV 就写在",
        " *  这里，是唯一真源，两边不会漂。",
        " *",
        " *  ⚠ 本文件和 hook/notice.zh.txt 都**不进发布包**；发布包里的",
        " *    bshook.dll 内也搜不到任何明文（tools/gen_notice_h.py --verify-dll 验收）。",
        " * ====================================================================== */",
        "#ifndef POPSHOT_NOTICE_BLOB_H",
        "#define POPSHOT_NOTICE_BLOB_H",
        "",
        "#define NOTICE_SEED     0x%08Xu" % seed,
        "#define NOTICE_IV       0x%02Xu" % iv,
        "#define NOTICE_BLOB_LEN %d" % len(blob),
        "",
        "static const unsigned char NOTICE_BLOB[NOTICE_BLOB_LEN] = {",
    ]
    for i in range(0, len(blob), 12):
        chunk = blob[i:i + 12]
        lines.append("    " + ", ".join("0x%02x" % b for b in chunk) + ",")
    lines += [
        "};",
        "",
        "#endif /* POPSHOT_NOTICE_BLOB_H */",
        "",
    ]
    return "\n".join(lines)


def write_if_changed(path, want, banner):
    """内容有变才落盘（CRLF：Windows C 工程，仓库里的 .c/.h 都是这样）。"""
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            have = f.read().replace("\r\n", "\n")
    except OSError:
        have = None
    if have == want:
        print(f"[notice] {path} 无变化")
        return True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(want)
    print(f"[notice] {banner} {path}（{len(want)} 字符）")
    return True


# --------------------------------------------------------------------------
#  验收：编出来的 DLL 里搜不搜得到明文
# --------------------------------------------------------------------------

#: 一定要在 DLL 里搜不到的东西：正文里每一段连续汉字（≥2 字）和长数字串（QQ 号）。
_CJK_RUN = re.compile(r"[㐀-鿿　-〿＀-￯]{2,}")
_DIGIT_RUN = re.compile(r"\b\d{6,}\b")

#: ★ **网址不算**。判据是「倒卖者能不能定位并改掉这段公告」，不是「DLL 里
#: 有没有出现过这几个字符」。GitHub 地址本来就以别的身份明文躺在 DLL 里
#: （`bshook.c:1066`，更新器拉不起来时那个提示框），改掉那一处也动不了公告
#: 一个字。把它算进来只会让验收永远红，红久了就没人看了。
#: 中文正文和 QQ 号才是真正的判据 —— 它们只在公告里出现。


def secret_words(blocks):
    """从原稿里抠出「绝不能出现在 DLL 里」的词。"""
    words = set()
    for kind, payload in blocks:
        parts = list(payload) if kind == "kv" else [payload]
        for part in parts:
            for m in _CJK_RUN.findall(part):
                token = m.strip()
                if len(token) >= 2:
                    words.add(token)
            for m in _DIGIT_RUN.findall(part):
                words.add(m)
    return sorted(words, key=len, reverse=True)


def verify_dll(path, blocks, blob):
    """在 DLL 里双编码搜明文。返回退出码（0 = 干净）。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as error:
        print(f"[notice] !! 读不了 {path}（{error}）", file=sys.stderr)
        return 1

    hits = []
    for word in secret_words(blocks):
        for enc in ("utf-8", "utf-16-le", "gbk"):
            try:
                needle = word.encode(enc)
            except UnicodeEncodeError:
                continue
            if needle in data:
                hits.append((word, enc))

    # ★ 两条都报完再返回，不要短路 —— 「明文泄了」和「blob 不在位」是两件事，
    #   只报一件会让人修完一个再撞另一个。
    bad = 0
    if hits:
        print(f"[notice] !! {path} 里搜到了 {len(hits)} 处明文，混淆没生效：",
              file=sys.stderr)
        for word, enc in hits:
            print(f"          {enc:10s} {word}", file=sys.stderr)
        bad = 1
    if blob not in data:
        print(f"[notice] !! {path} 里找不到混淆后的 blob —— "
              f"这份 DLL 不是用当前的 notice_blob.h 编的，先跑 hook\\build.bat",
              file=sys.stderr)
        bad = 1
    if bad:
        return 1

    print(f"[notice] OK {path}：blob 在位，"
          f"{len(secret_words(blocks))} 个特征词 x 3 种编码全部 0 命中")
    return 0


# --------------------------------------------------------------------------

def build():
    """读原稿 -> `(blocks, header_text, blob)`，顺带自检一次解密。"""
    with open(SOURCE_PATH, "r", encoding="utf-8-sig", newline="") as f:
        source = f.read()
    blocks = parse_notice(source)
    if not blocks:
        raise SystemExit(f"[notice] !! {SOURCE_PATH} 里没有任何可用的文案行")
    html = render_html(blocks)
    data = html.encode("utf-8")
    seed, iv, blob = obfuscate(data)
    # 自检：当场解回来对一遍，错的 blob 一个字节都不许出门。
    if deobfuscate(seed, iv, blob) != data:
        raise SystemExit("[notice] !! 混淆自检失败（解回来和原文不一致）")
    return blocks, html, render_header(seed, iv, blob), blob


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # ★ 控制台编码不能让这个脚本挂掉。`hook/build.bat` 拿**退出码**当闸门：
    #   只要打印时抛一次 UnicodeEncodeError，退出码就是 1，构建会被误判成
    #   「DLL 里有明文」。实测：`chcp 65001` 之外（比如直接在 GBK 控制台里跑）
    #   打一个 U+2713 就当场炸。所以这里把编码不了的字符降级成替代符，
    #   **永远不让输出本身改变退出码**。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    blocks, html, header, blob = build()

    if "--html" in argv:
        out = argv[argv.index("--html") + 1]
        with open(out, "w", encoding="utf-8", newline="") as f:
            f.write(html)
        print(f"[notice] 已导出 {out}（浏览器里打开看排版；"
              f"注意真环境是 IE7 文档模式、533x260）")
        return 0

    if "--verify-dll" in argv:
        return verify_dll(argv[argv.index("--verify-dll") + 1], blocks, blob)

    if "--check" in argv:
        try:
            with open(HEADER_PATH, "r", encoding="utf-8", newline="") as f:
                have = f.read().replace("\r\n", "\n")
        except OSError:
            have = None
        if have != header:
            print(f"[notice] !! {HEADER_PATH} 和 {SOURCE_PATH} 对不上，"
                  f"请跑一次 python tools/gen_notice_h.py", file=sys.stderr)
            return 1
        print("[notice] notice_blob.h 是最新的")
        return 0

    write_if_changed(HEADER_PATH, header, "已更新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
