"""工作流和续期入口共用的账号配置解析。"""

import hashlib
import hmac
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Dict, List

from scripts.renewal_config import (
    WebAccount,
    account_masks,
    cache_identity,
    notification_config,
    select_account,
)


@dataclass
class Account:
    """正式入口选择后的单账号配置，不持有账号数组。"""

    index: int
    phone: str
    token: str
    chat: str
    identifier: str
    stable_id: str = ""
    web: WebAccount | None = None
    legacy: List[str] | None = None

    def masks(self) -> List[str]:
        """返回需要补充遮罩的当前账号字段。"""
        return (account_masks(self.web) if self.web else self.legacy or []) + [
            self.token,
            self.chat,
        ]


def runtime_account(
    platform: str, environ: Dict[str, str], with_notification: bool = True
) -> Account:
    """Hax 只用新数组，Woiden 保持已有六字段配置。"""
    if platform == "hax":
        index, web = select_account(environ)
        token, chat = notification_config(environ) if with_notification else ("", "")
        raw = environ.get("BATCH_ACCOUNT_INDEX", str(index))
        if not re.fullmatch(r"[1-9][0-9]{0,2}", raw) or int(raw) > 256:
            raise ValueError("invalid_account_index")
        return Account(
            int(raw), web["phone"], token, chat, cache_identity(web), web["id"], web
        )
    if platform != "woiden":
        raise ValueError("invalid_platform")
    index, fields = selected_account(environ)
    return Account(
        index,
        fields[0],
        fields[1],
        fields[2],
        public_account_id(platform, fields),
        legacy=fields,
    )


def parse_batch(value: str) -> List[List[str]]:
    """解析六字段账号配置，错误消息不包含输入。"""
    if len(value) > 1_000_000:
        raise ValueError("invalid_accounts")
    records = re.split(r"[;\r\n]", value.replace("\\n", ";"))
    accounts = [
        [part.strip() for part in row.split(",")] for row in records if row.strip()
    ]
    if not 1 <= len(accounts) <= 256:
        raise ValueError("invalid_accounts")
    phones = set()
    for fields in accounts:
        if len(fields) != 6 or any(not field for field in fields):
            raise ValueError("invalid_account_fields")
        if not re.fullmatch(r"\+\d{1,4}-\d{4,16}", fields[0]):
            raise ValueError("invalid_phone_format")
        if not fields[3].isascii() or not fields[3].isdigit() or int(fields[3]) <= 0:
            raise ValueError("invalid_api_id")
        if fields[0] in phones or any("\x00" in field for field in fields):
            raise ValueError("invalid_accounts")
        phones.add(fields[0])
    return accounts


def selected_account(environ: Dict[str, str]) -> tuple[int, List[str]]:
    """按数字序号选择一个账号，默认处理第一个。"""
    accounts = parse_batch(environ.get("BATCH", ""))
    index = int(environ.get("BATCH_ACCOUNT_INDEX", "1"))
    if not 1 <= index <= len(accounts):
        raise ValueError("invalid_account_index")
    return index, accounts[index - 1]


def state_key(phone: str) -> str:
    """仅用于与原脚本内部交接状态，不写入公开状态文件。"""
    return hashlib.md5(phone.strip().encode()).hexdigest()[:8]


def public_account_id(platform: str, fields: Sequence[str] | Account) -> str:
    """从当前认证材料生成公开标识，账号重排不改变归属。"""
    if platform not in {"hax", "woiden"}:
        raise ValueError("invalid_account")
    if isinstance(fields, Account):
        return fields.identifier
    if platform not in {"hax", "woiden"} or len(fields) != 6 or not all(fields):
        raise ValueError("invalid_account")
    message = f"renewal-state:v1:{platform}:{fields[0]}".encode()
    return hmac.new(fields[5].encode(), message, hashlib.sha256).hexdigest()


def register_masks(values: List[str]) -> None:
    """向 GitHub 登记补充遮罩；普通终端不输出凭据。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    for value in sorted(set(values), key=len, reverse=True):
        if value:
            escaped = (
                value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            )
            print(f"::add-mask::{escaped}", flush=True)
