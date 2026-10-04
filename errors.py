"""模板服务的错误类型。

包含链方向统一为 ``[最外层, ..., 最内层]``（root 在最前，
实际触发问题的被包含文件在最后），方便定位「文件、位置及包含链」。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Loc:
    """单个文件内的 1 起行列位置。"""

    file: str
    line: int
    col: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}"


class TemplateError(Exception):
    """所有模板错误的基类。chain 为包含链上的 Loc（root → leaf）。"""

    def __init__(self, message: str, chain: list[Loc] | None = None):
        self.message = message
        self.chain: list[Loc] = list(chain or [])
        super().__init__(message)

    @property
    def location(self) -> Loc | None:
        return self.chain[-1] if self.chain else None

    def format(self) -> str:
        if not self.chain:
            return self.message
        head = self.chain[0]
        if len(self.chain) == 1:
            return f"编译拒绝: {self.message}  @ {head}"
        trace = " -> ".join(str(loc) for loc in self.chain)
        return f"编译拒绝: {self.message}  @ {trace}"

    def __str__(self) -> str:  # pragma: no cover - 等价于 format
        return self.format()


class CompileError(TemplateError):
    """编译阶段拒绝整份模板时抛出。"""


class RenderError(TemplateError):
    """渲染阶段缺失变量或类型错误；不输出任何半份 HTML。"""
