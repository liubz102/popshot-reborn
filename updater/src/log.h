/* --------------------------------------------------------------------------
   log.h —— logs\updater.log 追加日志（UTF-8）。所有关键节点都落一行，
   排查「更新到一半没反应」的现场问题用。
   -------------------------------------------------------------------------- */
#ifndef UPDATER_LOG_H
#define UPDATER_LOG_H

#include <stdarg.h>

/* 初始化（记下包根；同时把自身身份写一行）。 */
void log_init(const wchar_t *package_root, const char *tag_line);
/* 追加一行（UTF-8 写出，时间戳自动加）。中文请传宽串用 %ls。 */
void log_line(const char *fmt, ...);
/* 同上（显式 va_list 版）。 */
void log_vline(const char *fmt, va_list ap);

#endif /* UPDATER_LOG_H */
