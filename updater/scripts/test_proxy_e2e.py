"""test_proxy_e2e.py —— 端到端跑一遍「测速选源」（全本地，不依赖 GitHub）。

搭的东西：
    <tmp>\\sandbox-a\\ / sandbox-b\\   两个沙箱包根（BUILD.ver = V0.2.7，
                                     config\\update.config 各不相同）
    本地 http 127.0.0.1:8126（多线程）：
        /manifest-a.json  /manifest-b.json   目标 V0.2.201 / V0.2.202
        /update-a.zip（4 MB）/update-b.zip（1 MB）「直连」= 限速 256 KB/s（< 1 MiB/s）
        /fast/<原地址>    全速          —— 该被选中的代理
        /fast2/<原地址>   全速          —— 排在第二组（第 6 个），不该被测到
        /slow/<原地址>    128 KB/s      —— 不达标
        /stall/<原地址>   发 64 KB 就挂住 —— 断流
        http://127.0.0.1:1 / :2 连不上   —— 死代理

测速窗口 = 每个来源 10 秒、5 个一组（speedtest.h 的 SPEED_WINDOW_MS / SPEED_GROUP）；
manifest 兜底仍是 5 秒（MANIFEST_ATTEMPT_MS）。改了宏记得改这里的 WINDOW_MS / GROUP。

场景 A：直连慢 → 第一组 [死, 断流, 慢, 快, 死2] 里「快」达标 → 用它下载，第二组
       （fast2）一个请求都不该有；下载地址 = 代理前缀 + 原地址。
       顺带验窗口：直连那一测该是 ~10000 ms、~2.5 MB（256 KB/s × 10 s）。
场景 B：直连慢、代理只有 [死, 慢] → 全不达标 → 相对最快 = 直连（256 > 128 KB/s），
       日志 best-effort，包照样装上。
场景 C：manifest 的代理兜底 —— 直连的 /manifest-c.json 有头没身挂住，更新器 5 秒
       到点换代理（[死, 快] 随机顺序）取到 → 目标 V0.2.203；zip 走 /quick/ 全速直连。
场景 D：★ 更新源由**服务器**下发。本机 update.config 写着错的清单地址 + 死代理，
       假服务器在 /api/update-config 上给出正确地址 + 快代理（不传 --manifest-url）
       → 装上 V0.2.204、下载走服务器给的代理、本机那个错地址一次都没被请求。
场景 E：问不到服务器（端口没人监听）→ 安静退回本机 update.config，更新照跑。

跑法（先 updater\\build.bat）：
    runtime\\python\\python.exe updater\\scripts\\test_proxy_e2e.py
"""

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
EXE = os.path.join(ROOT, "updater", "bin", "BsPatcherChn.exe")
PORT = 8126
BASE = "http://127.0.0.1:%d" % PORT
OLD_VERSION = "0.2.7"
CHUNK = 64 * 1024
DIRECT_BPS = 256 * 1024          # 「直连」限速：0.25 MiB/s，不达标
SLOW_BPS = 128 * 1024
WINDOW_MS = 10000                # = speedtest.h SPEED_WINDOW_MS
GROUP = 5                        # = speedtest.h SPEED_GROUP

REQUESTS = []                    # 服务器收到的所有请求路径（断言第二组没被测）
REQ_LOCK = threading.Lock()
ZIPS = {}                        # name -> bytes
MANIFESTS = {}                   # name -> json bytes

#: 场景 D：假「服务器」在 /api/update-config 上下发的那份更新源。
SERVED_UPDATE_CONFIG = ""


def die(msg):
    print("!! PROXY-E2E FAIL:", msg)
    sys.exit(1)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, data, bps):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        sent = 0
        try:
            while sent < len(data):
                piece = data[sent:sent + CHUNK]
                self.wfile.write(piece)
                self.wfile.flush()
                sent += len(piece)
                if bps:
                    time.sleep(len(piece) / float(bps))
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass                 # 更新器到点关连接，正常

    def do_GET(self):
        path = self.path
        with REQ_LOCK:
            REQUESTS.append(path)
        if path == "/api/update-config":
            # 场景 D：这台「服务器」下发的更新源。收件那头是更新器的
            # resolve_update_source()（server\web\server.py 是真货，这里是夹具）。
            body = SERVED_UPDATE_CONFIG.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        m = re.search(r"(manifest-[abcd]\.json)$", path)
        if m:
            name = m.group(1)
            if name == "manifest-c.json" and path == "/" + name:
                # 场景 C：直连的 manifest 有头没身挂住 —— 逼更新器 5 秒到点换代理。
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "4096")
                self.end_headers()
                time.sleep(20)
                return
            body = MANIFESTS.get(name)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        m = re.search(r"(update-[abcd]\.zip)$", path)
        if not m:
            self.send_error(404)
            return
        data = ZIPS[m.group(1)]
        if path.startswith("/fast") or path.startswith("/quick/"):
            self._send(data, None)            # /fast/ /fast2/ /quick/ 都全速
        elif path.startswith("/slow/"):
            self._send(data, SLOW_BPS)
        elif path.startswith("/stall/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data[:CHUNK])
                self.wfile.flush()
                time.sleep(60)
            except (BrokenPipeError, ConnectionAbortedError,
                    ConnectionResetError):
                pass
        else:                                  # 直连
            self._send(data, DIRECT_BPS)


class QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        pass                     # 到点断连的 traceback 不刷屏


def make_zip(version, pad_bytes):
    """假客户端包：顶层目录 + BUILD.ver + game_patched\\test.txt + pad.bin
    （随机字节、STORED，让包真有这么大）。"""
    top = "PopShot-portable-win64_V" + version.replace(".", "-")
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for d in ("", "/game_patched"):
            zi = zipfile.ZipInfo(top + d + "/")
            zi.external_attr = 0x10
            zf.writestr(zi, "")
        zf.writestr(top + "/BUILD.ver", '{"version": "V%s"}\n' % version)
        zf.writestr(top + "/game_patched/test.txt",
                    "new content %s\n" % version)
        zf.writestr(top + "/pad.bin", os.urandom(pad_bytes))
    return buf.getvalue()


def make_manifest(version, zip_name, prefix=""):
    data = ZIPS[zip_name]
    return json.dumps({
        "format": 1, "repo": "test/local",
        "releases": [{
            "version": version, "date": "2026-09-07",
            "url": "%s%s/%s" % (BASE, prefix, zip_name),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }],
    }).encode()


def log_seconds_between(log, first_marker, second_marker):
    """两条日志行时间戳之差（秒，1 秒分辨率）；找不到返回 None。"""
    def stamp(marker):
        for line in log.splitlines():
            if marker in line:
                # ★ 秒后面**不要**再匹配 `]`：日志时间戳现在带时区
                # （`[2026-09-22 12:20:30 UTC+9]`，见「所有 log 标记时区」那次
                # 改动）。原来写死的 `\]` 从那以后一个都匹配不上，`stamp()`
                # 一直回 None —— 场景 C 的「5 秒到点」这条判据就此静默失效。
                m = re.match(r"\[(\d+)-(\d+)-(\d+) (\d+):(\d+):(\d+)", line)
                if m:
                    h, mi, s = int(m.group(4)), int(m.group(5)), int(m.group(6))
                    return h * 3600 + mi * 60 + s
        return None
    a, b = stamp(first_marker), stamp(second_marker)
    if a is None or b is None:
        return None
    return (b - a) % 86400


def build_sandbox(tmp, tag, proxies, manifest_url=None, register_port=None):
    """一个假包根。

    `manifest_url` / `register_port` 只有场景 D 用得上：前者往本机
    `update.config` 里写一个**故意是错的**清单地址（证明服务器那份压过它），
    后者告诉更新器去哪个端口要更新源。
    """
    sandbox = os.path.join(tmp, "sandbox-" + tag)
    os.makedirs(os.path.join(sandbox, "game_patched"))
    os.makedirs(os.path.join(sandbox, "config"))
    with open(os.path.join(sandbox, "BUILD.ver"), "w", encoding="utf-8") as f:
        f.write('{"version": "V%s"}\n' % OLD_VERSION)
    with open(os.path.join(sandbox, "config", "server.config"), "w",
              encoding="utf-8") as f:
        f.write("server_address = 127.0.0.1\n")
        if register_port:
            f.write("server_register_port = %d\n" % register_port)
    with open(os.path.join(sandbox, "config", "update.config"), "w",
              encoding="utf-8") as f:
        f.write("# 测试用代理列表\n")
        if manifest_url:
            f.write("manifest_url = %s\n" % manifest_url)
        for p in proxies:
            f.write(p + "\n")
    with open(os.path.join(sandbox, "game_patched", "test.txt"), "w") as f:
        f.write("old content\n")
    shutil.copyfile(EXE, os.path.join(sandbox, "game_patched",
                                      "BsPatcherChn.exe"))
    return sandbox


def clear_cache(version):
    cache = os.path.join(os.environ.get("TEMP", "."),
                         "popshot-update-%s.zip" % version)
    if os.path.exists(cache):
        os.remove(cache)


def run_updater(sandbox, manifest):
    """`manifest=None` = **不传** `--manifest-url`，让更新器自己去问服务器
    要更新源（场景 D）。传了就是老路：跳过问服务器那一步，直连用这个地址。"""
    env = dict(os.environ)
    env["POPSHOT_UPDATER_NOUI"] = "1"
    exe = os.path.join(sandbox, "game_patched", "BsPatcherChn.exe")
    argv = [exe]
    if manifest:
        argv += ["--manifest-url", "%s/%s" % (BASE, manifest)]
    t0 = time.time()
    proc = subprocess.run(argv, env=env, capture_output=True, timeout=300)
    dt = time.time() - t0
    log_path = os.path.join(sandbox, "logs", "updater.log")
    log = open(log_path, encoding="utf-8", errors="replace").read() \
        if os.path.exists(log_path) else ""
    return proc.returncode, dt, log


def need(cond, msg, log):
    if not cond:
        print("---- updater.log ----\n%s---------------------" % log)
        die(msg)


def scenario_a(tmp):
    with REQ_LOCK:
        REQUESTS.clear()
    # 第一组正好 GROUP 个（快的排第 4），fast2 是第 GROUP+1 个 = 第二组的头一个。
    proxies = ["http://127.0.0.1:1", BASE + "/stall", BASE + "/slow",
               BASE + "/fast", "http://127.0.0.1:2"]
    assert len(proxies) == GROUP
    proxies.append(BASE + "/fast2")
    sandbox = build_sandbox(tmp, "a", proxies)
    clear_cache("0.2.201")
    rc, dt, log = run_updater(sandbox, "manifest-a.json")
    print("A: exit=%d %.1fs" % (rc, dt))
    need(rc == 0, "A: 更新器退出码 %d" % rc, log)
    need("local update.config: %d proxies usable, 0 lines ignored"
         % (GROUP + 1) in log, "A: 代理列表没读对", log)
    need("--manifest-url given, server not asked" in log,
         "A: 传了 --manifest-url 却还是去问了服务器", log)
    need("speedtest pick: proxy[3] %s/fast (" % BASE in log and
         "qualified)" in log, "A: 没选中「快」代理", log)
    need("download %s/fast/%s/update-a.zip" % (BASE, BASE) in log,
         "A: 下载地址不是 代理前缀/原地址", log)
    need("代理地址：%s/fast" % BASE in log, "A: 界面状态行没带代理地址", log)
    m = re.search(r"speedtest direct \S+: (\d+) B / (\d+) ms", log)
    need(m is not None, "A: 日志里没有直连测速结果", log)
    d_bytes, d_ms = int(m.group(1)), int(m.group(2))
    print("A: direct probe %d B in %d ms" % (d_bytes, d_ms))
    need(WINDOW_MS - 1000 <= d_ms <= WINDOW_MS,
         "A: 直连测速窗口不是 ~%d s（%d ms）" % (WINDOW_MS // 1000, d_ms), log)
    expect = DIRECT_BPS * WINDOW_MS // 1000
    need(expect * 0.6 <= d_bytes <= expect * 1.4,
         "A: 直连 %d 秒收到 %d B，不像 256 KB/s（期望 ~%d）"
         % (WINDOW_MS // 1000, d_bytes, expect), log)
    m = re.search(r"speedtest proxy\[1\] \S+: (\d+) B / (\d+) ms", log)
    need(m is not None and int(m.group(1)) == CHUNK,
         "A: 断流代理该只收到 64 KB", log)
    m = re.search(r"speedtest proxy\[0\] \S+: (\d+) B", log)
    need(m is not None and int(m.group(1)) == 0, "A: 死代理该是 0 B", log)
    with REQ_LOCK:
        reqs = list(REQUESTS)
    need(not any(p.startswith("/fast2/") for p in reqs),
         "A: 第二组的 fast2 被测到了：%r" % reqs, log)
    need(any(p.startswith("/fast/") for p in reqs),
         "A: 服务器没收到走 /fast/ 的请求：%r" % reqs, log)
    ver = open(os.path.join(sandbox, "BUILD.ver"), encoding="utf-8").read()
    need("0.2.201" in ver, "A: BUILD.ver 没更新：%r" % ver, log)
    body = open(os.path.join(sandbox, "game_patched", "test.txt")).read()
    need(body == "new content 0.2.201\n", "A: test.txt 没覆盖：%r" % body, log)
    print("A: PASS (proxy /fast picked, group 2 untouched, package applied)")


def scenario_b(tmp):
    with REQ_LOCK:
        REQUESTS.clear()
    proxies = ["http://127.0.0.1:1", BASE + "/slow"]
    sandbox = build_sandbox(tmp, "b", proxies)
    clear_cache("0.2.202")
    rc, dt, log = run_updater(sandbox, "manifest-b.json")
    print("B: exit=%d %.1fs" % (rc, dt))
    need(rc == 0, "B: 更新器退出码 %d" % rc, log)
    need("speedtest pick: direct (" in log and "best-effort)" in log,
         "B: 全不达标时没退回直连 best-effort", log)
    need("download %s/update-b.zip" % BASE in log, "B: 下载地址不是直连", log)
    need("代理地址：直连Github" in log, "B: 界面状态行没写「直连Github」", log)
    need("speedtest direct" in log and "(complete)" in log,
         "B: 1 MB 的包直连 4 秒该在窗口内收完（complete）", log)
    ver = open(os.path.join(sandbox, "BUILD.ver"), encoding="utf-8").read()
    need("0.2.202" in ver, "B: BUILD.ver 没更新：%r" % ver, log)
    print("B: PASS (nothing qualified -> direct best-effort, package applied)")


def scenario_c(tmp):
    with REQ_LOCK:
        REQUESTS.clear()
    proxies = ["http://127.0.0.1:1", BASE + "/fast"]
    sandbox = build_sandbox(tmp, "c", proxies)
    clear_cache("0.2.203")
    rc, dt, log = run_updater(sandbox, "manifest-c.json")
    print("C: exit=%d %.1fs" % (rc, dt))
    need(rc == 0, "C: 更新器退出码 %d" % rc, log)
    need("manifest direct: failed (5 秒内没取到)" in log,
         "C: 直连 manifest 没按「5 秒内没取到」失败", log)
    need("manifest proxy[1] %s/fast: ok" % BASE in log,
         "C: 没通过「快」代理取到 manifest", log)
    need("target V0.2.203" in log, "C: 目标版本不对（manifest 内容没用上）", log)
    gap = log_seconds_between(log, "local update.config:",
                              "manifest direct: failed")
    need(gap is not None and 4 <= gap <= 7,
         "C: 直连 manifest 该在 ~5 秒到点（实际 %r 秒）" % gap, log)
    need("正在尝试第 " in log and "个代理：" in log,
         "C: 界面状态行没报「正在尝试第 N/M 个代理」", log)
    with REQ_LOCK:
        reqs = list(REQUESTS)
    need(any(p.endswith("/manifest-c.json") and p.startswith("/fast/")
             for p in reqs), "C: 服务器没收到代理路径的 manifest 请求：%r" % reqs, log)
    ver = open(os.path.join(sandbox, "BUILD.ver"), encoding="utf-8").read()
    need("0.2.203" in ver, "C: BUILD.ver 没更新：%r" % ver, log)
    print("C: PASS (manifest via proxy fallback after 5 s direct timeout)")


def scenario_d(tmp):
    """★ 更新源由**服务器**下发：manifest 地址和代理列表都以它为准。

    本机 `config\\update.config` 里故意写着**错的**清单地址和一个**死**代理；
    假服务器在 `/api/update-config` 上给出正确的清单地址和一个快代理。
    更新器必须用服务器那份 —— 判据是：本机那个错地址服务器**一次都没被请求过**，
    包按 manifest-d 装上了，而且下载走的是服务器给的那个代理。
    """
    global SERVED_UPDATE_CONFIG
    with REQ_LOCK:
        REQUESTS.clear()
    SERVED_UPDATE_CONFIG = (
        "# 服务器下发的更新源\n"
        "manifest_url = %s/manifest-d.json\n"
        "%s/fast\n" % (BASE, BASE))
    sandbox = build_sandbox(
        tmp, "d", ["http://127.0.0.1:1"],        # 本机：一个死代理
        manifest_url="%s/manifest-WRONG.json" % BASE,   # 本机：错的清单地址
        register_port=PORT)
    clear_cache("0.2.204")
    rc, dt, log = run_updater(sandbox, None)     # ★ 不传 --manifest-url
    print("D: exit=%d %.1fs" % (rc, dt))
    need(rc == 0, "D: 更新器退出码 %d" % rc, log)
    need("update source: asking %s/api/update-config" % BASE in log,
         "D: 更新器没去问服务器要更新源", log)
    need("update source: from server, 1 proxies usable" in log,
         "D: 没用上服务器下发的那份（代理列表）", log)
    need("manifest %s/manifest-d.json" % BASE in log,
         "D: 清单地址不是服务器给的那个", log)
    need("target V0.2.204" in log, "D: 目标版本不对（用错了 manifest）", log)
    need("download %s/fast/%s/update-d.zip" % (BASE, BASE) in log,
         "D: 下载没走服务器下发的那个代理", log)
    need("仓库：127.0.0.1:%d/manifest-d.json" % PORT in log,
         "D: 界面状态行没写清仓库地址", log)
    with REQ_LOCK:
        reqs = list(REQUESTS)
    need(not any("manifest-WRONG" in p for p in reqs),
         "D: 本机那个错地址被用了：%r" % reqs, log)
    need(any(p == "/api/update-config" for p in reqs),
         "D: 服务器没收到取更新源的请求：%r" % reqs, log)
    ver = open(os.path.join(sandbox, "BUILD.ver"), encoding="utf-8").read()
    need("0.2.204" in ver, "D: BUILD.ver 没更新：%r" % ver, log)
    print("D: PASS (manifest url + proxies both came from the server)")


def scenario_e(tmp):
    """服务器问不到时安静退回本机那份 —— 老服务端 / 服务器没开都走这条。

    `server_register_port` 指到一个没人监听的端口，更新器该在日志里记一句
    「server unavailable」然后照常用本机的 `update.config` 把包装上。
    """
    with REQ_LOCK:
        REQUESTS.clear()
    sandbox = build_sandbox(
        tmp, "e", [BASE + "/fast"],
        manifest_url="%s/manifest-d.json" % BASE,
        register_port=1)                         # 没人监听
    clear_cache("0.2.204")
    rc, dt, log = run_updater(sandbox, None)
    print("E: exit=%d %.1fs" % (rc, dt))
    need(rc == 0, "E: 更新器退出码 %d" % rc, log)
    need("update source: server unavailable" in log and "using local" in log,
         "E: 没记下「问不到服务器，退回本机」", log)
    need("manifest %s/manifest-d.json" % BASE in log,
         "E: 没用本机 update.config 里的清单地址", log)
    ver = open(os.path.join(sandbox, "BUILD.ver"), encoding="utf-8").read()
    need("0.2.204" in ver, "E: BUILD.ver 没更新：%r" % ver, log)
    print("E: PASS (server unreachable -> local update.config, update still ran)")


def main():
    if not os.path.exists(EXE):
        die("找不到 %s（先跑 updater\\build.bat）" % EXE)
    ZIPS["update-a.zip"] = make_zip("0.2.201", 4 * 1024 * 1024)
    ZIPS["update-b.zip"] = make_zip("0.2.202", 1 * 1024 * 1024)
    ZIPS["update-c.zip"] = make_zip("0.2.203", 1 * 1024 * 1024)
    ZIPS["update-d.zip"] = make_zip("0.2.204", 1 * 1024 * 1024)
    MANIFESTS["manifest-a.json"] = make_manifest("0.2.201", "update-a.zip")
    MANIFESTS["manifest-b.json"] = make_manifest("0.2.202", "update-b.zip")
    MANIFESTS["manifest-c.json"] = make_manifest("0.2.203", "update-c.zip",
                                                 prefix="/quick")
    MANIFESTS["manifest-d.json"] = make_manifest("0.2.204", "update-d.zip")

    httpd = QuietServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # 假门禁（test_e2e 的那只）：27799 空闲才起；被真服务端占着也没关系 ——
    # 探针无论被拒还是听不懂，都会退到 manifest 最新版这条路。
    gate = None
    try:
        sys.path.insert(0, HERE)
        from test_e2e import Gate
        gate = Gate()
        gate.start()
        print("fake gate on 27799")
    except OSError:
        gate = None
        print("27799 busy - probe falls back to newest-release path")

    tmp = tempfile.mkdtemp(prefix="popshot-proxy-e2e-")
    print("tmp:", tmp)
    try:
        scenario_a(tmp)
        scenario_b(tmp)
        scenario_c(tmp)
        scenario_d(tmp)
        scenario_e(tmp)
    finally:
        httpd.shutdown()
        if gate:
            gate.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    print("==== PROXY-E2E PASS ====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
