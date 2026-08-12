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

## 123. ★★★★★ 票据走 `CULoginReplyPacket` 的**第二个**字符串（实测钉死）

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

### ★ 实测结论：是 **`s2`（第二个字符串）**

做法比预想的更省事 —— 第一版把票据放 `s1`、中文说明放 `s2` 就直接跑了一次，
游戏服的日志把答案写在了脸上：

```text
连接#1 ★ 认证通过 用户名='alice' 票据=124853ee…
#1 ✗ gcpReqLogin 的票据无效或已过期（登录成功）；回 gspRepLogin(result=3) 断开
                                    ↑↑↑↑
             客户端转发上来的 wstring 是「登录成功」= 我们放在 s2 的那句中文
```

所以线上顺序是 `int32 结果码 / string s1 / string s2 / int32 ×3`，
**客户端转发给 `gcpReqLogin` 的是 `s2`**。`s1` 客户端没有转发（多半是显示用的）。

改法：`authserver.make_reply()` 里 `s1=中文说明, s2=票据`，
`--ticket-field` 默认 `s2`（`s1` 只留给回归排查）。
`test_online.py` 有一条 `test_the_ticket_goes_into_the_second_string_field`
钉住这件事，别再改回去。

**实机验收**（V0.2 会话 01）：注册页建 `alice` → 游戏里填 alice/pw123 →
`认证通过 票据=d9a3c3e1…` → `gspRepLogin(result=0) 账号='alice' 票据=d9a3c3e1…
stored_level=1 下发等级=2`。多账号隔离这条链**通了**。

## 124. ★★★★★ 启动「时有时无地失败」= DR0 武装循环把**毫秒当成了次数**

**现象**：`start.bat` 有时正常进登录框，有时游戏窗口一闪就没了，
`logs\bsloader.err` 写着：

```text
[bsloader] 失败: 等待 bshook.dll 初始化握手 (GetLastError=1460)   ← 1460 = ERROR_TIMEOUT
```

而对应的 `logs\bshook_*.log` 里**只有 VEH 安装那一行，没有「DR0 已武装」**，
日志在 `install_hooks()` 的 `HOOK 主模块范围` 之后就断了。
V0.2 会话 01 实测 6 次冷启动里失败 2 次。

**根因**（`bshook.c` 的 `arm_gameguard_breakpoint_thread`）：

```c
/* 改之前 */
for (ticks = 0; !g_stop && ticks < POPSHOT_BSHOOK_READY_TIMEOUT; ticks++) {
    SuspendThread(main_thread);  ... GetThreadContext ...
    if (main_thread_left_injection_apc(ctx.Eip)) { 设置 DR0; break; }
    ResumeThread(main_thread);
    Sleep(1);
}
```

`POPSHOT_BSHOOK_READY_TIMEOUT` 是给 `bsloader` 用的**毫秒数**（10000），
这里却被当成了**循环次数**。Windows 的 `Sleep(1)` 按系统定时器精度取整，
默认 15.6 毫秒 —— 也就是说：

| 以为 | 实际 |
|---|---|
| 循环最多跑 10 秒 | 循环最多跑 10000 × 15.6 ms ≈ **156 秒** |
| bsloader 等 10 秒够用 | 10 秒内循环**只跑了六百来轮** |

主线程慢一点离开注入 APC（机器忙、ASProtect 解壳久），
`bsloader` 就先判超时把子进程杀了，而 DLL 那边还在慢悠悠地转圈，
连一行失败原因都没来得及写。**这不是概率问题，是两边用了不同的时间基准。**

**顺带解释了日志为什么断在 `GetModuleHandleA`**：武装循环每一轮都
`SuspendThread(主线程)`。主线程如果正好在 loader 里持着 loader lock 被挂起，
`watch_thread` 的 `GetModuleHandleA("user32.dll")` 就会一直等 ——
日志看起来像「卡死」，其实是被自己人按住了。

**修法**：循环上限改成按 `GetTickCount()` 算的**时间**，并留 2 秒余量让 DLL
先写下明确的失败原因；`POPSHOT_BSHOOK_READY_TIMEOUT` 10 秒 → 15 秒
（机器忙的时候光解壳就要好几秒）。失败分支现在会打印「试了几轮 / 几毫秒」。

**验收**：改完后干净冷启动 `武装=True 命中=True 登录框=True`。

⚠ **一个会骗人的复现方式**：连续「杀掉 BigShot → 立刻重启」不要间隔太短。
那样失败的原因是**另一件事** —— `BigShot_Assa` 互斥体还没释放，新实例秒退，
`bsloader.err` 写的是「bshook.dll 初始化前游戏已经退出」而不是握手超时
（V0.1 §9）。两条错误信息要分清楚，别把互斥体的问题当成握手的问题。
两次启动之间至少留 5 秒。

## 125. ★★★★ 登录框（`#32770` / `PopShot`）的完整控件表和几何

`tools\gui_probe.py enum <pid>` 实测（V0.2 会话 01，已给它加上位置输出）。
**对话框客户区 530×527**：

| id | class | x, y, w, h | 原文 |
|---|---|---|---|
| 1013 | Static | 21, 275, 133×19 | 开始火枪手之旅! |
| 1014 | Static | 32, 312, 54×14 | 选择分区: |
| **1011** | **Button** | **98, 310, 126×18** | **炮火连天(电信)** |
| **1012** | **Button** | **98, 331, 126×18** | **枪林弹雨(网通)** |
| 1015 | Static | 245, 310, 49×14 | 用户名: |
| 1016 | Static | 245, 338, 49×14 | 密码: |
| 1004 | Edit | 305, 308, 117×21 | （用户名）|
| 1005 | Edit | 305, 336, 117×21 | （密码）|
| 1006 | Button | 434, 308, 74×51 | 开始 |
| 1007 | Button | 305, 362, 96×18 | 记住帐号 |
| 1009 | Static | 305, 385, 135×18 | 您忘记密码了吗? |
| 1017 | Static | 21, 415, 200×19 | 您还没有注册成为世纪天成用户吗? |
| 1018 | Static | 32, 446, 403×14 | 尽情享受炮炮火枪手带给您的乐趣, |
| 1019 | Static | 32, 460, 420×14 | 请赶紧注册成为世纪天成会员,让我们用游戏创造乐趣! |
| **1010** | **Static** | **32, 492, 152×16** | **注册成为世纪天成用户**（那条蓝色链接）|
| -1 | Button | 4, 256, 522×264 | （分组框）|
| -1 | Static | 21, 296 / 434, 490×2 | （两条分隔线）|
| 0 | MPlay.Control.MiniBrowser | -1, 0, 533×260 | 内嵌 IE 公告页 |

**V0.2 靠这张表定的两个改法**：

1. **`1012` 的新文案装不下**。「联机(服务器地址请改:server.config)」比
   「枪林弹雨(网通)」长一倍多，126 像素会被裁掉；右边 `x=245` 就是「用户名:」，
   横着加宽会压上去。→ 加 `BS_MULTILINE` 再放高成 **200×36**，
   左边这一列 `y=349..415` 之间是空的，两行正好放得下。
2. **`1010` 那条链接根本点不动**，原版就点不动：
   - 它**没有 `SS_NOTIFY`** —— 没这个样式的 Static 对 `WM_NCHITTEST` 返回
     `HTTRANSPARENT`，鼠标消息压根到不了它身上；
   - 而且它在 **z 序上被分组框压着**（`EnumChildWindows` 按 z 序返回，
     那个 `id=-1` 的分组 Button 排在所有 Static **前面**），
     `WindowFromPoint` 在链接位置拿到的是分组框。
   - 实测：真鼠标点上去，**没有任何 `ShellExecute` 调用**（我们 hook 了
     `ShellExecuteW/A` 全程监听，一条都没有）。

   → 所以不去猜原版怎么处理的：加 `SS_NOTIFY` + `SetWindowPos(HWND_TOP)`
   把它提到最上面 + 子类化窗口过程，自己在 `WM_LBUTTONUP` 里开我们的注册页。
   实机验收：点下去日志出现 `LOGIN 点了注册链接 -> "http://localhost:27810/"`，
   服务端 `[web] ::1 "GET / HTTP/1.1" 200`（顺带证明注册页的双栈监听是通的）。

## 126. ⚠ 注入期的线程里**不要调 `LoadLibrary`**

`install_shell_hooks()` 第一版用 `LoadLibraryA("shell32.dll")` 去拿
`ShellExecuteW` 的地址。本 DLL 是在 EXE 入口点**之前**用 APC 注入的，
`watch_thread` 跑起来时主线程还在 ntdll 的 loader 里 —— 旁边的线程调
`LoadLibrary` 会卡在 loader 锁上，实测让 `bsloader` 等不到握手而超时
（现象和 §124 一模一样，两个原因叠在一起排查了半天）。

**规矩**：注入期只用 `GetModuleHandleA`，拿不到就下一轮再试
（`watch_thread` 每 100 毫秒一轮）。真要 `LoadLibrary`，等到一个
「进程肯定已经起完了」的时刻再做 —— 比如用户点了某个按钮的回调里。

## 127. HTTP/1.1 keep-alive：**回 404 之前也必须把请求体读干净**

注册页的 `test_an_unknown_api_is_a_clean_404` 一开始是**偶发**失败（约 1/6），
报的是客户端侧的 `ConnectionAbortedError [WinError 10053]` 而不是 404。

根因不在测试里：`web/server.py` 的 `do_POST` 对认不出的路径直接回 404，
**没有读那次 POST 的 body**。我们用的是 `protocol_version = "HTTP/1.1"`，
连接默认保持；剩在缓冲里的 body 会被 `BaseHTTPRequestHandler` 当成**下一个请求的请求行**
去解析，解析失败连接就被掐掉 —— 客户端那边表现成「响应读到一半连接断了」，
而且取决于 TCP 分片时机，所以时有时无。

**规矩**：`do_POST` 一进来就把 body 读完（`_read_body()`），**再**去查路由表。
body 超限（>1 MB）时除了回 400 还要 `self.close_connection = True` ——
那种情况下我们本来就没打算读完它。

改完连跑 15 轮 289 项测试，0 次失败。

## 128. ★★★★★ 登录失败的中文提示**客户端自己会显示** —— 只要回对 NMCO 错误码

需求要的是「不存在这个用户 → 提示需要注册；密码不正确 → 如实报错」。
一开始以为要靠 `bshook` hook `MessageBoxW` 换文案，实际上**一行客户端代码都不用改**。

`CULoginReplyPacket`（opcode `0x000c`）的第一个 int32 结果码不是随便一个「非 0 = 失败」，
它会被 `nmconew.dll` 的 `0x10077000` 映射成 **NM 错误码**，客户端再拿这个码去
`Data/Chinese.ini`（UTF-16LE 的**韩→中对照表**）查中文，最后弹：

```text
MessageBoxW(text = "<中文> (<码>)", caption = "登录失败")
```

**实测三组**（`bshook` 的 MSGBOXW hook 直接把原文抓下来）。
⚠ 这三条能被玩家看见的前提是 **§129 那个「吞框」的 detour 已经拆掉** ——
在那之前框照弹、日志照记，但 `det_MessageBoxW` 直接 `return IDOK`，屏幕上什么都没有：

| 服务端回的结果码 | 客户端弹的框 |
|---|---|
| `20025` | 登录失败 / **`不存在的帐号 (20025)`** |
| `20026` | 登录失败 / **`密码错误 (20026)`** |
| 其它任何非零值 | 登录失败 / `认证服务器失败 (20000)`（一句没用的笼统话）|

`caller=00423E1F` —— 和 V0.1 §24 里「连不上认证服时弹 `认证服务器失败 (20000)`」
是**同一个调用点**，当年那个 20000 就是「落进 default 分支」的样子。

`Chinese.ini` 里对应的三条（键是韩文原串，值是中文）：

```text
ID가 존재하지 않습니다        = 不存在的帐号
비밀번호가 일치하지 않습니다   = 密码错误
인증 서버 실패                = 认证服务器失败
```

**所以 `authserver.py` 必须发 20025 / 20026**，不能用 1 / 2 这种自己编的码 ——
编的码会全部落进 default，玩家只看得到「认证服务器失败」。
`test_online.py` 的 `test_the_failure_codes_are_the_ones_the_client_understands`
钉住这三个常量。

★ 顺带作废了一条计划：**里程碑 F.6 的「bshook 换 MessageBoxW 文案」不用做了**。
但 **bshook 还是得改一处** —— 那个框本来根本弹不出来，见 §129。

### 映射表原文（`nmconew.dll` `0x10077000`，输入 = 包里的结果码）

```text
0                -> 0（成功）        0x4e22/0x4e23    -> 原样
0x4e24/0x4e25    -> 0x4e20           0x4e26~0x4e2a    -> 原样
0x4e2b(20011)    -> 0x4e20           0x4e2c(20012)    -> 0x4e26
0x4e2d(20013)    -> 0x4e20           0x4e2e/0x4e2f    -> 0x4e2f
0x4e33(20019)    -> **0（成功！）**   0x4e35(20021)    -> 原样
0x4e39(20025) / 0x4e3a(20026) / 0x4e3b(20027) / 0x4e3c(20028) -> 原样
其它一切          -> 0x4e20(20000)
```

调用链（全部静态读出来的，`0x1006ae4e` 那两行是关键）：

```text
nmconew 0x1006ad40  登录 RPC：把回包的 [pkt+0x34]（= 结果码）写进出参 out[4]，
                    传输层状态写进 out[0]
        0x1006afb0  out[0]==0 && out[4]==0 才算成功
        0x10076fd9  失败且 out[0]==0 -> MapError(out) 用的就是 out[4]
        0x10077000  上面那张映射表
BigShot 0x5332a7    错误码 -> 韩文键 -> Chinese.ini -> 中文串
        0x423de0    sprintf("%s (%d)")，0x423e19 弹框
```

## 129. ★★★★★ 登录失败框弹不出来 = **被我们自己的 `MessageBoxW` hook 吞了**

用户报「输入错误密码和不存在的用户都没有任何反应」。服务端日志明明回了失败码，
`bshook` 日志里也**明明白白记着那个框**：

```text
★MSGBOXW caller=00423E1F type=00000000 cap="登录失败"
         text="认证服务器失败 (20000)"
```

—— 框确实弹了，只是 `det_MessageBoxW` **`return IDOK` 了事，从来没调真的 MessageBox**。

那是 V0.1 阶段 2 留下的：当时要抓「谁调用了 GameGuard 的错误框」，而且自动化跑起来
没人点确定，所以直接抑制。**GameGuard 早就绕过了，这个抑制却一直留着**，
于是 V0.2 新加的登录失败提示一出生就被自己人掐死。

**修法**：两个 detour 改成「记完日志转发给真函数」。翻遍历史日志，客户端这辈子只弹过
两种框（`登录失败` 和 `图像引擎初始化失败`），都该让玩家看见，没有需要抑制的。

**教训**：观测期的「抑制 / 拦截」类 hook 是**借来的**，进入功能开发期要逐个还回去。
一个返回值写死的 detour 不会报错，只会让上层功能安静地失效。

## 130. ★★★★ 登录框的两个坑：注册链接开**两个**网页 / 分区单选钮登录后被禁用

**（1）一次点击开两个网页。** `SS_NOTIFY` 加上去之后，Static 的默认窗口过程会在
**`WM_LBUTTONDOWN`** 那一刻给对话框发 `WM_COMMAND/STN_CLICKED` ——
而客户端的对话框过程里**真的有**这条链接的处理器，它去开那个早就停机的
`member.tiancity.com`。我们的子类化只吃了 `WM_LBUTTONUP`，按下那一半照样漏给了原过程。

⚠ 这**不推翻 §125**：原版点不动是因为没有 `SS_NOTIFY` + z 序被分组框压着，
鼠标消息根本到不了那条 Static；处理器一直在，只是从来没被触发过。
另外那次点击**没有任何 `ShellExecute` 调用**（我们全程 hook 着），
说明客户端开浏览器走的不是 `ShellExecuteW/A` —— 具体走哪个 API 没继续查，
因为把消息吃掉就够了。

**修法**：`link_wndproc` 把 `WM_LBUTTONDOWN` / `WM_LBUTTONDBLCLK` / `WM_NCLBUTTONDOWN`
一并 `return 0` 吃掉，对话框过程收不到 `STN_CLICKED`，死链接自然不会被打开。
**实测**：点一次 → Edge 标签页 13 → 14（只多一个），新标签就是我们的注册页。

**（2）第一次点「开始」之后，`1011` / `1012` 两个单选钮被客户端永久 `EnableWindow(FALSE)`。**
原版的想法是「服务器选定了就不许再换」，但它**登录失败时也不解禁** ——
玩家想从「单机」改成「联机」只能重启游戏。
**修法**：`poll_login_dialog` 每轮检查，发现被禁用就解禁回来
（对话框还在 = 还没登录成功，这时候换分区没有任何副作用）。

## 131. ★★★ 存档里的 `level` 是**派生字段**，手改它必须连经验一起补

用户在另一台机器上导出 `testuser1` 的存档，把 `level` 从 1 改成 5 再上传，
提示上传成功，但 `accounts.json` 里等级还是 1。

**不是导入丢字段，是等级压根不存**：`level` 由经验推出来（D024），
`_merged_account()` **每次读盘都重算一遍**，写进去多少都会被算回去。

**修法**（`experience_for_import()`，只在导入这一刻生效）：
存档里的等级**高于**经验推出来的等级 -> 把经验补到那一级的起点（`(lv-1)*100`）；
否则以经验为准。也就是**等级只能往上抬，想降级得连经验一起改小**（D070）。

其余字段（password / display_name / tutorial_* / experience / money / character /
quest_difficulty / quest_unlock_all / character_unlock_all / owned_characters）
**本来就都能正常更新**，`test_import_updates_every_field_of_an_existing_account`
逐个钉住了，还顺带断言「`NEW_ACCOUNT_DEFAULTS` 加了新字段这条用例必须跟着补」。

## 132. ★★★★ 被顶号的客户端会**自动重连并重放旧票据** —— 那句怪提示就是这么来的

用户报：第二台电脑用同一个账号登录，第一台被挤掉，弹的却是
**「在无法连接的地方尝试了连接。」**。

**这句话是客户端自带的**，不是我们写的。`Pack_decrypt\Data\Chinese.ini`：

```text
접속할 수 없는 곳에서 접속하셨습니다.=在无法连接的地方尝试了连接。      ← 韩文直译，本来就拗口
기존 연결을 끊습니다. 다시 로그인해 주십시오.=现有连接已断开。请重新尝试连接。
로그인에 실패하였습니다. 아이디와 비밀번호를 확인하세요.=登录失败。请重新确认帐号和密码。
```

**这三句由 `gspRepLogin` 的结果码挑**（处理器 `0x54f2cc`，V0.1 §44 只记了跳转地址，
没记文案）。三条失败分支**结构完全一样**（`[esi+0x898]=0` → `call 0x5bc415` 断开
→ 查 `Data/Chinese.ini` → `call 0x40f304` 弹「公告」框），只有字符串指针不同：

| result | 分支 | 字符串 VA | 弹出来的中文 |
|---|---|---|---|
| 0 | `0x54f4af` | `0x692464` | （成功，进大厅）|
| 1 | `0x54f468` | `0x6924e0` | 登录失败。请重新确认帐号和密码。 |
| **2** | `0x54f416` | `0x6924a4` | **现有连接已断开。请重新尝试连接。** |
| 3 | `0x54f3cf` | `0x692478` | 在无法连接的地方尝试了连接。 |

★ 这个「公告」框**不是 `MessageBoxW`**（`0x40f304` 是客户端自绘的 D3D 弹窗），
所以 bshook 的 MessageBox hook 抓不到它，只能截图看。

### 为什么被顶号会走到 result=3

`gameserver` 顶号时只是 `close_now()` 把旧连接的 socket 关掉，**什么包都不发**。
但客户端不会安静地退回登录框 —— 它**立刻自动重连**（实测 9 毫秒后），
并把**同一张票据**原样重放。而那张票据在第二台电脑登录时已经被
`TicketStore.issue()` 作废了，于是服务端回 `result=3`（票据无效）。
用户看到的就是 result=3 的那句话。

用户那次实测的服务端日志（`logs/server.out`，V0.2 会话 02 遗留）：

```text
[23:55:40.020] #4 gcpReqLogin                        ← 第二台电脑 192.168.11.79
[23:55:40.022] #3 ⚠ 账号 'testuser' 在别处重新登录，本连接被顶掉
[23:55:40.031] #5 +++ 连接来自 ::ffff:127.0.0.1:5828  ← 被踢的客户端 9ms 后自己重连
[23:55:40.194] #5 ★ 游戏包 opcode=0x0100 (gcpReqLogin) 载荷 98 字节  ← 重放旧票据
[23:55:40.195] #5 ✗ 票据无效或已过期；回 gspRepLogin(result=3) 断开
```

**修法**：`TicketStore` 记一张 `_revoked` 表（票据 -> 用户名 + 原因 + 时刻，
跟着同一个 TTL 过期），游戏服 `resolve()` 落空后再问一句 `revoked_reason()`：
是被顶掉的就回 **`result=2`**，别的（乱码票据 / 真过期）仍然回 `result=3`。
D071 记了为什么不干脆全都回 2。

**实机验证**（会话 03，本机）：用 `tools/gs_ctl.py raw 0100 <payload>` 直接把
result=2 / result=3 推给真客户端，截图对比 ——
截图在 `.claude/sessions/2026-08-11-03/`，两句话和上表完全对上。

⚠ **重连不是每次都发生**：本机复现时被踢的客户端重连后**没有**重放票据，
而是给认证服发了 `0x000d` 登出就退出了。触发条件（大厅状态？房间里？）没继续查，
因为两条路都已经被覆盖：重放 -> result=2 有提示；不重放 -> 客户端自己退，也没错。

## 133. 连接事件日志和 `--verbose` 必须解耦

`--verbose` 一开就是逐包 hexdump + 每条连接两个抓包文件，日志按 MB 涨，
云端长期开服不可能开着。可「谁连上了 / 谁断开了 / 从哪个 IP」恰恰是长期要留的东西。

`server/eventlog.py` 因此独立于两个 `log()`：统一前缀 `[online]`，
`grep online logs/server.out` 就是一份完整流水；同时**追加**写 `logs/online.log`
（`server.out` 每次启动被覆盖，这份不会）。落盘失败一律吞掉，绝不能因为
磁盘满/目录只读把服务端拖垮。

★ IPv6 双栈监听（D063）收到 IPv4 连接时 `getpeername` 给的是 `::ffff:192.168.11.79`。
日志里剥掉 `::ffff:` 前缀 —— 玩家报 IP 时说的是 `192.168.11.79`，带前缀对不上号。
真 IPv6 地址加方括号，否则分不清哪一截是端口。

## 134. ★★★★★ 「主线程离开注入 APC」这个判据是错的 —— 别人的电脑上登录界面出不来

用户把 V0.1 的包发给别人，那台电脑上启动脚本不报错，**游戏登录界面不出现**，
弹「Game guard文件不存在或已变更，请重新安装Game guard。」

对方的 `logs\bshook_*.log`（`hook_merge\logs_aaa\`），6 次启动全一个样：

```text
[20:49:52.185] HWBP  GameGuard VEH 已安装，等待 DR0 命中 0054B0FC
[20:49:52.190] HWBP  主线程已离开注入 APC（EIP=007B27B3），DR0=0054B0FC 已武装   ← 注入后 5 毫秒
[20:49:55.981] ★MSGBOXW cap="公告" text="Game guard文件不存在或已变更…"
                        #02 frame=0019FB1C ret=0054B562 <<< 主模块
```

**日志说「已武装」，但 `DR0 命中` 那一行永远不出现。**

### 根因

旧判据 `main_thread_left_injection_apc()` 是「主线程 EIP 不在
ntdll / kernel32 / kernelbase / bshook 这四个模块里」。
**主模块自己不在排除表里** —— 而注入 APC 一返回，主线程落回的正是
主模块尾部的 **ASProtect 壳**（`EIP=0x007b27xx`，SizeOfImage 到 `0x7c6200`）。
于是判据在注入后 5 毫秒就成立，DR0 在**壳还在跑的时候**写下去，
随后壳 `NtContinue` 恢复旧 CONTEXT，把 `Dr0` 清回 0。

我这台机器一直没炸，纯属**运气**：这里的采样要 720 毫秒才第一次落在
排除表外（`EIP=5E921DB0`，某个后加载的 DLL），那时壳已经跑完
（同一份日志里 `UNPACK base+0x1000 initial` 直接就是明文 `e9 15 02 00 00`）。
对方机器上 `initial` 是密文 `20 03 cc b2 19 11 a3 00`，`changed (#1)` 才变明文
—— 两台机器的解壳时机差了几百毫秒，判据的对错就翻面了。

### 修法（两条，缺一不可）

1. **换成因果性判据**：等 `0x54b0fc` 那 5 个字节变成已知明文
   （原版 `E8 CF 60 01 00`，或兼容旧内存 patch 的 `B8 55 07 00 00`）。
   ASProtect 是**整段一次性**解壳的（UNPACK 日志里一步从密文变明文），
   字节对上就说明壳已经真的跑过了。这是「壳跑完了没有」的直接证据，
   不是对 EIP 的猜测。
   读之前先 `VirtualQuery` 确认页已提交、可读、没有 `PAGE_GUARD`
   —— 别用 `IsBadReadPtr`，它会真去踩一脚，可能把壳的 guard page 异常吃掉。
2. **加 10 毫秒的守护回合**：命中之前反复复查 `Dr0`/`Dr7`，被清掉就补回去。
   有些 Windows / 驱动 / 安全软件组合会在 `SetThreadContext` 成功**之后**
   再恢复一份旧 CONTEXT，只写一次照样会「显示已武装但永不命中」。
   `g_gg_break_state != 0`（VEH 已命中）就退出，开销只有几秒钟。

★ **还留了兜底**：等不到已知签名也照样武装 + 守护（日志写明「兜底已武装」）。
不这么做的话，遇到一个我们没预料到的 exe 变体会**比改之前更差** ——
旧代码好歹还武装了。命中后 VEH 自己会做签名判定，对不上就只撤断点、
让原指令正常跑，不会瞎改 EIP。

### 挂起主线程期间**绝对不能调 `bslog()`**

主线程可能正拿着日志的锁，那是必死的死锁。
`ensure_gameguard_breakpoint()` 全程不打日志，所有 `bslog` 都在 `ResumeThread` 之后。

### 回归夹具

`hook\test_gg_watchdog.c` + `hook\test-watchdog.bat`：编一个固定 ImageBase 的
32 位小程序，在 `0x54b0fc` 摆上原版 `call`，等 DLL 武装 DR0 后**主动清掉一次**，
再执行那个地址。守护补回来 -> VEH 返回 `0x755` -> exit 0；没补回来 -> exit 5。

```bash
hook\test-watchdog.bat
```

**实测**（会话 03，本机）：`[test] PASS`，且 DLL 日志里真的有
`!! DR0 被清掉了（原 Dr0=00000000 Dr7=00000000），已补回（第 1 次）`。
真游戏冷启动 3/3 全过：`目标指令已解壳（0 ms / 0 轮，原始 call）` → `DR0 命中`
→ 登录框改造完成。

## 135. ★★★★ `chcp 65001` 下 **`.bat` 里的中文会把后面的命令行拦腰截断** —— 连 `rem` 注释里的也算

**现象**：`start.bat` 里删掉几行中文 `echo` 之后，运行时冒出

```text
'…行拦腰截断（见' is not recognized as an internal or external command,
```

那串东西是**我写在 `rem` 注释里的文字**。注释不该被执行，但它被当成命令了。

**根因**：`cmd.exe` 执行批处理时会记住「当前读到文件的哪个偏移」，执行完一行再 seek 回去
接着读。`chcp 65001` 之后它把文件按 UTF-8 解码成字符再算偏移，而 seek 用的是**字节**
—— 一个汉字 3 字节记成 1，偏移一路往前漂。漂到某一刻 seek 落在一个命令行的**中间**，
cmd 就把后半截当成一条新命令去执行。

**关键点，别记错**：

- 这和 CRLF **无关**。全局规范里那条「.bat 必须 CRLF」是另一个坑，两个都要防，但修一个
  不解决另一个。本次三个 `.bat` 全程都是 CRLF + UTF-8 无 BOM，照炸。
- **不限于 `if (...) else (...)` 括号块**，顶层的 `rem` 一样中招。最初以为是括号块专属，
  改成 `goto` 标签后仍然报错，才定位到是全文件的偏移漂移。
- 漂移量取决于**它前面累计了多少个多字节字符**，所以「改之前好好的，只删了几行就炸」
  完全正常 —— 原来的版本只是恰好没漂到断点上。这类 bug 靠「看起来没问题」根本防不住。
- 中文 `pause` 提示（`请按任意键继续. . .`）是 cmd 自己出的，不受影响。

**定论（V0.2 会话 04 起执行）**：`start.bat` / `start-debug.bat` / `stop.bat`
**只允许 ASCII**，一个汉字都不留（注释也不行）。所有中文提示由
`tools\launch.ps1` / `tools\shutdown.ps1` 打印 —— `.ps1` 是 UTF-8 with BOM，
PowerShell 整文件读进来再解析，没有这个 seek 问题。
失败路径的「[启动失败] …」也一并搬进 `launch.ps1` 的 `trap`，bat 只负责转发退出码。
里程碑 K 生成服务端包的 `.bat` 时同样照办。见 D074。

**非交互验证手法**（含 `pause` 的 bat 也能自动跑）：

```bash
sed 's|^powershell .*|rem (stub)|; s|^set "RC=%ERRORLEVEL%"|set "RC=0"|' start.bat > t.bat
# 转 CRLF 后
cmd //c "t.bat <nul"
```

`<nul` 让 `pause` 读到 EOF 自己结束。RC 分别替成 0 和 1 就能把两条分支都走一遍。

---

# 第二批 —— 里程碑 I 大厅联机（2026-08-11 起）

## 136. ★★★★★ 流读写原语全表（`0x5d59xx` / `0x5d5axx`），逆包格式先查这张表

所有 `Packet_*::Serialize` / `::Deserialize` 都只调这十几个 helper。
认出它们，任何包的线格式都能直接读出来，不用再猜。

**读（Deserialize，`ecx` = 流）**

| va | 读什么 | 返回/落位 |
|---|---|---|
| `0x5d5942` | u8 | `al` |
| `0x5d5956` | int32 → bool | `al` |
| `0x5d596f` | **u16** | `ax`（调用方常跟一句 `movsx eax, ax`）|
| `0x5d5984` | int32 | `eax` |
| `0x5d5998` | float32 | st0 |
| `0x5d59c1` | n 个原始字节 | 写进指针参数 |
| `0x5d59d0` | 1 字节 | 写进指针参数 |
| `0x5d59de` | int32 → bool | 写进指针参数 |
| `0x5d59f1` | 2 字节 | 写进指针参数 |
| `0x5d59ff` | 4 字节 | 写进指针参数 |
| `0x5d5a0d` | 8 字节 | 写进指针参数 |
| `0x5d5b3a` | **字符串**（u16 字符数 + UTF-16LE）| 写进指针参数 |

**写（Serialize，`ecx` = 流）**

| va | 写什么 |
|---|---|
| `0x5d5901` | 1 字节 |
| `0x5d5910` | 2 字节 |
| `0x5d591f` | 4 字节 |
| `0x5d592e` | 8 字节 |
| `0x5d5a4c` | 1 字节**零扩展成 4 字节**再写（线上是 int32）|
| `0x5d5a5a` | 字符串（u16 字符数 + UTF-16LE）|

★ `re/vftables.json` 的 key 就是**类的 vftable 首地址**，
**槽 0 = Serialize、槽 1 = Deserialize**（实测：`0x6915f0` = `Packet_gcpReqListSession`
的 vft，客户端在 `0x54e6c8` 处 `mov [ebp-0x1c], 0x6915f0` 建对象，
随后 `call [eax]` 调的就是 `0x54c0e2` = Serialize）。
只是**相邻类的 vft 挨着排**，肉眼按 3 个一组去数很容易错一格 —— 要认某个函数属于谁，
拿它的 va 去镜像里搜 dword，看它落在哪个 key 后面 4 字节。

## 137. ★★★★★ `Session` 的线格式 = `0x0303` 的载荷（不含尾部 u16）；房间列表条目就是它

`Session` 对象 0x30 字节，构造函数 `0x4052ff`，反序列化 **`0x556e80`**
（`esi` = this，`edi` = 流 —— 这个函数用寄存器传参，不是标准 thiscall）：

```text
int32              -> +0x04   房间状态（2 = 待机中，其余 = 游戏中，见 V0.1 §102）
string             -> +0x08   房间标题
int32              -> +0x0c   ★ 人数分子，见 §138
string             -> +0x10   地图名
int32（存 1 字节）  -> +0x14   语义未定
SessionDescriptor  -> +0x18   房间类型 + 参数（vft 0x65e09c，反序列化 0x557401）
```

`0x556ed1` = `0x556e80` + 再读一个 u16 存 `+0x3c` —— **这就是 `0x0303` 的完整载荷**，
`server/gameserver.py` 的 `build_update_session()` 已经和它逐字段对上。

**所以房间列表里每一项的房间信息，可以直接复用 `build_update_session()` 的前半段。**
V0.1 §63 已经指出「`LobbyStage` 和房间列表条目是同一套布局」，这里是逐字段的确认。

## 138. ★★★★★ `0x0200 gspRepListSession` 的完整线格式（每项 0x30 字节的字段全部查明）

反序列化 `0x559009`（`ecx` = 流，`ebx` = 包对象）。**这是 V0.2 PROGRESS 里
「每项 0x30 字节的字段还没逆过」那条待办的答案。**

```text
u16   n                       房间数；n <= 0 时 0x559023 的 jle 跳过整个循环
n 次 {
    Session                   见 §137（new(0x30) + 构造 0x4052ff + 反序列化 0x556e80）
    u16    房间号             -> 包+0x0c 的 vector
    u8     人数分母           -> 包+0x18 的 vector
    int32(bool)  ?            -> 包+0x24 的 vector
}
u16   ?                       -> 包+0x38
```

**每个字段是干什么的**（处理器 `0x54f596` → 大厅列表模型 `0x72e618`
的 `SetSessionList` `0x43a543`；模型 +0x0c/+0x18/+0x24/+0x30/+0x44 依次收下上面五样）：

| 字段 | 用途 | 证据 |
|---|---|---|
| `Session+0x04` | 「待机中」/「게임중(游戏中)」 | `0x43e5de: cmp [session+4], 2` |
| `Session+0x08` | 房间标题 | —— |
| `Session+0x0c` | **`%s(%d/%d)` 的第一个 %d** | `0x43e63f: mov ecx,[session+0xc]` |
| 每项 u16 | **房间号**，UI 上显示 `值+1`（`'%d번'`）| `0x43a435` 取 `[模型+0x18 + i*2]`，`0x43e565: inc eax` |
| 每项 u8 | **`%s(%d/%d)` 的第二个 %d** | `0x43e5cf: movzx eax, byte [模型+0x24 + i]` |
| 每项 bool | 未定（列表 UI 里还没找到读它的地方）| —— |
| 尾部 u16 | -> 模型 +0x44，疑似总房间数/分页用 | —— |

格式串：`0x6656e4 = '%s(%d/%d)'`、`0x6656f8 = '게임중'`、`0x665700 = '대기중'`、
`0x665708 = '%d번'`。`Chinese.ini`：`대기중=待机中`。

⚠ **`(%d/%d)` 两个数谁是当前人数、谁是上限，光看反汇编分不出来** —— printf 顺序只说明
`Session+0x0c` 在前、每项 u8 在后。实机各填一个不同的数看一眼就知道，先按
「`Session+0x0c` = 当前人数，u8 = 上限」实现，实机一眼看错就对调。

## 139. ★★★★ `0x0200 gcpReqListSession` 请求（12 字节）的字段

序列化 `0x54c0e2`（vft `0x6915f0` 槽 0），构造 `0x54e68c`，调用链
`0x43a6a5 → 0x43a78e → 0x54e68c`：

```text
u16   起始房间号     0x43a6c1 传的是「当前列表里 index 0 的房间号」-> 分页锚点
u16   ?             恒 0
u16   ?             来自列表模型 +0x54
u8    ?             来自列表模型 +0x58
u8    ?             来自列表模型 +0x59
int32 游戏类型       ★ 由 [conn+0x89c] 频道码翻译：7->2 8->6 9->5 其余->1
```

`0x43a78e` 里那个 `[ebp+0x1c]` **不进包**，它是轮询节流的毫秒数
（`cmp edi, [模型+0x48] + 间隔`，另有 10000 毫秒的强制上限）。

**服务端先按「忽略前五个字段，只用 game_type 过滤」实现**，把实收的 12 字节记进日志，
等实机看到真值再决定要不要做分页。

## 140. ★★★★★ 加入房间 `0x0202 gcpReqMoveInto` 的请求 / 应答 / 结果码

**请求**（客户端 -> 服务端，序列化 `0x558d9d`）：

```text
int32   房间号
string  密码
int32（由 1 字节零扩展）  ?
```

**应答**（服务端 -> 客户端，反序列化 `0x5590d5` = 连读 4 个 int32，处理器 `0x54fd07`）：

```text
int32 result
int32 房间号     -> LobbyStage+0x1c8
int32 我的座位号  -> LobbyStage+0x1cc（经 0x405a1f；**-2 = 观战**）
int32 ?          成功分支没用到
```

| result | 客户端行为 |
|---|---|
| **0** | 成功：写房间号/座位号，`座位.occupied=1`、`座位.character_id=0`，`ChangeStage(5)` 进房间 |
| 1 | 弹「进入失败 / 此房间已开始游戏。」 |
| 2 | 弹「进入失败 / 已超出人数限制的房间。」 |
| 3 | 弹「进入失败 / 没有符合条件的房间。」 |
| 4 | 弹「进入失败 / 密码错误。」 |
| **5** | 先 `0x40889b`（把座位 0/1/2 的 `+0x14` 清 0）**再走成功分支** —— 疑似「进观战席」|
| 其它 | 弹「进入失败 / 无法进入房间。」 |

文案都是客户端自带的（`Chinese.ini`），和 D069 / D071 同一条路子：**回对结果码即可，
一个字都不用我们写**。

★ 成功之后客户端立刻 `ChangeStage(5)` 建 `RoomStage`，而 `RoomStage` 构造函数
`0x466979` 读的是 `LobbyStage` 里的 Session（状态/描述符/地图名）。所以服务端必须
**先 `0x0303` 再 `0x0202` 应答，然后 `0x0300`、最后 `0x030b`**，并且和建房那一路一样
**合并成一次 `sendall`**（V0.1 §120 / D058）。

## 141. ★★★★★ 聊天：`0x0305` 两个方向的线格式

**客户端 -> 服务端 `Packet_gcpSendChatMsg`**（vft `0x6916bc`，序列化 `0x54c26c`）：

```text
u8      聊天类型
string  正文
```

**服务端 -> 客户端 `Packet_gspReceiveChatMsg`**（vft `0x65e164`，
序列化 `0x404e3b` / 反序列化 `0x404e76`，处理器 `0x406adb`）：

```text
u16     发言者座位号     0..5；用 0x4045f9 判座位是否占用，占用就从座位里取昵称
string  发言者显示名     ★ 非空 -> 渲染成 '%s : %s' % (显示名, 正文)
                          空   -> 只渲染 '%s' % 正文（系统提示就走这条）
string  正文
int32   聊天类型         传给 0x40605d 决定颜色
```

也就是说**「谁说的」由包里的第二个字符串决定，不是从座位查的** ——
座位号只用于「点名字」之类的交互。系统消息把显示名留空即可。

## 142. 大厅包分发跳表 `@0x406332`（`0x0300`~`0x0311`，索引 = opcode 低字节）

```text
0x0300 -> 0x406232   0x0301 -> 0x40623f   0x0302 -> 0x40624b   0x0303 -> 0x406258
0x0304 -> 0x4062ea   0x0305 -> 0x406263   0x0306 -> 0x406272   0x0307 -> 0x40632d
0x0308 -> 0x40627e   0x0309 -> 0x4062ea   0x030a -> 0x4062ea   0x030b -> 0x40628a
0x030c -> 0x406295   0x030d -> 0x40629f   0x030e -> 0x4062ac   0x030f -> 0x4062b3
0x0310 -> 0x4062ea   0x0311 -> 0x4062bc
```

`0x0304 / 0x0309 / 0x030a / 0x0310` 都指向同一个 `0x4062ea`（结算那条路，V0.1 §98）。

## 143. ★★★★★ 里程碑 I 的实机确认（会话 04，单机一个客户端 + 假房间）

`tools/gs_ctl.py fakeroom` 造「没有连接的房间」，就能在**一台机器一个客户端**上
把房间列表、加入房间、聊天全部验完 —— 不必等第二台电脑。

**§138 里那两个「⚠ 还没实机确认」的问题，答案是**：

| 问题 | 实测结论 |
|---|---|
| `%s(%d/%d)` 哪个是当前人数 | **`Session+0x0c` = 当前人数，列表项的 u8 = 上限**。发 (2, 6) 显示成 `待机中(2/6)`，假设正确，不用对调 |
| 房间号从 0 开始分配对不对 | **对**。room_id=1 的房间显示成「2号」，room_id=0 显示成「1号」。`'%d번'` 确实是 `值+1` |

**一次跑通的整条链**（截图在 `.claude/sessions/2026-08-11-04/`）：

```text
05  大厅列表：「组队战 / 무투전 / 待机中(2/6) / 2号 对战房」—— 标题、状态、人数、房号全对
06  双击房间 -> 进房：三个座位的角色、昵称（测试玩家1/测试玩家2/alice）、
    等级（Lv.9/Lv.9/Lv.2）、「房主」标记、人物选择的 6+ 个头像 全部正确
10  聊天：消息条 `Bob : hello from bob`，且座位号对应的角色头上冒气泡
11  系统提示（显示名留空）：`someone joined the room.`，**没有「谁 : 」前缀**
14  客户端自己建房 -> 立刻出现在大厅列表里
15  退房 -> 房间从列表消失
```

**服务端日志同时确认的**：

- `0x0202` 请求载荷 **10 字节** = `int32 房间号 + wstr(空) + int32` —— 和 §140 对上
- 客户端发的 `0x0305` 解析正确：`聊天(type=0) 'alice': 'hibob' 座位=2`
- 建房后客户端紧跟着发 `0x0302`，服务端把地图名同步进大厅：
  `map='Domir_Newbie:NewPvp'` —— 房间列表里后进来的人看到的就是这个

⚠ **两点没验到**（需要第二台电脑，见 PROGRESS 的 ⏳ 区）：
真人加入时的 `0x0301` action 0 广播、房主离开时的房主转移。
单机只能靠单元测试（`test_room.py` 覆盖了这两条的**包序列**，但不是实机）。

## 144. ★★★ 进了房间之后客户端每 10 秒发**两发** `0x0310 gcpStartTcpRelay`（8 字节）

会话 04 实测：`alice` 进房间之后，服务端日志里稳定出现

```text
[10:23:20.923] #2 ★ 游戏包 opcode=0x0310 (gcpStartTcpRelay) 载荷 8 字节
[10:23:20.925] #2 ★ 游戏包 opcode=0x0310 (gcpStartTcpRelay) 载荷 8 字节
[10:23:30.941] ... （每 10 秒一对，一直发到退房）
```

**这是里程碑 J 的头等线索**（§122 的推论第一次有了实测支撑）：
战斗还没开始、只是待在房间里，客户端就已经在**主动要求建中继通道**了。
不回它也不影响进房 / 聊天 / 退房（本次全程没回过）。

J.1 探查从这里入手：先把这 8 字节 dump 出来（`start-debug.bat` 就有 hexdump），
再看 `Packet_gcpStartTcpRelay`（vft `0x65e0d4`）的序列化函数写了哪两个 int32。

## 145. 单机验证房间功能的手法（`fakeroom` / `rooms` / `delroom`）

```bash
runtime\python\python.exe tools\gs_ctl.py fakeroom 对战房 1 2   # 标题 类型 人数
runtime\python\python.exe tools\gs_ctl.py rooms                 # 看现在有哪些房间
runtime\python\python.exe tools\gs_ctl.py delroom 1             # 强行解散
```

假房间的座位 `conn=None`，广播时被 `Room.members()` 过滤掉，所以它只影响**列表和
座位快照**，不会往任何 socket 上发东西。

★ 这里踩到一个真缺陷：`Lobby._leave_unlocked` 原本按 `remaining`（还剩几条**连接**）
判「房间空了没有」，导致最后一个真人退出时把还坐着假座位的房间也解散了。
正常游玩时两者等价，但意图应该是「**座位**还有没有人」—— 已改成 `room.is_empty()`，
并补了回归测试。**这类「测试替身暴露出的真 bug」值得记一笔**：假房间不是玩具，
它逼出了一个真实场景里迟早会遇到的语义混淆。

## 146. 登录框自动化的两个坑（会话 04 又踩了一遍）

1. **`tools/click.py` 的坐标是客户区坐标，而 `tools/screenshot.py` 抓的是整个窗口
   矩形**（含边框和标题栏）。两者差 **(3, 26)**：截图上量到 `(x, y)`，
   要点的是 `click.py <pid> (x-3) (y-26)`。直接把截图坐标喂进去会点空。
   ★ 窗口被移动过之后这个差值**不变**（边框尺寸是固定的），不用重新量。
2. **没做过教程的账号一登录就被拉进教学关**，房间/大厅一个都点不到。
   跑大厅相关的验证前先把存档标成教程已完成：

   ```python
   for acc in json.load(open('server/data/accounts.json'))['accounts'].values():
       acc['tutorial_completed'] = True
   ```

---

# 第三批 —— 用户实机反馈（2026-08-11 会话 05 起）

## 147. ★★★★★ 有人离开房间要发 `0x0301` **action 1**，发 3 的话 3D 模型不消失

**用户实机报的缺陷**：非房主退房 / 房主踢人之后，房里剩下的人看到
「玩家列表」里那一行确实空了，但**上方蓝天白云那块的角色模型还杵在原地**
（截图 `.claude/sessions/2026-08-11-05/01-bug-离开后模型还在.png`）。

**根因**：V0.1 按名字把 action **3** 当成了「离开/踢出」，但客户端里
**只有 action 1/2 会销毁座位的角色对象**。逐条读 `0x40648d`（`0x4064f7` 起分支）：

| action | 去哪 | 干什么 |
|---|---|---|
| 0 | `0x406691` | 清 IP/端口 → **`0x405e1c` 建模型** → `0x406f42` / `0x4089fa` → 刷 UI |
| **1 / 2** | `0x406676` | 清 IP/端口 → **`0x405f8f` 销毁模型**并把 `[LobbyStage+0x1d0+i*4]` 清 0 → 刷 UI。★ 两个码在客户端里**完全等价**（同一个入口，没有任何分支） |
| 3 | `0x406628` | **按座位数据重建**模型：占用且模型在 → `0x405fba` 把模型对齐到 `seat+0x0c` 的角色 id；**不占用 → 什么都不做地返回**（连 `0x405a74` 刷 UI 都不走）|
| 4 | `0x406520` | 播「%s님이 %s 캐릭터로…」，然后**落到 `0x406628`** 重建模型 |

关键的两个子函数：

```text
0x405f8f(eax=LobbyStage, ecx=座位号)   ; 从场景里摘掉(0x474029) + 虚析构 + 指针清 0
0x405d8c(ecx=LobbyStage, arg=1)        ; 六个座位挨个对齐：占用就建/换，不占用就 0x405f8f
0x4045f9(ecx=LobbyStage, eax=座位号)   ; 「这个座位占用吗」= [LobbyStage + 座位*0x3c + 0x40]
```

★ **所有 action 分支之前**（`0x4064d6 → 0x556d9d`）都会先把包里的 `SessionSlot`
反序列化**进座位**。所以发 action 3 + 空座位时，「玩家列表」确实会空掉
（那是座位数据被冲掉的效果，不是 action 3 干的），只有模型留了下来 ——
这正好解释了用户看到的「名字没了、人还在」。

**为什么房主离开时看起来是好的**：房主走了要转移房主，服务端会**补发一份
`0x0300` 全量座位快照**，而 `0x0300` 的处理器 `0x40637a` 里调了 `0x405d8c(1)`，
它对每个不占用的座位都调 `0x405f8f` —— 顺手把模型收干净了。
非房主离开 / 踢人这两条路只发 `0x0301`，就露馅了。

**修法**：`SEAT_ACTION_LEAVE` 从 3 改成 **1**。`build_session_member_update`
原本有一条「action 1/2 会销毁角色对象，服务端绝不能发」的护栏（V0.1 单人时代的
判断），现在改成只挡跳表不认的码（0~4 之外）。

**实机验证**（会话 05，截图在 `.claude/sessions/2026-08-11-05/`）：

| # | 怎么验的 | 结果 |
|---|---|---|
| 02 | 对着**用户留在屏幕上的那个鬼影**直接 `gs_ctl.py raw 0301 010100000000000000000000` | 模型当场消失，客户端没崩，其余 UI 不受影响 —— 这一发就把「1 到底销不销毁」钉死了 |
| 03/04 | `tools/fakeclient.py`（§148）当第二个玩家：bob 进房 → 退房 | 房主那边模型消失、玩家列表空、系统提示「bob 离开了房间。」 |
| 05 | bob 再进来，房主点踢人 →「真要强制踢出吗?」→ 是 | 模型消失，提示「bob 被房主请出了房间。」|
| — | 反过来：bob 在房里，**房主**退房，看假客户端收到的字节 | `0x0301 action=1 座位=0` → `0x0300 房主座位=1` → 系统提示，顺序和内容都对 |

## 148. ★★★★ `tools/fakeclient.py` —— 一台电脑上就能造出「第二个玩家」

`gs_ctl.py fakeroom` 造的假房间没有连接，`gs_ctl.py raw` 只能手搓单个包。
**`tools/fakeclient.py` 走真 TCP + 真认证 + 真票据**，服务端完全把它当成正经玩家：

```bash
runtime\python\python.exe tools\fakeclient.py bob pw456 join 0 sleep 3 \
    chat "bob进来了" shot 34124 two.png leave sleep 3 shot 34124 after.png
```

命令是一串 token，从左往右执行：`join <房间号>` / `leave` / `chat <文本>` /
`sleep <秒>` / `hold <秒>` / `shot <真客户端pid> <png>` / `quit`（不发 0x0203 直接断，
= 模拟拔网线）。收到的每一帧都打印，`0x0301` 会额外解出 action 和座位号 ——
**「服务端到底广播了哪个 action」终于能在一台机器上逐字节看见**。

拼装它只需要三块现成的东西：`server/protocol.py`（47611 的帧 + 加密 + 登录包）、
`server/simple.py`（27799 那层 SimpleCipher，收发**两个方向各一份状态**）、
`server/gameserver.py`（`build_game` / `take_frame` / `w_i32` / `w_wstr`）。
连上 27799 之后的开场白是**明文 int32 版本号 311**，然后才是
`0x0100 gcpReqLogin`（载荷第一个字段 = 票据，其余字段服务端不读）。

★ 这把工具对**里程碑 J** 更重要：战斗内联机要验「两个人在同一局里」，
不然每验一次都得占用用户的第二台电脑。

---

# 第四批 —— 里程碑 J.1 战斗内联机探查（2026-08-11 会话 06 起）

> §149~§154 原本是纯静态逆向结论，**已在同一个会话里实机验证通过 —— 见 §155**。
> 还没验到的只剩「真客户端**收到** `0x040f` 之后的表现」（要两台电脑，⏳ 区第 7 条）。

## 149. ★★★★★ 战斗内同步的真实架构：**双通道**，而且「全部走游戏服」是原版自带的模式

§122 的推论**一半对一半错**。对的是「同步不在 gcp/gsp 协议里」；
错的是「只能靠 P2P UDP，云端 NAT 打不通就完了」。

真正的发送函数是 `GameSession::SendToAll` **`0x4077c2`**（`eax`=GameSession，`ebx`=UdpPacket）：

```text
0x407e94(packet, 1)          ; 盖上序列号等头部字段
packet.hdr[2] = 0xff         ; 目标座位 = 0xff（广播）
0x408619(session, packet)    ; ★ 通道 A：中继 TCP 或 0x040e→游戏服   ← 受 [session+0x3e4] 开关
for (i = 0; i < 6; i++) {    ; ★ 通道 B：UDP 直连每个座位            ← 受 [session+0x3f8] 开关
    if (!seatOccupied(i)) continue;              ; 0x4045f9
    if (i == mySeat) { 本地回环，见 §151 }        ; 0x405a5d
    else {
        if ([session+0x2bc] == 0) continue;      ; 没有 UDPBinder
        if ([session+0x3f8] == 0) continue;      ; ★ UDP 开关
        packet.hdr[2] = i;  0x4076ba(i, packet, 0);   ; sendto
    }
}
```

**通道 A `0x408619(session, packet)`**（3 个调用点：`0x4058cc` / `0x4077db` / `0x408257`）：

```text
if ([session+0x3e4] == 0) return;                  ; ★ 总开关，默认 0
if ([0x72e290] != 0 && [[0x72e290]+0x894] != 0)    ; RelayConnection 存在且已连上
     0x54beb6(packet)                              ;   → 走原版中继，rcp opcode 3
else 0x5594be(...)                                 ;   → 包成 0x040e 发给**游戏服**
```

`0x5594be`：`dst.SetType(0x040e)`，body = **整个 UdpPacket 的缓冲区**（含它自己 12 字节头），
然后 `[0x72e30c]`（= `ServerConnection`）`vft[0x18]` 发出去。

**收的一侧**：`GameSession::OnPacket`（`0x4061e2`，由 `ServerConnection` 分发器
`0x54e036` 在 `0x54e04a` 处调用）里，opcode **`0x040f`** → `0x4086b5`：
把收到的 RawPacket **剥掉自己的 10 字节头**（`0x55b9bc`），
剩下的字节当成 UdpPacket 交给 `0x407869` —— **和 UDP 收到的走同一个入口**。

### ⇒ 对 V0.2 的意义（这是 J.1 最重要的一句话）

**不需要实现原版的中继服务器，也不需要 UDP 打洞。**
客户端本来就支持「把全部 P2P 流量塞进已有的游戏服连接」，服务端只要：

1. 发 `0x0410`（int32 = 1）把 `[session+0x3e4]` 打开；
2. 把每个客户端发来的 `0x040e` 载荷**原样**再发给同房间的其他人，opcode 换成 `0x040f`；
3. **不要**回 `0x0210 gspJoinRelay`（不回，`[0x72e290]` 就一直是 NULL，
   通道 A 自然走 `0x040e` 那条 else）。

PLAN.md 里「J 是唯一有真实失败风险的一段」这句话，风险到这里基本消失了。

## 150. ★★★★★ `0x0410` / `0x040e` / `0x040f` 三个包的线格式（V0.2 要实现的全部）

| opcode | 方向 | 线格式 | 处理器 |
|---|---|---|---|
| `0x0410 gspToggleUdpClientCommunication` | 服务端 → 客户端 | **int32**（0/1），见 §136 的 `0x5d59de` | `0x408703` |
| `0x040e` | 客户端 → 服务端 | 载荷 = **一个完整的 UdpPacket**（12 字节头 + body，见 §151）| —— |
| `0x040f` | 服务端 → 客户端 | 同上，**原样转发即可** | `0x4086b5` → `0x407869` |

`0x0410` 的处理器 `0x408703` 做两件事：

```text
[session+0x3e4] = flag                                  ; 通道 A 总开关
setsockopt([0x72e30c]+8, IPPROTO_TCP, TCP_NODELAY, &flag, 4)   ; 顺手给游戏服 socket 关 Nagle
```

★ **`[session+0x3e4]` 默认是 0，而且每次退房（`0x406191` 发 `0x0203`）都会被清回 0**，
所以**每次进房 / 每次开局都要重发一次 `0x0410`**。
（`[session+0x3f8]`（UDP 开关）相反，是 GameSession 构造时 `0x405650` 直接写死 1，
服务端管不着 —— 意味着 UDP 那一路我们关不掉，只能让它自己在公网上打水漂，见 §151 为什么无害。）

★ 方向不冲突：客户端**发**的 `0x040f` 是 `gcpEndQuest`（V0.1 §45），
客户端**收**的 `0x040e` 是 `gspRepFirstUserResult`（`0x40741c`）。
gcp / gsp 是两套独立编号，别混。

## 151. ★★★★★ `UdpPacket` 的 12 字节头 + 每座位 `PktQueue` 序列号去重

`UdpPacket`（vft `0x64dcf8`，构造 `0x5bbde5`）和 `RawPacket`（vft `0x64dc68`，
构造 `0x5bba19`）是**两个不同的类**：RawPacket 头 10 字节、opcode 在 `+8`；
UdpPacket 头 **12 字节**：

```text
+0   u8    0xff        魔数，不是 0xff 直接丢（0x4078ab）
+1   i8    发送方座位   有效范围 -1..5（0x4078c9，signed 比较）
+2   i8    目标座位     0xff = 广播；[session+0x3da] != 0 时不校验（0x4078dd）
+3   u8    ?           还没查
+4   u16   会话/局号    必须 == [session+0x3c]（除非 0x409dc6([session+0x18]) 为真）
+6   u16   校验和       0x5bbdc1：从 +0x0c 起、种子 0x17（0x4078f0 对不上就丢）
+8   u16   序列号       ★ 去重/排序用，见下
+10  u16   内层 opcode  < 0x4000 → 0x407c01 正常分发；>= 0x4000 → 0x407918（另一条路，未查）
+12  ...   body
```

**去重**在 `PktQueue::Insert`（`0x54bb8c`，队列在 `[session + 座位*0x24 + 0x2e4]`，六个座位各一条）：

```text
if (seq <  queue.base)        丢弃（太老）
if (queue.slots[seq-base])    丢弃（★ 已经收过这一号 —— 重复投递在这里被吃掉）
否则 new RawPacket 拷一份存进槽位
```

★★ **这条是「双通道同时投递同一个包」安全的唯一依据**：
公网上 UDP 那一路发往对方上报的**内网 IP**（V0.1 §44），基本发不到；
局域网里发得到，于是同一个包会从 UDP 和 `0x040f` 各来一次 —— 被序列号吃掉，
不会双重结算。**所以我们不用（也没法）关掉 UDP 那一路。**

## 152. ★★★★ 原版中继（`0x0310` / `0x0210` / rcp）全貌 —— 查清了，但**决定不实现**

留档，免得以后有人再逆一遍。

**`0x0310 gcpStartTcpRelay`（客户端 → 服务端，8 字节）** —— §144 那两发的真身：

```text
int32  我的座位号     ← 0x409f7d = [[0x72e29c]+0x1cc]
int32  对方座位号
```

发送点 `0x4085a9`，调用点 `0x40591a` 在大厅每帧的 tick 里：
对**每个「不是我」且「有人」的座位**各发一发，每座位节流 10 秒
（`[session+0x3c0 + i*4]` 存上次发的时间，`+0x2710` = 10000 ms）。
所以 §144 看到的「每 10 秒两发」= 那个假房间里另外**两个**座位有人。**不回它没有任何副作用。**

**`0x0210 gspJoinRelay`（服务端 → 客户端，18 字节）** —— 处理器 `0x55431c`：

```text
NetAddress   { int32 ip; u16 port; }        ← 0x4438b9 / 0x4438dc
RelayAuthData{ int32 a; int32 b; int32 c; } ← 0x54c453 / 0x54c47e
```

客户端收到就 `new RelayConnection(0x8a8 字节)` 存进全局 `[0x72e290]`，TCP 连过去。
**`0x0211` = 拆掉这条连接**（`0x55437b`）。

**rcp（RelayConnection 那条 TCP 上的协议，opcode 在 `0x00xx` 段）**：

| opcode | 方向 | 内容 |
|---|---|---|
| `0` | 客户端 → 中继 | `Packet_rcpRegister` = RelayAuthData 三个 int32（连上就发，`0x54bdc2`）|
| `1` | 客户端 → 中继 | `Packet_rcpRepPing`（对方发 ping 时回）|
| `3` | 客户端 → 中继 | **数据**：body = 整个 UdpPacket（`0x55b992`）|
| `0` | 中继 → 客户端 | 数据：剥掉 10 字节头后交给 `0x407869` |
| `1` | 中继 → 客户端 | ping |
| `2` | 中继 → 客户端 | 「报一下身份」→ 客户端回 opcode 0 的 rcpRegister |

连上之后客户端会 `setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)`；
断开时弹客户端自带的公告框（字符串 `0x691588`）并把 `[session+0x3e4]` 清 0。

**为什么不实现**：它和 `0x040e`/`0x040f` 传的**是同一个 UdpPacket**，
效果完全一样，却要多开一个端口、多一套认证、多一条连接的生命周期。
见 D077。

## 153. ★★★ UDP 那一路的细节（我们不用，但要知道它一定会发包）

- `GameSession` 构造时 `new UDPBinder(0x28)` 存进 `[session+0x2bc]`（`0x405112`），
  回调绑的就是 `0x407869`（和 `0x040f` 同一个入口）。
- 随即 `0x5bba92(0x1e6c)` = **bind 到 UDP 端口 7788**，失败弹「…(Bind Fail)」（`0x65ea30`）。
  绑完 `WSAAsyncSelect(sock, hWnd, 0x40a, FD_READ|FD_WRITE|FD_CLOSE)`。
- 发送 `UDPBinder::SendTo`（`0x5bbb32`）→ `sendto`（IAT `0x637458`），
  **自带丢包模拟**：`rand()%100 < [binder+0x24]` 就直接不发。
- 目标地址来自每个座位的网络信息结构（`0x404d42`）：`+0x14/+0x18` 主 IP/端口、
  `+0x28/+0x2c` 备用、再不行遍历 `+0x1c..+0x20` 那个地址数组挨个发。
  这些 IP 就是 V0.1 §44 里客户端登录时上报的**内网 IP**。
- WS2_32 的 IAT 槽（这份镜像）：`0x637438` setsockopt、`0x637448` WSAAsyncSelect、
  `0x63744c` bind、`0x637450` htonl/inet_addr、`0x637454` htons、`0x637458` sendto。

## 154. 逆包时认清 `GameSession` 这个全局

`[0x72e29c]` = `GameSession` 单例（vft `0x65e130`），大厅和战斗**是同一个对象**，
`0x0300`~`0x0311` 和 `0x040e`/`0x040f`/`0x0410`/`0x0417`/`0x0418`/`0x041f`/`0x0420`
都在它的 `OnPacket`（`0x4061e2`）里分发（§142 那张表是它的一部分；
表只覆盖 `0x0300..0x0311`，其余走表后面那串 `sub eax,imm; je` 链）。

常用字段：

| 偏移 | 含义 |
|---|---|
| `+0x1cc` | **我的座位号** |
| `+0x2bc` | UDPBinder |
| `+0x2e4 + 座位*0x24` | 该座位的 `PktQueue`（§151）|
| `+0x3c` | 会话/局号（UdpPacket `+4` 要和它相等）|
| `+0x3c0 + 座位*4` | 上次发 `0x0310` 的时间戳（§152）|
| `+0x3e4` | **通道 A 开关**（`0x0410` 设；退房清 0）|
| `+0x3f8` | 通道 B（UDP）开关，构造时写死 1 |
| `+0x40 + 座位*0x3c` | 座位是否占用（`0x4045f9`）|

## 155. ★★★★★ J.1 的实机验证：整条链路跑通了（会话 06）

**§149~§154 的「⚠ 还没实测」到此解除。** 三段实测，字节都对上了。

### 实测 1 —— 开关一发，真客户端立刻开始发 `0x040e`

真客户端（`testuser1`）待在自己建的房间里，服务端推一发：

```bash
runtime\python\python.exe tools\gs_ctl.py raw 0410 01000000 --user testuser1
```

日志里 **24 毫秒后**就出现第一发 `0x040e`，此后稳定 **~7.8 Hz**（每 128 毫秒一发，
43 字节），一直发到关掉为止。§150 里「`0x0410` 是总开关」是直接观察到的。

### 实测 2 —— 服务端转发，第二个玩家逐字节收到

`tools/fakeclient.py` 现在能 `create`（建房）和 `peer`（发一发 `0x040e`），
于是**不用真客户端**也能跑通两个玩家：

```bash
runtime\python\python.exe tools\fakeclient.py alice pw123 create PeerTest 1 hold 45 leave
runtime\python\python.exe tools\fakeclient.py bob   pw456 join 0 sleep 2 peer 1 sleep 1 peer 2 sleep 2 leave
```

alice 那边收到的（顺序、内容全对）：

```text
0x0301 action=0 座位=1        bob 进房
0x0305 'bob 进入了房间。'
0x0410 开关 = 1               ★ 排在进房四连发**之后**
0x040f 15 字节  序列号=1      ★ 和 bob 发的 15 字节**完全一样**
0x040f 15 字节  序列号=2
0x0301 action=1 座位=1        bob 退房
0x0305 'bob 离开了房间。'
0x0410 开关 = 0               ★ 掉回一个人就关掉
```

### 实测 3 —— 真客户端的**真** `0x040e`，头部逐字段对上 §151

真客户端建房 + 假客户端 bob 加入，服务端把真客户端的第一发原样转给 bob：

```text
0000  ff 00 ff 00 00 00 71 b4 00 00 01 40 00 00 01 00
0010  00 00 00 32 00 7a 01 00 00 00 00 00 54 00 00 07
0020  00 00 00 00 00 22 02 43 01 19 00
magic=0xff 发送方座位=0 目标座位=广播 ?[3]=0
局号=0 校验和=0xb471 序列号=0 内层opcode=0x4001 body=31 字节
```

- `magic` / `发送方座位`（真客户端是房主，座位 0）/ `目标座位=0xff` 全部符合 §151；
- **`目标座位` 实测就是 0xff** —— 印证了「这条通道上不存在单播」，服务端无脑广播是对的。

### ★ 新查明：内层 opcode 分三段（`0x407869` 的两条出口）

| 内层 opcode | 去哪 | 语义 |
|---|---|---|
| `< 0x4000` | `0x407c01` → `PktQueue::Insert`（§151）| **排队 + 按序列号去重**，可靠有序那一类 |
| `0x4000`~`0x7fff` | `0x407918` → `0x407956` | **立刻处理，不排队不去重**，尽力而为那一类 |
| `>= 0x8000` | 丢弃（`0x40791c` 的 `jae`）| —— |

房间里那发 8 Hz 心跳的内层 opcode 是 **`0x4001`**，正好落在中间那段 ——
所以它的**序列号恒为 0** 也就说得通了（那条路根本不看序列号）。

⚠ **还没验到的**：真客户端**收到** `0x040f` 之后的表现。
两个真客户端在同一局里打一场才能看到（⏳ 区第 7 条，需要用户的第二台电脑）。
目前证到的是「真客户端会发、服务端会原样转、第二个连接会收到」。

## 156. ★★★★★ 中继连接的**帧格式和加密与 27799 完全相同**（实现原版中继的最后一块拼图）

`RelayConnection`（vft `0x69152c`）和 `ServerConnection`（vft `0x6916fc`）
**是同一个基类**，虚表逐槽比对：

| 槽 | RelayConnection | ServerConnection | |
|---|---|---|---|
| 0x08~0x14 | `0x5bc415` / `0x5bcd2e` / `0x5bcc8b` / `0x5bca70` | 同 | **完全一样**（基类实现）|
| **0x18 发包** | `0x5bc9ba`（**基类原样**）| `0x553031` | Server 那个只多一句提前 return，其余 `jmp 0x5bc9ba` —— **两者发包路径同一条** |
| 0x1c OnConnected | `0x54bdc2` 发 `rcpRegister` | `0x54d965` 发明文版本号 | 不同 |
| 0x34 OnPacket | `0x54bce1`（rcp 分发）| `0x54e036`（gsp 分发）| 不同 |

**加密对象装在基类构造函数里**（`0x5bc801`）：

```text
[this+0x87c] = 流密码(vft 0x64dd54, 参数 0, 1)   ; 发送方向
[this+0x888] = 流密码(vft 0x64dd54, 参数 5, 3)   ; 接收方向
```

—— 正是 `server/simple.py` 里写死的那两组参数
（`client_to_server() = (0,1)`、`server_to_client() = (5,3)`）。

### ⇒ 实现原版中继时要照抄的三条

1. **同一把 `SimpleCipher`**，两个方向各一份状态，和 27799 一模一样；
2. **没有明文版本号开场白** —— 那是 `ServerConnection::OnConnected` 独有的
   （`0x54d98f` 写 `0x137` = 311）。中继连上来第一件事就是 `rcpRegister`；
3. 帧就是 `RawPacket`，**10 字节头**（`0x5bb9e7` 写的就是它）：

```text
+0  u8   0xff        魔数
+1  u8   ?           这个函数不写它
+2  u16  载荷长度    = 总长 - 10
+4  u16  0           恒清零
+6  u16  标志        基类发包传 0
+8  u16  opcode      RawPacket::SetType（0x5bba0a）写的就是这里
```

★ 注意别和 `UdpPacket` 的 **12 字节头**（§151）搞混，两个类的头**布局不一样**。

# 第五批 —— 里程碑 J.3 原版 TCP 中继（2026-08-11 会话 07 起）

## 157. ★★★★★ 原版中继的**全部**线格式（逐条核对过反汇编，可以直接照着写）

§152 是从类名和调用点推的轮廓，这一条是**逐指令核对**的结果。
两处和 §152 不一样、且是**会咬人**的差别，写在 §158 / §159。

### `0x0210 gspJoinRelay`（服务端 → 客户端，18 字节）

反序列化 `0x54d22f` 依次调两个子对象的 `Deserialize`：

| 子对象 | 在包对象里的偏移 | 反序列化器 | 线格式 |
|---|---|---|---|
| `NetAddress`（vft `0x68c580`）| `+0x04` | `0x4438dc` | `int32 ip`（`0x5d59ff`）+ `u16 port`（`0x5d59f1`）|
| `RelayAuthData`（vft `0x691580`）| `+0x10` | `0x54c47e` | `int32 a` + `int32 b` + `int32 c`（三发 `0x5d59ff`）|

**`ip` 是网络字节序的原始 32 位，`port` 是主机序。** 证据在 `Connect`（`0x5bc50d`）：

```text
[ebp+8]  = ip   -> mov [ebp-0x10], edi        ; sin_addr **原样**写进去，没有 htonl
[ebp+0xc]= port -> htons(0x637454) -> [ebp-0x12]  ; sin_port 才过 htons
```

所以 Python 侧 `socket.inet_aton("127.0.0.1")`（= `7f 00 00 01`）直接当 4 字节写进去
就对，**不要**再翻转；端口写普通的 `u16`（小端 27798 = `96 6c`）。

★ `RelayAuthData` 那三个 int32 **客户端一个字节都不解释** ——
`0x54bc78` 把它们抄进 `[RelayConnection+0x89c/0x8a0/0x8a4]`，
之后 `0x54bdc2`（连上）和 `0x54bd1b`（被要求报身份）原样抄回来发出去。
**所以它的语义完全由服务端定**（我们拿它当「房间号 / 座位 / 一次性随机数」）。

### `0x0211`（服务端 → 客户端，载荷被无视）

处理器 `0x55437b`：`[0x72e290]` 非空就调它的 vft[0]（`0x54bcb3`，scalar deleting dtor）
并把全局清零。**走的不是 `OnDisconnected`**，所以没有 §158 那个副作用 ——
这是**唯一安全的拆连接方式**。

### rcp（中继那条 TCP 上的协议）—— 帧和 27799 完全相同

分发器 `RelayConnection::OnPacket` = `0x54bce1`，
`opcode = word[包缓冲 + 8]`（就是 `RawPacket` 头里的那个），三条分支：

| opcode | 方向 | 内容 | 反汇编 |
|---|---|---|---|
| `0` | **中继 → 客户端** | **数据**：载荷 = 一个完整的 `UdpPacket` | `0x54bd57`：`0x55b9bc` 剥掉 10 字节头 → `0x407869(0,0,0,&udp)` |
| `1` | **中继 → 客户端** | **ping**，载荷 0 字节 | `0x54bdab` → 回 opcode 1 |
| `2` | **中继 → 客户端** | **「报一下身份」**，载荷 0 字节 | `0x54bd1b` → 回 opcode 0 |
| 其它 | —— | 返回 0（未处理）| `0x54bcfa: xor al,al` |

客户端发出去的三种：

| opcode | 载荷 | 何时 | 反汇编 |
|---|---|---|---|
| `0` `rcpRegister` | **12 字节 = 三个 int32**（就是 `0x0210` 里给它的 `RelayAuthData`）| 连上就发；被 opcode 2 要求时再发 | `0x54befe`：`SetType(0)` + `0x404f59` → `0x54c453` 写三个 i32 |
| `1` `rcpRepPing` | **0 字节**（`Serialize` = `0x470807` = `ret 4`）| 收到 opcode 1 时 | `0x54bf4e`：`SetType(1)` |
| `3` 数据 | 一个完整的 `UdpPacket` | 有同步数据要发且中继已连上 | `0x55b992`：`SetType(3)` + 整个 UdpPacket 缓冲 |

`RawPacket` 头由 `0x5bb9e7` 写死（**和 27799 逐字节相同**，§156）：
`+0 = 0xff`、`+2 = 总长-10`、`+4 = 0`、`+6 = 标志(基类传 0)`、`+8 = opcode`。
加密同样是 `SimpleCipher`，两个方向 `(0,1)` / `(5,3)`，**没有明文版本号开场白**。

### 通道 A 的选路（`0x408619`，再确认一次）

```text
if ([GameSession+0x3e4] == 0) return;                  ; ★ 0x0410 的开关
if ([0x72e290] != 0 && [[0x72e290]+0x894] != 0)        ; 中继对象在且**已连上**
     0x54beb6(packet)   ; rcp opcode 3
else 0x5594be(...)      ; 0x040e -> 游戏服
```

★★ **走中继也照样要先发 `0x0410`。** 那个开关是两条路共同的总闸，
不发的话中继连上了也一个包都不会发。

## 158. ★★★★★ 中继连接一断，客户端**自己退出房间** —— 这是中继实现的最大风险

`RelayConnection::OnDisconnected` = `0x54be26`（vft `0x69152c` 的 `+0x20` 槽）：

```text
[0x72e290] = 0
[this+0x894] = 0
if ([0x72e29c] != 0) 0x406191(GameSession)   ; ★★ 见下
if ([0x72e2a4] != 0) 弹公告框(字符串 0x691588)
0x5bc41a(this)                                ; 关 socket
```

而 `0x406191` **不是**「清个标志」那么轻——它是**退出房间**：

```text
0x406191:  RawPacket::SetType(0x203)          ; gcpLeaveSession
           [0x72e30c]->vft[0x18](packet)      ; 发给游戏服
           [GameSession+0x3e4] = 0            ; 顺手关掉通道 A 的开关
```

⇒ **中继这条 TCP 只要断一次，玩家就被自己的客户端踢出房间，
同时通道 A 的开关也被清 0（连 `0x040e` 回退路径都一起没了）。**

这条把中继从「锦上添花」变成「**断了比没有更糟**」。实现上的硬要求：

1. 中继服务端**绝不能主动关**一条已注册的连接，除非玩家本来就要离开房间；
2. 要拆连接一律用 **`0x0211`**（走 dtor，不触发 `OnDisconnected`，§157）；
3. 连不上也算数：`0x5bc50d` 里 `WSAAsyncSelect(..., 0x33)` 带着 `FD_CONNECT|FD_CLOSE`，
   **地址填错 → 连接失败 → 大概率同样走到 `OnDisconnected`** → 玩家被踢出房间。
   所以 `0x0210` 里那个地址**必须是客户端一定连得到的**，不能靠猜。

★ D078 的「反悔条件」说的就是这个：真出问题就默认不回 `0x0210`，
客户端自然走 `0x040e` 回退路径。

## 159. ★★★★★ `0x0210` **一条连接只能回一次**，回第二次是定时炸弹

`0x55431c` 收到 `0x0210` 的动作是**无条件**的：

```text
0x5d5c92(0x8a8)          ; new RelayConnection
0x54bc78(...)            ; ctor，抄走 RelayAuthData
[0x72e290] 被 Connect 里的 this 覆盖   ; ★ 旧对象既不释放也不关 socket
```

也就是说再回一发 `0x0210`，旧的 `RelayConnection` 就变成**孤儿**：对象还在、
socket 还注册在窗口消息泵上。等它哪天收到 `FD_CLOSE`，
`OnDisconnected`（§158）照样触发 —— 把**新**连接的全局指针清成 0，
再发一发 `0x0203` 把玩家踢出房间。

⇒ 服务端必须记住「这条游戏连接已经回过 `0x0210` 了」，
在**明确拆掉之前**（发过 `0x0211`，或玩家已经离开房间）绝不重发。

而客户端那边 `0x0310` 是**每个别人坐着的座位每 10 秒一发**（§152），
也就是说重复请求是常态，**去重的责任 100% 在服务端**。

## 160. ★★★★★ 原版中继的实机验证：**真客户端真的连上来了，而且真的改走中继**

会话 07，一台电脑（真客户端 `testuser1` 建房 + 假客户端 `bob` 加入）。
§157~§159 的「照着写就行」到此从推论变成实测。

### 实测 1 —— 两个假客户端：整条 rcp 链路

```bash
runtime\python\python.exe tools\fakeclient.py alice pw123 create RelayTest 1 ^
    sleep 2 waitrelay 20 hold 25 leave
runtime\python\python.exe tools\fakeclient.py bob pw456 join 0 sleep 1 ^
    waitrelay 20 sleep 1 rpeer 5 sleep 1 rpeer 6 sleep 2 leave
```

alice 那边收到的（`tools/fakeclient.py` 现在会像真客户端一样自己去连中继）：

```text
← 收 0x0210 18 字节
[中继] 已连上 127.0.0.1:27798，发 rcpRegister(0, 0, 1)
[中继] ← 收 opcode 1（ping），回一发
[中继] ← 收 opcode 0（数据）15 字节
    发送方座位=1 序列号=5 内层opcode=0x0102        ★ 和 bob 发的**逐字节相同**
[中继] ← 收 opcode 0（数据）15 字节  序列号=6
```

### 实测 2 —— **真客户端**：`0x0310` → `0x0210` → 真的连过来 → 真的改走中继

```text
18:00:22.123 #3 ★ 游戏包 opcode=0x0310 (gcpStartTcpRelay) 载荷 8 字节
18:00:22.124 #3 ← 回 0x0210 gspJoinRelay 127.0.0.1:27798 认证=(1, 0, 4)
18:00:22.129 [relay] + 中继连接 ::ffff:127.0.0.1:9808        ← 5 毫秒后就连过来了
18:00:22.131 [relay] [testuser1@…] ✓ 注册成功（签发时房间 #1 座位 0）
```

**「真的改走中继」的直接证据**：整条连接上 `0x040e` **只出现过一次**
（18:00:12，中继还没建起来的那一刻），之后真客户端的 43 字节同步包
**全部从中继出去** —— 中继连接收工时的统计：

```text
- 中继连接结束 [testuser1@…] 在线 74.0 秒
  （帧 收 241 / 发 5；数据 收 235 / 发 0；ping 5 回 5）
```

也就是 `0x408619` 确实选了中继那条分支（§157 末尾的选路），
而且 **真客户端会回我们的 ping**（发 5 回 5）。
bob 那一侧同期收到 313 份数据，一份都没走 `0x040f` 回退。

### 实测 3 —— §159 的去重：重复的 `0x0310` **没有**换来第二发 `0x0210`

真客户端每 10 秒发一发 `0x0310`（18:00:22 / 18:00:32 / 18:00:42 …），
服务端只在**第一发**时回了 `0x0210`，之后只确认开关。

### 实测 4 —— `0x0211` 是安全的拆法（§157 的核心论断）

```bash
runtime\python\python.exe tools\gs_ctl.py raw 0211 --user testuser1
```

中继连接当场结束，**而客户端没有发 `0x0203`、没有退出房间、没有弹公告框**
—— 截图里它还好端端地待在「2 号房间」当房主。
证明 `0x55437b` 走的是析构（`0x54bcb3`）而不是 `OnDisconnected`（§158），
所以要拆连接只能用这个包，绝不能去关 socket。

⚠ **还没验到的**：§158 那条「中继一断客户端就退房」本身没有故意去触发
（那要真的掐断一条已注册的连接）。它是从 `0x54be26` 的反汇编直接读出来的，
而实测 4 恰好从反面印证了「走析构就不会有这个副作用」。

---

# 第八批发现 —— 战斗逻辑广播的可行性（2026-08-11 会话 08）

## 161. ★★★★★ 战斗内那几个包**广播出去是安全的**：客户端全部按跨机器一致的 id 找目标

PROGRESS 给 J.3 留了一条硬要求：「现有实现全是原样回显给发包的那一个人，
改成广播之前要先确认客户端**对别人的座位**也认这些包 …… 别照着『应该能行』
直接改」。这一节就是那次核对的结果 —— **逐个包读到了反汇编**。

结论先写：**六个包全部可以广播，一个都不会崩**。

### ★★ 玩家角色的对象句柄 = **座位 × 100000 + 100001**（`0x405f02`）

这是整节最关键的一句。`LobbyStage::CreateSeatCharacter`（`0x405e30` 起）：

```asm
00405edd  call 0x409f7d                 ; eax = 我的座位（[[0x72e29c]+0x1cc]）
00405ee2  cmp  ebx, eax                 ; ebx = 要建的那个座位
00405ee4  jne  0x405f17                 ;   是我 -> new(0x7c4)；不是我 -> new(0x744)
00405ee6  push 0x7c4 / call 0x5d5c92
00405f02  imul ecx, ebx, 0x186a0        ; 座位 × 100000
00405f08  add  ecx, 0x186a1             ; + 100001        ★ 这就是句柄
00405f0f  push eax                      ; 对象
00405f10  call 0x514311                 ; 建角色，句柄进 [char+0xd0]
```

所以座位 0 的角色句柄恒为 **100001 = 0x186a1**、座位 1 恒为 200001 ——
和 V0.1 §108 实测日志里那个 `句柄=0x000186a1` 完全对上。
**六台机器上算出来的是同一个数**，所以带句柄的包广播出去，别人查得到。

顺带查明世界句柄的分段规则（`0x473e65`）：

```asm
sub eax, 0x186a0 ; js -> return 20        ; 句柄 < 100000 = 类别 20
cdq / idiv 100000 / add eax, 10           ; 否则类别 = 10 + (句柄-100000)/100000
```

即句柄空间按 100000 一段切成 30 段（`0x47352a` 那个 `cmp ..., 0x2f4d62` 的初始化
循环正好 30 轮），**每段一个类别**：座位 0~5 的角色占前六段，怪物在第 9 段
（V0.1 实测的 `0x0010c8fb` = 1099003 落在 1000000~1099999）。
我们给掉落物用的 `ITEM_HANDLE_BASE = 0x40000000` 远在 30 段之外，撞不上。

### 逐包核对表

| 包 | 读侧 | 靠什么找目标 | 找不到会怎样 |
|---|---|---|---|
| `0x0406` 死亡广播 | `0x4938d2` | `World::Find(句柄)`（`0x474225` = 查 `[world+0xb4]` 那张 map） | `0x493914 test esi,esi / je` **整包丢掉**，不崩 |
| `0x0419` 重生 | `0x553ecc` → `GameContext::vf_d4` = `0x4931c2` | `0x404ff6(座位)` = `[GameSession + 座位*4 + 0x1d0]` | `0x4931e5 je` 直接 return |
| `0x0404` 掉落物落地 | `0x551a11` | 句柄是**服务端分配**的，客户端拿它建对象 | —— |
| `0x0405` 拾取放行 | `0x551d35` | **两把钥匙**：`0x404ff6(座位)` 取角色 + `World::Find(句柄)` 取物件，两个都非空才调 `item->vft[0xd4](角色)` | `0x551d7c/0x551d80` 两个 `je`，什么都不做 |
| `0x0415` 分数 | `0x4a3efe` | `[GameContextQuest + 座位*4 + 0x3b8]` | 座位有效性先过 `0x4045f9` |
| `0x0309` 结算 | `0x55210d` | 全程按 `pkt+0x04` 的座位号索引 | `0x552156 je` 丢包 |

### 客户端**只在事件属于自己时**才上报，所以基本不需要去重

`GameContext::CreateItem`（`0x4939c0`，`push 0x406` 在 `0x493a57`）有 6 个调用点，
玩家掉落那两条都带着「是不是我」的判据：

```asm
00508cbb  call 0x409f7d              ; 我的座位
00508cc0  cmp  [ebx+0x2ac], eax      ; 这个角色的座位 == 我？
00508cc6  jne  0x508ce9              ;   不是 -> 不发
...
004faa94  call 0x409f7d              ; 同一套（前面还有一发 6% 的 rand）
004faa99  cmp  [ebp+8], eax
```

`0x0413` 重生请求同理（V0.1 §108 的 `0x4fe70e`：
`[char+0x2ac] != [LobbyStage+0x1cc]` 就当帧不重生）。

**唯一会重复上报的是怪物的死亡**：怪是每台机器各自模拟的，同一只怪可能被
两个客户端同时判死并各发一发 `0x0408`。广播两遍 = 战绩表（`0x48c942`）
多记一次死亡，所以**去重责任在服务端**。

★ 去重的键必须是 **(句柄, 客户端报的死亡次数)**，不能只用句柄 ——
角色会死很多次，只按句柄去重第二次死就被吃掉了。而重复上报的两发**死亡
次数一定相同**：那一格是 `[char+0x600]`，全镜像里只有 `0x0406` 的处理器
（`Character::vf_c4` = `0x4ff1fd`）会写它，所以每台机器上的值都是服务端
上一次发下去的那个。真正的第二次死亡带着更大的数上来，键自然不同。

### `0x0411 gspEndGame` 可以每座位一份，但**没必要**

处理器 `0x551804` 分三段：

```asm
005518af  call 0x4a4096            ; 13 个 dword -> [GameContextQuest + 座位*0x34 + 0x3ec]
005518b4  call 0x409f7d            ; 我的座位
005518b9  cmp  ebx, eax / jne      ; ★ 只有「包里的座位 == 我」才更新下面四个全局
005518bd..005518d9                 ;   [0x72e330] += 值4 / [0x72e33c] / [0x72e340] / [0x72e344]
005518de  call 0x4913fc            ; 弹结算界面（`cmp byte [esi+4], bl / jne` 有重入保护）
005518e9  call 0x4087f0            ; ★ 每次都会**再发一发 0x0505 遥测**
```

也就是说多发几份不会串账（钱只加自己那一份），但每一份都会换来一发
`0x0505`，而 `0x4087f0` 重复调用是否安全没有 RE 依据。
**所以 V0.2 的选择是「`0x0309` 每座位一份、`0x0411` 每人只发自己那份」**
（D082）。代价：队友那一行的「分数 / 生命」（`[ctx+0x3ec+座位*0x34]`，
只有 `0x0411` 会写）显示 0。

### 结算的「完成 / 胜负」是 `0x0309` 尾部数组的**三个档**

§112 只查到「== 1 → 完成」。这次把另外两处读点补齐了：

```asm
004a4ba9  cmp eax, 1 / jne -> 「未完成」/「FAILED」    ; 结算界面标签（vf_10c(座位)）
0055223f  setge al                                    ; ★ >= 0 -> 胜利 BGM
00552242  cmp [LobbyStage+0x1c], 2                    ; 2 = 闯关
    闯关：BGM-StageClear(0x691d38) / BGM-Failed(0x672434)
    对战：BGM-Victory(0x691d20)    / BGM-Lose(0x691d0c)
004a4b4c  同一个 `== 2` 判据决定标签用「完成/未完成」还是 `CLEAR`/`FAILED`
```

于是：

| 尾部数组的值 | 标签 | BGM |
|---|---|---|
| `1` | 完成 / CLEAR | 胜利曲 |
| `0` | 未完成 / FAILED | **胜利曲**（`0 >= 0`）|
| `-1` | 未完成 / FAILED | 失败曲 |

⚠ **0 和 -1 是两个不同的档，别合并**：V0.1 单机没通关时发的就是 0，
改成 -1 会让单机的失败也开始放失败曲 —— 那是没验过的行为变更。

★ `0x0309` 的**值 4**（`pkt+0x18`）非 0 且座位 == 我时，`0x552170` 会弹一个
自绘公告框（韩文串 `0x691d58`）。我们一直填 0，保持。

### `0x0311 gspRepQuestRecordInPvp` 的六项是**六个座位**，不是六种游戏类型

处理器 `0x408a1c` 就是一个 `for (i = 0; i < 6; i++)`：座位有人才
`[LobbyStage + i*4 + 0x1ac] = records[i]`，然后 `0x4089fa` 把它交给座位对象的
`0x50b6a1`。全镜像里这个字段只有这一处写、一处读，是纯表现层
（多半是头顶的段位标记）。**具体是什么数仍未查明**，继续回全 0。
`gameserver.build_rep_quest_record_in_pvp` 的旧注释（「六种游戏类型」）已勘误。

## 162. ★★★★ J.3 战斗逻辑的实机验证（会话 08，两个假客户端 + 真服务端）

`tools/fakeclient.py` 加了整套战斗命令（`ready` / `loaded` / `die` / `respawn` /
`drop` / `pickup` / `score` / `cleared` / `nextmap` / `maploaded` / `endquest` /
`resultdone`），于是**一台电脑上就能跑完整的一局双人关卡**。
下面每一条都是真 TCP + 真票据 + 真服务端跑出来的。

| 验了什么 | 结果 |
|---|---|
| 掉落物广播 + **房间级**句柄 | bob 发 `0x0406` → **两个人**都收到 `0x0404 句柄=0x40000000` |
| 死亡广播 | alice 发 `0x0408` → 两人都收到 `0x0406 座位0 句柄=0x000186a1 死亡次数 -> 1` |
| ★ **拾取仲裁** | bob 先发 `0x0407` → 两人都收到 `0x0405 座位=1`；alice 随后发同一句柄，服务端记 `拾取被拒 … 已经被座位 1 捡走了`，**一个包都没回** |
| 分数广播 | 两人都收到 `0x0415 座位=0 -> 40` 和 `0x0415 座位=1 -> 25` |
| ★ **换图等所有人** | alice 先报 `0x0412` → 服务端 `还在等 1 人，先不放行`；bob 报到后**两人同时**收到 `0x0418` |
| ★ **结算每座位一份** | 两人各收到 **两份 `0x0309`（座位 0、座位 1）**，尾部数组都是 `[1,1,0,0,0,0]`；`0x0411` 各收到**自己那一份**（alice seat=0 / bob seat=1）|
| ★ **一局只结算一次** | alice 的第二发 `0x040f` → `本局已经结算过了；忽略`，零回包 |
| 各按各的分入账 | `alice +40 经验/金币`、`bob +25`，`accounts.json` 逐字段对上 |
| ★ **客人也解锁难度** | 两个账号的 `quest_difficulty` 都变成 `{"3": 1}`（`current_quest()` 改成先读大厅那一份之前，非房主是解锁不了的）|
| 房间状态 | 进 stage 7 时 `房间 #3 -> 游戏中`，结算看完 `-> 待机中` |
| ★ **第二局能再开** | 回房间后房主再按两次「开始」，两人都收到 `0x0401` → `0x0400` |

⚠ **还没验的**：这一切都是假客户端收到的**字节**。
「真客户端收到之后会不会照做」（别人的角色真的倒下 / 掉落物真的出现在两边 /
结算界面真的显示两行）**必须两台真机在同一局里**才能证，见 PROGRESS 的 ⏳ 区第 7 条。

---

## 163. ★★★ 打包（里程碑 K）踩到的硬事实

会话 09 重写打包脚本时查明的，都实测过（标了「未实测」的那条除外）。

### 163.1 上一版打包脚本打出来的客户端包**起不了服务端**

`tools\build-portable.ps1`（V0.1 留下的）用**手写白名单**列 `server\` 要拷哪些
文件，只有 `authserver / gameserver / account_store / protocol / simple / snow`
六个。V0.2 新增的 **`app.py`、`config.py`、`relay.py`、`relayserver.py`、
`lobby.py`、`tickets.py`、`eventlog.py`、`netlisten.py`、`web\`** 一个都没进包，
根目录的 `server.config` 也没有。

而 `tools\launch.ps1` 启的正是 `server\app.py` —— 也就是说
**这个包在任何一台不是开发机的电脑上都跑不起来**。
本机看不出来的原因：本机永远能从项目目录直接跑。

→ 修法见 D086（反向排除）+ D087（冒烟自检）。

### 163.2 `app.py` 的四个端口全部可以命令行覆盖 —— 这是冒烟自检的前提

`--auth-port / --game-port / --relay-port / --web-port`，再配上
`--accounts <临时路径>`（`AccountStore.__init__` 会自己把文件建出来）和
`--no-online-log`，就能**在开发服务端正在跑的情况下**把包里的服务端
另起一份验一遍，且不往包里写任何文件。

实测：自检用 47711 / 27899 / 27898 / 27910 四个口，和 47611 / 27799 / 27798 /
27810 完全不打架。

### 163.3 ★ Git for Windows 的 `tar` 会把 `D:\...` 当成远程主机

`Get-Command tar` 在装了 Git 的机器上（或从 Bash 里调起来时）可能拿到
MSYS 的 `/usr/bin/tar`，它把 `D:\work\...` 解释成「主机 D 上的路径」：

```text
tar (child): Cannot connect to D: resolve failed
tar: Child returned status 128
```

必须**写死** `%SystemRoot%\System32\tar.exe`（Windows 10 1803+ 自带
bsdtar 3.5.2，本机 19045 有）。同类陷阱：`find`、`sort` 也有 MSYS 版。

### 163.4 `.bat` 的真正约束是「纯 ASCII」，**不是「不许 chcp 65001」**

`start.bat` / `stop.bat` / `start-debug.bat` 三个都带着 `chcp 65001 >nul`
且工作正常（§135 / D074 说的是**文件里的多字节字符**会让 cmd 的字节偏移漂移）。
所以新的 `tools\build.bat` 也加了 `chcp 65001` —— 加了之后管道里的中文
才是 UTF-8，不加的话输出被按 CP936 编码，在 UTF-8 终端里全是乱码。
**文件本身仍然必须纯 ASCII**（打包脚本里有 `Test-AsciiOnly` 在每次打包时检查）。

### 163.5 `logs\server.out` 是 UTF-8，PowerShell 读它要显式 `-Encoding UTF8`

`app.py` 开头把 stdout/stderr `reconfigure(encoding="utf-8")` 了，所以落盘的是
UTF-8。Windows PowerShell 5.1 的 `Get-Content` 默认按系统 ANSI（中文机器 =
CP936）读，会得到「璐﹀彿瀛樻。」这类乱码 —— **文件本身没问题**。
记事本 / VSCode 打开正常。让测试的人把日志发回来时不用担心这一条。

### 163.6 体积和耗时（本机 RTX3070 / SSD 实测）

| 产物 | 目录 | 压缩后 | 工具 |
|---|---|---|---|
| 客户端包 | 391.4 MiB | 365.4 MiB（zip） | 7-Zip `-mx=5`，约 1 分钟 |
| 服务端包 | 22.2 MiB | 11.2 MiB（zip）/ 11.4 MiB（tar.gz） | 7-Zip / bsdtar，秒级 |

客户端包压不动是因为 `game_patched\Pack\*.pkn` 本来就是压缩过的。
`Compress-Archive` 压同样的东西要好几分钟，所以打包脚本优先找 7-Zip。

### 163.7 Linux 独立 Python 的包里**真的有符号链接和可执行位**

`tar -tvzf` 列 python-build-standalone 的 `install_only_stripped` 包，逐条实证：

```text
lrwxrwxrwx  python/bin/python3       -> python3.14
lrwxrwxrwx  python/bin/python        -> python3.14
lrwxrwxrwx  python/bin/pydoc3        -> pydoc3.14
-rwxrwxr-x  python/bin/pip3.14
```

**符号链接和可执行位都在**，而 Windows 上解开这两样都保不住。
（严格说：「保不住」这一步没有真机验证 —— 手上没有 Linux 环境去跑解出来的东西；
但「链接和权限位确实存在」是实测的。）
所以服务端包里放的是**没解开的 `.tar.gz`**，由 `tools/serverctl.sh`
第一次启动时在 Linux 上自己解（D088）。

---

## 164. ★★★★ `Invoke-WebRequest -UseBasicParsing` 的 `.Content` 对二进制响应是 `Byte[]`，不是字符串

会话 09 实测。**这个坑会伪装成「上游少了文件」**，很难往回猜。

### 现象

`build-server-package.ps1 -LinuxRuntime download` 下完 35 MiB 的运行时后报：

```text
[失败] SHA256SUMS 里没有 cpython-3.15.0rc1+...-install_only_stripped.tar.gz 这一条
```

看起来像上游漏发了校验和。**实际上那一条一直都在。**

### 根因

GitHub 的 release 资产以 `Content-Type: application/octet-stream` 下发，
而 Windows PowerShell 5.1 的 `Invoke-WebRequest -UseBasicParsing` 在这种
Content-Type 下把 `.Content` 给成 **`System.Byte[]`**（不是 `String`）。

于是这一句：

```powershell
foreach ($line in ($sumText -split "`n")) { ... }
```

是在对**字节数组**做 `-split`：PowerShell 先把每个 byte 各自转成字符串，
得到 **122082 个单字节字符串**（正好等于文件的字节数 —— 这就是识别这个坑的
特征：「行数」和「字节数」一模一样），一行完整的文件名当然永远匹配不上。

```text
$resp.Content.GetType()  ->  System.Byte[]
($resp.Content -split "`n").Count   ->  122082   # = 文件字节数
([Text.Encoding]::UTF8.GetString($resp.Content) -split "`n").Count  ->  853  # 真行数
```

### 做法

取文本一律走 `build-common.ps1` 的 `Get-WebText`，它按类型分流：

```powershell
if ($resp.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($resp.Content) }
```

JSON 接口（`api.github.com`）返回 `application/json`，`.Content` 本来就是字符串，
但也一并走这个函数 —— 免得以后有人复制这段代码去取别的东西。

### 同一次暴露出来的另外两条

1. **上游一个 release 里同时挂着 3.10 ~ 3.15，其中 3.15 是 `0rc1`（预发布）。**
   按名字倒序排会挑中 rc，不能拿去开服。现在按「`<系列>.<纯数字>`」匹配，
   `0rc1+` 过不了 `\d+\+`，**预发布天然被挡掉**；默认系列 `3.14`，
   和包里的 Windows 运行时（CPython 3.14.3）同一个大版本。
2. **校验失败的那次把 35 MiB 的文件留在了 `tools\_dl\` 里**（`throw` 排在
   `Remove-Item` 前面），下一次 `-LinuxRuntime auto` 会把这份**没验过**的东西
   直接打进发布包。现在：任何校验失败都删文件；校验通过则在旁边写一份
   `<文件>.sha256`，`auto` 模式**只认带旁证且哈希对得上的缓存**，
   否则打印「忽略未校验的缓存文件」当没有。

### `Sort-Object { $Matches[1] }` 也是错的（顺带修掉）

`$Matches` 是上一次 `-match` 留下的。`Sort-Object` 在整条管道过滤完之后才跑，
那时 `$Matches` 只剩最后一个元素的结果，所有元素会拿到同一个排序键。
排序脚本块里必须**自己再 match 一次**。

---

## 165. ★★★ `.gitignore` **不支持行尾注释**，写了规则就废

会话 09 实测。写 `.gitignore` 时给规则加了中文说明：

```gitignore
/game_patched/Dump/                 # 崩溃转储，一个 32 MB
/game_patched/Debug/                # 客户端自己的调试日志
/game_patched/BigShot.rpt           # 崩溃报告
/game_patched/GameGuard/*.erl       # GameGuard 的运行日志
/game_patched/UserConfig.ini        # 本机设置
```

**五条全部不生效**。gitignore 只认**整行注释**（`#` 在行首），
行尾的 `#` 不是注释起始符 —— 整行连同后面那串中文一起被当成 pattern，
自然匹配不上任何真实路径。

★ **最阴的地方**：`Dump/` 那条**看起来是生效的** —— 因为文件是 `*.mdmp`，
被另一条通用规则兜住了。只验一条会得出「规则没问题」的错误结论。

### 怎么验

不要靠读，造一个镜像仓库用 `git check-ignore` 挨个问：

```bash
mkdir /tmp/t && cd /tmp/t && git init -q .
cp <项目>/.gitignore .
# 按真实相对路径造一批空文件
mkdir -p game_patched/Dump && : > game_patched/Dump/x.mdmp
git check-ignore -q game_patched/Debug/2026-08-11.txt && echo 忽略 || echo 会提交
```

`git check-ignore -v <路径>` 还会打出是**哪一行**规则命中的，
排「为什么这个文件没被忽略」时比读文件快得多。

### 顺带两条

- 目录被忽略之后，git **不会再走进去**，所以 `!` 例外规则**救不回**
  被忽略目录里的文件。要留例外就别忽略它的父目录。
- ★ **「每次运行都会变」不是「可以不提交」的判据** —— 真正的判据是
  「缺了它构建产物还对不对」。本次差点栽在 `game_patched/UserConfig.ini` 上：
  它每次退出都被改写，看着就是本机杂项，其实是 **V0.1 §49 的修复本身**
  （`FullScreen=1` + `ColorDepth=1`）。客户端登录成功那一刻才写出它，
  所以新 clone 打出来的包里没有它 -> 玩家用内置默认值「全屏 + 16 位色」->
  D3D 模式枚举下溢 -> **开局卡死约 40 分钟，且没有任何报错**。
  `build-portable.ps1` 现在有一道闸：文件不在、或 `ColorDepth≠1` 就中止打包
  （三种情况都实测过）。
- `.gitignore` 里的非 ASCII 路径（本项目有 `原版安装包/`）是可以用的，
  前提是文件本身存成 **UTF-8 无 BOM** —— 带 BOM 的话第一条规则会被 BOM 污染。

## 165. ★★★★ 「变更队伍」和「游戏准备」——`SessionSlot` 的两个字段终于有名字了

用户 2026-08-12 实机报的两条缺陷，根因都在 `SessionSlot`（`build_session_slot`）
里两个一直填 0 的「未知字段」上：

| 线偏移 | 旧名 | **真名** | 宽度 |
|---|---|---|---|
| `+0x08` | `unknown_u8` | **队伍** | u8（`0x5d5942` 读 1 字节）|
| `+0x2e` | `unknown_2e` | **准备好了没有** | int32（`0x5d5956`）|

座位结构里的位置：`LobbyStage + 座位*0x3c + 0x40` 是座位起点，
所以队伍 = `[LobbyStage + 座位*0x3c + 0x48]`（取值器 **`0x40462c`**，
座位没占用时返回 -1），准备 = `[座位 + 0x2e]`。

### 队伍（`+0x08`）都被谁读

| 地址 | 干什么 | 前置条件 |
|---|---|---|
| `0x405e5d` | 房间里角色模型站哪边（交给 `0x473cb2` 当分组号）| `0x409df1(描述符)==1` |
| `0x4fbd5b` | 战斗里角色名牌的颜色（同队白、敌队红，实机截图确认）| 同上 |
| `0x4fedfc` / `0x4ffec3` | ★ **友军伤害判定**：两边队伍号**相同就直接不结算伤害** | ★ **无条件，不分模式** |
| `0x468495` | 组队战开局前的「两队人数必须相等」检查 | `0x409df1==1` |
| `0x479684` / `0x4796b8` | 数「我这队几个人」| `0x409df1==1` |

★★ 最后那条是隐形炸弹：**服务端一直给所有人发 0**，
于是 `0x4fedfc` 里任意两人的队伍号恒等 —— **对战里谁都打不动谁**。
这条在用户报的两个现象之外，是顺带修掉的。

`0x409df1(描述符)`：type==5 恒返回 1；type==1 返回 `arguments[0]`；其余返回 -1。
所以「组队战」= 房间描述符 `type==1 且 arguments[0]==1`。

### 队伍默认值：**按座位号奇偶交替**，客户端自己就是这么填的

`0x468952` 起是客户端填「Dummy」测试座位的代码：

```asm
mov  eax, [ebp+8]        ; 座位号
push 2 ; cdq ; pop ecx ; idiv ecx
mov  word [edi+0x10], bx ; 等级
mov  byte [edi+0x2e], 0  ; ★ 准备 = 0
inc  dl
mov  byte [edi+8], dl    ; ★ 队伍 = 座位号 % 2 + 1
```

取值只有 **1 和 2**；客户端的「变更队伍」按钮就是在这两个值之间翻
（`0x469f95` / `0x46deaa`：`cmp byte[slot+8],1 / sete al / inc al`）。

⚠ 组队战的开局前检查 `0x468495` 数 1 队和 2 队的人数，**不等就回错误码 3**
→ 房主按不动「开始」，客户端每隔一会儿还弹「两组人数不相同。请调整人数。」
（`0x467577`，只在**偶数人**时弹）。所以 3 人 / 5 人的组队战本来就开不起来 ——
这是原版行为，不是缺陷。队伍号 0 或 >2 的座位**两边都不算**。

### 「变更队伍」和「换角色」是**同一个包**

两条路都发**客户端方向的 `0x0301`**（座位号 + 一整个 `SessionSlot`），
而且都是**改一份座位副本就发出去、自己一动不动**，等服务端广播回来才生效：

| 发送点 | 干了什么 |
|---|---|
| `0x469f95`（座位面板）/ `0x46deaa`（「팀 바꾸기」按钮 `0x46d1db`）| 复制座位（`0x5d5c92(0x3c)` + `0x405357`），把 `+0x08` 在 1/2 之间翻 |
| `0x4692d0` / `0x467050` 等 | 改角色 id（`+0x0c`）|

所以服务端**只能靠「哪个字段变了」区分**：角色 id 没变而队伍变了 = 变更队伍。

★ 回错 action 的后果就是用户报的第 1 条：服务端一律回 **action 4**，
而 action 4（`0x406520`）会用 `seat+0x0c` 查角色名并播
`'%s님이 %s 캐릭터로 선택되었습니다.'` —— 于是点「变更队伍」冒出一条
**和换角色一模一样的韩文**，队伍还没变（服务端根本没存）。
正确的是 **action 3**（`0x406628`）：把包里的座位数据灌进去、重建模型、
刷房间 UI，**一个字都不播**。

★ 非房主只能改自己那一格，房主谁都能改 —— 客户端 `0x469f4f` 自己就这么判
（`[LobbyStage+0x1cc]`(我的座位) `== [+0x34]`(房主座位) 才放行任意座位）。

### 「游戏准备」= 客户端方向的 `0x030e`，载荷只有一个 int32

序列化 `0x558e78`（`push 0x40e`… 不，是 `push 0x30e`）只写一个字节零扩展成
int32 的值，**不带座位号**：

```asm
0x468e0d  call 0x404d42            ; 我的座位指针
0x468e12  cmp byte [eax+0x2e], 0
0x468e16  sete cl
0x468e19  mov byte [eax+0x2e], cl  ; ★ 客户端**自己先改**
0x468e1c  mov byte [ebp-0x10], cl
0x468e33  call 0x558e78            ; 发 0x030e(新值)
```

★★ **客户端自己先改再发**，所以按的人立刻看得见自己那行「준비중」——
用户报的第 2 条「准备状态只有自己能看到」正是这个：房里其他人只能靠服务端
广播，而服务端**根本不认识 `0x030e`**。

准备状态都被谁读：

| 地址 | 干什么 |
|---|---|
| `0x46c330` | 玩家列表里名字旁边的「준비중 / 准备中」标签 |
| `0x4696cd` | ★ 房主能不能按「开始」：数已准备的人，**房主那一格无条件算已准备**（`0x4696f8`）|
| `0x46b5f9` | 非房主自己的按钮：已准备就变灰（所以按钮是**单向**的，取消准备只能靠 F5）|

`0x4696cd` 的门槛（按在座人数）：1 人直接放行；≤4 人要 ≥2；≤6 人要 ≥3；
再多要 ≥6。**所以房主按不动「开始」和「准备状态别人看不见」是同一个根因**。

⚠ 服务端方向的 `0x030e` 是**另一回事**（`0x40899a`，弹
「팀 검색이 완료되었습니다」= 组队匹配完成），不要回显。

### 客户端会**自己清掉**所有人的准备状态

`LoadingStage` 构造函数（stage 5 → 6，`0x46fc0f` 起）：

```asm
for i in 0..5:
    if seatOccupied(i): [seat_i + 0x2e] = 0
```

所以服务端在「开一局」和「回房间」时也要清一次，否则下一局两边对不上。

### 实机验证（会话 10，一台电脑，对着真客户端直接推包）

用 `gs_ctl.py raw 0301 <hex>` 把 **action 3** 推给真客户端：

| 推了什么 | 屏幕上 |
|---|---|
| 座位 1 `ready=1` | 玩家列表里 `testuser1` 旁边出现黄色「准备中」，天空里模型脚下出现黄色 `Ready` 标签，**聊天框一个字都没有** |
| 座位 1 `team=2`（对方是 0）| 对方 `testuser2` 的名牌**从白变红**（敌队色）|
| 座位 1 `ready=0 team=0` | 两样都复原；随后客户端自己弹了「两组人数不相同。请调整人数。」—— 正是 `0x468495` 那条检查 |

⚠ 房主那一格**永远不显示「准备中」**：`0x46c311` 先判「是不是房主座位」，
是就画「팀 장（房主）」然后直接返回，根本不看 `+0x2e`。验证要挑非房主的座位。

## 166. ★★★ 大厅右侧「玩家列表」的数据源是 **`0x0212`**，不是 `0x020d`

PROGRESS 里挂了很久的待办（「`0x020d` 的服务端方向不是用户列表，真正的数据源
还没找到」）解决了。

- **请求**：客户端方向 `0x020d gcpReqUserList`，**5 字节**，
  发送点 `0x554513`（由 `0x43d0c9` 调，**每 10 秒**一次，节流常量 0x2710）：

  ```text
  u16  页号       从 0 起
  u16  每页几条   客户端写死 0x12 = 18（`0x441bed` / `0x44215c`）
  u8   过滤开关   1 = 全部，0 = 只看「추천상대（推荐对手）」
  ```

- **应答**：服务端方向 **`0x0212`** `Packet_gspRepUserList`（vft `0x6918d4`）。
  分发跳表 `@0x54e58a`（索引 = `opcode - 0x020e`）第 4 格 → `0x54e276`
  → 处理器 `0x55458b`。反序列化 `0x54d343`：

  ```text
  u16    页号       -> pkt+0x04   （`0x5d59f1` 读 2 字节）
  u16    每页几条   -> pkt+0x06
  u8     过滤开关   -> pkt+0x08   （`0x5d59d0` 读 1 字节）
  int32  条目数
  条目 × { string 昵称, int32 a, int32 b, int32 c }    每项 0x14 字节
  ```

  每项是一个 `UserSnap`（vft `0x665374`），反序列化 `0x43cf5c`
  = 一个字符串 + 三个 int32。

  处理器把头三个字段原样塞进列表管理器 `[0x72e674]` 的 `+0x14/+0x18/+0x1c`，
  也就是**服务端必须把请求里的页号 / 每页几条 / 开关原样回显**，
  客户端的上一页 / 下一页按钮（`0x43cffc` / `0x43d047`）全靠它算。

- 服务端方向的 `0x020d`（`0x553c5f`）只是把两个字符串塞进一个弹窗对象
  （`0x40f87c`），**和列表无关** —— 一直回它就是「列表永远空着」的根因。

⚠ 三个 int32 的业务含义**还没查明**。目前第一个填等级、其余填 0，
实机上列表能正常显示昵称和左边那颗星标（截图见会话 10）。

**实机验证（会话 10）**：真客户端 + `fakeclient.py` 各登一个账号，
大厅右侧「玩家列表」出现 `★ testuser1` / `★ testuser2` 两行；
假客户端断开后 10 秒内那一行自己消失。

## 167. ★★★★★ 对战（PvP）**必须由服务端判胜负并结束** —— 客户端那套永远跑不到

用户 2026-08-12 报的：「对战模式分出胜负后无法退出返回房间，胜利的人还可以动，
死的人无法复活，倒计时结束也不退出」。逐包对了一整局的 verbose 日志
（`logs/server.out`，00:46:13 开打 → 00:54:39 被手动杀进程），结论：

★ **整局连一发 `0x040f gcpEndQuest` 都没有。** 客户端从头到尾没打算结束这一局。

### 客户端自带的结束链，以及它为什么跑不到

```text
GameContextQuest::CheckMatchOver  0x4a3cf7   （每帧）
  if [this+0x3b0] != 2:  return              ★ 卡在这里
  if [this+0x564] != 0:  return
  if [this+0x5b4] != 0:  return              （已经发过了）
  if 0x4f629c([0x72e2f0]): return
  if [this+0x384]->vf(+0x20)():              ★ IVictoryCondition::IsOver
      6 秒后 -> 0x4a4026 发 0x040f，并置 [this+0x5b4]=1
```

`[GameContextQuest+0x3b0]` 是关卡状态，写它的只有 `0x4a3deb`（虚函数，
vftable 索引 20 / 偏移 0x50）。全镜像里**唯一**把它设成 **2** 的调用点是
`0x4f7164`，而那一段的前置条件是：

```asm
0x4f713f  mov eax, [0x72e260]
0x4f7144  call 0x4e71c0          ; 剧本状态机
0x4f7149  cmp eax, 3
0x4f714c  jne 0x4f7167           ; 不是 3 就什么都不做
```

`[0x72e260]` 是**地图剧本**对象，`0x4e71c0` 在剧本列表为空时直接返回 0。
**对战地图没有剧本** → 状态永远停在 1 → `CheckMatchOver` 第一行就返回 →
这一局永远不结束。闯关地图有剧本，所以 V0.1 起那一路一直是好的。

⇒ **判胜负和结束这件事，原版就是服务端的活。** 我们必须自己做。

### 客户端的胜负判据（照抄它，口径才对得上）

房间描述符 type 1 的胜负条件由工厂 `0x55e0de` 按 **`arguments[1]`** 选：

| `arguments[1]` | 类 | 时间上限 |
|---|---|---|
| 0 / 2 | `0x55e2b0` 那一路 | —— |
| 1 | `TimeAttackVictoryCondition` | —— |
| **3** | **`DeathMatchVictoryCondition`**（vft `0x6934bc`）| **240000 ms**（`0x55e133`: `mov esi, 0x3a980`，经 `vf(+0x18)` 存进 `[victory+0x188]`）|
| 7 | `DebugVictoryCondition` | 180000 ms |

`DeathMatchVictoryCondition::vf8`（`0x55bf20`）三条，任一成立就结束：

```text
① 已用时（timeGetTime() - [this+0x178]）> [this+0x188]        时间到
② 某个在座座位的分数 >= [this+0x198]                          分数到顶
③ 0x55c594(this)：组队模式下在座的人全同队 / 非组队在座 < 2   只剩一边
```

`[victory+0x198]`（分数上限）由构造函数 `0x55be71` 按人数定，默认 5：

| 组队战（键 = 人数 // 2） | 个人战（键 = 人数） |
|---|---|
| 1 → 4、2 → 6、3 → 8 | 2 → 4、3 → 6、4 → 8、5 → 9、6 → 10 |

### 服务端怎么数分：`0x0408` 里的「凶手」字段就是开火者的座位号

`0x0408` 线偏移 +0x05 那个字节是 `[char+0x158]`，而写它的是
`0x4fedee`（`mov [ebx+0x158], esi`，`esi` = 开火者的座位）。怪物 / 环境是
`0xff`。所以服务端不需要客户端另外上报分数就能数出杀敌数。

★ 而且**对战里客户端从不发 `0x0410 gcpUpdateQuestScore`**（整局日志零发），
所以 `Conn.quest_score` 恒为 0 —— 光靠它排名会永远判成「全场 0 分不判」（D083）。
对战的分数改用杀敌数。

### 顺带查明的两个客户端方向包（本次没实现，记着别再猜）

| opcode | 序列化 | 载荷 | 是什么 |
|---|---|---|---|
| `0x040d` | `0x558f8e` | 两个 int32 | `Character` 状态变更上报（`0x509843`：只有**自己**的角色才发）。实测节奏是「死亡 → 5 秒后 `0x0413` → 2 秒后 `0x040d(座位, 0)`，之后每 5 秒一发」 |
| `0x0409` | `0x467070` | 空 | 进房间那一刻发一次（`0x467078` 同时把房间 UI 存进 `[0x72e2cc]`）|
| `0x0415` | `0x558ef6` | 一个 int32 | `0x493b84` 发，战斗中偶发（0 / 1）|

不回它们不影响任何已验证的功能。

---

## 168. ★★★ 注册页「跳过新手教程」开关，以及顺手踩出来的两个坑

用户 2026-08-12 的需求：注册页加一个勾选框，勾上的话新账号直接跳过新手教程，
**进页面时默认勾着**。

### 落点：只动 `tutorial_completed` 一个字段

存档里和教程有关的是**两个**字段，别弄混：

| 字段 | 谁写 | 含义 |
|---|---|---|
| `tutorial_completed` | 人（注册页 / 手改存档） | **权威开关**。False 就一定重走一遍教程 |
| `tutorial_progress` | 客户端 `0x030f gcpReqFirstUserResult`（实测报 4 / 5） | 保真记录，只在 `completed=True` 时回放 |

`tutorial_state()`：`completed` 为假回 **0**，为真回 `max(3, progress)`。
客户端在这个数 `>= 3` 时跳过强制教学关（V0.1 §54），所以
**只置 `tutorial_completed=True` 就够了**，`progress` 保持 0 —— 我们没跑过教程，
不该往那一格编一个客户端从没上报过的值（D094）。

### ★ 坑一：`server/web/server.py` 独立跑时，日志里的 `✓` 会**打断正在处理的请求**

`python server\web\server.py --port ...` 这条调试入口有两处是坏的（都已修）：

1. **`sys.path` 的补丁写在文件末尾的 `__main__` 块里**，而模块级
   `from account_store import ...` 早就先执行过了 —— 直接跑必然
   `ModuleNotFoundError: No module named 'account_store'`。补丁必须提到 import **之前**。
2. 更阴的一条：`app.py` / `gameserver.py` 等都在模块级把 stdout/stderr
   `reconfigure(encoding="utf-8")`，**`web/server.py` 没有**。独立跑（或把 stdout
   重定向到文件）时默认编码是 GBK，`eventlog.online("注册页 ✓ …")` 里那个
   `✓` 当场抛 `UnicodeEncodeError`，把请求线程整个掀翻 ——
   **账号已经写进 `accounts.json` 了，但 200 应答没发出去**。
   浏览器 fetch 在连接被掐后重试，第二发撞上重名，玩家看到的是
   **「该用户名已存在，请在登录界面直接登录」**。

   ★ 记住这个症状：**「注册说重名，但存档里根本没有 / 或者刚好有一条」
   = 服务端在回包之前炸了**，不是账号系统的问题。走 `app.py` 的正常路径
   不受影响（它一进来就把两个流掰成 UTF-8 了）。

### ★ 坑二：`test_online.RegisterWebTests` 的账号名是**按用例名前 12 个字符**造的

原来是 `self.who = f"u{用例名[:12]}"`（用户名最长 16 个字符，所以要截）。
新加的 `test_register_with_skip_tutorial…` / `test_register_without_skip…` /
`test_register_defaults…` 截完**全是 `test_registe`**，和既有的
`test_register_then_duplicate` 共用同一个账号 —— 表现为一堆用例莫名其妙地
「该用户名已存在」，而且**先跑的那个也会被后跑的带红**。
已改成「计数器 + 一小截用例名」，唯一性靠计数器。

---

# 第九批 —— 用户实机反馈（2026-08-12 会话 12）

## 169. ★★★★★ 玩家列表 `UserSnap` 三个 int32 的含义 + 过滤开关的极性

会话 10 留的那条待办（「三个 int32 的业务含义还没查明，现在填『等级, 0, 0』」）
查明了，而且**填错位置正是用户报的「所有玩家显示的一样、分不清哪个是自己」**。

渲染函数 **`0x441df5`**（每帧画一行一行的那个循环，`0x441eb9`~`0x4420b8`），
条目结构 `UserSnap`（0x14 字节，反序列化 `0x43cf5c`）：

| 结构偏移 | 线上顺序 | 客户端拿它干什么 | 依据 |
|---|---|---|---|
| `+0x04` | 字符串 | 昵称，画在 x=0x4a、宽 0xc8 | `0x44201a` |
| `+0x08` | 第 1 个 int32 | **在不在打游戏**：`!=0` 画帧 4、`==0` 画帧 5（hover 各 +3）| `0x44203b`: `neg eax; sbb eax,eax; add eax,5` |
| `+0x0c` | 第 2 个 int32 | **等级**：`Images/General/LevelMark.smf` 第 `等级-1` 帧，钳在 1..60 | `0x441fc0`（`0x3c` 上限）|
| `+0x10` | 第 3 个 int32 | **竞技场（래더）等级**：`LadderMark.smf` 第 `20-它` 帧，越界取第 0 帧 | `0x441f3f`（`0x14` 上限）|

- `LevelMark.smf` 头里写着 **0x3c = 60 帧**、`LadderMark.smf` **0x14 = 20 帧**，
  和上面两个钳位常量对得上。
- 那两张徽章图是 `Images/NewUI2/LobbyUserList.smf` 的帧 4/5/6（暗）和 7/8/9（亮）：
  肉眼看是 **`P`（Playing）/ `W`（Waiting）/ 一个带气泡的 `W`**。代码只用 4/5，
  hover 时 +3 换成亮的那组。**帧 6/9 没人用。**
- ⇒ 上一版把等级填进第 1 个 int32：每一行都是「P + 1 级 + 天梯第 0 帧」，
  六个人长得一模一样。会话 10 截图里那颗「★」就是 `LadderMark` 的第 0 帧。

### ★ 过滤开关（请求第 5 个字节）：**1 = 只看待机玩家，而且这是默认档**

大厅右下角那两个按钮（帧对象 `[frame+0xe0]` / `[frame+0xe4]`），
标签和 tooltip 都能对上号：

| 控件 | 标签 | tooltip | 点它之后 `[frame+0xcc]` |
|---|---|---|---|
| `+0xe0` | `0x665c64` 대기유저（待机玩家）| `0x665c3c`「点击后将只显示待机玩家」| **1** |
| `+0xe4` | `0x665c30` 추천상대（推荐对手）| `0x665c08`「点击后将只显示推荐对手」| **0** |

初始化 `0x441bed` 写死 `[frame+0xcc] = 1`、按钮状态 `0xe4=0 / 0xe0=1`，
**和「点了待机玩家」之后完全一样** —— 所以进大厅的默认档是**待机玩家**。

`0x42cef7(属性 4, 值)` 就是「按下 / 未按下」：它把值写进 `[ctrl+0xb4]` 的 bit 2
（`0x42cf1d`），而 `0x42c8c2`~`0x42c8f7` 的绘制里这一位为 1 就把绘制原点
偏移 `[ctrl+0xc0]`（按钮被按下的那种位移）。也就是**当前档位的按钮画成按下**。

### 请求 / 应答的其余字段（§166 已记，这里补齐语义）

```text
请求 0x020d（5 字节，0x554513）:  u16 页号 / u16 每页几条(写死 0x12=18) / u8 过滤开关
应答 0x0212:                     头三个字段原样回显 + int32 条目数 + 条目 × 0x14 字节
```

翻页：下一页 `0x43cffc`、上一页 `0x43d047`。★ **过滤开关 = 0（推荐对手）
且频道 != 9 时，页号被钳死在 0..1**（`0x43d01d`），也就是推荐对手最多两页 36 人。

### 客户端**没有**「这一行是我自己」的标记

渲染循环和命中测试（`0x441cf3`）从头到尾**不比昵称**，条目里也没有「是不是我」
这一位。所以「分不清哪个是自己」在协议层无解，只能**不把自己列进去**（D095）。
点一行会走 `0x54edda`（拿昵称去开「玩家情报」窗口），自己那一行本来也没用。

## 170. ★★★★ 房间列表请求第 4 个字段 = 大厅左下角「全部 / 待机」，**默认也是「待机」**

§139 当初把这个 u8 记成「未定字段」，于是那对按钮怎么点都没反应
（用户 2026-08-12 报的第 3 条）。

请求（12 字节，序列化 `0x54e68c`，参数由 `0x43a78e` 传下来）：

```text
u16   起始房间号   分页锚点（列表里 index 0 的房间号）
u16   ?            恒 0
u16   每页几个     客户端写死 10（`0x44056c`：大厅一页正好 5×2 格）
u8    过滤开关     0 = 全部房间，1 = 只看待机房间
u8    ?            恒 0
int32 游戏类型     由频道码翻译：7->2 8->6 9->5 其余->1
```

| 控件 | 标签 | tooltip | 点它之后 `[frame+0xcc]` |
|---|---|---|---|
| `+0x11c` | `0x665a9c` 전체방_로비（全部）| `0x665a74`「点击后将显示全部房间」| **0**（`0x440ecd`）|
| `+0x118` | `0x665a64` 대기방_로비（待机）| `0x665a3c`「点击后将只显示待机房间」| **1**（`0x440f16`）|

初始化 `0x44057a` 写死 `[frame+0xcc] = 1`，同一段里 `0x43f328` 把
`0x11c` 画成未按下、`0x118` 画成按下 —— 和「点了待机」一样，
**所以进大厅默认只看待机房间**。

★ **旁证（真客户端的字节）**：V0.1 时代的抓包 `logs/game_00*_27799.dec.bin` 里
一共 14 发这个请求（对战 10 发 + 任务 4 发），**过滤字节全是 `01`，一次 `00` 都没有**
—— 和「默认 = 待机」完全一致。

## 171. ★★★★★ 断线重连：客户端**只重放旧票据，永远不回认证服**

用户报的第 5 条：「服务器断线会提示正在重连，服务器恢复上线后客户端显示
『在无法连接的地方尝试了连接』，无法自动重连」。

链路（`OnDisconnected` = **`0x54da2e`**，游戏服那条连接断掉时走它）：

1. `0x40f87c` 弹自绘公告框，文案 key `0x6928dc`
   =「서버 장애가 발생하여 자동으로 재접속을 시도합니다.」
   → `Chinese.ini`「发生服务器障碍,自动尝试连接。」——**用户看到的就是这句**；
2. 销毁 `GameSession`（`[0x72e29c]` 的 vft[0]，参数 1），`0x40bfdd` 复位；
3. 按频道码把 `[0x6dc678]` 设成 2 / 5 / 6 / 1（「回来之后该进哪个频道」）；
4. `0x5bc41a` 重连，连上之后**原样重放手里那张票据**发 `0x0100 gcpReqLogin`
   （和 §132 顶号那条路一模一样）；
5. 登录成功后，**第一发 `0x0200` 房间列表的处理器 `0x54f596` 开头**
   看见 `[0x6dc678] != -1`，就用它发一发 **`0x020b gcpReqMoveChannelByGameType`**
   （`0x55395a`）把自己送回频道，再把那个全局清回 -1。

⇒ **服务端只要认得那张票据，整条重连链路是客户端自己走完的**，我们一个包都不用加。

★ 第 5 步那个 `0x020b` 里的值是**由 `[conn+0x89c]` 频道码翻译**出来的
（`0x54dabe`：7→2、8→6、9→5、其余→1）。我们的服务端只会把频道码设成
`GAME_TYPE_CHANNEL_CODES` 里的 0 / 7 / 9，所以翻回来只可能是 1 / 2 / 5，
**都在 `on_game_packet` 的 `0x020b` 分支能回包的范围内**。
（那个分支对游戏类型 6 是「不回包」—— 只有频道码 8 才会走到，我们从来不发 8。）

### 为什么原来必然失败（两个独立原因，都会导致 `result=3`）

1. **票据表只在内存里**（`tickets.py` 开头原话写着「服务端一重启全部作废 ——
   这没问题，玩家本来就要重新登录」）。服务端被重启 = 票据全没 =
   `resolve()` 落空 → `result=3` →「在无法连接的地方尝试了连接。」
   ★ 那句文案是**客户端自带的**，不是我们写错字（§132 那张表）。
2. **TTL 从签发时刻算，且从不续期**（600 秒）。玩一局超过 10 分钟再断线，
   哪怕服务端一直活着，重连也会被判过期。

### 修法（D096）

- `resolve()` 每次命中就**续期**（滑动 TTL）；
- 游戏服登录成功时 `bind()`，把这张票据的 TTL 换成 `BOUND_TTL_SECONDS`
  （默认 12 小时）—— 服务端不在的那段时间没人能给它续期，短了就等于没修；
- 票据表**落盘** `server/data/tickets.json`（`--tickets` / `--no-ticket-file`）。
  存的是**剩余寿命换算出的绝对时刻**（`expires_at`，墙上时钟），所以服务端停机
  那段时间照样算进去：**未用过的票据熬不过 10 分钟停机，已 bind 的能熬 12 小时**。

### 实测（会话 12，一台电脑，`fakeclient.py --ticket` 重放）

| 重放什么 | 结果 |
|---|---|
| 已 bind 的票据，**服务端完整重启过一次** | `gspRepLogin result=0`，正常进大厅 |
| 没见过的票据 | `result=3`（「在无法连接的地方尝试了连接。」）|
| 被顶号作废的旧票据 | `result=2`（「现有连接已断开。请重新尝试连接。」）—— `revoked` 表也跟着落盘了 |

重启后服务端启动横幅会打一行「票据表 : …（当前 N 张有效）」，N 就是读回来的条数。

## 172. ⚠ RDP 里跑不起来客户端：「图像引擎初始化失败」

本会话想用真客户端验大厅那两处 UI，**失败**：这台机器当时是 **RDP 会话**
（`query session` 显示 `rdp-tcp#36` 是 Active，控制台会话被锁，`LogonUI` 在跑），
客户端起来之后弹 `#32770「错误提示」/「图像引擎初始化失败」`（D3D9 建不出设备），
`tools/screenshot.py screen` 抓下来是**全黑的 1920x1200**。

- **能用的**：`tools/gui_probe.py`（走 `WM_SETTEXT` / `BM_CLICK`，纯窗口消息，
  连登录框文案都能读回来验证）、`tools/fakeclient.py`（纯协议）。
- **不能用的**：截图、`tools/click.py`（D3D 自绘 UI 只能靠合成鼠标）。
- ⇒ 要验大厅/战斗画面，必须在**物理控制台会话**上跑。RDP 里做 UI 验证是白费时间，
  先 `query session` 看一眼再决定。
