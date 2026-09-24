/*
 * 内联 hook 的相对跳转重定位 —— 回归夹具（bug调查/27）。
 *
 * 由 test-insn-reloc.bat 编译成一个 32 位控制台程序，独立跑，不需要游戏、
 * 不需要注入、不需要网络。**直接 `#include "insn_reloc.h"`**，跑的就是
 * bshook.dll 里那一份 `install_inline_hook`，不是抄一份。
 *
 * ## 要守住的是什么
 *
 * `install_inline_hook` 从被钩函数的头部「偷」>=5 字节搬进蹦床。原来那一发是
 * `memcpy` —— 原样搬。而序言里出现 `E9/E8 rel32`、`EB rel8` 的唯一情形是
 * **第三方（输入法 / 安全软件）已经先内联挂过这个导出函数**，这三条指令的
 * 操作数是相对位移，搬到蹦床里就指向别处 ⇒ 第一次经蹦床调原函数就崩。
 * 玩家症状：开着搜狗输入法启动游戏时在加载页概率闪退（谁先挂钩是竞态，
 * 所以「有时又能进」）。
 *
 * 下面四个场景里，场景 B / C 就是那个闪退。修复前它们必崩（或返回垃圾），
 * 修复后必须拿到确定的数。
 *
 *   A 干净序言（微软热补丁头 8b ff 55 8b ec）  —— 别把原来好使的搞坏
 *   B 第三方用 `E9 rel32` 抢先挂了            —— ★ 闪退现场
 *   C 第三方用 `EB rel8` 抢先挂了（热补丁区）  —— ★ 闪退现场，位移只有 1 字节
 *   D 前缀 + 相对跳转（`66 E9`）              —— 宽度不明，必须**拒挂**且不动目标
 *   E 不认识的 opcode                         —— 必须拒挂且不动目标
 *
 * 判据全是「函数的返回值」：每个场景的真身 / 第三方处理函数返回一个各不相同
 * 的数，我们的 detour 返回「经蹦床拿到的数 + 1」。数对 = 整条钩子链是对的。
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

/* insn_reloc.h 只依赖 bslog。夹具给一个往 stdout 写的版本 —— 顺带让
   `★序言首字节是相对跳转` 那行诊断也进测试输出，肉眼能看见它真的打了。 */
void bslog(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    printf("    [hook] ");
    vprintf(fmt, ap);
    printf("\n");
    va_end(ap);
}

#include "insn_reloc.h"

static int g_fails = 0;

static void check(int ok, const char *what)
{
    printf("  %s  %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) ++g_fails;
}

static void check_eq(int got, int want, const char *what)
{
    if (got == want) printf("  PASS  %s (= %d)\n", what, got);
    else { printf("  FAIL  %s: got %d, want %d\n", what, got, want); ++g_fails; }
}

/* -------------------------------------------------------------------------- */
/* 手搓机器码：要精确控制序言长什么样，编译器生成的靠不住                     */
/* -------------------------------------------------------------------------- */

typedef int (*fn_t)(void);

static unsigned char *alloc_code(void)
{
    return (unsigned char *)VirtualAlloc(NULL, 0x1000, MEM_COMMIT | MEM_RESERVE,
                                         PAGE_EXECUTE_READWRITE);
}

/* mov eax, imm32 ; ret —— 拿来当「第三方的处理函数」和各种真身 */
static int emit_ret_const(unsigned char *p, int v)
{
    p[0] = 0xB8;
    *(long *)(p + 1) = (long)v;
    p[5] = 0xC3;
    return 6;
}

/* 微软热补丁序言 + mov eax,imm32 + pop ebp + ret：
     8b ff        mov edi,edi     ← 这 2 字节就是给挂钩用的
     55           push ebp
     8b ec        mov ebp,esp
     b8 imm32     mov eax,v
     5d           pop ebp
     c3           ret                                                        */
static int emit_hotpatch_fn(unsigned char *p, int v)
{
    p[0] = 0x8B; p[1] = 0xFF;
    p[2] = 0x55;
    p[3] = 0x8B; p[4] = 0xEC;
    p[5] = 0xB8;
    *(long *)(p + 6) = (long)v;
    p[10] = 0x5D;
    p[11] = 0xC3;
    return 12;
}

/* 在 at 处写 `E9 rel32` 跳到 to —— 第三方挂钩最常见的写法 */
static void emit_jmp32(unsigned char *at, const unsigned char *to)
{
    at[0] = 0xE9;
    *(long *)(at + 1) = (long)((UINT_PTR)to - (UINT_PTR)(at + 5));
}

/* -------------------------------------------------------------------------- */
/* 四个 detour。参数表是空的，所以不用 naked/asm，普通 C 就够                 */
/* -------------------------------------------------------------------------- */

static void *g_tramp_a, *g_tramp_b, *g_tramp_c;

static int det_a(void) { return ((fn_t)g_tramp_a)() + 1; }
static int det_b(void) { return ((fn_t)g_tramp_b)() + 1; }
static int det_c(void) { return ((fn_t)g_tramp_c)() + 1; }
static int det_never(void) { printf("  FAIL  不该被调到的 detour 跑了\n"); ++g_fails; return -1; }

/* -------------------------------------------------------------------------- */

/* A：干净的微软热补丁序言。修复前后都该好使 —— 防止「修了新的、坏了旧的」。 */
static void case_a(void)
{
    unsigned char *code = alloc_code();
    fn_t fn = (fn_t)code;

    printf("[A] 干净序言（8b ff 55 8b ec）\n");
    emit_hotpatch_fn(code, 42);
    check_eq(fn(), 42, "挂钩前真身返回 42");

    g_tramp_a = install_inline_hook(code, (void *)det_a, "case-A");
    check(g_tramp_a != NULL, "钩子装上了");
    if (g_tramp_a) check_eq(fn(), 43, "游戏 -> detour -> 蹦床 -> 真身");
}

/* B ★ 闪退现场：第三方先用 `E9 rel32` 挂了，我们再挂。
   修复前：蹦床里原样搬的那个 E9 会落到
   `第三方处理函数 + (真身地址 - 蹦床地址)` —— 两个 VirtualAlloc 差出多远
   全看运气，实机上是没映射的地址 ⇒ C0000005。 */
static void case_b(void)
{
    unsigned char *code = alloc_code();
    unsigned char *third = alloc_code();
    fn_t fn = (fn_t)code;

    printf("[B] 第三方用 E9 rel32 抢先挂钩（= 搜狗输入法那一类）\n");
    emit_hotpatch_fn(code, 42);
    emit_ret_const(third, 1000);      /* 「第三方的处理函数」 */
    emit_jmp32(code, third);          /* 第三方把序言改成 E9 -> 它自己 */
    check_eq(fn(), 1000, "第三方的钩子先生效（真身已经被它接走）");

    g_tramp_b = install_inline_hook(code, (void *)det_b, "case-B");
    check(g_tramp_b != NULL, "钩子装上了");
    if (g_tramp_b) {
        /* 修复前这一发就是闪退。链条：我们的 detour -> 蹦床(重算后的 E9)
           -> 第三方处理函数 -> 1000，所以该拿到 1001。 */
        check_eq(fn(), 1001, "游戏 -> 我们的 detour -> 蹦床 -> 第三方 -> 1000");
        check(*(unsigned char *)g_tramp_b == 0xE9, "蹦床首字节仍是 E9（重算位移，不是换指令）");
        {
            UINT_PTR tgt = (UINT_PTR)g_tramp_b + 5
                         + (UINT_PTR)(long)(*(long *)((unsigned char *)g_tramp_b + 1));
            check(tgt == (UINT_PTR)third, "蹦床那一跳指向第三方处理函数，不是垃圾地址");
        }
    }
}

/* C ★ 闪退现场之二：第三方用微软官方的热补丁办法 ——
   `EB F9`（往前跳 7 个字节，落进函数前面那 5 字节填充区），填充区里再 `E9`
   跳到自己。位移只有 1 字节（rel8），搬到蹦床里连「跳回同一个函数」都做不到，
   必须换成 `E9 rel32` 才装得下。 */
static void case_c(void)
{
    unsigned char *page = alloc_code();
    unsigned char *pad = page;        /* 5 字节热补丁填充区 */
    unsigned char *code = page + 5;   /* 函数真正的入口 */
    unsigned char *third = alloc_code();
    fn_t fn = (fn_t)code;

    printf("[C] 第三方用 EB rel8 + 热补丁填充区抢先挂钩\n");
    memset(pad, 0x90, 5);
    emit_hotpatch_fn(code, 42);
    emit_ret_const(third, 2000);
    emit_jmp32(pad, third);           /* 填充区：E9 -> 第三方 */
    code[0] = 0xEB; code[1] = 0xF9;   /* 入口：jmp -7 -> 填充区 */
    check_eq(fn(), 2000, "第三方的钩子先生效");

    g_tramp_c = install_inline_hook(code, (void *)det_c, "case-C");
    check(g_tramp_c != NULL, "钩子装上了");
    if (g_tramp_c) {
        check_eq(fn(), 2001, "游戏 -> 我们的 detour -> 蹦床 -> 填充区 -> 第三方 -> 2000");
        check(*(unsigned char *)g_tramp_c == 0xE9, "蹦床里 EB rel8 被换成了 E9 rel32");
    }
}

/* D：前缀 + 相对跳转（`66 E9` = jmp rel16）。位移宽度和 reloc_insn 的两条
   支路都不一样，不许猜 —— 必须拒挂，而且**一个字节都不许动目标**
   （动了就等于把函数改成跳向我们，却没有能用的蹦床，必崩）。 */
static void case_d(void)
{
    unsigned char *code = alloc_code();
    unsigned char before[8];
    void *tramp;

    printf("[D] 前缀 + 相对跳转（66 E9）：必须拒挂\n");
    emit_hotpatch_fn(code, 42);
    code[0] = 0x66; code[1] = 0xE9; code[2] = 0x02; code[3] = 0x00;
    memcpy(before, code, 8);

    tramp = install_inline_hook(code, (void *)det_never, "case-D");
    check(tramp == NULL, "拒挂（返回 NULL）");
    check(memcmp(code, before, 8) == 0, "目标字节没被动过");
}

/* E：不认识的 opcode（`0f 0b` = ud2）。长度算不出来 ⇒ 拒挂。
   这一条本来就是对的，钉住它，免得以后改 insn_len 时把「不认识就放弃」
   这个安全默认值弄丢。 */
static void case_e(void)
{
    unsigned char *code = alloc_code();
    unsigned char before[8];
    void *tramp;

    printf("[E] 不认识的 opcode（0f 0b）：必须拒挂\n");
    emit_hotpatch_fn(code, 42);
    code[0] = 0x0F; code[1] = 0x0B;
    memcpy(before, code, 8);

    tramp = install_inline_hook(code, (void *)det_never, "case-E");
    check(tramp == NULL, "拒挂（返回 NULL）");
    check(memcmp(code, before, 8) == 0, "目标字节没被动过");
}

/* F：`E8 rel32`（call）也走重定位那一支，但上面几个场景碰不到它
   —— 直接验算术：重定位之后指向的绝对地址必须没变。 */
static void case_f(void)
{
    unsigned char *src = alloc_code();
    unsigned char *dst = alloc_code();
    unsigned char *callee = alloc_code();
    int n;

    printf("[F] E8 rel32（call）的位移重算\n");
    src[0] = 0xE8;
    *(long *)(src + 1) = (long)((UINT_PTR)callee - (UINT_PTR)(src + 5));

    n = reloc_insn(dst, src, 5);
    check_eq(n, 5, "写了 5 字节");
    check(dst[0] == 0xE8, "还是 call，没被换成 jmp");
    {
        UINT_PTR tgt = (UINT_PTR)dst + 5 + (UINT_PTR)(long)(*(long *)(dst + 1));
        check(tgt == (UINT_PTR)callee, "重算后指向同一个绝对地址");
    }
}

int main(void)
{
    printf("== 内联 hook 相对跳转重定位 回归（bug调查/27）==\n\n");
    case_a(); printf("\n");
    case_b(); printf("\n");
    case_c(); printf("\n");
    case_d(); printf("\n");
    case_e(); printf("\n");
    case_f(); printf("\n");

    if (g_fails) { printf("== FAILED: %d 项不过 ==\n", g_fails); return 1; }
    printf("== ALL PASS ==\n");
    return 0;
}
