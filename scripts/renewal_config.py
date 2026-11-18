"""Hax 的账号数组配置；完整集合仅在受控配置层读取。"""

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.web_sessions import (
    HAX_CLIENT,
    MAX_SESSION_BYTES,
    TELEGRAM_CLIENT,
    load_private_json,
    load_snapshot,
    phone_digits,
    validate_snapshot,
)

# 会话边界返回已经严格校验的动态 JSON，后续不接收任意附加字段。
WebAccount = dict[str, Any]


def validate_identifier(value: object) -> str:
    """保留用户填写的非空标识符，不限制字符或修改首尾空白。"""
    if not isinstance(value, str) or not value:
        raise ValueError("invalid_account_identifier")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("invalid_account_identifier") from None
    return value


def identifier_directory(identifier: str) -> str:
    """将任意账号标识符映射为固定长度的目录名，避免直接拼接路径。"""
    encoded = validate_identifier(identifier).encode("utf-8")
    return "account-" + hashlib.sha256(encoded).hexdigest()


def validate_accounts(value: object) -> list[WebAccount]:
    """校验版本、账号标识符、身份和每个账号的两类会话。"""
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "accounts"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["accounts"], list)
        or not 1 <= len(value["accounts"]) <= 256
    ):
        raise ValueError("invalid_accounts")
    accounts = []
    identities: set[str] = set()
    phones: set[str] = set()
    users: set[str] = set()
    for account in value["accounts"]:
        if (
            not isinstance(account, dict)
            or set(account) != {"id", "phone", "telegram_web", "hax_web"}
            or not isinstance(account["phone"], str)
        ):
            raise ValueError("invalid_accounts")
        validate_identifier(account["id"])
        phone = phone_digits(account["phone"])
        telegram = validate_snapshot(
            account["telegram_web"],
            expected_client=TELEGRAM_CLIENT,
            expected_phone=phone,
        )
        user = telegram["account"]["userId"]
        hax = validate_snapshot(
            account["hax_web"],
            expected_client=HAX_CLIENT,
            expected_phone=phone,
            expected_user_id=user,
        )
        if account["id"] in identities or phone in phones or user in users:
            raise ValueError("duplicate_account")
        identities.add(account["id"])
        phones.add(phone)
        users.add(user)
        accounts.append(
            dict(
                id=account["id"],
                phone=account["phone"],
                telegram_web=telegram,
                hax_web=hax,
            )
        )
    return accounts


def parse_accounts(raw: str) -> list[WebAccount]:
    """受限解析 Environment Secret，不把错误输入带入异常。"""
    try:
        if len(raw.encode()) > MAX_SESSION_BYTES:
            raise ValueError("invalid_accounts")
        value = json.loads(raw, object_pairs_hook=_unique_fields)
        return validate_accounts(value)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("invalid_accounts") from None


def configured_accounts(environ: Mapping[str, str]) -> list[WebAccount]:
    """文件和 Actions 环境值共用格式，不允许两个来源同时提供。"""
    raw = environ.get("RENEWAL_ACCOUNTS", "")
    path = environ.get("RENEWAL_ACCOUNTS_FILE", "")
    if bool(raw) == bool(path):
        raise ValueError("invalid_accounts_source")
    if path:
        return validate_accounts(load_private_json(Path(path)))
    return parse_accounts(raw)


def select_account(environ: Mapping[str, str]) -> tuple[int, WebAccount]:
    """按私有标识符或公开匿名键选择凭据，展示序号不决定账号归属。"""
    accounts = configured_accounts(environ)
    selected = environ.get("RENEWAL_ACCOUNT_ID", "")
    key = environ.get("RENEWAL_ACCOUNT_KEY", "")
    if bool(selected) == bool(key) or key and not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("invalid_account_selection")
    matches = [
        (index, account)
        for index, account in enumerate(accounts, 1)
        if (cache_identity(account) == key if key else account["id"] == selected)
    ]
    if len(matches) != 1:
        raise ValueError("invalid_account_selection")
    return matches[0]


def notification_config(environ: Mapping[str, str]) -> tuple[str, str]:
    """共享通知配置与个人会话分开；只接受一组完整配置。"""
    values = []
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        raw, path = environ.get(name, ""), environ.get(name + "_FILE", "")
        if raw and path:
            raise ValueError("invalid_notification_source")
        if path:
            value = load_private_json(Path(path))
            if not isinstance(value, str):
                raise ValueError("invalid_notification")
            raw = value
        if (
            not raw
            or len(raw) > 512
            or any(character.isspace() for character in raw)
            or "\x00" in raw
        ):
            raise ValueError("invalid_notification")
        values.append(raw)
    return values[0], values[1]


def cache_identity(account: WebAccount) -> str:
    """以身份和主 DC 授权键派生标识，忽略排序和通知配置。"""
    account = validate_accounts(dict(version=1, accounts=[account]))[0]
    telegram = account["telegram_web"]["account"]
    key = bytes.fromhex(telegram[f"dc{telegram['dcId']}_auth_key"])
    message = (
        f"renewal-web:v1:hax:{account['id']}:{telegram['userId']}:{telegram['phone']}"
    )
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def account_masks(account: WebAccount) -> list[str]:
    """只登记选中账号的敏感字段，不输出数组正文。"""
    # 标识符允许单个字符，不登记其遮罩，避免匿名矩阵被误判为机密。
    fields = [account["phone"]]
    for section in (account["telegram_web"]["account"], account["hax_web"]["account"]):
        fields.extend(
            str(value)
            for name, value in section.items()
            if name in {"phone", "userId"}
            or name.endswith(("_auth_key", "_server_salt"))
        )
    fields.extend(cookie["value"] for cookie in account["hax_web"]["cookies"])
    return fields


def pack_accounts(manifest: Path) -> dict[str, Any]:
    """从账号清单引用的私有文件打包；不改变现有会话文件。"""
    value = load_private_json(manifest)
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "accounts"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["accounts"], list)
    ):
        raise ValueError("invalid_manifest")
    accounts = []
    for item in value["accounts"]:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "phone",
            "telegram_web_file",
            "hax_web_file",
        }:
            raise ValueError("invalid_manifest")
        if any(not isinstance(part, str) or not part for part in item.values()):
            raise ValueError("invalid_manifest")
        telegram = load_snapshot(
            manifest.parent / Path(item["telegram_web_file"]).expanduser()
        )
        hax = load_snapshot(manifest.parent / Path(item["hax_web_file"]).expanduser())
        accounts.append(
            dict(id=item["id"], phone=item["phone"], telegram_web=telegram, hax_web=hax)
        )
    result = dict(
        version=1, accounts=validate_accounts(dict(version=1, accounts=accounts))
    )
    if (
        len((json.dumps(result, separators=(",", ":")) + "\n").encode())
        > MAX_SESSION_BYTES
    ):
        raise ValueError("accounts_too_large")
    return result


def worker_account(environ: Mapping[str, str]) -> WebAccount:
    """worker 仅接受受控入口生成的单账号私有文件。"""
    path = environ.get("HAX_ACCOUNT_FILE", "")
    if (
        not path
        or not environ.get("PUBLIC_REPORT_FILE")
        or environ.get("RENEWAL_ACCOUNTS")
        or environ.get("RENEWAL_ACCOUNTS_FILE")
    ):
        raise ValueError("invalid_worker_configuration")
    accounts = validate_accounts(load_private_json(Path(path)))
    if len(accounts) != 1:
        raise ValueError("invalid_worker_configuration")
    return accounts[0]


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_field")
        result[key] = value
    return result
