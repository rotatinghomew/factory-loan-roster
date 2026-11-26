from __future__ import annotations

from typing import Any, Protocol

from .errors import GatewayFailure


class Gateway(Protocol):
    def send_replacement_request(self, message: dict[str, Any]) -> dict[str, Any]: ...


class LocalGateway:
    """Default adapter: record that the park exchange accepted the request."""

    def __init__(self):
        self.sent: list[dict[str, Any]] = []

    def send_replacement_request(self, message: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(message)
        return {"delivered": True, "remote_id": f"MSG-{len(self.sent)}"}


class FailingGateway:
    """Test/demo adapter for an unavailable replacement interface."""

    def __init__(self, message: str = "replacement interface unavailable"):
        self.message = message
        self.calls = 0

    def send_replacement_request(self, message: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise GatewayFailure(self.message, endpoint="replacement-request")
