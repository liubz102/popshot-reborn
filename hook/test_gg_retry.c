/*
 * GameGuard「绕过失败 -> 自动重来」回归夹具（V0.2 会话 15，§179）。
 *
 * 由 test-retry.bat 编译成临时 32 位进程，再通过正式 bsloader 注入 bshook.dll。
 * 夹具**故意不执行** 0x54b0fc（DR0 永远不会命中），而是像真客户端绕过失败时
 * 那样弹一个「Game guard文件不存在或已变更」的 MessageBoxW，然后退出。
 *
 *   前几次尝试（BSHOOK_GG_RETRY=1）：bshook 认出这个框 -> 吃掉它（不弹窗）
 *                                    -> 报 FAILED -> bsloader 自动重来
 *   最后一次   （BSHOOK_GG_RETRY=0）：框会真的弹出来，会卡住自动化测试，
 *                                    所以夹具这一次直接退出，不弹框
 *                                    -> bsloader 走「命中前就退出了」那条路
 *
 * 判据全在 bsloader 的输出里，见 test-retry.bat。
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "gg_bypass.h"

#define TARGET_PAGE ((LPVOID)0x0054B000u)

/* 和 test_gg_watchdog.c 同理：先把固定地址占住，别让 bshook 的蹦床占了。 */
static volatile unsigned char g_image_reservation[0x180000];

/* 等 bshook 把 MessageBoxW 的 hook 装上（watch_thread 第一轮就装）。
   READY 握手之后再多等一会儿最省事，反正夹具不赶时间。 */
static void wait_for_hooks(void)
{
    char name[128];
    DWORD n = GetEnvironmentVariableA(POPSHOT_BSHOOK_READY_ENV, name, sizeof(name));
    HANDLE ready;

    if (n > 0 && n < sizeof(name)) {
        ready = OpenEventA(SYNCHRONIZE, FALSE, name);
        if (ready) {
            WaitForSingleObject(ready, 5000);
            CloseHandle(ready);
        }
    }
    Sleep(800);
}

static int retry_allowed(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA(POPSHOT_BSHOOK_RETRY_ENV, buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

int main(void)
{
    static const unsigned char target_code[] = {
        0xE8, 0xCF, 0x60, 0x01, 0x00, /* call 0x005611d0 —— 只是让签名成立 */
        0xC3
    };
    DWORD old_protect;

    g_image_reservation[0] = 1;
    g_image_reservation[sizeof(g_image_reservation) - 1] = 1;
    if (!VirtualProtect(TARGET_PAGE, 0x1000, PAGE_EXECUTE_READWRITE, &old_protect))
        return 2;
    /* 摆上已知签名，好让 bsloader / bshook 正常武装 DR0 —— 我们要验的是
       「武装了但从来没命中」这条路，不是「等不到解壳」。 */
    CopyMemory((LPVOID)POPSHOT_GG_CHECK_VA, target_code, sizeof(target_code));
    FlushInstructionCache(GetCurrentProcess(), TARGET_PAGE, 0x1000);

    wait_for_hooks();

    if (retry_allowed()) {
        /* 真客户端在这里弹的就是这个框（V0.2 §134 抓到过原文）。 */
        MessageBoxW(NULL,
                    L"Game guard文件不存在或已变更，请重新安装Game guard。",
                    L"公告", MB_OK);
    }
    return 1;   /* 客户端绕过失败时也是非 0 退出 */
}
