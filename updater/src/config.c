/* --------------------------------------------------------------------------
   config.c —— 见 config.h。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include <stdlib.h>
#include "config.h"

/* 读整个文件成宽文本（UTF-8/UTF-16 按 BOM 认，gb 兜底）。最多读 raw_cap
   字节（多的截掉）。返回长度或 -1。 */
static int read_text_file_n(const wchar_t *path, wchar_t *out, size_t cap,
                            size_t raw_cap)
{
    HANDLE f;
    unsigned char *raw;
    DWORD got = 0;
    int n = -1;

    f = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                    NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return -1;
    raw = (unsigned char *)malloc(raw_cap + 2);
    if (!raw) { CloseHandle(f); return -1; }
    if (!ReadFile(f, raw, (DWORD)raw_cap, &got, NULL)) {
        CloseHandle(f);
        free(raw);
        return -1;
    }
    CloseHandle(f);
    raw[got] = raw[got + 1] = 0;

    if (got >= 2 && raw[0] == 0xFF && raw[1] == 0xFE) {
        /* UTF-16LE：字节直拷（x86 小端）。 */
        size_t chars = got / 2;
        if (chars > cap - 1) chars = cap - 1;
        memcpy(out, raw + 2, chars * 2);
        out[chars] = 0;
        n = (int)chars;
    } else {
        /* UTF-8（BOM 有无都行）/ 其他按 UTF-8 尽力解。 */
        n = MultiByteToWideChar(CP_UTF8, 0, (const char *)raw, (int)got,
                                out, (int)(cap - 1));
        if (n <= 0) {
            /* ★★ 输出缓冲放不下时 MultiByteToWideChar 返回 **0**
               （ERROR_INSUFFICIENT_BUFFER）。原来这里一律当成读取失败，
               于是**整个文件读不出来**、调用方悄悄退回默认值 ——
               `config/server.config` 长到一定程度后，更新器就再也读不到
               `server_address`，探针一直去连默认的 192.168.1.100
               （2026-09-11 实测：日志里 `probe host 192.168.1.100:27799`）。
               后果是「探针问不到服务器要哪个版本」，成对发布（D079）失效，
               而且被拒的客户端会停在「已是最新版本，无需更新」。

               解法：把输入截短到「一定放得下」再解一次 —— UTF-8 里一个
               宽字符最少占 1 字节，所以 cap-1 个字节至多解出 cap-1 个宽字符。
               截断点要退到一个 UTF-8 起始字节上，别把一个汉字切两半。 */
            DWORD fit = got;
            if (fit > (DWORD)(cap - 1)) fit = (DWORD)(cap - 1);
            while (fit > 0 && (raw[fit] & 0xC0) == 0x80) fit--;
            n = MultiByteToWideChar(CP_UTF8, 0, (const char *)raw, (int)fit,
                                    out, (int)(cap - 1));
        }
        if (n <= 0) n = -1;
        else out[n] = 0;
    }
    free(raw);
    return n;
}

/* server.config / BUILD.ver 用这一条。
   ★ 原来只读前 8190 字节 —— `config/server.config` 已经长到 10 KB 以上，
   后半截掉不说，解出来的宽字符还放不进调用方的缓冲区。
   现在放到 64 KB；真超了也不会再整个读失败（见 read_text_file_n 里
   那段说明）。 */
static int read_text_file(const wchar_t *path, wchar_t *out, size_t cap)
{
    return read_text_file_n(path, out, cap, 65536);
}

int cfg_server_address(const wchar_t *root, wchar_t *out, size_t cap)
{
    wchar_t path[MAX_PATH * 2];
    static wchar_t text[32768];
    wchar_t *line, *next;
    int found = 0;

    path_join(path, MAX_PATH * 2, root, L"config/server.config");
    wcscpy(out, L"192.168.1.100");                       /* config.py 同款默认 */
    if (read_text_file(path, text, 32768) < 0) return 1;  /* 没有文件用默认 */

    line = text;
    while (line && *line) {
        wchar_t *eol = wcschr(line, L'\n');
        wchar_t *eq, *key, *val, *vend;
        if (eol) { next = eol + 1; *eol = 0; }
        else next = NULL;
        while (*line == L' ' || *line == L'\t' || *line == L'\r' ||
               *line == 0xFEFF) line++;
        if (!*line || *line == L'#' || *line == L';') { line = next; continue; }
        eq = wcschr(line, L'=');
        if (!eq || eq == line) { line = next; continue; }
        *eq = 0;
        key = line;
        while (*key == L' ') key++;
        vend = eq;
        while (vend > key && vend[-1] == L' ') vend--;
        *vend = 0;
        val = eq + 1;
        while (*val == L' ' || *val == L'\t') val++;
        {
            size_t n = wcslen(val);
            while (n && (val[n - 1] == L'\r' || val[n - 1] == L' ' ||
                         val[n - 1] == L'\t')) val[--n] = 0;
        }
        if (wide_ieq(key, L"server_address") && *val) {
            /* [IPv6] 去括号（launch.ps1 同款）。 */
            if (*val == L'[') {
                wchar_t *close = wcsrchr(val, L']');
                if (close) {
                    *close = 0;
                    val++;
                    while (*val == L' ') val++;
                }
            }
            wcsncpy(out, val, cap - 1);
            out[cap - 1] = 0;
            found = 1;
        }
        line = next;
    }
    return 1;
    (void)found;
}

int cfg_local_version(const wchar_t *root, Ver *out)
{
    wchar_t path[MAX_PATH * 2];
    static wchar_t text[8192];
    wchar_t *p;
    int n;

    path_join(path, MAX_PATH * 2, root, L"BUILD.ver");
    if (read_text_file(path, text, 8192) < 0) return 0;
    /* bshook 同款：只认第一个 "version" 键（BUILD.ver 是我们自己的脚本写的，
       version 永远第一个键；完整 JSON 解析没必要）。 */
    p = text;
    while ((p = wcsstr(p, L"\"version\"")) != NULL) {
        p += 9;
        while (*p == L' ' || *p == L'\t' || *p == L'\r' || *p == L'\n') p++;
        if (*p != L':') continue;
        p++;
        while (*p == L' ' || *p == L'\t') p++;
        if (*p != L'"') continue;
        p++;
        {
            wchar_t value[64];
            wchar_t *q = value;
            while (*p && *p != L'"' && q - value < 63) *q++ = *p++;
            *q = 0;
            n = ver_parse(value, out);
            return n;
        }
    }
    (void)n;
    return 0;
}

int cfg_root_writable(const wchar_t *root)
{
    wchar_t probe[MAX_PATH * 2];
    HANDLE f;
    path_join(probe, MAX_PATH * 2, root, L".update-write-test");
    f = CreateFileW(probe, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                    FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return 0;
    CloseHandle(f);
    DeleteFileW(probe);
    return 1;
}

/* ------------------------------------------------------------------ */
/*  config\update.config —— 下载加速代理列表                             */
/* ------------------------------------------------------------------ */

int cfg_parse_proxy_list(const wchar_t *text, ProxyList *out)
{
    const wchar_t *p = text;

    memset(out, 0, sizeof(*out));
    while (p && *p) {
        const wchar_t *eol = wcschr(p, L'\n');
        const wchar_t *end = eol ? eol : p + wcslen(p);
        const wchar_t *s = p;
        size_t len;
        wchar_t url[PROXY_URL_CAP];
        int i, dup = 0;

        p = eol ? eol + 1 : NULL;
        /* 去首尾空白（含 BOM、CR）。 */
        while (s < end && (*s == L' ' || *s == L'\t' || *s == L'\r' ||
                           *s == 0xFEFF)) s++;
        while (end > s && (end[-1] == L' ' || end[-1] == L'\t' ||
                           end[-1] == L'\r')) end--;
        if (s == end || *s == L'#' || *s == L';') continue;
        len = (size_t)(end - s);
        /* 只认 http(s):// 开头、长度合理、中间没有空白的一行。 */
        if (len >= PROXY_URL_CAP ||
            !((len > 7 && _wcsnicmp(s, L"http://", 7) == 0) ||
              (len > 8 && _wcsnicmp(s, L"https://", 8) == 0))) {
            out->skipped++;
            continue;
        }
        for (i = 0; i < (int)len; i++)
            if (s[i] == L' ' || s[i] == L'\t') break;
        if (i < (int)len) { out->skipped++; continue; }
        wcsncpy(url, s, len);
        url[len] = 0;
        while (len > 8 && url[len - 1] == L'/') url[--len] = 0;   /* 末尾 / */
        for (i = 0; i < out->count; i++)
            if (wide_ieq(out->url[i], url)) { dup = 1; break; }
        if (dup || out->count >= PROXY_MAX) { out->skipped++; continue; }
        wcscpy(out->url[out->count++], url);
    }
    return out->count;
}

int cfg_proxy_list(const wchar_t *root, ProxyList *out)
{
    wchar_t path[MAX_PATH * 2];
    static wchar_t text[16384];          /* 只在 worker 线程里用，且只用一次 */

    memset(out, 0, sizeof(*out));
    path_join(path, MAX_PATH * 2, root, L"config/update.config");
    if (read_text_file_n(path, text, 16384, 65536) < 0)
        return 0;                        /* 没有文件 = 只用直连 */
    cfg_parse_proxy_list(text, out);
    return 1;
}
