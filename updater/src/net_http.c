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
    /* 两种形态：内存 or 文件+哈希。 */
    char   *mem;
    size_t  mem_cap;
    size_t  mem_len;
    HANDLE  file;
    Sha256 *hash;
    unsigned long long done;
    unsigned long long total;      /* 0 = 服务器没给长度 */
    net_progress_fn progress;
    void *user;
    int cancelled;
} Sink;

/* 下载期取消节拍（用户拍板 0.5s 内）：WinHttpQueryDataAvailable 会一直
   堵到有数据 —— 慢链路/断流时取消没机会被检查。试过把接收超时压到
   0.5s，但 >0.5s 的正常到货间隙会把请求毒化（ReadData 回 12019），
   慢速真下载直接报错 —— 弃。改为看门狗线程：每 200ms 查一次取消，
   发现取消就主动 Close 请求句柄，把阻塞中的读解锁（<=0.5s 生效）。 */
typedef struct NetWatch {
    HANDLE thread;
    HANDLE stop;                  /* net_fetch 收尾时叫停看门狗 */
    HINTERNET req;                /* 取消时要撬开的句柄（可为 NULL） */
    Sink *sink;                   /* 看门狗自己也按节拍跑进度回调 */
    volatile LONG req_closed;     /* 1 = 句柄已被看门狗关掉，主人别再关 */
} NetWatch;

static DWORD WINAPI net_watch_dog(LPVOID param)
{
    NetWatch *w = (NetWatch *)param;
    while (WaitForSingleObject(w->stop, 200) == WAIT_TIMEOUT) {
        if (w->sink->progress &&
            !w->sink->progress(w->sink->user, w->sink->done, w->sink->total)) {
            w->sink->cancelled = 1;
            InterlockedExchange(&w->req_closed, 1);
            WinHttpCloseHandle(w->req);   /* 撬开阻塞中的读 */
            return 0;
        }
    }
    return 0;
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
    if (s->mem) {
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

    /* 看门狗只陪「带进度回调的下载」（= 大文件、可取消）；manifest 这类
       小取（progress == NULL）用不着。 */
    if (s->progress) {
        watch.stop = CreateEventW(NULL, FALSE, FALSE, NULL);
        if (watch.stop) {
            watch.req = hreq;
            watch.sink = s;
            watch.req_closed = 0;
            watch.thread = CreateThread(NULL, 0, net_watch_dog, &watch, 0, NULL);
        }
    }

    for (;;) {
        DWORD got = 0;
        static unsigned char buf[1 << 20];
        if (!WinHttpQueryDataAvailable(hreq, &got)) {
            if (s->cancelled) {
                set_err(err_out, err_cap, L"cancelled");
                goto done;
            }
            set_err(err_out, err_cap, L"读取数据失败 (%lu)", GetLastError());
            goto done;
        }
        if (!got) break;                      /* 流结束 */
        if (got > sizeof(buf)) got = sizeof(buf);
        if (!WinHttpReadData(hreq, buf, got, &got)) {
            if (s->cancelled) {
                set_err(err_out, err_cap, L"cancelled");
                goto done;
            }
            set_err(err_out, err_cap, L"读取数据失败 (%lu)", GetLastError());
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
        if (s->cancelled) {
            set_err(err_out, err_cap, L"cancelled");
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
    return result;
}

int net_get_memory(const wchar_t *url, char *buf, size_t cap, size_t *out_len,
                   wchar_t *err_out, size_t err_cap)
{
    Sink s;
    unsigned long long total;
    int ok;
    sink_open_mem(&s, buf, cap);
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
