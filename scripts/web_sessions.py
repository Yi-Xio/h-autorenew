"""手动登录会话的最小格式、账号配对与私有文件读写。"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict

TELEGRAM_CLIENT = "telegram-web-k"
HAX_CLIENT = "hax-web"
TELEGRAM_ORIGIN = "https://web.telegram.org"
HAX_ORIGIN = "https://hax.co.id"
HAX_LOGIN_ORIGIN = "https://oauth.telegram.org"
HAX_LOGIN_BOT = "LoginHaxBot"
HAX_LOGIN_BOT_ID = "5938743585"
MAX_SESSION_BYTES = 48 * 1024
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_ROOT = PROJECT_ROOT / ".local"

AUTH_REASONS = frozenset(
    {
        "telegram_session_invalid",
        "hax_session_invalid",
        "hax_page_unverified",
        "access_blocked",
        "network_failed",
        "telegram_peer_unverified",
        "renewal_code_ambiguous",
        "renewal_code_unavailable",
        "browser_unavailable",
        "hax_authorization_required",
        "hax_account_mismatch",
        "hax_widget_unverified",
        "hax_callback_unverified",
        "hax_rules_unavailable",
        "adblock_detected",
    }
)


class AuthenticationError(RuntimeError):
    """认证失败仅携带固定原因，不保留凭据、页面或底层错误。"""

    def __init__(self, reason: str):
        if reason not in AUTH_REASONS:
            raise ValueError("invalid_auth_reason")
        self.reason = reason
        super().__init__(reason)


# JSON 和浏览器返回动态结构；在公开接口中统一校验后再使用。
Session = Dict[str, Any]

ERROR_MESSAGES = {
    "invalid_session": "会话格式无效或尚不支持，请重新准备。",
    "invalid_phone": "手机号需使用 +国家码-号码 格式。",
    "account_mismatch": "会话与指定账号不一致。",
    "multiple_accounts": "专用浏览器中存在多个已登录账号，请分别准备。",
    "confirmation_required": "导出 Hax 前，需要确认它属于指定的 Telegram 账号。",
    "not_logged_in": "尚未取得已登录页面，请先手动登录。",
    "page_unverified": "未能核实目标页面，请检查登录状态、网络和站点验证。",
    "browser_unavailable": "无法连接指定专用浏览器，请检查资料目录、调试端口及已打开的业务页面。",
    "dependency_missing": "缺少 DrissionPage，请通过 README 中的 Docker 准备入口运行。",
    "unsafe_file": "请使用已排除 Git 和 Docker 构建的 .local 私有目录，并仅允许当前用户访问。",
    "file_exists": "目标文件已存在，请使用新的文件名，确认后再自行替换。",
    "file_error": "会话文件读写失败。",
    "local_only": "会话导出仅允许在可信本机运行。",
    "invalid_arguments": "参数不完整或无效，请使用 --help 查看说明。",
    "hax_authorization_required": "Hax 的网站登录授权不可用，请先在专用浏览器手动准备。",
    "hax_account_mismatch": "Hax 网站登录组件识别到的账号与指定账号不一致。",
    "hax_widget_unverified": "未能核实 Hax 官方登录组件，请检查页面与网络。",
    "hax_callback_unverified": "Hax 登录回调未通过校验。",
    "hax_rules_unavailable": "无法准备 Hax 页面规则，请检查项目文件和浏览器连接。",
    "adblock_detected": "Hax 广告检测拦截了页面，请检查内置规则是否生效或网站检测是否变化。",
}


class SessionError(ValueError):
    """只携带固定原因，不附带会话值、文件内容或浏览器错误原文。"""

    def __init__(self, code: str) -> None:
        self.code = code if code in ERROR_MESSAGES else "invalid_session"
        super().__init__(self.code)


def phone_digits(phone: str) -> str:
    """将项目的手机号格式转换为 Web K 保存的纯数字形式。"""
    if not isinstance(phone, str) or not re.fullmatch(
        r"\+[1-9][0-9]{0,3}-[0-9]{4,16}", phone
    ):
        raise SessionError("invalid_phone")
    value = phone[1:].replace("-", "")
    if not 6 <= len(value) <= 15:
        raise SessionError("invalid_phone")
    return value


def telegram_snapshot(storage: object, phone: str) -> Session:
    """只导出唯一已登录账号的必要字段，不保存其他槽位和聊天缓存。"""
    expected = phone_digits(phone)
    if not isinstance(storage, dict):
        raise SessionError("invalid_session")
    accounts = []
    try:
        for key, raw in storage.items():
            if isinstance(key, str) and re.fullmatch(r"account[1-4]", key):
                if not isinstance(raw, str) or len(raw.encode()) > MAX_SESSION_BYTES:
                    raise SessionError("invalid_session")
                value = json.loads(raw, object_pairs_hook=_unique_pairs)
                if not isinstance(value, dict):
                    raise SessionError("invalid_session")
                if value.get("userId"):
                    accounts.append(value)
    except (ValueError, TypeError, RecursionError):
        raise SessionError("invalid_session") from None
    if not accounts:
        raise SessionError("not_logged_in")
    if len(accounts) != 1:
        raise SessionError("multiple_accounts")
    account = accounts[0]
    fields = _telegram_fields()
    ignored = {
        "firstName",
        "lastName",
        "isPremium",
        "push_key",
        "temp_auth_keys",
        "sign_in_dc_id",
    }
    if set(account) - fields - ignored:
        raise SessionError("invalid_session")
    minimal = {key: value for key, value in account.items() if key in fields}
    value = dict(
        version=1, client=TELEGRAM_CLIENT, origin=TELEGRAM_ORIGIN, account=minimal
    )
    return validate_snapshot(value, expected_phone=expected)


def hax_snapshot(
    cookies: object, telegram: object, confirmed_account: bool = False
) -> Session:
    """保存 Hax 的既有网站授权；运行时仍须联网核对账号。"""
    account = validate_snapshot(telegram, expected_client=TELEGRAM_CLIENT)["account"]
    if confirmed_account is not True:
        raise SessionError("confirmation_required")
    if not isinstance(cookies, list):
        raise SessionError("invalid_session")
    allowed = {
        "stel_ssid": "oauth.telegram.org",
        "stel_token": "oauth.telegram.org",
        "stel_acid": ".telegram.org",
    }
    selected = [
        cookie
        for cookie in cookies
        if isinstance(cookie, dict)
        and isinstance(cookie.get("name"), str)
        and cookie.get("name") in allowed
        and cookie.get("domain") == allowed[cookie["name"]]
        and cookie.get("path") == "/"
        and not cookie.get("partitionKey")
    ]
    fields = {
        "name",
        "value",
        "domain",
        "path",
        "secure",
        "httpOnly",
        "sameSite",
        "expires",
    }
    minimal = [
        {key: value for key, value in cookie.items() if key in fields}
        for cookie in selected
    ]
    value = dict(
        version=2,
        client=HAX_CLIENT,
        origin=HAX_ORIGIN,
        login_origin=HAX_LOGIN_ORIGIN,
        account={"phone": account["phone"], "userId": account["userId"]},
        binding="telegram-widget",
        cookies=minimal,
    )
    # cf_clearance 属于访问环境，不能作为可跨机器复用的登录凭据导出。
    return validate_snapshot(value)


def validate_snapshot(
    value: object,
    expected_client: str | None = None,
    expected_phone: str | None = None,
    expected_user_id: str | None = None,
) -> Session:
    """严格校验版本、来源、字段和账号；不把格式通过当作在线有效。"""
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise SessionError("invalid_session")
    client = value.get("client")
    if (
        not isinstance(client, str)
        or client not in {TELEGRAM_CLIENT, HAX_CLIENT}
        or value["version"] != (1 if client == TELEGRAM_CLIENT else 2)
    ):
        raise SessionError("invalid_session")
    fields = {"version", "client", "origin", "account"}
    if client == HAX_CLIENT:
        fields |= {"binding", "cookies", "login_origin"}
    if set(value) != fields or value["origin"] != (
        TELEGRAM_ORIGIN if client == TELEGRAM_CLIENT else HAX_ORIGIN
    ):
        raise SessionError("invalid_session")
    account = value["account"]
    if not isinstance(account, dict):
        raise SessionError("invalid_session")
    if not _digits(account.get("phone"), 6, 15) or not _digits(
        account.get("userId"), 1, 20
    ):
        raise SessionError("invalid_session")
    if (
        expected_client is not None
        and client != expected_client
        or expected_phone is not None
        and account["phone"] != expected_phone
        or expected_user_id is not None
        and account["userId"] != expected_user_id
    ):
        raise SessionError("account_mismatch")
    if client == TELEGRAM_CLIENT:
        _validate_telegram(account)
    else:
        if (
            set(account) != {"phone", "userId"}
            or value["binding"] != "telegram-widget"
            or value["login_origin"] != HAX_LOGIN_ORIGIN
        ):
            raise SessionError("invalid_session")
        _validate_hax_cookies(value["cookies"])
    try:
        if len(json.dumps(value, ensure_ascii=True).encode()) > MAX_SESSION_BYTES:
            raise SessionError("invalid_session")
    except (TypeError, ValueError, RecursionError):
        raise SessionError("invalid_session") from None
    return value


def parse_snapshot(raw: str, **expected: str) -> Session:
    """读取 Secret 或私有文件中的 JSON，拒绝超长输入和重复字段。"""
    try:
        if len(raw.encode()) > MAX_SESSION_BYTES:
            raise SessionError("invalid_session")
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError, RecursionError):
        raise SessionError("invalid_session") from None
    return validate_snapshot(value, **expected)


def save_snapshot(path: Path, value: object) -> None:
    """以当前用户独占权限新建私有文件，不覆盖已有凭据。"""
    save_private_json(path, validate_snapshot(value))


def save_private_json(path: Path, value: object) -> None:
    """在受保护的位置新建私有 JSON，不覆盖已有文件。"""
    try:
        raw = json.dumps(value, separators=(",", ":")) + "\n"
        if len(raw.encode()) > MAX_SESSION_BYTES:
            raise SessionError("invalid_session")
    except (TypeError, ValueError, RecursionError):
        raise SessionError("invalid_session") from None
    target = private_output_path(path)
    try:
        missing = []
        directory_path = target.parent
        while not directory_path.exists():
            missing.append(directory_path)
            directory_path = directory_path.parent
        for directory_path in reversed(missing):
            directory_path.mkdir(mode=0o700)
        for directory_path in target.parents:
            if directory_path.name == ".local":
                for private_parent in (target.parent, *target.parent.parents):
                    info = private_parent.lstat()
                    if info.st_uid != os.getuid() or info.st_mode & 0o077:
                        raise SessionError("unsafe_file")
                    if private_parent == directory_path:
                        break
                break
        directory = target.parent.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.getuid()
            or directory.st_mode & 0o077
        ):
            raise SessionError("unsafe_file")
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError:
        raise SessionError("file_exists") from None
    except OSError:
        raise SessionError("file_error") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        target.unlink(missing_ok=True)
        raise SessionError("file_error") from None


def private_output_path(path: Path) -> Path:
    """仅允许仓库外或该仓库已忽略、未跟踪的 .local 路径。"""
    try:
        target = path.expanduser().absolute()
        if os.name != "posix" or ".." in target.parts:
            raise SessionError("unsafe_file")
        # 系统级 /tmp、/var 别名允许解析；私有位置本身不可使用链接。
        for parent in (target, *target.parents):
            if parent.is_symlink():
                if parent.name not in {"tmp", "var"} or parent.parent != Path("/"):
                    raise SessionError("unsafe_file")
        resolved = target.resolve()
        repository = next(
            (parent for parent in resolved.parents if (parent / ".git").exists()),
            None,
        )
        if repository is not None:
            relative = resolved.relative_to(repository)
            if not relative.parts or relative.parts[0] != ".local":
                raise SessionError("unsafe_file")
            git_environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            }
            ignored = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "check-ignore",
                    "--quiet",
                    "--",
                    str(resolved),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                env=git_environment,
            )
            tracked = subprocess.run(
                ["git", "-C", str(repository), "ls-files", "-z", "--", ".local"],
                capture_output=True,
                timeout=5,
                check=True,
                env=git_environment,
            )
            rules = (repository / ".dockerignore").read_text().splitlines()
            if (
                ignored.returncode != 0
                or tracked.stdout
                or not {".local/", "/.local/"}.intersection(rules)
                or any(rule.lstrip().startswith("!") for rule in rules)
            ):
                raise SessionError("unsafe_file")
        return target
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SessionError("unsafe_file") from None


def load_snapshot(path: Path, **expected: str) -> Session:
    """只读取当前用户的私有普通文件，拒绝符号链接和过大文件。"""
    return validate_snapshot(load_private_json(path), **expected)


def load_private_json(path: Path) -> object:
    """读取私有 JSON，拒绝符号链接、重复字段和过大文件。"""
    if os.name != "posix":
        raise SessionError("unsafe_file")
    try:
        descriptor = os.open(
            private_output_path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > MAX_SESSION_BYTES
            ):
                raise SessionError("unsafe_file")
            raw = source.read(MAX_SESSION_BYTES + 1)
    except (OSError, UnicodeError):
        raise SessionError("file_error") from None
    try:
        return json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError, RecursionError):
        raise SessionError("invalid_session") from None


def _digits(value: object, minimum: int, maximum: int) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(rf"[1-9][0-9]{{{minimum - 1},{maximum - 1}}}", value)
    )


def _telegram_fields() -> set[str]:
    return {"phone", "userId", "dcId", "date", "auth_key_fingerprint"} | {
        f"dc{dc}_{field}" for dc in range(1, 6) for field in ("auth_key", "server_salt")
    }


def _validate_telegram(account: Session) -> None:
    dc = account.get("dcId")
    if (
        set(account) - _telegram_fields()
        or type(dc) is not int
        or dc not in range(1, 6)
        or f"dc{dc}_auth_key" not in account
    ):
        raise SessionError("invalid_session")
    for index in range(1, 6):
        key, salt = f"dc{index}_auth_key", f"dc{index}_server_salt"
        if key in account or salt in account:
            if not _hex(account.get(key), 512) or not _hex(account.get(salt), 16):
                raise SessionError("invalid_session")
    if "auth_key_fingerprint" in account and not _hex(
        account["auth_key_fingerprint"], 8
    ):
        raise SessionError("invalid_session")
    if "date" in account and (
        type(account["date"]) is not int or not 0 <= account["date"] < 2**53
    ):
        raise SessionError("invalid_session")


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(rf"[a-fA-F0-9]{{{length}}}", value)
    )


def _validate_hax_cookies(cookies: object) -> None:
    fields = {"name", "value", "domain", "path", "secure", "httpOnly", "expires"}
    if not isinstance(cookies, list) or not 2 <= len(cookies) <= 3:
        raise SessionError("invalid_session")
    allowed = {
        "stel_ssid": "oauth.telegram.org",
        "stel_token": "oauth.telegram.org",
        "stel_acid": ".telegram.org",
    }
    names = set()
    for cookie in cookies:
        if (
            not isinstance(cookie, dict)
            or not fields <= set(cookie)
            or set(cookie) - fields - {"sameSite"}
            or not isinstance(cookie["name"], str)
            or cookie["name"] not in allowed
            or cookie["name"] in names
            or cookie["domain"] != allowed[cookie["name"]]
            or cookie["path"] != "/"
            or not isinstance(cookie["value"], str)
            or not re.fullmatch(r"[\x21-\x7e]{1,8192}", cookie["value"])
            or any(character in cookie["value"] for character in ';"\\')
            or cookie["secure"] is not True
            or cookie["httpOnly"] is not True
            or "sameSite" in cookie
            and (
                not isinstance(cookie["sameSite"], str)
                or cookie["sameSite"] not in {"Strict", "Lax", "None"}
            )
            or type(cookie["expires"]) not in {int, float}
            or not (cookie["expires"] == -1 or 0 < cookie["expires"] < 2**53)
        ):
            raise SessionError("invalid_session")
        names.add(cookie["name"])
    if not {"stel_ssid", "stel_token"} <= names:
        raise SessionError("invalid_session")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> Session:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_field")
        result[key] = value
    return result
