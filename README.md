# 受限 HTML 模板编译服务

用**可信开发者模板**渲染**不可信字符串数据**的服务端模板系统。编译期做
上下文分析并产出可多次渲染的操作树；渲染期按上下文转义并强制 URL 安全策略。
核心（词法/解析/上下文状态机/分支与包含传播）全部手写，**没有调用任何
现成的上下文自动转义模板引擎**（无 Jinja2/Mako/MarkupSafe 等），仅使用
Python 标准库（`urllib`、`html.parser` 用于测试侧独立校验、`re/unittest`）。

## 文件（共 5 个，本服务自身代码）

| 文件 | 职责 |
|---|---|
| `errors.py` | `CompileError` / `RenderError` / `Loc`；包含链 root→leaf 定位 |
| `compiler.py` | 模板词法 + 手写 HTML 上下文状态机、`#if`/`#include` 传播、6 文件/20 KiB 预算 |
| `runtime.py` | 编译产物 `Program`、两遍渲染（先校验后输出）、转义、URL 策略 |
| `tests.py` | 58 个测试；用**独立** `html.parser.HTMLParser` 验证 DOM 结构不被数据改变 |
| `README.md` | 本文档 |

> 6 个文件 / 20 KiB 的上限约束的是**模板集合**，不是服务代码。

## 模板语法（仅三种指令）

```html
{{user.name}}                         文本或普通属性插值（点路径）
{{#if is_admin}}…{{else}}…{{/if}}    布尔条件（条件值必须是 bool）
{{#include "partials/nav.tpl"}}      静态包含（路径只能是字符串字面量）
```

* 变量**不能**选择包含文件：`#include` 只接受双引号字符串字面量，
  禁止绝对路径、`..` 回溯、反斜杠。
* `#if` 可嵌套；指令在文本和**带引号普通属性值**内可用。

## 编译期上下文与传播规则

编译器在编译阶段逐字符区分：普通文本、标签名、属性名、标签间隙、
单/双引号属性值、无引号属性值、HTML 注释、`script`/`style` RAWTEXT 区域。

上下文沿控制结构传播并做**结构兼容性检查**：

* **分支**：每个 `then`/`else` 分支结束时，状态（所在模式、标签栈、
  当前正在打开的标签/属性）必须与进入分支时完全相同。例如
  `{{#if f}}<div>{{else}}</div>{{/if}}` 两分支结束状态不兼容，整份拒绝。
* **包含**：被包含文件在包含点的上下文里编译，其结束状态必须与进入时一致；
  同一 include 在不同上下文（如元素内容 vs 属性值）会分别编译。
  包含成环（含自包含）拒绝。

以下情况**拒绝整份编译**，错误带文件名、行、列及完整包含链：

* 插值/指令落入：标签名、属性名、无引号属性值、标签间隙、
  HTML 注释、`script`/`style` 区域；
* 事件处理器属性 `on*`（大小写不敏感）或 `style`/`srcdoc`/`srcset`
  属性中出现任何插值；
* `href`/`src` 的动态值没有**独占整个属性值**
  （`href="p/{{x}}"`、`href="{{a}}{{b}}"` 均拒绝）；
* 结束标签内出现指令（变量不能构造标签片段）；
* 不支持 `<svg>`、`<math>` 及 RCDATA 标签（`<title>`/`<textarea>`）；
* 标签未闭合、结束标签不匹配、分支/包含结构不兼容、包含成环；
* 模板文件数超过 6 或模板总字节数超过 20 KiB。

## 输出编码与 URL 策略

* **文本插值**：`& < >` → `&amp; &lt; &gt;`。
* **普通属性插值**：额外转义 `" '` 及换行/制表符/回车
  （`&#39;`/`&#10;`/`&#13;`/`&#9;`），两种引号都无法逃逸。
* **动态 `href`/`src`**（编译期保证独占整个值）渲染时：
  1. 原始值先拒绝空串、所有 C0 控制字符与 DEL、任意位置的 HTML 空白、
     反斜杠；
  2. 按**固定基址**（`compile_template(..., base_url=...)`，
     默认 `http://localhost:8080/`）用 `urljoin` 解析；
  3. 最终协议只允许 `http`/`https`；`javascript:`、`data:`、`vbscript:`、
     `file:` 等及大小写/控制字符混淆全部拒绝；
  4. 相对地址（含 `//host` 协议相对形式）解析后 scheme/host/port
     必须与固定基址同站，拒绝外站主机、异端口、`///host` 多斜杠形态、
     含 `..` 路径段的相对地址；
  5. 通过的值（输出为规范化绝对地址）再做属性转义（`&`→`&amp;` 等）。
* **模板字面量受信任，不做清洗**：开发者写的静态 `onclick`、静态
  `href="javascript:..."`、`<!doctype>`、可信 `<script>` 内容原样保留。

## 渲染原子性

渲染分两遍：先递归校验整棵操作树（**包括未进入的分支**），任何缺失变量、
非字符串插值、非布尔条件、URL 策略失败都立即抛出 `RenderError`；
全部通过后才拼接输出。因此错误时调用方拿不到任何半份 HTML。
编译产物 `Program` 可复用渲染任意次，渲染不会修改操作树。

## 用法

```python
from compiler import compile_template
from runtime import render
from errors import CompileError, RenderError

templates = {
    "page.tpl": (
        '<!doctype html>'
        '<h1>{{title}}</h1>'
        '{{#include "item.tpl"}}'
        '<a href="{{next}}">next</a>'
    ),
    "item.tpl": '{{#if active}}<b>{{name}}</b>{{else}}<i>{{name}}</i>{{/if}}',
}

prog = compile_template(
    templates, "page.tpl",
    base_url="https://app.example.com/app/")   # 可重复使用

html = render(prog, {
    "title": "<script>x</script>",   # → &lt;script&gt;…
    "active": True,                  # 必须是 bool
    "name": "a'b\"c",
    "next": "/detail/42",            # → https://app.example.com/detail/42
})

try:
    render(prog, {"title": None, "active": 1, "name": "x",
                  "next": "javascript:alert(1)"})
except RenderError as e:
    print(e)        # 不输出任何 HTML
```

编译错误定位示例（包含链从根文件排到实际拒绝点）：

```
编译拒绝: 注释内禁止任何模板指令
  @ page.tpl:12:3 -> item.tpl:4:1 -> nav.tpl:2:9
```

## 运行测试

```
python3 -m unittest tests -v
```

58 个测试覆盖：文本/单双引号属性转义、用独立 `HTMLParser` 对 22 个
攻击载荷做 DOM 结构不变量验证、布尔严格类型、缺失变量（含未进入分支）
原子失败、点路径、分支结构不兼容、静态/嵌套/跨上下文 include、成环、
包含链定位、行列定位、6 文件与 20 KiB 上限、URL 接受/拒绝矩阵、
实体走私、大小写属性/标签、可信 RAWTEXT 边界、编译产物复用无污染。

## 明确的范围与不支持项

* 只渲染 HTML 元素/属性/注释/`script`/`style`；不支持 SVG、MathML、
  RCDATA 标签（其结束标签识别规则不同，避免上下文误判）。
* 不做表达式、过滤器、函数调用、循环；插值仅点路径，条件仅布尔变量。
* 不清洗受信任字面量——信任边界是**模板作者**，不是模板里的数据。
