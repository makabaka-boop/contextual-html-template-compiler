"""受限 HTML 模板编译器（核心，手写实现，不使用任何现成模板引擎）。

模板语法（只支持这三种指令）
============================
* ``{{name}}`` / ``{{a.b.c}}``：文本或普通属性插值；
* ``{{#if flag}} ... {{else}} ... {{/if}}``：布尔分支，条件必须是布尔；
* ``{{#include "other.tpl"}}``：静态包含，路径必须是字符串字面量。

编译期上下文
============
手写状态机在编译阶段区分：普通文本、标签名、属性名、标签间隙、
带引号属性值（单/双）、无引号属性值、注释、script/style RAWTEXT 区域。
上下文随 #if 两分支和 include 传播：

* #if 的每个分支结束时，状态（所在模式 + 标签栈 + 当前属性）必须与
  进入分支时完全相同，否则「两分支结束状态不兼容」拒绝整份编译；
* include 展开为内联序列，被包含文件的结束状态必须与包含点上下文
  完全相同；成环（包含链上出现当前文件）直接拒绝。

拒绝位置统一为「包含链 root → leaf + 文件行列」。
模板字面量本身受信任，编译器不宣称清洗字面量。
"""

from __future__ import annotations

import re

from errors import CompileError, Loc
from runtime import Program

MAX_FILES = 6
MAX_TOTAL_BYTES = 20 * 1024

# ---- 解析模式 ----
DATA = "DATA"
TAGGAP = "TAGGAP"        # <tag 之后、属性之间
TAGNAME = "TAGNAME"
ATTRNAME = "ATTRNAME"
AFTERNAME = "AFTERNAME"  # 属性名之后、等待 = 或下一属性
VALWAIT = "VALWAIT"      # = 之后、等待值
QATTR_D = "QATTR_D"
QATTR_S = "QATTR_S"
UATTR = "UATTR"          # 无引号属性值
COMMENT = "COMMENT"
RAWTEXT = "RAWTEXT"
QUOTED = (QATTR_D, QATTR_S)

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
RAWTEXT_TAGS = {"script", "style"}
# 不支持 SVG、MathML，也不支持 RCDATA（其结束标签识别与普通文本不同）。
FORBIDDEN_TAGS = {"svg", "math", "title", "textarea"}

EVENT_ATTR_RE = re.compile(r"^on[a-z]+$")
BANNED_ATTRS = {"style", "srcdoc", "srcset"}
URL_ATTRS = {"href", "src"}

_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_INCLUDE_RE = re.compile(r'^#include[ \t]+"([A-Za-z0-9._/-]+)"$')
_NAME_CHAR_RE = re.compile(r"[A-Za-z0-9._:-]")
_TAGNAME_CHAR_RE = re.compile(r"[A-Za-z0-9-]")
_WS = " \t\n\r\f"

_VAR, _IF, _ELSE, _ENDIF, _INCLUDE = "var", "if", "else", "endif", "include"


class _State:
    """可变解析状态；快照沿分支/包含比较与恢复。"""

    def __init__(self) -> None:
        self.mode = DATA
        self.stack: list[str] = []
        self.cur_tag = ""        # 正在打开的标签名
        self.attr: str | None = None
        self.raw: str | None = None
        self.selfclose = False
        self.val_lit = 0         # 当前引号属性值中已出现的字面量字符数
        self.val_vars = 0        # 当前引号属性值中已出现的插值数

    def snap(self) -> tuple:
        return (self.mode, tuple(self.stack), self.cur_tag, self.attr,
                self.raw, self.selfclose, self.val_lit, self.val_vars)

    @staticmethod
    def structural_of(snap: tuple) -> tuple:
        """从完整快照取结构兼容部分（剔除属性值内部词法计数器）。"""
        return (snap[0], snap[1], snap[2], snap[3], snap[4], snap[5])

    def structural(self) -> tuple:
        """结构兼容快照：不含当前属性值内的字面量/插值计数。

        分支比较的是「两分支结束状态是否兼容」——标签栈、所处模式、
        当前属性必须一致；属性值内部计数器属于词法簿记，不影响结构。
        """
        return self.structural_of(self.snap())

    def restore(self, snap: tuple) -> None:
        (self.mode, stack, self.cur_tag, self.attr, self.raw,
         self.selfclose, self.val_lit, self.val_vars) = snap
        self.stack = list(stack)


class _Loader:
    """内存模板仓库；执行 6 文件 / 20 KiB 预算。"""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.used: list[str] = []
        self.total = 0

    def load(self, name: str, loc: Loc, chain: list[Loc]) -> str:
        if name not in self.files:
            raise CompileError(f"包含的模板不存在: {name!r}",
                               chain + [loc])
        if name not in self.used:
            size = len(self.files[name].encode("utf-8"))
            # 两个预算独立判断：即便文件数已到上限，也要先报字节超限；
            # 任何一项超限都不得登记该文件。
            if self.total + size > MAX_TOTAL_BYTES:
                raise CompileError(
                    f"模板总长度超过上限 {MAX_TOTAL_BYTES} 字节"
                    f"（加入 {name!r} 后将达 {self.total + size} 字节）",
                    chain + [loc])
            if len(self.used) + 1 > MAX_FILES:
                raise CompileError(
                    f"模板文件数超过上限 {MAX_FILES}（第 {len(self.used) + 1} "
                    f"个文件 {name!r}）", chain + [loc])
            self.used.append(name)
            self.total += size
        return self.files[name]


class Compiler:
    def __init__(self, loader: _Loader, root: str, base_url: str):
        self.loader = loader
        self.root = root
        self.base_url = base_url
        # 同一 include 在不同进入上下文下分别编译：缓存键含状态快照。
        self.cache: dict[tuple, tuple[list[dict], tuple]] = {}
        self._attr_buf = ""

    # ------------------------------------------------------------------ #
    # 位置
    # ------------------------------------------------------------------ #

    @staticmethod
    def _loc_of(name: str, s: str, i: int) -> Loc:
        line = s.count("\n", 0, i) + 1
        last_nl = s.rfind("\n", 0, i)
        col = i - last_nl if last_nl >= 0 else i + 1
        return Loc(name, line, col)

    def _err(self, msg: str, name: str, s: str, i: int,
             chain: list[Loc]) -> CompileError:
        return CompileError(msg, chain + [self._loc_of(name, s, i)])

    # ------------------------------------------------------------------ #
    # 指令词法
    # ------------------------------------------------------------------ #

    def _directive(self, name, s, i, chain):
        """s[i:] 以 {{ 开头，返回 (kind, value, end)。"""
        close = s.find("}}", i + 2)
        if close == -1:
            raise self._err("未闭合的指令（缺少 }}）", name, s, i, chain)
        inner = s[i + 2:close].strip()
        end = close + 2

        if inner.startswith("#"):
            m = _INCLUDE_RE.match(inner)
            if m:
                path = m.group(1)
                if path.startswith("/") or ".." in path.split("/") \
                        or "\\" in path:
                    raise self._err(
                        "include 路径非法（禁止绝对路径、目录回溯、反斜杠）",
                        name, s, i, chain)
                return _INCLUDE, path, end
            body = inner[1:].strip()
            if body.startswith("if") and (
                    len(body) == 2 or body[2] in _WS):
                expr = body[2:].strip()
                if not _PATH_RE.fullmatch(expr):
                    raise self._err("#if 需要一个点路径布尔变量",
                                    name, s, i, chain)
                return _IF, expr.split("."), end
            raise self._err(f"未知指令 {{{inner}}}", name, s, i, chain)

        if inner == "/if":
            return _ENDIF, None, end
        if inner == "else":
            return _ELSE, None, end
        if not _PATH_RE.fullmatch(inner):
            raise self._err(
                f"非法插值表达式 {inner!r}（只允许点路径变量，"
                "变量不可选择包含文件）", name, s, i, chain)
        return _VAR, inner.split("."), end

    # ------------------------------------------------------------------ #
    # 入口与单文件
    # ------------------------------------------------------------------ #

    def compile_root(self) -> Program:
        # 根文件同样经过 6 文件 / 20 KiB 预算检查
        content = self.loader.load(self.root, Loc(self.root, 1, 1), [])
        state = _State()
        ops, stop, pos = self._parse(
            self.root, content, state, [],
            stops=(), lit_start=0)
        if stop is not None:
            raise self._err("出现孤立的 {{else}} 或 {{/if}}",
                            self.root, content, pos, [])
        if state.mode != DATA:
            raise self._err("根模板结束于标签/注释/脚本区域内部",
                            self.root, content, len(content), [])
        if state.stack:
            raise self._err(
                f"根模板结束于未闭合标签 <{state.stack[-1]}> 内",
                self.root, content, len(content), [])
        return Program(self.root, ops, self.base_url,
                       files_used=self.loader.used)

    def _compile_file(self, fname, entry: tuple,
                      chain: list[Loc], content: str) -> tuple[list[dict], tuple]:
        """编译被包含文件。

        chain 为 root 起的包含点位置列表（最后一项是本文件的包含点）。
        """
        key = (fname, entry)
        if key in self.cache:
            return self.cache[key]

        state = _State()
        state.restore(entry)

        ops, stop, pos = self._parse(fname, content, state, chain,
                                     stops=(), lit_start=0)
        if stop is not None:  # pragma: no cover - stops 为空
            raise self._err("孤立的分支指令", fname, content, pos, chain)
        # 包含展开不得改变包含点上下文：结束结构状态必须与进入时一致
        # （标签栈、所在模式、当前属性全部相同）。根模板的进入状态为
        # DATA + 空栈，因此这条检查同样覆盖根模板未闭合的情形。
        if state.structural() != _State.structural_of(entry):
            if state.mode in (COMMENT, RAWTEXT):
                raise self._err(
                    "模板结束于注释或 script/style 区域内",
                    fname, content, len(content), chain)
            raise self._err(
                f"模板结束状态与进入上下文不兼容（结束于标签 "
                f"<{state.stack[-1] if state.stack else ''}>"
                f" 或 {state.mode} 模式）",
                fname, content, len(content), chain)

        result = (ops, state.snap())
        self.cache[key] = result
        return result

    # ------------------------------------------------------------------ #
    # 核心状态机
    # ------------------------------------------------------------------ #

    def _parse(self, name, s, st: _State, chain, stops, lit_start):
        """解析 s[lit_start:]。

        返回 (ops, stop_kind|None, stop_pos|EOF)。
        命中 stops 中的指令时，flush 后返回，停止指令留给调用方消费。
        """
        ops: list[dict] = []
        n = len(s)
        i = lit_start

        def flush(upto: int) -> None:
            if upto > lit_start:
                ops.append({"op": "lit", "s": s[lit_start:upto]})

        while i < n:
            # 1) 指令识别优先（各模式有自己的准入规则）
            if s.startswith("{{", i):
                kind, value, end = self._directive(name, s, i, chain)

                if kind in stops:
                    flush(i)
                    return ops, kind, i

                allowed_mode = st.mode in (DATA,) + QUOTED
                if not allowed_mode:
                    self._reject_directive(name, s, i, chain, st)

                if kind in (_ELSE, _ENDIF):
                    raise self._err("出现孤立的 {{else}} 或 {{/if}}",
                                    name, s, i, chain)

                if kind == _VAR:
                    flush(i)
                    consumed_quote = self._emit_var(
                        value, name, s, i, st, chain, ops)
                    if consumed_quote:
                        # href/src 独占：}} 紧后的闭合引号由 _emit_var
                        # 作为字面量节点输出，扫描位置需额外越过该引号。
                        i = end + 1
                        lit_start = i
                        continue
                    i = end
                    lit_start = end
                    continue
                if kind == _IF:
                    flush(i)
                    i = self._do_if(value, name, s, i, end, st, chain,
                                    ops, stops)
                    lit_start = i
                    continue
                if kind == _INCLUDE:
                    flush(i)
                    self._do_include(value, name, s, i, st, chain, ops)
                    i = end
                    lit_start = end
                    continue

            c = s[i]

            # 2) 注释：只找 -->，其余逐字（指令已在上面拒绝）
            if st.mode == COMMENT:
                if s.startswith("-->", i):
                    i += 3
                else:
                    i += 1
                continue

            # 3) RAWTEXT（script/style）：只认同名结束标签，其余逐字
            if st.mode == RAWTEXT:
                if s.startswith("</", i):
                    j = i + 2
                    start = j
                    while j < n and _TAGNAME_CHAR_RE.match(s[j]):
                        j += 1
                    tag = s[start:j].lower()
                    if tag == st.raw and self._raw_close_ok(s, j):
                        # 指令落入结束标签仍拒绝
                        gt = s.find(">", j)
                        if gt == -1:
                            raise self._err(
                                "script/style 结束标签未闭合",
                                name, s, len(s), chain)
                        if "{{" in s[i:gt]:
                            raise self._err(
                                "结束标签中禁止指令", name, s, i, chain)
                        st.stack.pop()
                        st.mode = DATA
                        st.raw = None
                        i = gt + 1
                        continue
                i += 1
                continue

            # 4) 普通文本
            if st.mode == DATA:
                if c != "<":
                    i += 1
                    continue
                if s.startswith("<!--", i):
                    st.mode = COMMENT
                    i += 4
                    continue
                if s.startswith("</", i):
                    tag, after = self._read_close_tag(name, s, i, chain)
                    if not st.stack or st.stack[-1] != tag:
                        raise self._err(
                            f"结束标签 </{tag}> 与未闭合标签 "
                            f"<{st.stack[-1] if st.stack else '(无)'}> 不匹配",
                            name, s, i, chain)
                    st.stack.pop()
                    i = after
                    continue
                if i + 1 < n and s[i + 1] in "!?":
                    gt = s.find(">", i)
                    if gt == -1:
                        raise self._err("声明片段（<!/<?>）未闭合",
                                        name, s, i, chain)
                    if "{{" in s[i:gt]:
                        raise self._err("声明片段中禁止指令",
                                        name, s, i, chain)
                    i = gt + 1
                    continue
                if i + 1 < n and _TAGNAME_CHAR_RE.match(s[i + 1]):
                    st.mode = TAGNAME
                    st.cur_tag = s[i + 1]
                    st.selfclose = False
                    i += 2
                    continue
                if s.startswith("<{{", i):
                    # < 之后紧跟指令：动态标签名/标签片段，拒绝。
                    raise self._err(
                        "禁止在标签名位置使用插值或指令（变量不可构造"
                        "标签名）", name, s, i + 1, chain)
                # 孤立 '<'：受信任字面量逐字保留
                i += 1
                continue

            # 5) 标签名
            if st.mode == TAGNAME:
                if _TAGNAME_CHAR_RE.match(c):
                    st.cur_tag += c
                    i += 1
                    continue
                tag = st.cur_tag.lower()
                if c in _WS:
                    self._assert_tag_allowed(tag, name, s, i, chain)
                    st.cur_tag = tag
                    st.mode = TAGGAP
                    i += 1
                    continue
                if c == "/":
                    self._assert_tag_allowed(tag, name, s, i, chain)
                    st.cur_tag = tag
                    st.selfclose = True
                    st.mode = TAGGAP
                    i += 1
                    continue
                if c == ">":
                    self._assert_tag_allowed(tag, name, s, i, chain)
                    self._open_tag(st, tag)
                    i += 1
                    continue
                raise self._err(
                    f"标签名中出现非法字符 {c!r}（插值落入标签名在此拒绝）",
                    name, s, i, chain)

            # 6) 标签间隙
            if st.mode == TAGGAP:
                if c in _WS:
                    i += 1
                    continue
                if c == "/":
                    st.selfclose = True
                    i += 1
                    continue
                if c == ">":
                    self._open_tag(st, st.cur_tag.lower())
                    i += 1
                    continue
                if _NAME_CHAR_RE.match(c):
                    st.attr = None
                    self._attr_buf = c
                    st.mode = ATTRNAME
                    i += 1
                    continue
                raise self._err(
                    "标签名/属性间隙出现非法字符（禁止在此插值）",
                    name, s, i, chain)

            # 7) 属性名
            if st.mode == ATTRNAME:
                if _NAME_CHAR_RE.match(c):
                    self._attr_buf += c
                    i += 1
                    continue
                st.attr = self._attr_buf.lower()
                if c in _WS:
                    st.mode = AFTERNAME
                    i += 1
                    continue
                if c == "=":
                    st.mode = VALWAIT
                    i += 1
                    continue
                if c in (">", "/"):
                    # 布尔属性：回到标签间隙重新处理当前字符
                    st.mode = TAGGAP
                    continue
                raise self._err(
                    "属性名中出现非法字符（插值落入属性名在此拒绝）",
                    name, s, i, chain)

            # 8) 属性名后（布尔属性 / 等号）
            if st.mode == AFTERNAME:
                if c in _WS:
                    i += 1
                    continue
                if c == "=":
                    st.mode = VALWAIT
                    i += 1
                    continue
                st.mode = TAGGAP
                continue  # 当前字符按 gap 重新处理

            # 9) 等待属性值
            if st.mode == VALWAIT:
                if c in _WS:
                    i += 1
                    continue
                if c == '"':
                    st.mode, st.val_lit, st.val_vars = QATTR_D, 0, 0
                    i += 1
                    continue
                if c == "'":
                    st.mode, st.val_lit, st.val_vars = QATTR_S, 0, 0
                    i += 1
                    continue
                if c == ">":
                    raise self._err("属性缺少值", name, s, i, chain)
                st.mode = UATTR
                continue  # 当前字符按无引号值重新处理

            # 10) 引号属性值
            if st.mode in QUOTED:
                quote = '"' if st.mode == QATTR_D else "'"
                if c == quote:
                    st.mode = TAGGAP
                    st.attr = None
                    st.val_lit = st.val_vars = 0
                    i += 1
                    continue
                st.val_lit += 1
                i += 1
                continue

            # 11) 无引号属性值
            if st.mode == UATTR:
                if c in _WS:
                    st.mode = TAGGAP
                    continue
                if c == ">":
                    self._open_tag(st, st.cur_tag.lower())
                    i += 1
                    continue
                if c in "\"'=<`":
                    raise self._err("无引号属性值中出现受限字符",
                                    name, s, i, chain)
                i += 1
                continue

            raise CompileError(f"编译器内部错误：未知模式 {st.mode}")  # pragma

        flush(n)
        return ops, None, n

    # ------------------------------------------------------------------ #
    # 指令分派
    # ------------------------------------------------------------------ #

    def _reject_directive(self, name, s, i, chain, st: _State):
        if st.mode in (COMMENT, RAWTEXT):
            where = "注释" if st.mode == COMMENT else "script/style 区域"
            raise self._err(f"{where}内禁止任何模板指令", name, s, i, chain)
        if st.mode == UATTR:
            raise self._err("无引号属性值中禁止插值/指令", name, s, i, chain)
        if st.mode in (TAGNAME, ATTRNAME):
            where = "标签名" if st.mode == TAGNAME else "属性名"
            raise self._err(f"{where}中禁止插值/指令", name, s, i, chain)
        raise self._err(
            "指令只能出现在普通文本或带引号属性值中", name, s, i, chain)

    def _emit_var(self, path, name, s, i, st: _State, chain, ops) -> bool:
        """发射插值操作。返回 True 表示调用方需额外消费一个闭合引号
        （href/src 独占整个属性值的情形）。"""
        loc = self._loc_of(name, s, i)

        if st.mode == DATA:
            ops.append({"op": "var", "path": path, "kind": "text",
                        "loc": loc})
            return False

        # 带引号属性值
        attr = st.attr or ""
        if EVENT_ATTR_RE.match(attr) or attr in BANNED_ATTRS:
            raise self._err(
                f"属性 {attr!r} 中禁止任何插值（事件处理器属性 / "
                "style/srcdoc/srcset）", name, s, i, chain)

        if attr in URL_ATTRS:
            close = s.find("}}", i + 2) + 2
            quote = '"' if st.mode == QATTR_D else "'"
            if st.val_lit != 0 or st.val_vars != 0 \
                    or close >= len(s) or s[close] != quote:
                raise self._err(
                    f"属性 {attr!r} 的动态值必须由单个变量独占整个属性值"
                    "（如 href=\"{{url}}\"），不得拼接",
                    name, s, i, chain)
            ops.append({"op": "url", "path": path, "loc": loc})
            # 闭合引号不属于动态值，作为可信字面量紧随 URL 节点输出
            ops.append({"op": "lit", "s": quote})
            st.mode = TAGGAP
            st.attr = None
            st.val_lit = st.val_vars = 0
            return True

        ops.append({"op": "var", "path": path, "kind": "attr", "loc": loc})
        st.val_vars += 1
        return False

    def _do_if(self, path, name, s, dstart, dend, st: _State, chain,
               parent_ops, parent_stops):
        attr = st.attr or ""
        if st.mode in QUOTED and (
                EVENT_ATTR_RE.match(attr) or attr in BANNED_ATTRS
                or attr in URL_ATTRS):
            raise self._err(
                f"属性 {attr!r} 中禁止条件指令（事件属性 / "
                "style/srcdoc/srcset / href/src 动态值只能是单个变量）",
                name, s, dstart, chain)

        entry = st.snap()
        entry_struct = st.structural()
        loc = self._loc_of(name, s, dstart)

        # then
        then_ops, stop, pos = self._parse(
            name, s, st, chain, {_ELSE, _ENDIF}, lit_start=dend)
        if stop is None:
            raise self._err("#if 缺少 {{/if}}", name, s, dstart, chain)
        if st.structural() != entry_struct:
            raise self._err(
                "#if 两分支结束状态不兼容：then 分支离开时所处上下文"
                "（标签栈/引号属性/脚本区域）与进入时不同",
                name, s, pos, chain)

        else_ops: list[dict] = []
        kind, _, end = self._directive(name, s, pos, chain)  # 消费 else/endif

        if kind == _ELSE:
            st.restore(entry)
            else_ops, stop2, pos2 = self._parse(
                name, s, st, chain, {_ENDIF}, lit_start=end)
            if stop2 is None:
                raise self._err("#if 缺少 {{/if}}", name, s, dstart, chain)
            if st.structural() != entry_struct:
                raise self._err(
                    "#if 两分支结束状态不兼容：else 分支离开时所处上下文"
                    "（标签栈/引号属性/脚本区域）与进入时不同",
                    name, s, pos2, chain)
            kind2, _, endif_end = self._directive(name, s, pos2, chain)
            assert kind2 == _ENDIF
        else:
            endif_end = end

        st.restore(entry)
        parent_ops.append({"op": "if", "path": path, "loc": loc,
                           "then": then_ops, "else": else_ops})
        return endif_end

    def _do_include(self, path, name, s, dstart, st: _State, chain, ops):
        attr = st.attr or ""
        if st.mode in QUOTED and (
                EVENT_ATTR_RE.match(attr) or attr in BANNED_ATTRS
                or attr in URL_ATTRS):
            raise self._err(
                f"属性 {attr!r} 中禁止 include", name, s, dstart, chain)

        entry = st.snap()
        entry_struct = _State.structural_of(entry)
        here = self._loc_of(name, s, dstart)
        if any(loc.file == path for loc in chain):
            raise self._err(
                f"包含成环：{path!r} 已在包含链上", name, s, dstart, chain)

        child_chain = chain + [here]
        content = self.loader.load(path, Loc(path, 1, 1), child_chain)
        child_ops, child_end = self._compile_file(
            path, entry, child_chain, content)
        # 比较结构状态（属性值内部计数器为词法簿记，不影响兼容性）。
        if _State.structural_of(child_end) != entry_struct:
            raise self._err(
                f"被包含文件 {path!r} 的结束状态与包含点上下文不兼容"
                "（标签栈或所处模式不同）", name, s, dstart, chain)
        ops.append({"op": "seq", "items": child_ops})

    # ------------------------------------------------------------------ #
    # 标签辅助
    # ------------------------------------------------------------------ #

    def _assert_tag_allowed(self, tag, name, s, i, chain):
        if tag in FORBIDDEN_TAGS:
            raise self._err(
                f"不支持的标签 <{tag}>（SVG / MathML / RCDATA 标签被禁止）",
                name, s, i, chain)

    def _open_tag(self, st: _State, tag: str) -> None:
        # tag 已小写化；栈内统一小写以保证大小写不敏感的结束标签匹配。
        if tag in RAWTEXT_TAGS:
            st.mode = RAWTEXT
            st.raw = tag
            st.stack.append(tag)
        else:
            st.mode = DATA
            st.raw = None
            if tag not in VOID_TAGS and not st.selfclose:
                st.stack.append(tag)
        st.cur_tag = ""
        st.attr = None
        st.selfclose = False
        st.val_lit = st.val_vars = 0

    def _read_close_tag(self, name, s, i, chain):
        """读取 i 处的 </name ...>，校验形态，返回 (tag, '>' 之后位置)。"""
        gt = s.find(">", i)
        if gt == -1:
            raise self._err("结束标签未闭合", name, s, i, chain)
        if "{{" in s[i:gt]:
            raise self._err("结束标签中禁止指令（变量不可构造标签片段）",
                            name, s, i, chain)
        j = i + 2
        start = j
        while j < gt and _TAGNAME_CHAR_RE.match(s[j]):
            j += 1
        tag = s[start:j].lower()
        if not tag:
            raise self._err("非法结束标签", name, s, i, chain)
        if s[j:gt].strip() not in ("", "/"):
            raise self._err("结束标签中不允许属性", name, s, i, chain)
        return tag, gt + 1

    @staticmethod
    def _raw_close_ok(s: str, j: int) -> bool:
        """RAWTEXT 结束标签 </script 之后必须紧跟空白、/ 或 >。"""
        if j >= len(s):
            return False
        return s[j] in _WS + "/>"


def compile_template(files: dict[str, str], root: str,
                     base_url: str = "http://localhost:8080/") -> Program:
    """编译可信开发者模板集合，返回可多次渲染的编译产物。"""
    loader = _Loader(files)
    return Compiler(loader, root, base_url).compile_root()
