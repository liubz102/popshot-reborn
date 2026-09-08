"""test_cancel_ui.py —— 真窗口实测「下载中点取消」整条链路。

动机（V0.2 会话 50）：用户真机反馈下载过程中点「取消」没有用。
e2e 跑 --noui 测不到按钮；本脚本起**真实 UI 窗口**：
    - slow 场景：本地慢速 http（256 KB/s，8 MB ≈ 32 秒的点击窗口期）
    - stall 场景：服务器只发 64KB 就挂住 —— 复刻慢/断流下
      WinHttpQueryDataAvailable 长时间等不到数据，验证 0.5s 取消节拍
    - fast 场景（2026-09-08 加）：40 MB/s、400 MB —— 复刻用户真机
      「代理优选之后下载飞快」的处境。快源下每秒能收几千块，逐块刷界面
      会把 UI 线程的 post 队列灌满、饿死鼠标输入，取消按钮点了没反应。
      这一档专门盯住那个回归。
    - 原生档（--ui-mode 3）：UIAutomation 找名「取消」的元素点
    - IE 档（--ui-mode 1，默认档）：IE 的 DOM 不投影进 UIA，按坐标点

★ 两个夹具本身踩过的坑（2026-09-08）：
    1. IE 档的坐标以前写 (543,499)，实测按钮在窗口坐标 x 504~559 /
       y 477~495 —— 点在**下沿外 4 px**，一次都没点着；
    2. 判据以前只看「已取消更新」，而收尾的 CloseMainWindow 自己就会
       触发取消 —— 点没点着都能过。现在**先断言日志里有
       `ui: button btnCancel`**，点不着直接失败。

★ 需要交互桌面（窗口会短暂弹出）。跑法：
    runtime\\python\\python.exe updater\\scripts\\test_cancel_ui.py
        [1|3|both|stall|fast|all]
"""

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
EXE = os.path.join(ROOT, "updater", "bin", "BsPatcherChn.exe")
PORT = 8124

ZIP_SIZE = 8 << 20          # 8 MB
THROTTLE = 256 * 1024       # 256 KB/s → ~32s 下载期

FAST_ZIP_SIZE = 400 << 20   # 400 MB —— 和真机更新包一个量级
FAST_RATE = 40 << 20        # 40 MB/s，取自用户真机日志（378 MB / 9 s）
FAST_CHUNK = 16 << 10       # 16 KB 一发，逼近 TLS 记录大小；回环别合成大块

# 驱动脚本全程**按事件推进**，不靠固定等待：
#   开窗 -> 等日志出现 `download http`（下载真的开始了）-> 点，点到日志出现
#   `ui: button btnCancel` 为止 -> 等日志出现 FINISH-OK（取消成功或下载跑完
#   都会有）-> 关窗。所以点击时机跟下载快慢无关，慢机器上也不会点空。
PS_DRIVER = r'''
param([string]$ExePath, [string]$WorkDir, [string]$ManifestUrl, [int]$UiMode,
      [string]$LogPath, [string]$GoFlag)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$w = Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class W32Cancel {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
  [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  public static int[] Rect(IntPtr h) { RECT r; GetWindowRect(h, out r); return new int[]{r.L,r.T,r.R,r.B}; }
}
"@ -PassThru | Where-Object { $_.Name -eq 'W32Cancel' }
# ★ -PassThru 连嵌套的 RECT 一起回，是个数组；不挑出来 $w:: 调不着方法。

function Log-Has([string]$pat) {
    if (-not (Test-Path $LogPath)) { return $false }
    return [bool](Select-String -Path $LogPath -Pattern $pat -Quiet -SimpleMatch)
}
function Wait-Log([string]$pat, [int]$sec) {
    $d = (Get-Date).AddSeconds($sec)
    while ((Get-Date) -lt $d) {
        if (Log-Has $pat) { return $true }
        Start-Sleep -Milliseconds 100
    }
    return $false
}

$exe = Start-Process -FilePath $ExePath `
    -ArgumentList @('--manifest-url', $ManifestUrl, '--ui-mode', "$UiMode") `
    -WorkingDirectory $WorkDir -PassThru
$deadline = (Get-Date).AddSeconds(25)
while (-not $exe.MainWindowHandle -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 200; $exe.Refresh() }
if (-not $exe.MainWindowHandle) { Write-Output 'NO-WINDOW'; exit 1 }
if (-not (Wait-Log 'download http' 60)) {
    Write-Output 'NO-DOWNLOAD'; $exe.Kill(); exit 1 }
# 等服务器说「流够了」（见 py 里的 GO_FLAG）：下到一半才点，队列才堆得起来。
$goDeadline = (Get-Date).AddSeconds(180)
while (-not (Test-Path $GoFlag) -and (Get-Date) -lt $goDeadline) {
    Start-Sleep -Milliseconds 100 }
if (-not (Test-Path $GoFlag)) { Write-Output 'NO-GO'; $exe.Kill(); exit 1 }
Write-Output ("GO-AT {0}" -f (Get-Date -Format 'HH:mm:ss.fff'))

$h = $exe.MainWindowHandle
[void]$w::SetForegroundWindow($h)
$btn = $null
if ($UiMode -ne 1) {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($h)
    $name = [string][char]0x53D6 + [char]0x6D88
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, $name)
    $btn = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
    if (-not $btn) { Write-Output 'NO-BUTTON'; $exe.Kill(); exit 1 }
}
# 点到「更新器真的收到了」为止：第一下常常只是激活窗口；界面线程被饿死
# 时点了也不算数 —— 那正是要暴露的回归，所以只管点，判据交给日志。
$clickDeadline = (Get-Date).AddSeconds(45)
$hit = $false
$rounds = 0
while (-not $hit -and (Get-Date) -lt $clickDeadline) {
    $rounds++
    if ($UiMode -eq 1) {
        # IE 的 DOM 不投影进 UIA —— 按坐标点。取消按钮在窗口坐标
        # x 504~559 / y 477~495（2026-09-08 截图实量），取中心。
        $r = $w::Rect($h)
        $cx = $r[0] + 531
        $cy = $r[1] + 486
    } else {
        $pt = $btn.GetClickablePoint()
        $cx = [int]$pt.X
        $cy = [int]$pt.Y
    }
    [void]$w::SetCursorPos($cx, $cy)
    Start-Sleep -Milliseconds 150
    if ($rounds -eq 1) {
        Write-Output ("CLICK-AT {0} ({1},{2})" -f (Get-Date -Format 'HH:mm:ss.fff'), $cx, $cy)
    }
    [void]$w::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
    [void]$w::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 300
    $hit = Log-Has 'ui: button btnCancel'
}
if ($hit) {
    Write-Output ("CLICKED {0} rounds, seen at {1}" -f $rounds, (Get-Date -Format 'HH:mm:ss.fff'))
} else {
    Write-Output ("NOT-CLICKED after {0} rounds" -f $rounds)
}
[void](Wait-Log 'FINISH-OK' 120)
$exe.CloseMainWindow() | Out-Null
Start-Sleep -Seconds 2
if (-not $exe.HasExited) { $exe.Kill() }
Write-Output 'DONE'
'''

def die(msg):
    print("!! CANCEL-UI FAIL:", msg)
    sys.exit(1)


# 「什么时候点取消」的信号（★ 关键，2026-09-08）：不能一看到「下载开始」
# 就点 —— 那会儿快源的 post 队列还没堆起来，界面还没被饿死，坏版本也能过。
# 判据得是「下载真的流了一大截」：服务器自己数发出去多少字节，够了就把
# GO_FLAG 这个文件放出来，驱动脚本等到它才点。快机慢机都在「下到一半」点。
GO_FLAG = [None, 0, False]         # [flag 路径, 触发字节数, 已放出]


def arm_go(path, after_bytes):
    GO_FLAG[0], GO_FLAG[1], GO_FLAG[2] = path, after_bytes, False


def raise_go(sent):
    if GO_FLAG[0] and not GO_FLAG[2] and sent >= GO_FLAG[1]:
        GO_FLAG[2] = True
        with open(GO_FLAG[0], "w") as f:
            f.write("go\n")


class SlowHandler(http.server.BaseHTTPRequestHandler):
    def _manifest(self, ver, path, size, sha):
        body = json.dumps({
            "format": 1, "repo": "test/local",
            "releases": [{
                "version": ver, "date": "2026-08-23",
                "url": "http://127.0.0.1:%d/%s" % (PORT, path),
                "size": size, "sha256": sha,
            }]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, size, chunk_size, rate):
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        chunk = b"A" * chunk_size
        sent = 0
        t0 = time.perf_counter()
        try:
            while sent < size:
                self.wfile.write(chunk)
                sent += chunk_size
                raise_go(sent)
                # 按累计字节定节拍：sleep 的毫秒粒度撑不住快源，短等待自旋。
                target = t0 + sent / float(rate)
                while True:
                    d = target - time.perf_counter()
                    if d <= 0:
                        break
                    if d > 0.003:
                        time.sleep(d - 0.002)
        except Exception:
            pass                      # 取消把连接掐了是正常结局

    def do_GET(self):
        if self.path == "/manifest.json":
            self._manifest("9.9.9", "update.zip", ZIP_SIZE, SHA)
        elif self.path == "/manifest-stall.json":
            self._manifest("9.9.8", "stall.zip", ZIP_SIZE, SHA)
        elif self.path == "/manifest-fast.json":
            self._manifest("9.9.7", "fast.zip", FAST_ZIP_SIZE, fast_sha())
        elif self.path == "/update.zip":
            self._stream(ZIP_SIZE, 65536, THROTTLE)
        elif self.path == "/fast.zip":
            self._stream(FAST_ZIP_SIZE, FAST_CHUNK, FAST_RATE)
        elif self.path == "/stall.zip":
            # 断流场景：报 8MB 只发 64KB 就挂住不关连接 —— 复刻「GitHub
            # 慢/断流」下 WinHttpQueryDataAvailable 长时间等不到数据的处境。
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(ZIP_SIZE))
            self.end_headers()
            self.wfile.write(b"A" * 65536)
            raise_go(1 << 62)         # 断流档：发完那 64KB 就可以点了
            time.sleep(120)
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


SHA = hashlib.sha256(b"A" * (ZIP_SIZE // 65536 * 65536)).hexdigest()
# 8MB 正好整除 64KB，无需补尾

_FAST_SHA = []


def fast_sha():
    """400 MB 的哈希算一次就够；不跑 fast 档就不算。"""
    if not _FAST_SHA:
        h = hashlib.sha256()
        blk = b"A" * (1 << 20)
        for _ in range(FAST_ZIP_SIZE // len(blk)):
            h.update(blk)
        _FAST_SHA.append(h.hexdigest())
    return _FAST_SHA[0]


def build_sandbox(tmp, name):
    """一场景一个沙箱：日志不能串场，串了上一轮的 zip ready 会误伤。"""
    sandbox = os.path.join(tmp, "sandbox-%s" % name)
    os.makedirs(os.path.join(sandbox, "game_patched"))
    os.makedirs(os.path.join(sandbox, "config"))
    with open(os.path.join(sandbox, "BUILD.ver"), "w",
              encoding="utf-8") as f:
        f.write('{"version": "V0.0.1"}\n')
    with open(os.path.join(sandbox, "config", "server.config"), "w",
              encoding="utf-8") as f:
        f.write("server_address = 127.0.0.1\n")
    shutil.copyfile(EXE, os.path.join(sandbox, "game_patched",
                                      "BsPatcherChn.exe"))
    return sandbox


SCENES = {              # 场景 -> (目标版本, manifest 后缀, 流到多少字节才点)
    "slow":  ("9.9.9", "", ZIP_SIZE // 2),
    "stall": ("9.9.8", "-stall", 0),          # 断流档由 /stall.zip 自己放行
    "fast":  ("9.9.7", "-fast", FAST_ZIP_SIZE // 2),
}


def run_case(mode, tmp, scene="slow"):
    label = "mode=%d(%s)" % (mode, scene)
    ver, suffix, go_after = SCENES[scene]
    sandbox = build_sandbox(tmp, "%s-%d" % (scene, mode))
    go_flag = os.path.join(tmp, "go-%s-%d.flag" % (scene, mode))
    if os.path.exists(go_flag):
        os.remove(go_flag)
    arm_go(go_flag, go_after)
    # 每个场景用不同目标版本 → 不同缓存文件名；先清掉，别让上一轮的
    # 半截包把「下载中取消」变成「缓存复用」。
    cache = os.path.join(os.environ.get("TEMP", "."),
                         "popshot-update-%s.zip" % ver)
    if os.path.exists(cache):
        os.remove(cache)
    log = os.path.join(sandbox, "logs", "updater.log")
    ps = os.path.join(tmp, "driver-%s-%d.ps1" % (scene, mode))
    crlf = PS_DRIVER.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(ps, "wb") as f:
        f.write(b"\xef\xbb\xbf" + crlf.encode("utf-8"))
    url = "http://127.0.0.1:%d/manifest%s.json" % (PORT, suffix)
    t0 = time.time()
    out = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", ps,
         "-ExePath", os.path.join(sandbox, "game_patched",
                                  "BsPatcherChn.exe"),
         "-WorkDir", os.path.join(sandbox, "game_patched"),
         "-ManifestUrl", url, "-UiMode", str(mode), "-LogPath", log,
         "-GoFlag", go_flag],
        capture_output=True, timeout=300)
    stdout = out.stdout.decode("gbk", "replace")
    stderr = out.stderr.decode("gbk", "replace")
    print("%s driver: %s / %r (%.0fs)" %
          (label, stdout.strip().replace("\n", " | "), stderr,
           time.time() - t0))
    if not os.path.exists(log):
        die("%s 更新器没写日志" % label)
    text = open(log, encoding="utf-8", errors="replace").read()
    print("---- %s updater.log ----\n%s---------------------------" %
          (label, text))
    if "download http" not in text:
        die("%s 根本没进下载阶段" % label)
    # ★ 先判「点着了没有」：收尾的 CloseMainWindow 自己也会触发取消，
    #   只看「已取消更新」的话，点空了也能假过（2026-09-08 踩过）。
    if "ui: button btnCancel" not in text:
        die("%s 取消按钮压根没被按到 —— 要么坐标不对，要么界面线程被饿死"
            % label)
    if "zip ready" in text:
        die("%s 取消没生效：按钮收到了，下载还是一路跑完了（zip ready）"
            % label)
    if "FAIL" in text:
        die("%s 取消变成了失败：%s" %
            (label, text[text.index("FAIL"):][:120]))
    if not (("已取消更新" in text) and ("FINISH-OK" in text)):
        die("%s 日志里没有取消成功的痕迹（断流中取消超时？）" % label)
    print("%s CANCEL OK（按钮收到 + 下载中止 + 已取消提示）" % label)


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if not os.path.exists(EXE):
        die("找不到 %s" % EXE)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT),
                                            SlowHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="popshot-cancel-")
    print("sandbox:", tmp)
    try:
        if which in ("3", "both", "all"):
            run_case(3, tmp)
        if which in ("1", "both", "all"):
            run_case(1, tmp)
        if which in ("stall", "all"):
            run_case(3, tmp, scene="stall")
        if which in ("fast", "all"):
            run_case(1, tmp, scene="fast")
    finally:
        httpd.shutdown()
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)
    print("==== CANCEL-UI PASS ====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
