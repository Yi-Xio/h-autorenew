"""只根据 HTTP 与 Telegram API 返回判断发送结果。"""

from typing import Protocol


class TelegramResponse(Protocol):
    """现有 requests 响应所需的最小接口。"""

    status_code: int

    def json(self) -> object: ...


def telegram_accepted(response: TelegramResponse) -> bool:
    """发送接口明确返回 ok=true 才算成功，不输出响应正文。"""
    try:
        body = response.json()
        return (
            response.status_code == 200
            and isinstance(body, dict)
            and body.get("ok") is True
        )
    except Exception:
        return False
