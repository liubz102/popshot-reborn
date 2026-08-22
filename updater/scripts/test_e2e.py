"""端到端测试脚本：在沙箱里完整跑一遍更新器（不依赖 GitHub）。

搭的东西：
    <tmp>\\popshot-e2e\\                    沙箱包根
        BUILD.ver                          V0.0.9（旧版本）
        config\\server.config              server_address=127.0.0.1（连假门禁）
        game_patched\\BsPatcherChn.exe      新更新器（本工程产物）
        game_patched\\test.txt              旧内容
        runtime\\python\\python.exe         假「本机服务端」（sleep 挂着等被停）
    <tmp>\\popshot-e2e-decoy\\python.exe    诱饵：与本包无关的 python.exe
    <tmp>\\popshot-e2e-web\\                本地 http 服务目录
        manifest.json                      目标 V0.0.10
        update.zip                         假客户端包（含新 BUILD.ver、新 exe、新 test.txt）
    假门禁服务器：127.0.0.1:27799，重演握手后回 0xFE 拒绝帧（带 V0.0.10 文案）

验证点（和 python 版语义逐条对齐）：
    1. 探针被拒 -> 目标选 V0.0.10
    2. 下载 -> sha256 校验
    3. 应用：BUILD.ver 最后写；保护文件不覆盖；运行中的更新器自己改名
       .update_old 让位（新 exe 落原名）
    4. 更新后本地版本 = V0.0.10
    5. 停进程：包内 python 被树杀；诱饵 python 一个不碰且不引发
       「服务端结束不了」误报（V0.2 会话 50 真机踩坑的回归）
"""

import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import HTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # 仓库根
SERVER_DIR = os.path.join(ROOT, "server")
sys.path.insert(0, SERVER_DIR)
import simple                                            # noqa: E402

TARGET_VERSION = "0.2.8"
OLD_VERSION = "0.2.7"
OLD_WIRE = 2007          # 0.2.7 的线上编码（versioning.encode_wire）


CLEANUP_PROCS = []       # 测试自己起的进程，收尾（无论成败）统一带走
CLEANUP_PIDS = []        # 没有 Popen 句柄的（见 spawn_detached_sleeper）


def die(msg):
    print("!! E2E FAIL:", msg)
    for p in CLEANUP_PROCS:
        try:
            p.kill()
        except OSError:
            pass
    for pid in CLEANUP_PIDS:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True)
    sys.exit(1)


def pid_alive(pid):
    """事后核验用。绝不能在更新器运行期间持有目标进程句柄 —— 测试一攥
    句柄，杀掉的进程 pid 就一直可解析，「pid 失效被误判存活」的回归就
    被掩盖了（§238，上一版 e2e 正是这么漏掉的）。"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "if (Get-Process -Id %d -ErrorAction SilentlyContinue) {'y'} "
         "else {'n'}" % pid],
        capture_output=True, text=True, timeout=120).stdout.strip()
    return out == "y"


def spawn_detached_sleeper(python_exe):
    """复刻真实启动链：launch.ps1 用 Start-Process 起服务端后 powershell
    随即退出 —— 服务端 python 的爹死透了，没人攥它的进程句柄。
    返回 pid（不是 Popen，理由见 pid_alive）。"""
    # ★ PS 5.1 的 -ArgumentList 不给含空格的参数补引号（launch.ps1 同款坑），
    #   -c 的代码串必须自己内嵌双引号，否则 python 收到 `-c import` 就秒退。
    cmd = ("$p = Start-Process -FilePath '%s' "
           "-ArgumentList @('-c','\"import time; time.sleep(600)\"') "
           "-WindowStyle Hidden -PassThru; Write-Output $p.Id" % python_exe)
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                         capture_output=True, text=True, timeout=120)
    try:
        pid = int(out.stdout.strip())
    except ValueError:
        die("沙箱假服务端没起来（powershell 输出 %r / %r）"
            % (out.stdout, out.stderr))
    CLEANUP_PIDS.append(pid)
    return pid


def build_sandbox(sandbox, web):
    os.makedirs(os.path.join(sandbox, "game_patched"), exist_ok=True)
    os.makedirs(os.path.join(sandbox, "config"), exist_ok=True)
    with open(os.path.join(sandbox, "BUILD.ver"), "w", encoding="utf-8") as f:
        f.write('{"version": "V%s"}\n' % OLD_VERSION)
    with open(os.path.join(sandbox, "config", "server.config"),
              "w", encoding="utf-8") as f:
        # 玩家本地的 config\server.config（受保护，更新不许覆盖）
        f.write("# 测试配置\nserver_address = 127.0.0.1\n")
    with open(os.path.join(sandbox, "game_patched", "test.txt"), "w") as f:
        f.write("old content\n")
    shutil.copyfile(os.path.join(ROOT, "updater", "bin", "BsPatcherChn.exe"),
                    os.path.join(sandbox, "game_patched", "BsPatcherChn.exe"))

    # ---- 假更新包（顶层目录 + BUILD.ver + 新 exe + 新 test.txt） ----------
    top = "PopShot-portable-win64_V0-2-8"
    staging = os.path.join(web, "staging")
    if os.path.exists(staging):
        shutil.rmtree(staging)
    os.makedirs(os.path.join(staging, top, "game_patched"))
    os.makedirs(os.path.join(staging, top, "config"))
    with open(os.path.join(staging, top, "BUILD.ver"), "w", encoding="utf-8") as f:
        f.write('{"version": "V%s"}\n' % TARGET_VERSION)
    # 更新包里带一份模板 config\server.config —— 保护清单必须拦下它，
    # 不许覆盖玩家自己填的那份（zip 里没有这份的话，保护断言就是空转）。
    with open(os.path.join(staging, top, "config", "server.config"),
              "w", encoding="utf-8") as f:
        f.write("# 模板配置（更新不许覆盖玩家的）\nserver_address = 192.168.1.100\n")
    with open(os.path.join(staging, top, "game_patched", "test.txt"), "w") as f:
        f.write("new content after update\n")
    shutil.copyfile(os.path.join(ROOT, "updater", "bin", "BsPatcherChn.exe"),
                    os.path.join(staging, top, "game_patched", "BsPatcherChn.exe"))
    zpath = os.path.join(web, "update.zip")
    if os.path.exists(zpath):
        os.remove(zpath)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        # ★ 先写目录条目 —— 真实发布包（打包脚本）里有 18 个；旧代码
        #   的「紧凑下标当原始索引用」正是被「只有文件条目」的 zip 掩住
        #   的（§239：真机第一发就解到目录条目上报 zip 损坏）。
        for d in ("", "/game_patched", "/config"):
            zi = zipfile.ZipInfo(top + d + "/")
            zi.external_attr = 0x10            # 目录
            zf.writestr(zi, "")
        for base, _dirs, files in os.walk(staging):
            for name in files:
                full = os.path.join(base, name)
                zf.write(full, os.path.relpath(full, staging))
    shutil.rmtree(staging)

    digest = hashlib.sha256(open(zpath, "rb").read()).hexdigest()
    manifest = {
        "format": 1,
        "repo": "test/local",
        "releases": [{
            "version": TARGET_VERSION,
            "date": "2026-08-22",
            "url": "http://127.0.0.1:8123/update.zip",
            "size": os.path.getsize(zpath),
            "sha256": digest,
        }],
    }
    with open(os.path.join(web, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


class Gate(threading.Thread):
    """假门禁：收 4 字节版本号，无论值多少都回「请更新到 V0.0.10」。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.seen_wire = None
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 27799))
        self.sock.listen(4)
        self.running = True

    def run(self):
        while self.running:
            try:
                self.sock.settimeout(1.0)
                c, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                data = c.recv(4)
                if len(data) == 4:
                    dec = simple.SimpleCipher.client_to_server()
                    self.seen_wire = struct.unpack("<i", dec.decrypt(data))[0]
                    msg = "客户端版本过旧，请更新到 V%s 后再连接。" % TARGET_VERSION
                    payload = (struct.pack("<i", 1) +
                               struct.pack("<H", len(msg)) +
                               msg.encode("utf-16-le"))
                    frame = b"\xfe\x00" + struct.pack("<H", len(payload)) + payload
                    enc = simple.SimpleCipher.server_to_client()
                    c.sendall(enc.encrypt(frame))
            except OSError:
                pass
            finally:
                c.close()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass


def spawn_sleeper(python_exe, extra_env=None):
    """起一个挂着 sleep 的 python（模拟本机服务端 / 无关 python）。"""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    # CREATE_NO_WINDOW：不闪控制台；DETACHED 会让 python 找不到控制台句柄。
    proc = subprocess.Popen(
        [python_exe, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, creationflags=0x08000000)
    CLEANUP_PROCS.append(proc)
    return proc


def setup_fake_pythons(sandbox, decoy_dir):
    """沙箱里摆两个 python.exe：
       - sandbox\\runtime\\python\\ = 假「本机服务端」（更新器该杀；
         爹已退出、无人攥句柄 = 真实启动链形态，§238 回归）
       - decoy_dir                = 诱饵（更新器绝不许碰）。
       返回 (package_pid, decoy_proc)。"""
    src_dir = os.path.join(ROOT, "runtime", "python")
    py = os.path.join(src_dir, "python.exe")
    if not os.path.exists(py):
        die("找不到 runtime\\python\\python.exe：%s" % py)
    for dst_dir in (os.path.join(sandbox, "runtime", "python"), decoy_dir):
        os.makedirs(dst_dir, exist_ok=True)
        # 嵌入式发行版：python.exe + 同目录 DLL 就能跑（time 是内建模块）。
        shutil.copyfile(py, os.path.join(dst_dir, "python.exe"))
        for dll in os.listdir(src_dir):
            if dll.lower().endswith(".dll"):
                shutil.copyfile(os.path.join(src_dir, dll),
                                os.path.join(dst_dir, dll))
    return (spawn_detached_sleeper(os.path.join(sandbox, "runtime", "python",
                                                "python.exe")),
            spawn_sleeper(os.path.join(decoy_dir, "python.exe")))


def main():
    tmp = tempfile.mkdtemp(prefix="popshot-e2e-")
    sandbox = os.path.join(tmp, "sandbox")
    web = os.path.join(tmp, "web")
    decoy_dir = os.path.join(tmp, "decoy")
    os.makedirs(sandbox)
    os.makedirs(web)
    print("sandbox:", sandbox)

    build_sandbox(sandbox, web)
    pkg_pid, decoy_py = setup_fake_pythons(sandbox, decoy_dir)
    # 给假服务端一点成形时间（慢机器上 image path 查询要进程已成形）。
    time.sleep(1.5)
    if not pid_alive(pkg_pid):
        die("沙箱假服务端 python 没起来（pid=%d 查无此进程）" % pkg_pid)
    if decoy_py.poll() is not None:
        die("诱饵 python 没起来（exit=%r）" % decoy_py.returncode)
    print("fake package python (orphaned parent, no handles) + decoy running")

    def handler_factory(*args, **kwargs):
        return SimpleHTTPRequestHandler(*args, directory=web, **kwargs)

    httpd = HTTPServer(("127.0.0.1", 8123), handler_factory)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # 27799 空闲才起假门禁（开发机上常驻着真本机服务端，绝不能抢端口）。
    gate = None
    try:
        gate = Gate()
        gate.start()
        probe_expected = True
        print("fake gate listening on 27799 (probe-reject path)")
    except OSError:
        gate = None
        probe_expected = False
        # 探针连 127.0.0.1:27799 会撞上真服务器（要么拒绝要么收不了话），
        # 无论哪种，更新器都该退回「最新版」路径 —— 把门禁验证换掉。
        print("27799 busy - probe falls back to newest-release path")
    if gate is None:
        # 让探针走「连不上」：指向一个没人听的端口语义做不到（端口写死），
        # 实际会撞真服务器 —— 真服务器也回不了我们能解析的话就等于
        # unreachable。这里只断言更新器没被卡死即可。
        pass

    env = dict(os.environ)
    env["POPSHOT_UPDATER_NOUI"] = "1"
    exe = os.path.join(sandbox, "game_patched", "BsPatcherChn.exe")
    t0 = time.time()
    proc = subprocess.run(
        [exe, "--manifest-url", "http://127.0.0.1:8123/manifest.json"],
        env=env, capture_output=True, timeout=180)
    print("updater exit=%d, %.1fs" % (proc.returncode, time.time() - t0))

    httpd.shutdown()
    if gate:
        gate.stop()

    # ---- 停进程断言（§236/§238 回归：爹死透的服务端要真被停、诱饵不碰、
    #      不误报「结束不了」） ------------------------------------------------
    time.sleep(2)          # 杀掉的 pid 收尾有零点几秒
    if pid_alive(pkg_pid):
        die("沙箱假服务端 python 没被更新器停掉（pid=%d 还活着）" % pkg_pid)
    print("package python terminated by updater (pid=%d, handle-free)" % pkg_pid)

    if decoy_py.poll() is not None:
        die("诱饵 python 被更新器误杀了（按路径精确匹配失效）")
    print("decoy python untouched (still running) - no false 'cannot stop'")
    decoy_py.kill()
    decoy_py.wait(timeout=10)

    # ---- 断言 ------------------------------------------------------------
    if gate is not None:
        if gate.seen_wire is None:
            die("假门禁没等到探针握手（探针没连 127.0.0.1:27799）")
        print("probe wire version seen:", gate.seen_wire,
              "(%d == V%s)" % (OLD_WIRE, OLD_VERSION))
        if gate.seen_wire != OLD_WIRE:
            die("探针上报的版本号不对：期望 %d（V%s 编码），实际 %r"
                % (OLD_WIRE, OLD_VERSION, gate.seen_wire))

    ver = open(os.path.join(sandbox, "BUILD.ver"), encoding="utf-8").read()
    if TARGET_VERSION not in ver:
        die("BUILD.ver 没更新到 V%s：%r" % (TARGET_VERSION, ver))
    print("BUILD.ver ->", ver.strip())

    body = open(os.path.join(sandbox, "game_patched", "test.txt")).read()
    if body != "new content after update\n":
        die("test.txt 没被新内容覆盖：%r" % body)
    print("test.txt updated")

    cfg = open(os.path.join(sandbox, "config", "server.config"),
               encoding="utf-8").read()
    if "server_address = 127.0.0.1" not in cfg or "# 测试配置" not in cfg:
        die("config/server.config 被覆盖了（保护清单失效）：%r" % cfg)
    print("config/server.config preserved (protected)")

    old = os.path.join(sandbox, "game_patched", "BsPatcherChn.exe.update_old")
    new = os.path.join(sandbox, "game_patched", "BsPatcherChn.exe")
    if not os.path.exists(old):
        die("运行中的更新器没有改名让位（缺 BsPatcherChn.exe.update_old）")
    if not os.path.exists(new):
        die("新的 BsPatcherChn.exe 没落位")
    print("rename dance ok: .update_old + new exe in place")

    if os.path.exists(os.path.join(sandbox, ".popshot-apply-0")):
        die("staging 目录没清理")
    print("staging cleaned")

    log = os.path.join(sandbox, "logs", "updater.log")
    if os.path.exists(log):
        tail = open(log, encoding="utf-8", errors="replace").read()
        print("---- updater.log ----")
        print(tail)
    print("==== E2E PASS ====")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
