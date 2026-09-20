#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""adminapi.py —— 管理页（27810 `/admin`）的脚本客户端：批量改 18 把武器的参数。

    python adminapi.py --show 1920001      # 看一把武器现在的设定 + 参考值
    python adminapi.py --apply A           # 把 SET-A 整套推上去
    python adminapi.py --apply EMPTY       # 全部清空（回到「都用参考值」）

## 走的是真接口，不是直接改文件

`POST /admin/api/weapon` 会走 `weaponcfg.save_item()` 的**全部校验**
（范围 `strict=True`、跨格 `homing_error`、只收 `custom:true` 的 id），
写盘是原子的，`serial` 每次 +1，写完还会 `broadcast_hook_weapon_table()`
把新表广播给在线客户端（模式 `0xFF`，**只换表不写内存**）。

⇒ 直接改 `server/data/weapons.json` 能省几行代码，但会**绕过校验**，
测出来的「通过」就不代表运营真这么点也通得过。这里坚持走 HTTP。

## 三条一定要守的规矩

1. **一次请求必须把 `pve` 和 `pvp` 都给全** —— `save_item()` 是
   `{mode: params.get(mode) for mode in MODES}`，只给一套会把另一套**清空**。
2. **不带 `desc` 键** ⇒ 用户自己写的说明文一个字不动（带 `""` 会把它删掉）。
3. `homing_angle > 0` 就必须有 `homing_range > 0`，否则这一条 400。

## 鉴权

会话 cookie `popshot_admin`，没有 CSRF token、不要求自定义头 ⇒ `urllib` +
`HTTPCookieProcessor` 就够。失败限速只记**失败**的登录，业务接口不限速。
"""
import argparse
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if os.path.join(ROOT, "server") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "server"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

import params as paramsets  # noqa: E402
import weaponcfg  # noqa: E402

DEFAULT_PORT = 27810
DEFAULT_USER = "testuser1"
DEFAULT_PASSWORD = "123"


def config_port(default=DEFAULT_PORT):
    """从 `config/server.config` 读注册页 / 管理页端口，读不到就用默认值。"""
    path = os.path.join(ROOT, "config", "server.config")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            text = fp.read()
    except OSError:
        return default
    m = re.search(r"^\s*local_register_port\s*=\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else default


class AdminError(RuntimeError):
    """管理页回了 4xx/5xx，或回执里 `ok` 不为真。"""


class AdminClient(object):
    def __init__(self, port=None, user=DEFAULT_USER, password=DEFAULT_PASSWORD,
                 host="127.0.0.1"):
        self.base = "http://%s:%d" % (host, port or config_port())
        self.user = user
        self.password = password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    # ---- 底层 ------------------------------------------------------------

    def _call(self, path, payload=None, method=None):
        url = self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
        try:
            with self.opener.open(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            try:
                info = json.loads(body)
            except ValueError:
                info = {"message": body[:400]}
            raise AdminError("%s %s -> HTTP %d：%s"
                             % (method or "POST", path, err.code,
                                info.get("message", body[:200]))) from None
        except urllib.error.URLError as err:
            raise AdminError("连不上管理页 %s：%s —— 服务端在跑吗？" % (url, err)) from None
        try:
            info = json.loads(body)
        except ValueError:
            raise AdminError("%s 回的不是 JSON：%s" % (path, body[:200])) from None
        if not info.get("ok", False):
            raise AdminError("%s 回执 ok=false：%s" % (path, info.get("message", "")))
        return info

    # ---- 业务 ------------------------------------------------------------

    def login(self):
        self._call("/admin/api/login", {"name": self.user, "password": self.password})
        return self

    def get_weapon(self, item_id):
        return self._call("/admin/api/weapon?id=%d" % int(item_id), method="GET")

    def save_weapon(self, item_id, block):
        """`block` = {"pve": {...}, "pvp": {...}}；值为 `None` 表示「这一格留空」。

        ★ 不带 `desc` 键 —— 说明文不归这套测试管。
        """
        payload = {"id": int(item_id),
                   "params": {mode: dict(block.get(mode) or {})
                              for mode in paramsets.MODES}}
        return self._call("/admin/api/weapon", payload)

    def apply_table(self, table, report=None):
        """把一整套参数推上去。返回 {物品id: 回执}；任何一条失败就抛。"""
        out = {}
        for item_id in sorted(table):
            info = self.save_weapon(item_id, table[item_id])
            out[item_id] = info
            if report:
                report("  %s 已保存（serial=%s，推给 %s 条在线连接）"
                       % (item_id, info.get("serial"), info.get("pushed")))
        return out

    def serial(self):
        """当前 `weapons.json` 的 serial —— 用来判「这一发是不是本轮的」。"""
        any_id = paramsets.custom_ids()[0]
        return int(self.get_weapon(any_id).get("serial", 0))


def main(argv=None):
    ap = argparse.ArgumentParser(description="批量改自定义武器参数")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("--show", type=int, metavar="物品id")
    ap.add_argument("--apply", metavar="集名", help="EMPTY / A / B / C / D")
    args = ap.parse_args(argv)

    client = AdminClient(args.port, args.user, args.password).login()
    if args.show:
        view = client.get_weapon(args.show)
        print("== %s %s（custom=%s serial=%s）==" %
              (view["id"], view.get("name"), view.get("custom"), view.get("serial")))
        print("%-14s %-10s %-10s %-10s" % ("字段", "参考", "PVE", "PVP"))
        for f in view["fields"]:
            print("%-14s %-10s %-10s %-10s"
                  % (f["key"], f.get("reference"), f.get("pve"), f.get("pvp")))
        return 0
    if args.apply:
        table = paramsets.build(args.apply.upper())
        print("推 SET-%s：%d 把武器" % (args.apply.upper(), len(table)))
        client.apply_table(table, report=print)
        print("完成，serial =", client.serial())
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
