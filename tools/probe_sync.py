#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_sync.py —— 「我打不死人」现场探针：读客户端**每座位收包队列**的状态
（bug调查/9 / FINDINGS §216）

## 为什么要它

同步数据分两段走（§151）：内层 opcode `>= 0x4000`（`0x4001` 心跳，**位置就在
它 body 里**）立刻处理；`< 0x4000`（开火 / 命中 / 伤害等**全部战斗事件**）
先进每座位一条的 `PktQueue` 排队去重。所以「看得见人在动、就是打不着他」
= 心跳这一段好的、事件那一段被丢了，而丢包点只可能在 `PktQueue`：

    Insert  0x54bb8c   seq < base 丢弃；槽位已占（收过）丢弃
    FlushTo 0x54bb1d   n < base 直接 return；否则 base = n 并置「已激活」
    消费者  0x407c84   「已激活」== 0 且不是自己的座位 -> 整个座位跳过

`base` **只进不退**。所以只要读到「某个座位的 base 明显高于对方现在发到的
号」或者「已激活恒为 0」，就当场坐实是哪一条。

## 读什么

    GameSession = [0x72E29C]

    +0x04            阶段（0x5517a6 写 4 / 0x551904 写 2）
    +0x1c / +0x20    房间描述符 type / arguments[0]（0x409df1 的两个入参）
    +0x3c            ★ 局号 = 收包队列的纪元号（和 ResetQueues 同一个基本块）
    +0x1cc           我的座位
    +0x2c0           自己的**发送**队列（重传用）
    +0x2e4 + 座位*0x24   ★ 该座位的收包队列
    +0x3c0 + 座位*4  该座位最后一发心跳的时刻
    +0x3da / +0x3f8  目标座位是否校验 / 是否发 0x4002 讨重传
    座位*0x3c +0x40  占用     座位*0x3c +0x48  队伍号

    PktQueue（0x24 字节）
    +0x04 已激活   +0x08 base   +0x0c 上界   +0x10 空槽数
    +0x14 扫描游标 +0x18/+0x1c/+0x20 vector 的 begin/end/cap

## 用法

    python tools/probe_sync.py <pid>                     # 打一次快照就退
    python tools/probe_sync.py auto inf --log 路径.log   # ★ 长期盯到游戏退出
    python tools/probe_sync.py <pid> 120                 # 盯 120 秒
    python tools/probe_sync.py <pid> 120 0.2             # 自定采样间隔

pid 写 `auto` = 自己找 BigShot.exe；再配 `--wait-game`，游戏还没起来就等着，
所以可以在拉客户端**之前**就把探针挂上（start-debug.bat 走的正是这条路）。
时长写 `inf` / `forever` / `0` = 不限时，游戏进程一退探针跟着收工。

长期模式下三件事保证日志还能看：
  * 只在数值变化时记一行；无变化也每 `--heartbeat` 秒记一行（确认探针活着 + 对时刻）；
  * 换局单独拉一条横幅（局号 = 队列纪元号，这个 bug 就发生在换局那一刹那）；
  * 判据命中只报**新出现**的那一次，并且日志超过 `--max-mb` 自动滚动。

★ 窗口不在前台时游戏主循环基本不跑，读到的值会「冻住」，别据此下结论。
"""
import argparse
import ctypes as C
import os
import sys
import time
from ctypes import wintypes as W

k32 = C.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.ReadProcessMemory.argtypes = [W.HANDLE, W.LPCVOID, W.LPVOID,
                                  C.c_size_t, C.POINTER(C.c_size_t)]
k32.CreateToolhelp32Snapshot.restype = W.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
k32.CloseHandle.argtypes = [W.HANDLE]
k32.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
k32.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR,
                                           C.POINTER(W.DWORD)]

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x0002
STILL_ACTIVE = 259
INVALID_HANDLE_VALUE = C.c_void_p(-1).value

#: `GameSession`（逆向笔记里也叫 LobbyStage）的全局指针。
GAME_SESSION = 0x72E29C

SEAT_COUNT = 6
SEAT_STRIDE = 0x3C            #: 每座位那份 UI/名牌记录的步长
QUEUE_BASE = 0x2E4            #: 第 0 条收包队列
QUEUE_STRIDE = 0x24
SEND_QUEUE = 0x2C0            #: 自己的发送队列（0x4002 重传从这里取）

GAME_EXE = b"bigshot.exe"
#: 局号刚变的这几秒里 `base` 从 0 跳上去 = 上一纪元的心跳漏进来了（§216 四）。
EPOCH_WINDOW_S = 8.0


class PROCESSENTRY32(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD),
                ("th32ProcessID", W.DWORD),
                ("th32DefaultHeapID", C.POINTER(C.c_ulong)),
                ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
                ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", C.c_long),
                ("dwFlags", W.DWORD), ("szExeFile", C.c_char * 260)]


k32.Process32First.argtypes = [W.HANDLE, C.POINTER(PROCESSENTRY32)]
k32.Process32Next.argtypes = [W.HANDLE, C.POINTER(PROCESSENTRY32)]


# ---------------------------------------------------------------------------
#  进程
# ---------------------------------------------------------------------------
def find_pids():
    """按映像名找 BigShot.exe，返回 pid 列表（小的在前）。找不到就是空列表。"""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = C.sizeof(PROCESSENTRY32)
        if not k32.Process32First(snap, C.byref(entry)):
            return []
        found = []
        while True:
            if entry.szExeFile.lower() == GAME_EXE:
                found.append(entry.th32ProcessID)
            if not k32.Process32Next(snap, C.byref(entry)):
                break
        return sorted(found)
    finally:
        k32.CloseHandle(snap)


def process_path(h):
    """进程的映像路径；读不到返回 None（不影响采样，只是日志头少一行）。"""
    size = W.DWORD(32768)
    buf = C.create_unicode_buffer(size.value)
    if k32.QueryFullProcessImageNameW(h, 0, buf, C.byref(size)):
        return buf.value
    return None


def process_alive(h):
    """句柄对应的进程还活着吗 —— 游戏一退，长期模式就该收工了。"""
    code = W.DWORD(0)
    if not k32.GetExitCodeProcess(h, C.byref(code)):
        return False
    return code.value == STILL_ACTIVE


# ---------------------------------------------------------------------------
#  内存读取
# ---------------------------------------------------------------------------
def read(h, addr, n):
    """读 n 字节；读不到返回 None（进程退了 / 地址没映射都走这条）。"""
    buf = (C.c_char * n)()
    got = C.c_size_t(0)
    if not addr or not k32.ReadProcessMemory(h, C.c_void_p(addr), buf, n,
                                             C.byref(got)):
        return None
    return bytes(buf[:got.value]) if got.value == n else None


def u32(h, addr):
    raw = read(h, addr, 4)
    return None if raw is None else int.from_bytes(raw, "little")


def i32(h, addr):
    raw = read(h, addr, 4)
    return None if raw is None else int.from_bytes(raw, "little", signed=True)


def u8(h, addr):
    raw = read(h, addr, 1)
    return None if raw is None else raw[0]


def room_mode(desc_type, arg0):
    """照抄 `0x409df1`：type==5 -> 1；type==1 -> arguments[0]；其余 -> -1。"""
    if desc_type == 5:
        return 1
    if desc_type == 1:
        return arg0
    return -1


def mode_text(mode, desc_type):
    if desc_type not in (1, 5):
        return f"非对战（描述符 type={desc_type}）"
    return "组队战" if mode == 1 else f"个人战（arguments[0]={mode}）"


def queue_at(h, addr):
    """读一条 `PktQueue`。返回字典；读不到返回 None。"""
    live = u8(h, addr + 0x04)
    if live is None:
        return None
    begin, end = u32(h, addr + 0x18), u32(h, addr + 0x1C)
    slots = None
    if begin is not None and end is not None and end >= begin:
        slots = (end - begin) // 4
    return {"addr": addr, "live": live,
            "base": i32(h, addr + 0x08), "limit": i32(h, addr + 0x0C),
            "pending": i32(h, addr + 0x10), "cursor": i32(h, addr + 0x14),
            "slots": slots}


def snapshot(h):
    """把这一刻的同步状态读成一个字典。"""
    session = u32(h, GAME_SESSION)
    if not session:
        return None
    desc_type, arg0 = i32(h, session + 0x1C), i32(h, session + 0x20)
    mode = room_mode(desc_type, arg0)
    state = {
        "session": session,
        "stage": i32(h, session + 0x04),
        "game_id": i32(h, session + 0x3C),
        "my_seat": i32(h, session + 0x1CC),
        "desc_type": desc_type, "arg0": arg0, "mode": mode,
        "no_target_check": u8(h, session + 0x3DA),
        "may_nak": u8(h, session + 0x3F8),
        "send_queue": queue_at(h, session + SEND_QUEUE),
        "seats": [],
    }
    for seat in range(SEAT_COUNT):
        row = seat * SEAT_STRIDE
        state["seats"].append({
            "seat": seat,
            "occupied": u8(h, session + row + 0x40),
            "team": u8(h, session + row + 0x48),
            "queue": queue_at(h, session + QUEUE_BASE + seat * QUEUE_STRIDE),
            "last_beat": i32(h, session + 0x3C0 + seat * 4),
            "hole_flag": u8(h, session + 0x2B0 + seat),
            "resend_flag": u8(h, session + 0x2B6 + seat),
        })
    return state


def key(state):
    """只在这些东西变了的时候才打一行 —— 否则 8 Hz 的日志没法看。"""
    if state is None:
        return None
    rows = []
    for s in state["seats"]:
        q = s["queue"] or {}
        rows.append((s["occupied"], s["team"], q.get("live"), q.get("base"),
                     q.get("limit"), q.get("pending"), q.get("slots"),
                     s["hole_flag"], s["resend_flag"]))
    return (state["stage"], state["game_id"], state["my_seat"],
            state["mode"], tuple(rows))


# ---------------------------------------------------------------------------
#  日志（长期模式要自己管文件，所以滚动也在这儿）
# ---------------------------------------------------------------------------
class Sink:
    """同时往控制台和日志文件写；文件超上限就滚动，长期跑不会撑爆磁盘。"""

    def __init__(self, path=None, max_bytes=0, keep=3, header=()):
        self.path = path
        self.max_bytes = max_bytes
        self.keep = max(1, keep)
        self.header = list(header)
        self.fh = None
        self.written = 0
        self.rotating = False
        if path:
            folder = os.path.dirname(os.path.abspath(path))
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            self._open()

    def _open(self):
        # ★ 用 os.path.getsize 而不是 fh.tell()：文本模式的 tell() 返回的是
        #   不透明的位置标记，拿它当字节数去比上限不可靠。
        existed = os.path.exists(self.path) and os.path.getsize(self.path) > 0
        self.fh = open(self.path, "a", encoding="utf-8", newline="\n")
        self.written = os.path.getsize(self.path) if existed else 0
        if not existed:
            for line in self.header:
                self.fh.write(line + "\n")
            self.fh.flush()
            self.written = os.path.getsize(self.path)

    def _rotate(self):
        """x.log -> x.1.log -> x.2.log …，超出 keep 份的直接删。"""
        # ★ 重入保护：新文件的 header 万一就顶到上限（--max-mb 给得极小时），
        #   下面那句 write 会再触发一次滚动，一路递归到爆栈。
        self.rotating = True
        try:
            self.fh.close()
            stem, ext = os.path.splitext(self.path)
            for idx in range(self.keep, 0, -1):
                older = "%s.%d%s" % (stem, idx, ext)
                newer = self.path if idx == 1 \
                    else "%s.%d%s" % (stem, idx - 1, ext)
                if os.path.exists(older):
                    os.remove(older)
                if os.path.exists(newer):
                    os.rename(newer, older)
            self._open()
            self.write("（上一段日志已滚动为 %s.1%s）"
                       % (os.path.basename(stem), ext))
        finally:
            self.rotating = False

    def write(self, text=""):
        try:
            print(text, flush=True)
        except Exception:
            pass          # 后台启动时没有可用的控制台，写不进去无所谓
        if not self.fh:
            return
        line = text + "\n"
        self.fh.write(line)
        self.fh.flush()
        self.written += len(line.encode("utf-8"))
        if self.max_bytes and self.written >= self.max_bytes \
                and not self.rotating:
            self._rotate()

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None


def stamp():
    """长期跑要跨小时甚至跨天，所以时刻带上月日和毫秒。"""
    now = time.time()
    return time.strftime("%m-%d %H:%M:%S", time.localtime(now)) + \
        ".%03d" % int((now % 1) * 1000)


# ---------------------------------------------------------------------------
#  判据（FINDINGS §216 的那张表，直接写成人话）
# ---------------------------------------------------------------------------
class Watcher:
    """跟踪每座位队列「有没有在动」，把 §216 的三种死法分开报。

    只报**新出现**的问题：同一局里同一个座位的同一种毛病只说一次，
    否则长期模式下一条 ⚠ 会刷几千行，真正的现场反而淹掉。
    换局（局号变）时全部清账重来。
    """

    def __init__(self, stall_s):
        self.stall_s = stall_s
        self.game_id = None
        # ★ 探针刚接上时 epoch_at 是空的：这一局已经开了多久我们并不知道，
        #   判据 ③（开局窗口）这一局就不参与 —— 否则中途挂探针会把「正常
        #   推进上去的 base」错当成上一纪元漏包，第一帧就误报。
        self.epoch_at = None
        self.seats = {}          # 座位 -> 上次「有变化」的签名和时刻
        self.reported = set()    # (座位, 毛病种类)

    def reset(self, game_id):
        """换局：局号一变就是六条队列同时清零的那一刻（§216 三）。"""
        self.game_id = game_id
        self.epoch_at = time.monotonic()
        self.seats = {}
        self.reported = set()

    def _track(self, seat, q, last_beat, now):
        sig = (q["live"], q["base"], q["limit"], q["cursor"])
        rec = self.seats.get(seat)
        if rec is None or rec["sig"] != sig:
            rec = {"sig": sig, "since": now,
                   "beat": last_beat, "beat_moved": now}
            self.seats[seat] = rec
            return rec
        if last_beat != rec["beat"]:
            rec["beat"] = last_beat
            rec["beat_moved"] = now      # 对方还活着，只是队列不动
        return rec

    def check(self, state):
        """返回这一帧**新冒出来**的告警（人话，每条一行）。"""
        now = time.monotonic()
        if self.game_id is None:
            self.game_id = state["game_id"]      # 首帧：静默接上，不算换局
        elif state["game_id"] != self.game_id:
            self.reset(state["game_id"])
        out = []
        me = state["my_seat"]
        fresh_epoch = self.epoch_at is not None and \
            (now - self.epoch_at) <= EPOCH_WINDOW_S
        for s in state["seats"]:
            q = s["queue"]
            if not s["occupied"] or q is None or s["seat"] == me:
                continue
            if q["base"] is None or q["limit"] is None:
                continue
            rec = self._track(s["seat"], q, s["last_beat"], now)
            who = f"座位 {s['seat']}"
            still = now - rec["since"]
            # ★ 对方的心跳最近还在推进 = 他在线、包在到。少了这一条，游戏切到
            #   后台（主循环不跑，什么都不动）时会满屏误报。
            peer_live = (now - rec["beat_moved"]) <= self.stall_s

            # ① 只入队不派发：心跳的 FlushTo 一次都没成功过（§216 结论 2）。
            if not q["live"] and q["limit"] > 0 and still >= self.stall_s \
                    and peer_live:
                self._say(out, s["seat"], "never-live",
                          f"   ⚠⚠ {who} 的队列**从没激活过**（+4=0），"
                          f"上界已经爬到 {q['limit']} —— 他的战斗事件"
                          f"**全程只入队、一发都不派发**"
                          f"（已经这样 {still:.0f} 秒）。")
                continue

            # ② 卡在空洞上：base 不动、上界照涨，消费者在等永远不来的号。
            if q["live"] and q["limit"] > q["base"] and \
                    still >= self.stall_s and peer_live:
                self._say(out, s["seat"], "stalled",
                          f"   ⚠⚠ {who} base={q['base']} 卡住不动 {still:.0f} 秒，"
                          f"上界却已到 {q['limit']}（差 {q['limit'] - q['base']} 发），"
                          f"空洞标记={s['hole_flag']} 重传标记={s['resend_flag']}"
                          f" —— 消费者停在空洞上，这个座位的事件全积着不生效。")
                continue

            # ③ 基线被钉死：开局那几秒 base 就从 0 跳上去 = 旧纪元心跳漏进来。
            if fresh_epoch and q["base"] > 0 and q["limit"] <= q["base"]:
                self._say(out, s["seat"], "poisoned-base",
                          f"   ⚠ {who} 换局后 {now - self.epoch_at:.1f} 秒内 base "
                          f"就被抬到 {q['base']}（上界 {q['limit']}）—— 像是"
                          f"上一纪元的心跳漏了进来，他新一局的头 {q['base']} "
                          f"发事件会被丢（§216 四）。")

            if q["slots"] and q["pending"] and q["pending"] >= q["slots"] > 0 \
                    and still >= self.stall_s and peer_live:
                self._say(out, s["seat"], "all-empty",
                          f"   ⚠ {who} 有 {q['pending']} 个槽位一直空着"
                          f"（上界 {q['limit']}）—— 像是在等永远不会来的号。")
        return out

    def _say(self, out, seat, kind, text):
        if (seat, kind) in self.reported:
            return
        self.reported.add((seat, kind))
        out.append(text)


def dump(sink, state, when, tag=""):
    head = f"[{when}]"
    if tag:
        head += f" {tag}"
    sink.write(f"{head} GameSession=0x{state['session']:08x} "
               f"阶段={state['stage']} 局号={state['game_id']} "
               f"我的座位={state['my_seat']} "
               f"模式={mode_text(state['mode'], state['desc_type'])} "
               f"目标座位免校验={state['no_target_check']} "
               f"可发重传请求={state['may_nak']}")
    q = state["send_queue"]
    if q:
        sink.write(f"    发送队列（自己的）: 已激活={q['live']} base={q['base']} "
                   f"上界={q['limit']} 槽位={q['slots']}")
    for s in state["seats"]:
        if not s["occupied"]:
            continue
        q = s["queue"] or {}
        mine = " ←我" if s["seat"] == state["my_seat"] else ""
        sink.write(f"    座位 {s['seat']}{mine} 队伍={s['team']} "
                   f"| 队列 已激活={q.get('live')} base={q.get('base')} "
                   f"上界={q.get('limit')} 待收={q.get('pending')} "
                   f"槽位={q.get('slots')} 游标={q.get('cursor')} "
                   f"| 空洞标记={s['hole_flag']} 重传标记={s['resend_flag']} "
                   f"上次心跳={s['last_beat']}")


# ---------------------------------------------------------------------------
#  主循环
# ---------------------------------------------------------------------------
def open_target(pid):
    return k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
                           False, pid)


def resolve_pid(spec, wait_game, sink):
    """把 `auto` 变成真 pid。`--wait-game` 时一直等到游戏起来为止。"""
    if str(spec).lower() != "auto":
        return int(spec)
    told = False
    while True:
        pids = find_pids()
        if pids:
            if len(pids) > 1:
                sink.write(f"※ 找到多个 BigShot.exe（{pids}），盯最早的那个。")
            return pids[0]
        if not wait_game:
            return None
        if not told:
            sink.write("等 BigShot.exe 起来…（客户端注入要十几秒，正常）")
            told = True
        time.sleep(1.0)


def parse_seconds(text):
    """时长：`inf` / `forever` / `0` 都表示不限时。"""
    if text is None:
        return None
    low = str(text).strip().lower()
    if low in ("inf", "infinite", "forever", "0"):
        return 0.0
    return float(low)


def build_parser():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("pid", nargs="?")
    p.add_argument("seconds", nargs="?")
    p.add_argument("interval", nargs="?", type=float, default=0.25)
    p.add_argument("--log")
    p.add_argument("--max-mb", type=float, default=64.0)
    p.add_argument("--keep", type=int, default=3)
    # 长期跑时没变化也定期记一行：既确认探针还活着，也方便回头对时刻。
    p.add_argument("--heartbeat", type=float, default=60.0)
    # 队列多久不动才算「卡死」。战斗中每次开火都有事件包，15 秒足够保守。
    p.add_argument("--stall", type=float, default=15.0)
    p.add_argument("--note", action="append", default=[])
    p.add_argument("--wait-game", action="store_true")
    p.add_argument("-h", "--help", action="store_true")
    return p


def main():
    args = build_parser().parse_args()
    if args.help or not args.pid:
        print(__doc__)
        return 0 if args.help else 2

    seconds = parse_seconds(args.seconds)
    forever = (seconds is not None and seconds <= 0)
    once = (seconds is None)

    header = ["==== 炮炮火枪手 玩家间同步（收包队列）现场取证 ====",
              "启动      : " + time.strftime("%Y-%m-%d %H:%M:%S")]
    header.extend(args.note)
    sink = Sink(args.log, int(args.max_mb * 1024 * 1024), args.keep, header)
    try:
        return run(args, sink, seconds, forever, once)
    except KeyboardInterrupt:
        sink.write(f"[{stamp()}] 手动中断，采样结束。")
        return 0
    finally:
        sink.close()


def run(args, sink, seconds, forever, once):
    pid = resolve_pid(args.pid, args.wait_game, sink)
    if pid is None:
        sink.write("没找到 BigShot.exe —— 游戏没在跑？")
        return 2

    h = open_target(pid)
    if not h:
        sink.write(f"OpenProcess 失败：{C.get_last_error()}（pid={pid} 还在吗？"
                   f"要不要用管理员身份再来一次？）")
        return 1

    sink.write(f"pid       : {pid}")
    exe = process_path(h)
    if exe:
        sink.write(f"exe       : {exe}")
    if once:
        span = "单次快照"
    elif forever:
        span = "不限时（游戏退出即收工）"
    else:
        span = f"{seconds:g} 秒"
    sink.write(f"采样      : {span} / 每 {args.interval:g} 秒一次"
               f"（只在数值变化时记一行，无变化也每 {args.heartbeat:g} 秒记一行）")
    sink.write("=" * 50)
    sink.write("")

    state = snapshot(h)
    if state is None and once:
        sink.write("读不到 GameSession —— 客户端还没进过房间/战斗？")
        return 1
    if state is not None:
        dump(sink, state, stamp())
    if once:
        return 0

    if state is None:
        sink.write(f"[{stamp()}] 还没进过房间（GameSession 还是空的），等着…")
    sink.write("")
    sink.write("★ 游戏窗口不在前台时主循环基本不跑，读到的值会冻住，别据此下结论。")
    sink.write("★ 打到「打不死人」那一把时，心里记一下大概时刻，回头对这份日志。")
    sink.write("")

    watcher = Watcher(args.stall)
    last = key(state)
    last_game_id = state["game_id"] if state else None
    last_line = time.monotonic()
    end = time.monotonic() + (seconds or 0)

    while True:
        time.sleep(args.interval)
        now = time.monotonic()
        if not forever and now >= end:
            break
        if not process_alive(h):
            sink.write(f"[{stamp()}] 游戏进程已退出，采样结束。")
            return 0
        state = snapshot(h)
        if state is None:
            continue

        # 换局 = 六条队列同时清零的那一刻（§216 三）。这个 bug 就发生在这一
        # 刹那，所以单独拉一条横幅出来，回头一眼能找到第二局从哪儿开始。
        if last_game_id is not None and state["game_id"] != last_game_id:
            sink.write("")
            sink.write(f"════════ 换局：局号 {last_game_id} → "
                       f"{state['game_id']}（阶段 {state['stage']}）"
                       f" @ {stamp()} ════════")
        last_game_id = state["game_id"]

        alerts = watcher.check(state)
        now_key = key(state)
        changed = (now_key != last)
        beat_due = args.heartbeat > 0 and (now - last_line) >= args.heartbeat
        if changed or beat_due or alerts:
            tag = ""
            if alerts:
                tag = "★ 命中判据"
            elif not changed:
                tag = "心跳"
            dump(sink, state, stamp(), tag)
            for line in alerts:
                sink.write(line)
            sink.write("")
            last = now_key
            last_line = now

    sink.write("采样结束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
