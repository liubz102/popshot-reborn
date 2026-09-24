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
#include "ports.h"

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

/* server.config 的 key = value 扫描：# / ; 注释、BOM、CR、键名大小写不敏感，
   同一个键出现多次以**最后一次**为准（沿用改动前的行为）。
   找到返回 1 并把值（已去首尾空白）写进 out；没有这一行返回 0。

   ★ 一次只找一个键 —— 调用方要读两个键就读两遍文件。这个文件十来 KB，
   更新器一辈子只读这两下；换成「一趟扫多键」要多一个状态机，不值当。 */
static int read_server_key(const wchar_t *root, const wchar_t *want,
                           wchar_t *out, size_t cap)
{
    wchar_t path[MAX_PATH * 2];
    static wchar_t text[32768];
    wchar_t *line, *next;
    int found = 0;

    path_join(path, MAX_PATH * 2, root, L"config/server.config");
    if (read_text_file(path, text, 32768) < 0) return 0;  /* 没有文件 */

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
        if (wide_ieq(key, want) && *val) {
            wcsncpy(out, val, cap - 1);
            out[cap - 1] = 0;
            found = 1;
        }
        line = next;
    }
    return found;
}

int cfg_server_address(const wchar_t *root, wchar_t *out, size_t cap)
{
    wchar_t val[256];
    wchar_t *p = val;

    wcscpy(out, L"192.168.1.100");                       /* config.py 同款默认 */
    if (!read_server_key(root, L"server_address", val, 256)) return 1;
    /* [IPv6] 去括号（launch.ps1 同款）。 */
    if (*p == L'[') {
        wchar_t *close = wcsrchr(p, L']');
        if (close) {
            *close = 0;
            p++;
            while (*p == L' ') p++;
        }
    }
    if (*p) {
        wcsncpy(out, p, cap - 1);
        out[cap - 1] = 0;
    }
    return 1;
}

int cfg_server_register_port(const wchar_t *root)
{
    wchar_t val[64];
    int port;

    /* ★ 认不出一律回缺省，绝不返回 0 或负数：这一项只决定「问不问得到
       服务器上的 update.config」，问不到会自己退回本地那份（fail-open，
       和 server.config 整体的哲学一致）。 */
    if (!read_server_key(root, L"server_register_port", val, 64))
        return POPSHOT_DEFAULT_REGISTER_PORT;
    port = _wtoi(val);
    if (port <= 0 || port > 65535) return POPSHOT_DEFAULT_REGISTER_PORT;
    return port;
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
/*  config\update.config —— 更新源（manifest 地址 + 下载加速代理列表）    */
/* ------------------------------------------------------------------ */

/* 一行 `key = value`。返回 1 = 这一行确实是 key=value 形式（认不认得这个键
   另说：认得就存下，不认得按老规矩计进 skipped）。

   ★ 这一判定必须排在下面 take_proxy 的 http:// 判定**之前**。反过来的话
   `manifest_url = https://…` 会因为「中间有空白」被当成一行认不出的代理
   —— 那正是**老更新器**的行为，也正是新格式对老版本安全的原因：老版本
   只会把这一行丢进 skipped，绝不会把它当成一个代理地址去用。 */
static int take_key_value(const wchar_t *line, UpdateConfig *out)
{
    const wchar_t *eq = wcschr(line, L'=');
    const wchar_t *kend, *val;
    wchar_t key[64];
    size_t klen, i;

    if (!eq || eq == line) return 0;
    kend = eq;
    while (kend > line && (kend[-1] == L' ' || kend[-1] == L'\t')) kend--;
    klen = (size_t)(kend - line);
    if (klen == 0 || klen >= sizeof(key) / sizeof(key[0])) return 0;
    /* 键名里不许有空白，也不许有 : 或 / —— 那些是地址的长相。不挡的话
       `http://host/a=b` 这种带 = 的代理地址会被误判成一行配置。 */
    for (i = 0; i < klen; i++) {
        wchar_t c = line[i];
        if (c == L' ' || c == L'\t' || c == L':' || c == L'/') return 0;
    }
    memcpy(key, line, klen * sizeof(wchar_t));
    key[klen] = 0;

    val = eq + 1;
    while (*val == L' ' || *val == L'\t') val++;
    if (wide_ieq(key, L"manifest_url")) {
        wcsncpy(out->manifest_url, val, MANIFEST_URL_CAP - 1);
        out->manifest_url[MANIFEST_URL_CAP - 1] = 0;
    } else {
        out->proxies.skipped++;          /* 认不出的键，和从前一样只是忽略 */
    }
    return 1;
}

/* 一行代理地址。规则和改动前逐条一致（selftest 的 proxylist_tests 钉着）。 */
static void take_proxy(const wchar_t *s, size_t len, ProxyList *out)
{
    wchar_t url[PROXY_URL_CAP];
    int i, dup = 0;

    /* 只认 http(s):// 开头、长度合理、中间没有空白的一行。 */
    if (len >= PROXY_URL_CAP ||
        !((len > 7 && _wcsnicmp(s, L"http://", 7) == 0) ||
          (len > 8 && _wcsnicmp(s, L"https://", 8) == 0))) {
        out->skipped++;
        return;
    }
    for (i = 0; i < (int)len; i++)
        if (s[i] == L' ' || s[i] == L'\t') break;
    if (i < (int)len) { out->skipped++; return; }
    wcsncpy(url, s, len);
    url[len] = 0;
    while (len > 8 && url[len - 1] == L'/') url[--len] = 0;   /* 末尾 / */
    for (i = 0; i < out->count; i++)
        if (wide_ieq(out->url[i], url)) { dup = 1; break; }
    if (dup || out->count >= PROXY_MAX) { out->skipped++; return; }
    wcscpy(out->url[out->count++], url);
}

int cfg_parse_update_config(const wchar_t *text, UpdateConfig *out)
{
    const wchar_t *p = text;

    memset(out, 0, sizeof(*out));
    while (p && *p) {
        const wchar_t *eol = wcschr(p, L'\n');
        const wchar_t *end = eol ? eol : p + wcslen(p);
        const wchar_t *s = p;
        wchar_t line[MANIFEST_URL_CAP + 128];
        size_t len;

        p = eol ? eol + 1 : NULL;
        /* 去首尾空白（含 BOM、CR）。 */
        while (s < end && (*s == L' ' || *s == L'\t' || *s == L'\r' ||
                           *s == 0xFEFF)) s++;
        while (end > s && (end[-1] == L' ' || end[-1] == L'\t' ||
                           end[-1] == L'\r')) end--;
        if (s == end || *s == L'#' || *s == L';') continue;
        len = (size_t)(end - s);
        if (len >= sizeof(line) / sizeof(line[0])) {
            out->proxies.skipped++;      /* 长得离谱的一行，不看了 */
            continue;
        }
        memcpy(line, s, len * sizeof(wchar_t));
        line[len] = 0;
        if (take_key_value(line, out)) continue;
        take_proxy(line, len, &out->proxies);
    }
    return out->proxies.count;
}

int cfg_update_config(const wchar_t *root, UpdateConfig *out)
{
    wchar_t path[MAX_PATH * 2];
    static wchar_t text[16384];          /* 只在 worker 线程里用，且只用一次 */

    memset(out, 0, sizeof(*out));
    path_join(path, MAX_PATH * 2, root, L"config/update.config");
    if (read_text_file_n(path, text, 16384, 65536) < 0)
        return 0;                        /* 没有文件 = 只用直连 + 内置地址 */
    cfg_parse_update_config(text, out);
    return 1;
}

int cfg_parse_proxy_list(const wchar_t *text, ProxyList *out)
{
    UpdateConfig uc;
    cfg_parse_update_config(text, &uc);
    *out = uc.proxies;
    return out->count;
}

int cfg_proxy_list(const wchar_t *root, ProxyList *out)
{
    UpdateConfig uc;
    int got = cfg_update_config(root, &uc);
    *out = uc.proxies;
    return got;
}
