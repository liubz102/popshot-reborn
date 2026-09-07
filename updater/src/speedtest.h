/* --------------------------------------------------------------------------
   speedtest.h —— 下载源测速与选源（用户 2026-09-07 拍板的规则）。

   真正下载更新包之前先找最快的来源：
     1. 先单独测 GitHub 直连 SPEED_WINDOW_MS；速度 > SPEED_GOOD_BPS 就直连；
     2. 否则把 config\update.config 里的代理每 SPEED_GROUP 个一组**并行**测，
        一组里有达标的就选组内最快的那个，后面的组不再测；
     3. 全部测完仍无达标 → 所有来源（含直连）里相对最快的那个。
   每个来源的「速度」= 窗口内收到的字节数 / min(窗口, 提前收完的用时)。
   代理的用法是直接拼在原地址前面：<代理>/https://github.com/...。

   测速本身（WinHTTP）由调用方以 speed_measure_fn 注入 —— 编排逻辑因此
   不碰网络，selftest 用假测速函数把选源规则钉住。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_SPEEDTEST_H
#define UPDATER_SPEEDTEST_H

#include <windows.h>
#include "config.h"

#define SPEED_WINDOW_MS   5000              /* 每个来源测 5 秒（含建连），各阶段超时也是它 */
#define SPEED_GROUP       4                 /* 代理每 4 个一组并行 */
#define SPEED_GOOD_BPS    (1ULL << 20)      /* 达标线：> 1 MiB/s（界面显示的也是 MiB/s） */

typedef struct SpeedSample {
    unsigned long long bytes;     /* 窗口内收到的字节 */
    unsigned elapsed_ms;          /* min(窗口, 提前收完的用时)；0 按 1 算 */
    int cancelled;                /* 玩家点了取消 */
    wchar_t note[128];            /* 结束原因（到点 / 收完 / 出错），日志用 */
} SpeedSample;

/* 测一个完整下载地址。★ 一组里的 4 个会在 4 个线程里同时调它。 */
typedef void (*speed_measure_fn)(void *user, const wchar_t *url,
                                 SpeedSample *out);
/* 阶段通知（编排线程调用）：first = -1 直连；否则正在测
   proxies->url[first .. first+count-1]，total = 代理总数。 */
typedef void (*speed_phase_fn)(void *user, int first, int count, int total);

typedef struct SpeedPick {
    int index;                    /* -1 = 直连，否则 proxies->url[index] */
    unsigned long long bps;       /* 选中来源的测速结果 */
    int qualified;                /* 1 = 达标；0 = 全不达标、取相对最快 */
    int measured;                 /* 实际测过的来源数（含直连），日志/测试用 */
} SpeedPick;

/* 字节数 / 用时 -> B/s。 */
unsigned long long speed_bps(const SpeedSample *s);
/* 代理前缀 + 原地址 -> 加速地址；proxy 为空就是原地址。 */
void speed_compose_url(const wchar_t *proxy, const wchar_t *url,
                       wchar_t *out, size_t cap);
/* 编排。返回 1 = out 有效；0 = 中途被取消。
   proxies 为空时一个来源都不测，直接选直连（省 5 秒；也让 e2e 夹具不变）。 */
int speedtest_pick(const wchar_t *file_url, const ProxyList *proxies,
                   speed_measure_fn measure, speed_phase_fn phase, void *user,
                   SpeedPick *out);

/* ---- manifest 这类小件的代理兜底（不测速；用户 2026-09-07 第二条） ----------
   直连 MANIFEST_ATTEMPT_MS 内没取到 → 随机挑一个代理试，每个也是
   MANIFEST_ATTEMPT_MS，不成再随机换一个（每个代理最多试一次）→ 全败才算失败，
   调用方照旧走「取不到更新清单」的手动下载提示。 */

#define MANIFEST_ATTEMPT_MS 5000

/* 一次取件：窗口内成功返回 1；失败返回 0 且 err 带原因（取消 = L"cancelled"）。 */
typedef int (*proxy_fetch_fn)(void *user, const wchar_t *url,
                              wchar_t *err, size_t err_cap);
/* 每次尝试前的通知：attempt 从 1 起；total = 代理数；index = -1 直连 / 代理下标。 */
typedef void (*proxy_try_fn)(void *user, int attempt, int total, int index);

/* Fisher-Yates 洗牌，自带 LCG（不碰 CRT rand，selftest 可重放）。order 先填好。 */
void proxy_shuffle(int *order, int n, unsigned seed);
/* 返回 1 成功（*picked = -1 直连 / 代理下标）；0 失败（err = 最后一次的原因；
   取消时 err = L"cancelled" 且立刻停）。*attempts = 总共试了几次（含成功那次）。
   label 只用于日志（"manifest"）。 */
int proxy_fetch_fallback(const wchar_t *label, const wchar_t *url,
                         const ProxyList *proxies, unsigned seed,
                         proxy_fetch_fn fetch, proxy_try_fn notify, void *user,
                         int *picked, int *attempts, wchar_t *err, size_t err_cap);

#endif /* UPDATER_SPEEDTEST_H */
