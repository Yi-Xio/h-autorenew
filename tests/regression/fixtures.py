"""Web 认证测试使用的示例会话，不包含真实账号或授权材料。"""

import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any


def login_cookies(number: int = 1) -> list[dict[str, Any]]:
    """生成 Hax 登录组件使用的示例 Cookie。"""
    return [
        dict(
            name=name,
            value=f"EXAMPLE_{name.upper()}_{number:08d}",
            domain="oauth.telegram.org",
            path="/",
            secure=True,
            httpOnly=True,
            expires=-1,
        )
        for name in ("stel_ssid", "stel_token")
    ]


def authorization(user_id: str = "900000001") -> dict[str, Any]:
    """生成用于接口替身的示例授权结果，不生成真实签名。"""
    return dict(
        origin="https://hax.co.id",
        user=dict(
            id=user_id,
            first_name="Example",
            last_name="Account",
            auth_date=int(time.time()),
            hash="ab" * 32,
        ),
    )


class WidgetPage:
    """模拟官方组件的读取接口，不支持新授权或个人消息发送。"""

    def __init__(self, payload: object = None, ready: bool = True) -> None:
        self.responses = [ready, authorization() if payload is None else payload]
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.set = SimpleNamespace(timeouts=lambda **kwargs: None)

    def run_cdp(self, method: str, **arguments: Any) -> dict[str, Any]:
        self.requests.append((method, arguments))
        return {"result": {"value": self.responses.pop(0)}}

    def close(self) -> None:
        self.closed = True


def web_account(number: int = 1) -> dict[str, Any]:
    """生成账号配对一致、格式完整的示例状态。"""
    phone = "1555000" + ({1: "1234", 2: "5678"}.get(number, f"{number:04d}"))
    user = str(900000000 + number)
    identity = dict(phone=phone, userId=user)
    return dict(
        id=f"A{number:03d}",
        phone=f"+1-{phone[1:]}",
        telegram_web=dict(
            version=1,
            client="telegram-web-k",
            origin="https://web.telegram.org",
            account={
                **identity,
                "dcId": 2,
                "dc2_auth_key": f"{number:02x}" * 256,
                "dc2_server_salt": "ab" * 8,
            },
        ),
        hax_web=dict(
            version=2,
            client="hax-web",
            origin="https://hax.co.id",
            login_origin="https://oauth.telegram.org",
            account=identity,
            binding="telegram-widget",
            cookies=login_cookies(number),
        ),
    )


def web_environment(count: int = 1) -> dict[str, str]:
    """提供示例凭据和固定的账号选择。"""
    return dict(
        RENEWAL_ACCOUNTS=json.dumps(
            dict(
                version=1,
                accounts=[web_account(index) for index in range(1, count + 1)],
            )
        ),
        RENEWAL_ACCOUNT_ID="A001",
        TELEGRAM_BOT_TOKEN="123456:FAKE_BOT_TOKEN",
        TELEGRAM_CHAT_ID="chat-9988",
    )


PROJECT = Path(__file__).resolve().parents[2]
ACCOUNT = (
    "+1-5550001234,123456:FAKE_BOT_TOKEN,chat-9988,654321,FAKE_API_HASH,FAKE_SESSION"
)
SECOND = (
    "+1-5550005678,987654:OTHER_BOT_TOKEN,chat-8877,765432,OTHER_API_HASH,OTHER_SESSION"
)


def environment() -> dict[str, str]:
    """测试环境不继承真实凭据或 GitHub 的写入通道。"""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            (
                "GITHUB_",
                "ACTIONS_",
                "PUBLIC_",
                "TG_",
                "GIT_CONFIG_",
                "RENEWAL_",
                "TELEGRAM_",
                "HAX_",
            )
        )
        and key
        not in {
            "GH_TOKEN",
            "BATCH",
            "PROXY_CONTENT",
            "TWOCAPTCHA_API_KEY",
            "PYTHONPATH",
        }
    }


def navigation_sample(status: str = "ready") -> dict[str, object]:
    """提供固定结构的页面检查示例，不含 URL、表单值或身份。"""
    return dict(
        expected_origin=True,
        path_match=True,
        logout_link=status not in {"unknown", "login_required"},
        login_page=status == "login_required",
        code_input=True,
        submit_button=True,
        info_heading=False,
        rules_ready=True,
        dom_ready=status != "loading",
        adblock=status == "adblock",
        challenge=status == "challenge",
        status=status,
        document_state="loading" if status == "loading" else "complete",
        current_route="renew_code",
        expected_route="renew_code",
        elapsed_seconds=0,
    )
