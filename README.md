# Restricted HTML template compiler

This package compiles trusted developer-authored HTML templates that render untrusted string data. It implements its own template parser and HTML context scanner; no contextual autoescape template engine is used for the compiler core. `parse5` is used only in the test suite as an independent HTML parser.

## Supported template language

- Output: `{{name}}` or dotted paths such as `{{user.name}}`.
- Boolean conditionals: `{{#if flag}} ... {{else}} ... {{/if}}`.
- Static includes: `{{#include "partial.html"}}`.
- Include targets are fixed quoted strings. Data cannot select an included file.
- Maximum six files and 20 KiB total source across a compilation.

The developer template text is trusted and is not sanitized. Dynamic string data is never trusted.

## Compilation guarantees

The compiler tokenizes ordinary text, quoted attribute values, comments, DOCTYPE declarations, and `script`/`style` raw-text regions. Context is propagated into both conditional branches and through includes.

Compilation fails, with file, line/column, and include-chain information when possible, if:

- Both `if`/`else` branches end in incompatible HTML contexts.
- Includes form a cycle.
- Interpolation occurs in a tag name, attribute name, unquoted attribute value, event handler attribute, `style`, `srcdoc`, or `srcset`, a comment, markup declaration, or `script`/`style` region.
- A dynamic `href` or `src` does not occupy the entire quoted value.
- A template ends in the middle of a tag, quoted attribute, comment, script, or style region.
- SVG, MathML, or non-script/style special raw/RCDATA-style elements are used.

## Escaping and URLs

- Text interpolation escapes `&`, `<`, and `>`.
- Quoted attribute interpolation escapes `&`, `<`, `>`, `"`, and `'`.
- `href`/`src` interpolation is first resolved against a fixed base URL. Only `http:`, `https:`, or same-site relative references are retained. Control characters, tab/newline/CR whitespace, backslashes, and protocol-relative (`//host`) values are rejected. The accepted normalized value is then attribute-escaped.

Static literal attribute values are not rewritten or sanitized; only dynamic data is constrained.

## Atomic rendering

A compiled template is reusable. Each render validates every variable on the selected branch before constructing output:

- text and URL values must be strings;
- `if` values must be booleans;
- required URL strings must satisfy the fixed-base URL policy.

A missing variable, wrong type, or invalid URL throws before any partial HTML is returned.

## Example

```js
import { compileTemplates } from './src/index.js';

const compiled = compileTemplates({
  'index.html': '<h1>{{title}}</h1><a href="{{home}}">Home</a>',
}, 'index.html', { baseUrl: 'https://example.com/app/' });

compiled.render({
  title: '<script>alert(1)</script>',
  home: '/dashboard?next=home',
});
```

## Tests

```bash
npm test
```
