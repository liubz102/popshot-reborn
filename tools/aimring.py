#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aimring.py —— 给准星外圈补画「任意格数」的弹格盘（X_Mod · X4）。

    C:\\Python314\\python.exe tools/aimring.py              # 生成资源 + 头文件（幂等）
    C:\\Python314\\python.exe tools/aimring.py --dry-run    # 只说要做什么，不落盘
    C:\\Python314\\python.exe tools/aimring.py --header-only # 只重新生成 hook/aimring.h（编译时用，快）
    C:\\Python314\\python.exe tools/aimring.py --check       # 只检查产物是否最新（不写文件）

## 要解决的是什么（§43）

准星那圈弹格的贴图，客户端在 `0x48ca0d` 按弹匣容量**精确匹配**挑：

    cap=2→帧6  cap=3→帧5  cap=6→帧6  cap=10→帧8  cap=14→帧7  cap=18→帧9
    其余一律→帧4，而**帧 4 是一张没有任何刻度的光滑圆环**

原版玩家武器的 `MagazineCount` 只用过那 6 个值，所以美术只画了 6 张盘。
自定义武器一旦配成 15 / 20 发，外圈就退化成一个连起来的圆，看不到格子。

本工具把 `2..20` 里缺的 13 个容量补齐（**原版那 6 张一个字节不动**，
原版武器的观感完全不变），`> 20` 仍然走光滑圆环 —— 格子太密反而糊成一片，
这是用户 2026-09-19 拍的板。

## 怎么画的（不是从零画，是「原版光滑环 + 等分黑缝」）

实测：原版 14 格盘（帧 7）在格子中心处的径向剖面和光滑环（帧 4）**逐像素完全一致**，
缝里是黑色 `alpha≈194`、径向只占 `r∈[19,28]`（两端把内外黑边切透）。
⇒ 原版美术就是拿同一个环底切 N 条缝。本工具照做，所以新盘和原版天然同一个模子：
`w=2.5` 生成的 14 格盘和原版帧 7 几乎逐像素重合（那也是 3/6/14 格那一族的缝宽）。

## 幂等

底图永远取**帧 4**（位置固定在图集 `(1,64)`，本工具不碰），新帧一律写在
`y >= NEW_ROW_Y0` 的空白区，`.smf` 每次都按「原版前 19 帧 + 重新生成的 13 帧」重写。
⇒ 反复跑结果一致。原版区域（前 19 帧的记录 + `y < 316` 的像素）跑之前先校验，
被动过就拒绝工作 —— `test/test_aimring.py` 盯着这条。

★ 改完资源要跑 `tools\\build-pack.bat` 重新打加密卷，客户端才看得到。
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 资源落点（明文树；发布前要经 `tools\build-pack.bat` 打成加密卷）。
IMAGE_DIR = os.path.join(ROOT, "game_patched", "Pack_develop", "Images", "Game")
PNG_PATH = os.path.join(IMAGE_DIR, "AimPoint.png")
SMF_PATH = os.path.join(IMAGE_DIR, "AimPoint.smf")

#: C 侧的映射表（自动生成，`bshook.c` include 它）。
HEADER_PATH = os.path.join(ROOT, "hook", "aimring.h")

# ---------------------------------------------------------------------------
# ★★ 单一事实源：弹匣容量 → 图集帧号
# ---------------------------------------------------------------------------

#: `.smf` 的头是 8 字节（magic=2, 帧数），每帧 32 字节 `x0,y0,x1,y1,cx,cy,-1,-1`。
SMF_MAGIC = 2
SMF_HEADER = 8
SMF_RECORD = 32

#: 原版帧数。0~3 沙漏（换弹动画）、4~9 弹格盘、10~18 准星心。
ORIG_FRAMES = 19

#: 原版前 19 帧的 `.smf` 原样（校验用，防止底图被人动过之后我们还往上叠）。
ORIG_SMF_SHA256 = "391fb206d077a2bb3b45e7e0f241b04e9723f001c7a9aa1a86dac15efd7edd4e"

#: 光滑圆环（没有刻度）。`cap > DIAL_MAX` 和 `cap < DIAL_MIN` 都退到它。
DEFAULT_FRAME = 4

#: 原版自带的 6 张盘 —— **保持原样**，原版武器的观感一个像素都不许变。
#: （`cap=2` 用 6 格盘是原版就这样，不是笔误，见 §43。）
ORIG_DIAL = {2: 6, 3: 5, 6: 6, 10: 8, 14: 7, 18: 9}

#: `cap <= 1` 客户端压根不画外圈（`0x48ff7f`: `cmp [记录+0x60],1 / jle`）。
DIAL_MIN = 2

#: 用户 2026-09-19 拍板：**超过 20 发就用光滑圆环** —— 格子太多反而影响观感。
DIAL_MAX = 20

#: 要补画的容量（升序），帧号从 `ORIG_FRAMES` 往后顺排。
NEW_CAPS = tuple(c for c in range(DIAL_MIN, DIAL_MAX + 1) if c not in ORIG_DIAL)
NEW_DIAL = {cap: ORIG_FRAMES + i for i, cap in enumerate(NEW_CAPS)}

#: 完整映射：容量 → 帧号。C 侧那张表就是照它生成的。
DIAL = {**ORIG_DIAL, **NEW_DIAL}

TOTAL_FRAMES = ORIG_FRAMES + len(NEW_CAPS)

# ---------------------------------------------------------------------------
# 几何 —— 全部是从原版帧量出来的，别凭感觉改
# ---------------------------------------------------------------------------

#: 帧尺寸与圆心。原版每帧 62×62，圆心在像素 (30,30) 的**中心**，即几何坐标 30.5。
FRAME_W = FRAME_H = 62
CENTER = 30.5
HOTSPOT = 30                      # `.smf` 里记的 cx/cy

#: 缝宽（像素）。原版 3/6/14 格那一族就是这个值，也是格子数多时最清楚的一档。
#: （原版 10/18 格的缝宽 ≈4.3 px，是它们自己的风格，不作为基准。）
SEAM_W = 2.5

#: 缝里那段黑色的径向范围与 alpha（实测帧 7：`r∈[19,28]` 是 `(0,0,0,194)`，
#: 两端把内外黑边切透）。这里取的是拟合后最贴的一组。
SEAM_R_IN = 18.0
SEAM_R_OUT = 29.0
SEAM_ALPHA = 220

#: 超采样倍数（抗锯齿）。8 已经和原版的边缘过渡对得上。
SUPERSAMPLE = 8

#: 新帧在图集里的落点：`y >= 316` 整片空白，每行 8 个，格距 63 px。
NEW_ROW_Y0 = 316
CELL_PITCH = 63
ROW_X0 = 1
PER_ROW = 8


def frame_rect(index):
    """第 `index` 帧在图集里的矩形（只对新帧有效，原版帧从 `.smf` 里读）。"""
    k = index - ORIG_FRAMES
    row, col = divmod(k, PER_ROW)
    x0 = ROW_X0 + col * CELL_PITCH
    y0 = NEW_ROW_Y0 + row * CELL_PITCH
    return x0, y0, x0 + FRAME_W - 1, y0 + FRAME_H - 1


# ---------------------------------------------------------------------------
# 画
# ---------------------------------------------------------------------------

def render_dial(base, n, seam_w=SEAM_W):
    """把光滑环 `base`（62×62 RGBA）切成 `n` 格。

    缝 = 以圆心为轴、宽 `seam_w` 的径向条：`r∈[SEAM_R_IN, SEAM_R_OUT]` 涂黑，
    两端挖透明（原版就是这么切的，见模块开头）。混合在 premultiplied 空间做。
    """
    from PIL import Image

    px = base.load()
    out = Image.new("RGBA", (FRAME_W, FRAME_H))
    op = out.load()
    step = 360.0 / n
    half = seam_w / 2.0
    ss = SUPERSAMPLE
    tot = float(ss * ss)
    for y in range(FRAME_H):
        for x in range(FRAME_W):
            r0, g0, b0, a0 = px[x, y]
            cov_black = 0
            cov_clear = 0
            for j in range(ss):
                dy = y + (j + 0.5) / ss - CENTER
                for i in range(ss):
                    dx = x + (i + 0.5) / ss - CENTER
                    r = math.hypot(dx, dy)
                    if r < 1e-6:
                        continue
                    # 0° = 正上方（原版 14 格盘在 12 点正上方就是一条缝）
                    th = math.degrees(math.atan2(dx, -dy)) % 360.0
                    k = round(th / step)
                    # 点到「角度 k*step 那条射线」的垂直距离
                    if abs(math.sin(math.radians(th - k * step))) * r < half:
                        if SEAM_R_IN <= r <= SEAM_R_OUT:
                            cov_black += 1
                        else:
                            cov_clear += 1
            cb = cov_black / tot
            cc = cov_clear / tot
            keep = 1.0 - cb - cc
            na = a0 * keep + SEAM_ALPHA * cb          # 缝里那段是纯黑，rgb 贡献为 0
            if na <= 0.0:
                op[x, y] = (255, 255, 255, 0)
            else:
                f = a0 * keep / na
                op[x, y] = (int(round(r0 * f)), int(round(g0 * f)),
                            int(round(b0 * f)), int(round(na)))
    return out


# ---------------------------------------------------------------------------
# `.smf`
# ---------------------------------------------------------------------------

def read_smf(path):
    data = open(path, "rb").read()
    magic, n = struct.unpack("<2I", data[:SMF_HEADER])
    if magic != SMF_MAGIC:
        raise SystemExit(f"[aimring] {path} magic={magic}，不是认识的 .smf")
    want = SMF_HEADER + n * SMF_RECORD
    if len(data) < want:
        raise SystemExit(f"[aimring] {path} 只有 {len(data)} 字节，装不下 {n} 帧")
    recs = [struct.unpack("<8i", data[SMF_HEADER + i * SMF_RECORD:
                                      SMF_HEADER + (i + 1) * SMF_RECORD])
            for i in range(n)]
    return recs


def orig_smf_blob(recs):
    """前 19 帧的原样字节（校验用）。"""
    out = struct.pack("<2I", SMF_MAGIC, ORIG_FRAMES)
    for r in recs[:ORIG_FRAMES]:
        out += struct.pack("<8i", *r)
    return out


def render_smf(recs):
    """原版前 19 帧 + 新帧，重写整份。"""
    out = struct.pack("<2I", SMF_MAGIC, TOTAL_FRAMES)
    for r in recs[:ORIG_FRAMES]:
        out += struct.pack("<8i", *r)
    for idx in range(ORIG_FRAMES, TOTAL_FRAMES):
        x0, y0, x1, y1 = frame_rect(idx)
        out += struct.pack("<8i", x0, y0, x1, y1, HOTSPOT, HOTSPOT, -1, -1)
    return out


# ---------------------------------------------------------------------------
# C 头
# ---------------------------------------------------------------------------

def render_header():
    """`hook/aimring.h` 的完整内容（LF，写盘时转 CRLF）。"""
    lines = [
        "/* ========================================================================",
        " *  aimring.h —— 弹匣容量 → 准星外圈帧号。",
        " *",
        " *  ★★ 【自动生成，不要手改】",
        " *      源头是 tools/aimring.py（它同时画出那些帧），",
        " *      生成物和图集分叉会被 test/test_aimring.py 当场抓住。",
        " *",
        " *  原版 0x48ca0d 只认 6 个容量（2/3/6/10/14/18），其余一律返回帧 4 ——",
        " *  一张没有刻度的光滑圆环。自定义武器配到 15 / 20 发就会撞上它（FINDINGS §43）。",
        " *  bshook 把 0x48ca0d 整个改写成跳到我们的实现，用下面这张表作答。",
        " *",
        " *  原版那 6 个容量仍然返回原版帧号 —— 原版武器的观感一个像素都不变。",
        " * ====================================================================== */",
        "#ifndef POPSHOT_AIMRING_H",
        "#define POPSHOT_AIMRING_H",
        "",
        f"#define POPSHOT_AIM_DEFAULT_FRAME  {DEFAULT_FRAME}   "
        f"/* 光滑圆环：cap < {DIAL_MIN} 或 cap > {DIAL_MAX} */",
        f"#define POPSHOT_AIM_DIAL_MIN       {DIAL_MIN}   /* cap <= 1 客户端压根不画外圈 */",
        f"#define POPSHOT_AIM_DIAL_MAX       {DIAL_MAX}   "
        f"/* 用户拍板：超过就用光滑圆环，格子太密反而糊 */",
        f"#define POPSHOT_AIM_ORIG_FRAMES    {ORIG_FRAMES}   /* 原版图集帧数 */",
        f"#define POPSHOT_AIM_TOTAL_FRAMES   {TOTAL_FRAMES}   /* 补画之后的帧数 */",
        "",
        "/* 下标 = 弹匣容量，值 = 帧号。0 / 1 两格占位，取不到就是默认帧。 */",
        f"static const unsigned char POPSHOT_AIM_FRAME[POPSHOT_AIM_DIAL_MAX + 1] = {{",
    ]
    body = []
    for cap in range(0, DIAL_MAX + 1):
        if cap < DIAL_MIN:
            note = "不画外圈"
            val = DEFAULT_FRAME
        elif cap in ORIG_DIAL:
            note = "原版"
            val = ORIG_DIAL[cap]
        else:
            note = "补画"
            val = NEW_DIAL[cap]
        body.append(f"    /* cap={cap:<2d} */ {val:>2d},   /* {note} */")
    lines += body
    lines += [
        "};",
        "",
        "#endif /* POPSHOT_AIMRING_H */",
        "",
    ]
    return "\n".join(lines)


def write_if_changed(path, want, tag="aimring"):
    """内容有变才落盘。`.h` 用 CRLF（仓库里的 .c/.h 都是）。"""
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            have = f.read().replace("\r\n", "\n")
    except OSError:
        have = None
    if have == want:
        print(f"[{tag}] {os.path.relpath(path, ROOT)} 无变化")
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(want)
    print(f"[{tag}] 已更新 {os.path.relpath(path, ROOT)}")
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def check_pristine(recs, img):
    """跑之前确认原版区域没被动过 —— 被动过就别往上叠。"""
    got = hashlib.sha256(orig_smf_blob(recs)).hexdigest()
    if got != ORIG_SMF_SHA256:
        raise SystemExit(
            f"[aimring] AimPoint.smf 的前 {ORIG_FRAMES} 帧和原版对不上\n"
            f"          期望 {ORIG_SMF_SHA256}\n          实际 {got}\n"
            f"          （原版帧被人改过？先恢复再跑本工具）")
    if img.size != (512, 512):
        raise SystemExit(f"[aimring] AimPoint.png 尺寸是 {img.size}，期望 512×512")
    need_rows = (len(NEW_CAPS) + PER_ROW - 1) // PER_ROW
    need_y = NEW_ROW_Y0 + need_rows * CELL_PITCH
    if need_y > img.height:
        raise SystemExit(f"[aimring] 图集放不下：要用到 y={need_y}，只有 {img.height}")
    # 原版帧全部在 y < NEW_ROW_Y0；新帧区之外不许动
    for r in recs[:ORIG_FRAMES]:
        if r[3] >= NEW_ROW_Y0:
            raise SystemExit(f"[aimring] 原版帧 {r} 伸进了新帧区 y>={NEW_ROW_Y0}")


def build(dry_run=False, header_only=False, check=False):
    header = render_header()
    if header_only:
        write_if_changed(HEADER_PATH, header)
        return 0

    from PIL import Image

    recs = read_smf(SMF_PATH)
    img = Image.open(PNG_PATH).convert("RGBA")
    check_pristine(recs, img)

    # 底图：原版帧 4（光滑圆环）
    bx0, by0, bx1, by1 = recs[DEFAULT_FRAME][:4]
    base = img.crop((bx0, by0, bx1 + 1, by1 + 1))
    if base.size != (FRAME_W, FRAME_H):
        raise SystemExit(f"[aimring] 帧 {DEFAULT_FRAME} 是 {base.size}，期望 62×62")

    print(f"[aimring] 补画 {len(NEW_CAPS)} 张盘：" +
          " ".join(f"cap{c}→帧{NEW_DIAL[c]}" for c in NEW_CAPS))
    print(f"[aimring] 原版 6 张保持不动：" +
          " ".join(f"cap{c}→帧{ORIG_DIAL[c]}" for c in sorted(ORIG_DIAL)))
    print(f"[aimring] cap<{DIAL_MIN} 或 cap>{DIAL_MAX} → 帧 {DEFAULT_FRAME}（光滑圆环）")

    if dry_run:
        for cap in NEW_CAPS:
            print(f"    cap={cap:<3d} 帧={NEW_DIAL[cap]:<3d} 落点={frame_rect(NEW_DIAL[cap])}")
        print("[aimring] --dry-run：什么都没写")
        return 0

    out = img.copy()
    for cap in NEW_CAPS:
        idx = NEW_DIAL[cap]
        x0, y0, _, _ = frame_rect(idx)
        dial = render_dial(base, cap)
        out.paste(dial, (x0, y0))           # 直接覆盖，保证幂等

    want_smf = render_smf(recs)

    changed = False
    import io
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    new_png = buf.getvalue()
    if new_png != open(PNG_PATH, "rb").read():
        open(PNG_PATH, "wb").write(new_png)
        print(f"[aimring] 已更新 {os.path.relpath(PNG_PATH, ROOT)}")
        changed = True
    else:
        print(f"[aimring] {os.path.relpath(PNG_PATH, ROOT)} 无变化")
    if want_smf != open(SMF_PATH, "rb").read():
        open(SMF_PATH, "wb").write(want_smf)
        print(f"[aimring] 已更新 {os.path.relpath(SMF_PATH, ROOT)}"
              f"（{ORIG_FRAMES} → {TOTAL_FRAMES} 帧）")
        changed = True
    else:
        print(f"[aimring] {os.path.relpath(SMF_PATH, ROOT)} 无变化")
    write_if_changed(HEADER_PATH, header)
    if changed:
        print("[aimring] ★ 资源变了 —— 记得跑 tools\\build-pack.bat 重打加密卷")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="补画准星弹格盘")
    ap.add_argument("--dry-run", action="store_true", help="只说要做什么，不落盘")
    ap.add_argument("--header-only", action="store_true",
                    help="只重新生成 hook/aimring.h（编译时用）")
    ap.add_argument("--check", action="store_true", help="只检查产物是否最新")
    args = ap.parse_args(argv)

    if args.check:
        try:
            with open(HEADER_PATH, "r", encoding="utf-8", newline="") as f:
                have = f.read().replace("\r\n", "\n")
        except OSError:
            have = None
        if have != render_header():
            print("[aimring] !! hook/aimring.h 和 tools/aimring.py 对不上，"
                  "请跑一次 python tools/aimring.py --header-only", file=sys.stderr)
            return 1
        recs = read_smf(SMF_PATH)
        if len(recs) != TOTAL_FRAMES:
            print(f"[aimring] !! AimPoint.smf 有 {len(recs)} 帧，期望 {TOTAL_FRAMES}",
                  file=sys.stderr)
            return 1
        print("[aimring] 产物都是最新的")
        return 0

    return build(dry_run=args.dry_run, header_only=args.header_only)


if __name__ == "__main__":
    sys.exit(main())
