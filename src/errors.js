export class TemplateError extends Error {
  constructor(message, detail = {}) {
    super(message);
    this.name = 'TemplateError';
    this.code = detail.code ?? 'TEMPLATE_ERROR';
    this.file = detail.file;
    this.position = detail.position;
    this.includeChain = detail.includeChain ? [...detail.includeChain] : undefined;
    if (detail.cause) this.cause = detail.cause;
  }

  format() {
    const parts = [`${this.code}: ${this.message}`];
    if (this.file) parts.push(`at ${this.file}:${describePosition(this.position)}`);
    if (this.includeChain?.length > 1) parts.push(`include chain: ${this.includeChain.join(' -> ')}`);
    return parts.join('\n  ');
  }
}

export function describePosition(position = {}) {
  if (typeof position.offset !== 'number') return '?:?';
  return `${(position.line ?? 0) + 1}:${(position.column ?? 0) + 1}`;
}

export function positionAt(source, offset) {
  let line = 0;
  let column = 0;
  const end = Math.min(offset, source.length);
  for (let i = 0; i < end; i += 1) {
    if (source.charCodeAt(i) === 10) {
      line += 1;
      column = 0;
    } else {
      column += 1;
    }
  }
  return { offset, line, column };
}

export function fail(message, detail) {
  throw new TemplateError(message, detail);
}
