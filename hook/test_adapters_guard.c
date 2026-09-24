/*
 * GetAdaptersInfo 护栏 —— 回归夹具（bug调查/27）。
 *
 * 由 test-adapters-guard.bat 编译成一个 32 位控制台程序，独立跑，不需要游戏、
 * 不需要注入。**直接 `#include "adapters_guard.h"`**，跑的就是 bshook.dll 里
 * 那一份 `det_GetAdaptersInfo`，不是抄一份。
 *
 * ## 要守住的是什么
 *
 * BigShot.exe 的 `0x0040F7E6` 在栈上开 0x1900 字节（正好 10 个
 * IP_ADAPTER_INFO），调 `GetAdaptersInfo(buf, &len)`，**既不清零缓冲区、
 * 也不看返回值**，回来就顺着 `Next` 遍历。玩家机器上有 11 块网卡 ⇒ API 返回
 * ERROR_BUFFER_OVERFLOW 且一个字节都不写 ⇒ 客户端把未初始化的栈当链表走
 * ⇒ `0040F85F mov esi,[esi]` 吃 C0000005。16 份 minidump 全崩在这一条，
 * ESI = 0x00100081（一个 HWND），那片栈里读得出搜狗输入法的词库路径。
 *
 *   A  API 成功                         —— 护栏必须完全不插手
 *   B  ★ 崩溃现场：11 块网卡 / 10 块的缓冲区，不装护栏
 *   C  ★ 同一个现场，装上护栏 —— 必须不崩，且拿到前 10 块的 IP
 *   D  API 以别的失败码回来（没网卡）   —— 头一项清零，遍历空转收场
 *   E  调用方不是游戏主模块             —— 原样透传，缓冲区一个字节都不许动
 *   F  结构体布局                       —— 640 字节 / IP 字符串在 +0x1B0
 *   G  真的挂上真的 IPHLPAPI 跑一次     —— 装得上、拿得回来
 *   H  ★ 真 API + 修复前的客户端行为    —— 本机网卡顶够 10 块后，这一格必崩
 *
 * 判据全是「遍历拿到几个 IP / 有没有吃到访问违例」，不是看日志。
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <intrin.h>

void bslog(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    printf("    [hook] ");
    vprintf(fmt, ap);
    printf("\n");
    va_end(ap);
}

/* adapters_guard.h 认「调用方是不是游戏自己」靠这两个。夹具里就是夹具自己。 */
static UINT_PTR g_mod_lo = 0, g_mod_hi = 0;

#include "insn_reloc.h"
#include "adapters_guard.h"

static int g_fails = 0;

static void check(int ok, const char *what)
{
    printf("  %s  %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) ++g_fails;
}

static void check_eq(unsigned long got, unsigned long want, const char *what)
{
    if (got == want) printf("  PASS  %s (= %lu)\n", what, got);
    else { printf("  FAIL  %s: 得到 %lu, 应为 %lu\n", what, got, want); ++g_fails; }
}

/* -------------------------------------------------------------------------- */
/* 假的 GetAdaptersInfo —— 学真 API 的两条关键行为                             */
/*   · 缓冲区不够：**一个字节都不写**，只把所需大小写回 *size，返回 OVERFLOW   */
/*   · 够用：就地建链，内部指针（Next / CurrentIpAddress）全指进这块缓冲区     */
/* -------------------------------------------------------------------------- */
static ULONG g_fake_count = 11;          /* 玩家那台机器实测 11 块 */
static ULONG g_fake_rc_when_empty = ERROR_NO_DATA;

static void set_ip(char *dst, unsigned i)
{
    dst[0] = '1'; dst[1] = '0'; dst[2] = '.';
    dst[3] = '0'; dst[4] = '.'; dst[5] = '0'; dst[6] = '.';
    dst[7] = (char)('1' + (i % 9));
    dst[8] = 0;
}

static ULONG WINAPI fake_GetAdaptersInfo(ipa_info_t *buf, ULONG *size)
{
    ULONG need, i;

    if (!size) return ERROR_INVALID_PARAMETER;
    if (g_fake_count == 0) return g_fake_rc_when_empty;

    need = g_fake_count * (ULONG)sizeof(ipa_info_t);
    if (!buf || *size < need) { *size = need; return ERROR_BUFFER_OVERFLOW; }

    memset(buf, 0, need);
    for (i = 0; i < g_fake_count; i++) {
        set_ip(buf[i].IpAddressList.IpAddress, i);
        buf[i].CurrentIpAddress = &buf[i].IpAddressList;   /* ← 护栏必须截断它 */
        buf[i].Next = (i + 1 < g_fake_count) ? &buf[i + 1] : NULL;
    }
    *size = need;
    return NO_ERROR;
}

/* -------------------------------------------------------------------------- */
/* 客户端那个循环的 C 等价物（0040F81E .. 0040F863）                           */
/*                                                                            */
/*     L:  lea eax,[esi+0x1b0] / call inet_addr / test eax,eax / je +          */
/*         inc edi                                                            */
/*         mov esi,[esi]        ← esi = esi->Next                             */
/*         test esi,esi / jne L                                               */
/*                                                                            */
/* inet_addr 对一串垃圾返回 INADDR_NONE（非 0），所以这里「首字节非 0 就算数」  */
/* 和真现场等价。返回数到了几个 IP；吃到访问违例就返回 -1。                    */
/* -------------------------------------------------------------------------- */
static int walk_like_client(ipa_info_t *buf)
{
    ipa_info_t *esi = buf;
    int edi = 0;

    __try {
        while (edi < 10) {
            if (esi->IpAddressList.IpAddress[0] != 0) edi++;
            esi = esi->Next;
            if (!esi) break;
        }
        return edi;
    }
    __except (GetExceptionCode() == EXCEPTION_ACCESS_VIOLATION
              ? EXCEPTION_EXECUTE_HANDLER : EXCEPTION_CONTINUE_SEARCH) {
        return -1;
    }
}

/* 崩溃现场的那 0x1900 字节栈：客户端没清零，里面是别人留下的东西。
   照 dump 复刻 —— 头一项的 Next 是个野值，顺着它走第二跳才真的踩空。 */
static void make_crash_scene(ipa_info_t *buf)
{
    unsigned char *page;

    memset(buf, 0xCC, 10 * sizeof(ipa_info_t));
    /* 玩家现场 ESI = 0x00100081：那一页碰巧读得到（+0x1B0 的「IP 字符串」
       读出来是垃圾，inet_addr 返回 INADDR_NONE，EDI 还 ++ 了一次），
       再 `mov esi,[esi]` 才跳进真正没映射的地方。这里用一页 0xCC 复刻
       同一个形状：读得到、读出来还是野指针。 */
    page = (unsigned char *)VirtualAlloc(NULL, 0x1000, MEM_COMMIT | MEM_RESERVE,
                                         PAGE_READWRITE);
    if (page) memset(page, 0xCC, 0x1000);
    buf[0].Next = (ipa_info_t *)page;
}

/* -------------------------------------------------------------------------- */

static void case_a(void)
{
    static ipa_info_t buf[10];
    ULONG size = sizeof(buf);
    ULONG rc;

    printf("[A] API 成功时护栏不插手\n");
    g_fake_count = 3;
    s_GetAdaptersInfo = fake_GetAdaptersInfo;
    memset(buf, 0xCC, sizeof(buf));

    rc = det_GetAdaptersInfo(buf, &size);
    check_eq(rc, NO_ERROR, "返回 NO_ERROR");
    check_eq(size, 3 * sizeof(ipa_info_t), "*size = 3 块的大小");
    check_eq((unsigned long)walk_like_client(buf), 3, "客户端遍历数到 3 个 IP");
    check(buf[0].CurrentIpAddress == &buf[0].IpAddressList,
          "成功路径没去动 CurrentIpAddress");
}

static void case_b(void)
{
    static ipa_info_t buf[10];
    int n;

    printf("[B] ★崩溃现场：客户端不看返回值，直接走未初始化的栈\n");
    make_crash_scene(buf);
    n = walk_like_client(buf);
    check(n == -1, "不装护栏 ⇒ 访问违例（EXCEPTION_ACCESS_VIOLATION）");
    if (n != -1) printf("        （这次没崩，数到 %d —— 栈垃圾的内容说了算，"
                        "这正是玩家「有时又能进」的由来）\n", n);
}

static void case_c(void)
{
    static ipa_info_t buf[10];
    ULONG size = (ULONG)sizeof(buf);     /* 0x1900，和客户端一样 */
    ULONG rc;
    int n;

    printf("[C] ★同一个现场 + 护栏：11 块网卡 / 10 块的缓冲区\n");
    g_fake_count = 11;
    s_GetAdaptersInfo = fake_GetAdaptersInfo;
    make_crash_scene(buf);               /* 先把栈垃圾摆回去 */

    rc = det_GetAdaptersInfo(buf, &size);
    check_eq(rc, NO_ERROR, "护栏把 ERROR_BUFFER_OVERFLOW 兜成 NO_ERROR");
    check_eq(size, 10 * sizeof(ipa_info_t), "*size = 装得下的 10 块");

    n = walk_like_client(buf);
    check(n != -1, "客户端遍历不再崩");
    check_eq((unsigned long)n, 10, "数到 10 个 IP（功能没降级）");
    check(buf[9].Next == NULL, "最后一项的 Next 收成 NULL");
    check(buf[0].Next == &buf[1], "链子落在调用方缓冲区里，不是别人家的堆");
    check(buf[0].CurrentIpAddress == NULL,
          "CurrentIpAddress 被截断（不截就是指向已 free 的堆）");
    check(buf[0].IpAddressList.IpAddress[0] == '1', "第 1 块网卡的 IP 搬进来了");
}

static void case_d(void)
{
    static ipa_info_t buf[10];
    ULONG size = (ULONG)sizeof(buf);
    ULONG rc;
    int n;

    printf("[D] API 以别的失败码回来（一块网卡都没有）\n");
    g_fake_count = 0;
    g_fake_rc_when_empty = ERROR_NO_DATA;
    s_GetAdaptersInfo = fake_GetAdaptersInfo;
    make_crash_scene(buf);

    rc = det_GetAdaptersInfo(buf, &size);
    check_eq(rc, ERROR_NO_DATA, "失败码原样返回（没假装成功）");
    n = walk_like_client(buf);
    check(n != -1, "客户端遍历不崩");
    check_eq((unsigned long)n, 0, "数到 0 个 IP，循环第一轮就收场");
}

static void case_e(void)
{
    static ipa_info_t buf[10];
    ULONG size = (ULONG)sizeof(buf);
    ULONG rc;
    UINT_PTR save_lo = g_mod_lo, save_hi = g_mod_hi;

    printf("[E] 调用方不是游戏主模块（输入法 / 显卡 / 安全软件）—— 原样透传\n");
    g_fake_count = 11;
    s_GetAdaptersInfo = fake_GetAdaptersInfo;
    memset(buf, 0xCC, sizeof(buf));

    /* 把「主模块」挪到别处，这次调用的返回地址就落在范围外了。 */
    g_mod_lo = 0x00010000u; g_mod_hi = 0x00011000u;
    rc = det_GetAdaptersInfo(buf, &size);
    g_mod_lo = save_lo; g_mod_hi = save_hi;

    check_eq(rc, ERROR_BUFFER_OVERFLOW, "失败码原样透传，没被兜住");
    check_eq(size, 11 * sizeof(ipa_info_t),
             "*size 还是真 API 写回的所需大小（两步法不能被破坏）");
    check(*(unsigned char *)buf == 0xCC, "调用方缓冲区一个字节都没被动过");
}

static void case_f(void)
{
    printf("[F] 结构体布局 —— 客户端那段机器码按这两个数算的\n");
    check_eq((unsigned long)sizeof(ipa_info_t), 640, "sizeof(IP_ADAPTER_INFO)");
    check_eq((unsigned long)FIELD_OFFSET(ipa_info_t, IpAddressList.IpAddress), 0x1B0,
             "IpAddressList.IpAddress 的偏移（lea eax,[esi+0x1b0]）");
    check_eq((unsigned long)sizeof(ipa_str_t), 40, "sizeof(IP_ADDR_STRING)");
    check_eq(10 * (unsigned long)sizeof(ipa_info_t), 0x1900,
             "客户端栈缓冲区 0x1900 = 10 块");
}

static void case_g(void)
{
    static ipa_info_t buf[10];
    ULONG size = (ULONG)sizeof(buf);
    ULONG rc, need;

    GetAdaptersInfo_t entry;
    HMODULE iph;

    printf("[G] 真的挂上本机的 IPHLPAPI，真的调一次\n");
    iph = LoadLibraryA("IPHLPAPI.DLL");  /* 夹具自己不导入它 */
    check(iph != NULL, "IPHLPAPI.DLL 加载得上");
    if (!iph) return;

    s_GetAdaptersInfo = NULL;            /* 让 install_iphlpapi_hook 真去装 */
    install_iphlpapi_hook();
    check(s_GetAdaptersInfo != NULL, "GetAdaptersInfo 钩得上");
    if (!s_GetAdaptersInfo) return;

    /* 走导出函数的入口（头 5 字节已被换成 E9 → det_GetAdaptersInfo），
       和客户端调它的路径完全一样；不链 iphlpapi.lib。 */
    entry = (GetAdaptersInfo_t)GetProcAddress(iph, "GetAdaptersInfo");

    /* ★ 先用 NULL 探一次**真实所需大小**。
       不能拿成功路径回来的 `*size` 当需求量 —— `GetAdaptersInfo` 成功时
       **不改写** `*pOutBufLen`，读到的还是你自己传进去的那个数。
       （本夹具第一版就是这么误读的，还据此得出「本机 10 块网卡」的错结论。） */
    need = 0;
    rc = entry(NULL, &need);
    printf("        本机所需 %lu 字节 = %lu 块网卡（+%lu 个额外 IP 节点）；"
           "客户端给 %lu ⇒ %s\n",
           (unsigned long)need, (unsigned long)(need / sizeof(ipa_info_t)),
           (unsigned long)((need % sizeof(ipa_info_t)) / sizeof(ipa_str_t)),
           (unsigned long)sizeof(buf),
           need > sizeof(buf) ? "★命中 bug调查/27 的条件" : "够用，不触发");
    check(rc == ERROR_BUFFER_OVERFLOW || rc == ERROR_NO_DATA,
          "探大小返回 ERROR_BUFFER_OVERFLOW");

    size = (ULONG)sizeof(buf);
    rc = entry(buf, &size);
    printf("        取回 rc=%lu\n", (unsigned long)rc);
    check(rc == NO_ERROR || rc == ERROR_NO_DATA,
          "经过钩子后拿到的是正常结果（需求超了也该被护栏兜成 NO_ERROR）");
    check(walk_like_client(buf) != -1, "客户端那样遍历不崩");
    if (need > sizeof(buf))
        check(walk_like_client(buf) == (int)(sizeof(buf) / sizeof(ipa_info_t)),
              "★真·overflow 环境下拿到满额 10 块");
}

/* -------------------------------------------------------------------------- */
/* H —— ★ 修复前的客户端，一比一：真 API + 栈不清零 + 返回值不看              */
/*                                                                            */
/* 绕开护栏直接走蹦床 `s_GetAdaptersInfo`（= 没打补丁时客户端调到的那个真身）。 */
/* 这一格的结果**由本机环境决定**，夹具照着分流，不硬判：                      */
/*   · 所需字节 > 6400（网卡够多）⇒ API 一个字节不写 ⇒ 遍历栈垃圾 ⇒ 应当崩    */
/*   · 所需字节 ≤ 6400          ⇒ API 正常填 ⇒ 不崩（本机默认就是这种）      */
/* 要在本机看到前一种，得先把网卡数顶上去（见 bug调查/27 的「复现」一节）。    */
/* -------------------------------------------------------------------------- */
static void case_h(void)
{
    static ipa_info_t buf[10];
    ULONG size = (ULONG)sizeof(buf);
    ULONG rc, need = 0;
    int n;

    printf("[H] ★修复前的客户端一比一：真 API + 不清零 + 不看返回值\n");
    if (!s_GetAdaptersInfo) { printf("  SKIP  钩子没装上（场景 G 先跑）\n"); return; }

    s_GetAdaptersInfo(NULL, &need);
    make_crash_scene(buf);                    /* 把「没清零的栈」摆回去 */
    rc = s_GetAdaptersInfo(buf, &size);       /* ← 不经护栏，客户端原本的调用 */
    n = walk_like_client(buf);
    printf("        所需 %lu / 给了 %lu ⇒ rc=%lu，遍历结果 %d\n",
           (unsigned long)need, (unsigned long)sizeof(buf),
           (unsigned long)rc, n);

    if (need > sizeof(buf)) {
        check(rc == ERROR_BUFFER_OVERFLOW, "真 API 返回 ERROR_BUFFER_OVERFLOW");
        check(n == -1, "★复现：不装护栏 ⇒ 客户端遍历吃到访问违例");
        if (n != -1)
            printf("        （这次没崩，数到 %d —— 栈里恰好没有能走下去的野指针，"
                   "这正是玩家「有时又能进」的由来）\n", n);
    } else {
        printf("  SKIP  本机网卡还不够多，触发不了 —— 这一格只在顶够网卡后有意义\n");
        check(rc == NO_ERROR || rc == ERROR_NO_DATA, "够用时真 API 正常返回");
    }
}

int main(void)
{
    HMODULE self = GetModuleHandleA(NULL);
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)self;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)((BYTE *)self + dos->e_lfanew);

    g_mod_lo = (UINT_PTR)self;
    g_mod_hi = g_mod_lo + nt->OptionalHeader.SizeOfImage;

    printf("== GetAdaptersInfo 护栏 回归（bug调查/27）==\n");
    printf("   夹具主模块 %08X..%08X\n\n", (unsigned)g_mod_lo, (unsigned)g_mod_hi);

    case_a(); printf("\n");
    case_b(); printf("\n");
    case_c(); printf("\n");
    case_d(); printf("\n");
    case_e(); printf("\n");
    case_f(); printf("\n");
    case_g(); printf("\n");
    case_h(); printf("\n");

    if (g_fails) { printf("== FAILED: %d 项不过 ==\n", g_fails); return 1; }
    printf("== ALL PASS ==\n");
    return 0;
}
