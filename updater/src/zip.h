/* --------------------------------------------------------------------------
   zip.h —— miniz zip reader 的薄封装（miniz 在 vendor\miniz\）。
   更新包 = 客户端全量 zip，里面带一层顶层目录（PopShot-portable-win64_V0-2-7\）。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_ZIP_H
#define UPDATER_ZIP_H

#include <windows.h>
#include "miniz.h"

typedef struct ZipFile {
    mz_zip_archive arc;
    int opened;
    /* 条目表（去掉目录项后的文件条目，rel = 剥掉顶层目录后的相对路径，
       正斜杠统一改反斜杠方便直接拼 Windows 路径）。
       ★ mzidx = 每个接受条目在 zip 里的【原始】序号（含目录项）——
       真实包带 18 个目录条目，两张表错位过一版：解压拿紧凑下标当原始
       索引用，第一发就解到目录条目上报「zip 损坏」（§239）。 */
    int count;
    int *mzidx;                           /* [count]：miniz 原始索引 */
    wchar_t (*names)[MAX_PATH * 2];      /* zip 内原始名（含顶层目录） */
    wchar_t (*rels)[MAX_PATH * 2];       /* 剥顶层后的路径（\ 分隔） */
} ZipFile;

/* 打开并扫描。校验：必须有 <顶层>\BUILD.ver（python 版同款完整性闸）。
   失败返回 0，err 收原因。 */
int zip_open(ZipFile *z, const wchar_t *path, wchar_t *err, size_t err_cap);
void zip_close(ZipFile *z);

/* 把第 i 个条目解到 dest_root\<rel>（父目录递归建）。
   返回 1 成功（miniz 自带 CRC 校验，损坏在此报出来）。 */
int zip_extract_to(ZipFile *z, int i, const wchar_t *dest_root,
                   wchar_t *err, size_t err_cap);

#endif /* UPDATER_ZIP_H */
