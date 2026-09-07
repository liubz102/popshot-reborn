/* --------------------------------------------------------------------------
   speedtest.c —— 见 speedtest.h。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include "speedtest.h"
#include "util.h"
#include "log.h"

unsigned long long speed_bps(const SpeedSample *s)
{
    unsigned elapsed = s->elapsed_ms ? s->elapsed_ms : 1;
    return s->bytes * 1000ULL / elapsed;
}

void speed_compose_url(const wchar_t *proxy, const wchar_t *url,
                       wchar_t *out, size_t cap)
{
    if (!proxy || !*proxy)
        _snwprintf(out, cap, L"%ls", url);
    else
        _snwprintf(out, cap, L"%ls/%ls", proxy, url);
    out[cap - 1] = 0;
}

/* ---- 一组并行探针 -------------------------------------------------------- */

typedef struct Probe {
    speed_measure_fn measure;
    void *user;
    wchar_t url[1200];            /* 代理 256 + 原地址 512 绰绰有余 */
    SpeedSample sample;
} Probe;

static DWORD WINAPI probe_thread(LPVOID param)
{
    Probe *p = (Probe *)param;
    p->measure(p->user, p->url, &p->sample);
    return 0;
}

/* 起不了线程就当场串行测：结果一样，只是慢。 */
static void run_group(Probe *probes, int count)
{
    HANDLE threads[SPEED_GROUP];
    int i;
    for (i = 0; i < count; i++) {
        threads[i] = CreateThread(NULL, 0, probe_thread, &probes[i], 0, NULL);
        if (!threads[i]) probe_thread(&probes[i]);
    }
    for (i = 0; i < count; i++) {
        if (!threads[i]) continue;
        /* 不设上限：探针自己的窗口 / 超时（SPEED_WINDOW_MS）就是上限。 */
        WaitForSingleObject(threads[i], INFINITE);
        CloseHandle(threads[i]);
    }
}

/* ★ 只在编排线程里记日志：log 文件按 FILE_SHARE_READ 打开，多个探针线程
   同时写会互相把行丢掉（log.c 写失败静默放弃）。 */
static void log_sample(const wchar_t *label, const wchar_t *url,
                       const SpeedSample *s)
{
    wchar_t mib[32];
    mib_to_wide(speed_bps(s), mib, 32);
    log_line("speedtest %ls %ls: %llu B / %lu ms = %ls MiB/s (%ls)",
             label, url, s->bytes, (unsigned long)s->elapsed_ms, mib,
             s->note[0] ? s->note : L"-");
}

static void log_pick(const ProxyList *proxies, const SpeedPick *pick)
{
    wchar_t mib[32];
    mib_to_wide(pick->bps, mib, 32);
    if (pick->index < 0)
        log_line("speedtest pick: direct (%ls MiB/s, %ls)", mib,
                 pick->qualified ? L"qualified" : L"best-effort");
    else
        log_line("speedtest pick: proxy[%d] %ls (%ls MiB/s, %ls)", pick->index,
                 proxies->url[pick->index], mib,
                 pick->qualified ? L"qualified" : L"best-effort");
}

int speedtest_pick(const wchar_t *file_url, const ProxyList *proxies,
                   speed_measure_fn measure, speed_phase_fn phase, void *user,
                   SpeedPick *out)
{
    Probe group[SPEED_GROUP];
    SpeedSample direct;
    unsigned long long best_bps;
    int best_index;
    int first;

    memset(out, 0, sizeof(*out));
    out->index = -1;
    if (!proxies || proxies->count <= 0) {
        log_line("speedtest: no proxies configured, direct download");
        return 1;
    }

    /* --- 1. 直连单独测 -------------------------------------------------- */
    if (phase) phase(user, -1, 1, proxies->count);
    memset(&direct, 0, sizeof(direct));
    measure(user, file_url, &direct);
    out->measured = 1;
    if (direct.cancelled) return 0;
    log_sample(L"direct", file_url, &direct);
    best_bps = speed_bps(&direct);
    best_index = -1;
    if (best_bps > SPEED_GOOD_BPS) {
        out->bps = best_bps;
        out->qualified = 1;
        log_pick(proxies, out);
        return 1;
    }

    /* --- 2. 代理分组并行：一组里有达标的就选组内最快，后面不再测 -------- */
    for (first = 0; first < proxies->count; first += SPEED_GROUP) {
        int count = proxies->count - first;
        int i;
        int group_best = -1;
        unsigned long long group_best_bps = 0;
        if (count > SPEED_GROUP) count = SPEED_GROUP;
        if (phase) phase(user, first, count, proxies->count);
        for (i = 0; i < count; i++) {
            memset(&group[i], 0, sizeof(Probe));
            group[i].measure = measure;
            group[i].user = user;
            speed_compose_url(proxies->url[first + i], file_url,
                              group[i].url, 1200);
        }
        run_group(group, count);
        out->measured += count;
        for (i = 0; i < count; i++)
            if (group[i].sample.cancelled) return 0;
        for (i = 0; i < count; i++) {
            unsigned long long bps = speed_bps(&group[i].sample);
            wchar_t label[32];
            _snwprintf(label, 32, L"proxy[%d]", first + i);
            label[31] = 0;
            log_sample(label, group[i].url, &group[i].sample);
            if (bps > SPEED_GOOD_BPS && bps > group_best_bps) {
                group_best = first + i;
                group_best_bps = bps;
            }
            if (bps > best_bps) {           /* 平局留先测的（直连优先） */
                best_bps = bps;
                best_index = first + i;
            }
        }
        if (group_best >= 0) {
            out->index = group_best;
            out->bps = group_best_bps;
            out->qualified = 1;
            log_pick(proxies, out);
            return 1;
        }
    }

    /* --- 3. 都不达标：所有来源里相对最快的 ------------------------------ */
    out->index = best_index;
    out->bps = best_bps;
    out->qualified = 0;
    log_pick(proxies, out);
    return 1;
}

/* ====================================================================== */
/*  manifest 的代理兜底（不测速）                                            */
/* ====================================================================== */

void proxy_shuffle(int *order, int n, unsigned seed)
{
    int i;
    for (i = n - 1; i > 0; i--) {
        int j, t;
        seed = seed * 1103515245u + 12345u;          /* 经典 LCG，够洗几十张牌 */
        j = (int)((seed >> 16) % (unsigned)(i + 1));
        t = order[i]; order[i] = order[j]; order[j] = t;
    }
}

int proxy_fetch_fallback(const wchar_t *label, const wchar_t *url,
                         const ProxyList *proxies, unsigned seed,
                         proxy_fetch_fn fetch, proxy_try_fn notify, void *user,
                         int *picked, int *attempts, wchar_t *err, size_t err_cap)
{
    int order[PROXY_MAX];
    int n = proxies ? proxies->count : 0;
    int k;

    *picked = -1;
    *attempts = 0;
    if (err_cap) err[0] = 0;

    /* --- 直连先来 ------------------------------------------------------- */
    if (notify) notify(user, 1, n, -1);
    (*attempts)++;
    if (fetch(user, url, err, err_cap)) {
        log_line("%ls direct: ok", label);
        return 1;
    }
    log_line("%ls direct: failed (%ls)", label, err);
    if (wide_ieq(err, L"cancelled")) return 0;

    /* --- 随机顺序逐个试代理，每个最多一次 ------------------------------- */
    for (k = 0; k < n; k++) order[k] = k;
    proxy_shuffle(order, n, seed);
    for (k = 0; k < n; k++) {
        int idx = order[k];
        wchar_t purl[1200];
        speed_compose_url(proxies->url[idx], url, purl, 1200);
        if (notify) notify(user, *attempts + 1, n, idx);
        (*attempts)++;
        if (fetch(user, purl, err, err_cap)) {
            log_line("%ls proxy[%d] %ls: ok", label, idx, proxies->url[idx]);
            *picked = idx;
            return 1;
        }
        log_line("%ls proxy[%d] %ls: failed (%ls)", label, idx,
                 proxies->url[idx], err);
        if (wide_ieq(err, L"cancelled")) return 0;
    }
    return 0;                                   /* err = 最后一次的原因 */
}
