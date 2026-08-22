/* --------------------------------------------------------------------------
   manifest.h —— GitHub manifest.json 的手写解析（面向已知 schema，
   认不出 fail-open 由调用方处理，绝不在玩家机上崩）。

   schema（tools\update-manifest.json）：
     { "format":1, "repo":"...", "releases":[ {"version":"0.2.7",
       "date":"...", "url":"...", "size":394780106, "sha256":"..."}, ... ] }
   releases[0] 是最新版（update_manifest.py 前插）。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_MANIFEST_H
#define UPDATER_MANIFEST_H

#include "util.h"

#define MANIFEST_MAX_RELEASES 32

typedef struct ReleaseEntry {
    Ver version;
    wchar_t version_text[32];
    wchar_t url[512];
    wchar_t sha256[80];              /* 64 hex + NUL */
    unsigned long long size;         /* 0 = manifest 没写 */
} ReleaseEntry;

typedef struct Manifest {
    int count;
    ReleaseEntry entries[MANIFEST_MAX_RELEASES];
} Manifest;

/* 解析 + 校验（= python validate_manifest：任何一条缺 url/sha256 或版本
   认不出 -> 整份作废，返回 0）。 */
int manifest_parse(const char *json_text, Manifest *out);

#endif /* UPDATER_MANIFEST_H */
