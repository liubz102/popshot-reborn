/* ========================================================================== */
/* fsview.h —— 无边框全屏的「画面矩形」和坐标换算（X_Mod X21 / §147 / D107）   */
/*                                                                            */
/* 游戏的后台缓冲 / 界面坐标系写死 1024×768（`[App+0x44/0x48]`，视口 0x5bfc71）。*/
/* 无边框全屏时窗口盖满显示器，Present 把后台缓冲画进「画面矩形」（保持比例时 */
/* 和界面同比例、居中、留黑边；拉伸时就是整个客户区，X22）；大厅类阶段的光标 */
/* 读写要在「客户区坐标」和                                                   */
/* 「界面坐标」之间换算。这里只有纯整数运算，不依赖 Windows 头 ——              */
/* `hook/bshook.c` 和 `test/hook/test_fsview.c` 共用这一份，换算只有一处。      */
/*                                                                            */
/* 取整规则（审查时算出来的，别改成「取像素中心」）：                          */
/*   客户区 → 界面：u = floor((c − L) · ui / W)       真向下取整（c < L 时为负）*/
/*   界面 → 客户区：c = L + ceil(u · W / ui)                                   */
/* 两个轴各算各的：该轴画面不比界面小（W ≥ ui，常见情况）时                    */
/* 界面→客户区→界面 严格回到原值；该轴画面比界面小（比如 1280×720 的屏，      */
/* 高 720 < 768）时差 ±1。拉伸（X22）时两个轴的倍数不同，规则照样按轴成立。     */
/* ★ 取像素中心那种写法在 1 < W/ui < 2 时往返会漂（审查实例：k=1.40625、u=3     */
/*   → c=4 → 2），放开抓取时光标会一格一格挪。                                 */
/* ========================================================================== */
#ifndef POPSHOT_FSVIEW_H
#define POPSHOT_FSVIEW_H

/* 和 Win32 的 RECT 同布局（四个 LONG），bshook 里可以直接当 RECT 用。 */
typedef struct fsv_rect { long l, t, r, b; } fsv_rect;

static long fsv_floordiv(long long a, long long b)   /* b > 0 */
{
    long long q = a / b;
    if (a % b != 0 && a < 0) q--;
    return (long)q;
}

static long fsv_ceildiv(long long a, long long b)    /* b > 0 */
{
    long long q = a / b;
    if (a % b != 0 && a > 0) q++;
    return (long)q;
}

static int fsv_valid(const fsv_rect *pic)
{
    return pic->r > pic->l && pic->b > pic->t;
}

/* 在 area_w × area_h 的客户区里放下最大的、和 ui_w : ui_h 同比例的矩形，居中。
   比例比界面更宽 ⇒ 左右黑边；更窄（5:4、竖屏）⇒ 上下黑边；正好 4:3 ⇒ 没有黑边。 */
static void fsv_fit(long area_w, long area_h, long ui_w, long ui_h, fsv_rect *pic)
{
    long w, h;

    if (area_w <= 0 || area_h <= 0 || ui_w <= 0 || ui_h <= 0) {
        pic->l = pic->t = pic->r = pic->b = 0;
        return;
    }
    if ((long long)area_w * ui_h >= (long long)area_h * ui_w) {
        h = area_h;
        w = (long)(((long long)area_h * ui_w + ui_h / 2) / ui_h);
        if (w > area_w) w = area_w;
    } else {
        w = area_w;
        h = (long)(((long long)area_w * ui_h + ui_w / 2) / ui_w);
        if (h > area_h) h = area_h;
    }
    pic->l = (area_w - w) / 2;
    pic->t = (area_h - h) / 2;
    pic->r = pic->l + w;
    pic->b = pic->t + h;
}

/* 按玩家选的全屏画面摆画面矩形（X_Mod X22 / D108）：
   stretch = 0「全屏(保持比例)」—— fsv_fit，和界面同比例、居中、留黑边；
   stretch = 1「全屏(拉伸)」  —— 整个客户区，两个轴各自缩放，没有黑边。
   下面的换算本来就是 x / y 各按各的比例算，拉伸不用另写一套；fsv_bars 对整块画面回 0 块。
   4:3 的屏上两种结果一样（fsv_fit 放出来的就是整块屏）。 */
static void fsv_layout(long area_w, long area_h, long ui_w, long ui_h, int stretch, fsv_rect *pic)
{
    if (stretch && area_w > 0 && area_h > 0) {
        pic->l = pic->t = 0;
        pic->r = area_w;
        pic->b = area_h;
        return;
    }
    fsv_fit(area_w, area_h, ui_w, ui_h, pic);
}

/* 客户区坐标 → 界面坐标。不夹：画面外的点换出来就在 [0, ui) 外（窗口模式下
   光标移出窗口时原版本来就会拿到界面外的值）。画面矩形无效时原样不动。 */
static void fsv_client_to_ui(const fsv_rect *pic, long ui_w, long ui_h, long *x, long *y)
{
    long pw = pic->r - pic->l, ph = pic->b - pic->t;

    if (pw <= 0 || ph <= 0 || ui_w <= 0 || ui_h <= 0) return;
    *x = fsv_floordiv((long long)(*x - pic->l) * ui_w, pw);
    *y = fsv_floordiv((long long)(*y - pic->t) * ui_h, ph);
}

/* 界面坐标 → 客户区坐标：落在界面那一格对应的第一个客户区像素上。 */
static void fsv_ui_to_client(const fsv_rect *pic, long ui_w, long ui_h, long *x, long *y)
{
    long pw = pic->r - pic->l, ph = pic->b - pic->t;

    if (pw <= 0 || ph <= 0 || ui_w <= 0 || ui_h <= 0) return;
    *x = pic->l + fsv_ceildiv((long long)*x * pw, ui_w);
    *y = pic->t + fsv_ceildiv((long long)*y * ph, ui_h);
}

/* 客户区 cw × ch 里画面矩形之外的黑边，最多 4 块（上、下、左、右），返回块数。 */
static int fsv_bars(long cw, long ch, const fsv_rect *pic, fsv_rect out[4])
{
    int n = 0;

    if (!fsv_valid(pic)) {
        if (cw > 0 && ch > 0) {
            out[0].l = 0; out[0].t = 0; out[0].r = cw; out[0].b = ch;
            return 1;
        }
        return 0;
    }
    if (pic->t > 0)  { out[n].l = 0;      out[n].t = 0;      out[n].r = cw;     out[n].b = pic->t; n++; }
    if (pic->b < ch) { out[n].l = 0;      out[n].t = pic->b; out[n].r = cw;     out[n].b = ch;     n++; }
    if (pic->l > 0)  { out[n].l = 0;      out[n].t = pic->t; out[n].r = pic->l; out[n].b = pic->b; n++; }
    if (pic->r < cw) { out[n].l = pic->r; out[n].t = pic->t; out[n].r = cw;     out[n].b = pic->b; n++; }
    return n;
}

#endif /* POPSHOT_FSVIEW_H */
