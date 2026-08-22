/* --------------------------------------------------------------------------
   main.c —— 更新流程编排（全逻辑都在这个 exe 里，python 完全退出更新链）。

   玩家看到的样子（原版 BsPatcherChn/NGM 的交互）：
     客户端被版本门禁拒绝 -> 拉起 game_patched\BsPatcherChn.exe（本程序）->
     原版风格更新窗口（双进度条「目前/全部」）自动跑：检查 -> 下载 ->
     停本机服务端 -> 覆盖 -> 「更新完成，请重新启动游戏」。用户拍板：
     完成后只提示手动重启（start.bat / start-debug.bat），不自动拉起。

   命令行：
     （客户端升级分支带的原版 NGM参数 全忽略，只认）
     -procid:'N'       游戏进程号（等它退出用）
     --elevated        （内部）本次已是管理员
     --zip <path>      （内部）复用已下载的更新包（提权重跑不重复下载）
     --target-version  （内部）跳过探针直接指定目标
     --manifest-url <url>  （测试）覆盖 manifest 地址
     --ui-mode 1|2|3   （测试）强制渲染链某一档
     --noui            无界面跑（自动化测试；等价 POPSHOT_UPDATER_NOUI=1）
     --selftest        回归自检（构建闸门）
   -------------------------------------------------------------------------- */
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#include <stdlib.h>
#include "util.h"
#include "log.h"
#include "config.h"
#include "cipher.h"
#include "sha256.h"
#include "manifest.h"
#include "net_http.h"
#include "probe.h"
#include "procs.h"
#include "apply.h"
#include "ui_window.h"
#include "ports.h"

static const char UPDATER_TAG[] =
    "POPSHOT-UPDATER/3 (all-in-one: NGM-style UI + full update; updater\\src)";

/* 地址硬编码（发版人交代：不新增配置文件）。与 tools\update-manifest.json
   里的 repo 一致；手动下载兜底页给玩家看。 */
static const wchar_t MANIFEST_URL[] =
    L"https://github.com/liubz102/popshot-reborn/releases/latest/download/manifest.json";
static const wchar_t RELEASES_PAGE[] =
    L"https://github.com/liubz102/popshot-reborn/releases";

typedef struct Args {
    DWORD procid;
    int elevated;
    wchar_t zip[MAX_PATH * 2];
    wchar_t target_version[64];
    wchar_t manifest_url[1024];
    int ui_mode;
    int noui;
} Args;

typedef struct Ctx {
    wchar_t root[MAX_PATH * 2];
    Args args;
    Ver local;
    int local_valid;
} Ctx;

static Ctx g_ctx;

/* ------------------------------------------------------------------ */
/*  小件                                                                */
/* ------------------------------------------------------------------ */

static void finish_ok(const wchar_t *text)
{
    ui_set_stage(UI_STAGE_FINAL);
    ui_status(text);
    ui_swap_button();
    log_line("FINISH-OK");
}

static void finish_fail(const wchar_t *reason)
{
    ui_set_stage(UI_STAGE_FINAL);
    /* 失败详情进公告区（大块、看得清 + 手动下载地址）；底部小字只留一句。
       （真机踩坑修复：原来整段详情挤在 11px 单行小字里，显示不全。） */
    ui_announce_error(reason);
    ui_status(L"自动更新没能完成，详见上方说明。");
    ui_swap_button();
    log_line("FAIL %ls", reason);
}

static int root_is_writable_or_ask(void)
{
    if (cfg_root_writable(g_ctx.root)) return 1;
    if (g_ctx.args.elevated) return 1;     /* 提权过还写不进 = 后面自然报错 */
    return 0;
}

static int elevate_and_rerun(const wchar_t *zip, const wchar_t *target_version)
{
    wchar_t self[MAX_PATH * 2];
    wchar_t params[2100];
    INT_PTR rc;

    module_path(self, MAX_PATH * 2);
    _snwprintf(params, 2100, L"--elevated --zip \"%ls\"", zip);
    if (target_version && *target_version)
        _snwprintf(params + wcslen(params), 2100 - wcslen(params),
                   L" --target-version %ls", target_version);
    if (g_ctx.args.procid)
        _snwprintf(params + wcslen(params), 2100 - wcslen(params),
                   L" --procid %lu", (unsigned long)g_ctx.args.procid);
    params[2099] = 0;

    rc = (INT_PTR)ShellExecuteW(NULL, L"runas", self, params, g_ctx.root,
                                SW_SHOWNORMAL);
    /* rc == 5 (SE_ERR_ACCESSDENIED) = 玩家在 UAC 上点了否：按拍板安静退。 */
    log_line("runas relaunch rc=%d", (int)rc);
    return rc > 32;
}

/* ------------------------------------------------------------------ */
/*  manifest 取用与目标选择（update_client.py fetch/pick 的移植）          */
/* ------------------------------------------------------------------ */

static int fetch_manifest(Manifest *m, wchar_t *err, size_t err_cap)
{
    const wchar_t *url = g_ctx.args.manifest_url[0]
                             ? g_ctx.args.manifest_url : MANIFEST_URL;
    static char buf[262144];
    size_t len = 0;
    wchar_t net_err[256];

    ui_status(L"正在获取更新清单……");
    if (!net_get_memory(url, buf, sizeof(buf), &len, net_err, 256)) {
        _snwprintf(err, err_cap, L"取不到更新清单（%ls）", net_err);
        return 0;
    }
    if (!manifest_parse(buf, m)) {
        _snwprintf(err, err_cap, L"更新清单内容认不出");
        return 0;
    }
    return 1;
}

static const ReleaseEntry *pick_target(const Manifest *m, const Ver *wanted,
                                       int *need_update)
{
    int i;
    *need_update = 0;
    if (wanted) {
        for (i = 0; i < m->count; i++)
            if (ver_cmp(&m->entries[i].version, wanted) == 0) {
                if (g_ctx.local_valid &&
                    ver_cmp(&g_ctx.local, &m->entries[i].version) >= 0)
                    return NULL;               /* 服务器要的版本本地已有 */
                return &m->entries[i];
            }
        {
            wchar_t wtext[32];
            ver_format(wanted, wtext, 32);
            log_line("manifest has no server-wanted %ls, use newest", wtext);
        }
    }
    /* 最新版 = releases[0]（update_manifest.py 前插）。 */
    if (g_ctx.local_valid && ver_cmp(&g_ctx.local, &m->entries[0].version) >= 0)
        return NULL;
    *need_update = 1;
    return &m->entries[0];
}

/* ------------------------------------------------------------------ */
/*  下载（进度/速度/剩余时间 -> 双进度条的「全部」条 + 剩余时间行）         */
/* ------------------------------------------------------------------ */

/* 字节数 -> MiB 一位小数的宽串。 */
static void mib_wide(unsigned long long bytes, wchar_t *out, size_t cap)
{
    unsigned long long mib_x10 = bytes * 10 / (1ULL << 20);
    _snwprintf(out, cap, L"%llu.%llu", mib_x10 / 10, mib_x10 % 10);
    out[cap - 1] = 0;
}

static int file_size_is(const wchar_t *path, unsigned long long want)
{
    WIN32_FILE_ATTRIBUTE_DATA fad;
    ULONGLONG size;
    if (!GetFileAttributesExW(path, GetFileExInfoStandard, &fad)) return 0;
    size = (ULONGLONG)fad.nFileSizeHigh << 32 | fad.nFileSizeLow;
    return size == want;
}

static void apply_progress_cb(void *user, int done, int total)
{
    int percent;
    (void)user;
    if (total <= 0) return;
    percent = done * 100 / total;
    ui_progress_current(percent);
    if (done % 400 == 0) {
        wchar_t text[96];
        _snwprintf(text, 96, L"正在写入文件……已完成 %d / %d", done, total);
        text[95] = 0;
        ui_status(text);
    }
}

typedef struct DownloadUi {
    ULONGLONG started;
    int last_percent;
} DownloadUi;

static int download_progress(void *user, unsigned long long done,
                             unsigned long long total)
{
    DownloadUi *du = (DownloadUi *)user;
    int percent = 0;
    unsigned long long now = GetTickCount64();
    unsigned long long elapsed;

    if (ui_cancel_requested()) return 0;

    if (total) {
        percent = (int)(done * 100 / total);
        if (percent != du->last_percent) {
            du->last_percent = percent;
            ui_progress_total(percent);
        }
        elapsed = now - du->started;
        if (elapsed > 800 && done) {
            unsigned long long speed = done * 1000 / elapsed;   /* B/s */
            unsigned long long eta = (total - done) / (speed ? speed : 1);
            wchar_t got[32], spd[32], remain[128];
            mib_wide(done, got, 32);
            mib_wide(speed, spd, 32);
            _snwprintf(remain, 128,
                       L"已下载 %ls MiB  %ls MiB/s  剩余约 %llu 秒",
                       got, spd, (unsigned long long)(eta > 99999 ? 99999 : eta));
            remain[127] = 0;
            ui_remaining(remain);
        }
    }
    return 1;
}

static int fetch_zip_cached(const ReleaseEntry *e, wchar_t *zip_out,
                            size_t cap, wchar_t *err, size_t err_cap)
{
    wchar_t base[MAX_PATH], name[96];
    ULONGLONG started;
    DownloadUi du;

    if (!GetTempPathW(MAX_PATH, base)) {
        _snwprintf(err, err_cap, L"拿不到临时目录");
        return 0;
    }
    _snwprintf(name, 96, L"popshot-update-%ls.zip", e->version_text);
    name[95] = 0;
    _snwprintf(zip_out, cap, L"%s%s", base, name);
    zip_out[cap - 1] = 0;

    /* 缓存复用：提权重跑 / 玩家再点一次不重复下 400MB（判据 = sha256）。 */
    if (file_exists(zip_out)) {
        wchar_t have[80];
        ui_status(L"发现已下载的更新包，正在校验……");
        if (e->size == 0 || file_size_is(zip_out, e->size)) {
            if (sha256_file(zip_out, have) &&
                wide_ieq(have, e->sha256)) {
                log_line("zip cache reuse %ls", zip_out);
                return 1;
            }
        }
        DeleteFileW(zip_out);
    }

    ui_status(L"正在下载完整客户端包（约 400 MiB），请耐心等待……");
    log_line("download %ls", e->url);
    started = GetTickCount64();
    du.started = started;
    du.last_percent = -1;

    {
        Sha256 s;
        wchar_t digest[80];
        int ok;
        if (!sha256_begin(&s)) {
            _snwprintf(err, err_cap, L"初始化哈希失败");
            return 0;
        }
        ok = net_download_file(e->url, zip_out,
                               e->size ? (long long)e->size : -1,
                               &s, download_progress, &du, err, err_cap);
        if (!ok) {
            sha256_end(&s);
            return 0;                       /* err 里带 cancelled 或原因 */
        }
        if (!sha256_finish(&s, digest)) {
            sha256_end(&s);
            _snwprintf(err, err_cap, L"计算校验值失败");
            return 0;
        }
        sha256_end(&s);
        if (!wide_ieq(digest, e->sha256)) {
            DeleteFileW(zip_out);
            _snwprintf(err, err_cap,
                       L"sha256 校验不过（更新源文件损坏或被篡改）");
            return 0;
        }
    }
    {
        ULONGLONG secs = (GetTickCount64() - started) / 1000 + 1;
        log_line("zip ready %ls (%llu MB in %llu s)",
                 zip_out, e->size >> 20, secs);
    }
    ui_remaining(NULL);
    return 1;
}

/* ------------------------------------------------------------------ */
/*  worker：完整更新流程                                                  */
/* ------------------------------------------------------------------ */

static DWORD WINAPI worker_main(LPVOID param)
{
    Manifest manifest;
    wchar_t err[512];
    const ReleaseEntry *target = NULL;
    wchar_t zip_path[MAX_PATH * 2];
    wchar_t ver_text[64];

    (void)param;
    wcscpy(zip_path, g_ctx.args.zip);       /* 提权重跑的直通参数 */

    if (!zip_path[0]) {
        ProbeResult pr;
        wchar_t host[256];
        int need_update = 0;

        /* --- 探针：问服务器「该升到哪版」 ---------------------------- */
        ui_status(L"正在探测服务器，确认需要的版本……");
        cfg_server_address(g_ctx.root, host, 256);
        probe_server(host, POPSHOT_GAME_PORT,
                     g_ctx.local_valid ? &g_ctx.local : NULL, &pr);
        if (pr.status == PROBE_OK) {
            finish_ok(L"服务器已接受当前版本，无需更新。");
            return 0;
        }
        if (pr.status == PROBE_REJECTED) {
            if (pr.wanted_valid)
                log_line("probe rejected wanted=%d.%d.%d msg=%ls",
                         pr.wanted.major, pr.wanted.minor, pr.wanted.patch,
                         pr.message);
            else
                log_line("probe rejected (no version in text) msg=%ls",
                         pr.message);
        } else {
            log_line("probe unreachable, fall back to newest");
        }

        /* --- manifest 与目标版本 -------------------------------------- */
        if (!fetch_manifest(&manifest, err, 512)) {
            finish_fail(err);
            return 1;
        }
        if (g_ctx.args.target_version[0]) {
            Ver forced;
            if (!ver_parse(g_ctx.args.target_version, &forced)) {
                finish_fail(L"--target-version 认不出（内部参数错误）");
                return 1;
            }
            target = pick_target(&manifest, &forced, &need_update);
        } else if (pr.status == PROBE_REJECTED && pr.wanted_valid) {
            target = pick_target(&manifest, &pr.wanted, &need_update);
        } else {
            target = pick_target(&manifest, NULL, &need_update);
        }
        if (!target) {
            finish_ok(L"已是最新版本，无需更新。");
            return 0;
        }
        ver_format(&target->version, ver_text, 64);
        log_line("target %ls", ver_text);
        ui_announce_version(ver_text);
        ui_status(L"准备下载更新……");

        /* --- 下载 + 校验（在提权之前：临时目录不需要管理员） ---------- */
        ui_set_stage(UI_STAGE_DOWNLOAD);
        if (!fetch_zip_cached(target, zip_path, MAX_PATH * 2, err, 512)) {
            if (wide_ieq(err, L"cancelled"))
                finish_ok(L"已取消更新。可以关闭本窗口。");
            else
                finish_fail(err);
            return 1;
        }
    } else {
        /* --zip 直通：目标版本从参数/manifest 补齐（进度条目标）。 */
        if (g_ctx.args.target_version[0]) {
            wcsncpy(ver_text, g_ctx.args.target_version, 63);
            ver_text[63] = 0;
            ui_announce_version(ver_text);
        }
    }

    if (ui_cancel_requested()) {
        finish_ok(L"已取消更新。可以关闭本窗口。");
        return 0;
    }

    /* --- 等游戏退出（game_patched 的文件都被它锁着） ----------------- */
    {
        DWORD still[PROCS_MAX_PIDS];
        int still_count = 0;
        int rc = procs_wait_game_exit(g_ctx.args.procid, still, &still_count);
        int i;
        if (rc == 1) {
            int btn = ui_message_box(
                L"更新需要关闭游戏。<br>点击「确认」将自动结束游戏进程。",
                0, 1);
            if (btn != UI_BTN_CONFIRM) {
                finish_ok(L"已取消更新。可以关闭本窗口。");
                return 0;
            }
            for (i = 0; i < still_count; i++)
                procs_tree_kill(still[i]);
            if (procs_wait_gone(still, still_count, 20, 250) != 0) {
                finish_fail(L"游戏进程结束不了，文件仍被占用。请手动关闭游戏后重试。");
                return 1;
            }
        } else if (rc == 2) {
            finish_fail(L"游戏进程结束不了，文件仍被占用。请手动关闭游戏后重试。");
            return 1;
        }
    }

    /* --- 停本机服务端/中继（锁着 runtime\python\python.exe） ---------- */
    {
        int ended = 0;
        int stopped = procs_stop_package_pythons(g_ctx.root, &ended);
        if (stopped)
            log_line("stopped package pythons=%d", stopped);
        if (ended) {
            finish_fail(L"本机服务端进程结束不了（runtime 仍被占用）。请手动关闭它的窗口后重试更新。");
            return 1;
        }
    }

    /* --- 写权限：平时零提权，写不进才弹原版 CONFIRMRUNADMIN 框 --------- */
    if (!g_ctx.args.elevated && !root_is_writable_or_ask()) {
        if (!ui_confirm_admin()) {
            finish_fail(L"没有管理员权限，写不了游戏目录。");
            return 1;
        }
        if (!elevate_and_rerun(zip_path, ver_text)) {
            finish_fail(L"更新程序没能以管理员身份启动。");
            return 1;
        }
        ui_status(L"已把更新交给管理员窗口继续，本窗口可以关闭。");
        ui_swap_button();
        log_line("elevated rerun spawned, exiting");
        ui_request_quit(0);
        return 0;
    }

    /* --- 应用 -------------------------------------------------------- */
    ui_set_stage(UI_STAGE_APPLY);
    ui_remaining(NULL);
    {
        int moved = 0;
        ui_status(L"正在应用更新（写入文件）……");
        if (!apply_update(zip_path, g_ctx.root, apply_progress_cb, NULL,
                          &moved, err, 512)) {
            finish_fail(err);
            return 1;
        }
        DeleteFileW(zip_path);            /* 删不了也无妨（缓存按哈希复用） */
    }

    /* --- 完成态：提示手动重启（用户拍板：不自动拉起任何程序） ---------- */
    {
        Ver now;
        wchar_t text[256];
        if (cfg_local_version(g_ctx.root, &now)) {
            wchar_t nowtext[32];
            ver_format(&now, nowtext, 32);
            _snwprintf(text, 256,
                L"更新完成，现在是 %ls。\n请关闭本窗口后运行 start.bat 重新启动游戏。",
                nowtext);
        } else {
            wcscpy(text, L"更新完成。\n请关闭本窗口后运行 start.bat 重新启动游戏。");
        }
        text[255] = 0;
        ui_progress_total(100);
        ui_progress_current(100);
        finish_ok(text);
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/*  参数解析 / 单实例锁 / 入口                                            */
/* ------------------------------------------------------------------ */

static DWORD parse_procid(const wchar_t *cmdline)
{
    const wchar_t *p = wcsstr(cmdline, L"-procid:");
    DWORD value = 0;
    int digits = 0;
    if (!p) return 0;
    p += 8;
    while (*p == L'\'' || *p == L'"' || *p == L' ') p++;
    while (*p >= L'0' && *p <= L'9' && digits < 10) {
        value = value * 10u + (DWORD)(*p - L'0');
        p++;
        digits++;
    }
    return digits ? value : 0;
}

static int arg_value(const wchar_t *cmdline, const wchar_t *flag,
                     wchar_t *out, size_t cap)
{
    const wchar_t *p = wcsstr(cmdline, flag);
    if (!p) return 0;
    p += wcslen(flag);
    while (*p == L' ') p++;
    out[0] = 0;
    if (*p == L'"') {
        p++;
        while (*p && *p != L'"' && wcslen(out) < cap - 1) out[wcslen(out)] = *p++;
    } else {
        while (*p && *p != L' ' && wcslen(out) < cap - 1) out[wcslen(out)] = *p++;
    }
    return out[0] != 0;
}

/* 单实例锁：logs\update.lock，600 秒 stale 抢占（python 版同款语义）。 */
typedef struct SingleLock {
    HANDLE file;
    wchar_t path[MAX_PATH * 2];
} SingleLock;

static int single_lock_acquire(SingleLock *lk, const wchar_t *root)
{
    path_join(lk->path, MAX_PATH * 2, root, L"logs");
    CreateDirectoryW(lk->path, NULL);
    wcscat(lk->path, L"\\update.lock");
    if (file_exists(lk->path)) {
        HANDLE f = CreateFileW(lk->path, GENERIC_READ, FILE_SHARE_READ, NULL,
                               OPEN_EXISTING, 0, NULL);
        if (f != INVALID_HANDLE_VALUE) {
            FILETIME write_ft, now_ft;
            ULONGLONG *w = (ULONGLONG *)&write_ft, *n = (ULONGLONG *)&now_ft;
            GetFileTime(f, NULL, NULL, &write_ft);
            CloseHandle(f);
            GetSystemTimeAsFileTime(&now_ft);
            if (*n >= *w && (*n - *w) / 10000 > 600000)
                DeleteFileW(lk->path);      /* 上次崩了没清，抢过来 */
        }
    }
    lk->file = CreateFileW(lk->path, GENERIC_WRITE, 0, NULL, CREATE_NEW,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    if (lk->file != INVALID_HANDLE_VALUE) {
        char pid_text[32];
        DWORD wrote;
        _snprintf(pid_text, sizeof(pid_text), "%lu",
                  (unsigned long)GetCurrentProcessId());
        WriteFile(lk->file, pid_text, (DWORD)strlen(pid_text), &wrote, NULL);
        return 1;
    }
    return GetLastError() == ERROR_ACCESS_DENIED ? -1 : 0;  /* 锁不上放行 */
}

static void single_lock_release(SingleLock *lk)
{
    if (lk->file != INVALID_HANDLE_VALUE && lk->file != NULL) {
        CloseHandle(lk->file);
        lk->file = INVALID_HANDLE_VALUE;
        DeleteFileW(lk->path);
    }
}

int WINAPI wWinMain(HINSTANCE me, HINSTANCE prev, PWSTR cmd, int show)
{
    wchar_t root[MAX_PATH * 2];
    Args *args = &g_ctx.args;
    const wchar_t *cmdline = GetCommandLineW();
    SingleLock lock;
    int lock_rc;
    HANDLE worker = NULL;
    char tagline[512];
    int noui_env = 0;
    char envbuf[8];

    (void)me; (void)prev; (void)cmd; (void)show;

    /* --- 参数 --------------------------------------------------------- */
    memset(&g_ctx, 0, sizeof(g_ctx));
    args->procid = parse_procid(cmdline);
    args->elevated = wcsstr(cmdline, L"--elevated") != NULL;
    args->noui = wcsstr(cmdline, L"--noui") != NULL;
    arg_value(cmdline, L"--zip ", args->zip, MAX_PATH * 2);
    arg_value(cmdline, L"--target-version ", args->target_version, 64);
    arg_value(cmdline, L"--manifest-url ", args->manifest_url, 1024);
    {
        wchar_t m[8];
        if (arg_value(cmdline, L"--ui-mode ", m, 8))
            args->ui_mode = _wtoi(m);
    }
    envbuf[0] = 0;
    if (GetEnvironmentVariableA("POPSHOT_UPDATER_NOUI", envbuf, sizeof(envbuf))
        && strcmp(envbuf, "0") != 0)
        noui_env = 1;
    if (wcsstr(cmdline, L"--selftest") || wcsstr(cmdline, L"--preview")) {
        extern int selftest_run(int preview);
        return selftest_run(wcsstr(cmdline, L"--preview") != NULL);
    }

    package_root(root, MAX_PATH * 2);
    wcscpy(g_ctx.root, root);
    log_init(root, "start");
    {
        char cmd8[1024];
        wide_to_utf8(cmdline, cmd8, sizeof(cmd8));
        _snprintf(tagline, sizeof(tagline), "%s elevated=%d procid=%lu cmd=%s",
                  UPDATER_TAG, args->elevated,
                  (unsigned long)args->procid, cmd8);
        tagline[sizeof(tagline) - 1] = 0;
        log_line("%s", tagline);
    }

    /* --- 本地版本 ------------------------------------------------------ */
    g_ctx.local_valid = cfg_local_version(root, &g_ctx.local);
    {
        wchar_t v[32];
        if (g_ctx.local_valid) {
            ver_format(&g_ctx.local, v, 32);
        } else
            wcscpy(v, L"?");
        log_line("local version %ls", v);
    }

    /* --- 单实例 -------------------------------------------------------- */
    lock.file = INVALID_HANDLE_VALUE;
    lock.path[0] = 0;
    lock_rc = single_lock_acquire(&lock, root);
    if (lock_rc == 0) {
        log_line("another updater instance running, exit");
        if (!args->noui && !noui_env)
            MessageBoxW(NULL, L"已经有一个更新程序在运行了。", L"自动更新",
                        MB_ICONINFORMATION);
        return 0;
    }

    /* --- UI + worker ---------------------------------------------------- */
    ui_init(root, args->ui_mode, args->noui || noui_env);
    if (!(args->noui || noui_env)) {
        if (!ui_window_create_patch()) {
            /* 窗口都建不起来（极端）——退无界面模式，更新照跑。 */
            log_line("ui window create failed, falling back to noui");
            ui_shutdown();
            ui_init(root, 0, 1);
        }
    }

    worker = CreateThread(NULL, 0, worker_main, NULL, 0, NULL);
    ui_pump_until_quit(worker);

    if (worker) {
        WaitForSingleObject(worker, 15000);
        CloseHandle(worker);
    }
    ui_shutdown();
    single_lock_release(&lock);
    return 0;
}
