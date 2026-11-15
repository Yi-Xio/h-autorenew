"""工作流和续期入口共用的账号配置解析。"""

import hashlib
import hmac
import os
import re
from collections.abc import Sequence
from typing import Dict, List


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


def public_account_id(platform: str, fields: Sequence[str]) -> str:
    """用 Session 作为 HMAC 密钥生成公开标识，账号重排不改变归属。"""
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
