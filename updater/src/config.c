/* --------------------------------------------------------------------------
   config.c —— 见 config.h。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include "config.h"

/* 读整个文件成宽文本（UTF-8/UTF-16 按 BOM 认，gb 兜底）。返回长度或 -1。 */
static int read_text_file(const wchar_t *path, wchar_t *out, size_t cap)
{
    HANDLE f;
    unsigned char raw[8192];
    DWORD got = 0;
    int n;

    f = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                    NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return -1;
    if (!ReadFile(f, raw, sizeof(raw) - 2, &got, NULL)) { CloseHandle(f); return -1; }
    CloseHandle(f);
    raw[got] = raw[got + 1] = 0;

    if (got >= 2 && raw[0] == 0xFF && raw[1] == 0xFE) {
        /* UTF-16LE：字节直拷（x86 小端）。 */
        size_t chars = got / 2;
        if (chars > cap - 1) chars = cap - 1;
        memcpy(out, raw + 2, chars * 2);
        out[chars] = 0;
        return (int)chars;
    }
    /* UTF-8（BOM 有无都行）/ 其他按 UTF-8 尽力解。 */
    n = MultiByteToWideChar(CP_UTF8, 0, (const char *)raw, (int)got,
                            out, (int)(cap - 1));
    if (n <= 0) return -1;
    out[n] = 0;
    return n;
}

int cfg_server_address(const wchar_t *root, wchar_t *out, size_t cap)
{
    wchar_t path[MAX_PATH * 2];
    static wchar_t text[4096];
    wchar_t *line, *next;
    int found = 0;

    path_join(path, MAX_PATH * 2, root, L"config/server.config");
    wcscpy(out, L"192.168.1.100");                       /* config.py 同款默认 */
    if (read_text_file(path, text, 4096) < 0) return 1;  /* 没有文件用默认 */

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
