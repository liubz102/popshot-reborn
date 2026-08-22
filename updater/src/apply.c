/* --------------------------------------------------------------------------
   apply.c —— 见 apply.h。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include "apply.h"
#include "util.h"
#include "log.h"

/* update_client.py PROTECTED_PATHS 同款（主保护其实在打包侧：zip 里本来
   就没有这些；这是第二道纵深防御）。目录条目按前缀匹配。 */
static const wchar_t *PROTECTED_PATHS[] = {
    L"server/data/accounts.json",
    L"config/server.config",
    L"game_patched/UserConfig.ini",
    L"game_patched/BigShot.rpt",
    L"logs",
    L"game_patched/Dump",
    L"game_patched/Debug",
};

#define UPDATE_OLD_SUFFIX L".update_old"

int apply_is_protected(const wchar_t *rel_in)
{
    wchar_t rel[MAX_PATH * 2];
    wchar_t *p;
    size_t i;

    wcsncpy(rel, rel_in, MAX_PATH * 2 - 1);
    rel[MAX_PATH * 2 - 1] = 0;
    for (p = rel; *p; p++)
        if (*p == L'\\') *p = L'/';
    /* 去首尾斜杠。 */
    p = rel;
    while (*p == L'/') p++;
    {
        size_t n = wcslen(p);
        while (n && p[n - 1] == L'/') p[--n] = 0;
    }
    if (!*p) return 1;                      /* 顶层目录本身不碰 */
    for (i = 0; i < sizeof(PROTECTED_PATHS) / sizeof(PROTECTED_PATHS[0]); i++) {
        const wchar_t *entry = PROTECTED_PATHS[i];
        size_t len = wcslen(entry);
        if (wcscmp(p, entry) == 0)
            return 1;
        /* 前缀目录匹配：logs 命中 logs/xxx。 */
        if (wcsncmp(p, entry, len) == 0 && p[len] == L'/')
            return 1;
    }
    return 0;
}

/* 覆盖一个文件：MoveFileEx REPLACE_EXISTING；被占/只读重试；从第 4 次
   起把目标改名 .update_old 让位（先清上次遗留的同名，防二次更新挡道）。 */
static int copy_with_retry(const wchar_t *src, const wchar_t *dst,
                           wchar_t *err, size_t err_cap)
{
    int attempt;
    for (attempt = 0; attempt < 20; attempt++) {
        if (MoveFileExW(src, dst, MOVEFILE_REPLACE_EXISTING))
            return 1;
        {
            DWORD e = GetLastError();
            if (attempt == 0 ||
                (e != ERROR_ACCESS_DENIED && e != ERROR_SHARING_VIOLATION &&
                 e != ERROR_LOCK_VIOLATION)) {
                /* 只读位最常见 —— 清掉再试一轮。 */
                SetFileAttributesW(dst, FILE_ATTRIBUTE_NORMAL);
            }
            if (attempt >= 3 &&
                (e == ERROR_ACCESS_DENIED || e == ERROR_SHARING_VIOLATION ||
                 e == ERROR_LOCK_VIOLATION)) {
                wchar_t old[MAX_PATH * 2];
                wcsncpy(old, dst, MAX_PATH * 2 - 6);
                old[MAX_PATH * 2 - 6] = 0;
                wcscat(old, UPDATE_OLD_SUFFIX);
                DeleteFileW(old);           /* 上次遗留（正常该被启动脚本清） */
                if (MoveFileExW(dst, old, 0) &&
                    MoveFileExW(src, dst, MOVEFILE_REPLACE_EXISTING)) {
                    log_line("rename-dance %ls -> %ls", dst, old);
                    return 1;
                }
            }
        }
        Sleep(250);
    }
    _snwprintf(err, err_cap, L"覆盖 %ls 失败（重试 20 次，错误 %lu）",
               dst, GetLastError());
    err[err_cap - 1] = 0;
    return 0;
}

int apply_update(const wchar_t *zip_path, const wchar_t *root,
                 apply_progress_fn progress, void *user,
                 int *moved_out, wchar_t *err, size_t err_cap)
{
    ZipFile z;
    wchar_t staging[MAX_PATH * 2];
    wchar_t src[MAX_PATH * 2], dst[MAX_PATH * 2];
    int i, buildver_index = -1, moved = 0, skipped = 0;
    wchar_t pid_text[32];
    unsigned long pid = GetCurrentProcessId();

    *moved_out = 0;
    if (!zip_open(&z, zip_path, err, err_cap)) return 0;

    /* staging 建包根（同盘：MoveFileEx 搬运用，跨盘会失败；盘空间问题也
       在正确的盘上暴露）。目录名带 pid 防撞。 */
    u64_to_wide((unsigned long long)pid, pid_text, 32);
    _snwprintf(staging, MAX_PATH * 2, L"%s\\.popshot-apply-%s", root, pid_text);
    staging[MAX_PATH * 2 - 1] = 0;
    if (file_exists(staging)) delete_tree(staging);
    if (!ensure_dir(staging)) {
        _snwprintf(err, err_cap, L"建不了临时目录 %ls", staging);
        err[err_cap - 1] = 0;
        zip_close(&z);
        return 0;
    }

    /* 整包先解到 staging：zip 损坏（CRC）在任何文件搬动之前暴露。
       BUILD.ver 也解出来，但搬运阶段最后走（= 提交点）。 */
    for (i = 0; i < z.count; i++) {
        if (apply_is_protected(z.rels[i])) { skipped++; continue; }
        if (wide_ieq(z.rels[i], L"BUILD.ver")) buildver_index = i;
        if (!zip_extract_to(&z, i, staging, err, err_cap)) {
            delete_tree(staging);
            zip_close(&z);
            return 0;
        }
    }

    /* 逐文件搬（BUILD.ver 最后 = 提交点）。 */
    for (i = 0; i < z.count; i++) {
        if (apply_is_protected(z.rels[i])) continue;
        if (i == buildver_index) continue;
        path_join(src, MAX_PATH * 2, staging, z.rels[i]);
        path_join(dst, MAX_PATH * 2, root, z.rels[i]);
        {
            wchar_t *p = wcsrchr(dst, L'\\');
            if (p) {
                *p = 0;
                if (!ensure_dir(dst)) {
                    _snwprintf(err, err_cap, L"建目录失败：%ls", dst);
                    delete_tree(staging);
                    zip_close(&z);
                    return 0;
                }
                *p = L'\\';
            }
        }
        if (!copy_with_retry(src, dst, err, err_cap)) {
            delete_tree(staging);
            zip_close(&z);
            return 0;
        }
        moved++;
        if (progress) progress(user, moved, z.count);
    }

    if (buildver_index >= 0) {
        path_join(src, MAX_PATH * 2, staging, z.rels[buildver_index]);
        path_join(dst, MAX_PATH * 2, root, z.rels[buildver_index]);
        if (!copy_with_retry(src, dst, err, err_cap)) {
            delete_tree(staging);
            zip_close(&z);
            return 0;
        }
    }

    delete_tree(staging);
    zip_close(&z);
    *moved_out = moved;
    log_line("applied moved=%d skipped=%d", moved, skipped);
    return 1;
}
