# FINDINGS — V0.2 硬事实

> 只记「**是什么**」。「为什么这么选」记在 `DECISIONS.md`。
> **失败的尝试也要记**，避免下一个会话重复踩坑。
> 编号**接着 V0.1 往下排，从 §122 起**（跨版本全局唯一，引用不会歧义）。

---

# 第零批 —— V0.1 关键结论速查（索引，不是原文）

V0.1 的原文在 `develop_history/V0.1_基础单机功能开发/.claude/FINDINGS.md`（§1~§121，5517 行）。
下面这张表是**索引**：先在这里定位，再决定要不要翻原文。写「V0.1 §NN」就是指那个文件。

## A. 联机开发最可能要翻的条目

| 想知道什么 | 去看 |
|---|---|
| **客户端→服务端 opcode 全表**（94 个，含类名） | V0.1 §45 |
| **服务端→客户端分发跳表**（`0x54e036`，opcode 就是下标） | V0.1 §46 / §81 |
| 认证服（47611）NMCO 协议、帧格式、16 个消息类 | V0.1 §35 / §36 / §38 / §39 |
| **认证握手完整报文**（`authserver.py --reply login` 实测跑通） | V0.1 §39 |
| 游戏服（27799）握手序列 + `gcpReqLogin` 载荷逐字段 | V0.1 §44 |
| 游戏服帧格式（`0xFE` 控制帧 / `0xFF` 游戏帧）+ SimpleCipher | V0.1 §42 / §43 |
| `gspRepLogin` 全部 8 个业务 int32 的落位 | V0.1 §95 |
| 金币 / 经验 / 等级只能靠 `0x0600 gspRepMoney` 下发 | V0.1 §96 |
| **房间座位 `SessionSlot` 线格式**（12 个字段） | V0.1 §78 |
| **`0x0300` 房间座位快照** | V0.1 §79 |
| **`0x0301` 换角色要服务端广播回来才生效**（action 4） | V0.1 §103 |
| `Session` / `SessionDescriptor` 线格式；房间类型 `1 普通 / 2 闯关 / 5 天梯` | V0.1 §69 / §59 |
| 建房那一串包的**顺序硬约束**（`0x0303` 必须在 `0x0201` 之前） | V0.1 §65 / §66 / §67 |
| **一次 `sendall` 合并**，否则「人物选择」小概率缩回 3 个头像 | V0.1 §120 |
| 大厅标签页切换 `0x020b → 0x0701` | V0.1 §62 |
| 「不符合等级要求，无法连接」= 账号等级**恰好等于 1** | V0.1 §83 |
| 「等级太低，无法选择任务」= 读的是**房主座位里的 u16 等级** | V0.1 §77 |
| 开局链 `0x0402 → 0x0401 / 0x0400 → 0x0403 → 0x0402` | V0.1 §84 / §85 |
| 死亡 / 重生链 `0x0408 → 0x0406`、`0x0413 → 0x0419` | V0.1 §108 |
| 掉落 `0x0406 → 0x0404`、拾取 `0x0407 → 0x0405` | V0.1 §113 / §115 |
| 换图 `0x0411 → 0x0417`、加载放行 `0x0412 → 0x0418` | V0.1 §111 |
| 结算 `0x040f → 0x0309 + 0x0411`（顺序是硬约束）+ 四格数值来源 | V0.1 §98 / §99 / §116 |
| 「完成 / 未完成」标签在 `0x0309` 的尾部数组里 | V0.1 §112 |
| `0x0106 gcpReportHack` = 免费的正确性检查器 | V0.1 §88 |
| 难度解锁只能靠 `0x020c` 全量快照；`0x0416` 会弹通知窗 | V0.1 §118 |
| 隐藏角色 / 隐藏关卡两把锁（`0x030b` + 地区 patch） | V0.1 §119 |
| GameGuard 绕过：DR0 + VEH 在 `0x54b0fc` 执行瞬间返回 `0x755` | V0.1 §121 |
| 登录框控件 id（`1004`/`1005`/`1006`/`1011`/`1012`） | V0.1 §24 |
| 客户端命令行 `/serverip:` `/serverport:` + 已死的官方地址 | V0.1 §14 / §3 |
| 日志刷盘拖慢启动那件事（`bslog` 每条 2 毫秒） | V0.1 §105 |
| `.ps1` 必须 UTF-8 with BOM | V0.1 §106 |

## B. 绝对不要做的事（V0.1 血泪，V0.2 照样成立）

- **不要发 `0x0409`**：两条分支都是「开始失败」提示框（V0.1 §76）。
- **不要在客户端报 `0x0403` 之前发 `0x0402`**：空指针崩溃（V0.1 §82）。
- **不要发服务端方向的 `0x0408`**：那是「与 %s 的网络状态不佳」告警（V0.1 §89）。
- **不要回显客户端的 `0x0406`**（那是掉落请求，服务端方向是死亡广播 = 随机杀角色）。
- **不要回显客户端的 `0x0411`**（那是换图请求，服务端方向是结算 = 中途踢进结算界面）。
- **不要回显客户端的 `0x0417`**（那是通关上报，服务端方向是换图放行）。
- **不要在收到 `0x0412` 之前发 `0x0418`**：客户端换图加载循环是**前置**判断。
- **不要给 `0x0407` 的应答加过滤**：漏回一发，那件掉落物**永久作废**（`[item+0x2a8]=1`）。
- **不要在 `0x0300` 之前发 `0x030b`**：角色持有判定第一步查「座位已占用」。
- **不要用 `0x0416` 改难度解锁**：会弹一个没读过的通知窗。
- **不要按进程名杀 Python**（`Get-Process python | Stop-Process`）：用户机器上还有别的活儿。
  一律按端口的 `OwningProcess` 精确停。
- ★ **同号反向是这套协议的常态**：同一个 opcode 在两个方向上语义完全不同，
  只能靠方向区分。加任何新应答之前先查 V0.1 §45 + §46 两张表。

## C. 账号存档 JSON 的形状（V0.1 建立，V0.2 在此之上扩展）

```json
{ "schema_version": 1, "active_account": null,
  "accounts": { "<用户名>": {
      "password": "", "display_name": "",
      "tutorial_completed": false, "tutorial_progress": 0,
      "level": 1, "experience": 0, "money": 0, "character": 0,
      "quest_difficulty": {}, "quest_unlock_all": true,
      "character_unlock_all": true, "owned_characters": [] } } }
```

- 等级由经验推出（`EXPERIENCE_PER_LEVEL = 100`，本地自定，不是逆出来的）。
- `quest_difficulty` / `quest_unlock_all` 只能靠 `0x020c` 下发。
- `character_unlock_all` / `owned_characters` 只能靠 `0x030b` 下发，且要排在 `0x0300` 之后。
- **`active_account` 是 V0.1 的单机遗物，V0.2 要删掉**（换成票据）。

---

# 第一批 —— V0.2 联机（2026-08-10 起）

## 122. ★★★★ 战斗内的角色位置同步**不在 gcp/gsp 协议里**（线索汇总，待 J.1 证实）

V0.1 结束时留了一句「真正的角色位置同步包还没找到」，嫌疑列表是
`0x040a` / `0x040b` / `0x040d`。逆向产物里有一组类名指向另一个答案：

```text
re/packets.txt      Packet_gcpStartTcpRelay        (客户端 -> 服务端)
                    Packet_gspJoinRelay            (服务端 -> 客户端)
                    Packet_rcpRegister             ← rcp = relay client packet
                    Packet_rcpRepPing
                    Packet_gspToggleUdpClientCommunication
                    Packet_gspToggleSendReplay
re/rtti_types.txt   RelayConnection   RelayAuthData   UDPBinder   UdpPacket
re/vftables.json    0x65e0d4 Packet_gcpStartTcpRelay   0x691814 Packet_gspJoinRelay
                    0x69152c RelayConnection           0x691580 RelayAuthData
V0.1 §45            0x00xx 段 = 「RelayConnection 用」（0x0000 / 0x0001 / 0x0003）
V0.1 §44            gcpReqLogin 的载荷里带着客户端的**内网 IP**（实测 192.168.11.215）
                    和疑似网卡 MAC —— 服务端要这个只有一个用途：告诉别的客户端去哪找它
boost 绑定类名      bind_t<..., GameSession, UDPBinder, ..., RawPacket&>
                    → GameSession 有一个吃 RawPacket 的 UDP 回调
```

**推论**（尚未实测，J.1 的任务就是证实或推翻）：
战斗内同步走**客户端之间的 UDP 直连**，服务端用 `gspToggleUdpClientCommunication`
开关它；打不通时走 `gcpStartTcpRelay` / `gspJoinRelay` 让双方连到一台中继服务器
（`RelayConnection` + `rcpRegister`，opcode 在 `0x00xx` 段）。

**为什么这对 V0.2 是头等大事**：
- 内网 IP 上报意味着 **P2P 只在同一局域网内可用**；云服务器在 NAT 后大概率打不通。
- 如果结论成立，云端联机必须由我们实现原版那套 TCP 中继 —— 那套协议一个字节都还没逆过。

**J.1 的验证手段**（成本从低到高）：
1. `bshook` 加 `sendto` / `recvfrom` / `WSASendTo` / `WSARecvFrom` / `bind` 的 hook，
   打一局单机看有没有 UDP 流量、绑在哪个端口；
2. `re/rtti_types.txt` 搜类名 → `re/vftables.json` 拿 vftable →
   `tools/re_bs.py xref` 找发送点 → 紧跟的 `push <opcode>` 就是包号（V0.1 §111 的手法）；
3. 读 `RelayConnection` 的虚表槽，对照 `ServerConnection`（V0.1 §47）看两者帧格式差在哪。

## 123. 认证服的登录应答有两个字符串字段，票据大概率走 `s1`

`server/protocol.py` 的 `build_login_reply(result, s1, s2, a, b, c)` 对应
`CULoginReplyPacket`（opcode `0x000c`）。客户端随后向游戏服发 `gcpReqLogin`，
**载荷第一个字段就是一个 wstring**，V0.1 §44 的实测抓包里它是**空的**
—— 因为我们一直回的是空串。

```text
认证服 0x000f CULogin2Packet(用户名, 密码)
   → 0x000c CULoginReplyPacket(result, s1, s2, a, b, c)
客户端 → 游戏服 0x0100 gcpReqLogin
   [0] wstring  ← ★ 就是从上面某个字符串字段转发过来的「票据」
   [1] 本机内网 IP（按字节）
   ...
```

⚠ **`s1` 还是 `s2` 尚未实测**。做法：两边各填一个可区分的测试串
（如 `s1="TICKET-S1"` / `s2="TICKET-S2"`），看游戏服的 `gcpReqLogin` 收到哪个。
这是 V0.2 多账号隔离的关键一环，**结论出来立刻补在这一节下面**。
