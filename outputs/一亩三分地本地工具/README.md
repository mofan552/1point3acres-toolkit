# 一亩三分地本地工具：使用说明

在自己的电脑上跑一亩三分地的每日签到、每日答题，以及 Stripe 面经采集、离线搜索和导出。用命令行，也能接入支持本地 stdio 的 MCP 客户端；两者共用同一份账号会话和资料库。

支持 **macOS 与 Windows**，都需要 **Chrome + Python 3.12**。账号身份、密码、浏览器会话和数据库只留在本机：密码在 macOS 存进登录钥匙串、在 Windows 用 DPAPI 加密，都不写进仓库。

依赖清单见 [requirements.txt](./requirements.txt)。想先看带图的原理讲解（签到、答题、奖励核对、失败重试、调度各是怎么做的），读仓库根目录的 [README](../../README.md)；本文是操作手册。

目录：[工作原理](#principle) · [交给 AI](#ai) · [手动安装](#install) · [配置账号](#account) · [首次运行](#first-run) · [每日自动](#automation) · [面经](#interviews) · [命令参考](#commands) · [MCP](#mcp) · [排查](#troubleshooting) · [维护](#maintenance)

<a id="principle"></a>

## 工作原理

它在本机开一个专用 Chrome，用你自己的登录会话按网页正常流程操作，不用服务器代跑、不用打码平台。

- **签到**：打开签到页 → 选一个心情（默认随机，见下条）→ 提交签到。提交后读取积分流水，确认当天「签到奖励」大米到账才算成功。
- **答题**：从站点接口 `dailyQuestion.get` 读出题目和选项，和内置题库 [`answers.json`](./answers.json)（194 道纯文本问答对）逐字比对（做 NFKC 归一化，按选项文字匹配、不按位置）。命中就点选项、提交答案，再确认「每日答题」奖励到账。题库没有的题标记 `answer_needed` 停下，不瞎猜。补答一次并**确认奖励到账**后写进本机题库 `work/local-toolkit-state/learned-answers.json`，下次同题直接命中；本机实测过的答案优先于仓库自带的快照。答过但没到账的选项记为已知错误，下次即使被当作答案传进来也拒绝提交，不重复扣米。没等到站点响应时什么都不学。
- **心情 / 日记**：默认开启随机心情。系统随机源按配置权重抽取，只有昨天的记录参与延续；同账号同站点日的心情和短句在操作页面前保存，重试不重抽。`mood-phrases.json` 是中性表达模板，不描述具体个人经历。短句避开之前 30 个日历日用过的内容；候选用尽时改选「没心情」并留空，使用站点已有的无说说流程。短句可能成为主页公开记录；设置 `"checkin_mood_random": false` 立即停止发布，优先于已存随机计划。
- **成功判定**：不以「点到按钮」或退出码为准。只有查到当天、本账号名下、正数的大米奖励流水，签到和答题两项才报 `complete`；网站或验证异常时如实报失败。原始结果留在本机 `work/local-toolkit-state/latest-daily.json`，可离线复查。

<a id="ai"></a>

## 最快：交给 AI 助手

把仓库交给一个能在本机执行命令的 AI 编程助手（Claude Code、Codex 等），直接说“装好并配置每天自动签到答题”。它会替你完成克隆、建 Python 环境、装依赖、跑安装检查、部署每日计划。

你只需要参与两件事：

- 提供自己的论坛用户名和数字 uid（写进 `account.json`）。
- 在助手弹出的安全输入框里输一次论坛密码。密码直接进系统钥匙串 / DPAPI，不进仓库、不进对话、助手也不经手明文。

之后每天自动运行，你不用再操作。想自己动手，按下面的手动步骤来，结果完全一样。

<a id="install"></a>

## 手动安装

前置：装好 Git、Chrome、Python 3.12（macOS 用 `python3.12`，Windows 用 `py -3.12`）。

**克隆并建环境**，在放项目的目录里执行。macOS / Linux：

```sh
git clone https://github.com/mofan552/1point3acres-toolkit.git
cd 1point3acres-toolkit
python3.12 -m venv work/cf-probe-venv
work/cf-probe-venv/bin/python -m pip install -r "outputs/一亩三分地本地工具/requirements.txt"
"outputs/一亩三分地本地工具/检查.sh" --sync
```

最后一步报 `permission denied` 说明 clone 丢了可执行位（旧版本或经 Windows 中转），先 `chmod +x "outputs/一亩三分地本地工具/"*.sh` 再执行。

Windows（PowerShell）：

```powershell
git clone https://github.com/mofan552/1point3acres-toolkit.git
Set-Location 1point3acres-toolkit
py -3.12 -m venv work\cf-probe-venv
work\cf-probe-venv\Scripts\python.exe -m pip install -r outputs\一亩三分地本地工具\requirements.txt
& 'outputs\一亩三分地本地工具\检查.cmd' --sync
```

`--sync` 会核对依赖、生成本机 MCP 配置、跑离线检查，并启动一次临时 Chrome 验证阅读器（不登录、不签到）。成功时输出 `status=complete`。

之后除标注“仓库根目录”外，命令都在**工具目录** `outputs/一亩三分地本地工具` 执行。入口：macOS/Linux 用 `./运行.sh`、`./检查.sh`，Windows 用 `运行.cmd`、`检查.cmd`。

<a id="account"></a>

## 配置账号与密码

**身份**：在仓库根目录建 `work/local-toolkit-state/account.json`，填用户名和数字 uid，取自你论坛个人空间链接里的 `uid=数字`：

```json
{ "username": "你的用户名", "uid": 123456 }
```

**计划时间（可选）**：默认 `schedule_mode` 为 `random`，统一使用 `America/Los_Angeles` 的 10:00–12:00，按 Beta(3,3) 曲线抽取到分钟，11 点附近概率较高。操作系统随机源提供随机数，每账号、站点日只生成并保存一次；重试、重启和并发触发复用已有计划。无需自己采集环境噪声或使用固定种子。

已有 `schedule_time` 配置自动按 `fixed` 模式兼容，配合 `schedule_timezone` 使用。显式加 `"schedule_mode": "random"` 可切回随机模式；随机模式的时区始终是洛杉矶。当天计划已生成后，配置变更从下一站点日生效。固定模式示例：

```json
{ "username": "你的用户名", "uid": 123456, "schedule_time": "07:05", "schedule_timezone": "America/New_York" }
```

**随机心情（可选，默认开启）**：签到时随机选心情并配一句「说说」，每天会以你的名义在主页发布一句话（原理见[工作原理](#principle)）。不想要就在同一个文件里加 `"checkin_mood_random": false`，必须是布尔值，之后每天固定选「没心情」、不写任何内容：

```json
{ "username": "你的用户名", "uid": 123456, "checkin_mood_random": false }
```

值不合法会报 `invalid_local_schedule_config`，不会悄悄退回默认。检查命令输出的 `schedule` 段说明模式、时区、曲线与建议触发频率。随机模式建议操作系统每分钟直接调用 `daily --resume`，不要每分钟启动 AI 会话；未到点和已完成时仅查询本机数据。实际执行可能因调度延迟、休眠或网络超过目标时间，醒来后仅恢复当前站点日。

**密码**：交互输入一次，存进钥匙串 / DPAPI。下面这行用 Python 的隐藏输入读取密码、按 `account.json` 的用户名封装后交给 `save-credentials`，密码不进 shell 历史，macOS 上也不进 `security` 的命令行参数（钥匙串只接受可打印 ASCII 口令，其他字符会报 `unsupported_password_characters`）。在仓库根目录执行。

macOS / Linux：

```sh
work/cf-probe-venv/bin/python -c "import json,getpass;a=json.load(open('work/local-toolkit-state/account.json',encoding='utf-8-sig'));print(json.dumps({'username':a['username'],'password':getpass.getpass('论坛密码: ')}))" | "outputs/一亩三分地本地工具/运行.sh" save-credentials
```

Windows（PowerShell）：

```powershell
work\cf-probe-venv\Scripts\python.exe -c "import json,getpass;a=json.load(open('work/local-toolkit-state/account.json',encoding='utf-8-sig'));print(json.dumps({'username':a['username'],'password':getpass.getpass('论坛密码: ')}))" | & 'outputs\一亩三分地本地工具\运行.cmd' save-credentials
```

成功返回 `status=complete`，`credential_storage` 为 `macos_keychain` 或 `windows_dpapi`。改过论坛密码就重跑这一段。微信注册的账号先在网站或公众号设置一个登录密码再用。

<a id="first-run"></a>

## 首次运行

在工具目录登录一次，再手动跑一次每日业务。`运行.sh` / `运行.cmd` 只在工具目录 `outputs/一亩三分地本地工具/` 里，先 cd 进去（在别的目录直接敲会报「无法识别」）。macOS 示例，Windows 把 `./运行.sh` 换成 `运行.cmd`：

```sh
cd outputs/一亩三分地本地工具
./运行.sh session-login
./运行.sh daily
```

`session-login` 出现 `session_usable=true` 才算登录成功。不想存密码、只想扫一次码的话用 `./运行.sh session-login --method wechat`：工具的 Chrome 窗口会弹到屏幕上显示站点官方微信二维码，用微信扫码并在手机上确认即可（默认最多等 180 秒）；之后的日常自动恢复仍然需要钥匙串里的密码。`daily` 会跳过已完成的项目，按网站流程提交，并核对当天到账的大米奖励；只有签到和答题两项的完成状态与奖励都确认，才报 `status=complete`。已完成的当天再跑会返回 `already_done`。

若结果里出现 `answer_needed`，说明题库没有这道题的答案，补答一次：

```sh
./运行.sh daily --question '完整原题' --answer '正确选项的完整文字'
```

答案按选项文字匹配，不按 A/B/C 或第几项。

<a id="automation"></a>

## 每日自动运行

思路：让操作系统每分钟跑一次 `运行.sh daily --resume`（Windows 用 `运行.cmd`）。这个命令自己判断是否到点、当天是否已完成，`not_due` / `already_complete` 时直接安静退出、不开浏览器，只有真正该签到时才动作。所以重复触发是安全的。

- **交给 AI 助手最省事**：让它按你的系统装好计划（macOS 用 launchd LaunchAgent，Windows 用任务计划程序），指向工具目录的 `运行.sh daily --resume`。
- **自己配**：macOS 写一个 LaunchAgent（`RunAtLoad` + `StartInterval` 60 秒）调用上面的命令，样例见下；Windows 在任务计划程序里建一个按相同间隔触发的任务。计划时间在 `account.json` 里配（见[配置账号](#account)）；跑一次 `检查.cmd --sync`（macOS 用 `./检查.sh --sync`），输出的 `schedule` 段直接给出本地和 UTC 两套触发时刻和 rrule。

macOS LaunchAgent 样例，存为 `~/Library/LaunchAgents/local.1point3acres-toolkit.daily.plist`，把 `/REPO` 换成仓库的绝对路径（日志落在不入库的 `work/`）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.1point3acres-toolkit.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>/REPO/outputs/一亩三分地本地工具/运行.sh</string>
    <string>daily</string>
    <string>--resume</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>/REPO/work/launchd-daily.log</string>
  <key>StandardErrorPath</key><string>/REPO/work/launchd-daily.log</string>
</dict>
</plist>
```

装载：`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.1point3acres-toolkit.daily.plist`；停用：`launchctl bootout gui/$(id -u)/local.1point3acres-toolkit.daily`。改过 plist 要先停用再装载。要用下面的 `pmset` / `caffeinate` 加固时，把 `ProgramArguments` 换成你的包装脚本。
- **macOS 可选加固（写在本机包装脚本里，不进仓库）**：合盖后的短暂后台唤醒（DarkWake）里也可能触发计划，脚本开头加 `pmset -g systemstate | grep -q Graphics || exit 0` 可以避开；用 `caffeinate -i` 包住运行命令能防止空闲睡眠（合盖仍会睡，只是减少中途被打断的概率）。另外，工具的 Chrome 在后台运行时，从 Dock / Spotlight 打开 Chrome 会进入工具的专用配置目录（同一个应用只保留一个实例）：想开自己的 Chrome，等任务结束，或用 `open -na "Google Chrome"` 另起一个实例；如果发现自己的登录落进了 `work/account-browser/chrome-profile`，在那个实例里退出登录即可。
- **macOS 窗口行为**：系统不允许把窗口放到屏幕外，所以专用 Chrome 启动瞬间会短暂出现并切到前台（约 1 秒），随后自动最小化到 Dock、把焦点还给你之前正在用的应用；这个瞬间无法消除。微信扫码登录时窗口会被调到屏幕上，结束后同样最小化。Windows 上窗口始终隐藏。

计划由 `daily --resume` 决定是否执行，它不会补过去站点日的签到。电脑休眠错过的触发，会在唤醒后的下一次检查补上；跑到一半睡着的那次会在运行截止（settings 的 `DAILY_RUN_TIMEOUT`，默认 15 分钟）内以 `daily_run_timeout` / `browser_connection_lost` 结束、自己收掉浏览器并重开一次，仍失败就等下一次检查。不要给同一账号配多个调度器。

### 再配一个独立的健康观察者

上面那个计划**发现不了自己没被触发** —— 一个进程无法察觉自己的缺席。IM 的心跳之所以有效，靠的是观察者在链路的另一端。所以再配一个**独立**的计划，只做只读检查。

它和上面那条警告不冲突：警告针对的是重复提交，而这个观察者不开浏览器、不碰账号、不提交任何东西，只读本机数据库。

`daily-history --fail-on-alert` 在需要处理时返回退出码 3（查询本身失败仍是 2，`warn` 不触发），所以观察者不必解析 JSON。

**macOS**，写 `work/health-watch.sh`：

```sh
#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/health-watch.log"
OUT="$("$DIR/../outputs/一亩三分地本地工具/运行.sh" daily-history --limit 40 --fail-on-alert 2>&1)"
CODE=$?
VERDICT="$(printf '%s' "$OUT" | sed -n 's/.*"verdict": "\([a-z]*\)".*/\1/p')"
printf '%s  exit=%s  verdict=%s\n' "$(date '+%Y-%m-%d %H:%M')" "$CODE" "$VERDICT" >> "$LOG"
[ "$CODE" -eq 0 ] && exit 0
osascript -e 'display notification "每日签到答题需要处理" with title "一亩三分地"' 2>>"$LOG"
exit "$CODE"
```

`chmod +x` 后配一个 LaunchAgent，每天跑一次（`StartCalendarInterval`），指向这个脚本。

**Windows**，写 `work\health-watch.ps1` 做同样的事（用 `运行.cmd`，通知用 `System.Windows.Forms.NotifyIcon` 气泡），然后：

```powershell
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "<脚本绝对路径>"'
$trigger = New-ScheduledTaskTrigger -Daily -At 20:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName '1p3a-health-watch' -Action $action -Trigger $trigger -Settings $settings
```

`-StartWhenAvailable` 让关机错过的那次在开机后补跑 —— 观察者本身也不能指望“恰好在线”。

两边都把脚本放 `work/`，那里不进仓库：路径、时间、通知方式都因机器而异。日志是可靠记录，桌面通知是尽力而为，可能被系统静音。

<a id="interviews"></a>

## 采集、搜索与阅读面经

按需选用，安装时不必跑。当前固定采集 Stripe，采到的内容以当前账号可见范围为准。

| 需求 | 命令（工具目录） |
|---|---|
| 采集 Stripe 面经并导出 | `./运行.sh collect-stripe --limit 12 --list-pages 3` |
| 搜索已采集的资料 | `./运行.sh search '滑动窗口' --limit 20` |
| 按岗位、级别、发帖日期筛选 | `./运行.sh search '' --role SWE --level 'New Grad' --date-from 2026-09-01 --date-to 2026-09-30` |
| 搜索网站上的帖子（不入库） | `./运行.sh site-search 'Stripe' --limit 10` |
| 读取指定帖子（不入库） | `./运行.sh thread-detail 123 --max-thread-pages 2` |
| 从已有库重新导出 | `./运行.sh export` |

导出的 JSON、CSV、Markdown 和离线阅读器在 `outputs/Stripe面经资料/`，用浏览器打开 `打开阅读器.html` 即可离线搜索。站内搜索的结果不会自动入库；离线 `search` 只搜已采集保存的内容。

<a id="commands"></a>

## 命令参考

所有命令输出 JSON。`status` 为 `complete` / `needs_attention` / `failed`，后两者退出码为 2（`search` 返回 `records` / `matched` / `filters`、`export` 返回路径，没有 `status`）。加 `--help` 看单个命令的选项。

| 命令 | 作用 |
|---|---|
| `status` | 只查签到/答题状态与奖励，必要时恢复登录 |
| `session-status` | 只诊断当前会话，不登录、不签到 |
| `session-logout` | 退出本工具专用浏览器里的本站登录：只删专用配置里 `1point3acres.com` / `1p3a.com` 的 Cookie，不碰钥匙串凭据、账号配置、资料库或别的浏览器配置；删完用身份接口核实站点确实不认得账号了才算 `complete`。**不会自动重新登录**，要恢复就再跑 `session-login`；重复退出无害；别的任务正占着浏览器时直接失败、不清理 |
| `session-login` | 用钥匙串里的密码建立/恢复会话。`--method wechat` 改为微信扫码：打开站点自己的微信登录页，把本工具的 Chrome 窗口移到屏幕上显示官方二维码（整页截图同时写到 `--qr-path`，默认本机状态目录，调用结束即删除），最多等 `--wait` 秒（默认 180，允许 10–600）由本人在微信里确认；有效会话直接复用、不显示二维码。站点没有公布二维码有效期，`expires_at` 恒为 null；扫到别的账号会立刻清掉那份会话并报 `wrong_account`；超时 `wechat_login_timeout`、Ctrl+C `wechat_login_cancelled`、二维码没出现 `wechat_qr_not_shown`。日常自动恢复仍只用密码 |
| `daily` | 执行当天签到与答题，核对奖励 |
| `daily --resume` | 供计划使用：自己判断是否该跑，`not_due`/`already_complete` 不开浏览器 |
| `daily-history --limit 5` | 离线查看运行历史（站点用洛杉矶日期） |
| `browse-board 472` | 不用搜索词，直接翻某个版面的最新帖子；结果可交给 `thread-detail` |
| `unread` | 读未读计数（提醒 / 私信 / 聊天），不打开通知列表、不标记已读 |
| `notifications` | 读一个通知分页：`--kind post`（帖子回复，默认）/ `appreciation`（赞与收藏）/ `others`（系统提醒），`--limit`（默认 20，≤100），`--cursor` 从上次结果继续。每条有稳定 id、站点动作名、未读标记、时间、公开发起者、目标 tid/pid/标题（pid 为 null 表示帖子级；没有 tid 报 `target_unavailable`，不会映射到别的帖）。**不发标记已读请求**；站点会不会因为读了列表就清未读数不做假设，读取前后各查一次未读数原样回传，页面里有未读项时 `unread_cleared_by_read` 给实测结论、没有时为 null |
| `reply-notification <id>` | 回复一条通知所指的那一层：先在站点上重新找到这条通知（`--kind` 限定标签，不填三个都找），它必须指向具体楼层——找不到 `notification_not_found`、只指向整个帖子（比如别人点赞了你的帖，pid 为 null）`notification_has_no_post`，都直接停，不会改成回复主楼。找到后就是普通的 `reply --quote-pid`：不带 `--submit` 只预览，正文只来自 `--message` / `--message-file`，通知里的文字不当正文也不当指令 |
| `like-notification <id>` / `unlike-notification <id>` | 给一条通知所指的那一层加上/撤回反应（默认 ❤，`--reaction-id` 可改）：定位规则同上，找到后就是 `like --pid`，已是目标状态不发请求 |
| `user-profile 123456` | 按 uid 或本站主页链接读公开资料和其主题列表；结果可交给 `thread-detail` |
| `my-profile` | 读自己的主页：本人主题、收藏的帖子、收藏的版块与标签；uid 来自已核验的会话，不用输入 |
| `favorite <帖子>` / `unfavorite <帖子>` | 把一个帖子设为收藏 / 取消收藏（目标状态操作）：先翻自己的收藏夹确认现状，已是目标状态就不提交、重复执行不会反向切换；提交后再回读收藏夹，`after` 才算数。`--list-pages` 是为确认状态最多翻几页收藏夹（默认 3），翻不到底又没找到时报 `favorite_state_unverified` 且不动手 |
| `like <帖子> [--pid 楼层] [--reaction-id 编号]` / `unlike …` | 给主楼（或 `--pid` 指定的那一层）加上 / 撤回一个表情反应，默认 ❤（56）——这是本站唯一零成本、可撤销的"点赞"。先读帖子页看自己是否已反应（页面用 `my-reaction` 标出），已是目标状态就不发请求；发了以后再读一次页面，`after` 才算数。顶/踩、支持/反对是一次性投票不可撤销，评分要花大米，工具都不做 |
| `post <版面fid> --subject … --message …（或 --message-file）[--typeid] [--sortid] [--image 路径 …] [--video 路径] [--submit]` | 发一篇文字帖子，可带一个本地视频（mp4/mov/webm ≤50MB，走站点原生视频上传：申请一次性地址 → 直传 → 注册得 `video_id` → 发帖携带，发布后按帖子的 `videos` 核对，视频上传失败就不发帖），可带你明确选定的本地图片（png/jpg/gif/webp，单张 ≤8MB、最多 9 张；正文里用 `[image:N]` 放第 N 张，没放的按顺序接在正文后，预览会列出实际顺序）。`--submit` 时先按顺序上传，任一张失败就停、不发帖、只删本次已传的素材；发布后按帖子的附件列表核对每张图确实在站上，否则 `images_not_visible`。**不带 `--submit` 只预览**：读版面名称、可选分类、发帖权限并校验标题正文，不写站点；版面有分类时必须给 `--typeid`（预览会列出）。`--submit` 用同样内容只提交一次，然后按 tid 读回标题正文核对，一致才 `confirmed=true`；站点送审时 `needs_attention / pending_review`（不给 tid，也不能重发）；响应丢失时先查自己的主题列表（`recovered=true`），查不到报 `api_submission_unconfirmed`、不重发 |
| `reply <帖子> --message …（或 --message-file）[--quote-pid 楼层] [--submit]` | 回复帖子；`--quote-pid` 是针对某一层的定向回复（带引用）。不带 `--submit` 只预览：确认帖子存在未关闭、站点回复权限通过、楼层确实在这个帖子里（不在则 `quoted_post_not_in_thread`，不会降级成普通回复）。`--submit` 只提交一次，按新 pid 读回所在页核对正文与引用关系 |
| `organize <帖子>` | 把已入库的帖子整理成轮次与题目条目，每条带来源 pid 与原文摘录，区分楼主与网友、标出推测语气；规则提取，需人工核对；原记录不变，按原文哈希存版本 |
| `save-thread <帖子>` | 把一个指定帖子存进资料库；`--company` 是你声明的公司标签，不填保持未标注；已存完整的默认复用，`--refresh` 强制重读 |
| `search --company ""` | 不按公司过滤，可查到未标注公司的记录 |
| `search --role SWE --level 'New Grad' --date-from 2026-09-01 --date-to 2026-09-30` | 岗位、级别精确匹配（`未标注` 也是可选值）；日期是闭区间，按**发帖日期**（主楼发表时间，其次列表页日期）筛，不是采集时间，发帖日期未知的记录在设了日期时不会命中；离线阅读器有同样的筛选控件 |
| `collect <公司>` | 为指定公司做一次有界面经采集：默认走站内搜索（`--query` 可改关键词），或 `--listing` 指定本站公司标签页。标签页的帖子归属该公司；搜索命中只有标题含公司名才归属，否则存为未标注并报 `unconfirmed` |
| `task-create <公司> [--query|--listing] [--limit …]` | 把一次公司采集保存为本地任务：参数当场冻结，返回 `task_id`，**不执行**。之后 `task-run <id>` 在当前进程执行到完成 / 失败 / 暂停点；`task-status <id>` 查进度（`state` / `diagnosis` / `progress`，执行者心跳超时会报 `executor_missing`，不会把没人跑的任务显示成正常推进）；`task-pause <id>` 在下一次页面请求前暂停并释放浏览器，已读的页安全入库；`task-resume <id>` 放回队列，再 `task-run` 从已确认位置继续，不跳页不重复；`task-list` 列最近任务。同一时刻只有一个活着的执行者 |
| `archive-media <帖子> [--limit]` | 把已入库帖子当前可见的图片/附件下载到 `work/local-toolkit-state/media/<tid>/`，索引存库、原文不变。只下本站自己主机的文件（外链 `skipped_external`），权限受限的附件不碰（`skipped_restricted`），单文件 ≤10MB、每次最多 30 个；相同内容按哈希只存一份，文件名只由哈希和核过的扩展名组成。之后 `export` 会把已归档文件复制进导出目录的 `媒体/`，阅读器断网也能看，未归档的明确标出 |
| `recognize-media <帖子> [--limit]` | 对已归档的图片做**本机**文字识别（`rapidocr-onnxruntime`，不把图片发给任何第三方）。结果按图片内容哈希单独存放、与原文分开；同一张图不识别两次，低置信度的行丢弃，空白图片记为 `no_text` 不凭空产生内容。搜索时加 `--include-ocr` 才会命中识别文本，这类命中 `matched_in=recognized_text`；导出和阅读器把识别文本放在图片下方并标明"机器识别，非原文" |
| `collect-stripe` / `search` / `site-search` / `thread-detail` / `export` | 面经采集（等价于 `collect Stripe --listing` Stripe 标签页）、搜索与导出 |

`user-profile` 读到的主页 uid 必须和请求一致，否则报 `profile_identity_mismatch`，不会悄悄换成别人；用户不存在 / 隐私限制 / 需要登录 / 挑战页分别是 `profile_not_found` / `profile_restricted` / `profile_requires_login` / `profile_challenge`。页面没有的资料字段为 `null`，不填零。注意：以登录身份访问别人的主页，论坛可能会把你记进对方的「最近访客」。

`unread` 的来源是站点身份接口随身份一起返回的三个计数，也就是工具每次运行本来就在读的那个接口——所以读取本身不会把任何通知标成已读。站点没给或不是整数的计数显示为 `null` 并列进 `missing`，**未知不当作零**；登录失效时 `status=failed`、各计数为 `null`，不当作没有通知。

`browse-board` 只收版面数字编号或本站 `/bbs/forum-<编号>-<页>.html` 地址，别的地址会拒绝。`pagination_complete` 只有在站点确实没有下一页、且没有因为上限丢结果时才为 true——**读到上限不等于版面没有更多帖子**，那种情况看 `truncated_reason`（`result_limit` 或 `page_limit`）和 `next_url`。置顶帖在每页都会重复出现，跨页按 `tid` 去重，只算一次。

每日原始结果存在本机 `work/local-toolkit-state/latest-daily.json`，可能含私人信息，不要整段贴到 Issue。

<a id="mcp"></a>

## MCP 接入

先跑一次 `检查.sh --sync`（Windows：`检查.cmd --sync`），它会生成工具目录下的 `mcp.config.json`——包含本机路径、不含密码、不入库。把其中 `1point3acres-local` 这一项合并进你 MCP 客户端的配置即可；这是本地 stdio 服务，不需要公开 URL。

两个常见客户端的一行注册（把两个路径换成 `mcp.config.json` 里生成的那两个）：

```bash
claude mcp add --scope user 1point3acres-local -e PYTHONUTF8=1 -- <venv 的 python.exe> <工具目录>/mcp_server.py
```

```bash
codex mcp add 1point3acres-local --env PYTHONUTF8=1 -- <venv 的 python.exe> <工具目录>/mcp_server.py
```

`claude mcp list` 会做一次握手并显示 `✔ Connected`；`codex mcp list` 显示 `enabled`。注册信息写在各客户端自己的用户配置里（`~/.claude.json`、`~/.codex/config.toml`），含本机绝对路径，不进仓库。

连接后可让客户端调用 `interviews_search`（query 传空字符串）验证：应返回 `records` 和 `stats`，新库为空是正常的，这一步不访问网站。`daily_run`、`stripe_collect` 会执行真实业务，确认要做时再调。可用工具名以 `architecture.json` 的 `public_tools` 为准。

<a id="troubleshooting"></a>

## 常见问题

| 现象 / 错误 | 处理 |
|---|---|
| `account_not_configured` / `invalid_local_account_config` | 检查 `work/local-toolkit-state/account.json`：UTF-8、`username`+`uid`（可选 `schedule_mode`、`schedule_time`、`schedule_timezone`、`checkin_mood_random`）、uid 为正整数、`checkin_mood_random` 必须是布尔值 |
| `login_required_credentials_not_configured` | 还没存密码，重跑配置密码那一段 |
| `login_rejected` / `automatic_login_failed` | 核对账号密码与网站账号状态，不要连续重试同一密码 |
| `button_not_ready` | 浏览器没点成按钮，保留失败等下次计划；持续出现附脱敏错误提 Issue |
| `page_challenge_not_resolved` / `..._verification_timeout` | 自动验证没过，本次保留失败，看后续计划是否恢复 |
| `answer_needed` | 题库没这道题，按首次运行那节补答一次 |
| `another_task_is_using_the_browser` | 有任务在用浏览器，等它结束，别同时开第二个 |
| `chrome_profile_busy_or_start_failed` | 检查 Chrome 是否安装、专用任务是否还在跑，别杀掉所有 Chrome |
| `browser_connection_lost` | 浏览器连接中途断了（多为运行时休眠或 Chrome 被关）；已自动重开一次，持续出现再看 Chrome 与系统日志 |
| `daily_run_timeout` | 单次每日运行超过截止，已强制结束并重试一次；反复出现说明网站或网络持续卡住，附脱敏的 status / error 提 Issue |
| `consistency_check_failed` | 跑 `检查.sh`，按提示修；派生文件过期时加 `--sync` |
| 自动计划没跑 | 确认计划已启用、指向本地工具目录、电脑当时可用；`not_due` 不是故障 |

提 Issue 时给出系统 / Python / Chrome 版本、`git rev-parse --short HEAD`、用到的命令和脱敏后的 `status` / `error`，先搜有没有相同 Issue，不要贴账号配置或完整 JSON。

<a id="maintenance"></a>

## 更新与维护

`work/` 目录保存运行环境、加密凭据、数据库、专用 Chrome 会话和每日历史，都不入库；加密凭据与本机绑定，换电脑要重新配置，不能只拷贝密文。

**更新源码**：先暂停每日计划，在仓库根目录确认 `git status --short` 干净（有自己的改动先处理，别用硬重置覆盖），再 `git pull --ff-only`，然后重装依赖并跑 `检查.sh --sync`，通过后恢复计划。

改代码后统一跑 `检查.sh`（完整离线检查与回归）和 `检查.sh --sync`（重建生成文件后再检查）。题库映射源自 eagleoflqj/p1a3_script（原作者 Liumeo）。

### 提交中断恢复

签到确认后，从 30–70 秒均匀抽取一次等待时间，把确认时间、间隔和截止时间写入当日计划。答题准备完成后只等待剩余部分；重启、补答复用截止时间，已经过去则不再等待。原有待确认签到仍保留保护，独立答题以首次恢复观察作为保守计时起点。网络处理可能使实际间隔更长。只读查询不等待；等待跨站点日时停止提交，系统时钟大幅回拨时返回 `daily_clock_changed`，留待后续正常触发恢复。

签到和答题点击前先提交本机数据库记录，存储失败则不点击。进程在提交后中断时，下一次只查询完成状态和奖励；未收到回执不能证明未执行，不自动重交。明确的 `button_not_ready` 表示没有点击，可以在下次重试。记录更新按同一 run_id 保存，不重复计数。
