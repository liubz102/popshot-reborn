/* --------------------------------------------------------------------------
   updater.c —— 自动更新引导器（占着 game_patched\BsPatcherChn.exe 的名字）

   为什么叫这个名字：原版客户端的升级分支（ServerConnection 虚表槽 12
   0x54dbf6 收到非零结果码）会按 locale 拉起 BsPatcherChn.exe（国服名，
   命令行模板见 V0.1 FINDINGS §14）。把我们的引导器放在这个路径上，
   客户端一行不用改、bshook 一个 hook 不用加，整条 NGM 死链
   （platform.tiancity.com / NGMDll.dll / IE 控件 UI）就被整个绕开了。
   原版 exe 在 git 历史里留着。

   玩家看到的样子（仿原版 NGM 的交互，2026-08-22 用户拍板）：

     客户端被版本门禁拒绝 → 弹框「游戏需要更新。自动更新需要管理员
     权限，是否允许？」→ 点「是」→ UAC 确认（显示「炮炮火枪手 自动
     更新」，见 updater.rc）→ 管理员控制台窗口跑 tools\update_client.py
     （下载/进度/速度/出错留窗全在那边）→ 点「否」/取消 UAC → 直接退出。

   实现要点：

   * **必须内嵌 asInvoker manifest**（updater.manifest）：文件名里的
     "Patcher" 会命中 Windows UAC「安装程序检测」启发式，没 manifest 的
     exe 被自动要求管理员 —— 客户端 CreateProcess 拉它必失败
     （ERROR_ELEVATION_REQUIRED，2026-08-22 真机定位到的「闪退」根因），
     手动双击也会莫名其妙弹 UAC。声明 asInvoker = 回到原版 NGM 的静默
     启动，要不要提权由我们自己问玩家。
   * 提权方式是 **runas 重跑自己**（--elevated）：UAC 框显示的是带中文名
     的本程序（elevate cmd.exe 会显示「Windows 命令处理程序」，问玩家
     「这是什么」）。提权后的实例直接拉工作进程，继承管理员身份。
   * 工作进程经 `cmd /s /c "… & pause"` 拉起：cmd 拥有控制台，无论
     python 怎么退（正常/异常/没起来）窗口都停在 pause，永不秒关。
   * 两个实例都**拉起后立即退出**，不等待任何进程（防死锁）。
   * POPSHOT_UPDATER_NOUI=1：跳过询问与提权、直接拉工作进程 —— 仅供
     自动化测试用（无头环境里 MessageBox 会永远挂着）。
   * 参数只透传 -procid（python 等游戏退出用），其余 NGM 参数全忽略。

   编译：tools\updater\build.bat（vcvars32，x86；rc 编译版本资源、
   manifest 嵌入，都在那边）。
   -------------------------------------------------------------------------- */

#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>      /* ShellExecuteW（runas 提权重跑自己） */
#include <string.h>
#include <stdio.h>
#include <wchar.h>

/* 字段调试用的身份标记（strings 一眼能认出这是我们的引导器）。 */
static const char UPDATER_TAG[] =
    "POPSHOT-UPDATER/2 (UAC-consent; replaces Nexon NGM bootstrap; tools/updater.c)";

/* Python 拉不起来时给玩家看的手动下载地址（和 tools/update_client.py
   的 RELEASES_PAGE 保持一致）。 */
static const wchar_t MANUAL_URL[] =
    L"https://github.com/liubz102/popshot-reborn/releases";

/* ------------------------------------------------------------------ */
/*  日志：<root>\logs\updater.log 追加一行（UTF-8）。只记关键行，      */
/*  排查「更新器没起来 / 起来就退」这类现场问题用。                    */
/* ------------------------------------------------------------------ */
static void write_log(const wchar_t *root, const char *msg)
{
    wchar_t path[MAX_PATH * 2];
    HANDLE f;
    SYSTEMTIME t;
    char line[2048];
    DWORD wrote;

    wcscpy(path, root);
    wcscat(path, L"\\logs");
    CreateDirectoryW(path, NULL);              /* 不在也不报错 */
    wcscat(path, L"\\updater.log");

    f = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                    OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return;
    SetFilePointer(f, 0, NULL, FILE_END);
    GetLocalTime(&t);
    _snprintf(line, sizeof(line), "[%04u-%02u-%02u %02u:%02u:%02u] %s\r\n",
              t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond, msg);
    line[sizeof(line) - 1] = 0;
    WriteFile(f, line, (DWORD)strlen(line), &wrote, NULL);
    CloseHandle(f);
}

static void die_msgbox(const wchar_t *root, const char *logmsg,
                       const wchar_t *reason)
{
    wchar_t text[1024];
    if (root && root[0]) write_log(root, logmsg);
    _snwprintf(text, 1024, L"%s\n\n请手动下载完整客户端(QQ群文件或Github)：\n%s",
               reason, MANUAL_URL);
    text[1023] = 0;
    MessageBoxW(NULL, text, L"更新失败", MB_ICONERROR | MB_OK);
}

/* ------------------------------------------------------------------ */
/*  包根：本 exe 在 <root>\game_patched\BsPatcherChn.exe，上跳一级。   */
/* ------------------------------------------------------------------ */
static void package_root(wchar_t *out, size_t cap)
{
    wchar_t *p;
    GetModuleFileNameW(NULL, out, (DWORD)cap);
    p = wcsrchr(out, L'\\'); if (p) *p = 0;    /* -> <root>\game_patched */
    p = wcsrchr(out, L'\\'); if (p) *p = 0;    /* -> <root>             */
}

/* ------------------------------------------------------------------ */
/*  系统「主版本号是否 >= 10」。                                        */
/*                                                                    */
/*  ★ 用 ntdll!RtlGetVersion，不用 GetVersionEx：后者对没有兼容性清单  */
/*    的进程谎报 6.2，Win10 上会错走 runtime-win7（判据等价于           */
/*    tools/wincompat.ps1 的 Get-WindowsBuildMajor）。                  */
/* ------------------------------------------------------------------ */
typedef LONG (WINAPI *RtlGetVersion_t)(void *);

static int windows_build_major(void)
{
    OSVERSIONINFOW info;
    RtlGetVersion_t fn;

    fn = (RtlGetVersion_t)GetProcAddress(
        GetModuleHandleA("ntdll.dll"), "RtlGetVersion");
    if (!fn) return 10;                       /* 拿不到就按新系统走 */
    ZeroMemory(&info, sizeof(info));
    info.dwOSVersionInfoSize = sizeof(info);
    if (fn(&info) != 0) return 10;
    return (int)info.dwMajorVersion;
}

/* ------------------------------------------------------------------ */
/*  -procid:'1234' -> 1234。原版命令行给这个值包一层单引号，双引号      */
/*  也顺手吃下；找不到 / 认不出返回 0（python 那边会自己按进程名找）。  */
/* ------------------------------------------------------------------ */
static DWORD parse_procid(const wchar_t *cmdline)
{
    const wchar_t *p = wcsstr(cmdline, L"-procid:");
    DWORD value = 0;
    int digits = 0;

    if (!p) return 0;
    p += 8;                                    /* 跳过 "-procid:" */
    while (*p == L'\'' || *p == L'"' || *p == L' ') p++;
    while (*p >= L'0' && *p <= L'9' && digits < 10) {
        value = value * 10u + (DWORD)(*p - L'0');
        p++;
        digits++;
    }
    return digits ? value : 0;
}

static int noui_mode(void)
{
    char buf[8];
    return GetEnvironmentVariableA("POPSHOT_UPDATER_NOUI",
                                   buf, sizeof(buf)) && strcmp(buf, "0") != 0;
}

/* ------------------------------------------------------------------ */
/*  拉工作进程：cmd /s /c "title … & python update_client.py & pause"。 */
/*  cmd 拥有新控制台、python 继承 —— 任何退出方式窗口都停在 pause，     */
/*  永不秒关。调用方（无论是否提权）拉起后立即返回。                    */
/* ------------------------------------------------------------------ */
static int spawn_worker(const wchar_t *root, const wchar_t *python,
                        const wchar_t *script, DWORD procid)
{
    wchar_t cmdbin[MAX_PATH * 2];
    wchar_t args[256];
    wchar_t cmdline[2100];
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;

    if (procid)
        _snwprintf(args, 256, L"--procid %lu", (unsigned long)procid);
    else
        args[0] = 0;
    GetSystemDirectoryW(cmdbin, MAX_PATH * 2);
    wcscat(cmdbin, L"\\cmd.exe");
    _snwprintf(cmdline, 2100,
               L"\"%s\" /s /c \"title PopShot Auto Update & \"%s\" \"%s\"%s%s & pause\"",
               cmdbin, python, script, args[0] ? L" " : L"", args);
    cmdline[2099] = 0;

    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));
    if (!CreateProcessW(cmdbin, cmdline, NULL, NULL, FALSE,
                        CREATE_NEW_CONSOLE, NULL, root, &si, &pi)) {
        char logmsg[1600];
        _snprintf(logmsg, sizeof(logmsg),
                  "FAIL CreateProcess err=%lu cmd=%ls",
                  (unsigned long)GetLastError(), cmdline);
        die_msgbox(root, logmsg,
                   L"更新程序启动失败（错误码见 logs\\updater.log）。");
        return 1;
    }
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return 0;
}

int WINAPI WinMain(HINSTANCE me, HINSTANCE prev, LPSTR cmdline_ansi, int show)
{
    wchar_t root[MAX_PATH * 2];
    wchar_t modern[MAX_PATH * 2], legacy[MAX_PATH * 2];
    wchar_t script[MAX_PATH * 2];
    wchar_t selfpath[MAX_PATH * 2];
    wchar_t params[512];
    wchar_t *python;
    char envbuf[32];
    char logmsg[1600];
    DWORD procid;
    int build, elevated;
    HINSTANCE h;

    (void)me; (void)prev; (void)cmdline_ansi; (void)show;

    package_root(root, MAX_PATH * 2);
    procid = parse_procid(GetCommandLineW());
    elevated = wcsstr(GetCommandLineW(), L"--elevated") != NULL;

    /* 第一行就落地：哪怕后面哪一步炸了，logs\updater.log 里也有迹可循。 */
    _snprintf(logmsg, sizeof(logmsg), "start %s elevated=%d cmd=%ls",
              UPDATER_TAG, elevated, GetCommandLineW());
    write_log(root, logmsg);

    /* --- 包完整性先于一切询问：答应了提权才发现包坏，体验最差 ------- */
    build = windows_build_major();
    envbuf[0] = 0;
    if (GetEnvironmentVariableA("POPSHOT_FORCE_LEGACY", envbuf, sizeof(envbuf))
            && strcmp(envbuf, "0") != 0) {
        build = 6;                             /* 强制走老运行时（对照测试用） */
    }
    wcscpy(modern, root); wcscat(modern, L"\\runtime\\python\\python.exe");
    wcscpy(legacy, root); wcscat(legacy, L"\\runtime-win7\\python\\python.exe");
    python = modern;
    if (build < 10 && GetFileAttributesW(legacy) != INVALID_FILE_ATTRIBUTES)
        python = legacy;
    if (GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES) {
        _snprintf(logmsg, sizeof(logmsg),
                  "FAIL python not found (root=%ls)", root);
        die_msgbox(root, logmsg,
                   L"找不到包里的 Python 运行时，客户端包不完整。");
        return 1;
    }
    wcscpy(script, root);
    wcscat(script, L"\\tools\\update_client.py");
    if (GetFileAttributesW(script) == INVALID_FILE_ATTRIBUTES) {
        _snprintf(logmsg, sizeof(logmsg),
                  "FAIL update_client.py missing (root=%ls)", root);
        die_msgbox(root, logmsg,
                   L"找不到 tools\\update_client.py，客户端包不完整。");
        return 1;
    }

    if (!elevated && !noui_mode()) {
        /* --- 问玩家（仿原版 NGM 的「运行需要管理者权限」） ------------- */
        if (MessageBoxW(NULL,
                L"游戏需要更新。\n\n自动更新需要管理员权限，是否允许？",
                L"自动更新", MB_ICONQUESTION | MB_YESNO) != IDYES) {
            write_log(root, "declined by user");   /* 取消 = 直接退出 */
            return 0;
        }

        /* --- runas 重跑自己（UAC 框显示 updater.rc 里的中文名） -------- */
        GetModuleFileNameW(NULL, selfpath, MAX_PATH * 2);
        if (procid)
            _snwprintf(params, 512, L"--elevated --procid %lu",
                       (unsigned long)procid);
        else
            wcscpy(params, L"--elevated");
        h = ShellExecuteW(NULL, L"runas", selfpath, params, root,
                          SW_SHOWNORMAL);
        if ((INT_PTR)h <= 32) {
            /* 返回 5 (SE_ERR_ACCESSDENIED) = 玩家在 UAC 框上点了「否」。
               按用户拍板：取消就安静退出，别再弹别的框。其余才是真错误。 */
            _snprintf(logmsg, sizeof(logmsg),
                      "runas self relaunch rc=%d", (int)(INT_PTR)h);
            write_log(root, logmsg);
            if ((INT_PTR)h != 5) {
                die_msgbox(root, logmsg,
                           L"更新程序没能以管理员身份启动"
                           L"（错误码见 logs\\updater.log）。");
            }
            return 0;
        }
        write_log(root, "elevated relaunch spawned");
        return 0;                              /* 两个实例都不等待 */
    }

    /* --- 提权过的自己（或 NOUI 测试路径）：直接拉工作进程 ------------- */
    {
        int rc = spawn_worker(root, python, script, procid);
        if (rc == 0)
            write_log(root, elevated ? "spawn (elevated)" : "spawn (noui)");
        return rc;
    }
}
