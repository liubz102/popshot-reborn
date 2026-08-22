/* --------------------------------------------------------------------------
   log.c —— 见 log.h。文件写失败一律静默放弃，绝不影响更新主流程。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include "log.h"
#include "util.h"

static wchar_t g_root[MAX_PATH * 2];

void log_init(const wchar_t *package_root, const char *tag_line)
{
    wcsncpy(g_root, package_root ? package_root : L"", MAX_PATH * 2 - 1);
    g_root[MAX_PATH * 2 - 1] = 0;
    if (tag_line) log_line("%s", tag_line);
}

void log_vline(const char *fmt, va_list ap)
{
    wchar_t path[MAX_PATH * 2];
    wchar_t wstamp[32];
    char stamp[32];
    char body[1600];
    char line[2048];
    HANDLE f;
    DWORD wrote;
    const char *i = fmt;
    char *o = body;
    size_t cap = sizeof(body);

    /* ★ 不用 _vsnprintf：%ls（宽串转窄）走 CRT locale，C locale 下中文
       全部变空串。自己扫格式串：%%s 直拷、%%ls 用 wide_to_utf8。 */
    #define PUTC(ch) do { \
        if (o - body < (long)cap - 1) *o++ = (ch); \
    } while (0)

    while (*i && o - body < (long)cap - 1) {
        if (*i != '%') { *o++ = *i++; continue; }
        i++;
        if (*i == '%') { PUTC('%'); i++; continue; }
        if (i[0] == 'l' && i[1] == 's') {                 /* %ls 宽串 */
            wchar_t *w = va_arg(ap, wchar_t *);
            char tmp[1024];
            if (w && wide_to_utf8(w, tmp, sizeof(tmp)) >= 0) {
                char *t = tmp;
                while (*t && o - body < (long)cap - 1) *o++ = *t++;
            }
            i += 2;
            continue;
        }
        if (i[0] == 'h' && i[1] == 's') {                 /* %hs 窄串 */
            char *s = va_arg(ap, char *);
            if (!s) s = "(null)";
            while (*s && o - body < (long)cap - 1) *o++ = *s++;
            i += 2;
            continue;
        }
        if (*i == 's') {                                  /* %s 窄串 */
            char *s = va_arg(ap, char *);
            if (!s) s = "(null)";
            while (*s && o - body < (long)cap - 1) *o++ = *s++;
            i++;
            continue;
        }
        /* 整数：吃掉 [ll][d u x X]，按宽度取 vararg 再格式化。 */
        {
            int longs = 0;
            char conv = 0;
            while (*i == 'l') { longs++; i++; }
            if (*i == 'd' || *i == 'i' || *i == 'u' || *i == 'x' ||
                *i == 'X') {
                conv = *i++;
                {
                    unsigned long long v;
                    int neg = 0;
                    char num[32];
                    int n;
                    if (conv == 'd' || conv == 'i') {
                        if (longs >= 2) { long long t = va_arg(ap, long long); neg = t < 0; v = neg ? (unsigned long long)(-(t + 1)) + 1 : (unsigned long long)t; }
                        else { long t = va_arg(ap, long); neg = t < 0; v = neg ? (unsigned long long)(-(t + 1)) + 1 : (unsigned long long)t; }
                    } else {
                        if (longs >= 2) v = va_arg(ap, unsigned long long);
                        else v = va_arg(ap, unsigned long);
                    }
                    if (neg) PUTC('-');
                    if (conv == 'x' || conv == 'X')
                        n = _snprintf(num, sizeof(num), conv == 'x' ? "%llx" : "%llX", v);
                    else
                        n = _snprintf(num, sizeof(num), "%llu", v);
                    if (n > 0) {
                        char *t = num;
                        while (*t && o - body < (long)cap - 1) *o++ = *t++;
                    }
                }
                continue;
            }
        }
        /* 其余逐字透传（调用点没用过的格式）。 */
        PUTC('%');
        if (*i) { PUTC(*i); i++; }
    }
    *o = 0;
    #undef PUTC

    if (!g_root[0]) return;
    path_join(path, MAX_PATH * 2, g_root, L"logs");
    CreateDirectoryW(path, NULL);                       /* 不在也不报错 */
    wcscat(path, L"\\updater.log");

    now_stamp(wstamp, 32);
    stamp[0] = 0;
    wide_to_utf8(wstamp, stamp, sizeof(stamp));
    _snprintf(line, sizeof(line), "[%s] %s\r\n", stamp, body);
    line[sizeof(line) - 1] = 0;

    f = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                    OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return;
    SetFilePointer(f, 0, NULL, FILE_END);
    WriteFile(f, line, (DWORD)strlen(line), &wrote, NULL);
    CloseHandle(f);
}

void log_line(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    log_vline(fmt, ap);
    va_end(ap);
}
