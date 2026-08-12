# 炮炮火枪手 —— 服务端包

给「开服的人」用的包。解压就能跑，**目标机器不需要预装任何东西**
（Windows 的 Python 运行时已经打进包里；Linux 见下面「Linux 上跑」一节）。

打包信息在 `BUILD.txt` 里。**客户端包和服务端包必须成对使用**，
两边 `BUILD.txt` 的「打包批次」要一致 —— 版本不配套时的典型症状是
玩家一进房间就被弹回大厅。

---

## Windows 上跑

```text
双击 start.bat        日常开服（精简日志）
双击 start-debug.bat  排查问题（逐包 hexdump，日志按 MB 涨，别长期开）
双击 stop.bat         关服
```

第一次启动时 Windows 防火墙会弹窗，**必须点「允许访问」**，否则别的电脑连不进来。
不小心点了「取消」的话，用管理员权限的命令行执行：

```bat
netsh advfirewall firewall add rule name=PopShot dir=in action=allow protocol=TCP localport=47611,27799,27798,27810
```

## Linux 上跑

```text
unzip PopShot-server.zip        # 或 tar -xzf PopShot-server.tar.gz
cd PopShot-server
chmod +x *.sh tools/*.sh        # ZIP 不保留可执行位，解压后补一下
./start.sh                      # 没 chmod 的话用 sh start.sh
./stop.sh
```

Python 的挑选顺序：

1. 包里的 `runtime-linux/`（打包时选了「带 Linux 运行时」才有）。里面是一个
   **没有解开的 `.tar.gz`**，第一次 `./start.sh` 会自动解开它 —— 打包是在
   Windows 上做的，那边解会丢掉符号链接和可执行位，所以留到 Linux 上再解；
2. 系统的 `python3`，要求 **3.10 或更新**。

两个都没有时 `start.sh` 会直接报错并把两条出路写出来。

服务端在后台跑（`nohup`），关掉终端不影响它；pid 记在 `logs/server.pid`。

---

## 要放行哪些端口

| 端口 | 协议 | 谁用 | 能不能改 |
|---|---|---|---|
| `47611` | TCP | 认证服 | **不能**，客户端写死 |
| `27799` | TCP | 游戏服 | **不能**，客户端写死 |
| `27798` | TCP | 战斗同步中继 | 不建议，改了客户端包也要重编 |
| `27810` | TCP | 用户注册网页 | 能，改 `server.config` 的 `local_register_port` |

**云主机要开两道**：系统防火墙（ufw / firewalld）**和**云厂商控制台里的
安全组 / 网络 ACL。只开一道是最常见的「本机能连、外面连不上」。

调试控制通道（27800）在服务端包里**默认关闭**（`app.py --no-control`），
不需要放行。

---

## 玩家那边怎么连

1. 把**同一批次**的客户端包发给玩家；
2. 玩家改自己那份 `server.config`：

   ```text
   server_address = 你这台服务器的 IP 或域名
   server_register_port = 27810
   ```

   IPv4 / IPv6 / 域名都支持，IPv6 加不加方括号都行；
3. 玩家先在浏览器打开 `http://<你的地址>:27810/` 注册账号
   （游戏登录界面下方的注册链接点开的也是这个页面）；
4. 游戏登录界面选**「远程服务器」**，用刚注册的账号密码登录。

---

## 账号数据

全部账号就是 `server/data/accounts.json` **一个文件**，备份它等于备份全服。

⚠ **密码是明文保存的**（本项目的既定做法）。请提醒玩家不要用在别处用过的密码，
也不要把这个文件发给别人。玩家还可以在注册页的「存档转移助手」里自助导出 / 导入存档。

---

## 出问题先看哪

| 文件 | 内容 |
|---|---|
| `logs/online.log` | **谁连上、谁断开、从哪个 IP、在线多久。**精简模式也照记，重启不清空 |
| `logs/server.out` | 服务端全部输出（**每次启动会被覆盖**） |
| `logs/server.err` | 崩溃和异常栈 |

**玩家说进不去，第一件事是看 `logs/online.log`**：

- 里面**根本没有**他那条记录 → 包没到你这台机器，问题在网络：地址填错、
  端口没放行、或者他没选「远程服务器」；
- 有「认证服 ✗」→ 账号或密码不对（他还没注册，或者注册在别的服务器上）；
- 有「✓ 认证通过」但没有「游戏服 ✓ 登录」→ 27799 没放行；
- 有「⚠ 被顶号」→ 同一个账号在两台机器上登录了。

排查完整协议时才用 `start-debug.bat` / `start-debug.sh`，
它会逐包 hexdump 并给每条连接落一对抓包文件。
