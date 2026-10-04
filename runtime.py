"""运行时：保存编译产物并多次渲染。

渲染分两遍：
1. validate 遍递归走完整棵操作树（包括未进入的分支），任何缺失变量
   或类型错误立即抛出 ``RenderError``；
2. emit 遍才真正拼接 HTML。

因此数据有问题时调用方拿不到任何半份 HTML。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit, urljoin

from errors import CompileError, Loc, RenderError, TemplateError

# ---------------------------------------------------------------------------
# 操作码
# ---------------------------------------------------------------------------

# 编译产物是一棵操作树，节点为 dict：
#   {"op": "lit",      "s": str}
#   {"op": "var",      "path": [..], "kind": "text"|"attr", "loc": Loc}
#   {"op": "url",      "path": [..], "loc": Loc}
#   {"op": "if",       "path": [..], "loc": Loc,
#    "then": [op...], "else": [op...]}
#   {"op": "seq",      "items": [op...]}   （include 内联后的序列）


@dataclass
class Program:
    root_file: str
    operations: list[dict]
    base_url: str
    files_used: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 转义（编译器核心自行实现，不使用任何自动转义模板引擎）
# ---------------------------------------------------------------------------

_TEXT_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
# 属性转义同时覆盖两种引号：编译器允许单/双引号属性值，数据不可选引号。
_ATTR_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;",
                 '"': "&quot;", "'": "&#39;",
                 "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}


def escape_text(value: str) -> str:
    return "".join(_TEXT_ESCAPES.get(c, c) for c in value)


def escape_attr(value: str) -> str:
    return "".join(_ATTR_ESCAPES.get(c, c) for c in value)


# ---------------------------------------------------------------------------
# 数据取值与类型
# ---------------------------------------------------------------------------

def _lookup(path: list[str], data: dict, loc: Loc) -> object:
    cur: object = data
    for i, part in enumerate(path):
        if not isinstance(cur, dict) or part not in cur:
            where = ".".join(path)
            kind = "缺失变量" if not isinstance(cur, dict) else "缺失变量"
            raise RenderError(f"{kind}: {where!r}", [loc])
        cur = cur[part]
    return cur


def _as_text(value: object, path: list[str], loc: Loc) -> str:
    if isinstance(value, str):
        return value
    where = ".".join(path)
    raise RenderError(
        f"类型错误: {where!r} 需要字符串，得到 {type(value).__name__}", [loc])


def _as_bool(value: object, path: list[str], loc: Loc) -> bool:
    if isinstance(value, bool):
        return value
    where = ".".join(path)
    raise RenderError(
        f"类型错误: 条件 {where!r} 需要布尔值，得到 {type(value).__name__}",
        [loc])


# ---------------------------------------------------------------------------
# 动态 URL 校验
#
# 规则：动态值必须独占整个 href/src 属性值（这一点在编译期保证）。
# 再按固定基址解析，只接受：
#   - http / https 绝对地址；
#   - 站内相对地址（解析后落在固定基址的站点上，即 scheme/host 一致）。
# 拒绝控制字符、反斜杠（防止 /\example.com 这类浏览器误判）、
# 无 host 的 http(s) URL、其他一切协议（javascript:、data:、vbscript:…）。
# ---------------------------------------------------------------------------

_CONTROL = frozenset(
    [chr(c) for c in range(0x00, 0x20)] + [chr(0x7F)]
)
# 所有 HTML 空白；动态 URL 值任意位置都不允许，避免不同浏览器剥除差异。
_WHITESPACE = frozenset(" \t\n\r\f")


def validate_url(raw: str, base_url: str, loc: Loc) -> str:
    """返回可安全放入属性的已解析 URL（返回规范化绝对地址）。"""

    if raw == "":
        raise RenderError("类型错误: 动态 URL 为空字符串", [loc])

    # 先按原始字节拒绝：控制字符、DEL、反斜杠、任意位置的空白。
    # 不依赖浏览器/urljoin 剥字符（\t 会被 urljoin 静默吞掉）。
    for c in raw:
        if c in _CONTROL:
            raise RenderError(f"不安全的 URL：包含控制字符 {c!r}", [loc])
        if c in _WHITESPACE:
            raise RenderError("不安全的 URL：不允许空白字符", [loc])
        if c == "\\":
            raise RenderError("不安全的 URL：不允许反斜杠", [loc])

    # 显式协议引用必须带主机名；"https://" 这类空引用不能被
    # urljoin 静默回退成基址。
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://")):
        authority = raw.split("/", 3)[2]
        if not authority:
            raise RenderError("不安全的 URL：协议后缺少主机名", [loc])
    if lowered in ("http:", "https:"):
        raise RenderError("不安全的 URL：协议引用不完整", [loc])

    base = urlsplit(base_url)
    resolved = urlsplit(urljoin(base_url, raw))

    if resolved.scheme not in ("http", "https"):
        raise RenderError(
            f"不安全的 URL：协议 {resolved.scheme!r} 不被允许，仅允许 "
            "http/https 或站内相对地址", [loc])

    if resolved.hostname in (None, ""):
        raise RenderError("不安全的 URL：缺少主机名", [loc])

    # 相对地址（包括 //host/... 协议相对形式）必须落在固定基址的站内。
    is_absolute = raw.startswith(("http://", "https://"))
    if not is_absolute:
        if (resolved.scheme, resolved.hostname, resolved.port) != (
                base.scheme, base.hostname, base.port):
            raise RenderError(
                f"不安全的 URL：相对地址解析到站外主机 {resolved.hostname!r}",
                [loc])

        # ///host/... 这类多前导斜杠形态在「相对 vs 协议相对」上存在
        # 解析器差异且无正常用途，一律拒绝。
        if raw.startswith("///"):
            raise RenderError(
                "不安全的 URL：禁止多前导斜杠的相对地址", [loc])

        # 同站相对地址内禁止 ".." 路径段：浏览器/标准库对 /a/../../b
        # 的规范化在边界上存在差异，且模板场景没有正当用途。
        if any(seg == ".." for seg in raw.split("/")):
            raise RenderError(
                "不安全的 URL：站内相对地址禁止 .. 路径段", [loc])

    return resolved.geturl()


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _validate(op: dict, data: dict) -> None:
    kind = op["op"]
    if kind == "lit":
        return
    if kind in ("var", "url"):
        value = _lookup(op["path"], data, op["loc"])
        _as_text(value, op["path"], op["loc"])
        if kind == "url":
            # 校验阶段就执行全部 URL 规则，失败即整体失败。
            validate_url(value, op["_program"].base_url, op["loc"])
        return
    if kind == "if":
        value = _lookup(op["path"], data, op["loc"])
        _as_bool(value, op["path"], op["loc"])
        for child in op["then"]:
            _validate(child, data)
        for child in op["else"]:
            _validate(child, data)
        return
    if kind == "seq":
        for child in op["items"]:
            _validate(child, data)
        return
    raise CompileError(f"运行时未知操作码: {kind}")  # pragma: no cover


def _emit(op: dict, data: dict, out: list[str]) -> None:
    kind = op["op"]
    if kind == "lit":
        out.append(op["s"])
    elif kind == "var":
        value = _as_text(_lookup(op["path"], data, op["loc"]),
                         op["path"], op["loc"])
        out.append(escape_text(value) if op["kind"] == "text"
                   else escape_attr(value))
    elif kind == "url":
        value = _as_text(_lookup(op["path"], data, op["loc"]),
                         op["path"], op["loc"])
        out.append(escape_attr(
            validate_url(value, op["_program"].base_url, op["loc"])))
    elif kind == "if":
        value = _as_bool(_lookup(op["path"], data, op["loc"]),
                         op["path"], op["loc"])
        branch = op["then"] if value else op["else"]
        for child in branch:
            _emit(child, data, out)
    elif kind == "seq":
        for child in op["items"]:
            _emit(child, data, out)
    else:  # pragma: no cover
        raise CompileError(f"运行时未知操作码: {kind}")


def render(program: Program, data: dict) -> str:
    """用 data 渲染编译产物。任何缺失/类型错误都不返回半份 HTML。"""
    if not isinstance(data, dict):
        raise RenderError("类型错误: 渲染数据必须是对象（dict）")

    # 给 url 节点挂回 program 以便访问 base_url（渲染后移除）。
    def attach(op: dict) -> None:
        if op["op"] == "url":
            op["_program"] = program
        elif op["op"] == "if":
            for c in op["then"] + op["else"]:
                attach(c)
        elif op["op"] == "seq":
            for c in op["items"]:
                attach(c)

    for op in program.operations:
        attach(op)

    try:
        for op in program.operations:
            _validate(op, data)
        out: list[str] = []
        for op in program.operations:
            _emit(op, data, out)
        return "".join(out)
    finally:
        def detach(op: dict) -> None:
            if "_program" in op:
                del op["_program"]
            if op["op"] == "if":
                for c in op["then"] + op["else"]:
                    detach(c)
            elif op["op"] == "seq":
                for c in op["items"]:
                    detach(c)
        for op in program.operations:
            detach(op)


__all__ = ["Program", "RenderError", "TemplateError", "escape_text",
           "escape_attr", "validate_url", "render"]
