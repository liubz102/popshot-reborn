/* --------------------------------------------------------------------------
   net_http.c —— 见 net_http.h。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <winhttp.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include <stdlib.h>
#include "net_http.h"
#include "util.h"

#pragma comment(lib, "winhttp.lib")

/* 老 SDK 头里没有 TLS1.2/1.3 的位定义，自己补（值是 winhttp 的固定位掩码）。 */
#ifndef WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_1
#define WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_1 0x00000200
#endif
#ifndef WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2
#define WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2 0x00000800
#endif
#ifndef WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3
#define WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3 0x00002000
#endif

static const wchar_t *USER_AGENT = L"PopShotUpdater/3.0";

typedef struct Sink {
    /* 三种形态：内存 / 文件+哈希 / 丢弃只数字节（测速探针）。 */
    char   *mem;
    size_t  mem_cap;
    size_t  mem_len;
    HANDLE  file;
    Sha256 *hash;
    int     discard;
    unsigned long long done;
    unsigned long long total;      /* 0 = 服务器没给长度 */
    net_progress_fn progress;
    void *user;
    int cancelled;
    /* 测速探针专用：deadline（GetTickCount64 绝对值，0 = 不设）一到，
       看门狗关句柄收工并置 expired；timeout_ms 是各阶段超时（0 = 默认
       10/10/30/30 秒）；cancel 是不带进度的取消探针。 */
    ULONGLONG deadline;
    unsigned timeout_ms;
    net_cancel_fn cancel;
    int expired;
    /* 测速探针的分格记录（可 NULL）：每收一块按到达时刻记进对应的格。 */
    NetTrace *trace;
    ULONGLONG trace_start;
} Sink;

/* 下载期取消节拍（用户拍板 0.5s 内）：WinHttpQueryDataAvailable 会一直
   堵到有数据 —— 慢链路/断流时取消没机会被检查。试过把接收超时压到
   0.5s，但 >0.5s 的正常到货间隙会把请求毒化（ReadData 回 12019），
   慢速真下载直接报错 —— 弃。改为看门狗线程：每 200ms 查一次取消，
   发现取消就主动 Close 请求句柄，把阻塞中的读解锁（<=0.5s 生效）。
   测速探针复用同一条狗：到 deadline 那一毫秒同样关句柄收工。 */
typedef struct NetWatch {
    HANDLE thread;
    HANDLE stop;                  /* net_fetch 收尾时叫停看门狗 */
    HINTERNET req;                /* 取消时要撬开的句柄（可为 NULL） */
    Sink *sink;                   /* 看门狗自己也按节拍跑进度回调 */
    volatile LONG req_closed;     /* 1 = 句柄已被看门狗关掉，主人别再关 */
} NetWatch;

static void net_watch_cut(NetWatch *w)
{
    InterlockedExchange(&w->req_closed, 1);
    WinHttpCloseHandle(w->req);       /* 撬开阻塞中的读 */
}

static DWORD WINAPI net_watch_dog(LPVOID param)
{
    NetWatch *w = (NetWatch *)param;
    Sink *s = w->sink;
    for (;;) {
        DWORD wait = 200;
        if (s->deadline) {
            ULONGLONG now = GetTickCount64();
            if (now >= s->deadline) {
                s->expired = 1;
                net_watch_cut(w);
                return 0;
            }
            if (s->deadline - now < wait) wait = (DWORD)(s->deadline - now);
        }
        if (WaitForSingleObject(w->stop, wait) != WAIT_TIMEOUT) return 0;
        if ((s->progress && !s->progress(s->user, s->done, s->total)) ||
            (s->cancel && s->cancel())) {
            s->cancelled = 1;
            net_watch_cut(w);
            return 0;
        }
    }
}

static int sink_open_mem(Sink *s, char *buf, size_t cap)
{
    memset(s, 0, sizeof(*s));
    s->mem = buf; s->mem_cap = cap; s->mem_len = 0;
    return 1;
}

static int sink_open_file(Sink *s, const wchar_t *dest, Sha256 *hash,
                          net_progress_fn progress, void *user)
{
    memset(s, 0, sizeof(*s));
    s->file = CreateFileW(dest, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                          FILE_ATTRIBUTE_NORMAL, NULL);
    if (s->file == INVALID_HANDLE_VALUE) return 0;
    s->hash = hash;
    s->progress = progress;
    s->user = user;
    return 1;
}

static int sink_write(Sink *s, const void *data, DWORD len)
{
    if (s->discard) {
        /* 测速探针：只数字节，什么都不存；按到达时刻记进分格。 */
        if (s->trace && s->trace->bucket_ms) {
            ULONGLONG idx = (GetTickCount64() - s->trace_start) / s->trace->bucket_ms;
            if (idx >= (ULONGLONG)s->trace->nbuckets)
                idx = (ULONGLONG)s->trace->nbuckets - 1;   /* 卡在到点那一刻的 */
            s->trace->bucket[idx] += len;
        }
    } else if (s->mem) {
        if (s->mem_len + len > s->mem_cap) return 0;
        memcpy(s->mem + s->mem_len, data, len);
        s->mem_len += len;
    } else {
        DWORD wrote = 0;
        if (!WriteFile(s->file, data, len, &wrote, NULL) || wrote != len)
            return 0;
        if (s->hash && !sha256_update(s->hash, data, len)) return 0;
    }
    s->done += len;
    if (s->progress && !s->progress(s->user, s->done, s->total))
        s->cancelled = 1;
    return 1;
}

static void set_err(wchar_t *err_out, size_t err_cap, const wchar_t *fmt, ...)
{
    if (!err_out || !err_cap) return;
    {
        va_list ap;
        va_start(ap, fmt);
        _vsnwprintf(err_out, err_cap, fmt, ap);
        err_out[err_cap - 1] = 0;
        va_end(ap);
    }
}

/* 读循环里的失败：先分清是不是看门狗动的手（取消 / 到点），再当真错误。
   ★ 紧跟失败的那个 WinHttp 调用之后调，中间别插别的 Win32 调用
   （GetLastError 要还是它的）。 */
static void read_failed(const Sink *s, wchar_t *err_out, size_t err_cap,
                        const wchar_t *what)
{
    DWORD code = GetLastError();
    if (s->cancelled)     set_err(err_out, err_cap, L"cancelled");
    else if (s->expired)  set_err(err_out, err_cap, L"expired");
    else                  set_err(err_out, err_cap, L"%ls (%lu)", what, code);
}

#define READ_CHUNK (1 << 20)

/* 公共下载管线：把 url 的内容整段读进 sink。
   total_out 拿服务器给的 Content-Length（0=没给）。 */
static int net_fetch(const wchar_t *url, Sink *s, unsigned long long *total_out,
                     wchar_t *err_out, size_t err_cap)
{
    URL_COMPONENTSW uc;
    wchar_t host[256];
    wchar_t path[1024];
    HINTERNET hnet = NULL, hconn = NULL, hreq = NULL;
    NetWatch watch;
    DWORD secure = 0;
    BOOL ok;
    DWORD status = 0, status_size = sizeof(status);
    unsigned long long total = 0;
    int result = 0;
    unsigned char *buf = NULL;       /* ★ 每次调用自己的读缓冲：几路测速探针
                                        并行跑，共用 static 缓冲会互相踩 */

    memset(&watch, 0, sizeof(watch));

    *total_out = 0;
    memset(&uc, 0, sizeof(uc));
    uc.dwStructSize = sizeof(uc);
    uc.lpszHostName = host;   uc.dwHostNameLength = 256;
    uc.lpszUrlPath = path;    uc.dwUrlPathLength = 1024;
    uc.nScheme = (INTERNET_SCHEME)-1;
    if (!WinHttpCrackUrl(url, 0, 0, &uc)) {
        set_err(err_out, err_cap, L"URL 解析失败 (%lu)", GetLastError());
        return 0;
    }

    hnet = WinHttpOpen(USER_AGENT, WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                       WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!hnet) {
        set_err(err_out, err_cap, L"WinHttpOpen 失败 (%lu)", GetLastError());
        return 0;
    }
    if (s->timeout_ms)
        WinHttpSetTimeouts(hnet, (int)s->timeout_ms, (int)s->timeout_ms,
                           (int)s->timeout_ms, (int)s->timeout_ms);
    else
        WinHttpSetTimeouts(hnet, 10000, 10000, 30000, 30000);

    hconn = WinHttpConnect(hnet, host,
                           (INTERNET_PORT)uc.nPort, 0);
    if (!hconn) {
        set_err(err_out, err_cap, L"连不上 %ls (%lu)", host, GetLastError());
        goto done;
    }
    hreq = WinHttpOpenRequest(hconn, L"GET", path, NULL, WINHTTP_NO_REFERER,
                              WINHTTP_DEFAULT_ACCEPT_TYPES,
                              uc.nScheme == INTERNET_SCHEME_HTTPS
                                  ? WINHTTP_FLAG_SECURE : 0);
    if (!hreq) {
        set_err(err_out, err_cap, L"WinHttpOpenRequest 失败 (%lu)", GetLastError());
        goto done;
    }

    /* 尽力开 TLS1.1/1.2/1.3：Win7 老版 winhttp 不认全部位也没关系，
       能设多少是多少（GitHub 只讲 TLS1.2+）。 */
    secure = WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_1 |
             WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2 |
             WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3;
    WinHttpSetOption(hreq, WINHTTP_OPTION_SECURE_PROTOCOLS,
                     &secure, sizeof(secure));

    if (!WinHttpSendRequest(hreq, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
                            WINHTTP_NO_REQUEST_DATA, 0, 0, 0) ||
        !WinHttpReceiveResponse(hreq, NULL)) {
        set_err(err_out, err_cap, L"网络请求失败（%ls）(%lu)",
                secure ? L"HTTPS" : L"HTTP", GetLastError());
        goto done;
    }

    ok = WinHttpQueryHeaders(hreq,
                             WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                             WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size,
                             WINHTTP_NO_HEADER_INDEX);
    if (!ok || status != 200) {
        set_err(err_out, err_cap, L"服务器返回状态码 %lu", ok ? status : 0);
        goto done;
    }

    {
        DWORD clen = 0, clen_size = sizeof(clen);
        if (WinHttpQueryHeaders(hreq, WINHTTP_QUERY_CONTENT_LENGTH |
                                 WINHTTP_QUERY_FLAG_NUMBER,
                                 WINHTTP_HEADER_NAME_BY_INDEX,
                                 &clen, &clen_size, WINHTTP_NO_HEADER_INDEX))
            total = clen;
    }
    *total_out = total;
    s->total = total;
    if (s->progress && total)
        s->progress(s->user, 0, total);       /* 先报一次总量，UI 能算百分比 */

    /* 看门狗陪「带进度回调的下载」（= 大文件、可取消）和测速探针
       （有 deadline / cancel）；manifest 这类小取用不着。 */
    if (s->progress || s->deadline || s->cancel) {
        watch.stop = CreateEventW(NULL, FALSE, FALSE, NULL);
        if (watch.stop) {
            watch.req = hreq;
            watch.sink = s;
            watch.req_closed = 0;
            watch.thread = CreateThread(NULL, 0, net_watch_dog, &watch, 0, NULL);
        }
    }

    buf = (unsigned char *)malloc(READ_CHUNK);
    if (!buf) {
        set_err(err_out, err_cap, L"内存不足");
        goto done;
    }
    for (;;) {
        DWORD got = 0;
        if (!WinHttpQueryDataAvailable(hreq, &got)) {
            read_failed(s, err_out, err_cap, L"读取数据失败");
            goto done;
        }
        if (!got) break;                      /* 流结束 */
        if (got > READ_CHUNK) got = READ_CHUNK;
        if (!WinHttpReadData(hreq, buf, got, &got)) {
            read_failed(s, err_out, err_cap, L"读取数据失败");
            goto done;
        }
        if (!got) break;
        if (!sink_write(s, buf, got)) {
            if (s->cancelled)
                set_err(err_out, err_cap, L"cancelled");
            else
                set_err(err_out, err_cap, L"写入本地文件失败（磁盘满？）");
            goto done;
        }
        if (s->cancelled || s->expired) {
            set_err(err_out, err_cap, s->cancelled ? L"cancelled" : L"expired");
            goto done;
        }
    }
    result = 1;

done:
    /* 先叫停看门狗并等它退场，再碰句柄 —— 它取消时关过 hreq，
       这里按 req_closed 分工，避免双重 Close。 */
    if (watch.thread) {
        SetEvent(watch.stop);
        WaitForSingleObject(watch.thread, 5000);
        CloseHandle(watch.thread);
    }
    if (watch.stop) CloseHandle(watch.stop);
    if (hreq && !watch.req_closed) WinHttpCloseHandle(hreq);
    if (hconn) WinHttpCloseHandle(hconn);
    if (hnet) WinHttpCloseHandle(hnet);
    free(buf);
    return result;
}

int net_get_memory(const wchar_t *url, char *buf, size_t cap, size_t *out_len,
                   unsigned window_ms, net_cancel_fn cancel,
                   wchar_t *err_out, size_t err_cap)
{
    Sink s;
    unsigned long long total;
    int ok;
    sink_open_mem(&s, buf, cap);
    if (window_ms) {
        s.timeout_ms = window_ms;
        s.deadline = GetTickCount64() + window_ms;   /* 窗口含建连 */
    }
    s.cancel = cancel;
    ok = net_fetch(url, &s, &total, err_out, err_cap);
    if (!ok) return 0;
    if (s.mem_len + 1 > cap) {
        set_err(err_out, err_cap, L"回应太大");
        return 0;
    }
    buf[s.mem_len] = 0;
    *out_len = s.mem_len;
    return 1;
}

int net_download_file(const wchar_t *url, const wchar_t *dest,
                      long long expected_size,
                      Sha256 *hash, net_progress_fn progress, void *user,
                      wchar_t *err_out, size_t err_cap)
{
    Sink s;
    unsigned long long total = 0;
    int ok;
    wchar_t reason[256];

    if (!sink_open_file(&s, dest, hash, progress, user)) {
        set_err(err_out, err_cap, L"建不了临时文件 %ls (%lu)", dest,
                GetLastError());
        return 0;
    }
    ok = net_fetch(url, &s, &total, reason, 256);
    if (s.file != INVALID_HANDLE_VALUE) CloseHandle(s.file);
    if (!ok) {
        DeleteFileW(dest);
        set_err(err_out, err_cap, L"%ls", reason);
        return 0;
    }
    if (expected_size >= 0 && (long long)s.done != expected_size) {
        wchar_t got[32], want[32];
        u64_to_wide(s.done, got, 32);
        u64_to_wide((unsigned long long)expected_size, want, 32);
        DeleteFileW(dest);
        set_err(err_out, err_cap,
                L"下载不完整（收到 %ls 字节，应为 %ls）", got, want);
        return 0;
    }
    return 1;
}

int net_probe_speed(const wchar_t *url, unsigned window_ms, unsigned bucket_ms,
                    net_cancel_fn cancel,
                    unsigned long long *bytes_out, unsigned *elapsed_out,
                    NetTrace *trace_out, wchar_t *note_out, size_t note_cap)
{
    Sink s;
    unsigned long long total = 0;
    wchar_t reason[256];
    ULONGLONG start = GetTickCount64();
    ULONGLONG elapsed;
    int ok;

    memset(&s, 0, sizeof(s));
    s.discard = 1;
    s.timeout_ms = window_ms;
    s.deadline = start + window_ms;      /* 窗口从建连前就开始算 */
    s.cancel = cancel;
    if (trace_out) {
        memset(trace_out, 0, sizeof(*trace_out));
        if (!bucket_ms) bucket_ms = 100;
        while (window_ms / bucket_ms + 1 > NET_TRACE_BUCKETS) bucket_ms *= 2;
        trace_out->bucket_ms = bucket_ms;
        trace_out->nbuckets = (int)(window_ms / bucket_ms) + 1;
        s.trace = trace_out;
        s.trace_start = start;
    }
    reason[0] = 0;
    ok = net_fetch(url, &s, &total, reason, 256);
    elapsed = GetTickCount64() - start;
    if (elapsed > window_ms) elapsed = window_ms;   /* 到点关句柄那几毫秒不算 */
    if (!elapsed) elapsed = 1;
    *bytes_out = s.done;
    *elapsed_out = (unsigned)elapsed;
    if (note_out && note_cap) {
        if (ok) set_err(note_out, note_cap, L"complete");
        else    set_err(note_out, note_cap, L"%ls", reason);
    }
    return s.cancelled ? 0 : 1;
}
