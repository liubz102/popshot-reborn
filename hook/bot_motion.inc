/* Server-owned bot motion. Included after the common hook installer in bshook.c.
   Original reliable events still execute: damage, projectiles and animation
   retain their normal ordering. Only already-server-owned kinematics are kept.
   All state below is used on the game's main thread. No worker/network I/O. */
#include "bot_motion.h"

static BmTrack bm_tracks[6];
static uintptr_t bm_session;
static int bm_epoch;
static int bm_enabled;
static void *bm_packet_original, *bm_jump_original, *bm_hit_original;
static void *bm_dash_pos_original, *bm_dash_original, *bm_bind_original;
static void *bm_packet_drop = (void *)0x40793a;
static void *bm_hit_effects = (void *)0x50f94e;
static void *bm_dash_after_position = (void *)0x492e2e;

static uintptr_t bm_refresh(void)
{
    uintptr_t session = *(uintptr_t *)0x72e29c;
    int epoch = session ? *(int *)(session + 0x3c) : -1;
    if (session != bm_session || epoch != bm_epoch) {
        memset(bm_tracks, 0, sizeof(bm_tracks));
        bm_session = session;
        bm_epoch = epoch;
    }
    return session;
}

static int bm_owns(void *object)
{
    int seat;
    if (!bm_enabled || !object) return 0;
    __try {
        if (!bm_refresh()) return 0;
        seat = *(int *)((unsigned char *)object + 0x2ac);
        return seat >= 0 && seat < 6 && bm_tracks[seat].identity
            && bm_tracks[seat].object == (uintptr_t)object
            && *(uintptr_t *)(bm_session + 0x1d0 + seat * 4) == (uintptr_t)object
            && *(int *)(bm_session + 0x1cc) != seat;
    } __except(EXCEPTION_EXECUTE_HANDLER) { return 0; }
}

/* Runs AFTER original magic/seat/epoch/checksum validation (0x4078f6).
   Legacy packets and clients keep the original path. No reserved state bytes
   are repurposed. The complete extension is covered by the original checksum. */
static int bm_receive(void *session, const unsigned char *p, unsigned len)
{
    unsigned seat;
    uintptr_t object;
    unsigned char *queue;
    if (!bm_enabled || len != 59 || p[10] != 1 || p[11] != 0x40
            || memcmp(p + 43, "BSM\1", 4)) return 1;
    __try {
        seat = p[1];
        if (bm_refresh() != (uintptr_t)session || seat >= 6
                || *(int *)((unsigned char *)session + 0x1cc) == (int)seat
                || *(uint32_t *)(p + 14) != 1 || p[18] != seat) return 0;
        object = *(uintptr_t *)((unsigned char *)session + 0x1d0 + seat * 4);
        if (!bm_accept(&bm_tracks[seat], *(uint32_t *)(p + 47),
                       *(uint32_t *)(p + 51), object)) return 0;
        if (object) {
            /* Motion dependencies are simulated by the server too. Keeping a
               stale native dependency would reject the authoritative snapshot. */
            *(uint32_t *)(object + 0x164) = 0;
            *(uint32_t *)(object + 0x168) = 0;
        }
        /* A first UDP snapshot can beat the TCP seed. Bot streams start at 0;
           activate without FlushTo(N), preserving every pending reliable event. */
        queue = (unsigned char *)session + 0x2e4 + seat * 0x24;
        if (!queue[4] && *(uint32_t *)(queue + 8) == 0) queue[4] = 1;
        return 1;
    } __except(EXCEPTION_EXECUTE_HANDLER) { return 1; }
}

typedef struct BmGuard { void *object; uint32_t identity; BmKinematics motion; } BmGuard;
static void bm_capture_guard(BmGuard *guard, void *object)
{
    guard->object = NULL;
    if (bm_owns(object)) {
        guard->object = object;
        guard->identity = bm_tracks[*(int *)((unsigned char *)object + 0x2ac)].identity;
        bm_save(&guard->motion, (unsigned char *)object);
    }
}
static void bm_restore_guard(BmGuard *guard)
{
    if (bm_owns(guard->object)
            && guard->identity == bm_tracks[*(int *)((unsigned char *)guard->object + 0x2ac)].identity)
        bm_restore(&guard->motion, (unsigned char *)guard->object);
}

static __declspec(naked) void bm_packet_detour(void)
{
    __asm {
        pushfd
        pushad
        push dword ptr [ebp-1ch]
        push esi
        push edi
        call bm_receive
        add esp, 12
        test eax, eax
        jz discard
        popad
        popfd
        jmp dword ptr [bm_packet_original]
    discard:
        popad
        popfd
        jmp dword ptr [bm_packet_drop]
    }
}

/* Jump uses ESI=this and one stack argument. Keep its visual/timer work. */
static __declspec(naked) void bm_jump_detour(void)
{
    __asm {
        push ebp
        mov ebp, esp
        sub esp, 64
        pushfd
        pushad
        push esi
        lea eax, [ebp-64]
        push eax
        call bm_capture_guard
        add esp, 8
        popad
        popfd
        push dword ptr [ebp+8]
        call dword ptr [bm_jump_original]
        pushfd
        pushad
        lea eax, [ebp-64]
        push eax
        call bm_restore_guard
        add esp, 4
        popad
        popfd
        mov esp, ebp
        pop ebp
        ret 4
    }
}

/* OnHit has already selected a reaction tier at this point. Skip only motion,
   retaining 0x50f94e onward (dependency release, effects, notifications). */
static __declspec(naked) void bm_hit_detour(void)
{
    __asm {
        pushfd
        pushad
        push esi
        call bm_owns
        add esp, 4
        test eax, eax
        jz original
        popad
        popfd
        jmp dword ptr [bm_hit_effects]
    original:
        popad
        popfd
        jmp dword ptr [bm_hit_original]
    }
}

static __declspec(naked) void bm_bind_detour(void)
{
    __asm {
        test edx, edx
        jz original
        pushfd
        pushad
        push ecx
        call bm_owns
        add esp, 4
        test eax, eax
        jz restore_original
        popad
        popfd
        lea eax, [ecx+168h]
        ret 4
    restore_original:
        popad
        popfd
    original:
        jmp dword ptr [bm_bind_original]
    }
}

/* OnDash's network position write precedes Character::Dash. */
static __declspec(naked) void bm_dash_pos_detour(void)
{
    __asm {
        pushfd
        pushad
        push eax
        call bm_owns
        add esp, 4
        test eax, eax
        jz original
        popad
        popfd
        jmp dword ptr [bm_dash_after_position]
    original:
        popad
        popfd
        jmp dword ptr [bm_dash_pos_original]
    }
}

/* Character::Dash: ECX=this, two stack arguments. */
static __declspec(naked) void bm_dash_detour(void)
{
    __asm {
        push ebp
        mov ebp, esp
        sub esp, 64
        pushfd
        pushad
        push ecx
        lea eax, [ebp-64]
        push eax
        call bm_capture_guard
        add esp, 8
        popad
        popfd
        push dword ptr [ebp+12]
        push dword ptr [ebp+8]
        call dword ptr [bm_dash_original]
        pushfd
        pushad
        lea eax, [ebp-64]
        push eax
        call bm_restore_guard
        add esp, 4
        popad
        popfd
        mov esp, ebp
        pop ebp
        ret 8
    }
}

/* These six signatures are complete, position-independent instruction blocks.
   The old generic decoder cannot size A1, LEA or CMP AX,imm16 correctly. Use
   only the verified lengths below; never feed a mid-instruction trampoline. */
static void *bm_install_verified(uintptr_t address, void *detour, unsigned length)
{
    unsigned char *target = (unsigned char *)address;
    unsigned char *trampoline;
    DWORD old;
    trampoline = (unsigned char *)VirtualAlloc(NULL, length + 5,
        MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!trampoline) return NULL;
    memcpy(trampoline, target, length);
    trampoline[length] = 0xe9;
    *(uint32_t *)(trampoline + length + 1) = (uint32_t)(address + length - (uintptr_t)(trampoline + length + 5));
    if (!VirtualProtect(target, 5, PAGE_EXECUTE_READWRITE, &old)) {
        VirtualFree(trampoline, 0, MEM_RELEASE);
        return NULL;
    }
    target[0] = 0xe9;
    *(uint32_t *)(target + 1) = (uint32_t)((uintptr_t)detour - address - 5);
    VirtualProtect(target, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, length);
    return trampoline;
}

static int try_patch_bot_motion(void)
{
    struct Site { uintptr_t va; const char *sig; int len; void *detour; void **original; };
    struct Site sites[] = {
        {0x4078f6, "\x66\x8b\x46\x0a\x66\x3d\x00\x40", 8, bm_packet_detour, &bm_packet_original},
        {0x501d57, "\xb8\x2b\x2a\x63\x00", 5, bm_jump_detour, &bm_jump_original},
        {0x50f800, "\xa1\xdc\xe2\x72\x00", 5, bm_hit_detour, &bm_hit_original},
        {0x50e636, "\x8d\x81\x68\x01\x00\x00", 6, bm_bind_detour, &bm_bind_original},
        {0x492e23, "\x8b\x0b\x8b\x53\x04", 5, bm_dash_pos_detour, &bm_dash_pos_original},
        {0x5020d9, "\xb8\xaa\x8a\x62\x00", 5, bm_dash_detour, &bm_dash_original}
    };
    unsigned i;
    if (bm_enabled) return 1;
    for (i = 0; i < sizeof(sites)/sizeof(sites[0]); ++i)
        if (!*sites[i].original && memcmp((void *)sites[i].va, sites[i].sig, sites[i].len)) return 0;
    for (i = 0; i < sizeof(sites)/sizeof(sites[0]); ++i) {
        if (!*sites[i].original)
            *sites[i].original = bm_install_verified(sites[i].va, sites[i].detour, sites[i].len);
        if (!*sites[i].original) return 0;
    }
    bm_enabled = 1;
    bslog("BSM1    bot 运动权威接收已启用：版本去旧，事件保留伤害/动画，运动由服务端结算");
    return 1;
}
