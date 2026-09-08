/* Build-only harness. Unicorn loads this DLL and calls the ACTUAL detours.
   No game is launched; original callees are supplied by the replay fixture. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <string.h>
static void bslog(const char *fmt, ...) { (void)fmt; }
#include "bot_motion.inc"

__declspec(dllexport) void bm_test_setup(void *jump, void *hit, void *bind, void *dashpos, void *dash)
{
    memset(bm_tracks, 0, sizeof(bm_tracks));
    bm_session = 0;
    bm_epoch = -1;
    bm_jump_original = jump;
    bm_packet_original = (void *)0x4078f6;
    bm_hit_original = hit;
    bm_bind_original = bind;
    bm_dash_pos_original = dashpos;
    bm_dash_original = dash;
    bm_enabled = 1;
}
__declspec(dllexport) int bm_test_receive(void *s, const unsigned char *p, unsigned len)
{ return bm_receive(s, p, len); }
__declspec(dllexport) void *bm_test_entry(int i)
{
    switch (i) {
    case 0: return bm_jump_detour;
    case 1: return bm_hit_detour;
    case 2: return bm_bind_detour;
    case 3: return bm_dash_pos_detour;
    case 4: return bm_dash_detour;
    case 5: return bm_packet_detour;
    default: return NULL;
    }
}
__declspec(dllexport) int bm_test_install(void)
{
    bm_enabled = 0;
    bm_packet_original = bm_jump_original = bm_hit_original = NULL;
    bm_bind_original = bm_dash_pos_original = bm_dash_original = NULL;
    return try_patch_bot_motion();
}
__declspec(dllexport) void *bm_test_original(int i)
{
    switch (i) {
    case 0: return bm_packet_original;
    case 1: return bm_jump_original;
    case 2: return bm_hit_original;
    case 3: return bm_bind_original;
    case 4: return bm_dash_pos_original;
    case 5: return bm_dash_original;
    default: return NULL;
    }
}
