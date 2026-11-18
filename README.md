原项目：[fscarmen3/h-autorenew](https://github.com/fscarmen3/h-autorenew)

做了以下修改：

- 为续期任务增加受控执行入口，隔离原始日志，停止向 GitHub 上传原始日志和页面截图。Telegram 保留文字报告和经校验的最终 VPS 信息页截图
- 工作流限定在 `master` 运行，引用 `renewal` 环境
- 到期日记录改用 Actions 缓存存储，取消自动向仓库提交状态文件以记录日期
- 各账号各新增 Github Action Summary 摘要页暂时续期结果
- 修改登录认证方式。Hax 复用已有的网站授权，Telegram Web 复用已有的登录态。提供本地脚本，手动登录后可按账号导出两份凭据
  - 主要是因 Telegram 风控导致无法创建 Telegram App
- 增加适当的测试

---

# Auto Hax

通过浏览器自动检查并续期 Hax VPS，支持 Telegram 通知。仓库也保留 Woiden 续期工作流。以下说明适用于包含这些改动的 `master` 分支。

- 部署 Hax → [准备 Hax 凭据](#准备-hax-凭据) → [配置 GitHub Actions](#配置-github-actions) → [首次运行与验收](#首次运行与验收)
- 部署 Woiden → [准备 Woiden 凭据](#准备-woiden-凭据) → [配置 GitHub Actions](#配置-github-actions)
- 在本机验证 → [Hax 本地实测](#hax-本地实测)或[本地回归测试](#本地回归测试)
- 排查和维护 → [查看运行结果](#查看运行结果)、[到期日缓存](#到期日缓存)、[调度与同步上游](#调度与同步上游)

## 工作原理

1. GitHub Actions 为每个账号创建独立任务，先从缓存读取到期日。有效期充足时跳过浏览器续期。
2. 需要检查时，任务准备浏览器和 WARP。Hax 先恢复 Telegram Web 并核对固定的 Hax 业务会话，再复用保存的网站授权登录 Hax。Woiden 使用 Telethon 和个人账号的 StringSession。
3. 续期脚本读取 VPS 信息，根据网站实际到期日决定是否续期。Hax 在请求续期码前记录业务会话中的消息基线，只接收本轮的新码。最终提交后，以网站显示的本轮成功提示确认续期。
4. 任务通过通知 Bot 发送报告和符合条件的 VPS 信息页截图，按需保存已核对的到期日，并在 GitHub Actions 的 Summary 展示结果。

Hax 使用 GitHub 托管 Ubuntu 24.04 镜像预装的 Google Chrome。Woiden 工作流在 `ubuntu-latest` 中安装 Chromium。两份工作流均配置 Python 3.13，并在需要启动续期时准备 WARP。 [Hax 工作流](.github/workflows/hax-renew.yml) · [Woiden 工作流](.github/workflows/woiden-renew.yml)

## 准备 Hax 凭据

本地准备和实测需要 Git、Bash、Docker 与 Compose，以及用于打开容器桌面的浏览器。容器镜像使用 Ubuntu 24.04、`linux/amd64`、Python 3.13.18 和 Chrome。ARM64 主机需要具备运行 amd64 容器的能力。首次构建会下载镜像和依赖。 [Docker 配置](tests/docker/compose.yaml)

在源码仓库根目录运行：

```bash
./tests/live/prepare.sh
```

1. 输入账号标识符和手机号。手机号格式为 `+国家码-号码`。账号标识符是你为这个账号取的名字，比如 `hax.co.id.us`。它可以是任意非空的有效文本，脚本按原文匹配，首尾空格也会保留。
2. 打开终端显示的容器桌面链接，在 Telegram Web 和 Hax 页面上手动登录同一个 Telegram 账号，完成 Hax 网站授权。保留这两个页面，回终端按回车导出。
3. 按提示填写通知 Bot Token 和接收会话的 Chat ID。所有 Hax 账号共用这一组通知配置，准备其他账号时可以沿用。手机号和通知配置在本地交互中可见，请使用可信终端。 若还不知道 Chat ID，可先与自己的通知 Bot 建立会话，再从 Bot API 的消息更新中查看 `message.chat.id`。 [创建 Bot](https://core.telegram.org/bots/features#botfather) · [读取消息更新](https://core.telegram.org/bots/api#getupdates)
4. 可以继续准备另一个账号。更新已有账号时，使用原标识符并输入 `UPDATE` 确认。更新前的配置会保存在私有归档内。

桌面只绑定本机的 `127.0.0.1`，默认端口为 `6080`。端口冲突时可用 `HAX_DESKTOP_PORT=6081 ./tests/live/prepare.sh`。导出的 Telegram Web 状态和 Hax 网站授权必须属于同一个账号。旧版 Hax 会话文件需要用当前准备入口重新导出。

### 本地文件与 GitHub Secret

```text
.local/
├── secrets/
│   ├── account-<SHA-256>/
│   │   ├── telegram.json
│   │   └── hax.json
│   ├── manifest.json
│   ├── accounts.json
│   ├── notification-token.json
│   ├── notification-chat.json
│   └── archive/
├── browser/
├── state/
└── results/
```

- `manifest.json` 引用每个账号的两份凭据，`accounts.json` 把凭据打包成完整 JSON。部署时使用 `accounts.json` 的全文作为 `RENEWAL_ACCOUNTS`，上限为 48 KiB。新增账号后更新这个 Secret 即可。
- 通知配置保存在两个 JSON 字符串文件中。配置 GitHub 时填入字符串内容，去掉 JSON 外层引号。
- Git 和 Docker 构建均排除 `.local/`。准备入口会校验目录权限与忽略规则。浏览器资料、凭据和结果目录使用账号标识符派生的目录名，原标识符保存在私有文件中。新机器克隆仓库后，可运行准备入口重新生成凭据。
- 需要单独导出或打包时，可查看 `python3 setup_web_sessions.py --help` 和 `python3 setup_renewal_accounts.py --help`。通常由准备入口调用。 [会话导出](setup_web_sessions.py) · [账号打包](setup_renewal_accounts.py)

## Hax 本地实测

准备好凭据后，在源码仓库根目录选择所需的命令：

| 操作 | 命令 |
| --- | --- |
| 离线检查凭据文件和通知配置的格式 | `./tests/live/prepare.sh --check` |
| 用真实账号按生产到期规则运行一轮 | `./tests/live/run.sh renew` |
| 跳过缓存预筛和本地到期门槛，尝试真实续期 | `./tests/live/run.sh renew --force` |
| 用新浏览器验证登录凭据 | `./tests/live/run.sh check-auth` |

`run.sh` 默认依配置顺序处理所有账号。可重复使用 `--account-identifier 'hax.co.id.us'` 选定账号。标识符包含空格或特殊字符时请加引号，以连字符开头时使用 `--account-identifier='-示例'`。

`renew` 和 `renew --force` 使用与 Actions 相同的续期入口，可发送真实通知。强制实测需要每个所选账号至少确认一次续期成功、通知接口接受文字和截图、且本地保存可靠的到期日。站点仍决定是否接受续期。`check-auth` 会连接 WARP 并验证登录，失败时可以发通知。它只验证登录，结果见本轮的 `receipt.json`。

每次运行在 `.local/results/<轮次>/<派生目录名>/` 保存回执。`renew` 和 `renew --force` 完成摘要生成后，还会写入 `summary.md`。回执包含私有账号标识符、执行方式、结果、可用的检查分类和计数。本地到期日缓存保存在 `.local/state/`，GitHub 环境与远端缓存上传仍需在 Actions 中验收。

实测的 WARP 服务运行在独立容器内。准备和实测共用本地互斥锁。使用同一份 Telegram Web 凭据实测前，先退出准备容器并确认对应的云端任务已结束。本地任务异常中断后如仍有锁，确认相关容器和脚本已退出，再移除 `.local/state/operation.lock`。 [本地实测入口](tests/live/run.sh)

## 准备 Woiden 凭据

Woiden 的 `BATCH` 为每个账号保存六个字段：

```text
phone,tg_bot_token,tg_chat_id,tg_api_id,tg_api_hash,tg_session
+1-5550001234,<Bot Token>,<Chat ID>,<API ID>,<API Hash>,<StringSession>
```

第一行说明字段顺序，配置时仅填写实际账号行。字段用半角逗号分隔，多个账号用半角分号 `;` 分隔。手机号采用 `+国家码-号码`，API ID 填正整数，每个账号分别生成自己的 StringSession。Telegram 应用 API ID 和 API Hash 从 `my.telegram.org` 的 **API development tools** 获取。 [Telegram 应用凭据](https://core.telegram.org/api/obtaining_api_id)

在可信本机安装 uv。从源码仓库根目录运行会话工具，输入前五字段并保留末尾逗号，让第六字段为空。脚本处理第一个非空账号，并交互询问验证码及两步验证密码。终端会显示交互输入和完整 Session，请妥善保存。 [uv 脚本运行说明](https://docs.astral.sh/uv/guides/scripts/#running-a-script-with-dependencies)

macOS 或 Linux 的 Bash、Zsh：

```bash
read -r -s BATCH
export BATCH
uv run --no-project --python 3.13 --with telethon==1.36.0 python setup_tg_session.py
unset BATCH
```

Windows PowerShell：

```powershell
$env:BATCH = Read-Host '输入前五字段，末尾保留逗号'
try {
    uv run --no-project --python 3.13 --with telethon==1.36.0 python setup_tg_session.py
} finally {
    Remove-Item Env:BATCH -ErrorAction SilentlyContinue
}
```

生成后，把完整的六字段账号值填入 `BATCH` Secret，去掉输出中的变量名和外层引号。 [会话生成工具](setup_tg_session.py)

## 配置 GitHub Actions

1. 将包含这些代码的 `master` 分支推送到自己的 Fork，在 GitHub 将 `master` 设为默认分支。GitHub 从默认分支发现定时和手动触发的工作流。 [工作流触发条件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)
2. 打开 **Settings → Environments**，创建 `renewal`。在 **Deployment branches and tags** 中选择 **Selected branches and tags**，只添加一条 **Branch** 类型的 `master` 规则。 [GitHub 环境配置](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
3. 在 `renewal` 的 **Environment secrets** 中配置实际使用的平台，迁移后移除同名 Repository Secrets，确保凭据受环境的分支规则约束。
4. 在 **Actions** 启用需要运行的工作流。只使用 Hax 时启用 **Hax Auto Renew**，Woiden 对应 **Woiden Auto Renew**。

| Environment Secret | 使用方 | 填写内容 |
| --- | --- | --- |
| `RENEWAL_ACCOUNTS` | Hax 必填 | `.local/secrets/accounts.json` 的完整内容 |
| `TELEGRAM_BOT_TOKEN` | Hax 必填 | 通知机器人的 Token |
| `TELEGRAM_CHAT_ID` | Hax 必填 | 通知接收会话的 Chat ID |
| `BATCH` | Woiden 必填 | 一个或多个六字段账号配置 |
| `PROXY_URL` | 两者可选 | 用于登录和浏览器访问的代理地址 |
| `SOCKS_PORT` | 两者可选 | 使用本地 SOCKS 监听时的端口，默认 `10808` |

两份工作流只在 `refs/heads/master` 执行账号任务。续期不需要额外 PAT。工作流的 `actions: write` 用于清理旧运行记录。

### 代理与 WARP

代理由 [代理配置脚本](gen_singbox_config.py)解析，具体协议和参数以该脚本支持的格式为准。配置代理后，最终续期仍使用运行环境中的系统 WARP。

Hax 在续期过程里先核实 WARP 连接和有效的直连 IPv4。出口已可用时直接复用，连接异常时按剩余时间尝试恢复。若浏览器因切换到 WARP 而重建，会恢复本轮 Hax 会话，并重新核对登录状态和续期码页面。

### 首次运行与验收

1. 在所用工作流的 **Actions** 页面点击 **Run workflow**，选择 `master`。
2. 在本轮 **Summary** 检查每个账号的续期、通知和缓存结果。剩余时间充足导致的跳过属于正常结果。
3. 对实际提交的 Hax 续期，以网站本轮显示的 **Your VPS has been renewed until …** 确认成功，并以 VPS 信息页的 **Valid until** 辅助核对日期。同一天再次提交时，成功提示与日期不变可以同时成立。
4. 再运行一轮，检查账号缓存能否恢复，以及符合条件的账号是否被预筛跳过。

## 查看运行结果

| 项目 | 如何理解 |
| --- | --- |
| 续期 | 区分成功或已延长、无需续期、失败、待核实和无可用 VPS。Hax 判断本轮站点反馈，Woiden 的公开结果核对到期日是否延长 |
| 文字与截图 | 分别统计通知接口接受和发送失败的次数。没有生成可发送截图时不计作发送失败 |
| 缓存 | 分别显示读取结果和本轮保存结果。只有核实本轮缓存键可查到，才显示已保存 |
| 运行详情 | 展开查看阶段、耗时、固定失败分类、重试次数和 WARP 统计 |

Hax 在 Telegram 中列出当前 VPS 及账号准备阶段的重试明细。Job Summary 汇总整轮账号，多个 VPS 共用的准备重试只计一次。正常等待与轮询不计入。

临时读不到 Telegram Web 页面时，脚本会在最多 8 分钟的登录恢复预算内继续观察，持续卡住时按间隔重开固定会话。续期码请求可能已经发出时，沿用该请求和消息基线。最终按钮明确未点到时可以再准备一次，点击结果不明时只核对原提交反馈。没有明确反馈的续期标为待核实。

本地回执的 `checks` 和 `navigation` 可显示固定的认证、验证和页面状态，Actions 日志也会输出相应的固定诊断。Telegram 的 `client_loading` 表示客户端未就绪，`peer_loading` 表示固定业务会话尚未显示。续期失败、结果待核实或缺少有效报告时，续期步骤会返回非零。账号任务未创建或者 runner 提前终止时，请直接查看 GitHub 的作业和失败步骤。

## 到期日缓存

**Actions 先根据缓存判断是否启动续期脚本，脚本再根据网站实际到期日判断是否执行续期。**

- 每个账号先尝试恢复自己的缓存。有效期达到门槛时发送跳过通知，省去安装运行依赖和启动浏览器的步骤。
- 缓存缺失、无效或恢复失败时，用网站现状判断。上次记录为无可用 VPS 时也会再次查询。
- 本轮可靠日期和明确的无 VPS 状态可以保存。值未变化时不重复上传，没有可靠观察时保留旧缓存。到期日不再写回 Git。
- 缓存按平台和账号隔离。Hax 在账号重新排序后继续使用原有缓存，修改账号标识符或换用新的 Telegram Web 授权键时使用新的缓存键。
- GitHub 会移除超过 7 天未访问的缓存，缺失的缓存可以在查询网站后重建。 [GitHub 缓存清理规则](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#default-limits)

## 调度与同步上游

| 工作流 | cron，UTC | 北京时间 | 剩余有效期达到以下天数时跳过 |
| --- | --- | --- | --- |
| Hax Auto Renew | `8 */12 * * *` | 每天 08:08、20:08 | 5 天 |
| Woiden Auto Renew | `32 */8 * * *` | 每天 00:32、08:32、16:32 | 2 天 |

调整门槛时，同步修改工作流的 `SKIP_DAYS` 与对应的 `HAX_MIN_VALID_DAYS` 或 `WOIDEN_MIN_VALID_DAYS`。定时时间是计划触发时间，GitHub 繁忙时可以延后或丢弃任务。公开仓库连续 60 天无仓库活动时，计划任务会自动停用。 [GitHub 定时规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

Hax 按匿名账号键控制并发，Woiden 按展示序号控制并发。每个平台由账号 1 在任务结束时尝试清理超过 24 小时的已完成运行记录，每次最多检查 100 条，保留当前任务及未完成任务。

### 同步上游

在 GitHub 先把原作者的 `main` 同步到自己 Fork 的 `upstream`。本地工作区干净，且本地提交已经推送到自己的 Fork 后，执行：

```bash
git fetch origin
git switch master
git merge --ff-only origin/master
git merge --no-commit --no-ff origin/upstream
```

本流程以自己的 Fork 为 `origin`。合并后审查工作流使用凭据、输出日志、上传文件和保存状态的方式，验证通过再提交并推送 `master`。

## 本地回归测试

在源码仓库根目录运行：

```bash
./tests/regression/run.sh
```

回归容器禁用外部网络。用例使用示例凭据、本地页面和服务替身，并用真实 Chrome 验证浏览器行为。没有发现用例、用例失败或有用例被跳过，入口都会返回非零。 [回归入口](tests/regression/run.sh) · [测试代码](tests/regression/)

有开发环境时，可用 Ruff、Pyright 对照同一依赖环境下的基线，工作流可用以下命令检查：

```bash
actionlint .github/workflows/hax-renew.yml .github/workflows/woiden-renew.yml
```

## 隐私与运行边界

- 公开运行通过 `python3 -m scripts.actions_runtime hax` 或 `python3 -m scripts.actions_runtime woiden` 启动。公开日志及摘要只发布受控字段，原始输出、页面和截图留在临时运行环境。
- 文字报告和符合条件的最终 VPS 信息页截图可以包含账号及服务器信息，请发往自己的 Telegram 接收会话。把登录凭据、Bot Token 和代理配置存放在私有目录或 Environment Secrets。验证码只在本轮操作中使用。
- Hax 的个人 Telegram Web 会话只供核对身份和读取固定业务会话，允许客户端自身同步。脚本通过独立通知 Bot 发送业务报告。已有网站授权失效或需要新授权时，重新运行凭据准备入口，手动完成登录和授权。
- Hax 凭据准备与运行共用项目内置的 Hax 页面兼容规则。在打开 Hax 前注册规则，WARP 重建浏览器时重新注册。 [Hax 页面规则](scripts/hax_rules.js) · [浏览器初始化](scripts/browser_setup.py)
- 正式调度入口为 GitHub Actions。本地 Docker 提供 Hax 凭据准备、按需实测和开发回归。Woiden 的会话工具有 PowerShell 用法，完整续期仍面向 Ubuntu runner。

## 源码入口

| 想查看的内容 | 入口 |
| --- | --- |
| Hax 与 Woiden 的账号级执行 | [hax-renew.py](hax-renew.py) · [woiden-renew.py](woiden-renew.py) |
| Hax 单台 VPS 的续期顺序、页面操作和重试记录 | [flow.py](scripts/hax_renewal/flow.py) · [operations.py](scripts/hax_renewal/operations.py) · [retry_report.py](scripts/hax_renewal/retry_report.py) |
| Hax 网站授权、Telegram Web 读码和本轮续期反馈 | [hax_login.py](scripts/hax_login.py) · [web_auth.py](scripts/web_auth.py) · [hax_result.py](scripts/hax_result.py) |
| 账号任务、缓存、运行摘要 | [actions_runtime.py](scripts/actions_runtime.py) · [workflow.py](scripts/workflow.py) · [state.py](scripts/state.py) · [job_summary.py](scripts/job_summary.py) |
| 本地准备、实测和回归 | [tests/live/](tests/live/) · [tests/regression/](tests/regression/) · [tests/docker/](tests/docker/) |

项目使用 [MIT 许可证](LICENSE)。
