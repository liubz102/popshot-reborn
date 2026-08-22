/* --------------------------------------------------------------------------
   manifest.c —— 见 manifest.h。极小 JSON 扫描器：只认我们自己的 schema，
   任何意外形状都返回失败（调用方退回「手动下载」），不崩溃。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
#include "manifest.h"

typedef struct Cursor {
    const char *p;
} Cursor;

static void skip_ws(Cursor *c)
{
    while (*c->p == ' ' || *c->p == '\t' || *c->p == '\r' || *c->p == '\n')
        c->p++;
}

static int skip_value(Cursor *c);                 /* 前向声明 */

/* 解析一个 JSON 字符串到 out（宽字符）。返回 1 成功并让游标停在闭引号后。 */
static int parse_string(Cursor *c, wchar_t *out, size_t cap)
{
    size_t n = 0;
    if (*c->p != '"') return 0;
    c->p++;
    while (*c->p && *c->p != '"') {
        unsigned ch;
        if (*c->p == '\\') {
            c->p++;
            switch (*c->p) {
            case '"':  ch = '"';  c->p++; break;
            case '\\': ch = '\\'; c->p++; break;
            case '/':  ch = '/';  c->p++; break;
            case 'b':  ch = '\b'; c->p++; break;
            case 'f':  ch = '\f'; c->p++; break;
            case 'n':  ch = '\n'; c->p++; break;
            case 'r':  ch = '\r'; c->p++; break;
            case 't':  ch = '\t'; c->p++; break;
            case 'u': {
                int k;
                ch = 0;
                c->p++;
                for (k = 0; k < 4; k++) {
                    int v;
                    if (*c->p >= '0' && *c->p <= '9') v = *c->p - '0';
                    else if (*c->p >= 'a' && *c->p <= 'f') v = *c->p - 'a' + 10;
                    else if (*c->p >= 'A' && *c->p <= 'F') v = *c->p - 'A' + 10;
                    else return 0;
                    ch = ch * 16 + v;
                    c->p++;
                }
                break;
            }
            default: return 0;
            }
        } else {
            /* UTF-8 多字节聚合后转宽字符。 */
            int need = 1;
            unsigned char b = (unsigned char)*c->p;
            if ((b & 0xE0) == 0xC0) need = 2;
            else if ((b & 0xF0) == 0xE0) need = 3;
            else if ((b & 0xF8) == 0xF0) need = 4;
            {
                wchar_t tmp[2];
                int w = MultiByteToWideChar(CP_UTF8, 0, c->p, need,
                                            tmp, 2);
                if (w <= 0) return 0;
                if (n + (size_t)w < cap) {
                    int k;
                    for (k = 0; k < w; k++) out[n++] = tmp[k];
                }
                c->p += need;
            }
            continue;
        }
        if (n + 1 < cap) out[n++] = (wchar_t)ch;
    }
    if (*c->p != '"') return 0;
    c->p++;
    out[n < cap ? n : cap - 1] = 0;
    return 1;
}

/* 整数（manifest 的 size/format 都是整数；负数/小数认不出直接失败）。 */
static int parse_number(Cursor *c, unsigned long long *out)
{
    unsigned long long v = 0;
    int digits = 0;
    while (*c->p >= '0' && *c->p <= '9') {
        v = v * 10u + (unsigned long long)(*c->p - '0');
        c->p++;
        digits++;
    }
    if (!digits) return 0;
    *out = v;
    return 1;
}

static int skip_value(Cursor *c)
{
    skip_ws(c);
    if (*c->p == '{') {
        c->p++;
        skip_ws(c);
        if (*c->p == '}') { c->p++; return 1; }
        for (;;) {
            wchar_t key[64];
            skip_ws(c);
            if (!parse_string(c, key, 64)) return 0;
            skip_ws(c);
            if (*c->p != ':') return 0;
            c->p++;
            if (!skip_value(c)) return 0;
            skip_ws(c);
            if (*c->p == ',') { c->p++; continue; }
            if (*c->p == '}') { c->p++; return 1; }
            return 0;
        }
    }
    if (*c->p == '[') {
        c->p++;
        skip_ws(c);
        if (*c->p == ']') { c->p++; return 1; }
        for (;;) {
            if (!skip_value(c)) return 0;
            skip_ws(c);
            if (*c->p == ',') { c->p++; continue; }
            if (*c->p == ']') { c->p++; return 1; }
            return 0;
        }
    }
    if (*c->p == '"') {
        wchar_t sink[512];
        return parse_string(c, sink, 512);
    }
    if (*c->p == 't') { if (strncmp(c->p, "true", 4) == 0) { c->p += 4; return 1; } return 0; }
    if (*c->p == 'f') { if (strncmp(c->p, "false", 5) == 0) { c->p += 5; return 1; } return 0; }
    if (*c->p == 'n') { if (strncmp(c->p, "null", 4) == 0) { c->p += 4; return 1; } return 0; }
    {
        unsigned long long v;
        if (*c->p == '-') c->p++;                  /* 跳过负号当 0 处理 */
        return parse_number(c, &v);
    }
}

/* 解析 releases 数组里的一个对象到 entry。 */
static int parse_release_object(Cursor *c, ReleaseEntry *entry)
{
    int have_version = 0, have_url = 0, have_sha = 0;

    memset(entry, 0, sizeof(*entry));
    skip_ws(c);
    if (*c->p != '{') return 0;
    c->p++;
    skip_ws(c);
    if (*c->p == '}') { c->p++; return 0; }        /* 空对象不行 */
    for (;;) {
        wchar_t key[64];
        skip_ws(c);
        if (!parse_string(c, key, 64)) return 0;
        skip_ws(c);
        if (*c->p != ':') return 0;
        c->p++;
        skip_ws(c);
        if (wide_ieq(key, L"version")) {
            if (!parse_string(c, entry->version_text, 32)) return 0;
            if (!ver_parse(entry->version_text, &entry->version)) return 0;
            have_version = 1;
        } else if (wide_ieq(key, L"url")) {
            if (!parse_string(c, entry->url, 512)) return 0;
            have_url = 1;
        } else if (wide_ieq(key, L"sha256")) {
            if (!parse_string(c, entry->sha256, 80)) return 0;
            have_sha = 1;
        } else if (wide_ieq(key, L"size")) {
            unsigned long long v;
            if (!parse_number(c, &v)) return 0;
            entry->size = v;
        } else {
            if (!skip_value(c)) return 0;
        }
        skip_ws(c);
        if (*c->p == ',') { c->p++; continue; }
        if (*c->p == '}') { c->p++; break; }
        return 0;
    }
    /* validate_manifest 同款：url/sha256/版本三样缺一整份作废。 */
    if (!have_version || !have_url || !have_sha) return 0;
    if (!entry->url[0] || wcslen(entry->sha256) != 64) return 0;
    return 1;
}

int manifest_parse(const char *json_text, Manifest *out)
{
    Cursor c;
    int saw_releases = 0;

    memset(out, 0, sizeof(*out));
    c.p = json_text;
    skip_ws(&c);
    if (*c.p != '{') return 0;
    c.p++;
    skip_ws(&c);
    if (*c.p == '}') return 0;
    for (;;) {
        wchar_t key[64];
        skip_ws(&c);
        if (!parse_string(&c, key, 64)) return 0;
        skip_ws(&c);
        if (*c.p != ':') return 0;
        c.p++;
        skip_ws(&c);
        if (wide_ieq(key, L"releases")) {
            if (*c.p != '[') return 0;
            c.p++;
            skip_ws(&c);
            saw_releases = 1;
            if (*c.p != ']') {
                for (;;) {
                    if (out->count >= MANIFEST_MAX_RELEASES) return 0;
                    if (!parse_release_object(&c, &out->entries[out->count]))
                        return 0;
                    out->count++;
                    skip_ws(&c);
                    if (*c.p == ',') { c.p++; continue; }
                    if (*c.p == ']') break;
                    return 0;
                }
            }
            c.p++;                                  /* 吃掉 ] */
        } else {
            if (!skip_value(&c)) return 0;
        }
        skip_ws(&c);
        if (*c.p == ',') { c.p++; continue; }
        if (*c.p == '}') break;
        return 0;
    }
    if (!saw_releases || out->count == 0) return 0;
    return 1;
}
