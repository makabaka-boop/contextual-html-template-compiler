"""受限 HTML 模板编译服务测试。

独立验证手段：使用标准库 ``html.parser.HTMLParser``（与模板编译器完全
独立的解析器）解析渲染产物，断言：

* 危险数据不能改变预期 DOM 结构（标签、属性名固定）；
* 引号、实体、标签片段无法逃出文本/属性上下文；
* 危险 URL 数据不能注入新属性/新标签，URL 协议被限制。
"""

from __future__ import annotations

import unittest
from html.parser import HTMLParser

from compiler import (
    compile_template, MAX_FILES, MAX_TOTAL_BYTES,
    VOID_TAGS, RAWTEXT_TAGS,
)
from errors import CompileError, RenderError, Loc
from runtime import render, validate_url, escape_attr, escape_text

BASE = "https://app.example.com/app/"


def C(tpl, base=BASE, **extra):
    files = {"r.tpl": tpl}
    files.update(extra)
    return compile_template(files, "r.tpl", base_url=base)


class _DOM(HTMLParser):
    """独立 HTML 解析器：记录标签骨架与属性，用于结构对比。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.events: list[tuple] = []
        self.attrs_at: dict[int, list[tuple[str, str | None]]] = {}

    def handle_starttag(self, tag, attrs):
        self.events.append(("start", tag, list(attrs)))
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.events.append(("start", tag, list(attrs)))

    def handle_endtag(self, tag):
        self.events.append(("end", tag))
        if tag in self.stack:
            # 浏览器式容错：弹到匹配项；结构正确时应精确匹配
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data):
        self.events.append(("data", data))

    def skeleton(self):
        # 预期 DOM 结构只看标签；文本内容用 events 另行断言
        return [(e[0], e[1]) for e in self.events
                if e[0] in ("start", "end")]


def parse(html: str) -> _DOM:
    d = _DOM()
    d.feed(html)
    d.close()
    return d


# 不可信字符串攻击样本
XSS = [
    "<script>alert(1)</script>",
    "</p><script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "\"><img src=x onerror=alert(1)>",
    "'><img src=x onerror=alert(1)>",
    "' onmouseover='alert(1)",
    "\" onmouseover=\"alert(1)",
    "<svg/onload=alert(1)>",
    "<a href=\"javascript:alert(1)\">x</a>",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "javascript:alert(1)",
    "  javascript:alert(1)",
    "java\tscript:alert(1)",
    "ja&#x76;ascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "//evil.com/",
    "/\\evil.com",
    "{{x}}",
    "<!--",
    "--><script>x</script>",
    "\x00\x01\x0a",
    "http://localhost:8080.evil.com/",
]


class TestEscaping(unittest.TestCase):
    def test_text_escape(self):
        for v in XSS:
            p = C("<div>{{x}}</div>")
            html = render(p, {"x": v})
            dom = parse(html)
            self.assertEqual(dom.skeleton(),
                             [("start", "div"), ("end", "div")],
                             msg=f"payload={v!r} html={html!r}")
            # 数据应被实体化后原样往返
            self.assertIn(("data", v), dom.events, msg=v)
            self.assertNotIn("<script>", html)

    def test_attr_escape_double(self):
        for v in XSS:
            p = C('<div title="{{x}}">ok</div>')
            html = render(p, {"x": v})
            dom = parse(html)
            self.assertEqual(dom.skeleton(),
                             [("start", "div"), ("end", "div")], msg=v)
            attrs = [e[2] for e in dom.events if e[0] == "start"][0]
            self.assertEqual(attrs, [("title", v)], msg=v)
            self.assertIn(("data", "ok"), dom.events)

    def test_attr_escape_single(self):
        for v in XSS:
            p = C("<div title='{{x}}'>ok</div>")
            html = render(p, {"x": v})
            dom = parse(html)
            self.assertEqual(dom.skeleton(),
                             [("start", "div"), ("end", "div")], msg=v)
            attrs = [e[2] for e in dom.events if e[0] == "start"][0]
            self.assertEqual(attrs, [("title", v)],
                             msg=f"payload={v!r} html={html!r}")

    def test_multiple_vars_same_attr(self):
        p = C('<p title="{{a}} {{b}}"></p>')
        html = render(p, {"a": "1\"2", "b": "<x>"})
        dom = parse(html)
        attrs = dom.events[0][2]
        self.assertEqual(attrs, [("title", '1"2 <x>')])

    def test_escape_units(self):
        self.assertEqual(escape_text("<&>"), "&lt;&amp;&gt;")
        self.assertEqual(escape_attr("\"'<>&\n"),
                         "&quot;&#39;&lt;&gt;&amp;&#10;")


class TestIndependentParserStructure(unittest.TestCase):
    """用独立解析器验证危险数据不能改变预期 DOM。"""

    def test_complex_template_structure_invariant(self):
        tpl = (
            '<section><h1>{{title}}</h1>'
            '{{#if show}}<p class="a {{cls}}">{{body}}</p>{{else}}'
            '<p>{{empty}}</p>{{/if}}'
            '<a href="{{url}}">{{label}}</a></section>'
        )
        p = C(tpl)
        expected_true = [
            ("start", "section"), ("start", "h1"), ("end", "h1"),
            ("start", "p"), ("end", "p"),
            ("start", "a"), ("end", "a"), ("end", "section"),
        ]
        expected_false = [
            ("start", "section"), ("start", "h1"), ("end", "h1"),
            ("start", "p"), ("end", "p"),
            ("start", "a"), ("end", "a"), ("end", "section"),
        ]
        data_good = {
            "title": "T", "show": True, "cls": "c", "body": "B",
            "empty": "E", "url": "/home", "label": "go",
        }
        for payload in XSS:
            d = dict(data_good)
            d.update(title=payload, cls=payload, body=payload,
                     empty=payload, label=payload)
            # URL 位单独给安全值，URL 规则在另一组测试
            html = render(p, d)
            self.assertEqual(parse(html).skeleton(), expected_true,
                             msg=("true", payload, html))
            d["show"] = False
            html = render(p, d)
            self.assertEqual(parse(html).skeleton(), expected_false,
                             msg=("false", payload, html))

    def test_tag_fragment_cannot_break_out(self):
        p = C("<ul><li>{{a}}</li><li>{{b}}</li></ul>")
        for payload in XSS:
            html = render(p, {"a": payload, "b": payload})
            skel = parse(html).skeleton()
            self.assertEqual(skel, [
                ("start", "ul"),
                ("start", "li"), ("end", "li"),
                ("start", "li"), ("end", "li"),
                ("end", "ul"),
            ], msg=(payload, html))

    def test_entities_double_encoded(self):
        p = C("<p>{{x}}</p>")
        html = render(p, {"x": "&amp;"})
        # 数据里的实体不能被解析器当作实体结构
        self.assertEqual(html, "<p>&amp;amp;</p>")
        self.assertIn(("data", "&amp;"), parse(html).events)


class TestBranches(unittest.TestCase):
    def test_if_bool_required(self):
        p = C("{{#if f}}a{{else}}b{{/if}}")
        self.assertEqual(render(p, {"f": True}), "a")
        self.assertEqual(render(p, {"f": False}), "b")
        for bad in ("true", 1, 0, None, [], {}):
            with self.assertRaises(RenderError):
                render(p, {"f": bad})

    def test_nested_if(self):
        tpl = "{{#if a}}[{{#if b}}X{{else}}Y{{/if}}]{{else}}Z{{/if}}"
        p = C(tpl)
        self.assertEqual(render(p, {"a": True, "b": True}), "[X]")
        self.assertEqual(render(p, {"a": True, "b": False}), "[Y]")
        self.assertEqual(render(p, {"a": False, "b": True}), "Z")

    def test_branch_with_balanced_tags_ok(self):
        p = C("{{#if f}}<b>{{a}}</b>{{else}}<i>{{a}}</i>{{/if}}")
        self.assertEqual(render(p, {"f": True, "a": "A"}), "<b>A</b>")
        self.assertEqual(render(p, {"f": False, "a": "A"}), "<i>A</i>")

    def test_branch_attribute_ok(self):
        p = C('<div class="x {{#if f}}a{{else}}b{{/if}}"></div>')
        self.assertIn('class="x a"', render(p, {"f": True}))
        self.assertIn('class="x b"', render(p, {"f": False}))

    def test_unbalanced_branches_rejected(self):
        bad = [
            "{{#if f}}<div>{{else}}</div>{{/if}}",
            "{{#if f}}<div>{{/if}}",
            "{{#if f}}<a href=\"{{#if g}}x{{/if}}\">{{/if}}",
            "<!-- {{#if f}} a {{/if}} -->",
            "<script>{{#if f}}{{/if}}</script>",
        ]
        for tpl in bad:
            with self.assertRaises(CompileError) as cm:
                C(tpl)
            self.assertTrue(cm.exception.location)

    def test_unclosed_if(self):
        with self.assertRaises(CompileError):
            C("{{#if f}}x")
        with self.assertRaises(CompileError):
            C("{{else}}")


class TestIncludes(unittest.TestCase):
    def test_basic_include(self):
        p = C('<div>{{#include "p.tpl"}}</div>',
              **{"p.tpl": "<b>{{x}}</b>"})
        self.assertEqual(render(p, {"x": "X"}), "<div><b>X</b></div>")

    def test_same_include_different_contexts(self):
        # 同一文件分别出现在：元素内容、双引号属性、嵌套包含链中
        p = C(
            '<main>{{#include "m.tpl"}}'
            '<i title="A {{#include "m.tpl"}} Z"></i></main>',
            **{
                "m.tpl": "[{{x}}]",
            },
        )
        html = render(p, {"x": "<q>"})
        dom = parse(html)
        self.assertEqual(dom.skeleton(), [
            ("start", "main"), ("start", "i"), ("end", "i"),
            ("end", "main"),
        ])
        self.assertIn("[&lt;q&gt;]", html)
        self.assertIn('title="A [&lt;q&gt;] Z"', html)

    def test_include_inherits_tag_stack(self):
        # 包含点在 <ul> 内，被包含文件可以放 <li> 且保持栈不变
        p = C('<ul>{{#include "li.tpl"}}</ul>',
              **{"li.tpl": "<li>{{x}}</li>"})
        html = render(p, {"x": "X"})
        dom = parse(html)
        self.assertEqual(dom.skeleton(),
                         [("start", "ul"), ("start", "li"),
                          ("end", "li"), ("end", "ul")])
        self.assertIn(("data", "X"), dom.events)

    def test_include_context_mismatch_rejected(self):
        with self.assertRaises(CompileError) as cm:
            compile_template({
                "r.tpl": '<div>{{#include "b.tpl"}}</div>',
                "b.tpl": "</div>",
            }, "r.tpl")
        self.assertEqual(cm.exception.location.file, "b.tpl")

    def test_include_cycle(self):
        with self.assertRaises(CompileError) as cm:
            compile_template({
                "r.tpl": '{{#include "a.tpl"}}',
                "a.tpl": '{{#include "b.tpl"}}',
                "b.tpl": '{{#include "a.tpl"}}',
            }, "r.tpl")
        chain = [l.file for l in cm.exception.chain]
        # 定位包含链
        self.assertEqual(chain[0], "r.tpl")
        self.assertIn("a.tpl", chain)
        self.assertIn("b.tpl", chain)

    def test_self_include(self):
        with self.assertRaises(CompileError):
            compile_template({"r.tpl": '{{#include "r.tpl"}}'}, "r.tpl")

    def test_dynamic_include_rejected(self):
        for tpl in ['{{#include x}}', '{{#include "x"}}{{y}}'[:0] or
                    '{{#include "x" extra}}']:
            with self.assertRaises(CompileError):
                C(tpl)

    def test_missing_include_has_chain(self):
        with self.assertRaises(CompileError) as cm:
            C('<a>{{#include "nope.tpl"}}</a>')
        self.assertEqual(cm.exception.location.file, "nope.tpl")
        self.assertEqual(len(cm.exception.chain), 2)

    def test_include_inside_bad_attr_rejected(self):
        for tpl in [
            '<div onclick="{{#include \"p.tpl\"}}">',
            '<div style="{{#include \"p.tpl\"}}">',
            '<a href="{{#include \"p.tpl\"}}">',
        ]:
            with self.assertRaises(CompileError):
                C(tpl, **{"p.tpl": "x"})


class TestRejectedContexts(unittest.TestCase):
    def _rej(self, tpl):
        with self.assertRaises(CompileError) as cm:
            C(tpl)
        return cm.exception

    def test_tag_name(self):
        for tpl in ["<{{x}}>", "<{{#if f}}a{{else}}b{{/if}}>",
                    "<d{{x}}>"]:
            self.assertIsNotNone(self._rej(tpl).location)

    def test_attr_name(self):
        self._rej("<div {{x}}=1>")
        self._rej("<div a{{x}}=1>")

    def test_unquoted_attr(self):
        self._rej("<div class={{x}}>")
        self._rej("<div class=a{{x}}>")

    def test_event_attrs(self):
        for attr in ("onclick", "onload", "onmouseover", "ONCLICK"):
            self._rej(f'<div {attr}="{{x}}">')

    def test_banned_attrs(self):
        for attr in ("style", "srcdoc", "srcset"):
            self._rej(f'<div {attr}="{{x}}">')

    def test_comment_and_rawtext(self):
        self._rej("<!-- {{x}} -->")
        self._rej("<!-- {{#if f}}{{/if}} -->")
        self._rej("<script>var a={{x}}</script>")
        self._rej("<style>.a{color:{{x}}}</style>")
        self._rej("<script>{{#include \"p.tpl\"}}</script>")

    def test_url_attr_rules(self):
        self._rej('<a href="pre{{x}}">')
        self._rej('<a href="{{x}}tail">')
        self._rej('<a href="{{x}}{{y}}">')
        self._rej("<a href='{{x}}'>tail".replace("'tail'", ""))

    def test_forbidden_tags(self):
        for tag in ("svg", "math", "title", "textarea",
                    "SVG", "MATH"):
            self._rej(f"<{tag}></{tag}>")

    def test_close_tag_integrity(self):
        self._rej("</{{x}}>")
        self._rej("</div {{x}}>")
        self._rej("<div></p>")


class TestURLPolicy(unittest.TestCase):
    def setUp(self):
        self.p = C('<a href="{{u}}">{{t}}</a><img src="{{v}}">')

    def _render(self, u, v="/v"):
        return render(self.p, {"u": u, "v": v, "t": "x"})

    def test_accepted(self):
        ok = [
            ("http://other.example/a", "http://other.example/a"),
            ("https://other.example/a", "https://other.example/a"),
            ("https://app.example.com/x",
             "https://app.example.com/x"),
            ("/root", "https://app.example.com/root"),
            ("rel/path", "https://app.example.com/app/rel/path"),
            ("?q=1", "https://app.example.com/app/?q=1"),
            ("#frag", "https://app.example.com/app/#frag"),
            ("//app.example.com/x", "https://app.example.com/x"),
        ]
        for raw, want in ok:
            html = self._render(raw)
            self.assertIn(f'href="{want}"', html, msg=raw)

    def test_rejected_dangerous(self):
        bad = [
            "javascript:alert(1)",
            "JaVaScRiPt:alert(1)",
            "  javascript:alert(1)",
            "java\x09script:alert(1)",
            "java\x00script:alert(1)",
            "data:text/html,x",
            "vbscript:msgbox",
            "file:///etc/passwd",
            "ftp://h/",
            "//evil.com/x",
            "\\\\evil.com",
            "/\\evil.com",
            "https://evil.com/x",  # 协议相对/绝对外站已在上面区分；
                                   # https 绝对外站是允许的，故移除
            "http://",
            "https://",
            "",
            " ",
            "a b",
            "a\tb",
        ]
        bad.remove("https://evil.com/x")
        for raw in bad:
            with self.assertRaises(RenderError, msg=raw):
                self._render(raw)

    def test_absolute_https_offsite_allowed(self):
        html = self._render("https://evil.example/x")
        self.assertIn('href="https://evil.example/x"', html)

    def test_url_entity_smuggling(self):
        # 带实体/HTML 特殊字符的值即便通过协议也要被属性转义
        html = self._render("https://app.example.com/a?x=1&y=2")
        self.assertIn("&amp;", html)
        self.assertIn('href="https://app.example.com/a?x=1&amp;y=2"', html)

    def test_url_render_is_atomic(self):
        # URL 在 validate 阶段失败 -> 整份渲染失败
        with self.assertRaises(RenderError):
            self._render("javascript:1")

    def test_validate_unit(self):
        loc = Loc("r.tpl", 1, 1)
        self.assertEqual(
            validate_url("/a", BASE, loc), "https://app.example.com/a")
        for bad in ("javascript:1", "x\x01", "a\\b", "//h/"):
            with self.assertRaises(RenderError):
                validate_url(bad, BASE, loc)


class TestAtomicRender(unittest.TestCase):
    def setUp(self):
        self.p = C("<p>{{a}}</p>{{#if b}}{{c}}{{else}}{{d}}{{/if}}")

    def test_missing_var_no_partial_output(self):
        with self.assertRaises(RenderError) as cm:
            render(self.p, {"a": "A", "b": True})  # 缺 c
        self.assertIn("c", cm.exception.message)

    def test_missing_var_in_untaken_branch_still_fails(self):
        # 未进入分支里的缺失变量也必须失败（先整树校验）
        with self.assertRaises(RenderError):
            render(self.p, {"a": "A", "b": True, "c": "C"})  # 缺 d
        with self.assertRaises(RenderError):
            render(self.p, {"a": "A", "b": False, "d": "D"})  # 缺 c

    def test_type_errors(self):
        with self.assertRaises(RenderError):
            render(self.p, {"a": 1, "b": True, "c": "C", "d": "D"})
        with self.assertRaises(RenderError):
            render(self.p, {"a": "A", "b": "yes", "c": "C", "d": "D"})
        with self.assertRaises(RenderError):
            render(self.p, {"a": "A", "b": None, "c": "C", "d": "D"})

    def test_root_data_must_be_object(self):
        with self.assertRaises(RenderError):
            render(self.p, [("a", "A")])

    def test_dotted_paths(self):
        p = C("<p>{{u.name}}</p>{{#if u.flag}}Y{{/if}}")
        self.assertEqual(render(p, {"u": {"name": "N", "flag": True}}),
                         "<p>N</p>Y")
        with self.assertRaises(RenderError):
            render(p, {"u": {"name": "N", "flag": 0}})
        with self.assertRaises(RenderError):
            render(p, {"u": "name"})

    def test_program_reused_many_times(self):
        p = C("<b>{{x}}</b>")
        outs = {render(p, {"x": str(i)}) for i in range(50)}
        self.assertEqual(len(outs), 50)


class TestLocations(unittest.TestCase):
    def test_line_col(self):
        tpl = "<div>\n  <span>{{x}}</span>\n  <{{y}}>"
        try:
            C(tpl)
            self.fail()
        except CompileError as e:
            loc = e.location
            self.assertEqual((loc.line, loc.col), (3, 4))

    def test_chain_format(self):
        try:
            compile_template({
                "r.tpl": 'A\n{{#include "a.tpl"}}\nB',
                "a.tpl": 'X\n  {{#include "b.tpl"}}',
                "b.tpl": "<{{x}}>",
            }, "r.tpl")
            self.fail()
        except CompileError as e:
            chain_files = [l.file for l in e.chain]
            self.assertEqual(chain_files, ["r.tpl", "a.tpl", "b.tpl"])
            # 包含点位置沿链传播
            self.assertEqual((e.chain[0].line, e.chain[0].col), (2, 1))
            self.assertEqual((e.chain[1].line, e.chain[1].col), (2, 3))
            # 最终定位到 b.tpl 标签名位置
            self.assertEqual((e.chain[2].line, e.chain[2].col), (1, 2))


class TestLimits(unittest.TestCase):
    def test_too_many_files(self):
        files = {f"f{i}.tpl": f'{{{{#include "f{i+1}.tpl"}}}}'
                 for i in range(MAX_FILES)}
        files[f"f{MAX_FILES}.tpl"] = "end"
        with self.assertRaises(CompileError) as cm:
            compile_template(files, "f0.tpl")
        self.assertIn("文件数", cm.exception.message)

    def test_too_large_total(self):
        files = {
            "r.tpl": '{{#include "big.tpl"}}',
            "big.tpl": "x" * (MAX_TOTAL_BYTES + 1),
        }
        with self.assertRaises(CompileError) as cm:
            compile_template(files, "r.tpl")
        self.assertIn("总长度", cm.exception.message)

    def test_too_large_total_multi_file(self):
        # 最后一个被包含文件把总字节推过上限（文件数未超）
        files = {
            "r.tpl": '{{#include "b.tpl"}}' + "x" * 10220,
            "b.tpl": '{{#include "c.tpl"}}',
            "c.tpl": "y" * 10240,
        }
        self.assertGreater(sum(len(v) for v in files.values()),
                           MAX_TOTAL_BYTES)
        with self.assertRaises(CompileError) as cm:
            compile_template(files, "r.tpl")
        self.assertIn("总长度", cm.exception.message)
        self.assertEqual(cm.exception.location.file, "c.tpl")

    def test_root_file_size_checked(self):
        with self.assertRaises(CompileError):
            compile_template({"r.tpl": "x" * (MAX_TOTAL_BYTES + 1)},
                             "r.tpl")

    def test_within_limits_ok(self):
        files = {f"f{i}.tpl": f'{{{{#include "f{i+1}.tpl"}}}}'
                 for i in range(MAX_FILES - 1)}
        files[f"f{MAX_FILES - 1}.tpl"] = "ok"
        p = compile_template(files, "f0.tpl")
        self.assertEqual(len(p.files_used), MAX_FILES)


class TestCaseInsensitive(unittest.TestCase):
    def test_uppercase_tags_match(self):
        p = C("<DIV>{{x}}</DIV><P title='{{t}}'>z</P>")
        html = render(p, {"x": "X", "t": "T"})
        self.assertEqual(parse(html).skeleton(),
                         [("start", "div"), ("end", "div"),
                          ("start", "p"), ("end", "p")])

    def test_uppercase_sensitive_attrs(self):
        for at in ("OnClick", "ONCLICK", "Style", "SRCDOC", "SrcSet"):
            with self.assertRaises(CompileError):
                C('<div ' + at + '="{{x}}"></div>')
        for at in ("HREF", "SRC"):
            p = C('<a ' + at + '="{{u}}"></a>')
            html = render(p, {"u": "/ok"})
            self.assertIn(at.lower() + "=", html.lower())

    def test_single_quote_url_exclusive(self):
        p = C("<a href='{{u}}'>z</a>")
        html = render(p, {"u": "ok"})
        self.assertEqual(html,
                         "<a href='https://app.example.com/app/ok'>z</a>")
        with self.assertRaises(RenderError):
            render(p, {"u": "javascript:1"})


class TestTrustedRawtext(unittest.TestCase):
    def test_script_literal_preserved_verbatim(self):
        tpl = ('<script>var a = 1 < 2; if (b > 3) {} var t="<div>";'
               '</script><p>{{x}}</p>')
        html = render(C(tpl), {"x": "X"})
        self.assertIn('var a = 1 < 2', html)
        self.assertIn("<p>X</p>", html)

    def test_fake_rawtext_end_tag(self):
        # </scriptx> 不能结束 RAWTEXT
        tpl = '<script>var s="</scriptx>";</script><p>{{x}}</p>'
        html = render(C(tpl), {"x": "X"})
        self.assertEqual(parse(html).skeleton(),
                         [("start", "script"), ("end", "script"),
                          ("start", "p"), ("end", "p")])


class TestProgramReuse(unittest.TestCase):
    def test_no_render_state_leaks_into_program(self):
        p = C('<a href="{{u}}">{{#if f}}{{a}}{{else}}{{b}}{{/if}}</a>')
        render(p, {"u": "/a", "f": True, "a": "A", "b": "B"})
        render(p, {"u": "/b", "f": False, "a": "A", "b": "B"})

        def walk(x):
            if isinstance(x, dict):
                self.assertNotIn("_program", x)
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(p.operations)


class TestTrustedLiterals(unittest.TestCase):
    """模板字面量受信任：正常声明/可信脚本不被清洗。"""

    def test_doctype_and_trusted_script(self):
        tpl = ('<!doctype html><head><script>var s = "</";</script></head>'
               '<body>{{x}}</body>')
        p = C(tpl)
        html = render(p, {"x": "A"})
        self.assertTrue(html.lower().startswith("<!doctype html>"))
        self.assertIn("<script>var s = \"</\";</script>", html)

    def test_booleans_and_void_elements(self):
        tpl = '<input disabled type="text" value="{{v}}"><br><hr>'
        html = render(C(tpl), {"v": "X"})
        dom = parse(html)
        self.assertEqual(dom.skeleton(),
                         [("start", "input"), ("start", "br"),
                          ("start", "hr")])

    def test_static_href_prefix_with_var_is_rejected(self):
        # href/src 的动态值只允许独占整个属性值；前缀拼接即使前缀是
        # 受信任字面量也拒绝（动态部分无法按整体做 URL 策略）。
        with self.assertRaises(CompileError):
            C('<a href="/static/{{x}}">y</a>')
        # 纯静态 href 与独占动态 href 均合法
        self.assertIn('href="/static/x"',
                      render(C('<a href="/static/x">y</a>'), {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
