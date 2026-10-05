import { fail, positionAt } from './errors.js';

const IDENTIFIER_START = /[A-Za-z_$]/;
const PATH_CHAR = /[A-Za-z0-9_$.]/;

export function parseTemplate(source, file) {
  const root = { type: 'fragment', nodes: [] };
  const stack = [{ node: root, elseSeen: false }];

  const currentNodes = () => {
    const frame = stack[stack.length - 1];
    return frame.elseSeen ? frame.node.elseNodes : frame.node.nodes;
  };

  const locAt = (offset) => ({ file, ...positionAt(source, offset) });

  const pushLiteral = (start, end) => {
    if (end > start) currentNodes().push({ type: 'literal', text: source.slice(start, end), loc: locAt(start) });
  };

  const requirePath = (raw, loc) => {
    const expression = raw.trim();
    if (!expression || !/^[A-Za-z_$][A-Za-z0-9_$]*(\.[A-Za-z_$][A-Za-z0-9_$]*)*$/.test(expression)) {
      fail(`invalid variable path ${JSON.stringify(expression)}`, {
        code: 'INVALID_TEMPLATE_SYNTAX',
        file,
        position: loc,
      });
    }
    return expression.split('.');
  };

  const requireIncludeTarget = (raw, loc) => {
    const expression = raw.trim();
    const quote = expression[0];
    if ((quote !== '"' && quote !== "'") || expression[expression.length - 1] !== quote || expression.length < 2) {
      fail('include must use a quoted static string', {
        code: 'INVALID_INCLUDE',
        file,
        position: loc,
      });
    }
    const target = expression.slice(1, -1);
    if (!target || [...target].some((char) => char.charCodeAt(0) <= 0x1f || char.charCodeAt(0) === 0x7f)) {
      fail('include target is empty or contains a control character', {
        code: 'INVALID_INCLUDE',
        file,
        position: loc,
      });
    }
    return target;
  };

  let cursor = 0;
  while (cursor < source.length) {
    const start = source.indexOf('{{', cursor);
    if (start === -1) {
      pushLiteral(cursor, source.length);
      break;
    }
    pushLiteral(cursor, start);

    const end = source.indexOf('}}', start + 2);
    if (end === -1) {
      fail('unterminated {{ interpolation', {
        code: 'INVALID_TEMPLATE_SYNTAX',
        file,
        position: locAt(start),
      });
    }

    const loc = locAt(start);
    const tag = source.slice(start + 2, end);
    const trimmed = tag.trim();
    if (tag.startsWith('{') || tag.endsWith('}')) {
      fail('triple-brace unescaped interpolation is not supported', {
        code: 'INVALID_TEMPLATE_SYNTAX',
        file,
        position: loc,
      });
    }

    if (trimmed === 'else') {
      const frame = stack[stack.length - 1];
      if (frame.node === root || frame.elseSeen) {
        fail('unmatched {{else}}', { code: 'INVALID_TEMPLATE_SYNTAX', file, position: loc });
      }
      frame.elseSeen = true;
    } else if (trimmed === '/if') {
      const frame = stack.pop();
      if (!frame || frame.node === root) {
        fail('unmatched {{/if}}', { code: 'INVALID_TEMPLATE_SYNTAX', file, position: loc });
      }
    } else if (trimmed.startsWith('#if ')) {
      const path = requirePath(trimmed.slice(4), loc);
      const node = { type: 'if', path, nodes: [], elseNodes: [], loc };
      currentNodes().push(node);
      stack.push({ node, elseSeen: false });
    } else if (trimmed.startsWith('#include ')) {
      const target = requireIncludeTarget(trimmed.slice(9), loc);
      currentNodes().push({ type: 'include', target, loc });
    } else if (!trimmed || trimmed.startsWith('#') || trimmed.startsWith('/') || !IDENTIFIER_START.test(trimmed[0] ?? '') || !PATH_CHAR.test(trimmed)) {
      fail(`unsupported template tag ${JSON.stringify(trimmed)}`, {
        code: 'INVALID_TEMPLATE_SYNTAX',
        file,
        position: loc,
      });
    } else {
      currentNodes().push({ type: 'output', path: requirePath(trimmed, loc), loc });
    }
    cursor = end + 2;
  }

  if (stack.length > 1) {
    const open = stack[stack.length - 1].node;
    fail('unterminated {{#if}} block', {
      code: 'INVALID_TEMPLATE_SYNTAX',
      file,
      position: open.loc,
    });
  }
  return root;
}
