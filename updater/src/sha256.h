/* --------------------------------------------------------------------------
   sha256.h —— CNG（bcrypt.dll，Win7+ 系统自带）的流式 SHA-256。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_SHA256_H
#define UPDATER_SHA256_H

#include <windows.h>
#include <bcrypt.h>

typedef struct Sha256 {
    BCRYPT_ALG_HANDLE  alg;
    BCRYPT_HASH_HANDLE hash;
    unsigned char     *obj;          /* CNG 的哈希对象缓冲 */
    unsigned long      objlen;
} Sha256;

/* 返回 1 成功。begin 之后可多次 update，finish 出 64 字符小写 hex。 */
int  sha256_begin(Sha256 *s);
int  sha256_update(Sha256 *s, const void *data, unsigned long len);
int  sha256_finish(Sha256 *s, wchar_t hex_out[65]);
void sha256_end(Sha256 *s);          /* 释放（幂等） */

/* 整文件哈希（1MB 块流式读）。返回 1 成功。 */
int  sha256_file(const wchar_t *path, wchar_t hex_out[65]);

#endif /* UPDATER_SHA256_H */
