/*
 * 无边框全屏的画面矩形 / 坐标换算 —— 原生夹具（X_Mod X21 / D107）。
 *
 * 由 test-fsview.bat 编译成 32 位控制台程序独立跑，不需要游戏。
 * **直接 `#include "fsview.h"`**（hook/ 下那一份），测的就是 bshook.dll 里用的换算，不是抄一份。
 *
 * 要守住的：
 *   1. 画面矩形：在客户区里、居中、和 1024×768 同比例、有一条边贴满；
 *   2. 界面 → 客户区 → 界面：画面不比界面小时**严格**回原值，小时差 ±1
 *      （放开抓取时游戏把虚拟光标放回屏幕，往返漂一格光标就会一点点挪）；
 *   3. 客户区 → 界面：单调；画面左上角那个像素是 0、右下角是 1023 / 767；
 *      画面外一个像素就是 -1 / 1024（真向下取整，不是向 0 截断）；
 *   4. 黑边：和画面不重叠，加起来正好铺满客户区；
 *   5. 画面矩形无效时换算原样不动（不除 0）。
 */
#include <stdio.h>
#include <stdlib.h>
#include "fsview.h"

#define UI_W 1024
#define UI_H 768

static int g_fail;
static int g_checks;

#define CHECK(cond, ...) do {                                   \
        g_checks++;                                             \
        if (!(cond)) {                                          \
            if (g_fail < 40) { printf("  FAIL: "); printf(__VA_ARGS__); printf("\n"); } \
            g_fail++;                                           \
        }                                                       \
    } while (0)

static const long MONITORS[][2] = {
    { 800,  600}, {1024,  768}, {1280,  720}, {1280, 1024}, {1366,  768},
    {1536,  864}, {1600,  900}, {1680, 1050}, {1920, 1080}, {1920, 1200},
    {2560, 1080}, {2560, 1440}, {2880, 1800}, {3440, 1440}, {3840, 2160},
    {5120, 1440}, {1080, 1920}, { 768, 1366},
};

static void check_fit(long w, long h, const fsv_rect *p)
{
    long pw = p->r - p->l, ph = p->b - p->t;
    long long skew = (long long)pw * UI_H - (long long)ph * UI_W;

    CHECK(fsv_valid(p), "%ldx%ld: 画面矩形无效", w, h);
    CHECK(p->l >= 0 && p->t >= 0 && p->r <= w && p->b <= h,
          "%ldx%ld: 画面 (%ld,%ld)-(%ld,%ld) 出了客户区", w, h, p->l, p->t, p->r, p->b);
    CHECK(pw == w || ph == h, "%ldx%ld: 画面 %ldx%ld 哪条边都没贴满", w, h, pw, ph);
    /* 比例：宽 = round(高 × 4/3) 或 高 = round(宽 × 3/4)，误差不到半个像素 */
    CHECK(llabs(skew) <= (pw == w ? UI_W / 2 : UI_H / 2),
          "%ldx%ld: 画面 %ldx%ld 不是 4:3（偏 %lld）", w, h, pw, ph, skew);
    CHECK(labs((w - p->r) - p->l) <= 1 && labs((h - p->b) - p->t) <= 1,
          "%ldx%ld: 画面 (%ld,%ld)-(%ld,%ld) 没居中", w, h, p->l, p->t, p->r, p->b);
}

static void check_roundtrip(long w, long h, const fsv_rect *p)
{
    long pw = p->r - p->l, ph = p->b - p->t;
    long u, c, prev, x, y;

    /* 界面 → 客户区 → 界面（两个轴分开扫，另一轴取中间值） */
    /* 画面比界面小（k < 1）时最后一格界面坐标会落到画面外一个像素（ceil 的代价），
       那种屏上锁鼠标的夹框会把它拉回来；画面不比界面小时必须严格在画面里。 */
    for (u = 0; u < UI_W; u++) {
        x = u; y = UI_H / 2;
        fsv_ui_to_client(p, UI_W, UI_H, &x, &y);
        CHECK(x >= p->l && (pw >= UI_W ? x < p->r : x <= p->r),
              "%ldx%ld: 界面 x=%ld → 客户区 %ld 不在画面里", w, h, u, x);
        fsv_client_to_ui(p, UI_W, UI_H, &x, &y);
        if (pw >= UI_W) CHECK(x == u, "%ldx%ld: 界面 x=%ld 往返成了 %ld", w, h, u, x);
        else            CHECK(labs(x - u) <= 1, "%ldx%ld: 界面 x=%ld 往返成了 %ld（>±1）", w, h, u, x);
    }
    for (u = 0; u < UI_H; u++) {
        x = UI_W / 2; y = u;
        fsv_ui_to_client(p, UI_W, UI_H, &x, &y);
        CHECK(y >= p->t && (ph >= UI_H ? y < p->b : y <= p->b),
              "%ldx%ld: 界面 y=%ld → 客户区 %ld 不在画面里", w, h, u, y);
        fsv_client_to_ui(p, UI_W, UI_H, &x, &y);
        if (ph >= UI_H) CHECK(y == u, "%ldx%ld: 界面 y=%ld 往返成了 %ld", w, h, u, y);
        else            CHECK(labs(y - u) <= 1, "%ldx%ld: 界面 y=%ld 往返成了 %ld（>±1）", w, h, u, y);
    }

    /* 客户区 → 界面：单调、边界值、画面外是真向下取整 */
    prev = -1000000;
    for (c = p->l - 60; c < p->r + 60; c++) {
        x = c; y = p->t;
        fsv_client_to_ui(p, UI_W, UI_H, &x, &y);
        CHECK(x >= prev, "%ldx%ld: 客户区 x=%ld → %ld 比前一个 %ld 小（不单调）", w, h, c, x, prev);
        if (c >= p->l && c < p->r)
            CHECK(x >= 0 && x < UI_W, "%ldx%ld: 画面内客户区 x=%ld → 界面 %ld 越界", w, h, c, x);
        else
            CHECK(x < 0 || x >= UI_W, "%ldx%ld: 画面外客户区 x=%ld → 界面 %ld 却在界面内", w, h, c, x);
        prev = x;
    }
    x = p->l; y = p->t;
    fsv_client_to_ui(p, UI_W, UI_H, &x, &y);
    CHECK(x == 0 && y == 0, "%ldx%ld: 画面左上角 → (%ld,%ld)，应为 (0,0)", w, h, x, y);
    /* 画面比界面小时一个客户区像素盖不止一格界面，右下角可能落在 1022 / 766 */
    x = p->r - 1; y = p->b - 1;
    fsv_client_to_ui(p, UI_W, UI_H, &x, &y);
    CHECK((pw >= UI_W ? x == UI_W - 1 : (x >= UI_W - 2 && x < UI_W))
          && (ph >= UI_H ? y == UI_H - 1 : (y >= UI_H - 2 && y < UI_H)),
          "%ldx%ld: 画面右下角 → (%ld,%ld)，应为 (1023,767)", w, h, x, y);
    /* 画面外一格：真向下取整 ⇒ 负数（向 0 截断会得 0，这条就是钉它的）；
       画面不比界面小时正好 -1。 */
    x = p->l - 1; y = p->t - 1;
    fsv_client_to_ui(p, UI_W, UI_H, &x, &y);
    CHECK(x < 0 && y < 0 && (pw < UI_W || x == -1) && (ph < UI_H || y == -1),
          "%ldx%ld: 画面左上外一格 → (%ld,%ld)，应为负数（画面不小于界面时 -1）", w, h, x, y);
    x = p->r; y = p->b;
    fsv_client_to_ui(p, UI_W, UI_H, &x, &y);
    CHECK(x >= UI_W && y >= UI_H, "%ldx%ld: 画面右下外一格 → (%ld,%ld) 还在界面内", w, h, x, y);
}

static void check_bars(long w, long h, const fsv_rect *p)
{
    fsv_rect bars[4];
    int n = fsv_bars(w, h, p, bars), i, j;
    long long area = (long long)(p->r - p->l) * (p->b - p->t);

    for (i = 0; i < n; i++) {
        const fsv_rect *b = &bars[i];
        CHECK(b->r > b->l && b->b > b->t, "%ldx%ld: 黑边 %d 是空的", w, h, i);
        CHECK(b->l >= 0 && b->t >= 0 && b->r <= w && b->b <= h, "%ldx%ld: 黑边 %d 出了客户区", w, h, i);
        CHECK(b->r <= p->l || b->l >= p->r || b->b <= p->t || b->t >= p->b,
              "%ldx%ld: 黑边 %d 和画面重叠", w, h, i);
        for (j = 0; j < i; j++) {
            const fsv_rect *o = &bars[j];
            CHECK(b->r <= o->l || b->l >= o->r || b->b <= o->t || b->t >= o->b,
                  "%ldx%ld: 黑边 %d 和 %d 重叠", w, h, i, j);
        }
        area += (long long)(b->r - b->l) * (b->b - b->t);
    }
    CHECK(area == (long long)w * h, "%ldx%ld: 画面 + 黑边 = %lld，不等于客户区 %lld",
          w, h, area, (long long)w * h);
}

static void check_known(long w, long h, long l, long t, long r, long b)
{
    fsv_rect p;
    fsv_fit(w, h, UI_W, UI_H, &p);
    CHECK(p.l == l && p.t == t && p.r == r && p.b == b,
          "%ldx%ld: 画面 (%ld,%ld)-(%ld,%ld)，应为 (%ld,%ld)-(%ld,%ld)",
          w, h, p.l, p.t, p.r, p.b, l, t, r, b);
}

int main(void)
{
    size_t i;
    fsv_rect bad = {0, 0, 0, 0};
    long x = 123, y = 456;

    for (i = 0; i < sizeof(MONITORS) / sizeof(MONITORS[0]); i++) {
        long w = MONITORS[i][0], h = MONITORS[i][1];
        fsv_rect p;
        int before = g_fail;
        fsv_fit(w, h, UI_W, UI_H, &p);
        check_fit(w, h, &p);
        check_roundtrip(w, h, &p);
        check_bars(w, h, &p);
        printf("  %5ldx%-5ld 画面 (%4ld,%4ld)-(%4ld,%4ld) %s\n",
               w, h, p.l, p.t, p.r, p.b, g_fail == before ? "ok" : "FAIL");
    }

    /* 用户这台、超宽屏、5:4、刚好 4:3、比界面还小的屏 —— 钉住具体数 */
    check_known(1920, 1080,  240,  0, 1680, 1080);
    check_known(3440, 1440,  760,  0, 2680, 1440);
    check_known(1280, 1024,    0, 32, 1280,  992);
    check_known(1024,  768,    0,  0, 1024,  768);
    check_known(1366,  768,  171,  0, 1195,  768);
    check_known(1280,  720,  160,  0, 1120,  720);

    /* 画面矩形无效：不动、不除 0；黑边铺满整个客户区 */
    fsv_client_to_ui(&bad, UI_W, UI_H, &x, &y);
    CHECK(x == 123 && y == 456, "无效画面：客户区→界面改了值 (%ld,%ld)", x, y);
    fsv_ui_to_client(&bad, UI_W, UI_H, &x, &y);
    CHECK(x == 123 && y == 456, "无效画面：界面→客户区改了值 (%ld,%ld)", x, y);
    {
        fsv_rect bars[4];
        int n = fsv_bars(640, 480, &bad, bars);
        CHECK(n == 1 && bars[0].l == 0 && bars[0].t == 0 && bars[0].r == 640 && bars[0].b == 480,
              "无效画面：黑边应是整个客户区一块，得到 %d 块", n);
    }
    fsv_fit(0, 1080, UI_W, UI_H, &bad);
    CHECK(!fsv_valid(&bad), "客户区宽 0：画面矩形应无效");

    printf("[fsview] %d 项检查，%d 项失败\n", g_checks, g_fail);
    return g_fail ? 1 : 0;
}
