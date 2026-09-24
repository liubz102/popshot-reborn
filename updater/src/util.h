/* --------------------------------------------------------------------------
   util.h —— 通用小件：编码转换、路径、版本号、动态宽字符串缓冲。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_UTIL_H
#define UPDATER_UTIL_H

#include <windows.h>
#include <stddef.h>

/* ---- 版本号（语义与 server\versioning.py 逐条对齐） --------------------- */

typedef struct Ver {
    int major, minor, patch;
} Ver;

/* "0.2.7"/"v0.2"/"V1.2.3"（多行、# ; 注释、空段/超限拒绝）-> 0 成功。 */
int  ver_parse(const wchar_t *text, Ver *out);
/* Ver -> "V0.2.7"（wchar，日志统一这个格式）。 */
void ver_format(const Ver *v, wchar_t *out, size_t cap);
/* Ver 比较：<0 / 0 / >0。 */
int  ver_cmp(const Ver *a, const Ver *b);
/* 线上编码 major*1e6+minor*1e3+patch；311 保留、<1000 拒绝 -> 返回 -1。 */
long ver_encode_wire(const Ver *v);

/* ---- 编码转换（长度全部按「字符数」计，不含结尾 0） --------------------- */

/* gb2312(gbk, CP936) 字节 -> 宽字符。返回目标长度；cap 不够返回 -1。 */
int  gbk_to_wide(const char *src, size_t srclen, wchar_t *dst, size_t cap);
/* 宽字符 -> gb2312 字节（写 announce.html 用）。 */
int  wide_to_gbk(const wchar_t *src, size_t srclen, char *dst, size_t cap);
/* 宽字符 -> UTF-8（写日志用），带 NUL 结尾。 */
int  wide_to_utf8(const wchar_t *src, char *dst, size_t cap);
/* UTF-8 字节 -> 宽字符（服务器回来的 update.config）。自动吃掉 BOM；
   cap 不够时截到放得下为止（退回 UTF-8 起始字节，不切半个汉字）。
   返回宽字符数，失败 -1。 */
int  utf8_to_wide(const char *src, size_t srclen, wchar_t *dst, size_t cap);

/* ---- 更新源的显示名 ------------------------------------------------------ */

/* manifest 地址 -> 界面上「仓库：」那一行用的简写（最长 40 字符，超了截断
   加「…」）。去掉 scheme 和 www.，保留域名 + 前两段路径；域名正好是
   github.com 时连它一起去掉：
     https://github.com/liubz102/popshot-reborn/releases/latest/download/manifest.json
       -> liubz102/popshot-reborn
     https://oss.example.com/pkg/manifest.json   -> oss.example.com/pkg/manifest.json
   ★ 纯展示，不参与任何判断 —— 认不出的地址原样截断即可。 */
void manifest_repo_label(const wchar_t *url, wchar_t *out, size_t cap);

/* ---- 路径 ---------------------------------------------------------------- */

/* 拼路径（防溢出的 _snwprintf 包装）。 */
void path_join(wchar_t *out, size_t cap, const wchar_t *a, const wchar_t *b);
int  file_exists(const wchar_t *path);
/* 递归建目录（单级父目录不存在也一并建）。 */
int  ensure_dir(const wchar_t *path);
/* 递归删除目录树（清 staging 用），失败返回非 0。 */
int  delete_tree(const wchar_t *path);
/* 规范化斜杠方向为 \\（zip 条目名 / -> \）。 */
void slashes_to_back(wchar_t *path);
/* 大小写不敏感的宽串比较（lstrcmpiW 的函数指针友好版）。 */
int  wide_ieq(const wchar_t *a, const wchar_t *b);
/* 拿进程自己的 exe 完整路径。 */
void module_path(wchar_t *out, size_t cap);
/* exe 在 <root>\game_patched\ -> 包根。 */
void package_root(wchar_t *out, size_t cap);

/* ---- 其他 ---------------------------------------------------------------- */

/* 宽路径 -> "file:///" URL（非 ASCII 按 UTF-8 百分号编码、空格 %20）。 */
void file_url_from_path(const wchar_t *path, wchar_t *out, size_t cap);
/* base64（标准字母表，带填充）。返回写出长度（不含 NUL），cap 不足 -1。 */
int  base64_encode(const unsigned char *src, size_t len, char *dst, size_t cap);
/* 64 位整数 -> 十进制宽串。 */
void u64_to_wide(unsigned long long v, wchar_t *out, size_t cap);
/* 字节数 -> MiB 一位小数的宽串（"123.4"），界面/日志的速度、大小都用它。 */
void mib_to_wide(unsigned long long bytes, wchar_t *out, size_t cap);
/* 本机此刻的 UTC 偏移："UTC+8" / "UTC-3" / "UTC+5:30"。 */
void utc_offset_text(wchar_t *out, size_t cap);
/* 时间格式 "YYYY-MM-DD HH:MM:SS UTC+8"（★ 带时区，bug调查/25）。 */
void now_stamp(wchar_t *out, size_t cap);

#endif /* UPDATER_UTIL_H */
