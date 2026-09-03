# V0.3 后半 总计划 —— 装备合成与商店

**目标**：让游戏里有东西可赚、可买、可穿，且**穿上之后属性真的在战斗里变**。

**每个里程碑都以「实机能看到的现象」收尾，不以「代码写完了」收尾。**

---

## 里程碑

| # | 名字 | 交付 | 状态 |
|---|---|---|---|
| M0 | 进度管理体系 | 本套文件 + 根目录指针 `CLAUDE.md` | ✅ 完成 |
| M1 | 数据基线 | `tools/shopdata.py` → `server/shop_items.json`；三份默认配置 | ✅ 完成 |
| M2 | 存档扩展 | `accounts.json` 新字段 + **幂等补齐** + 增删改查 + `admin_accounts` | ✅ 完成 |
| **M3** | **★ spike：装备生效** | 实机看到**最大 HP 变了**（换弹速度不受装备影响，见「三条既成事实」2） | 🟡 代码完成，**⏳ 等实机验证** |
| **M4** | **★ spike：商店 UI 协议** | 实机**打开商店界面且看得到商品**，货架 opcode 落锤 | 🟡 **下一步**（不依赖 M3 的结论） |
| M5 | 商店购买 + 仓库装备 | 花钱买到武器、仓库里换装、`0x030b` 跟着变 | ⬜ |
| M6 | 材料掉落 + 结算显示 + 调试 bat | 通关拿到材料、**结算界面那一栏画出来**、`quest-clear.bat` 一键通关 | ⬜ |
| M7 | 合成 | 攒够材料在合成界面点一下，装备进仓库 | ⬜ |
| M8 | 管理员网页 | `/admin` 登录、改三份 json 即时生效、管理员账号增删改查 | ⬜ |
| M9 | 测试 · 兼容 · 打包 · 上线 | 3.8 绿、打包钩子、云上迁移演练 | ⬜ |

**依赖**：M0 → M1 → M2 是地基。**M3 / M4 是两个 spike，必须尽早跑**
（用户选了只做原生 UI，风险全压在协议上，要让它最早暴露）。
M5 依赖 M3+M4；M6 依赖 M2；M7 依赖 M4+M6；**M8 是纯 Web 活，可与 M5~M7 并行**。

---

## M1 · 数据基线（纯离线，不碰运行时）

**新增 `tools/shopdata.py` + `tools/update-shopdata.bat`**（照 `tools/weapondata.py`
+ `update-weapondata.bat` 的形制），从 `D:\git\popshot-reborn\main\Pack_decrypt\Data\` 读：

- `ShopItem-Chn.ini`（UTF-16LE）→ itemId / `PartFlag` / `Tag` / 图标韩文名
- `EquipBonus-Chn.ini`（UTF-16LE）→ 13 格加成。★ **用 `int()` 解析，对齐客户端的 `_wtoi`**
- `weapon.ini`（CP949）→ 武器 itemId 经 `Tag` 关到弹药 `Id`，带出 damage / reload / magazine
- `Promotion-chn.ini`（UTF-16LE）→ 任务奖励基线（`Reward0/1/2`）

**产物 `server/shop_items.json`**（进 git、进服务端包）：
`{itemId: {part_flag, character, kind, series, tier, ammo_id, icon, name_kr, bonus, weapon}}`

**新增 `server/shopdata.py`** —— 读产物的懒加载 Store（照 `server/weapondata.py`），
提供 `item(id)` / `part_flag(id)` / `bonus(id)` / `is_material(id)` / `conflicts(a,b)`。

**三份运行时配置**（先在代码里写 `DEFAULT_*`，首次启动生成到 `server/data/`）：

| 文件 | 内容 | 初期规模 |
|---|---|---|
| `shop.json` | **所有会进玩家背包的东西**：id / 中文名 / 价格 / 是否上架 / 等级 / 角色限定 / 天数 | 武器 63 + 材料 ~20 + 合成产物 ~15 |
| `recipe.json` | 产物 id / 花费 / 等级 / 角色 / **最多 4 种材料** | ~15 条 |
| `drops.json` | 原版 4 条基线 + 按关卡难度掉珠子的扩展 | ~30 条 |

中文名按韩文原意翻译填进 `shop.json` 的 `name`（材料名参考新浪页面与
`Promotion-chn.ini` 的 `TextReward`）。用户随后可在管理页改。

---

## M2 · 存档扩展（`server/account_store.py`）

**每个账号新增三个字段**（加进 `NEW_ACCOUNT_DEFAULTS`，老号靠 `_merged_account`
自动补齐，**不动 `schema_version`**）：

```json
"inventory": {"1120041": {"count": 1, "expires": null}},
"equipped":  [1010015, 1120041],
"materials": {"30018": 3, "10001": 12}
```

`equipped` 是 **itemId 列表**（不是「部位→id」字典）—— 直接对上 `0x030b` 的语义。
服务端保证列表内两两 `part_flag` 不冲突（位与为 0），套装天然被这条规则覆盖。

**顶层新增 `admin_accounts`**（和 `accounts` 平级）：`{"admin": {"password": "Admin123"}}`。

**新方法**（全部「持锁 → 读盘 → 改 → 写盘」）：
`spend_money`（★ 查余额+扣必须同一把锁）、`add_materials` / `consume_materials`、
`add_item` / `has_item`、`set_equipped`、
`admin_verify / admin_add / admin_set_password / admin_remove / admin_names`
（★ `admin_remove` 删到只剩一个时拒绝）。

**幂等补齐**：照 `realign_levels()` 写 `ensure_item_fields()`，在 `app.py` 里
`_report_level_realign` 旁边加一行调用。**没有账号需要改时不写盘。**

---

## M3 · ★ spike：装备生效（最先跑，决定成败）

**改一处**：`send_slot_equipped_list()` 里把物品列表从「只有商城角色 id」
换成「商城角色 id + 当前装备的 itemId」。

**新增调试命令**：`equip <itemId>` / `unequip <itemId>` / `inv`，改存档并立刻重发 `0x030b`。

⏳ 实机验三条：① `Hp=8` 的装备 → **血条最大值变长**（加法、100% 生效，最好看）；
② `Defense=8` 的上衣 → 挨打伤害偶尔变小（★ **只有 15% 概率**，要多挨几下）；
③ 左轮 R1 `1120041` → 换弹变快，**同时验「武器 itemId 进 `0x030b` 客户端会不会真换枪」**。

**这一步不通，M5/M7 全部要重新设计。**

---

## M4 · ★ spike：商店 UI 协议

**目标：实机点开「商店」，界面出得来、看得到商品、不崩。**

1. **先只装监听**：给 `0x0704` / `0x0605` / `0x0607` / `0x0700` 各加处理器，
   只打日志 + hexdump，确认客户端真的会发、载荷长什么样
   （把「四发请求」从 🔍静态升成 ✅实测）。
2. **逐个试应答**（`gs_ctl.py raw` 手工发，一次只加一发）：
   `0x0704`→`0x0604` 空清单、`0x0605`→`0x0505` 空清单、`0x0607`→空礼物清单。
   ★★ **货架目录**：依次试 `0x0500` / `0x0503` / `0x0504`，哪个能让商品列表
   出内容就是它（`0x0503`/`0x0504` 处理器 `0x446700`/`0x446b62` 未逆，需要时现逆）。
3. **落锤后立刻更新 `re/packet_api.md`**（新开一节「商店 / 合成 / 仓库段」）。

⚠ **止损**：三个都试不出 → ① 逆 `0x446700`/`0x446b62` 拿真实结构
→ ② 用 `0x0604` 把「可购买的」当已拥有物品显示（畸形但能用）
→ ③ **停下来找用户重新拍板。不要硬凑。**

---

## M5 · 商店购买 + 仓库装备

新增 `server/shop.py`（业务逻辑 + 组包纯函数）和 `server/shopcfg.py`
（读三份 json，**mtime+size 热重载**，照 `versioning.load_client_filter()`，
带 `_reload=True` 测试参数）。`gameserver.py` 只做「解包 → 调 `shop.py` → 组包 → 发」。

| 收到 | 做什么 | 回什么 |
|---|---|---|
| `0x0704` | 读存档 inventory / equipped | `0x0604 gspRepEquippedList` |
| 进商店 | 读 `shop.json` 里 `listed=true` 的 | 货架包（M4 落锤的那个） |
| `0x0602` 购买 | 校验上架/等级/角色/未持有/余额 → `spend_money` + `add_item` | `0x0502` + **补发 `0x0600`** + 重发 `0x0604` |
| `0x0604` 上行 | 装备 / 卸下 → `set_equipped` | 回显 + **重发 `0x030b`** |

★ 失败要回明确失败码，不是静默不回。★ 价格从 `shop.json` 取，**不信包里的任何数值**。

---

## M6 · 材料掉落 + 结算界面显示 + 快速通关脚本

**掉落**：`send_end_game()` 里 `add_quest_reward` 旁加一发 `add_materials`；
规则做成纯函数 `quest_materials(quest_id, difficulty, cleared) -> {id: n}`
（照 `quest_reward` 的形状），数据来自 `drops.json`。

**★ 结算界面「合成材料」栏**（用户明确要求，链路见 FINDINGS §3）：
每种材料发一发 **`0x041c gspRewardReceived`**（`座位, 0, itemId, 数量`），
**排在 `0x0309` 之前** ⇒ `0x041c` × N → `0x0309` → `0x0411`。
组包纯函数 `build_reward_received(seat, slot, item_id, count)`。

**`tools/quest-clear.bat`**：骨架照 `tools/update-chrprops.bat`
（开发者工具，可含中文，**CRLF + UTF-8 无 BOM**），内容是调 `gs_ctl.py clear`。
★ `gs_ctl` 新增 `clear`：`quest.mark_success(True)` + `send_end_game()` ——
现有 `endgame` 打不出「通关」（`send_end_game(success=)` 全仓库没有调用点传过）。

---

## M7 · 合成

| 收到 | 做什么 | 回什么 |
|---|---|---|
| `0x0605` | 读 `recipe.json`，过滤等级/角色/已拥有 | `0x0505`（`CompositionRule` 列表） |
| `0x0606` | 校验材料够/金币够/未持有 → `consume_materials` + `spend_money` + `add_item` | `0x0506` + 补发 `0x0600` + 重发 `0x0604` |

★ 一条配方**最多 4 种材料**（UI 只有 4 个槽），校验时强制。
★ **无成功率**（原版 UI 没有任何概率控件），别自己发明。

---

## M8 · 管理员网页（可与 M5~M7 并行）

`server/web/server.py` 加路由（**和注册页共用 27810 端口**）：
`GET /admin`、`POST /admin/api/login|logout`、
`GET|POST /admin/api/config/{shop|recipe|drops}`、
`GET /admin/api/admins` + `POST /admin/api/admins/{add|password|remove}`。

- **会话**：内存 dict `{token: (admin, 过期)}` + `Set-Cookie HttpOnly`，重启失效。
- **登录限速**：照 `RegisterRateLimiter`。★ 公网可达 + 明文弱默认密码，
  限速是必要补偿；**页面上要提示「请立刻改掉默认密码」**。
- **删管理员至少保留一个** —— 在 `account_store` 层拒绝，不只前端拦。
- **存盘前校验**（itemId 存在 / 价格非负 / 配方材料 ≤4 且都存在 / 掉落参数合法），
  **校验不过不落盘**；落盘走「tmp + fsync + os.replace」。
- **热重载**：存盘后什么都不用做，`shopcfg` 下次读发现 mtime 变了就重读。

---

## M9 · 测试 · 兼容 · 打包 · 上线

- 新测试模块**必须加进 `server/run_tests.py` 的 `MODULES` 元组**，否则不会被跑。
- **Win7 兼容**：`runtime-win7\python\python.exe server\run_tests.py` 也要绿。
- **打包**（`tools/build-common.ps1`）：核心 `.py` 加进 `$must`；
  ★ **`shop_items.json` 要新写 `Copy-ShopData`**（新增 json 不会自动进包）；
  ★ **`admin.html` 要加进 web 白名单**；`server/data/*.json` 打包只建空目录。
- **`.gitignore`** 补 `server/data/{shop,recipe,drops}.json`（现有规则是逐文件的）。
- **云上迁移演练**：拿线上 `accounts.json` 样本 → 跑新版 → 确认原有字段一个不少、
  值一个不变 → 反复启动 3 次确认第 2/3 次不写盘 → 确认三份 json 已存在时**不覆盖**。

---

## 三条既成事实（不是 bug，别当 bug 查）

1. **攻击 / 防御加成只有 15% 概率触发**（`0x6938cc = 0.15f`，硬编码在客户端 exe 里）。
   HP / SP 是 100% 生效的加法，移动速度是 100% 生效的百分比。
2. **换弹速度不受装备加成影响**，只能靠换武器 —— 这正是「极速 R 系列」的价值。
3. **装备的属性数值改不了**（在客户端 pak 的 `EquipBonus.ini` 里，要改得重打客户端包）。
   我们能决定的只有「谁能拿到哪件」和「卖多少钱」。

## 本版不做

耐久度 / 修理、礼物系统（`0x0607` 只回空清单）、附魔（`Enchant.ini` 客户端里不存在）、
宠物 / 称号 / 期限制装备（`expires` 字段预留，初期一律永久）、
付费角色（已全体解锁）、结算界面的「称号卡片」槽（`0x041c` 的 `slot=1`，包已查明但留空）。
