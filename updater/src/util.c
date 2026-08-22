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

void now_stamp(wchar_t *out, size_t cap)
{
    SYSTEMTIME t;
    GetLocalTime(&t);
    _snwprintf(out, cap, L"%04u-%02u-%02u %02u:%02u:%02u",
               t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
    out[cap - 1] = 0;
}
