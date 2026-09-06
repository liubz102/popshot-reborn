#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理页 `/admin`（V0.3商店 M8）。全部走真的 HTTP。

★ 这一组里最重要的是 `test_every_api_needs_a_login` —— 漏挂一个
`_require_admin()` 就等于把那个接口开在公网上。
"""
import http.cookiejar
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import account_store                                           # noqa: E402
import cfgmerge                                                  # noqa: E402
import shopcfg                                                 # noqa: E402
import shopdata                                                # noqa: E402
from account_store import AccountStore                         # noqa: E402
from web import admin as web_admin                             # noqa: E402
from web import server as web_server                           # noqa: E402


#: 三份默认配置**只生成一次**，之后每个用例复制一份。
#:
#: ★ 不是过早优化：`ensure_files()` 要把 1870 件物品过一遍算出 141 条商品 +
#:   35 条配方，一次约半秒；30 多个用例各生成一次就是 17 秒，
#:   直接把全量测试拖慢三成。
_TEMPLATE = None


def _template_dir():
    global _TEMPLATE
    if _TEMPLATE is None:
        _TEMPLATE = tempfile.TemporaryDirectory()
        shopcfg.ensure_files(_TEMPLATE.name)
    return _TEMPLATE.name


class _AdminCase(unittest.TestCase):
    """一台真的 HTTP 服务器 + 一份临时存档 + 一份临时 `server/data/`。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.accounts = AccountStore(os.path.join(self.tmp.name, "accounts.json"))
        # 默认管理员由启动时的幂等补齐建出来（和真实开服同一条路）。
        self.accounts.ensure_item_fields()

        # ★ 配置接口走的是无参的 `shopcfg.path_of()` ⇒ 改模块级的 DATA_DIR。
        self.data_dir = os.path.join(self.tmp.name, "data")
        os.makedirs(self.data_dir)
        saved_dir = shopcfg.DATA_DIR
        shopcfg.DATA_DIR = self.data_dir
        self.addCleanup(shopcfg.invalidate)
        self.addCleanup(setattr, shopcfg, "DATA_DIR", saved_dir)
        shopcfg.invalidate()
        for filename in web_admin.CONFIG_FILES.values():
            shutil.copyfile(os.path.join(_template_dir(), filename),
                            os.path.join(self.data_dir, filename))

        self.httpd = web_server.make_server(0, self.accounts, "127.0.0.1",
                                            cooldown=0)
        self.port = self.httpd.server_address[1]
        # ★ 每个用例一台新服务器（会话表 / 限速表 / 配置都要是干净的），
        #   所以 `shutdown()` 会被调 30 多次。`serve_forever` 默认 **0.5 秒**
        #   轮询一次退出标志，照默认值走的话光关服务器就要 18 秒。
        thread = threading.Thread(
            target=lambda: self.httpd.serve_forever(poll_interval=0.02),
            daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

        # 每个用例一个独立的 cookie 罐 = 一个独立的浏览器。
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    # ---------------------------------------------------------------- HTTP
    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, path, payload=None, opener=None):
        """返回 `(状态码, 解出来的 JSON 或原始文本)`。"""
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url(path), data=data, headers=headers)
        try:
            with (opener or self.opener).open(req, timeout=10) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, body = error.code, error.read()
        text = body.decode("utf-8")
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            return status, text

    def login(self, name=None, password=None):
        return self.request("/admin/api/login", {
            "name": name or account_store.DEFAULT_ADMIN_NAME,
            "password": (password if password is not None
                         else account_store.DEFAULT_ADMIN_PASSWORD)})


class AdminAuthTests(_AdminCase):

    def test_the_page_renders_with_the_placeholders_filled_in(self):
        status, html = self.request("/admin")
        self.assertEqual(200, status)
        # 占位符必须被换掉，不能原样漏到页面上。
        self.assertNotIn("__USERNAME_RULE__", html)
        self.assertNotIn("__PASSWORD_RULE__", html)
        self.assertIn(account_store.USERNAME_RULE_TEXT, html)

    def test_the_default_password_warning_is_not_on_the_page_any_more(self):
        # ★ D24：用户 2026-09-05 要求页面上不显示这条，挪进了启动日志。
        #   这条用例是防回归的 —— 别有人「顺手」把红字加回来。
        _status, html = self.request("/admin")
        self.assertNotIn("出厂口令", html)
        self.assertNotIn("DEFAULT_PASSWORD_IN_USE", html)

    def test_the_default_password_check_follows_the_actual_password(self):
        # D3 的补偿之二现在长在这个函数上（`app.py` 启动时打一行警告）。
        self.assertTrue(
            web_admin.default_admin_password_in_use(self.accounts))
        self.accounts.admin_set_password(account_store.DEFAULT_ADMIN_NAME,
                                         "SomethingElse1")
        self.assertFalse(
            web_admin.default_admin_password_in_use(self.accounts))

    def test_login_succeeds_and_sets_an_http_only_cookie(self):
        status, result = self.login()
        self.assertEqual(200, status)
        self.assertTrue(result["ok"], result)
        cookies = [c for c in self.jar if c.name == web_admin.SESSION_COOKIE]
        self.assertEqual(1, len(cookies))
        # HttpOnly / SameSite 都不是 cookielib 的一等字段，从原始属性里翻。
        self.assertIn("HttpOnly", cookies[0]._rest)
        self.assertEqual("/admin", cookies[0].path)
        # ★ **有 `expires` 才是持久 cookie** —— 会话 cookie（`expires is None`）
        #   一关标签页就没了，用户「一小时内重新打开页面该跳过登录页」
        #   就落空了（D29）。
        self.assertIsNotNone(cookies[0].expires)
        self.assertFalse(cookies[0].discard)

    def test_a_wrong_password_does_not_log_you_in(self):
        _status, result = self.login(password="nope")
        self.assertFalse(result["ok"])
        _status, session = self.request("/admin/api/session")
        self.assertFalse(session["logged_in"])

    def test_every_api_needs_a_login(self):
        # ★★ 漏挂一个 `_require_admin()` 就等于把那个接口开在公网上。
        for path, payload in (
                ("/admin/api/config/items", None),
                ("/admin/api/config/shop", None),
                ("/admin/api/config/recipe", None),
                ("/admin/api/config/drops", None),
                ("/admin/api/admins", None),
                ("/admin/api/catalog", None),
                ("/admin/itemicons.png", None),
                ("/admin/api/item?id=1120041", None),
                ("/admin/api/players?q=a", None),
                ("/admin/api/player?name=alice", None),
                ("/admin/api/player", {"name": "alice", "money": 1}),
                ("/admin/api/config/shop", {"text": "{}"}),
                ("/admin/api/admins/add", {"name": "carol", "password": "pw1"}),
                ("/admin/api/admins/password", {"name": "admin", "password": "pw1"}),
                ("/admin/api/admins/role", {"name": "admin", "role": "operator"}),
                ("/admin/api/admins/remove", {"name": "admin"}),
        ):
            status, result = self.request(path, payload)
            self.assertEqual(401, status, path)
            self.assertFalse(result["ok"], path)

    def test_logout_really_ends_the_session(self):
        self.login()
        self.request("/admin/api/logout", {})
        _status, session = self.request("/admin/api/session")
        self.assertFalse(session["logged_in"])
        status, _result = self.request("/admin/api/admins")
        self.assertEqual(401, status)

    def test_a_malformed_cookie_does_not_blow_up(self):
        req = urllib.request.Request(self.url("/admin/api/session"),
                                     headers={"Cookie": "=====;;;"})
        with urllib.request.urlopen(req, timeout=10) as response:
            self.assertEqual(200, response.status)

    def test_an_unknown_admin_path_is_a_clean_404(self):
        for path, payload in (("/admin/api/nope", None),
                              ("/admin/api/nope", {})):
            status, result = self.request(path, payload)
            self.assertEqual(404, status)
            self.assertFalse(result["ok"])

    def test_the_register_page_still_works(self):
        # 管理页是**混进同一个 Handler** 的 —— 别把注册页碰坏了。
        status, html = self.request("/")
        self.assertEqual(200, status)
        self.assertIn("存档转移助手", html)


class AdminLoginRateLimitTests(_AdminCase):
    """D3 的补偿之一：明文弱口令 + 公网可达 ⇒ 登录必须限速。"""

    def setUp(self):
        super().setUp()
        # 服务器是 `make_server` 建的，限速器在类属性上；换一个能控时钟的。
        self.now = 1000.0
        handler = self.httpd.RequestHandlerClass
        handler.admin_limiter = web_admin.LoginRateLimiter(
            cooldown=5, clock=lambda: self.now)

    def test_a_failure_locks_the_ip_for_a_while(self):
        self.login(password="nope")
        status, result = self.login()          # 这次口令是对的
        self.assertEqual(429, status)
        self.assertIn("登录太频繁", result["message"])
        # ★ 被限住时连「有没有这个管理员」都问不出来 —— 否则限速本身
        #   就成了一个免费的枚举接口。
        self.assertNotIn("尚未注册", result["message"])

    def test_the_lock_expires(self):
        self.login(password="nope")
        self.now += 6
        _status, result = self.login()
        self.assertTrue(result["ok"], result)

    def test_a_success_clears_the_penalty(self):
        # 「输错一次、马上输对」的人不该在下一次登录时还要再等一轮。
        self.login(password="nope")
        self.now += 6
        self.login()
        self.assertEqual(0, self.httpd.RequestHandlerClass
                         .admin_limiter.retry_after("127.0.0.1"))

    def test_zero_cooldown_turns_it_off(self):
        self.httpd.RequestHandlerClass.admin_limiter = \
            web_admin.LoginRateLimiter(cooldown=0)
        self.login(password="nope")
        _status, result = self.login()
        self.assertTrue(result["ok"], result)


class AdminConfigTests(_AdminCase):

    def setUp(self):
        super().setUp()
        self.assertTrue(self.login()[1]["ok"])

    def get(self, which):
        return self.request("/admin/api/config/" + which)[1]

    def save(self, which, text):
        return self.request("/admin/api/config/" + which, {"text": text})[1]

    def test_reading_gives_back_the_file_on_disk(self):
        for which, filename in web_admin.CONFIG_FILES.items():
            result = self.get(which)
            self.assertTrue(result["ok"], result)
            self.assertEqual([], result["warnings"])
            self.assertTrue(result["path"].endswith(filename))
            with open(shopcfg.path_of(filename), "r", encoding="utf-8") as fp:
                self.assertEqual(fp.read(), result["text"])

    def test_an_unknown_config_name_is_a_404(self):
        status, result = self.request("/admin/api/config/nope")
        self.assertEqual(404, status)
        self.assertFalse(result["ok"])

    def test_saving_takes_effect_without_a_restart(self):
        raw = json.loads(self.get("shop")["text"])
        raw["items"][0]["price"] = 12345
        item_id = raw["items"][0]["id"]
        result = self.save("shop", json.dumps(raw, ensure_ascii=False))
        self.assertTrue(result["ok"], result)
        self.assertIn("即刻生效", result["message"])
        # ★ 热重载：不重启服务端，下一次读就是新值。
        table, warnings = shopcfg.shop()
        self.assertEqual([], warnings)
        self.assertEqual(12345, table[item_id]["price"])

    def test_broken_json_is_refused_and_the_file_is_untouched(self):
        before = self.get("shop")["text"]
        result = self.save("shop", "{ oops not json")
        self.assertFalse(result["ok"])
        self.assertIn("不是合法的 JSON", result["message"])
        self.assertIn("行", result["message"])       # 行号要报出来
        self.assertEqual(before, self.get("shop")["text"])

    def test_a_schema_error_is_refused_and_the_file_is_untouched(self):
        # ★★ 存一份坏文件下去，服务端会退回上一份好的继续跑（D10）——
        #    用户会以为改生效了，实际没有。所以**校验不过就不落盘**。
        before = self.get("recipe")["text"]
        raw = json.loads(before)
        raw["recipes"][0]["materials"] = [
            {"id": 10001, "count": 1}, {"id": 10002, "count": 1},
            {"id": 10003, "count": 1}, {"id": 10004, "count": 1},
            {"id": 20007, "count": 1}]              # 5 种 > UI 的 4 个槽
        result = self.save("recipe", json.dumps(raw, ensure_ascii=False))
        self.assertFalse(result["ok"])
        self.assertIn("校验没过，没有保存", result["message"])
        self.assertEqual(before, self.get("recipe")["text"])

    def test_an_empty_body_is_refused(self):
        before = self.get("drops")["text"]
        for text in ("", "   ", None):
            result = self.save("drops", text)
            self.assertFalse(result["ok"], text)
        self.assertEqual(before, self.get("drops")["text"])

    def test_the_guidance_notes_are_not_written_back(self):
        # ★ D16：`_说明` 那几句话是写给**手改 json 的人**看的，而现在唯一的
        #   编辑入口是管理页，说明已经画在面板上了（`SCHEMA[...]["help"]`）。
        #   ⇒ 保存只写 `format` + 那一个列表，老文件里的 `_说明` 就此消失。
        raw = json.loads(self.get("drops")["text"])
        raw["_说明"] = ["旧文件里留下来的说明"]
        raw["_随便什么"] = 1
        self.assertTrue(self.save("drops", json.dumps(raw, ensure_ascii=False))["ok"])
        saved = json.loads(self.get("drops")["text"])
        self.assertEqual(["format", "rules"], sorted(saved))
        # 规则本身一条不少 —— 去掉的只有注释键。
        self.assertEqual(len(raw["rules"]), len(saved["rules"]))

    def test_a_field_the_validator_ignores_still_survives_a_save(self):
        # 管理页对「字段表还没登记」的键退回通用输入框，模型就是原对象
        # ⇒ 它必须能原样存回去。这里从服务端这一侧钉住：**列表元素里的
        # 未知键不会被吃掉**（被吃掉的只有最外层 `_` 开头的注释键）。
        raw = json.loads(self.get("drops")["text"])
        raw["rules"][0]["以后新增的字段"] = "保留我"
        self.assertTrue(self.save("drops", json.dumps(raw, ensure_ascii=False))["ok"])
        saved = json.loads(self.get("drops")["text"])
        self.assertEqual("保留我", saved["rules"][0]["以后新增的字段"])

    def test_a_broken_file_on_disk_is_reported_when_reading(self):
        # 文件坏了时 `shopcfg` 保留上一份好的（D10）—— 页面必须说出来，
        # 否则人会以为自己看到的这份就是服务端在用的那份。
        with open(shopcfg.path_of(shopcfg.DROPS_FILENAME), "w",
                  encoding="utf-8") as fp:
            fp.write("{ broken")
        shopcfg.invalidate()
        result = self.get("drops")
        self.assertTrue(result["ok"])            # 文本照样给你看，好去修
        self.assertTrue(result["warnings"])


class AdminConfigConflictTests(_AdminCase):
    """两个管理员同时改同一份配置（D36）。

    ★ 这一组关心的**只有两件事**：撞车时到底有没有写盘、没撞车时对方的改动
    有没有跟着进来。合并算法本身在 `test_cfgmerge` 里逐条钉。
    """

    def setUp(self):
        super().setUp()
        self.assertTrue(self.login()[1]["ok"])
        # 第二个浏览器 = 第二个 cookie 罐。用同一个账号就行 —— 冲突检测
        # 认的是「哪一份 base」，不是「谁」。
        self.other = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.assertTrue(self.request("/admin/api/login", {
            "name": account_store.DEFAULT_ADMIN_NAME,
            "password": account_store.DEFAULT_ADMIN_PASSWORD},
            opener=self.other)[1]["ok"])

    # -------------------------------------------------------------- 小工具
    def open_page(self, which, opener=None):
        """一个浏览器打开这一页拿到的原文（这就是它的 `base`）。"""
        result = self.request("/admin/api/config/" + which, opener=opener)[1]
        self.assertTrue(result["ok"], result)
        return result["text"]

    def save(self, which, base, entries, opener=None, **extra):
        payload = {"text": json.dumps(
            {"format": shopcfg.FORMAT,
             shopcfg.SCHEMA[which]["list_key"]: entries}, ensure_ascii=False),
            "base": base}
        payload.update(extra)
        return self.request("/admin/api/config/" + which, payload,
                            opener=opener)[1]

    @staticmethod
    def rows(text, which="shop"):
        return json.loads(text)[shopcfg.SCHEMA[which]["list_key"]]

    def on_disk(self, which="shop"):
        with open(shopcfg.path_of(web_admin.CONFIG_FILES[which]),
                  "r", encoding="utf-8") as fp:
            return self.rows(fp.read(), which)

    # ------------------------------------------------------------ 自动合并
    def test_改了不同的物品直接合并而且不提示(self):
        base = self.open_page("shop")
        rows = self.rows(base)
        mine, theirs = [dict(r) for r in rows], [dict(r) for r in rows]
        mine[0]["price"] = 111
        theirs[1]["price"] = 222

        self.assertTrue(self.save("shop", base, theirs,
                                  opener=self.other)["ok"])
        result = self.save("shop", base, mine)
        self.assertTrue(result["ok"], result)
        self.assertNotIn("conflict", result)
        # ★ 回文里就是合并后的最新状态 —— 前台拿它当场刷新画面。
        merged = dict((r["id"], r["price"]) for r in self.rows(result["text"]))
        self.assertEqual(111, merged[rows[0]["id"]])
        self.assertEqual(222, merged[rows[1]["id"]], "对方那一条要跟着进来")
        self.assertEqual(1, len(result["adopted"]))

    def test_对方新加的条目我没动就跟着进来(self):
        base = self.open_page("shop")
        rows = self.rows(base)
        theirs = [dict(r) for r in rows] + [
            {"id": 1010064, "kind": "armor", "price": 50, "listed": False}]
        self.assertTrue(self.save("shop", base, theirs,
                                  opener=self.other)["ok"])
        mine = [dict(r) for r in rows]
        mine[0]["price"] = 111
        result = self.save("shop", base, mine)
        self.assertTrue(result["ok"], result)
        self.assertIn(1010064, [r["id"] for r in self.rows(result["text"])])

    # ---------------------------------------------------------------- 撞车
    def test_改了同一个物品就撞车而且一个字节都不写(self):
        base = self.open_page("shop")
        rows = self.rows(base)
        mine, theirs = [dict(r) for r in rows], [dict(r) for r in rows]
        mine[0]["price"] = 111
        mine[1]["price"] = 333          # 这一条没人碰，应该进「未冲突」
        theirs[0]["price"] = 222

        self.assertTrue(self.save("shop", base, theirs,
                                  opener=self.other)["ok"])
        before = self.on_disk()
        result = self.save("shop", base, mine)
        self.assertFalse(result["ok"])
        self.assertTrue(result["conflict"])
        self.assertEqual(before, self.on_disk(), "撞车时文件不该动")

        self.assertEqual(1, len(result["conflicts"]))
        self.assertEqual(1, len(result["mergeable"]))
        # 提示框里要写清楚是哪一件、为什么撞了。
        self.assertIn(str(rows[0]["id"]), result["conflicts"][0]["label"])
        self.assertTrue(result["conflicts"][0]["reason"])
        self.assertIn(str(rows[1]["id"]), result["mergeable"][0]["label"])

    def test_单独提交未冲突物品(self):
        base = self.open_page("shop")
        rows = self.rows(base)
        mine, theirs = [dict(r) for r in rows], [dict(r) for r in rows]
        mine[0]["price"] = 111
        mine[1]["price"] = 333
        theirs[0]["price"] = 222
        self.assertTrue(self.save("shop", base, theirs,
                                  opener=self.other)["ok"])
        clash = self.save("shop", base, mine)

        # 管理员点了「单独提交未冲突物品」—— 原样再发一次，加 only。
        again = self.save("shop", base, mine,
                          only=[row["key"] for row in clash["mergeable"]])
        self.assertTrue(again["ok"], again)
        prices = dict((r["id"], r["price"]) for r in self.rows(again["text"]))
        self.assertEqual(222, prices[rows[0]["id"]], "冲突那件要回到对方的值")
        self.assertEqual(333, prices[rows[1]["id"]], "未冲突那件才是我的")

    def test_单独提交时第三个人又插进来了还要再报一次(self):
        # ★ 用户 2026-09-06 明确要求：单独提交前重新检测一次。
        base = self.open_page("shop")
        rows = self.rows(base)
        mine = [dict(r) for r in rows]
        mine[1]["price"] = 333
        third = [dict(r) for r in rows]
        third[1]["price"] = 444
        self.assertTrue(self.save("shop", base, third,
                                  opener=self.other)["ok"])
        result = self.save("shop", base, mine,
                           only=[cfgmerge.key_text(((rows[1]["id"],), 0))])
        self.assertFalse(result["ok"])
        self.assertTrue(result["conflict"])

    # ------------------------------------------------ 商店 ⇄ 合成互斥的撞车
    def unlist_first_recipe(self):
        """默认配方全是上架的 —— 先下架一条，好当「谁把它重新上架」的起点。"""
        base = self.open_page("recipe")
        rows = [dict(r) for r in self.rows(base, "recipe")]
        rows[0]["listed"] = False
        self.assertTrue(self.save("recipe", base, rows)["ok"])
        return rows[0]["result"]

    def test_我在商店上架对方同时在合成上架也算撞车(self):
        product = self.unlist_first_recipe()
        shop_base = self.open_page("shop")
        recipe_base = self.open_page("recipe")       # 我看到的：它没上架

        # 对方刚把这件东西在合成里上架（我手上那份 recipe 还是旧的，看不见）。
        theirs = [dict(r) for r in self.rows(recipe_base, "recipe")]
        theirs[0]["listed"] = True
        self.assertTrue(self.save("recipe", recipe_base, theirs,
                                  opener=self.other)["ok"])

        # 我把同一件东西摆上商店货架。
        mine = [dict(r) for r in self.rows(shop_base)]
        mine.append({"id": product, "kind": "armor",
                     "price": 500, "listed": True})
        before = self.on_disk("shop")
        result = self.save("shop", shop_base, mine, cross_base=recipe_base)
        self.assertFalse(result["ok"], "对方刚在合成里上架，这该算撞车")
        self.assertTrue(result["conflict"])
        self.assertEqual(before, self.on_disk("shop"))
        self.assertIn("合成", result["conflicts"][0]["reason"])
        self.assertIn(str(product), result["conflicts"][0]["label"])

    def test_对方本来就在合成上架的走前台那道确认框不在这儿报(self):
        # 我手上那份 recipe 已经显示它上架了 ⇒ D33 的 `listingClash()`
        # 会先问一句、然后自动下架合成。服务端不该再拦一次。
        recipe_base = self.open_page("recipe")       # 默认全是上架的
        product = self.rows(recipe_base, "recipe")[0]["result"]
        shop_base = self.open_page("shop")
        mine = [dict(r) for r in self.rows(shop_base)]
        mine.append({"id": product, "kind": "armor",
                     "price": 500, "listed": True})
        result = self.save("shop", shop_base, mine, cross_base=recipe_base)
        self.assertTrue(result["ok"], result)

    def test_两种撞车一次说完(self):
        """★ 合并撞车 + 互斥撞车同时发生时，要在**同一个**清单里全列出来。
        分两轮的话运营点了「单独提交未冲突的」，第二轮才发现里面还有互斥的。
        """
        product = self.unlist_first_recipe()
        shop_base = self.open_page("shop")
        recipe_base = self.open_page("recipe")
        rows = self.rows(shop_base)

        # 对方干了两件事：改了商店里第 0 条、把 product 在合成里上架。
        theirs = [dict(r) for r in rows]
        theirs[0]["price"] = 222
        self.assertTrue(self.save("shop", shop_base, theirs,
                                  opener=self.other)["ok"])
        listed = [dict(r) for r in self.rows(recipe_base, "recipe")]
        listed[0]["listed"] = True
        self.assertTrue(self.save("recipe", recipe_base, listed,
                                  opener=self.other)["ok"])

        # 我也改了第 0 条（合并撞车）、把 product 摆上货架（互斥撞车）、
        # 顺手改了第 1 条（这条谁也没碰，该进「未冲突」）。
        mine = [dict(r) for r in rows]
        mine[0]["price"] = 111
        mine[1]["price"] = 333
        mine.append({"id": product, "kind": "armor",
                     "price": 500, "listed": True})
        result = self.save("shop", shop_base, mine, cross_base=recipe_base)
        self.assertFalse(result["ok"])
        reasons = sorted(row["reason"] for row in result["conflicts"])
        self.assertEqual(2, len(reasons), result["conflicts"])
        self.assertTrue(any("合成" in r for r in reasons), reasons)
        self.assertTrue(any("也改了" in r for r in reasons), reasons)
        # 未冲突清单里**只有**那一条，不该混进互斥撞车的。
        self.assertEqual(1, len(result["mergeable"]))
        self.assertIn(str(rows[1]["id"]), result["mergeable"][0]["label"])

    def test_对方在合成里上架的是别的东西不算撞车(self):
        first = self.unlist_first_recipe()
        shop_base = self.open_page("shop")
        recipe_base = self.open_page("recipe")
        theirs = [dict(r) for r in self.rows(recipe_base, "recipe")]
        theirs[0]["listed"] = True                   # 对方上架的是 first
        self.assertTrue(self.save("recipe", recipe_base, theirs,
                                  opener=self.other)["ok"])
        other_product = theirs[1]["result"]
        self.assertNotEqual(first, other_product)
        mine = [dict(r) for r in self.rows(shop_base)]
        mine.append({"id": other_product, "kind": "armor",
                     "price": 500, "listed": True})
        self.assertTrue(self.save("shop", shop_base, mine,
                                  cross_base=recipe_base)["ok"])

    # ------------------------------------------------------------ 退路
    def test_不带_base_就是老行为整份覆盖(self):
        # 脚本 / 老页面还能用；`test_saving_takes_effect_without_a_restart`
        # 那一组走的就是这条路。
        base = self.open_page("shop")
        rows = self.rows(base)
        theirs = [dict(r) for r in rows]
        theirs[0]["price"] = 222
        self.assertTrue(self.save("shop", base, theirs,
                                  opener=self.other)["ok"])
        mine = [dict(r) for r in rows]
        mine[0]["price"] = 111
        result = self.request("/admin/api/config/shop", {"text": json.dumps(
            {"format": shopcfg.FORMAT, "items": mine}, ensure_ascii=False)})[1]
        self.assertTrue(result["ok"], result)
        self.assertEqual(111, dict((r["id"], r["price"])
                                   for r in self.on_disk())[rows[0]["id"]])

    def test_坏掉的_base_退回整份覆盖而不是报错(self):
        base = self.open_page("shop")
        mine = [dict(r) for r in self.rows(base)]
        mine[0]["price"] = 111
        result = self.save("shop", "{ 这不是 json", mine)
        self.assertTrue(result["ok"], result)


class AdminAssetTests(_AdminCase):
    """样式 / 脚本 / 图标图集这三个静态件（D16 的新前台靠它们）。"""

    def fetch(self, path, headers=None, opener=None):
        """返回 `(状态码, 响应头, 原始字节)` —— 图集是二进制，不能按文本读。"""
        req = urllib.request.Request(self.url(path), headers=headers or {})
        try:
            with (opener or self.opener).open(req, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_the_stylesheet_and_script_need_no_login(self):
        # ★ `/admin` 本身（登录表单）就是未登录状态渲染的 —— 样式和脚本
        #   再要登录，登录页就成了一堆裸标签。它们里面没有秘密。
        for path, marker in (("/admin/admin.css", b"--amber"),
                             ("/admin/admin.js", b"admin.js")):
            status, headers, body = self.fetch(path)
            self.assertEqual(200, status, path)
            self.assertIn(marker, body, path)
            self.assertIn("charset=utf-8", headers["Content-Type"].lower())

    def test_the_page_pulls_in_the_stylesheet_and_the_script(self):
        # 打包漏了哪个文件，本地不一定看得出来 —— 这里钉住引用关系。
        _status, html = self.request("/admin")
        self.assertIn('href="/admin/admin.css"', html)
        self.assertIn('src="/admin/admin.js"', html)

    #: `admin.js` 自己 `el.id = "…"` 建出来的节点，html 里当然没有。
    #: 加控件时如果又多了一个，往这儿补一行，别把下面那条用例关掉。
    JS_MADE_IDS = {"cfgShown"}

    def test_every_id_the_script_looks_up_exists_in_the_page(self):
        """★ `$("拼错的id")` 返回 `null`，**浏览器不报错**，只是那个按钮
        再也不响应 —— 页面看上去好好的，功能悄悄没了。加控件时最容易
        只改一半（写了 `onclick` 忘了加 `<button>`，或者反过来）。
        """
        _status, _h, js = self.fetch("/admin/admin.js")
        _status, html = self.request("/admin")
        page_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
        wanted = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)',
                                js.decode("utf-8")))
        self.assertLessEqual(wanted - self.JS_MADE_IDS, page_ids)

    def test_the_atlas_comes_back_as_a_png_once_logged_in(self):
        self.login()
        status, headers, body = self.fetch("/admin/itemicons.png")
        self.assertEqual(200, status)
        self.assertEqual("image/png", headers["Content-Type"])
        self.assertEqual(b"\x89PNG\r\n\x1a\n", body[:8])

    def test_the_atlas_revalidates_instead_of_being_resent(self):
        # 0.62 MB 的图，每次刷新都重下太浪费；但 `max-age` 又会让「刚跑完
        # update-shopicons.bat，浏览器里还是旧图」出现一整天。⇒ ETag + no-cache。
        self.login()
        _status, headers, _body = self.fetch("/admin/itemicons.png")
        etag = headers["ETag"]
        self.assertIn("no-cache", headers["Cache-Control"])
        status, _h, body = self.fetch("/admin/itemicons.png",
                                      {"If-None-Match": etag})
        self.assertEqual(304, status)
        self.assertEqual(b"", body)

    def test_a_missing_asset_says_which_file_is_missing(self):
        # 打包漏一个文件时，云上 500 而本地好好的 —— 至少要说清是哪一个。
        path = os.path.join(web_admin.HERE, "itemicons.png")
        backup = path + ".bak"
        os.replace(path, backup)
        self.addCleanup(lambda: os.path.exists(backup) and os.replace(backup, path))
        self.login()
        status, result = self.request("/admin/itemicons.png")
        self.assertEqual(404, status)
        self.assertIn("itemicons.png", result["message"])


class AdminCatalogTests(_AdminCase):
    """`/admin/api/catalog` —— 物品表 + 字段描述表 + 图集元信息。"""

    def setUp(self):
        super().setUp()
        self.assertTrue(self.login()[1]["ok"])
        self.catalog = self.request("/admin/api/catalog")[1]
        self.assertTrue(self.catalog["ok"], self.catalog)

    def test_it_only_lists_items_that_can_go_into_a_backpack(self):
        # ★ 校验器只放行 `ownable` 的（§11）—— 选得到却存不进去的东西
        #   不该出现在选择器里。
        self.assertTrue(self.catalog["items"])
        for entry in self.catalog["items"]:
            self.assertTrue(shopdata.ownable(entry["id"]), entry)
        listed = set(e["id"] for e in self.catalog["items"])
        for item_id in shopdata.ids_of_kind("material"):
            if shopdata.ownable(item_id):
                self.assertIn(item_id, listed)

    def test_almost_everything_has_an_icon_cell(self):
        cells = [e for e in self.catalog["items"] if e.get("cell") is not None]
        # 原版素材本来就缺几个图标，缺的画问号占位；但绝大多数得有。
        self.assertGreater(len(cells), 0.95 * len(self.catalog["items"]))
        icons = self.catalog["icons"]
        self.assertEqual("/admin/itemicons.png", icons["url"])
        top = max(e["cell"] for e in cells)
        self.assertLess(top, (icons["width"] // icons["size"])
                        * (icons["height"] // icons["size"]))

    def test_items_carry_the_same_description_the_game_shows(self):
        # ★ D26：管理页的悬停浮窗画的就是这一段，而它和 `0x0501` 的
        #   `ItemInfo+0x18`（游戏内提示框下半那块）是同一个函数算的
        #   —— 两边看到的数字必须一致。
        _status, result = self.request("/admin/api/catalog")
        by_id = {item["id"]: item for item in result["items"]}
        for item_id in (1120011, 1010037):
            self.assertEqual(
                shopcfg.item_desc_zh(shopdata.get(item_id)),
                by_id[item_id]["desc"], item_id)
        # 说明是空的就不带这个键（800 件里 170 件没有，白占体积）。
        self.assertNotIn("desc", by_id[10001])

    def test_the_item_lookup_carries_the_description_too(self):
        _status, result = self.request("/admin/api/item?id=1120011")
        self.assertEqual(shopcfg.item_desc_zh(shopdata.get(1120011)),
                         result["desc"])

    def test_the_schema_covers_every_config_file(self):
        schema = self.catalog["schema"]
        self.assertEqual(sorted(web_admin.CONFIG_FILES), sorted(schema))
        for which, spec in schema.items():
            self.assertTrue(spec["fields"], which)
            self.assertTrue(spec["help"], which)
            self.assertTrue(spec["list_key"], which)


class AdminAccountApiTests(_AdminCase):

    def setUp(self):
        super().setUp()
        self.assertTrue(self.login()[1]["ok"])

    def names(self):
        return self.request("/admin/api/admins")[1]["names"]

    def test_add_and_remove(self):
        result = self.request("/admin/api/admins/add",
                              {"name": "carol", "password": "SecretPw"})[1]
        self.assertTrue(result["ok"], result)
        self.assertEqual(["admin", "carol"], self.names())
        result = self.request("/admin/api/admins/remove", {"name": "carol"})[1]
        self.assertTrue(result["ok"], result)
        self.assertEqual(["admin"], self.names())

    def test_removing_the_last_admin_is_refused_by_the_server(self):
        # ★ 拦在 `account_store` 层，不只拦前端 —— 前端拦得住鼠标，
        #   拦不住直接 POST（这条用例就是直接 POST 的）。
        result = self.request("/admin/api/admins/remove", {"name": "admin"})[1]
        self.assertFalse(result["ok"])
        self.assertIn("至少要保留一个系统管理员", result["message"])
        self.assertEqual(["admin"], self.names())

    def test_a_bad_name_or_password_is_refused_with_the_rule_text(self):
        result = self.request("/admin/api/admins/add",
                              {"name": "有中文", "password": "SecretPw"})[1]
        self.assertFalse(result["ok"])
        self.assertIn("用户名只能用", result["message"])
        result = self.request("/admin/api/admins/add",
                              {"name": "carol", "password": ""})[1]
        self.assertFalse(result["ok"])
        self.assertIn("密码长度", result["message"])

    def test_changing_my_own_password_logs_me_out(self):
        result = self.request("/admin/api/admins/password",
                              {"name": "admin", "password": "NewPass1"})[1]
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["logged_out"])
        # ★ 旧令牌必须当场作废：不然「我把密码改了」和「拿着旧密码登进来的
        #   人还在操作」会同时成立。
        self.assertEqual(401, self.request("/admin/api/admins")[0])
        self.assertTrue(self.login(password="NewPass1")[1]["ok"])

    def test_changing_someone_elses_password_kicks_only_them(self):
        self.request("/admin/api/admins/add",
                     {"name": "carol", "password": "SecretPw"})
        other = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request("/admin/api/login",
                     {"name": "carol", "password": "SecretPw"}, opener=other)
        self.assertEqual(200, self.request("/admin/api/admins",
                                           opener=other)[0])
        result = self.request("/admin/api/admins/password",
                              {"name": "carol", "password": "Another1"})[1]
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["logged_out"])       # 我自己还在
        self.assertEqual(200, self.request("/admin/api/admins")[0])
        self.assertEqual(401, self.request("/admin/api/admins",
                                           opener=other)[0])

    def test_removing_someone_kicks_their_session_too(self):
        self.request("/admin/api/admins/add",
                     {"name": "carol", "password": "SecretPw"})
        other = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request("/admin/api/login",
                     {"name": "carol", "password": "SecretPw"}, opener=other)
        self.request("/admin/api/admins/remove", {"name": "carol"})
        self.assertEqual(401, self.request("/admin/api/admins",
                                           opener=other)[0])

    def test_an_unknown_manage_action_is_a_404(self):
        status, result = self.request("/admin/api/admins/explode", {"name": "x"})
        self.assertEqual(404, status)
        self.assertFalse(result["ok"])

    # ------------------------------------------------------------ 权限
    def test_the_list_carries_everybody_s_role(self):
        self.request("/admin/api/admins/add",
                     {"name": "carol", "password": "SecretPw",
                      "role": "operator"})
        result = self.request("/admin/api/admins")[1]
        self.assertEqual([{"name": "admin", "role": "system"},
                          {"name": "carol", "role": "operator"}],
                         result["admins"])

    def test_login_and_session_report_the_role(self):
        # 前台靠它决定藏哪两个标签页。
        self.assertEqual("system", self.login()[1]["role"])
        self.assertEqual("system", self.request("/admin/api/session")[1]["role"])

    def test_changing_a_role_takes_effect_without_a_relogin(self):
        """★ 权限是**每一发请求现查**的：把一个人降成运营，他手里那个令牌
        应该当场失去「玩家资料」和「管理员账号」，不该等他重新登录。"""
        self.request("/admin/api/admins/add",
                     {"name": "carol", "password": "SecretPw",
                      "role": "system"})
        other = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request("/admin/api/login",
                     {"name": "carol", "password": "SecretPw"}, opener=other)
        self.assertEqual(200, self.request("/admin/api/admins",
                                           opener=other)[0])
        result = self.request("/admin/api/admins/role",
                              {"name": "carol", "role": "operator"})[1]
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["self_demoted"])       # 降的不是我自己
        # 同一个 cookie，没重登，直接就进不去了。
        self.assertEqual(403, self.request("/admin/api/admins",
                                           opener=other)[0])
        # 但配置页照常能用 —— 运营的活儿一点没少。
        self.assertEqual(200, self.request("/admin/api/config/shop",
                                           opener=other)[0])

    def test_demoting_myself_is_reported_so_the_page_can_react(self):
        self.request("/admin/api/admins/add",
                     {"name": "carol", "password": "SecretPw",
                      "role": "system"})
        result = self.request("/admin/api/admins/role",
                              {"name": "admin", "role": "operator"})[1]
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["self_demoted"])
        self.assertEqual(403, self.request("/admin/api/admins")[0])

    def test_demoting_the_last_system_admin_is_refused_by_the_server(self):
        self.request("/admin/api/admins/add",
                     {"name": "carol", "password": "SecretPw",
                      "role": "operator"})
        result = self.request("/admin/api/admins/role",
                              {"name": "admin", "role": "operator"})[1]
        self.assertFalse(result["ok"])
        self.assertIn("至少要保留一个系统管理员", result["message"])
        self.assertEqual("system", self.request("/admin/api/session")[1]["role"])

    def test_operators_do_not_count_when_removing_the_last_system_admin(self):
        self.request("/admin/api/admins/add",
                     {"name": "carol", "password": "SecretPw",
                      "role": "operator"})
        result = self.request("/admin/api/admins/remove", {"name": "admin"})[1]
        self.assertFalse(result["ok"])
        self.assertIn("至少要保留一个系统管理员", result["message"])


class OperatorPermissionTests(_AdminCase):
    """运营（`operator`）能碰什么、不能碰什么（D34，用户 2026-09-06 拍板）。"""

    def setUp(self):
        super().setUp()
        self.assertTrue(self.login()[1]["ok"])          # 先用系统管理员建人
        self.assertTrue(self.request(
            "/admin/api/admins/add",
            {"name": "carol", "password": "SecretPw",
             "role": "operator"})[1]["ok"])
        self.request("/admin/api/logout", {})
        self.assertTrue(self.login("carol", "SecretPw")[1]["ok"])

    def test_the_session_says_operator(self):
        self.assertEqual("operator", self.request(
            "/admin/api/session")[1]["role"])

    def test_the_four_config_pages_all_work(self):
        """运营的活儿就是这四页 —— 读得到、也存得进去。"""
        for which in ("items", "shop", "recipe", "drops"):
            status, result = self.request("/admin/api/config/" + which)
            self.assertEqual(200, status, which)
            self.assertTrue(result["ok"], which)
            status, saved = self.request("/admin/api/config/" + which,
                                         {"text": result["text"]})
            self.assertEqual(200, status, which)
            self.assertTrue(saved["ok"], which)
        # 物品选择器和图集也要能用，否则那四页画不出来。
        self.assertTrue(self.request("/admin/api/catalog")[1]["ok"])
        self.assertTrue(self.request("/admin/api/item?id=1120041")[1]["ok"])

    def test_every_system_only_api_is_403(self):
        """★★ 前台把那两个标签藏起来只是画面 —— 这条用例是**直接 POST**，
        证明藏掉的按钮背后真的有一道门。"""
        for path, payload in (
                ("/admin/api/players?q=a", None),
                ("/admin/api/player?name=alice", None),
                ("/admin/api/player", {"name": "alice", "money": 1}),
                ("/admin/api/admins", None),
                ("/admin/api/admins/add", {"name": "dave", "password": "pw12345"}),
                ("/admin/api/admins/password", {"name": "carol",
                                                "password": "Another1"}),
                ("/admin/api/admins/role", {"name": "carol", "role": "system"}),
                ("/admin/api/admins/remove", {"name": "admin"}),
                ("/admin/api/admins/from_player", {"name": "alice"}),
        ):
            status, result = self.request(path, payload)
            self.assertEqual(403, status, path)
            self.assertFalse(result["ok"], path)
            self.assertIn("系统管理员", result["message"], path)

    def test_an_operator_cannot_promote_himself(self):
        # 上面那条已经覆盖了，但这一条是**最要命**的一种越权，单独立一条。
        self.assertEqual(403, self.request(
            "/admin/api/admins/role", {"name": "carol", "role": "system"})[0])
        self.assertEqual("operator", self.request(
            "/admin/api/session")[1]["role"])


class AdminItemLookupTests(_AdminCase):

    def setUp(self):
        super().setUp()
        self.assertTrue(self.login()[1]["ok"])

    def test_a_real_item_comes_back_with_its_slot_and_bonus(self):
        result = self.request("/admin/api/item?id=1120041")[1]
        self.assertTrue(result["ok"], result)
        self.assertEqual(1120041, result["id"])
        self.assertEqual("weapon", result["kind"])
        self.assertEqual(1024, result["part_flag"])    # 武器槽 1
        self.assertTrue(result["ownable"])
        self.assertEqual("리볼버 R1", result["name_kr"])

    def test_an_id_the_client_does_not_know_says_so(self):
        # ★ 这正是这个小工具存在的理由：写错一个 id，界面上就是个空格子。
        result = self.request("/admin/api/item?id=9999999")[1]
        self.assertFalse(result["ok"])
        self.assertIn("空格子", result["message"])

    def test_a_non_numeric_id_is_refused(self):
        result = self.request("/admin/api/item?id=abc")[1]
        self.assertFalse(result["ok"])


class AdminPlayerTests(_AdminCase):
    """玩家资料页：找人、改等级 / 金币 / 材料 / 仓库物品（V0.3商店 D22）。

    ★ **商店在卖的东西也能直接发**（D23a，用户 2026-09-06 推翻了 D23）：
    等级门槛在**穿上**那一刻还要再判一次，塞进仓库不等于绕过它。
    """

    #: 挑几件东西当样本。材料一定不上架；`_LISTED` 是货架上在卖的那一批。
    _MATERIAL = 10001
    _MATERIAL2 = 10002
    _ARMOR = 1010064
    _LISTED = 1120011

    def setUp(self):
        super().setUp()
        self.accounts.register("alice", "pw1", display_name="爱丽丝")
        self.accounts.register("bob", "pw2", display_name="小明")
        self.login()

    def player(self, name="alice"):
        status, result = self.request(
            "/admin/api/player?name=" + urllib.parse.quote(name))
        self.assertEqual(200, status)
        self.assertTrue(result["ok"], result)
        return result["player"]

    def save(self, **payload):
        payload.setdefault("name", "alice")
        return self.request("/admin/api/player", payload)

    # ------------------------------------------------------------ 查找
    def test_search_matches_the_username_and_the_nickname(self):
        _status, by_user = self.request("/admin/api/players?q=ali")
        self.assertEqual(["alice"], [p["username"] for p in by_user["players"]])
        # ★ 昵称是中文 —— 查询串必须百分号编码，服务端那头 `parse_qs`
        #   才解得回 UTF-8（浏览器的 `encodeURIComponent` 干的是同一件事）。
        _status, by_nick = self.request(
            "/admin/api/players?q=" + urllib.parse.quote("小明"))
        self.assertEqual(["bob"], [p["username"] for p in by_nick["players"]])

    def test_search_is_case_insensitive_and_lists_everyone_when_empty(self):
        _status, upper = self.request("/admin/api/players?q=ALICE")
        self.assertEqual(["alice"], [p["username"] for p in upper["players"]])
        _status, all_of_them = self.request("/admin/api/players?q=")
        self.assertEqual(["alice", "bob"],
                         [p["username"] for p in all_of_them["players"]])

    def test_an_unknown_player_is_a_clean_404(self):
        status, result = self.request("/admin/api/player?name=nobody")
        self.assertEqual(404, status)
        self.assertFalse(result["ok"])

    # -------------------------------------------------------- 等级 / 金币
    def test_setting_the_level_moves_the_experience_with_it(self):
        _status, result = self.save(level=7, money=1234)
        self.assertTrue(result["ok"], result)
        _name, account = self.accounts.get_account("alice")
        self.assertEqual(7, account["level"])
        self.assertEqual(account_store.experience_for_level(7),
                         account["experience"])
        self.assertEqual(1234, account["money"])

    def test_resaving_the_same_level_keeps_the_experience_inside_that_level(self):
        # 5 级、本级内又攒了 40 点。再按「5 级」保存一次不该把那 40 点抹掉
        # —— 和 `experience_for_import` 中间那条是同一个道理。
        self.accounts.add_quest_reward(
            "alice", experience=account_store.experience_for_level(5) + 40)
        _status, result = self.save(level=5)
        self.assertTrue(result["ok"], result)
        _name, account = self.accounts.get_account("alice")
        self.assertEqual(account_store.experience_for_level(5) + 40,
                         account["experience"])

    def test_the_level_is_clamped_to_the_curve(self):
        self.save(level=999)
        _name, account = self.accounts.get_account("alice")
        self.assertEqual(account_store.LEVEL_MAX, account["level"])

    def test_a_non_numeric_level_is_refused(self):
        _status, result = self.save(level="七级")
        self.assertFalse(result["ok"])
        self.assertIn("等级", result["message"])

    def test_saving_nothing_says_so_and_touches_nothing(self):
        before = self.player()
        _status, result = self.save(level=before["level"], money=before["money"])
        self.assertTrue(result["ok"], result)
        self.assertEqual([], result["changes"])

    # ------------------------------------------------------ 材料 / 仓库
    def test_materials_can_be_set_and_cleared(self):
        self.save(materials={str(self._MATERIAL): 5,
                             str(self._MATERIAL2): 3})
        self.assertEqual(5, account_store.material_count(
            self.accounts.get_account("alice")[1], self._MATERIAL))
        # 0 = 把这一格删掉，另一格不受影响。
        self.save(materials={str(self._MATERIAL): 0})
        _name, account = self.accounts.get_account("alice")
        self.assertEqual({self._MATERIAL2: 3},
                         account_store.material_counts(account))

    def test_an_item_the_shop_does_not_sell_can_be_handed_out(self):
        _status, result = self.save(inventory={str(self._ARMOR): 1})
        self.assertTrue(result["ok"], result)
        _name, account = self.accounts.get_account("alice")
        self.assertTrue(account_store.has_item(account, self._ARMOR))

    def test_a_shop_item_can_be_handed_out_too(self):
        """★★ D23a：商店在卖的东西**也能直接发**（用户 2026-09-06 拍板）。

        以前这一条是反的（拒收 + 报「这些是商店在卖的东西」）。撤掉的理由：
        等级门槛在**穿上**那一刻客户端还会再判一次，塞进仓库穿不上身。
        """
        status, result = self.save(inventory={str(self._LISTED): 1})
        self.assertEqual(200, status)
        self.assertTrue(result["ok"], result)
        _name, account = self.accounts.get_account("alice")
        self.assertTrue(account_store.has_item(account, self._LISTED))

    def test_nothing_in_the_view_is_read_only_any_more(self):
        # 「锁着的」那一类连字段都没有了 —— 前台照着它画只读格子。
        self.accounts.add_item("alice", self._LISTED)
        self.accounts.add_item("alice", self._ARMOR)
        for row in self.player()["inventory"]:
            self.assertNotIn("locked", row)

    def test_a_shop_item_can_be_taken_away_again(self):
        self.accounts.add_item("alice", self._LISTED)
        _status, result = self.save(inventory={str(self._LISTED): 0})
        self.assertTrue(result["ok"], result)
        _name, account = self.accounts.get_account("alice")
        self.assertFalse(account_store.has_item(account, self._LISTED))

    def test_dropping_an_item_also_takes_it_off(self):
        self.accounts.add_item("alice", self._ARMOR)
        self.accounts.equip_item("alice", self._ARMOR)
        self.save(inventory={str(self._ARMOR): 0})
        _name, account = self.accounts.get_account("alice")
        self.assertEqual([], account_store.equipped_items(account))
        self.assertFalse(account_store.has_item(account, self._ARMOR))

    def test_an_id_the_client_does_not_know_is_refused(self):
        _status, result = self.save(inventory={"424242": 1})
        self.assertFalse(result["ok"])

    def test_a_negative_count_is_refused(self):
        _status, result = self.save(materials={str(self._MATERIAL): -1})
        self.assertFalse(result["ok"])
        self.assertIn("负数", result["message"])

    # ------------------------------------------------------- 分页（10 行一页）
    def test_the_list_is_paged_ten_at_a_time(self):
        for i in range(23):
            self.accounts.register("p%02d" % i, "pw1")
        _status, first = self.request("/admin/api/players?q=p")
        self.assertEqual(23, first["total"])
        self.assertEqual(10, first["size"])
        self.assertEqual(3, first["pages"])
        self.assertEqual(0, first["page"])
        self.assertEqual(10, len(first["players"]))
        _status, last = self.request("/admin/api/players?q=p&page=2")
        self.assertEqual(3, len(last["players"]))
        self.assertEqual(2, last["page"])

    def test_paging_past_the_end_falls_back_to_the_last_page(self):
        # 换了查询串却还停在第 5 页时，回一张空表会被当成「查无此人」。
        for i in range(12):
            self.accounts.register("p%02d" % i, "pw1")
        _status, result = self.request("/admin/api/players?q=p&page=9")
        self.assertEqual(1, result["page"])
        self.assertEqual(2, len(result["players"]))

    def test_a_junk_page_number_is_treated_as_the_first_page(self):
        _status, result = self.request("/admin/api/players?q=&page=abc")
        self.assertEqual(0, result["page"])

    # -------------------------------------- 装备类只有「有 / 没有」（无数量）
    def test_equipment_has_no_count_only_presence(self):
        # ★ 客户端的形态标志里装备发的是 `0x08` 可装备位，**数量那一格根本
        #   没人读**（§28）。所以 ×5 只是「有」，存成 ×1。
        _status, result = self.save(inventory={str(self._ARMOR): 5})
        self.assertTrue(result["ok"], result)
        _name, account = self.accounts.get_account("alice")
        self.assertEqual(1, account_store.inventory_items(account)
                         [self._ARMOR]["count"])

    def test_an_old_stray_count_on_equipment_is_left_alone(self):
        # 老存档里可能躺着 ×2（早先 `give` 发过两次）。「有无没变」就一个字节
        # 都不该动 —— 否则点开看一眼再保存，会多出一行「×2 -> ×1」，
        # 一个客户端根本不读的数字却让人以为自己改了什么。
        self.accounts.add_item("alice", self._ARMOR, count=2)
        _status, result = self.save(inventory={str(self._ARMOR): 2})
        self.assertEqual([], result["changes"])
        _name, account = self.accounts.get_account("alice")
        self.assertEqual(2, account_store.inventory_items(account)
                         [self._ARMOR]["count"])

    def test_turning_equipment_off_and_on_still_works_with_a_stray_count(self):
        self.accounts.add_item("alice", self._ARMOR, count=3)
        self.save(inventory={str(self._ARMOR): 0})
        _name, account = self.accounts.get_account("alice")
        self.assertFalse(account_store.has_item(account, self._ARMOR))
        self.save(inventory={str(self._ARMOR): 1})
        _name, account = self.accounts.get_account("alice")
        self.assertEqual(1, account_store.inventory_items(account)
                         [self._ARMOR]["count"])

    def test_the_view_says_which_rows_have_a_meaningful_count(self):
        self.accounts.add_materials("alice", {self._MATERIAL: 2})
        self.accounts.add_item("alice", self._ARMOR)
        view = self.player()
        self.assertTrue(view["materials"][0]["stackable"])
        rows = {row["id"]: row for row in view["inventory"]}
        self.assertFalse(rows[self._ARMOR]["stackable"])

    def test_the_view_marks_what_is_being_worn(self):
        self.accounts.add_item("alice", self._ARMOR)
        self.accounts.equip_item("alice", self._ARMOR)
        rows = {row["id"]: row for row in self.player()["inventory"]}
        self.assertTrue(rows[self._ARMOR]["equipped"])

    # ------------------------------------------------------------ 视图
    def test_the_view_carries_what_the_page_needs(self):
        self.accounts.add_materials("alice", {self._MATERIAL: 2})
        view = self.player()
        self.assertEqual("爱丽丝", view["nickname"])
        self.assertEqual(account_store.LEVEL_MAX, view["level_max"])
        self.assertFalse(view["online"])
        self.assertEqual([{"id": self._MATERIAL, "count": 2,
                           "stackable": True}],
                         view["materials"])


class AdminPromotePlayerTests(_AdminCase):
    """「设为管理员（运营）」：把玩家账号原样收进管理员表（D40）。

    ★ 这一组里最重要的是 `test_the_password_never_travels_through_the_page`
    —— 这个功能的全部意义就是「不用手抄密码」，抄一遍还不如手动加。
    """

    def setUp(self):
        super().setUp()
        self.accounts.register("alice", "pw1", display_name="爱丽丝")
        self.login()

    def test_a_player_becomes_an_operator_with_his_own_password(self):
        status, result = self.request("/admin/api/admins/from_player",
                                      {"name": "alice"})
        self.assertEqual(200, status)
        self.assertTrue(result["ok"], result)
        self.assertEqual("operator", self.accounts.admin_role("alice"))
        # ★ 真正的判据：**用游戏里那套用户名密码**就能登进管理页。
        self.request("/admin/api/logout", {})
        self.assertTrue(self.login("alice", "pw1")[1]["ok"])
        self.assertEqual("operator",
                         self.request("/admin/api/session")[1]["role"])

    def test_the_password_never_travels_through_the_page(self):
        """请求里只有用户名，回文里一个密码字都不许有（铁律 9）。"""
        _status, result = self.request("/admin/api/admins/from_player",
                                       {"name": "alice"})
        self.assertNotIn("pw1", json.dumps(result, ensure_ascii=False))

    def test_the_player_list_says_what_role_he_already_has(self):
        """列表上那个钮要变成灰的、写上实际权限，靠的就是这个字段。"""
        rows = self.request("/admin/api/players?q=")[1]["players"]
        self.assertEqual([None], [row["admin_role"] for row in rows])
        self.request("/admin/api/admins/from_player", {"name": "alice"})
        rows = self.request("/admin/api/players?q=")[1]["players"]
        self.assertEqual(["operator"], [row["admin_role"] for row in rows])
        # 后来在「管理员账号」页升成系统管理员 ⇒ 钮上的字也跟着变。
        self.request("/admin/api/admins/role",
                     {"name": "alice", "role": "system"})
        rows = self.request("/admin/api/players?q=")[1]["players"]
        self.assertEqual(["system"], [row["admin_role"] for row in rows])

    def test_promoting_the_same_player_twice_is_refused(self):
        self.assertTrue(self.request("/admin/api/admins/from_player",
                                     {"name": "alice"})[1]["ok"])
        _status, again = self.request("/admin/api/admins/from_player",
                                      {"name": "alice"})
        self.assertFalse(again["ok"])
        self.assertIn("已经存在", again["message"])

    def test_a_player_who_does_not_exist_is_refused(self):
        _status, result = self.request("/admin/api/admins/from_player",
                                       {"name": "nobody"})
        self.assertFalse(result["ok"])
        self.assertIsNone(self.accounts.admin_role("nobody"))


class AdminSessionStoreTests(unittest.TestCase):
    """`AdminSessions` 本身（不走 HTTP）。"""

    def setUp(self):
        self.now = 100.0
        self.sessions = web_admin.AdminSessions(ttl=60, clock=lambda: self.now)

    def test_a_token_resolves_until_it_expires(self):
        token = self.sessions.issue("admin")
        self.assertEqual("admin", self.sessions.resolve(token))
        self.now += 61
        self.assertIsNone(self.sessions.resolve(token))

    def test_tokens_are_unique_and_not_guessable_length(self):
        tokens = {self.sessions.issue("admin") for _ in range(50)}
        self.assertEqual(50, len(tokens))
        self.assertTrue(all(len(t) >= 32 for t in tokens))

    def test_dropping_is_idempotent(self):
        token = self.sessions.issue("admin")
        self.sessions.drop(token)
        self.sessions.drop(token)               # 不该抛
        self.assertIsNone(self.sessions.resolve(token))
        self.assertIsNone(self.sessions.resolve(None))

    def test_dropping_an_admin_kills_every_one_of_their_sessions(self):
        mine = [self.sessions.issue("carol") for _ in range(3)]
        others = self.sessions.issue("admin")
        self.sessions.drop_admin("carol")
        for token in mine:
            self.assertIsNone(self.sessions.resolve(token))
        self.assertEqual("admin", self.sessions.resolve(others))

    # ------------------------------------------------- 滑动过期（D29）
    def test_every_request_slides_the_deadline(self):
        """★ 用户 2026-09-05 要的：**最后一次操作**之后一小时才登出。

        判据是「有请求就算有操作」—— 隔一会儿摸一下，永远不掉线。
        """
        token = self.sessions.issue("admin")
        for _ in range(10):
            self.now += 59
            self.assertEqual("admin", self.sessions.resolve(token))
        self.now += 61
        self.assertIsNone(self.sessions.resolve(token))

    def test_a_slide_does_not_revive_an_already_expired_token(self):
        # 过期了就是过期了，续期只对还活着的令牌成立。
        token = self.sessions.issue("admin")
        self.now += 61
        self.assertIsNone(self.sessions.resolve(token))
        self.now += 1
        self.assertIsNone(self.sessions.resolve(token))

    def test_sliding_one_token_does_not_touch_another(self):
        keep = self.sessions.issue("admin")
        idle = self.sessions.issue("carol")
        self.now += 59
        self.assertEqual("admin", self.sessions.resolve(keep))
        self.now += 2                       # idle 的到期时刻已经过了
        self.assertEqual("admin", self.sessions.resolve(keep))
        self.assertIsNone(self.sessions.resolve(idle))

    def test_the_shipped_idle_timeout_is_one_hour(self):
        # 用户拍板的数。改它之前先确认是用户又说了话，不是谁顺手调的。
        self.assertEqual(3600, web_admin.SESSION_TTL_SECONDS)

    def test_the_cookie_outlives_the_session_on_purpose(self):
        """★ cookie 的 `Max-Age` 故意比会话 ttl 长（D29）。

        写成一样的话，「登录后连续操作两小时」到第 60 分钟浏览器会自己
        把 cookie 丢掉 —— 明明一直在用却被踢出去。cookie 只负责
        「关掉页面别消失」，真正说了算的是服务端那份滑动到期时刻。
        """
        self.assertGreater(web_admin.SESSION_COOKIE_MAX_AGE,
                           web_admin.SESSION_TTL_SECONDS)


if __name__ == "__main__":
    unittest.main()
