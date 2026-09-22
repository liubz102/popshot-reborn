/* --------------------------------------------------------------------------
   util.c —— 见 util.h。版本号语义必须和 server\versioning.py 一致
   （selftest.c 钉住关键向量）。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <shellapi.h>
#include <shlwapi.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include <stdlib.h>
#include "util.h"

/* ------------------------------------------------------------------ */
/*  版本号（server\versioning.py 的 C 镜像）                            */
/* ------------------------------------------------------------------ */

int ver_parse(const wchar_t *text, Ver *out)
{
    const wchar_t *p = text;
    int numbers[3];
    int count = 0;

    if (!text) return 0;
    while (*p) {
        const wchar_t *line = p;
        const wchar_t *e;
        /* 找行尾 */
        while (*p && *p != L'\n') p++;
        e = p;
        if (*p == L'\n') p++;
        /* strip：去首尾空白 + BOM + \r */
        while (line < e && (*line == L' ' || *line == L'\t' ||
                            *line == 0xFEFF || *line == L'\r')) line++;
        while (e > line && (e[-1] == L' ' || e[-1] == L'\t' || e[-1] == L'\r')) e--;
        if (line == e) continue;
        if (line[0] == L'#' || line[0] == L';') continue;
        if (line[0] == L'v' || line[0] == L'V') {
            line++;
            while (line < e && *line == L' ') line++;
        }
        /* 拆 1~3 段纯数字 */
        count = 0;
        {
            const wchar_t *seg = line;
            for (;;) {
                const wchar_t *q = seg;
                int value = 0, digits = 0;
                while (q < e && *q >= L'0' && *q <= L'9') {
                    value = value * 10 + (*q - L'0');
                    q++; digits++;
                }
                if (!digits) return 0;              /* 空段/非数字 */
                if (count < 3) numbers[count] = value;
                count++;
                if (q >= e) break;
                if (*q != L'.') return 0;
                seg = q + 1;
                if (seg >= e) return 0;             /* 尾部带点的空段 */
            }
        }
        if (count < 1 || count > 3) return 0;
        while (count < 3) numbers[count++] = 0;
        if (numbers[0] > 2146 || numbers[1] > 999 || numbers[2] > 999) return 0;
        out->major = numbers[0];
        out->minor = numbers[1];
        out->patch = numbers[2];
        return 1;
    }
    return 0;
}

void ver_format(const Ver *v, wchar_t *out, size_t cap)
{
    if (!v) { wcsncpy(out, L"?", cap); out[cap - 1] = 0; return; }
    _snwprintf(out, cap, L"V%d.%d.%d", v->major, v->minor, v->patch);
    out[cap - 1] = 0;
}

int ver_cmp(const Ver *a, const Ver *b)
{
    if (a->major != b->major) return a->major < b->major ? -1 : 1;
    if (a->minor != b->minor) return a->minor < b->minor ? -1 : 1;
    if (a->patch != b->patch) return a->patch < b->patch ? -1 : 1;
    return 0;
}

long ver_encode_wire(const Ver *v)
{
    long wire;
    if (!v) return -1;
    if (v->major > 2146 || v->minor > 999 || v->patch > 999) return -1;
    wire = v->major * 1000000L + v->minor * 1000L + v->patch;
    if (wire == 311) return -1;        /* 原版保留值（0.0.311） */
    if (wire < 1000) return -1;        /* 与原版客户端小数字区间混淆 */
    return wire;
}

/* ------------------------------------------------------------------ */
/*  编码转换                                                            */
/* ------------------------------------------------------------------ */

int gbk_to_wide(const char *src, size_t srclen, wchar_t *dst, size_t cap)
{
    int n = MultiByteToWideChar(936, 0, src, (int)srclen, dst, (int)(cap - 1));
    if (n <= 0) return -1;
    dst[n] = 0;
    return n;
}

int wide_to_gbk(const wchar_t *src, size_t srclen, char *dst, size_t cap)
{
    int n = WideCharToMultiByte(936, 0, src, (int)srclen, dst, (int)(cap - 1),
                                NULL, NULL);
    if (n <= 0) return -1;
    dst[n] = 0;
    return n;
}

int wide_to_utf8(const wchar_t *src, char *dst, size_t cap)
{
    int n = WideCharToMultiByte(CP_UTF8, 0, src, -1, dst, (int)cap,
                                NULL, NULL);
    return n > 0 ? n - 1 : -1;
}

int utf8_to_wide(const char *src, size_t srclen, wchar_t *dst, size_t cap)
{
    int n;
    size_t fit = srclen;

    if (cap == 0) return -1;
    /* UTF-8 BOM 吃掉（服务器那头原样回文件字节，文件可能带 BOM）。 */
    if (srclen >= 3 && (unsigned char)src[0] == 0xEF &&
        (unsigned char)src[1] == 0xBB && (unsigned char)src[2] == 0xBF) {
        src += 3;
        fit = srclen - 3;
    }
    n = MultiByteToWideChar(CP_UTF8, 0, src, (int)fit, dst, (int)(cap - 1));
    if (n <= 0) {
        /* ★ 输出缓冲放不下时 MultiByteToWideChar 返回 **0**，不是负数
           —— 一律当成失败的话整份内容就悄悄没了（config.c 那边为这个
           栽过一次：server.config 一长，server_address 就读不出来）。
           UTF-8 里一个宽字符最少占 1 字节 ⇒ 截到 cap-1 字节一定放得下；
           截断点要退回一个 UTF-8 起始字节上，别把一个汉字切两半。 */
        if (fit > cap - 1) fit = cap - 1;
        while (fit > 0 && ((unsigned char)src[fit] & 0xC0) == 0x80) fit--;
        n = MultiByteToWideChar(CP_UTF8, 0, src, (int)fit, dst, (int)(cap - 1));
    }
    if (n <= 0) return -1;
    dst[n] = 0;
    return n;
}

void manifest_repo_label(const wchar_t *url, wchar_t *out, size_t cap)
{
    const wchar_t *p = url ? url : L"";
    const wchar_t *scheme;
    size_t n = 0, keep;
    int slashes = 0;
    int max_slashes = 2;          /* 域名 + 两段路径 = 停在第 3 个 / */

    if (cap == 0) return;
    out[0] = 0;
    /* 去掉 scheme 和 www.，只留「域名 + 前两段路径」；域名正好是 github.com
       时连它一起去掉，只剩 owner/repo：
         https://github.com/liubz102/popshot-reborn/releases/latest/download/manifest.json
           -> liubz102/popshot-reborn
         https://oss.example.com/pkg/manifest.json
           -> oss.example.com/pkg/manifest.json
       ★ 为什么 github.com 特殊：状态行只有两行 455px，下载那一行还要塞
         代理地址，省下这 11 个字符是实打实的余量；而**别的**域名必须留着
         —— 不然玩家看不出这包到底是不是从 GitHub 下的。
         判据是「整段域名就等于 github.com」，`github.com.evil.tld` 不匹配。
       ★ 这一行纯给人看（界面上的「仓库：」），不参与任何判断：
         认不出的地址原样截断就行，绝不能因为它出错。 */
    scheme = wcsstr(p, L"://");
    if (scheme) p = scheme + 3;
    if (_wcsnicmp(p, L"www.", 4) == 0) p += 4;
    if (_wcsnicmp(p, L"github.com/", 11) == 0) {
        p += 11;
        max_slashes = 1;          /* 域名没了，owner/repo 两段 = 停在第 2 个 / */
    }
    while (p[n] && !(p[n] == L'/' && ++slashes > max_slashes)) n++;
    /* 太长就截断加省略号（最长的也就 GitHub 那种 owner/repo，够用）。 */
    keep = cap - 1;
    if (keep > 40) keep = 40;
    if (n <= keep) {
        wcsncpy(out, p, n);
        out[n] = 0;
    } else {
        wcsncpy(out, p, keep - 1);
        out[keep - 1] = 0x2026;          /* … */
        out[keep] = 0;
    }
}

/* ------------------------------------------------------------------ */
/*  路径                                                                */
/* ------------------------------------------------------------------ */

void path_join(wchar_t *out, size_t cap, const wchar_t *a, const wchar_t *b)
{
    _snwprintf(out, cap, L"%s\\%s", a, b);
    out[cap - 1] = 0;
}

int file_exists(const wchar_t *path)
{
    return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}

int ensure_dir(const wchar_t *path)
{
    wchar_t buf[MAX_PATH * 2];
    wchar_t *p;
    wcsncpy(buf, path, MAX_PATH * 2 - 1);
    buf[MAX_PATH * 2 - 1] = 0;
    /* 逐级建：把每个 \\ 临时断开 */
    p = buf;
    while (*p) {
        if (*p == L'\\' && p > buf + 2) {
            *p = 0;
            if (!CreateDirectoryW(buf, NULL) &&
                GetLastError() != ERROR_ALREADY_EXISTS)
                return 0;
            *p = L'\\';
        }
        p++;
    }
    if (!CreateDirectoryW(buf, NULL) && GetLastError() != ERROR_ALREADY_EXISTS)
        return 0;
    return 1;
}

int delete_tree(const wchar_t *path)
{
    /* Shlwapi 的递归删除（shell 路径版，不带 SHFileOperation 的确认框）。 */
    int n = (int)wcslen(path);
    wchar_t *buf = (wchar_t *)malloc((n + 2) * sizeof(wchar_t));
    SHFILEOPSTRUCTW op;
    int rc;
    if (!buf) return -1;
    wcscpy(buf, path);
    buf[n + 1] = 0;                       /* 双 NUL 终止 */
    ZeroMemory(&op, sizeof(op));
    op.wFunc = FO_DELETE;
    op.pFrom = buf;
    op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI |
                FOF_NOCONFIRMMKDIR;
    rc = SHFileOperationW(&op);
    free(buf);
    return rc;
}

void slashes_to_back(wchar_t *path)
{
    for (; *path; path++)
        if (*path == L'/') *path = L'\\';
}

int wide_ieq(const wchar_t *a, const wchar_t *b)
{
    return lstrcmpiW(a, b) == 0;
}

void module_path(wchar_t *out, size_t cap)
{
    GetModuleFileNameW(NULL, out, (DWORD)cap);
}

void package_root(wchar_t *out, size_t cap)
{
    wchar_t *p;
    GetModuleFileNameW(NULL, out, (DWORD)cap);
    p = wcsrchr(out, L'\\'); if (p) *p = 0;    /* -> <root>\game_patched */
    p = wcsrchr(out, L'\\'); if (p) *p = 0;    /* -> <root>             */
}

/* ------------------------------------------------------------------ */
/*  其他                                                                */
/* ------------------------------------------------------------------ */

void file_url_from_path(const wchar_t *path, wchar_t *out, size_t cap)
{
    /* file:/// + 每个字节：ASCII 安全字符直出，其余按 UTF-8 %XX。
       冒号放行：盘符 C: 的那个冒号必须保持原样。 */
    static const wchar_t *safe =
        L"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        L"-_.~/:";
    wchar_t *o = out;
    const wchar_t *i = path;
    const wchar_t *prefix = L"file:///";

    wcsncpy(o, prefix, cap - 1);
    o += wcslen(prefix);
    while (*i && (size_t)(o - out) < cap - 8) {
        if (*i == L'\\') { *o++ = L'/'; i++; continue; }
        if (wcschr(safe, *i)) { *o++ = *i++; continue; }
        {
            /* 非 ASCII/保留字符 -> UTF-8 多字节再 %XX */
            char utf8[8];
            int n = WideCharToMultiByte(CP_UTF8, 0, i, 1, utf8, sizeof(utf8),
                                        NULL, NULL);
            int k;
            for (k = 0; k < n && n > 0; k++) {
                _snwprintf(o, 4, L"%%%02X", (unsigned char)utf8[k]);
                o += 3;
            }
            i++;
        }
    }
    *o = 0;
    out[cap - 1] = 0;
}

static const char B64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

int base64_encode(const unsigned char *src, size_t len, char *dst, size_t cap)
{
    size_t need = ((len + 2) / 3) * 4 + 1;
    size_t i;
    char *o = dst;
    if (cap < need) return -1;
    for (i = 0; i + 2 < len; i += 3) {
        unsigned v = (src[i] << 16) | (src[i + 1] << 8) | src[i + 2];
        *o++ = B64[(v >> 18) & 63];
        *o++ = B64[(v >> 12) & 63];
        *o++ = B64[(v >> 6) & 63];
        *o++ = B64[v & 63];
    }
    if (i < len) {
        unsigned v = src[i] << 16;
        int pad = 1;
        if (i + 1 < len) { v |= src[i + 1] << 8; pad = 2; }
        *o++ = B64[(v >> 18) & 63];
        *o++ = B64[(v >> 12) & 63];
        *o++ = pad == 2 ? B64[(v >> 6) & 63] : '=';
        *o++ = '=';
    }
    *o = 0;
    return (int)(o - dst);
}

void u64_to_wide(unsigned long long v, wchar_t *out, size_t cap)
{
    _snwprintf(out, cap, L"%llu", v);
    out[cap - 1] = 0;
}

void mib_to_wide(unsigned long long bytes, wchar_t *out, size_t cap)
{
    unsigned long long mib_x10 = bytes * 10 / (1ULL << 20);
    _snwprintf(out, cap, L"%llu.%llu", mib_x10 / 10, mib_x10 % 10);
    out[cap - 1] = 0;
}

/* 本机的 UTC 偏移（`UTC+8` / `UTC-3` / `UTC+5:30`）。
   本地时刻减 UTC 时刻现算，不读注册表 —— 启动那一刻的夏令时自动就对。
   和 `hook/bshook.c` 的 `bslog_zone_minutes()`、`server/tzstamp.py` 同一套写法。

   ★★ **偏移只算一次，之后一直沿用**（用户 2026-09-20 第二轮拍板，三侧统一）。
     玩家全在中国（UTC+8，不用夏令时），为「更新器跑着的时候跨过夏令时切换」
     这个本项目里不存在的场景每行现算一遍不值当。
     ★ 落定的是**分钟数**不是字符串：调用方各自拿自己的 `cap` 去拼，
       省一份静态缓冲区，也不用担心谁把它写坏。
     ★ 多线程：两条线程算出的是同一个值，撞了也无害；**先写值、后写旗**。 */
static long g_zone_mins = 0;
static int  g_zone_ready = 0;        /* 0 = 还没算过 */

void utc_offset_text(wchar_t *out, size_t cap)
{
    SYSTEMTIME lt, ut;
    FILETIME lf, uf;
    long long diff, half;
    long mins, hh, mm;
    wchar_t sign;

    if (cap) out[0] = 0;
    if (!g_zone_ready) {
        GetLocalTime(&lt);
        GetSystemTime(&ut);
        if (!SystemTimeToFileTime(&lt, &lf) || !SystemTimeToFileTime(&ut, &uf))
            return;                     /* 算不出就先不落定，下一次再试 */
        diff = (((long long)lf.dwHighDateTime << 32) | lf.dwLowDateTime)
             - (((long long)uf.dwHighDateTime << 32) | uf.dwLowDateTime);
        half = diff >= 0 ? 300000000LL : -300000000LL;  /* 100ns -> 分钟，就近 */
        g_zone_mins = (long)((diff + half) / 600000000LL);
        g_zone_ready = 1;                               /* 先值后旗 */
    }
    mins = g_zone_mins;
    sign = mins < 0 ? L'-' : L'+';
    if (mins < 0) mins = -mins;
    hh = mins / 60;
    mm = mins % 60;
    if (mm) _snwprintf(out, cap, L"UTC%c%ld:%02ld", sign, hh, mm);
    else    _snwprintf(out, cap, L"UTC%c%ld", sign, hh);
    out[cap - 1] = 0;
}

/* ★ 后面那个 `UTC+8` 是 2026-09-20 加的：更新器的日志会和玩家机器上的崩溃包、
   开发机上的打包戳摆在一起看，三台机器三个时区，不写出来会比反
   （bug调查/25，`server/tzstamp.py` 的文件头记了那次踩坑）。 */
void now_stamp(wchar_t *out, size_t cap)
{
    SYSTEMTIME t;
    wchar_t zone[16];
    GetLocalTime(&t);
    utc_offset_text(zone, 16);
    _snwprintf(out, cap, L"%04u-%02u-%02u %02u:%02u:%02u %s",
               t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond, zone);
    out[cap - 1] = 0;
}
