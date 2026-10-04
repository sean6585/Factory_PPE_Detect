"""回應模型與例外。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

__all__ = ["SignalTowerResponse", "SignalTowerError", "ERROR_CODES"]

#: 手冊 p.57 的錯誤碼對照表
ERROR_CODES: Dict[str, str] = {
    "002": "無效的命令 (Invalid command)",
    "003": "未指定命令 (The command is not specified)",
    "004": "未指定數值 (The value is not specified)",
    "005": "無效的數值 (Invalid value)",
}


@dataclass
class SignalTowerResponse:
    """一次 HTTP 命令的結果。"""

    url: str = ""
    http_status: int = 0
    body: str = ""
    elapsed_ms: int = 0
    transport_error: Optional[str] = None  # 逾時或無法連線時的訊息

    @property
    def is_success(self) -> bool:
        """control 命令成功時設備回應 "Success."。"""
        return (
            self.transport_error is None
            and 200 <= self.http_status < 300
            and self.body.lstrip().lower().startswith("success")
        )

    @property
    def error_code(self) -> Optional[str]:
        """設備回應 "Error.<code>" 時的錯誤碼。"""
        text = self.body.strip()
        if not text.lower().startswith("error"):
            return None
        _, _, code = text.partition(".")
        return code.strip()

    def describe(self) -> str:
        if self.transport_error is not None:
            return f"通訊失敗：{self.transport_error}"
        code = self.error_code
        if code is not None:
            return f"裝置回傳錯誤 {code}：{ERROR_CODES.get(code, '未定義的錯誤碼')}"
        if self.is_success:
            return "成功 (Success.)"
        return f"HTTP {self.http_status}"

    def raise_for_status(self) -> "SignalTowerResponse":
        """失敗時丟出 SignalTowerError。"""
        if not self.is_success:
            raise SignalTowerError(self.describe(), self)
        return self

    def __str__(self) -> str:
        return self.describe()


class SignalTowerError(Exception):
    """通訊失敗或設備回傳錯誤碼。"""

    def __init__(self, message: str, response: Optional[SignalTowerResponse] = None):
        super().__init__(message)
        self.response = response
