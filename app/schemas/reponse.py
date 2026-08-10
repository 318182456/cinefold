"""统一响应体。

模块名沿用原项目的拼写（reponse），以免前端契约对不上。
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


class ResponseEntity:
    def __init__(self, code: int = 200, message: str = "success", data: Any = None):
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "data": self.data}

    @staticmethod
    def ok(data: Any = None, message: str = "success") -> dict:
        return ResponseEntity(200, message, data).to_dict()

    @staticmethod
    def fail(message: str = "error", code: int = 500, data: Any = None) -> dict:
        return ResponseEntity(code, message, data).to_dict()

    @staticmethod
    def json(data: Any = None, message: str = "success", code: int = 200) -> JSONResponse:
        return JSONResponse(
            status_code=200,
            content=ResponseEntity(code, message, data).to_dict(),
        )
