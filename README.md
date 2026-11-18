# Auto Hax

原项目：[fscarmen3/h-autorenew](https://github.com/fscarmen3/h-autorenew)

做了以下修改：

- 为续期任务增加受控执行入口，隔离原始日志，停止向 GitHub 上传原始日志和页面截图；Telegram 保留文字报告和经校验的最终 VPS 信息页截图。
- 工作流限定在 `master` 运行，引用 `renewal` 环境，并按步骤传入所需凭据。
- 到期日记录改存到 Actions 缓存，保留启动前预筛和网站实际判断，停止向仓库自动提交状态文件。
- 增加每账号一份中文 Summary，分别显示续期、通知和缓存结果，将耗时、重试及失败位置放入折叠详情。
- 增加测试，覆盖公开输出、异常处理、通知返回和缓存恢复等情况。
- Hax 改用预先准备的网站授权和 Telegram Web 状态，增加导出、账号数组打包与检查工具；先核对已有授权账号，再建立本轮 Hax 会话，继续使用原有续期操作。
- 提供 Docker 凭据准备、示例回归与真实续期入口，本地机密统一放在被忽略的 `.local/`。

自动续期 hax VPS 订阅的工具，通过浏览器自动化完成登录、验证、续期全流程，并支持 Telegram 通知。

同时支持 Woiden。本 Fork 的 `upstream` 分支保存上游原版，`master` 分支维护修改后的运行版本。以下部署说明适用于 `master`。

## 按需阅读

- 首次部署：[准备账号凭据](#准备账号凭据) → [配置 GitHub Actions](#配置-github-actions) → [首次运行与验收](#首次运行与验收)。
- 了解运行结果：[查看 Summary](#查看-summary)和[到期日缓存与两层判断](#到期日缓存与两层判断)。
- 日常管理：[调度与运行维护](#调度与运行维护)、[同步上游](#同步上游)和[本地检查](#本地检查)。
- 了解公开范围和限制：[隐私与运行边界](#隐私与运行边界)。

## 工作原理

1. Actions 为 Hax 账号生成匿名 ID 和展示序号，每个账号使用独立的临时 Ubuntu 24.04 runner。
2. 从缓存读取本平台、本账号的到期日，判断是否需要启动续期脚本。
3. 需要检查时，安装运行依赖、连接 WARP；Hax 浏览器在访问网站前注册内置页面规则，再复用已经批准的网站授权完成登录，并恢复 Telegram Web。Woiden 继续使用原有认证方式。
4. 读取 VPS 信息，根据网站实际到期日判断是否续期；需要时处理 Turnstile、算术验证码和 reCAPTCHA。
5. 通过 Telegram Bot 发送文字报告及可用的最终 VPS 信息页截图，并按需保存可靠状态。
6. 在 GitHub Summary 中分别展示续期、通知和缓存结果。

Hax 工作流使用 Ubuntu 24.04 runner 预装的 Google Chrome，并准备 Python 3.13、Xvfb、ffmpeg 和官方 WARP client。Woiden 工作流沿用 Chromium。本机无需安装整套续期环境或长期运行程序。

## 准备账号凭据

### Docker 内准备与实测

本机需要 Git、Bash、Docker Engine 与 Compose，以及浏览器。Docker Desktop 可直接使用；镜像明确使用 Ubuntu 24.04 / linux/amd64、Python 3.13.18、Chrome 和官方 WARP。首次构建需要下载运行依赖，后续复用构建缓存。ARM64 使用 amd64 模拟；Linux 引擎须支持该架构。Windows 原生脚本未验证，可在具备 Docker 的 Linux / WSL 环境中评估。

从源码仓库根目录执行两步：

```bash
./tests/live/prepare.sh
./tests/live/run.sh
```

第一步会启动只绑定 `127.0.0.1` 的容器浏览器桌面，在终端显示访问链接。按提示填写账号标识符和手机号后，Chrome 先创建 Hax 空白标签页，注册内置页面规则后打开 Hax，再另开 Telegram Web。随后在桌面中**手动**登录同一个账号，完成必要的网站授权。保留两个业务页面，回到终端按回车，脚本读取最小状态并核对账号，再提示输入共享通知 Bot Token 和 Chat ID。本地终端中的这些交互输入均可见；完成后只报告固定结果，不额外打印完整配置。一个浏览器资料目录只对应一个账号；脚本不会自动输入验证码、点击聊天 Confirm 或申请新授权。

可以在同一轮继续准备多个账号，也可以以后重新运行第一步新增账号。账号标识符使用任意非空字符串，例如 `hax.co.id.us`、`香港 主机 / 备用`；支持中文、空格和符号，首尾空白原样保留，按完整字符串区分账号。标识符保存在配置的 `id` 字段中，长期保持稳定，不因排序而修改；整体配置仍受 48 KB 容量限制。更新已有标识符时必须输入 `UPDATE` 确认；全部新配置校验通过后替换，旧版本保留在项目内私有归档中。请在可信本机使用此桌面，用完后退出准备入口；同一 Web 授权不能同时运行准备浏览器和测试浏览器。

第二步直接在独立容器中执行**真实续期并发送通知**，默认不运行通用回归；开发者回归使用 `./tests/regression/run.sh` 独立触发。默认逐个处理所有已配置账号，可用 `--account-identifier 'hax.co.id.us'` 限定范围；带空格或特殊符号时使用引号，以 `-` 开头时使用 `--account-identifier='原标识符'`。完整实测主动绕过缓存预筛，并将网站跳过门槛临时设为 366 天，以验证实际提交；生产默认仍为 5 天。未真正续期、缺少可靠反馈、通知失败或没有保存可靠状态均不会算作完整实测通过。成功后另用全新浏览器检查原凭据是否还能复用。

两步共用镜像；WARP 只运行在实测容器的独立桥接网络，使用 `NET_ADMIN` 和 `/dev/net/tun`，没有 host 网络、宿主 D-Bus 或 Docker socket 挂载。宿主无需安装项目 Python、Chrome、OCR 或 WARP 依赖。容器退出后清理临时会话和进程，不把原始日志、聊天、OCR 图片或页面截图导回宿主。通知 Bot 仍可发送文字及经校验的最终 VPS 信息页截图。

Docker 与 Hax Actions 共用项目内的 `scripts/hax_rules.js`。规则仅在 `https://hax.co.id` 生效，在网页脚本执行前注册，精确处理已确认的 `adsbygoogle.js` 广告探测 GET 请求，避免广告服务不可达时网站清空页面；同时阻止包含已知拦截提示的 `load` 回调。其他 URL、方法、请求体和事件使用浏览器原有行为。准备端、正式认证和 WARP 重建后的 Hax 标签页均使用同一注册入口，不再自动安装 AdGuard 或等待商店下载。已有浏览器资料中的扩展设置由用户管理。

这套规则用于 Hax 页面兼容，不提供完整广告过滤。注册失败与网站实际广告拦截分别报告，不归为登录态失效；网站成功反馈仍由独立的续期结果检查核实。规则随项目代码发布，网站检测变化时需复核规则及测试；无网回归使用本地提供的示例页面。

WARP 重建后的页面检查核对目标来源、路径、登录标识和业务控件。全页尚未加载完成时，只有 `DOMContentLoaded` 已结束、规则标识一致且上述条件通过，才可以继续，不必等待无关资源全部完成；文档或延迟脚本仍未准备完、业务条件缺失时继续等待，未核实则失败。

报告保留最近六次固定状态，包括页面类别、加载阶段、登录标识、表单控件和规则是否存在。Actions 输出最后一次检查，本地回执单独保存在 `navigation` 字段；不包含完整 URL、表单值、Cookie、聊天或页面原文。诊断信息不代替业务成功证据。

认证和验证码的最后检查另存于 `checks`。认证区分网站组件、官方授权组件、已有授权查询、回调和 Telegram Web 业务界面；验证码区分人机验证、音频处理、算术求解和网站最终拒绝。每项只有固定的 `step` 和 `result`，正式入口输出相同分类，不记录答案、业务码、音频或异常原文。`client_loading` 表示 Telegram Web 客户端界面尚未就绪，`peer_loading` 表示固定业务会话尚未显示，均不直接认定凭据失效。

reCAPTCHA 在原有五秒查找预算内等待 iframe 和复选框就绪，分别记录控件未出现、仍在加载和读取异常。已检测到验证码但求解失败、依赖缺失或无法检查页面时，不会继续填写业务码和点击最终续期按钮；本地验证与 Actions 使用相同检查。

`--mode readonly` 也会先准备 WARP，再通过正式入口检查认证，不提交续期或修改到期日。这样单独检查及成功后的凭据复用检查均采用与真实续期一致的网络准备条件；只读通过仍不能代替完整续期验收。只读回执保留正式入口的固定 `reason` 和认证 `checks`，可以区分客户端尚未就绪、业务会话尚未显示及明确的登录态失效；它使用独立结果文件，不覆盖正常续期报告。

运行时先恢复 Telegram Web 并核实固定业务会话，再复用 Hax 已有授权，之后执行原有续期步骤。Telegram 尚未就绪时不会开始 Hax 授权查询；消息基线仍在网站请求业务码前建立，不把准备期间的旧消息当作本轮验证码。

### 项目内私有数据

```text
.local/
├── secrets/
│   ├── account-<SHA-256>/telegram.json  # 最小 Telegram Web K 状态
│   ├── account-<SHA-256>/hax.json       # Hax 第 2 版网站授权状态
│   ├── manifest.json             # 按账号引用两份状态的清单
│   ├── accounts.json             # RENEWAL_ACCOUNTS 的完整值
│   ├── notification-token.json   # 一个 JSON 字符串
│   ├── notification-chat.json    # 一个 JSON 字符串
│   └── archive/                  # 更新前的私有版本
├── browser/                      # 准备端按账号隔离的浏览器资料
├── state/                        # 本地模拟缓存及互斥锁
└── results/                      # 每轮、每账号的匿名摘要和固定结果
```

`.local/` 同时被 Git 和 Docker 构建排除，生成前核对已忽略且未跟踪。目录和文件分别使用 `0700`、`0600`；符号链接、不安全权限和意外覆盖会被拒绝。Docker 镜像与构建缓存由 Docker 自身管理。克隆仓库不会包含任何真实凭据，新机器需重新运行准备入口。

浏览器资料、凭据和实测结果使用 `account-` 加标识符 UTF-8 内容的完整 SHA-256 作为目录名，由脚本统一生成；`/`、`..`、控制字符或较长名称不会直接成为路径。原标识符保留在私有清单中。公开 Actions 矩阵及并发分组使用账号认证材料派生的匿名键，不输出原标识符。

Telegram 导出不包含姓名、聊天历史、其他账号或临时授权键。Hax 文件只保存登录组件的既有授权 Cookie，不保存短效签名、PHPSESSID 或 cf_clearance；旧 Hax 第 1 版文件需重新导出。脚本在每次运行时重新核对身份和受保护页面；离线格式校验不保证授权在线有效。

### 检查、续期与缓存

```bash
# 离线检查已保存的账号与通知配置，不访问 Telegram
./tests/live/prepare.sh --check

# 正式只读认证检查；失败时允许通知 Bot 报告，个人会话不写入
./tests/live/run.sh --mode readonly --account-identifier 'hax.co.id.us'

# 按默认门槛恢复本地缓存并判断是否续期，可能仅检查或跳过
./tests/live/run.sh --mode policy --account-identifier 'hax.co.id.us'

# 将文件值仅在容器内部转换为 Actions 同名字段，实际续期
./tests/live/run.sh --input actions --account-identifier 'hax.co.id.us'
```

本地入口复用正式配置校验、账号选择、续期入口、缓存预筛和 Summary 生成，不维护第二套续期流程。`renew` 是默认的完整实测模式；`readonly` 和 `policy` 分别报告检查与跳过，不能作为完整续期成功。结果位于 `.local/results/<轮次>/<派生目录名>/`，其中 `summary.md` 可直接阅读，`receipt.json` 记录账号标识符、固定结果、计数、通知状态及本地保存情况；续期后凭据复用检查另记为 `<派生目录名>-reuse/`。

本地只模拟缓存文件的恢复与保存，不调用 GitHub 缓存或删除真实运行记录。Summary 会注明这个边界，不能用本地成功证明 GitHub 的 Environment 规则、缓存上传或原生页面均已通过。GitHub 平台仍需发布后单独验收。

互斥锁防止准备和实测并发使用同一授权。正常退出会清理锁；若宿主断电或进程被强制结束，先确认没有相关容器，再移除 `.local/state/operation.lock`。桌面端口默认 6080，可用 `HAX_DESKTOP_PORT=6081 ./tests/live/prepare.sh` 调整。

本地锁不覆盖 GitHub Actions。使用同一份 Web 授权实测前，确认对应云端任务已结束，并避免在测试期间再次触发；需要时暂停定时运行，测试后恢复。手机上的独立 Telegram 会话可以保留。

### 导出与打包工具

`setup_web_sessions.py`、`setup_renewal_accounts.py` 是准备入口复用的通用工具，支持 `--help`；通常无需在宿主直接执行。导出工具用 `--account-identifier` 指定账号标识符，默认使用 `.local/browser/<派生目录名>/`，保存到 `.local/secrets/<派生目录名>/telegram.json` 或 `hax.json`，直接导出拒绝覆盖。Hax 导出会查询官方组件已有授权并核对用户 ID，必须明确确认账号。

清单使用数组，新增账号不新增 Secret 名称。以下为格式示例，手机号和文件内容需在准备流程中实际生成：

```json
{
  "version": 1,
  "accounts": [
    {
      "id": "hax.co.id.us",
      "phone": "+1-5550001234",
      "telegram_web_file": "account-8ade6a36c35f37d05319c2fde591f1f2d0922200e10579307de42161e71e9a44/telegram.json",
      "hax_web_file": "account-8ade6a36c35f37d05319c2fde591f1f2d0922200e10579307de42161e71e9a44/hax.json"
    },
    {
      "id": "hax.co.id.hk",
      "phone": "+1-5550005678",
      "telegram_web_file": "account-03eed44edc50e2fca264cbf60f6c278974136f037ec8d66dcb46bf75e2a68ba5/telegram.json",
      "hax_web_file": "account-03eed44edc50e2fca264cbf60f6c278974136f037ec8d66dcb46bf75e2a68ba5/hax.json"
    }
  ]
}
```

打包工具将两份完整对象嵌入 `accounts.json`，检查身份配对、重复账号和 48 KB 容量；输出默认位于 `.local/secrets/accounts.json`。配置后将该文件**完整内容**填入 GitHub 的 `RENEWAL_ACCOUNTS`；不要填写清单、文件路径或旧 StringSession。共享通知配置各自存为一个 JSON 字符串，填写对应 GitHub Secret 时使用字符串本身，不带 JSON 外层引号。

## Woiden 凭据准备

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

Hax 使用下面的三项 Secret；Woiden 仍单独使用六字段 `BATCH`。两种配置不能混用。

### 分支与环境

1. 在自己的 Fork 中准备包含这些修改的 `master` 分支，将仓库默认分支设为 `master`。
2. 打开 **Settings → Environments**，创建名为 `renewal` 的环境。
3. 在 **Deployment branches and tags** 中选择 **Selected branches and tags**，仅添加 **Branch** 类型的 `master`，不要添加同名 Tag 规则。
4. 在该环境的 Secrets 中保存下表配置。清理同名 Repository Secrets，以及其他会向未改造分支提供这些凭据的来源。
5. 配置完成后，到 **Actions** 启用实际需要的工作流。只使用 Hax 时启用 **Hax Auto Renew** 即可。

| Environment Secret | 必填 | 说明 |
| --- | --- | --- |
| `RENEWAL_ACCOUNTS` | Hax 必填 | 打包工具生成的账号数组 JSON |
| `TELEGRAM_BOT_TOKEN` | Hax 必填 | 共享通知机器人的 Token |
| `TELEGRAM_CHAT_ID` | Hax 必填 | 共享通知接收会话 |
| `BATCH` | Woiden 必填 | 原有六字段账号配置 |
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

开发者可在已安装并启动 WARP 服务的隔离 Linux 环境中运行 `python -m tests.live.warp --mode ready`，或用 `--mode rotate --attempts 3` 核实严格换出口。该入口不读取账号配置、不执行续期，只输出固定状态和计数；未取得新出口时返回非零。WARP 示例回归位于 `tests/regression/test_warp.py`，和其他回归一同由 `./tests/regression/run.sh` 执行。

## 首次运行与验收

1. 打开 **Actions → Hax Auto Renew** 或 **Woiden Auto Renew**，点击 **Run workflow**，选择 `master`。
2. 打开本次运行的 **Summary**，核对账号任务、执行结果及 Telegram 通知。
3. 对实际执行续期的 Hax VPS，核对本轮网站显示 **Your VPS has been renewed until …** 的成功结果，并重新查看信息页的 **Valid until**。当天重复续期时，成功提示与日期不变可以同时成立。
4. 再手动运行一次，检查缓存恢复和有效期充足时的跳过是否正常。
5. 确认通知和缓存结果符合预期后，再依赖定时任务日常运行。

启用工作流后，YAML 中的定时计划也会生效。绿色状态可能表示本轮无需续期；任务退出 0、WARP 连通或通知发送成功，都不能单独证明 VPS 已续期。

### 查看 Summary

每个账号生成一份中文摘要，主要结果直接显示，运行详情可折叠展开。

| 检查项 | 如何理解 |
| --- | --- |
| 续期 | Hax 以本轮站点实际成功提示确认，区分成功、无需续期、失败、待核实和无可用 VPS；VPS 按台计数，账号检查失败和无可用 VPS 按账号计数 |
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

本地真实验收可以临时将 HAX_MIN_VALID_DAYS 设为大于网站实际剩余天数，并从受控入口运行，以绕过脚本跳过判断；生产默认值保持 5。Hax 主要核实本轮提交后的实际成功提示，到期日另行核对；不以点击完成、静态源码话术或内部成功标志代替站点结果。

状态按平台和账号隔离。缓存使用不透明账号标识，账号调整顺序不改变缓存归属；更换 Session 后会冷启动。缓存中的标识和到期日不是私密信息，不应在缓存中放入凭据、日志或截图。

GitHub 会清理超过 7 天未访问的缓存，这不是创建满 7 天就过期。正常调度会持续访问当前账号记录；缓存被清理或手动删除后，下轮通过网站检查重建。规则和访问边界见 [GitHub 缓存说明](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)。

## 调度与运行维护

| 工作流 | cron，UTC | 北京时间 | 剩余有效期达到以下天数时跳过 |
| --- | --- | --- | --- |
| Hax Auto Renew | `8 */12 * * *` | 每天 08:08、20:08 | 5 天 |
| Woiden Auto Renew | `32 */8 * * *` | 每天 00:32、08:32、16:32 | 2 天 |

- 调整门槛时，同步修改工作流的 `SKIP_DAYS` 与对应的 `HAX_MIN_VALID_DAYS` / `WOIDEN_MIN_VALID_DAYS`；站点实际可续期窗口以真实页面为准。
- Hax 按匿名键防止同账号重叠，账号重排不改变会话、缓存或并发归属。账号标识符参与匿名键生成，不要随意更换；修改配置前先等待进行中的任务结束。Woiden 保留原有按序号控制的方式。
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

只运行示例回归，不需要准备真实凭据：

```bash
./tests/regression/run.sh
```

回归测试集中在 `tests/regression/`，使用示例凭据、接口和 worker 替身，以及真实 Chrome 访问本地页面；不登录真实账号或连接系统 WARP。没有发现用例、存在失败或浏览器场景被跳过均返回非零。`tests/live/` 负责真实业务，`tests/docker/` 管理两类入口共用的容器环境。

已具备开发工具时，也可以直接运行与基线比较的静态检查：

```bash
uv run --no-project --python 3.13 ruff check --no-cache .
uv run --no-project --python 3.13 pyright .
actionlint .github/workflows/hax-renew.yml .github/workflows/woiden-renew.yml
shellcheck -x scripts/setup_warp.sh tests/docker/*.sh tests/live/*.sh tests/regression/*.sh
```

完整项目仍有上游历史静态检查问题，维护时应对照相同依赖环境，不为本次功能全量格式化或重写原脚本。示例测试通过不能代替真实业务或 GitHub 服务验收。

## 隐私与运行边界

- 公开续期入口只发布账号序号、固定阶段、结果计数与耗时，不转发 worker 的原始标准输出和错误输出。原主脚本仍保留上游诊断，公开运行请使用 `python3 -m scripts.actions_runtime hax` 或 `python3 -m scripts.actions_runtime woiden`。
- 不向 GitHub 上传原始日志和页面截图。临时页面、代理配置及 OCR 图片在内部处理；Telegram 保留文字报告和经页面身份检查的最终 VPS 信息页截图，登录与验证码诊断截图不生成。
- Secrets 用于保存凭据，不应把真实 Session、Token、验证码、代理配置写入仓库、Issue 或公开日志。截图和 Telegram 详细报告可以包含 VPS 地址及账号识别信息，应发送到自己的接收会话。
- Hax 只复用已经批准的网站授权，核对账号和当前结果后建立站点会话；不发起新的授权、验证码或聊天 Confirm。个人 Telegram Web 只读取当前身份与固定业务会话，不发送个人消息、不点击机器人回调、不提取其他聊天。允许官方客户端自身自动同步，独立通知 Bot 发送业务报告。Woiden 的旧认证和原有完整重试语义不在本次迁移范围内。
- 完整续期流程当前以 GitHub 托管 Ubuntu 为部署环境，依赖系统 WARP、Linux 进程控制及浏览器。已提供本地 Docker 准备和实测入口；Windows 原生续期及长期 Docker 调度部署未实现。

## 源码入口

| 路径 | 职责 |
| --- | --- |
| [hax-renew.py](hax-renew.py)、[woiden-renew.py](woiden-renew.py) | 浏览器登录、验证码、续期、通知和 WARP 控制 |
| [scripts/actions_runtime.py](scripts/actions_runtime.py) | 公开执行入口、私有临时目录及进程清理 |
| [scripts/public_report.py](scripts/public_report.py)、[scripts/job_summary.py](scripts/job_summary.py) | 受控报告、结果判定和中文摘要 |
| [scripts/workflow.py](scripts/workflow.py)、[scripts/state.py](scripts/state.py) | 账号矩阵、缓存预筛和状态保存 |
| [scripts/accounts.py](scripts/accounts.py)、[scripts/telegram.py](scripts/telegram.py) | 账号配置、公开标识和通知返回校验 |
| [scripts/browser_setup.py](scripts/browser_setup.py)、[scripts/hax_rules.js](scripts/hax_rules.js) | Hax 文档前规则注册、定向广告探测处理及已知拦截弹窗识别 |
| [scripts/setup_warp.sh](scripts/setup_warp.sh) | 官方 WARP client 安装和连接检查 |
| [setup_tg_session.py](setup_tg_session.py) | 可信终端中生成 Telegram Session |
| [setup_web_sessions.py](setup_web_sessions.py)、[scripts/web_sessions.py](scripts/web_sessions.py) | 本机 Web 会话导出、私有文件保存和离线校验 |
| [setup_renewal_accounts.py](setup_renewal_accounts.py)、[scripts/renewal_config.py](scripts/renewal_config.py) | 账号数组打包、配置校验和单账号选择 |
| [scripts/web_auth.py](scripts/web_auth.py) | Telegram Web 状态恢复和本轮业务码读取 |
| [scripts/hax_result.py](scripts/hax_result.py) | 只记录本轮真实提交后的页面和弹窗反馈，日期不变不误报失败 |
| [scripts/hax_login.py](scripts/hax_login.py) | 核对 Hax 已有网站授权、建立站点会话和识别页面状态 |
| [gen_singbox_config.py](gen_singbox_config.py) | 代理解析与配置生成 |
| [tests/regression/](tests/regression/)、[tests/live/](tests/live/)、[tests/docker/](tests/docker/) | 示例回归、真实准备与实测、共用容器环境 |
| [requirements.txt](requirements.txt) | 运行依赖约束，保留原有管理方式 |
| [LICENSE](LICENSE) | MIT 许可证 |
