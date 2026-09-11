/* --------------------------------------------------------------------------
   main.c —— 更新流程编排（全逻辑都在这个 exe 里，python 完全退出更新链）。

   玩家看到的样子（原版 BsPatcherChn/NGM 的交互）：
     客户端被版本门禁拒绝 -> 拉起 game_patched\BsPatcherChn.exe（本程序）->
     原版风格更新窗口（双进度条「目前/全部」）自动跑：检查（manifest 直连
     5 秒没取到就随机换 config\update.config 里的代理取）-> 测速选源
     （GitHub 直连 vs 同一份代理列表，speedtest.c）-> 下载 ->
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
     --check-proxies   代理体检：config\update.config 里每个代理真连 2 秒，
                       报「连不连得上 / 出不出数据 / 多快」（控制台 + 日志）
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
#include "speedtest.h"
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
    ProxyList proxies;            /* config\update.config，worker 开头读一次 */
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

/* manifest 走代理兜底（用户 2026-09-07 第二条，不测速）：直连 5 秒没取到 →
   随机挑代理逐个试（每个 5 秒）→ 全败才报「取不到更新清单」走手动下载提示。
   编排在 speedtest.c: proxy_fetch_fallback()，这里只提供一次取件和界面文字。 */

typedef struct ManifestFetch {
    char *buf;
    size_t cap;
    size_t len;
} ManifestFetch;

static int manifest_try(void *user, const wchar_t *url, wchar_t *err,
                        size_t err_cap)
{
    ManifestFetch *mf = (ManifestFetch *)user;
    mf->len = 0;
    if (net_get_memory(url, mf->buf, mf->cap, &mf->len, MANIFEST_ATTEMPT_MS,
                       ui_cancel_requested, err, err_cap))
        return 1;
    if (wide_ieq(err, L"expired"))
        _snwprintf(err, err_cap, L"%d 秒内没取到", MANIFEST_ATTEMPT_MS / 1000);
    err[err_cap - 1] = 0;
    return 0;
}

static void manifest_notify(void *user, int attempt, int total, int index)
{
    wchar_t text[400];
    (void)user;
    if (index < 0) {
        ui_status(L"正在获取更新清单……");
        return;
    }
    _snwprintf(text, 400,
               L"正在获取更新清单……直连 GitHub 没取到，正在尝试第 %d/%d 个代理：%ls",
               attempt - 1, total, g_ctx.proxies.url[index]);
    text[399] = 0;
    ui_status(text);
}

static int fetch_manifest(Manifest *m, wchar_t *err, size_t err_cap)
{
    const wchar_t *url = g_ctx.args.manifest_url[0]
                             ? g_ctx.args.manifest_url : MANIFEST_URL;
    static char buf[262144];
    ManifestFetch mf;
    wchar_t net_err[256];
    int picked = -1, attempts = 0;
    unsigned seed = (unsigned)GetTickCount64() ^ (GetCurrentProcessId() << 16);

    mf.buf = buf;
    mf.cap = sizeof(buf);
    mf.len = 0;
    if (!proxy_fetch_fallback(L"manifest", url, &g_ctx.proxies, seed,
                              manifest_try, manifest_notify, &mf,
                              &picked, &attempts, net_err, 256)) {
        if (wide_ieq(net_err, L"cancelled")) {
            _snwprintf(err, err_cap, L"cancelled");
            return 0;
        }
        if (attempts > 1)
            _snwprintf(err, err_cap,
                       L"取不到更新清单：GitHub 直连和 %d 个代理都没取到"
                       L"（最后一次：%ls）", attempts - 1, net_err);
        else
            _snwprintf(err, err_cap, L"取不到更新清单（%ls）", net_err);
        err[err_cap - 1] = 0;
        return 0;
    }
    if (!manifest_parse(buf, m)) {
        _snwprintf(err, err_cap, L"更新清单内容认不出");
        return 0;
    }
    return 1;
}

/* `force` = 服务器**明确拒了我们**（探针回 REJECTED）。
   ★★ 这时「本地版本号已经够新」**不能**再当成「无需更新」——
   服务器看过这个客户端之后说的不行，版本号一样只说明**文件被改过**
   （hook 完整性校验，D85）。原来那条 `local >= target -> return NULL` 会让
   被篡改的客户端永远停在「已是最新版本，无需更新」，谁也修不好它。
   重装同一个版本正好把被改掉的文件覆盖回去，属于自愈。 */
static const ReleaseEntry *pick_target(const Manifest *m, const Ver *wanted,
                                       int force, int *need_update)
{
    int i;
    *need_update = 0;
    if (wanted) {
        for (i = 0; i < m->count; i++)
            if (ver_cmp(&m->entries[i].version, wanted) == 0) {
                if (!force && g_ctx.local_valid &&
                    ver_cmp(&g_ctx.local, &m->entries[i].version) >= 0)
                    return NULL;               /* 服务器要的版本本地已有 */
                *need_update = 1;
                return &m->entries[i];
            }
        {
            wchar_t wtext[32];
            ver_format(wanted, wtext, 32);
            log_line("manifest has no server-wanted %ls, use newest", wtext);
        }
    }
    /* 最新版 = releases[0]（update_manifest.py 前插）。 */
    if (!force && g_ctx.local_valid &&
        ver_cmp(&g_ctx.local, &m->entries[0].version) >= 0)
        return NULL;
    *need_update = 1;
    return &m->entries[0];
}

/* ------------------------------------------------------------------ */
/*  进度（用户拍板 V0.2 会话 50）：                                    */
/*    「目前」= 当前步骤自己的进度（下载按字节、覆盖按文件数）；        */
/*    「全部」= 整个更新流程的加权总进度（探针/清单→下载→等游戏退出→   */
/*              停服务端→解压覆盖）。                                  */
/* ------------------------------------------------------------------ */

/* 「全部」条的分段里程碑（百分比）。 */
#define PHASE_AFTER_MANIFEST  2    /* 探针 + 清单拿齐、目标已定 */
#define PHASE_AFTER_DOWNLOAD  60   /* 下载 + sha256 校验完成 */
#define PHASE_AFTER_GAMEEXIT  62   /* 游戏退出（等/强杀）完成 */
#define PHASE_AFTER_STOPS     64   /* 本机服务端/中继停干净 */
#define PHASE_AFTER_APPLY     99   /* staging 解压 + 覆盖完成（100=收尾） */

/* 某步骤内 frac(0-100) 折算成「全部」条的读数。 */
static void overall_from(int step_base, int step_end, int frac)
{
    int percent = step_base + (step_end - step_base) * frac / 100;
    if (percent > 100) percent = 100;
    ui_progress_total(percent);
}

/* ------------------------------------------------------------------ */
/*  下载（进度/速度/剩余时间；「目前」=下载字节%、「全部」=全流程）      */
/* ------------------------------------------------------------------ */

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
    overall_from(PHASE_AFTER_STOPS, PHASE_AFTER_APPLY, percent);
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
            ui_progress_current(percent);
            overall_from(PHASE_AFTER_MANIFEST, PHASE_AFTER_DOWNLOAD, percent);
        }
        elapsed = now - du->started;
        if (elapsed > 800 && done) {
            unsigned long long speed = done * 1000 / elapsed;   /* B/s */
            unsigned long long eta = (total - done) / (speed ? speed : 1);
            wchar_t got[32], spd[32], remain[128];
            mib_to_wide(done, got, 32);
            mib_to_wide(speed, spd, 32);
            _snwprintf(remain, 128,
                       L"已下载 %ls MiB  %ls MiB/s  剩余约 %llu 秒",
                       got, spd, (unsigned long long)(eta > 99999 ? 99999 : eta));
            remain[127] = 0;
            ui_remaining(remain);
        }
    }
    return 1;
}

/* ------------------------------------------------------------------ */
/*  测速选源（规则和编排在 speedtest.c；这里只提供 WinHTTP 探针和界面文字） */
/* ------------------------------------------------------------------ */

static void measure_source(void *user, const wchar_t *url, SpeedSample *out)
{
    (void)user;
    memset(out, 0, sizeof(*out));
    if (!net_probe_speed(url, SPEED_WINDOW_MS, SPEED_BUCKET_MS,
                         ui_cancel_requested, &out->bytes, &out->elapsed_ms,
                         &out->trace, out->note, 128))
        out->cancelled = 1;
}

static void speed_phase(void *user, int first, int count, int total)
{
    wchar_t text[160];
    (void)user;
    if (first < 0)
        _snwprintf(text, 160, L"正在测速：GitHub 直连（%d 秒）",
                   SPEED_WINDOW_MS / 1000);
    else
        _snwprintf(text, 160,
                   L"正在测速：第 %d~%d 个代理（共 %d 个，每组 %d 秒）",
                   first + 1, first + count, total, SPEED_WINDOW_MS / 1000);
    text[159] = 0;
    ui_remaining(text);
}

/* 选下载源：直连 or 代理拼前缀。返回 1 = url/label 有效；0 = 玩家取消。 */
static int choose_download_source(const ReleaseEntry *e, wchar_t *url_out,
                                  size_t url_cap, wchar_t *label_out,
                                  size_t label_cap)
{
    const ProxyList *proxies = &g_ctx.proxies;   /* worker 开头读过 */
    SpeedPick pick;

    if (proxies->count > 0)
        ui_status(L"正在寻找最快的下载源......");
    if (!speedtest_pick(e->url, proxies, measure_source, speed_phase, NULL,
                        &pick))
        return 0;
    ui_remaining(NULL);
    if (pick.index < 0) {
        speed_compose_url(NULL, e->url, url_out, url_cap);
        _snwprintf(label_out, label_cap, L"直连Github");
    } else {
        speed_compose_url(proxies->url[pick.index], e->url, url_out, url_cap);
        _snwprintf(label_out, label_cap, L"%ls", proxies->url[pick.index]);
    }
    label_out[label_cap - 1] = 0;
    return 1;
}

static int fetch_zip_cached(const ReleaseEntry *e, wchar_t *zip_out,
                            size_t cap, wchar_t *err, size_t err_cap)
{
    wchar_t base[MAX_PATH], name[96];
    wchar_t dl_url[1200];                 /* 代理 256 + 原地址 512 */
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

    /* --- 测速选源（用户拍板 2026-09-07）：直连够快就直连，否则挑代理 ---
       在缓存判定之后：包已经在手就不必测速。 */
    {
        wchar_t label[PROXY_URL_CAP];
        wchar_t text[400];
        wchar_t size_text[32];
        if (!choose_download_source(e, dl_url, 1200, label, PROXY_URL_CAP)) {
            _snwprintf(err, err_cap, L"cancelled");
            return 0;
        }
        if (e->size)
            u64_to_wide((e->size + (1u << 20) - 1) >> 20, size_text, 32);
        else
            wcscpy(size_text, L"400");
        /* 状态行两行 455px（模板 CurrentTxt 覆盖样式，break-all）：最长的
           代理地址也放得下。用户要求直连也要写「代理地址：直连Github」。 */
        _snwprintf(text, 400,
                   L"正在下载客户端包（约 %ls MB），网络不通或太慢时可从QQ群文件手动下载。"
                   L"代理地址：%ls", size_text, label);
        text[399] = 0;
        ui_status(text);
    }
    log_line("download %ls", dl_url);
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
        ok = net_download_file(dl_url, zip_out,
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
    int i;

    (void)param;
    wcscpy(zip_path, g_ctx.args.zip);       /* 提权重跑的直通参数 */

    if (!zip_path[0]) {
        ProbeResult pr;
        wchar_t host[256];
        wchar_t hook_dll[MAX_PATH * 2];
        int need_update = 0;

        /* --- 代理列表：manifest 兜底和下载测速共用这一份 ---------------- */
        cfg_proxy_list(g_ctx.root, &g_ctx.proxies);
        log_line("proxy list: %d usable, %d lines ignored",
                 g_ctx.proxies.count, g_ctx.proxies.skipped);

        /* --- 探针：问服务器「该升到哪版」 ---------------------------- */
        ui_status(L"正在探测服务器，确认需要的版本……");
        cfg_server_address(g_ctx.root, host, 256);
        /* ★ 探针要和真客户端发**一模一样的 4 个字节**（版本号 + hook 完整性
           校验位，D85）—— 不然服务端会判它「没带校验位」，PROBE_OK 就永远
           不可能出现，「无需更新」那条路等于废掉。 */
        _snwprintf(hook_dll, MAX_PATH * 2, L"%ls\\hook\\bin\\bshook.dll",
                   g_ctx.root);
        hook_dll[MAX_PATH * 2 - 1] = 0;
        log_line("probe host %ls:%d (root=%ls)", host, POPSHOT_GAME_PORT,
                 g_ctx.root);
        probe_server(host, POPSHOT_GAME_PORT,
                     g_ctx.local_valid ? &g_ctx.local : NULL, hook_dll, &pr);
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
            if (wide_ieq(err, L"cancelled")) {
                finish_ok(L"已取消更新。可以关闭本窗口。");
                return 0;
            }
            finish_fail(err);
            return 1;
        }
        if (g_ctx.args.target_version[0]) {
            Ver forced;
            if (!ver_parse(g_ctx.args.target_version, &forced)) {
                finish_fail(L"--target-version 认不出（内部参数错误）");
                return 1;
            }
            /* --target-version 是提权后重跑自己带的，那时已经决定要更新了。 */
            target = pick_target(&manifest, &forced, 1, &need_update);
        } else if (pr.status == PROBE_REJECTED && pr.wanted_valid) {
            /* ★ force=1：服务器刚刚拒了我们，版本号一样也得重装（见 pick_target）。 */
            target = pick_target(&manifest, &pr.wanted, 1, &need_update);
        } else {
            /* 探针连不上 —— 没人说我们有问题，保持「已是最新就别动」。 */
            target = pick_target(&manifest, NULL,
                                 pr.status == PROBE_REJECTED, &need_update);
        }
        if (!target) {
            finish_ok(L"已是最新版本，无需更新。");
            return 0;
        }
        ver_format(&target->version, ver_text, 64);
        log_line("target %ls", ver_text);
        ui_announce_version(ver_text);
        ui_status(L"准备下载更新……");
        ui_progress_total(PHASE_AFTER_MANIFEST);

        /* --- 下载 + 校验（在提权之前：临时目录不需要管理员） ---------- */
        ui_set_stage(UI_STAGE_DOWNLOAD);
        ui_progress_current(0);
        if (!fetch_zip_cached(target, zip_path, MAX_PATH * 2, err, 512)) {
            if (wide_ieq(err, L"cancelled"))
                finish_ok(L"已取消更新。可以关闭本窗口。");
            else
                finish_fail(err);
            return 1;
        }
        ui_progress_current(100);
        ui_progress_total(PHASE_AFTER_DOWNLOAD);
    } else {
        /* --zip 直通：目标版本从参数/manifest 补齐（进度条目标）。 */
        if (g_ctx.args.target_version[0]) {
            wcsncpy(ver_text, g_ctx.args.target_version, 63);
            ver_text[63] = 0;
            ui_announce_version(ver_text);
        }
        ui_progress_total(PHASE_AFTER_DOWNLOAD);   /* 包已就手，视同下载完成 */
    }

    if (ui_cancel_requested()) {
        finish_ok(L"已取消更新。可以关闭本窗口。");
        return 0;
    }

    /* --- 等游戏退出（game_patched 的文件都被它锁着） ----------------- */
    ui_progress_current(0);
    {
        DWORD still[PROCS_MAX_PIDS];
        int still_count = 0;
        int rc;
        ui_status(L"正在等待游戏退出……");
        rc = procs_wait_game_exit(g_ctx.args.procid, still, &still_count);
        if (rc == 3) {
            finish_ok(L"已取消更新。可以关闭本窗口。");
            return 0;
        }
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
            {
                int gone = procs_wait_gone(still, still_count, 20, 250);
                if (gone == -1) {
                    finish_ok(L"已取消更新。可以关闭本窗口。");
                    return 0;
                }
                if (gone != 0) {
                    finish_fail(L"游戏进程结束不了，文件仍被占用。请手动关闭游戏后重试。");
                    return 1;
                }
            }
        } else if (rc == 2) {
            finish_fail(L"游戏进程结束不了，文件仍被占用。请手动关闭游戏后重试。");
            return 1;
        }
    }
    ui_progress_total(PHASE_AFTER_GAMEEXIT);

    /* --- 停本机服务端/中继（锁着 runtime\python\python.exe） ---------- */
    {
        int ended = 0;
        int stopped;
        ui_status(L"正在停止本机服务端……");
        stopped = procs_stop_package_pythons(g_ctx.root, &ended);
        if (stopped)
            log_line("stopped package pythons=%d", stopped);
        if (ended == 2) {
            finish_ok(L"已取消更新。可以关闭本窗口。");
            return 0;
        }
        if (ended) {
            finish_fail(L"本机服务端进程结束不了（runtime 仍被占用）。请手动关闭它的窗口后重试更新。");
            return 1;
        }
    }
    ui_progress_total(PHASE_AFTER_STOPS);

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
    ui_progress_current(0);
    {
        int moved = 0;
        int zip_bad = 0;
        ui_status(L"正在应用更新（写入文件）……");
        if (!apply_update(zip_path, g_ctx.root, apply_progress_cb, NULL,
                          &moved, &zip_bad, err, 512)) {
            if (zip_bad) {
                /* 缓存是按 sha256 复用的 —— sha 对但 zip 读不了时，
                   不丢缓存每次都原地卡死（真机踩坑 §239）。丢了逼重下。
                   ★ 手动下载地址不在这里重复：finish_fail 的公告页末尾
                   本来就固定带「请手动下载完整客户端(QQ群文件或Github)：
                   <Releases 地址>」区块（真机反馈：写两遍显得啰嗦）。 */
                DeleteFileW(zip_path);
                _snwprintf(err + wcslen(err), 512 - wcslen(err),
                           L"\n已丢弃下载缓存，重新运行更新器会重新下载。"
                           L"\n若反复失败请从QQ群文件或Github手动下载完整客户端"
                           L"（下载地址见下方）。");
                err[511] = 0;
            }
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

/* ------------------------------------------------------------------ */
/*  --check-proxies —— 代理体检（用户 2026-09-08 要「实测这些代理能不能下」）*/
/*  代理站点常换常挂，出问题时要能当场用**更新器自己的 WinHTTP 链路**       */
/*  （同一套 UA / 请求头 / TLS / 超时）逐个真连一次 —— curl 的头和 TLS 都   */
/*  不一样，结果不能直接当数。                                             */
/* ------------------------------------------------------------------ */

/* 每个来源真连这么久：够看出「连得上、在出数据、速度大概多少」，
   又不至于把玩家的流量喝掉太多（11 个来源 × 2 秒）。 */
#define CHECK_WINDOW_MS 2000

/* 控制台 + updater.log 双写。控制台走 WriteConsoleW（宽字符，不看代码页，
   中文不会乱）；被重定向到文件/管道时退回 UTF-8 字节。
   ★ GUI 子系统程序不一定拿得到继承来的 stdout（从 Git Bash 起就没有），
   那时借 AttachConsole 挂上的那个控制台自己开 CONOUT$。 */
static HANDLE check_out(void)
{
    static HANDLE h = NULL;
    if (!h) {
        h = GetStdHandle(STD_OUTPUT_HANDLE);       /* 继承来的管道 / 重定向文件 */
        if (!h || h == INVALID_HANDLE_VALUE) {
            AttachConsole(ATTACH_PARENT_PROCESS);  /* 没有就借父进程的控制台 */
            h = CreateFileW(L"CONOUT$", GENERIC_WRITE,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                            OPEN_EXISTING, 0, NULL);
        }
    }
    return (h == INVALID_HANDLE_VALUE) ? NULL : h;
}

static void check_say(const wchar_t *fmt, ...)
{
    wchar_t line[1024];
    HANDLE out = check_out();
    DWORD wrote = 0;
    va_list ap;

    va_start(ap, fmt);
    _vsnwprintf(line, 1024, fmt, ap);
    line[1023] = 0;
    va_end(ap);

    if (out && !WriteConsoleW(out, line, (DWORD)wcslen(line), &wrote, NULL)) {
        char utf8[2048];
        if (wide_to_utf8(line, utf8, sizeof(utf8)) >= 0)
            WriteFile(out, utf8, (DWORD)strlen(utf8), &wrote, NULL);
    }
    {   /* 日志里去掉行尾换行，log_line 自己会加 */
        wchar_t *nl = wcschr(line, L'\n');
        if (nl) *nl = 0;
        if (line[0]) log_line("check: %ls", line);
    }
}

/* 返回 0 = 这个源能用；1 = 不能用。 */
static int check_one(const wchar_t *label, const wchar_t *url)
{
    SpeedSample s;
    wchar_t peak[32], got[32];
    int ok;

    memset(&s, 0, sizeof(s));
    net_probe_speed(url, CHECK_WINDOW_MS, SPEED_BUCKET_MS, NULL,
                    &s.bytes, &s.elapsed_ms, &s.trace, s.note, 128);
    /* 窗口到点还在出数据（expired）或整包收完（complete）都算能下；
       其余（状态码非 200、连不上、TLS 失败）都是不能用。 */
    ok = s.bytes > 0 &&
         (wide_ieq(s.note, L"expired") || wide_ieq(s.note, L"complete"));
    mib_to_wide(speed_bps(&s), peak, 32);
    mib_to_wide(s.bytes, got, 32);
    check_say(L"%ls  %-38ls  %ls MiB  %ls MiB/s  (%ls)\n",
              ok ? L"[ OK ]" : L"[FAIL]", label, got, peak,
              s.note[0] ? s.note : L"-");
    return ok ? 0 : 1;
}

static int check_proxies_run(void)
{
    wchar_t root[MAX_PATH * 2];
    Manifest m;
    wchar_t err[512];
    const wchar_t *file_url;
    int i, bad = 0;

    /* ★ 这里别学 selftest 去 freopen("CONOUT$", stdout)：CRT 那一下会连
       STD_OUTPUT_HANDLE 一起改掉，`> out.txt` 和管道就全成了空文件。
       出口交给 check_out() 按序挑（继承的 stdout 优先）。 */
    package_root(root, MAX_PATH * 2);
    wcscpy(g_ctx.root, root);
    log_init(root, "start (--check-proxies)");
    ui_init(root, 0, 1);                    /* 无界面 */

    cfg_proxy_list(root, &g_ctx.proxies);
    check_say(L"=== 代理体检 ===\n");
    check_say(L"config\\update.config：%d 个可用，%d 行被忽略\n",
              g_ctx.proxies.count, g_ctx.proxies.skipped);

    if (!fetch_manifest(&m, err, 512)) {
        check_say(L"取不到更新清单，没法拿到测试地址：%ls\n", err);
        ui_shutdown();
        return 2;
    }
    file_url = m.entries[0].url;
    check_say(L"测试地址：%ls\n", file_url);
    check_say(L"每个来源真连 %d 秒（不下完，看得出连不连得上、出不出数据）\n\n",
              CHECK_WINDOW_MS / 1000);

    bad += check_one(L"直连 GitHub", file_url);
    for (i = 0; i < g_ctx.proxies.count; i++) {
        wchar_t url[1200];
        speed_compose_url(g_ctx.proxies.url[i], file_url, url, 1200);
        bad += check_one(g_ctx.proxies.url[i], url);
    }
    check_say(L"\n=== %d 个来源，%d 个不能用 ===\n",
              g_ctx.proxies.count + 1, bad);
    ui_shutdown();
    FreeConsole();
    return bad ? 1 : 0;
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
    if (wcsstr(cmdline, L"--check-proxies"))
        return check_proxies_run();

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
    procs_set_cancel_probe(ui_cancel_requested);   /* 停进程等态可被取消打断 */
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
