#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理页 `/admin`（V0.3商店 M8）。全部走真的 HTTP。

★ 这一组里最重要的是 `test_every_api_needs_a_login` —— 漏挂一个
`_require_admin()` 就等于把那个接口开在公网上。
"""
import http.cookiejar
import json
import os
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

    def test_the_page_renders_and_warns_about_the_default_password(self):
        status, html = self.request("/admin")
        self.assertEqual(200, status)
        # ★ D3 的补偿之二：弱默认口令 + 公网可达 ⇒ 页面上必须喊出来。
        self.assertIn("请立刻在下面「管理员账号」里改掉它", html)
        self.assertIn("var DEFAULT_PASSWORD_IN_USE = true;", html)
        # 占位符必须被换掉，不能原样漏到页面上。
        self.assertNotIn("__USERNAME_RULE__", html)
        self.assertNotIn("__PASSWORD_RULE__", html)
        self.assertNotIn("__DEFAULT_PASSWORD_IN_USE__", html)

    def test_the_banner_flag_follows_the_actual_password(self):
        self.accounts.admin_set_password(account_store.DEFAULT_ADMIN_NAME,
                                         "SomethingElse1")
        _status, html = self.request("/admin")
        self.assertIn("var DEFAULT_PASSWORD_IN_USE = false;", html)

    def test_login_succeeds_and_sets_an_http_only_cookie(self):
        status, result = self.login()
        self.assertEqual(200, status)
        self.assertTrue(result["ok"], result)
        cookies = [c for c in self.jar if c.name == web_admin.SESSION_COOKIE]
        self.assertEqual(1, len(cookies))
        # HttpOnly / SameSite 都不是 cookielib 的一等字段，从原始属性里翻。
        self.assertIn("HttpOnly", cookies[0]._rest)
        self.assertEqual("/admin", cookies[0].path)

    def test_a_wrong_password_does_not_log_you_in(self):
        _status, result = self.login(password="nope")
        self.assertFalse(result["ok"])
        _status, session = self.request("/admin/api/session")
        self.assertFalse(session["logged_in"])

    def test_every_api_needs_a_login(self):
        # ★★ 漏挂一个 `_require_admin()` 就等于把那个接口开在公网上。
        for path, payload in (
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

    def test_the_schema_covers_all_three_config_files(self):
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
        self.assertIn("至少要保留一个管理员", result["message"])
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
    """玩家资料页：找人、改等级/金币/材料/非商店物品（V0.3商店 D22）。

    ★ 这一组里最重要的是 `test_a_shop_item_cannot_be_handed_out` ——
    商店按**真实等级**卖东西，管理页要是能直接把 4 级才卖的枪塞进 1 级号的
    仓库，那条等级门槛就白设了。
    """

    #: 挑几件东西当样本。材料一定不上架；`_ARMOR` 是「商店里买不到」的那一批。
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

    def test_a_shop_item_cannot_be_handed_out(self):
        # ★★ 这条是整组的重点：商店在卖的东西只能靠买。
        status, result = self.save(inventory={str(self._LISTED): 1})
        self.assertEqual(200, status)
        self.assertFalse(result["ok"])
        self.assertIn(str(self._LISTED), result["message"])
        _name, account = self.accounts.get_account("alice")
        self.assertFalse(account_store.has_item(account, self._LISTED))

    def test_a_shop_item_the_player_owns_comes_back_locked(self):
        self.accounts.add_item("alice", self._LISTED)
        self.accounts.add_item("alice", self._ARMOR)
        rows = {row["id"]: row for row in self.player()["inventory"]}
        self.assertTrue(rows[self._LISTED]["locked"])
        self.assertFalse(rows[self._ARMOR]["locked"])

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

    # ------------------------------------------------------------ 视图
    def test_the_view_carries_what_the_page_needs(self):
        self.accounts.add_materials("alice", {self._MATERIAL: 2})
        view = self.player()
        self.assertEqual("爱丽丝", view["nickname"])
        self.assertEqual(account_store.LEVEL_MAX, view["level_max"])
        self.assertFalse(view["online"])
        self.assertEqual([{"id": self._MATERIAL, "count": 2, "locked": False}],
                         view["materials"])


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


if __name__ == "__main__":
    unittest.main()
