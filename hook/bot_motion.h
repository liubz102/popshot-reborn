/* BSM1 pure state primitives, shared by the hook and native replay tests. */
#ifndef BS_BOT_MOTION_H
#define BS_BOT_MOTION_H
#include <stdint.h>

typedef struct BmTrack {
    uint32_t identity, revision;
    uintptr_t object;
} BmTrack;

static int bm_newer(uint32_t a, uint32_t b) { return (int32_t)(a - b) > 0; }

static int bm_accept(BmTrack *t, uint32_t identity, uint32_t revision, uintptr_t object)
{
    if (!identity || !revision) return 0;
    if (t->identity) {
        if (identity != t->identity) {
            if (!bm_newer(identity, t->identity)) return 0;
        } else {
            if (!bm_newer(revision, t->revision)) return 0;
            /* Same seat, different character object: wait for the new life id. */
            if (t->object && object && t->object != object) return 0;
        }
    }
    t->identity = identity;
    t->revision = revision;
    t->object = object;
    return 1;
}

/* World position, velocity, grounded flags, horizontal subpixel remainder,
   transient dash/knockback drift. Health, stamina, animation and facing are not
   in this list. Copy integer bits so no FPU state or rounding mode is changed. */
typedef struct BmKinematics { uint32_t values[10]; } BmKinematics;
static const unsigned bm_offsets[10] = {
    0x34, 0x38, 0x120, 0x124, 0x128, 0x130, 0x4c0, 0x4c4, 0x4c8, 0x4cc
};
static void bm_save(BmKinematics *out, const unsigned char *object)
{
    unsigned i;
    for (i = 0; i < 10; ++i) out->values[i] = *(const uint32_t *)(object + bm_offsets[i]);
}
static void bm_restore(const BmKinematics *in, unsigned char *object)
{
    unsigned i;
    for (i = 0; i < 10; ++i) *(uint32_t *)(object + bm_offsets[i]) = in->values[i];
}
#endif
