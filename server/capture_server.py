#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capture_server.py —— 阶段3/4 抓包用的假服务端骨架

在 127.0.0.1 上监听若干端口，accept 后把客户端发来的原始字节 hexdump 到屏幕 + 落盘
logs/conn_<seq>_<port>.bin（原始）与 .txt（带时间戳 hexdump）。**只收不发**（先抓首包）。

用法：
    python server/capture_server.py                # 默认监听 47611
    python server/capture_server.py 47611 40001 ... # 指定多个端口

配合 bshook.dll 的 connect 重定向（目标改写成 127.0.0.1，端口不变）使用。
"""
import sys, socket, threading, time, os, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(ROOT, "logs")
os.makedirs(LOGDIR, exist_ok=True)

_seq = 0
_seq_lock = threading.Lock()

def next_seq():
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq

def hexdump(b, base=0):
    out = []
    for i in range(0, len(b), 16):
        chunk = b[i:i+16]
        hexs = " ".join(f"{x:02x}" for x in chunk)
        hexs = f"{hexs:<47}"
        asc = "".join(chr(x) if 0x20 <= x < 0x7f else "." for x in chunk)
        out.append(f"  {base+i:04x}  {hexs}  |{asc}|")
    return "\n".join(out)

def handle(conn, addr, port):
    seq = next_seq()
    peer = f"{addr[0]}:{addr[1]}"
    stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    binpath = os.path.join(LOGDIR, f"conn_{seq:03d}_{port}.bin")
    txtpath = os.path.join(LOGDIR, f"conn_{seq:03d}_{port}.txt")
    print(f"[{stamp}] +++ conn#{seq} 端口{port} <- {peer}")
    total = 0
    with open(binpath, "wb") as fb, open(txtpath, "w", encoding="utf-8") as ft:
        ft.write(f"# conn#{seq} port={port} peer={peer} at {stamp}\n")
        ft.flush()
        conn.settimeout(30)
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                fb.write(data); fb.flush()
                t = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                dump = hexdump(data, total)
                ft.write(f"\n[{t}] recv {len(data)} bytes @off {total}:\n{dump}\n"); ft.flush()
                print(f"[{t}] conn#{seq} recv {len(data)} bytes:\n{dump}")
                total += len(data)
        except socket.timeout:
            print(f"conn#{seq} 30s 无数据，关闭")
        except Exception as e:
            print(f"conn#{seq} 异常: {e}")
    print(f"[{datetime.datetime.now():%H:%M:%S}] --- conn#{seq} 关闭，共 {total} 字节 -> {binpath}")
    try: conn.close()
    except Exception: pass

def serve(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError as e:
        print(f"!! 端口 {port} 绑定失败: {e}")
        return
    s.listen(8)
    print(f"[capture] 监听 127.0.0.1:{port}")
    while True:
        conn, addr = s.accept()
        threading.Thread(target=handle, args=(conn, addr, port), daemon=True).start()

def main():
    ports = [int(x) for x in sys.argv[1:]] or [47611]
    print(f"[capture] 端口: {ports}  日志目录: {LOGDIR}")
    for p in ports:
        threading.Thread(target=serve, args=(p,), daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("bye")

if __name__ == "__main__":
    main()
