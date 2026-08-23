/* --------------------------------------------------------------------------
   zip.c —— 见 zip.h。条目名的安全闸：拒绝绝对路径、盘符、.. 穿越。
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include "zip.h"
#include "util.h"

/* zip 条目名（原始字节）-> 宽字符，顺手做安全检查。0 = 拒绝。
   ★ 编码（§240 真机踩坑）：zip 文件名没有强制编码 —— 打了 UTF-8
   标志位(0x800)的按 UTF-8；没打的多半是打包机系统 ANSI（资源管理器
   压缩/部分工具写 GBK）。这版 miniz 的 file_stat 不暴露标志位，用等价
   启发式：先严格 UTF-8（MB_ERR_INVALID_CHARS，非法序列即败），败了
   退回 CP_ACP。纯 ASCII 两条路殊途同归；GBK 中文串几乎必含 UTF-8
   非法序列。旧代码一律宽恕式 UTF-8 → GBK 字节解成乱码名，游戏目录
   里凭空多出「文件名乱码的快捷方式」。 */
static int entry_name_ok(const char *name, wchar_t *out, size_t cap)
{
    int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                                name, -1, out, (int)cap);
    if (n <= 0)
        n = MultiByteToWideChar(CP_ACP, 0, name, -1, out, (int)cap);
    wchar_t *p;
    if (n <= 0) return 0;
    if (wcsstr(out, L"..")) return 0;               /* 穿越一律拒绝 */
    if (out[0] == L'/' || out[0] == L'\\') return 0; /* 绝对路径 */
    if (out[0] && out[1] == L':') return 0;          /* 盘符 */
    for (p = out; *p; p++)
        if (*p == L'/') *p = L'\\';
    return 1;
}

int zip_open(ZipFile *z, const wchar_t *path, wchar_t *err, size_t err_cap)
{
    char path_mb[MAX_PATH * 2];
    mz_uint i, total;
    int files = 0;
    wchar_t top[MAX_PATH * 2];
    int top_len = -1;
    int have_buildver = 0;

    memset(z, 0, sizeof(*z));
    WideCharToMultiByte(CP_UTF8, 0, path, -1, path_mb, sizeof(path_mb),
                        NULL, NULL);
    if (!mz_zip_reader_init_file(&z->arc, path_mb, 0)) {
        _snwprintf(err, err_cap, L"打开更新包失败（zip 损坏或读不了）");
        err[err_cap - 1] = 0;
        return 0;
    }
    z->opened = 1;

    /* 第一遍：数文件条目 + 认顶层目录。 */
    total = mz_zip_reader_get_num_files(&z->arc);
    for (i = 0; i < total; i++) {
        char name[MAX_PATH * 2];
        wchar_t wname[MAX_PATH * 2];
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&z->arc, i, &st)) continue;
        if (st.m_is_directory) continue;
        if (!mz_zip_reader_get_filename(&z->arc, i, name, sizeof(name)))
            continue;
        if (!entry_name_ok(name, wname, MAX_PATH * 2)) continue;
        files++;
        if (top_len < 0) {
            wchar_t *slash = wcschr(wname, L'\\');
            if (slash) {
                size_t n = slash - wname;
                memcpy(top, wname, n * 2);
                top[n] = 0;
                top_len = (int)n;
            } else {
                top[0] = 0;
                top_len = 0;
            }
        }
    }
    if (!files) {
        _snwprintf(err, err_cap, L"更新包里没有文件条目");
        goto fail;
    }

    z->names = (wchar_t(*)[MAX_PATH * 2])HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY,
        (SIZE_T)files * MAX_PATH * 2 * sizeof(wchar_t));
    z->rels = (wchar_t(*)[MAX_PATH * 2])HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY,
        (SIZE_T)files * MAX_PATH * 2 * sizeof(wchar_t));
    z->mzidx = (int *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY,
                                (SIZE_T)files * sizeof(int));
    if (!z->names || !z->rels || !z->mzidx) {
        _snwprintf(err, err_cap, L"内存不足");
        goto fail;
    }

    /* 第二遍：填 names/rels/mzidx；rels 必须都落在同一顶层目录下。
       mzidx 记的是 zip 里的原始序号（含目录项）—— 解压必须用它，
       拿紧凑下标会在有目录条目时全体错位（§239 真机踩坑）。 */
    for (i = 0; i < total; i++) {
        char name[MAX_PATH * 2];
        wchar_t wname[MAX_PATH * 2];
        mz_zip_archive_file_stat st;
        const wchar_t *rel;
        if (!mz_zip_reader_file_stat(&z->arc, i, &st)) continue;
        if (st.m_is_directory) continue;
        if (!mz_zip_reader_get_filename(&z->arc, i, name, sizeof(name)))
            continue;
        if (!entry_name_ok(name, wname, MAX_PATH * 2)) continue;
        rel = wname;
        if (top_len > 0) {
            if (wcsncmp(wname, top, (size_t)top_len) != 0 ||
                wname[top_len] != L'\\') {
                _snwprintf(err, err_cap,
                           L"更新包顶层目录不统一（%ls）", wname);
                goto fail;
            }
            rel = wname + top_len + 1;
        }
        if (!*rel) continue;                        /* 顶层目录文件本身 */
        wcsncpy(z->names[z->count], wname, MAX_PATH * 2 - 1);
        wcsncpy(z->rels[z->count], rel, MAX_PATH * 2 - 1);
        z->mzidx[z->count] = (int)i;
        if (wide_ieq(rel, L"BUILD.ver")) have_buildver = 1;
        z->count++;
    }
    if (!have_buildver) {
        _snwprintf(err, err_cap, L"更新包里没有 BUILD.ver —— 不是完整的客户端包");
        goto fail;
    }
    return 1;

fail:
    zip_close(z);
    return 0;
}

void zip_close(ZipFile *z)
{
    if (z->opened) {
        mz_zip_reader_end(&z->arc);
        z->opened = 0;
    }
    if (z->names) { HeapFree(GetProcessHeap(), 0, z->names); z->names = NULL; }
    if (z->rels)  { HeapFree(GetProcessHeap(), 0, z->rels);  z->rels = NULL; }
    if (z->mzidx) { HeapFree(GetProcessHeap(), 0, z->mzidx); z->mzidx = NULL; }
    z->count = 0;
}

/* miniz 只收 char* 目标名（fopen 按 ANSI 解释）—— 中文路径会被写坏
   （§240 真机踩坑：UTF-8 字节经 fopen 落盘 = 乱码文件名）。改用
   extract_to_callback + CreateFileW，全程宽字符。 */
static size_t extract_write_cb(void *user, mz_uint64 ofs,
                               const void *buf, size_t n)
{
    HANDLE h = *(HANDLE *)user;
    DWORD wrote;
    (void)ofs;                       /* 顺序写，文件指针自己在走 */
    if (!WriteFile(h, buf, (DWORD)n, &wrote, NULL) || wrote != (DWORD)n)
        return 0;
    return n;
}

int zip_extract_to(ZipFile *z, int i, const wchar_t *dest_root,
                   wchar_t *err, size_t err_cap)
{
    wchar_t dst[MAX_PATH * 2];
    wchar_t *p;
    HANDLE h;

    path_join(dst, MAX_PATH * 2, dest_root, z->rels[i]);
    /* 递归建父目录。 */
    p = wcsrchr(dst, L'\\');
    if (p) {
        *p = 0;
        if (!ensure_dir(dst)) {
            _snwprintf(err, err_cap, L"建目录失败：%ls", dst);
            return 0;
        }
        *p = L'\\';
    }
    h = CreateFileW(dst, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                    FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) {
        _snwprintf(err, err_cap, L"建不了目标文件：%ls", dst);
        return 0;
    }
    /* ★ 用原始索引（含目录项的序号），不是紧凑下标。miniz 自带 CRC 校验。 */
    if (!mz_zip_reader_extract_to_callback(&z->arc, (mz_uint)z->mzidx[i],
                                           extract_write_cb, &h, 0)) {
        CloseHandle(h);
        DeleteFileW(dst);
        _snwprintf(err, err_cap, L"解压失败（zip 损坏/CRC 不过）：%ls",
                   z->rels[i]);
        err[err_cap - 1] = 0;
        return 0;
    }
    CloseHandle(h);
    return 1;
}
