#ifndef POPSHOT_ADAPTERS_GUARD_H
#define POPSHOT_ADAPTERS_GUARD_H

/*
 * 启动期闪退护栏：`GetAdaptersInfo` 缓冲区不够时，客户端会去遍历栈垃圾。
 *
 * 从 bshook.c 里分出来的独立一份，**为了能单测**（bug调查/27）——
 * 夹具 hook\test_adapters_guard.c 直接 include 本文件，跑的就是线上这一份
 * 代码，不是抄一份。include 之前必须先有：
 *
 *     bslog(fmt, ...)            日志
 *     install_inline_hook(...)   insn_reloc.h
 *     g_mod_lo / g_mod_hi        主模块地址范围（判「调用方是不是游戏自己」）
 *
 * ★ 真正的病因见下面 det_GetAdaptersInfo 头上那段。
 */

/* -------------------------------------------------------------------------- */
/* IP_ADAPTER_INFO 的二进制布局                                                */
/*                                                                            */
/* 不 include <iphlpapi.h>：那会把 bshook.dll 拖上 iphlpapi.lib 的导入，而我们 */
/* 只需要 GetProcAddress 拿一个地址 + 知道结构体怎么摆。字段名照抄 MSDN。      */
/* -------------------------------------------------------------------------- */
#define IPA_STR_LEN   16    /* IP_ADDRESS_STRING / IP_MASK_STRING             */
#define IPA_NAME_LEN  260   /* MAX_ADAPTER_NAME_LENGTH + 4                    */
#define IPA_DESC_LEN  132   /* MAX_ADAPTER_DESCRIPTION_LENGTH + 4             */
#define IPA_ADDR_LEN  8     /* MAX_ADAPTER_ADDRESS_LENGTH                     */

typedef struct ipa_str_s {
    struct ipa_str_s *Next;
    char   IpAddress[IPA_STR_LEN];
    char   IpMask[IPA_STR_LEN];
    DWORD  Context;
} ipa_str_t;                            /* IP_ADDR_STRING，40 字节 */

typedef struct ipa_info_s {
    struct ipa_info_s *Next;            /* +0x000 ← 客户端遍历的就是这个字段  */
    DWORD      ComboIndex;
    char       AdapterName[IPA_NAME_LEN];
    char       Description[IPA_DESC_LEN];
    UINT       AddressLength;
    BYTE       Address[IPA_ADDR_LEN];
    DWORD      Index;
    UINT       Type;
    UINT       DhcpEnabled;
    ipa_str_t *CurrentIpAddress;
    ipa_str_t  IpAddressList;           /* +0x1AC，.IpAddress 落在 +0x1B0      */
    ipa_str_t  GatewayList;
    ipa_str_t  DhcpServer;
    BOOL       HaveWins;
    ipa_str_t  PrimaryWinsServer;
    ipa_str_t  SecondaryWinsServer;
    long       LeaseObtained;
    long       LeaseExpires;
} ipa_info_t;                           /* IP_ADAPTER_INFO，640 字节 */

/* 这两条是硬约束：客户端那段机器码**按 640 算个数、按 +0x1B0 取 IP 字符串**
   （见下面反汇编）。哪天编译器换了对齐、或者谁动了上面的字段，编译期就得停
   —— 运行期才发现就是又一次闪退。 */
typedef char ipa_assert_size_640[(sizeof(ipa_info_t) == 640) ? 1 : -1];
typedef char ipa_assert_ip_at_1b0[
    (FIELD_OFFSET(ipa_info_t, IpAddressList.IpAddress) == 0x1B0) ? 1 : -1];

typedef ULONG (WINAPI *GetAdaptersInfo_t)(ipa_info_t *, ULONG *);
static GetAdaptersInfo_t s_GetAdaptersInfo = NULL;

/* -------------------------------------------------------------------------- */
/* 日志去重：按「结果有没有翻转」，不按次数、不按时间窗                        */
/* -------------------------------------------------------------------------- */
static ULONG g_ipa_last_rc = 0xFFFFFFFFu;
static ULONG g_ipa_last_n  = 0xFFFFFFFFu;

static int ipa_state_changed(ULONG rc, ULONG n)
{
    if (rc == g_ipa_last_rc && n == g_ipa_last_n) return 0;
    g_ipa_last_rc = rc;
    g_ipa_last_n  = n;
    return 1;
}

/* -------------------------------------------------------------------------- */
/* det_GetAdaptersInfo —— bug调查/27 的真正病因就在这儿兜住                    */
/*                                                                            */
/* ★ 以下**不是推断，是 16 份 minidump 逐份实证的**：                          */
/*                                                                            */
/*   BigShot.exe 的 `0x0040F7E6` 那个函数负责收集本机所有网卡的 IP（加载页     */
/*   「网络初始化中」那一步）。它在**栈上**开 0x1900 字节 = 正好 10 个         */
/*   IP_ADAPTER_INFO（10 × 640），调 `GetAdaptersInfo(buf, &len)`，            */
/*   **既不先清零缓冲区、也不看返回值**，回来就直接遍历：                      */
/*                                                                            */
/*       0040F80F  mov  dword [ebp-0x7c], 0x1900   ; len = 10 个的大小         */
/*       0040F816  call GetAdaptersInfo            ; ← 返回值从此没人看        */
/*       0040F81B  lea  esi, [ebp-0x74]            ; esi = buf                 */
/*   L:  0040F81E  cmp  edi, 0xa                                              */
/*       0040F823  lea  eax, [esi+0x1b0]           ; &Adapter->IpAddressList   */
/*       0040F82A  call [inet_addr]                ;   .IpAddress              */
/*       ...                                                                  */
/*       0040F85F  mov  esi, [esi]                 ; esi = Adapter->Next  ←崩  */
/*       0040F861  test esi, esi                                              */
/*       0040F863  jne  L                                                     */
/*                                                                            */
/*   玩家机器上有 **11 块**网卡：dump 里 `*len` 被 API 写回 0x1B80 = 11 × 640，*/
/*   比给出去的 0x1900 大 —— 于是 GetAdaptersInfo 返回 ERROR_BUFFER_OVERFLOW   */
/*   并且**一个字节都不写**。那 6400 字节栈上原本躺着什么，就被当成链表遍历    */
/*   什么。16 份 dump 全崩在同一条 `mov esi,[esi]`，ESI = 0x00100081（一个     */
/*   HWND 值），而那片栈里能直接读出搜狗输入法的词库路径                       */
/*   `...\SogouPY\Users\...`（UTF-16）和一串 msctf / user32 的返回地址。       */
/*                                                                            */
/*   「开着搜狗就概率闪退、关掉就一直正常」到此闭环：输入法只是**在那片栈上    */
/*   留下非 0 垃圾**的那一方。没有它时那几页栈多半还是全 0，`mov esi,[esi]`    */
/*   第一轮读到 0，循环干净收场，谁也看不出毛病。                              */
/*                                                                            */
/*   ⇒ 这和内联钩子的相对跳转重定位（insn_reloc.h）**是两件事**。那个缺陷是    */
/*     真的、也该修，但它不是本次闪退的原因：崩溃落点在游戏模块内部            */
/*     （0040F85F，01:0000E85F BigShot.exe），不在蹦床、也不在模块之外。       */
/*                                                                            */
/* 护栏做两件事，都**只对游戏主模块自己发起的调用**生效 —— 输入法 / 显卡 /     */
/* 安全软件也会调这个 API，它们的「先传小缓冲区问所需大小」是标准用法，必须    */
/* 原样透传：                                                                  */
/*                                                                            */
/*   ① 保命底线：返回值不是 NO_ERROR 时，把调用方缓冲区的**头一个结构体**清零。*/
/*      `Next` 和 `IpAddressList.IpAddress` 都成了 0，客户端那个循环第一轮就    */
/*      正常结束。任何失败码都兜得住，不挑 ERROR_BUFFER_OVERFLOW。             */
/*   ② 功能不降级：失败码正好是 ERROR_BUFFER_OVERFLOW 时，再用一块够大的堆     */
/*      缓冲区取全量，把**前 N 块**（N = 调用方缓冲区装得下的个数）搬进去、    */
/*      `Next` 串成落在调用方缓冲区里的链、末尾置 NULL，返回 NO_ERROR。        */
/*      11 块网卡的玩家照样拿得到前 10 块的 IP。                               */
/* -------------------------------------------------------------------------- */
static ULONG WINAPI det_GetAdaptersInfo(ipa_info_t *buf, ULONG *size)
{
    UINT_PTR caller = (UINT_PTR)_ReturnAddress();
    ULONG in_size, rc, rc2, need, cap, n, bsz;
    ipa_info_t *big, *src, *dst;

    if (!s_GetAdaptersInfo) return ERROR_NOT_SUPPORTED;   /* 没装成就不该进来 */

    /* 不是游戏自己调的，或者参数本来就没给缓冲区 —— 原样透传。 */
    if (!buf || !size || caller < g_mod_lo || caller >= g_mod_hi)
        return s_GetAdaptersInfo(buf, size);

    in_size = *size;
    rc = s_GetAdaptersInfo(buf, size);
    if (rc == NO_ERROR) {
        if (ipa_state_changed(rc, 0))
            bslog("IPADAPT GetAdaptersInfo 正常返回（缓冲区 %lu 字节够用）",
                  (unsigned long)in_size);
        return rc;
    }

    /* ① 保命底线。只清头一个结构体：调用方要是正拿一块小缓冲区探大小，
          in_size 就是那块的真实大小，取小的那个才不会越界写。 */
    memset(buf, 0, in_size < sizeof(ipa_info_t) ? (size_t)in_size : sizeof(ipa_info_t));

    need = *size;                       /* overflow 时 API 写回的所需字节数 */
    cap  = in_size / sizeof(ipa_info_t);
    if (rc != ERROR_BUFFER_OVERFLOW || cap == 0 || need < sizeof(ipa_info_t)) {
        if (ipa_state_changed(rc, 0))
            bslog("IPADAPT !! GetAdaptersInfo 失败 rc=%lu（缓冲区 %lu 字节）——"
                  " 已把客户端缓冲区头一项清零，它那个不看返回值的遍历会空转收场"
                  "（bug调查/27）", (unsigned long)rc, (unsigned long)in_size);
        return rc;
    }

    /* ② 缓冲区不够而已 —— 玩家网卡比客户端预留的 %lu 块多。 */
    big = (ipa_info_t *)malloc((size_t)need);
    if (!big) {
        if (ipa_state_changed(rc, 0))
            bslog("IPADAPT !! 需要 %lu 字节放全部网卡，malloc 失败 ——"
                  " 客户端这次拿不到本机 IP（但不会崩）", (unsigned long)need);
        return rc;
    }
    bsz = need;
    rc2 = s_GetAdaptersInfo(big, &bsz);
    if (rc2 != NO_ERROR) {
        free(big);
        if (ipa_state_changed(rc, 0))
            bslog("IPADAPT !! 用 %lu 字节重取仍失败 rc=%lu —— 客户端这次拿不到"
                  "本机 IP（但不会崩）", (unsigned long)need, (unsigned long)rc2);
        return rc;
    }

    n = 0;
    src = big;
    dst = buf;
    while (src && n < cap) {
        memcpy(dst, src, sizeof(ipa_info_t));
        /* 搬过来的内部指针全指着 big，马上就要 free —— 一律截断。
           客户端只读 IpAddressList.IpAddress（+0x1B0），截掉多余的 IP /
           网关 / DHCP / WINS 链对它没有任何影响。 */
        dst->CurrentIpAddress         = NULL;
        dst->IpAddressList.Next       = NULL;
        dst->GatewayList.Next         = NULL;
        dst->DhcpServer.Next          = NULL;
        dst->PrimaryWinsServer.Next   = NULL;
        dst->SecondaryWinsServer.Next = NULL;
        dst->Next                     = NULL;
        if (n) buf[n - 1].Next = dst;   /* 上一项串到这一项，链落在调用方缓冲区内 */
        n++;
        dst++;
        src = src->Next;
    }
    free(big);
    *size = n * (ULONG)sizeof(ipa_info_t);

    if (ipa_state_changed(NO_ERROR, n))
        bslog("IPADAPT ★本机网卡需要 %lu 字节、客户端只给了 %lu —— 已截成前 %lu"
              " 块交回去（原 rc=%lu ERROR_BUFFER_OVERFLOW）。不截的话它会把未初始化"
              "的栈当链表遍历，那就是 bug调查/27 那个加载页闪退",
              (unsigned long)need, (unsigned long)in_size,
              (unsigned long)n, (unsigned long)rc);
    return NO_ERROR;
}

/* -------------------------------------------------------------------------- */
/* 装钩子。幂等；IPHLPAPI 还没加载就直接回来，让下一个调用点再试              */
/* （DllMain 一次、install_hooks 一次）——「模块在不在」是事实，不是时间。      */
/* -------------------------------------------------------------------------- */
/* ★ 对照开关：`BSHOOK_NO_ADAPTERS_GUARD=1` ⇒ 不装护栏，客户端恢复成原样。
   存在的理由只有一个 —— 验「修复前真的会崩」得能把护栏关掉，而重编一个
   没护栏的 DLL 会改掉 `manifest-hook.json` 里的 SHA、让服务端拒绝这个客户端
   （D85）。用环境变量就不用动 DLL，同一份二进制两种行为。
   ⚠ 打开它 + 本机网卡 ≥ 11 块 = 主动复现 bug调查/27 的闪退。 */
static int adapters_guard_disabled(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("BSHOOK_NO_ADAPTERS_GUARD", buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) && buf[0] != '0';
}

static void install_iphlpapi_hook(void)
{
    HMODULE iph;

    if (s_GetAdaptersInfo) return;

    if (adapters_guard_disabled()) {
        static int logged = 0;            /* 两个调用点，话只说一遍 */
        if (!logged) {
            logged = 1;
            bslog("HOOK    ★BSHOOK_NO_ADAPTERS_GUARD 已设 —— 不装 GetAdaptersInfo 护栏，"
                  "网卡 ≥ 11 块的机器会在加载页闪退（bug调查/27 的对照组）");
        }
        return;
    }

    iph = GetModuleHandleA("IPHLPAPI.DLL");
    if (!iph) {
        bslog("HOOK    IPHLPAPI 尚未加载，GetAdaptersInfo 护栏等下一轮");
        return;
    }
    s_GetAdaptersInfo = (GetAdaptersInfo_t)install_inline_hook(
        (void *)GetProcAddress(iph, "GetAdaptersInfo"),
        (void *)det_GetAdaptersInfo, "iphlpapi:GetAdaptersInfo");
    if (!s_GetAdaptersInfo)
        bslog("HOOK    !! GetAdaptersInfo 钩不上 —— 网卡多于 10 块的机器仍可能"
              "在加载页闪退（bug调查/27）");
}

#endif /* POPSHOT_ADAPTERS_GUARD_H */
