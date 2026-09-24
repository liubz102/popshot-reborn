#ifndef POPSHOT_INSN_RELOC_H
#define POPSHOT_INSN_RELOC_H

/*
 * 通用内联 hook（x86-32）：长度反汇编 + 相对跳转重定位 + 装钩子。
 *
 * 从 bshook.c 里搬出来的，**为了能单测**（bug调查/27）——
 * 夹具 hook\test_insn_reloc.c 直接 include 本文件，跑的就是线上这一份代码，
 * 不是抄一份。除了 `bslog(fmt, ...)` 之外不依赖 bshook.c 的任何东西，
 * 所以 include 之前必须先有 bslog。
 *
 * ★ 会不会被第三方抢先挂钩，是这里唯一真正难的事，见 `reloc_insn` 头上那段。
 */

/* -------------------------------------------------------------------------- */
/* 极简 x86 长度反汇编器 —— 只为算出内联 hook 要偷几个字节（>=5）             */
/* 覆盖常见函数序言指令；遇到不认识的 opcode 返回 0（放弃 hook，安全）        */
/* -------------------------------------------------------------------------- */
static int insn_len(const unsigned char *p)
{
    unsigned char op = p[0];
    /* 前缀 */
    if (op == 0x66 || op == 0x67 || op == 0xF2 || op == 0xF3) return 1 + insn_len(p + 1);
    switch (op) {
    case 0x50: case 0x51: case 0x52: case 0x53:      /* push r32 */
    case 0x54: case 0x55: case 0x56: case 0x57:
    case 0x58: case 0x59: case 0x5A: case 0x5B:      /* pop r32  */
    case 0x5C: case 0x5D: case 0x5E: case 0x5F:
    case 0x90: case 0xC3: case 0xC9:                 /* nop/ret/leave */
        return 1;
    case 0x6A:                                       /* push imm8 */
        return 2;
    case 0x68:                                       /* push imm32 */
        return 5;
    case 0xB8: case 0xB9: case 0xBA: case 0xBB:      /* mov r32, imm32 */
    case 0xBC: case 0xBD: case 0xBE: case 0xBF:
        return 5;
    case 0xE9: case 0xE8:                            /* jmp/call rel32 */
        return 5;
    case 0xEB:                                       /* jmp rel8 */
        return 2;
    case 0x8B: case 0x89:                            /* mov r/m32,r32 / mov r32,r/m32 */
    case 0x33: case 0x85:                            /* xor r/m32,r32 / test r/m32,r32 */
    {
        unsigned char modrm = p[1];
        unsigned char mod = modrm >> 6, rm = modrm & 7;
        int len = 2;
        if (mod != 3 && rm == 4) len += 1;           /* SIB */
        if (mod == 1) len += 1;                       /* disp8 */
        else if (mod == 2) len += 4;                  /* disp32 */
        else if (mod == 0 && rm == 5) len += 4;       /* disp32 (no base) */
        else if (mod == 0 && rm == 4 && (p[2] & 7) == 5) len += 4; /* SIB base=5 */
        return len;
    }
    case 0x83:                                       /* grp1 r/m32, imm8 (sub/add esp,..) */
    {
        unsigned char modrm = p[1];
        unsigned char mod = modrm >> 6, rm = modrm & 7;
        int len = 2;
        if (mod != 3 && rm == 4) len += 1;
        if (mod == 1) len += 1;
        else if (mod == 2) len += 4;
        else if (mod == 0 && rm == 5) len += 4;
        return len + 1;                               /* + imm8 */
    }
    case 0x81:                                       /* grp1 r/m32, imm32 */
    {
        unsigned char modrm = p[1];
        unsigned char mod = modrm >> 6, rm = modrm & 7;
        int len = 2;
        if (mod != 3 && rm == 4) len += 1;
        if (mod == 1) len += 1;
        else if (mod == 2) len += 4;
        else if (mod == 0 && rm == 5) len += 4;
        return len + 4;                               /* + imm32 */
    }
    case 0xFF:                                        /* grp5 (push/call/jmp r/m) */
    {
        unsigned char modrm = p[1];
        unsigned char mod = modrm >> 6, rm = modrm & 7;
        int len = 2;
        if (mod != 3 && rm == 4) len += 1;
        if (mod == 1) len += 1;
        else if (mod == 2) len += 4;
        else if (mod == 0 && rm == 5) len += 4;
        return len;
    }
    }
    return 0; /* 不认识 */
}

/* -------------------------------------------------------------------------- */
/* 把偷来的一条指令搬进蹦床 —— 相对跳转 / 调用必须按新地址重算位移            */
/*                                                                            */
/* ★ bug调查/27：这里原来是一发 `memcpy`，把序言原样搬进蹦床。而序言里会出现  */
/*   `E9/E8 rel32`、`EB rel8` 的**唯一**情形是「第三方已经内联挂过这个导出     */
/*   函数」—— 输入法 / 安全软件 / 加速器注入的组件都这么干。这三条指令的操作数 */
/*   是「相对本条指令末尾」的位移，搬到别处就指向别处：蹦床里那一跳会落到      */
/*   `对方的处理函数 + (导出函数地址 - 蹦床地址)`。本项目的蹦床实测在          */
/*   0x001E0000 附近、user32 在 0x76xxxxxx，差出约 -0x76640000 —— 必然是没映射 */
/*   的地址，⇒ **第一次经蹦床调原函数就 C0000005**，而且崩在所有模块之外，     */
/*   原版异常过滤器给不出有意义的 rpt（Dump 目录里往往什么都没留下）。         */
/*                                                                            */
/*   玩家症状：开着搜狗输入法时在启动加载页概率闪退，关掉输入法就正常。        */
/*   「有概率」来自竞态 —— 我们的 `install_hooks` 和输入法组件被载入进程、     */
/*   挂上自己的钩，谁先谁后不定：对方先挂 ⇒ 我们搬走对方的 `E9` ⇒ 必崩；       */
/*   我们先挂 ⇒ 序言是干净的微软热补丁头（`8b ff 55 8b ec`）⇒ 一切正常。       */
/*                                                                            */
/*   重算之后钩子链是对的：游戏 → 我们的 detour → 蹦床(`E9`→对方处理函数)      */
/*   → 对方的蹦床 → 真正的导出函数。谁先挂都不再有影响。                      */
/*                                                                            */
/* 返回写进 dst 的字节数；0 = 搬不动，调用方必须放弃 hook（绝不许硬搬）。      */
/* -------------------------------------------------------------------------- */
static int reloc_insn(unsigned char *dst, const unsigned char *src, int len)
{
    UINT_PTR tgt;
    int i;

    /* 前缀后面跟着相对跳转（`66 E9` = jmp rel16 之类）：位移宽度和下面两支的
       假设不一样，不猜 —— 直接放弃 hook。实机的 API 序言里没见过这种写法，
       但漏判的后果就是上面那个闪退，所以宁可不挂。 */
    for (i = 0; i < len; i++) {
        unsigned char op = src[i];
        if (op == 0x66 || op == 0x67 || op == 0xF2 || op == 0xF3) continue;
        if (i > 0 && (op == 0xE9 || op == 0xE8 || op == 0xEB)) return 0;
        break;
    }

    if (src[0] == 0xE9 || src[0] == 0xE8) {          /* jmp / call rel32 */
        tgt = (UINT_PTR)(src + 5) + (UINT_PTR)(long)(*(const long *)(src + 1));
        dst[0] = src[0];
        *(long *)(dst + 1) = (long)(tgt - (UINT_PTR)(dst + 5));
        return 5;
    }
    if (src[0] == 0xEB) {                            /* jmp rel8 → 换成 jmp rel32 */
        tgt = (UINT_PTR)(src + 2) + (UINT_PTR)(long)(*(const signed char *)(src + 1));
        dst[0] = 0xE9;
        *(long *)(dst + 1) = (long)(tgt - (UINT_PTR)(dst + 5));
        return 5;
    }

    /* 其余 `insn_len` 认的指令全是位置无关的：x86-32 的 `disp32` 是**绝对**
       地址（不是 x64 的 RIP 相对），立即数 / 寄存器操作 / `ff 25 [disp32]`
       间接跳转搬到哪儿都照样对。 */
    memcpy(dst, src, len);
    return len;
}

/* -------------------------------------------------------------------------- */
/* 通用内联 hook：在 target 头部写 E9 跳到 detour，返回可调用原函数的蹦床      */
/* 偷够 >=5 字节（按指令边界），蹦床 = [搬过来的字节][E9 跳回 target+n]        */
/* ★ 搬字节走 `reloc_insn`，不是 memcpy —— 理由见它头上那段（bug调查/27）。    */
/* -------------------------------------------------------------------------- */
static void *install_inline_hook(void *target, void *detour, const char *name)
{
    unsigned char *t = (unsigned char *)target;
    unsigned char *tramp;
    DWORD oldp;
    int stolen = 0, guard = 0, in, out;

    if (!t) { bslog("HOOK    %s: target=NULL, 跳过", name); return NULL; }

    bslog("HOOK    %s @ %08X 序言: %02x %02x %02x %02x %02x %02x %02x %02x",
          name, (unsigned)(UINT_PTR)t,
          t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7]);

    /* ★ 这一行就是 bug调查/27 的指纹：玩家日志里有它，就说明那台机器上有别的
       东西先挂了同一个导出函数（搜狗输入法实测会）。留着它，下次同类问题
       一 grep 就定案，不用再猜。 */
    if (t[0] == 0xE9 || t[0] == 0xE8 || t[0] == 0xEB)
        bslog("HOOK    %s: ★序言首字节是相对跳转（%02x）—— 第三方（输入法 / 安全"
              "软件）已经先内联挂过这个导出函数；蹦床按新地址重算位移，不原样搬"
              "（bug调查/27）", name, t[0]);

    while (stolen < 5 && guard++ < 8) {
        int l = insn_len(t + stolen);
        if (l <= 0) { bslog("HOOK    %s: 未知 opcode %02x @ +%d, 放弃", name, t[stolen], stolen); return NULL; }
        stolen += l;
    }
    if (stolen < 5) { bslog("HOOK    %s: 偷不够 5 字节, 放弃", name); return NULL; }

    /* 64 字节（`VirtualAlloc` 反正按页给）：偷到的最多 15 字节，`EB`→`E9` 每条
       还会长 3 字节，加上尾巴那一跳，上界 29 —— 留一倍余量。 */
    tramp = (unsigned char *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!tramp) { bslog("HOOK    %s: VirtualAlloc 失败", name); return NULL; }
    for (in = 0, out = 0; in < stolen; ) {
        int l = insn_len(t + in);
        int w = reloc_insn(tramp + out, t + in, l);
        if (w <= 0) {
            bslog("HOOK    %s: +%d 处那条指令（%02x %02x）搬不动, 放弃"
                  "（带前缀的相对跳转，见 reloc_insn）", name, in, t[in], t[in + 1]);
            VirtualFree(tramp, 0, MEM_RELEASE);
            return NULL;
        }
        in += l;
        out += w;
    }
    tramp[out] = 0xE9;
    *(DWORD *)(tramp + out + 1) = (DWORD)((UINT_PTR)(t + stolen) - (UINT_PTR)(tramp + out + 5));

    if (!VirtualProtect(t, 5, PAGE_EXECUTE_READWRITE, &oldp)) {
        bslog("HOOK    %s: VirtualProtect 失败", name);
        VirtualFree(tramp, 0, MEM_RELEASE);
        return NULL;
    }
    t[0] = 0xE9;
    *(DWORD *)(t + 1) = (DWORD)((UINT_PTR)detour - (UINT_PTR)(t + 5));
    VirtualProtect(t, 5, oldp, &oldp);
    FlushInstructionCache(GetCurrentProcess(), t, 5);
    /* 蹦床是刚 VirtualAlloc 出来的新页，但它马上就要被当代码执行 —— 该刷。 */
    FlushInstructionCache(GetCurrentProcess(), tramp, (SIZE_T)out + 5);

    bslog("HOOK    %s: 安装成功, 偷了 %d 字节（搬进蹦床 %d 字节）, 蹦床 @ %08X",
          name, stolen, out, (unsigned)(UINT_PTR)tramp);
    return tramp;
}

#endif /* POPSHOT_INSN_RELOC_H */
