/* ========================================================================== */
/* promo_wire.h —— 晋级任务表（gsp 0x0F03）的线格式 + 喂给游戏加载器的只读流    */
/*                (X_Mod X23 / §154~ / D111)                                   */
/*                                                                            */
/* 服务端（`server/missions.py` 的 `build_table_frames`）把**这个玩家自己的**   */
/* 任务表生成一整份客户端 ini（UTF-16LE + BOM + CRLF，约 100 KB），切成几片：   */
/*                                                                            */
/*   22 B 片头 `<HIIIHHI`：u16 format=1 · u32 serial · u32 total · u32 crc32   */
/*                          · u16 index · u16 count · u32 offset              */
/*   + 数据（每片 ≤ 32 KiB；帧载荷长是 u16，整张表一帧装不下）                */
/*                                                                            */
/* 拼装规矩（和 `missions.parse_table_frames` 一条一条对应，那边是测试用的镜像）：*/
/*   index==0 重置；后续片 serial / total / crc / count 和第 0 片一致、          */
/*   index / offset 连续；收齐验长度和 crc32（zlib 那个）。任何一条不对 =        */
/*   **整份丢弃**（返回 PROMO_BAD + 原因），等服务端下一次推。                  */
/*                                                                            */
/* 只读流：游戏的 ini 解析器（`0x5d48c3`）只认流对象虚表上的两格 ——            */
/*   +0x18 Read(dst, n) → 读到的字节数（thiscall，被调方弹 8 字节）；          */
/*   +0x14 IsEof()      → al 非 0 = 读完了（thiscall，没有栈参数）。          */
/* 这里用 `__fastcall` 冒充 thiscall：ecx = this、edx 不用、其余参数在栈上、    */
/* 被调方清栈 —— 和 thiscall 的二进制约定一模一样。                           */
/* 别的槽一律指向 `promo_stream_trap`（宿主定义 PROMO_STREAM_TRAP 决定怎么报）：*/
/* 解析器里只有那两个间接调用（`test_patchsites_promo` 扫过），真被调到别的槽  */
/* 就是镜像不是这一版。                                                       */
/*                                                                            */
/* 这里只有纯 C + memcpy / malloc，不依赖 Windows 头 ——                        */
/* `hook/bshook.c` 和 `test/hook/test_promo_wire.c` 共用这一份。               */
/* ========================================================================== */
#ifndef POPSHOT_PROMO_WIRE_H
#define POPSHOT_PROMO_WIRE_H

#include <stdlib.h>
#include <string.h>

#define PROMO_FORMAT        1
#define PROMO_HEADER_BYTES  22
#define PROMO_CHUNK_MAX     (32u * 1024u)
/* 整张表的上限：出厂 105 条约 100 KB，留到 4 MB —— 再大就是片头坏了，别去 malloc。 */
#define PROMO_TOTAL_MAX     (4u * 1024u * 1024u)

#define PROMO_PART  0       /* 这一片收下了，还没收齐 */
#define PROMO_DONE  1       /* 收齐了，长度和 crc 都对：数据在 promo_asm_take() */
#define PROMO_BAD   (-1)    /* 这一片不对，整份丢弃（*why 是原因） */

#ifndef PROMO_FASTCALL
#define PROMO_FASTCALL __fastcall
#endif

/* 流对象的「意外虚函数」被调到时做什么。bshook 里是抛自己的异常码（加载外面的
   __except 接住、旧表留用）；夹具里默认什么都不做（它只核槽位表，不去调）。 */
#ifndef PROMO_STREAM_TRAP
#define PROMO_STREAM_TRAP(self) ((void)(self))
#endif

/* ------------------------------------------------------------------ crc32 */

/* zlib 的 crc32（多项式 0xEDB88320、初值 / 终值取反）。和 Python 的
   `zlib.crc32(data) & 0xFFFFFFFF` 逐位相同。逐位算：100 KB 不到一毫秒，
   不值得为它常驻一张 1 KB 的表。 */
static unsigned promo_crc32(const unsigned char *p, unsigned n)
{
    unsigned crc = 0xFFFFFFFFu;
    while (n--) {
        int k;
        crc ^= *p++;
        for (k = 0; k < 8; k++)
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

/* ----------------------------------------------------------------- 片头 */

typedef struct promo_head {
    unsigned format, serial, total, crc, index, count, offset;
} promo_head;

static unsigned promo_u16(const unsigned char *p) { return (unsigned)p[0] | ((unsigned)p[1] << 8); }
static unsigned promo_u32(const unsigned char *p)
{
    return (unsigned)p[0] | ((unsigned)p[1] << 8) | ((unsigned)p[2] << 16) | ((unsigned)p[3] << 24);
}

/* 载荷 → 片头。不够 22 字节返回 0。 */
static int promo_parse_head(const unsigned char *p, unsigned len, promo_head *h)
{
    if (!p || len < PROMO_HEADER_BYTES) return 0;
    h->format = promo_u16(p + 0);
    h->serial = promo_u32(p + 2);
    h->total  = promo_u32(p + 6);
    h->crc    = promo_u32(p + 10);
    h->index  = promo_u16(p + 14);
    h->count  = promo_u16(p + 16);
    h->offset = promo_u32(p + 18);
    return 1;
}

/* ----------------------------------------------------------------- 拼装 */

typedef struct promo_asm {
    unsigned char *buf;     /* malloc 的，total 字节（total 为 0 时也给 1 字节） */
    unsigned serial, total, crc, count;
    unsigned next;          /* 下一片该是几号 */
    unsigned got;           /* 已经拼了多少字节 */
    int active;             /* 收过第 0 片、还没收齐 / 没出错 */
} promo_asm;

static void promo_asm_reset(promo_asm *a)
{
    if (a->buf) free(a->buf);
    memset(a, 0, sizeof(*a));
}

static int promo_asm_fail(promo_asm *a, const char **why, const char *text)
{
    promo_asm_reset(a);
    if (why) *why = text;
    return PROMO_BAD;
}

/* 喂一片（`p` / `len` = 帧载荷，含 22 B 片头）。返回 PROMO_PART / PROMO_DONE / PROMO_BAD。
   PROMO_DONE 之后用 promo_asm_take() 把数据拿走（所有权交给调用方）。 */
static int promo_asm_feed(promo_asm *a, const unsigned char *p, unsigned len, const char **why)
{
    promo_head h;
    unsigned body;

    if (why) *why = "";
    if (!promo_parse_head(p, len, &h))
        return promo_asm_fail(a, why, "片头不完整");
    if (h.format != PROMO_FORMAT)
        return promo_asm_fail(a, why, "格式号不认识");
    if (h.index == 0) {
        promo_asm_reset(a);
        /* 每片至少 1 字节（空表是 1 片 0 字节）⇒ 片数不会比总长还多。 */
        if (h.count == 0 || h.total > PROMO_TOTAL_MAX || h.count > (h.total ? h.total : 1u))
            return promo_asm_fail(a, why, "第 0 片的总长 / 片数不像话");
        a->buf = (unsigned char *)malloc(h.total ? h.total : 1u);
        if (!a->buf)
            return promo_asm_fail(a, why, "内存不够");
        a->serial = h.serial;
        a->total = h.total;
        a->crc = h.crc;
        a->count = h.count;
        a->active = 1;
    }
    if (!a->active)
        return promo_asm_fail(a, why, "没收到第 0 片");
    if (h.serial != a->serial || h.total != a->total || h.crc != a->crc || h.count != a->count)
        return promo_asm_fail(a, why, "片头和第 0 片对不上");
    if (h.index != a->next || h.offset != a->got)
        return promo_asm_fail(a, why, "片号 / 偏移不连续");
    body = len - PROMO_HEADER_BYTES;
    if (body > a->total - a->got)
        return promo_asm_fail(a, why, "数据比总长还多");
    if (body) memcpy(a->buf + a->got, p + PROMO_HEADER_BYTES, body);
    a->got += body;
    a->next++;
    if (a->next < a->count) return PROMO_PART;
    if (a->got != a->total)
        return promo_asm_fail(a, why, "长度不对");
    if (promo_crc32(a->buf, a->total) != a->crc)
        return promo_asm_fail(a, why, "crc 不对");
    a->active = 0;
    return PROMO_DONE;
}

/* PROMO_DONE 之后拿走数据：返回 malloc 的缓冲（调用方 free），*size / *serial / *crc 填好。 */
static unsigned char *promo_asm_take(promo_asm *a, unsigned *size, unsigned *serial, unsigned *crc)
{
    unsigned char *buf = a->buf;
    if (size) *size = a->total;
    if (serial) *serial = a->serial;
    if (crc) *crc = a->crc;
    a->buf = NULL;
    promo_asm_reset(a);
    return buf;
}

/* --------------------------------------------------------------- 只读流 */

#define PROMO_VT_SLOTS   16          /* 0x00..0x3c，解析器只用 +0x14 / +0x18 */
#define PROMO_VT_ISEOF   (0x14 / 4)
#define PROMO_VT_READ    (0x18 / 4)

typedef void (*promo_fn)(void);

typedef struct promo_stream {
    const promo_fn *vt;             /* ★ 必须是第一格：游戏 `mov eax,[ecx]` 取虚表 */
    const unsigned char *data;
    unsigned size;
    unsigned pos;
    unsigned reads, eofs, traps;    /* 被调了几次（日志 / 夹具用） */
} promo_stream;

/* +0x18 Read(dst, n)：抄 min(n, 剩下的) 字节，返回抄了多少（读完了返回 0）。 */
static int PROMO_FASTCALL promo_stream_read(promo_stream *self, void *edx_unused, void *dst, unsigned n)
{
    unsigned left = self->size - self->pos;
    (void)edx_unused;
    self->reads++;
    if (n > left) n = left;
    if (n) {
        memcpy(dst, self->data + self->pos, n);
        self->pos += n;
    }
    return (int)n;
}

/* +0x14 IsEof()：读完了返回 1。解析器每读完一行问一次（`0x5d4bba: test al,al`）。 */
static int PROMO_FASTCALL promo_stream_eof(promo_stream *self)
{
    self->eofs++;
    return self->pos >= self->size;
}

/* 其余槽。解析器不该调到这里；调到了交给宿主报（bshook 抛异常，不返回）。 */
static int PROMO_FASTCALL promo_stream_trap(promo_stream *self)
{
    self->traps++;
    PROMO_STREAM_TRAP(self);
    return 0;
}

static const promo_fn PROMO_STREAM_VT[PROMO_VT_SLOTS] = {
    (promo_fn)promo_stream_trap, (promo_fn)promo_stream_trap,   /* +0x00 +0x04 */
    (promo_fn)promo_stream_trap, (promo_fn)promo_stream_trap,   /* +0x08 +0x0c */
    (promo_fn)promo_stream_trap, (promo_fn)promo_stream_eof,    /* +0x10 +0x14 IsEof */
    (promo_fn)promo_stream_read, (promo_fn)promo_stream_trap,   /* +0x18 Read  +0x1c */
    (promo_fn)promo_stream_trap, (promo_fn)promo_stream_trap,
    (promo_fn)promo_stream_trap, (promo_fn)promo_stream_trap,
    (promo_fn)promo_stream_trap, (promo_fn)promo_stream_trap,
    (promo_fn)promo_stream_trap, (promo_fn)promo_stream_trap,
};

static void promo_stream_open(promo_stream *s, const unsigned char *data, unsigned size)
{
    memset(s, 0, sizeof(*s));
    s->vt = PROMO_STREAM_VT;
    s->data = data;
    s->size = size;
}

/* ------------------------------------------------------- 给日志的小工具 */

/* 数一下 UTF-16LE 正文里有几节（行首的 `[`）。日志里和服务端那行「各链几条」对账用。 */
static unsigned promo_count_sections(const unsigned char *data, unsigned size)
{
    unsigned i, n = 0;
    int at_line_start = 1;
    for (i = 0; i + 1 < size; i += 2) {
        unsigned ch = promo_u16(data + i);
        if (ch == 0xFEFFu && i == 0) continue;                 /* BOM */
        if (at_line_start && ch == '[') n++;
        at_line_start = (ch == '\n');
    }
    return n;
}

#endif /* POPSHOT_PROMO_WIRE_H */
