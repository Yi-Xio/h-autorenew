# Auto Hax

原项目：[fscarmen3/h-autorenew](https://github.com/fscarmen3/h-autorenew)

做了以下修改：

- 为续期任务增加受控执行入口，隔离原始日志，停止向 GitHub 上传原始日志和页面截图；Telegram 保留文字报告和经校验的最终 VPS 信息页截图。
- 工作流限定在 `master` 运行，引用 `renewal` 环境，并按步骤传入所需凭据。
- 到期日记录改存到 Actions 缓存，保留启动前预筛和网站实际判断，停止向仓库自动提交状态文件。
- 增加每账号一份中文 Summary，分别显示续期、通知和缓存结果，将耗时、重试及失败位置放入折叠详情。
- 增加测试，覆盖公开输出、异常处理、通知返回和缓存恢复等情况。

自动续期 hax VPS 订阅的工具，通过浏览器自动化完成登录、验证、续期全流程，并支持 Telegram 通知。

同时支持 Woiden。本 Fork 的 `upstream` 分支保存上游原版，`master` 分支维护修改后的运行版本。以下部署说明适用于 `master`。

## 按需阅读

- 首次部署：[准备账号凭据](#准备账号凭据) → [配置 GitHub Actions](#配置-github-actions) → [首次运行与验收](#首次运行与验收)。
- 了解运行结果：[查看 Summary](#查看-summary)和[到期日缓存与两层判断](#到期日缓存与两层判断)。
- 日常管理：[调度与运行维护](#调度与运行维护)、[同步上游](#同步上游)和[本地检查](#本地检查)。
- 了解公开范围和限制：[隐私与运行边界](#隐私与运行边界)。

## 工作原理

1. Actions 为账号生成数字序号矩阵，每个账号使用独立的临时 Ubuntu runner。
2. 从缓存读取本平台、本账号的到期日，判断是否需要启动续期脚本。
3. 需要检查时，安装运行依赖、连接 WARP，再通过 DrissionPage 浏览器和 Telegram Widget 登录网站。
4. 读取 VPS 信息，根据网站实际到期日判断是否续期；需要时处理 Turnstile、算术验证码和 reCAPTCHA。
5. 通过 Telegram Bot 发送文字报告及可用的最终 VPS 信息页截图，并按需保存可靠状态。
6. 在 GitHub Summary 中分别展示续期、通知和缓存结果。

浏览器、Python 3.13、Chromium、Xvfb、ffmpeg 和官方 WARP client 均由工作流在 runner 中准备。本机无需安装整套续期环境或长期运行程序。

## 准备账号凭据

### BATCH 格式

每个账号使用六个字段，字段之间用半角逗号分隔：

```text
phone,tg_bot_token,tg_chat_id,tg_api_id,tg_api_hash,tg_session
```

| 字段 | 说明 |
| --- | --- |
| `phone` | Telegram 手机号，格式为 `+国家码-号码` |
| `tg_bot_token` | 发送通知的 Bot Token |
| `tg_chat_id` | 接收通知的 Chat ID |
| `tg_api_id` | Telegram 应用 API ID，填写正整数 |
| `tg_api_hash` | Telegram 应用 API Hash |
| `tg_session` | 对应个人 Telegram 账号的 StringSession |

运行配置要求六字段均非空。同一个手机号不要重复配置；多个账号用半角分号 `;` 分隔。下面均为占位示例，需替换尖括号内的内容：

```text
+1-5550001234,<Bot Token>,<Chat ID>,<API ID>,<API Hash>,<Session>
```

```text
+1-5550001234,<Bot Token 1>,<Chat ID 1>,<API ID 1>,<API Hash 1>,<Session 1>;+1-5550005678,<Bot Token 2>,<Chat ID 2>,<API ID 2>,<API Hash 2>,<Session 2>
```

在 [my.telegram.org](https://my.telegram.org) 的 **API development tools** 中申请应用凭据，参考 [Telegram 官方说明](https://core.telegram.org/api/obtaining_api_id)。通知机器人由 [BotFather](https://core.telegram.org/bots/features#botfather)管理；先与自己的机器人建立会话，再通过 [getUpdates](https://core.telegram.org/bots/api#getupdates) 等可信方式取得接收会话的 `message.chat.id`。

个人 Session 用于登录，Bot Token 用于通知，两者不能互相替代。每个个人账号分别生成自己的 Session。

### 生成 Telegram Session

在可信本机克隆源码后进入源码仓库根目录，先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。本节命令只运行 Telegram Session 工具，不需要 WARP 或浏览器；uv 按本次命令提供 Telethon 依赖，参考[脚本运行说明](https://docs.astral.sh/uv/guides/scripts/#running-a-script-with-dependencies)。

先准备前五字段，末尾保留逗号，让第六字段为空：

```text
+<国家码>-<手机号>,<Bot Token>,<Chat ID>,<API ID>,<API Hash>,
```

macOS / Linux 的 Bash、Zsh：

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

输入配置后，脚本在同一进程中询问验证码和两步验证密码；无两步验证时按提示回车。每次处理第一个非空账号，多账号分别运行。

工具的交互输入会显示在终端，输出也包含完整的 `BATCH` 和可复用 Session。只在可信终端操作，不在公开 Actions 中生成或记录这些内容。保存到 Secret 时，只复制六字段值，不带输出中的 `BATCH=` 或外层引号。

## 配置 GitHub Actions

### 分支与环境

1. 在自己的 Fork 中准备包含这些修改的 `master` 分支，将仓库默认分支设为 `master`。
2. 打开 **Settings → Environments**，创建名为 `renewal` 的环境。
3. 在 **Deployment branches and tags** 中选择 **Selected branches and tags**，仅添加 **Branch** 类型的 `master`，不要添加同名 Tag 规则。
4. 在该环境的 Secrets 中保存下表配置。清理同名 Repository Secrets，以及其他会向未改造分支提供这些凭据的来源。
5. 配置完成后，到 **Actions** 启用实际需要的工作流。只使用 Hax 时启用 **Hax Auto Renew** 即可。

| Environment Secret | 必填 | 说明 |
| --- | --- | --- |
| `BATCH` | 是 | 完整六字段账号配置，多账号以分号分隔 |
| `PROXY_URL` | 否 | 代理 URL，运行时映射到程序的 `PROXY_CONTENT` |
| `SOCKS_PORT` | 否 | 本地 SOCKS 监听端口，默认 `10808` |
| `TWOCAPTCHA_API_KEY` | 否 | Woiden 可选的验证码服务凭据 |

公开仓库可使用上述环境功能，配置依据见 [GitHub 环境文档](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)。工作流中的 `environment: renewal` 仅引用环境，不会自动配置分支限制；需要手动完成以上设置。本方案无需添加每次运行都要人工批准的 reviewer。

两份工作流只允许 `refs/heads/master` 运行。日常续期不需要代码写入权限或额外 PAT；`actions: write` 用于清理旧运行记录。

### 可选代理

配置 `PROXY_URL` 后，程序通过 [gen_singbox_config.py](gen_singbox_config.py) 处理代理。支持 VLESS、VMess、Trojan、Shadowsocks、Hysteria2 / HY2、TUIC、AnyTLS 和 SOCKS5；具体参数支持以解析代码为准。

代理属于登录与浏览器访问的可选配置，最终续期提交仍依赖 runner 上的系统 WARP。工作流会准备 WARP，不能只靠设置代理替代它。

每台 Hax VPS 的首次续期尝试先核实 WARP 连接和有效的直连 IPv4；已可用时复用当前出口，不强制重新注册，也不计为换 IP。失败恢复中明确要求更换出口的分支，仍必须取得与轮换前不同、且本轮未用过的出口；尝试耗尽后停止当前续期，不用原出口继续提交。

WARP 注册失败、连接未核实、出口查询失败和未取得新出口分别报告。出口查询拒绝 HTTP 错误、空响应、网页和无效 IP；轮换前后统一查询直连 IPv4，不混用浏览器代理出口。这个判断不代表 Hax 页面已经可用，登录、业务页面及续期结果仍须独立核实。Woiden 的 WARP 逻辑保持原状。

开发者可在已安装并启动 WARP 服务的隔离 Linux 环境中运行 `python -m tests.live.warp --mode ready`，或用 `--mode rotate --attempts 3` 核实严格换出口。该入口不读取账号配置、不执行续期，只输出固定状态和计数；未取得新出口时返回非零。示例回归位于 `tests/regression/test_warp.py`，现有其他回归仍通过原入口发现，整体测试目录整理留给认证工程。

## 首次运行与验收

1. 打开 **Actions → Hax Auto Renew** 或 **Woiden Auto Renew**，点击 **Run workflow**，选择 `master`。
2. 打开本次运行的 **Summary**，核对账号任务、执行结果及 Telegram 通知。
3. 对实际执行续期的 VPS，重新打开站方面板，确认 **Valid until** 确实向后延长。
4. 再手动运行一次，检查缓存恢复和有效期充足时的跳过是否正常。
5. 确认通知和缓存结果符合预期后，再依赖定时任务日常运行。

启用工作流后，YAML 中的定时计划也会生效。绿色状态可能表示本轮无需续期；任务退出 0、WARP 连通或通知发送成功，都不能单独证明 VPS 已续期。

### 查看 Summary

每个账号生成一份中文摘要，主要结果直接显示，运行详情可折叠展开。

| 检查项 | 如何理解 |
| --- | --- |
| 续期 | 区分已延长、无需续期、失败、待核实和无可用 VPS；VPS 按台计数，账号检查失败和无可用 VPS 按账号计数 |
| Telegram 文字 / 截图 | 分别显示接口接受的发送次数和失败次数；没有生成可发送截图不算发送失败 |
| 缓存读取 | 区分命中、缺失、无效和恢复失败 |
| 到期日保存 | 区分已保存、无需更新、无可靠新值、失败和待确认；只有确认本轮缓存存在才显示已保存 |
| 运行详情 | 查看最后阶段、已知失败位置、脚本耗时、续期重试及 WARP 次数 |

通知或缓存失败单独标注，不改写网站实际到期日是否延长的事实。通知成功表示发送接口接受请求，不保证接收方已经阅读。

续期失败、结果待核实、无效或缺失报告会使续期步骤失败。runner 未启动或被强制终止时，摘要无法保证生成，应同时查看 GitHub 自身的作业和步骤状态。

## 到期日缓存与两层判断

**Actions 先根据缓存判断是否需要启动续期脚本；脚本启动后，仍根据网站实际到期日判断是否真正执行续期。**

- 每个平台先生成数字账号矩阵，各账号 job 都先恢复自己的状态，再做预筛；即使全部跳过，也会执行缓存恢复。
- 有效期充足时，不安装运行依赖、不启用 WARP，也不启动浏览器；账号 job 本身仍会运行。
- 缓存缺失、损坏或恢复失败时，启动脚本检查网站。`NOVPS` 表示上次确认无可用 VPS，也需要重新检查现状。
- 只保存本轮可靠日期或明确的无 VPS 状态；没有可靠新值时不覆盖旧记录，内容不变时不重复上传。
- 不再读取或回写仓库里的到期日文件，不生成更新到期日的 Git 提交。

状态按平台和账号隔离。缓存使用不透明账号标识，账号调整顺序不改变缓存归属；更换 Session 后会冷启动。缓存中的标识和到期日不是私密信息，不应在缓存中放入凭据、日志或截图。

GitHub 会清理超过 7 天未访问的缓存，这不是创建满 7 天就过期。正常调度会持续访问当前账号记录；缓存被清理或手动删除后，下轮通过网站检查重建。规则和访问边界见 [GitHub 缓存说明](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)。

## 调度与运行维护

| 工作流 | cron，UTC | 北京时间 | 剩余有效期达到以下天数时跳过 |
| --- | --- | --- | --- |
| Hax Auto Renew | `8 */12 * * *` | 每天 08:08、20:08 | 5 天 |
| Woiden Auto Renew | `32 */8 * * *` | 每天 00:32、08:32、16:32 | 2 天 |

- 调整门槛时，同步修改工作流的 `SKIP_DAYS` 与对应的 `HAX_MIN_VALID_DAYS` / `WOIDEN_MIN_VALID_DAYS`；站点实际可续期窗口以真实页面为准。
- Hax / Woiden 使用按配置序号共享的账号并发组。调整账号顺序前，先等待进行中的任务结束。
- 同一平台由账号 #1 清理旧运行记录，即使该账号预筛跳过也会尝试执行。每轮最多检查 100 条候选记录，保留当前运行、未完成记录和 24 小时内记录；需要留存排查信息时及时记录不含机密的结论。
- 定时任务使用默认分支，GitHub 可能延迟或丢弃计划任务。公开仓库连续 60 天没有仓库活动时，定时工作流会被停用；不要把定时执行或缓存读写当作保活保证。定期人工检查 Actions，停用后按提示重新启用。依据见 [schedule 事件说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

### 同步上游

在 GitHub 将原作者的 `main` 同步到本 Fork 的 `upstream`。自己的运行版本继续使用 `master`。

当本地修改已提交并推送、工作区干净时，在源码仓库根目录执行：

```bash
git fetch origin
git switch master
git merge --ff-only origin/master
git merge --no-commit --no-ff origin/upstream
```

本地只需要指向自己 Fork 的 `origin`。合并后处理冲突，检查上游是否重新引入敏感输出、截图上传、凭据使用或旧状态回写，完成验证后再提交并推送 `master`。Secrets 不随 Git 合并替换，但新代码如何使用它们仍需审阅。

## 本地检查

以下命令在源码仓库根目录执行，需要已有 uv、Ruff、Pyright 和 actionlint：

```bash
uv run --no-project --python 3.13 python -m unittest discover -v
uv run --no-project --python 3.13 ruff check --no-cache scripts tests
uv run --no-project --python 3.13 pyright scripts tests
actionlint .github/workflows/hax-renew.yml .github/workflows/woiden-renew.yml
bash -n scripts/setup_warp.sh
shellcheck scripts/setup_warp.sh
```

最后一项另需 ShellCheck。测试使用假凭据、接口和 worker 替身、模拟缓存，不登录真实账号或安装系统 WARP。它们不能代替真实 Chromium、Telegram、WARP、缓存服务和站点续期验收。

完整项目仍有上游历史静态检查问题，维护时应与基线比较，不为本次功能全量格式化或重写原脚本。

## 隐私与运行边界

- 公开续期入口只发布账号序号、固定阶段、结果计数与耗时，不转发 worker 的原始标准输出和错误输出。原主脚本仍保留上游诊断，公开运行请使用 `python3 -m scripts.actions_runtime hax` 或 `python3 -m scripts.actions_runtime woiden`。
- 不向 GitHub 上传原始日志和页面截图。临时页面、代理配置及 OCR 图片在内部处理；Telegram 保留文字报告和经页面身份检查的最终 VPS 信息页截图，登录与验证码诊断截图不生成。
- Secrets 用于保存凭据，不应把真实 Session、Token、验证码、代理配置写入仓库、Issue 或公开日志。截图和 Telegram 详细报告可以包含 VPS 地址及账号识别信息，应发送到自己的接收会话。
- Telegram 自动确认仍沿用上游逻辑，尚未完成站点与本次授权请求的关联修复；原有完整重试语义也未整体重写。
- 完整续期流程当前以 GitHub 托管 Ubuntu 为部署环境，依赖系统 WARP、Linux 进程控制及浏览器。Windows 原生续期和完整 Docker 部署尚未实现；Windows 可通过浏览器使用 GitHub Actions，或按前文准备 Session。

## 源码入口

| 路径 | 职责 |
| --- | --- |
| [hax-renew.py](hax-renew.py)、[woiden-renew.py](woiden-renew.py) | 浏览器登录、验证码、续期、通知和 WARP 控制 |
| [scripts/actions_runtime.py](scripts/actions_runtime.py) | 公开执行入口、私有临时目录及进程清理 |
| [scripts/public_report.py](scripts/public_report.py)、[scripts/job_summary.py](scripts/job_summary.py) | 受控报告、结果判定和中文摘要 |
| [scripts/workflow.py](scripts/workflow.py)、[scripts/state.py](scripts/state.py) | 账号矩阵、缓存预筛和状态保存 |
| [scripts/accounts.py](scripts/accounts.py)、[scripts/telegram.py](scripts/telegram.py) | 账号配置、公开标识和通知返回校验 |
| [scripts/setup_warp.sh](scripts/setup_warp.sh) | 官方 WARP client 安装和连接检查 |
| [setup_tg_session.py](setup_tg_session.py) | 可信终端中生成 Telegram Session |
| [gen_singbox_config.py](gen_singbox_config.py) | 代理解析与配置生成 |
| [tests/](tests/) | 假数据行为测试 |
| [requirements.txt](requirements.txt) | 运行依赖约束，保留原有管理方式 |
| [LICENSE](LICENSE) | MIT 许可证 |
