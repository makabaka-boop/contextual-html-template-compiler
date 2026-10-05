import { fail } from './errors.js';

const MODE = Object.freeze({
  DATA: 'DATA',
  TAG_OPEN: 'TAG_OPEN',
  END_TAG_OPEN: 'END_TAG_OPEN',
  TAG_NAME: 'TAG_NAME',
  END_TAG_NAME: 'END_TAG_NAME',
  END_TAG_AFTER_NAME: 'END_TAG_AFTER_NAME',
  BEFORE_ATTR: 'BEFORE_ATTR',
  ATTR_NAME: 'ATTR_NAME',
  AFTER_ATTR_NAME: 'AFTER_ATTR_NAME',
  BEFORE_ATTR_VALUE: 'BEFORE_ATTR_VALUE',
  ATTR_VALUE_DOUBLE: 'ATTR_VALUE_DOUBLE',
  ATTR_VALUE_SINGLE: 'ATTR_VALUE_SINGLE',
  ATTR_VALUE_UNQUOTED: 'ATTR_VALUE_UNQUOTED',
  AFTER_ATTR_VALUE: 'AFTER_ATTR_VALUE',
  SELF_CLOSING_START: 'SELF_CLOSING_START',
  COMMENT: 'COMMENT',
  COMMENT_DASH: 'COMMENT_DASH',
  COMMENT_BANG_DASH: 'COMMENT_BANG_DASH',
  MARKUP_DECL: 'MARKUP_DECL',
  PI: 'PI',
  RAW: 'RAW',
  RAW_LT: 'RAW_LT',
  RAW_END_NAME: 'RAW_END_NAME',
  RAW_END_TAG: 'RAW_END_TAG',
});

const RAW_TAGS = new Set(['script', 'style']);
const FORBIDDEN_TAGS = new Set([
  'title',
  'textarea',
  'svg',
  'math',
  'noscript',
  'noframes',
  'noembed',
  'iframe',
  'xmp',
  'plaintext',
]);

const DANGEROUS_ATTRS = new Set(['style', 'srcdoc', 'srcset']);

function initialState() {
  return {
    mode: MODE.DATA,
    tagName: '',
    endName: '',
    attrName: '',
    attrPolicy: null,
    hasAttrLiteral: false,
    hasUrlOutput: false,
    rawType: '',
    rawEndName: '',
    declaration: '',
    startOffset: null,
    lastLoc: null,
  };
}

function cloneState(state) {
  return { ...state };
}

function isWhitespace(char) {
  return char === '\t' || char === '\n' || char === '\f' || char === ' ' || char === '\r';
}

function isNameChar(char) {
  return /[A-Za-z0-9_:.-]/.test(char);
}

function policyFor(name) {
  const lower = name.toLowerCase();
  if (lower.startsWith('on')) return 'event';
  if (DANGEROUS_ATTRS.has(lower)) return 'dangerous';
  if (lower === 'href' || lower === 'src') return 'url';
  return null;
}

function mergePolicy(a, b) {
  if (a === b) return a;
  if (a === 'event' || b === 'event') return 'event';
  if (a === 'dangerous' || b === 'dangerous') return 'dangerous';
  if (a === 'url' || b === 'url') return 'url';
  return null;
}

function statesCompatible(a, b) {
  if (a.mode !== b.mode) return false;
  switch (a.mode) {
    case MODE.TAG_NAME:
    case MODE.END_TAG_NAME:
    case MODE.END_TAG_AFTER_NAME:
    case MODE.BEFORE_ATTR:
    case MODE.AFTER_ATTR_NAME:
    case MODE.BEFORE_ATTR_VALUE:
    case MODE.AFTER_ATTR_VALUE:
    case MODE.SELF_CLOSING_START:
      return (
        a.tagName === b.tagName &&
        a.attrName === b.attrName &&
        a.attrPolicy === b.attrPolicy &&
        a.hasAttrLiteral === b.hasAttrLiteral &&
        a.hasUrlOutput === b.hasUrlOutput &&
        a.rawType === b.rawType
      );
    case MODE.ATTR_NAME:
      return a.tagName === b.tagName && a.attrName === b.attrName && a.rawType === b.rawType;
    case MODE.ATTR_VALUE_DOUBLE:
    case MODE.ATTR_VALUE_SINGLE:
    case MODE.ATTR_VALUE_UNQUOTED:
      return (
        a.tagName === b.tagName &&
        a.attrName === b.attrName &&
        a.attrPolicy === b.attrPolicy &&
        a.rawType === b.rawType &&
        a.hasAttrLiteral === b.hasAttrLiteral &&
        a.hasUrlOutput === b.hasUrlOutput
      );
    case MODE.RAW:
      return a.rawType === b.rawType;
    case MODE.RAW_LT:
      return a.rawType === b.rawType;
    case MODE.RAW_END_NAME:
    case MODE.RAW_END_TAG:
      return a.rawType === b.rawType && a.rawEndName === b.rawEndName;
    case MODE.COMMENT:
    case MODE.COMMENT_DASH:
    case MODE.COMMENT_BANG_DASH:
      return true;
    case MODE.MARKUP_DECL:
      return a.declaration === b.declaration;
    case MODE.PI:
      return true;
    default:
      return true;
  }
}

function mergeStates(a, b) {
  if (!statesCompatible(a, b)) return null;
  const merged = cloneState(a);
  if (merged.mode === MODE.ATTR_NAME) merged.attrPolicy = mergePolicy(a.attrPolicy, b.attrPolicy);
  return merged;
}

class HtmlScanner {
  constructor(state = initialState()) {
    this.state = state;
  }

  clone() {
    return new HtmlScanner(cloneState(this.state));
  }

  restore(scanner) {
    this.state = cloneState(scanner.state);
  }

  merge(scanner) {
    const merged = mergeStates(this.state, scanner.state);
    if (!merged) {
      fail('if/else branches finish in incompatible HTML contexts', {
        code: 'INCOMPATIBLE_BRANCH_CONTEXT',
        position: this.state.startOffset ?? undefined,
      });
    }
    this.state = merged;
  }

  assertComplete() {
    const s = this.state;
    if (s.mode !== MODE.DATA) {
      fail('template ends in the middle of an HTML tag, attribute, comment, script, or style region', {
        code: 'UNTERMINATED_HTML_CONTEXT',
        file: s.lastLoc?.file,
        position: s.lastLoc ?? undefined,
      });
    }
  }

  literal(text, loc) {
    let line = loc.line ?? 0;
    let column = loc.column ?? 0;
    for (let index = 0; index < text.length; index += 1) {
      const char = text[index];
      this.char(char, { ...loc, offset: (loc.offset ?? 0) + index, line, column });
      if (char === '\n') {
        line += 1;
        column = 0;
      } else {
        column += 1;
      }
    }
  }

  failAt(message, code, loc) {
    fail(message, { code, file: loc.file, position: loc });
  }

  char(char, loc = {}) {
    const s = this.state;
    s.lastLoc = loc;
    switch (s.mode) {
      case MODE.DATA:
        if (char === '<') {
          s.mode = MODE.TAG_OPEN;
          s.startOffset = loc.offset ?? null;
        }
        return;

      case MODE.TAG_OPEN:
        if (char === '!') {
          s.mode = MODE.MARKUP_DECL;
          s.declaration = '';
        } else if (char === '/') {
          s.mode = MODE.END_TAG_OPEN;
        } else if (/[A-Za-z]/.test(char)) {
          s.mode = MODE.TAG_NAME;
          s.tagName = char;
        } else if (char === '?') {
          s.mode = MODE.PI;
        } else {
          this.failAt('invalid tag opening', 'MALFORMED_TEMPLATE_HTML', loc);
        }
        return;

      case MODE.END_TAG_OPEN:
        if (/[A-Za-z]/.test(char)) {
          s.mode = MODE.END_TAG_NAME;
        } else {
          this.failAt('invalid end tag', 'MALFORMED_TEMPLATE_HTML', loc);
        }
        return;

      case MODE.TAG_NAME:
        if (isWhitespace(char)) this.finishStartTagName(loc);
        else if (char === '>') {
          this.finishStartTagName(loc);
          this.closeStartTag(loc);
        } else if (char === '/') s.mode = MODE.SELF_CLOSING_START;
        else if (isNameChar(char)) s.tagName += char;
        else this.failAt('invalid character in tag name', 'MALFORMED_TEMPLATE_HTML', loc);
        return;

      case MODE.END_TAG_NAME:
        if (isWhitespace(char)) s.mode = MODE.END_TAG_AFTER_NAME;
        else if (char === '>') this.closeEndTag(loc);
        else if (isNameChar(char)) s.endName += char;
        else this.failAt('invalid character in end tag name', 'MALFORMED_TEMPLATE_HTML', loc);
        return;

      case MODE.END_TAG_AFTER_NAME:
        if (isWhitespace(char)) return;
        if (char === '>') this.closeEndTag(loc);
        else this.failAt('attributes are not allowed on end tags', 'MALFORMED_TEMPLATE_HTML', loc);
        return;

      case MODE.BEFORE_ATTR:
        if (isWhitespace(char)) return;
        if (char === '>') this.closeStartTag(loc);
        else if (char === '/') s.mode = MODE.SELF_CLOSING_START;
        else if (/[A-Za-z_:]/.test(char)) this.startAttribute(char);
        else this.failAt('invalid character before attribute', 'MALFORMED_TEMPLATE_HTML', loc);
        return;

      case MODE.ATTR_NAME:
        if (isWhitespace(char)) s.mode = MODE.AFTER_ATTR_NAME;
        else if (char === '=') this.enterAttributeValue(loc);
        else if (char === '>') this.closeStartTag(loc);
        else if (char === '/') s.mode = MODE.SELF_CLOSING_START;
        else if (isNameChar(char)) {
          s.attrName += char;
          s.attrPolicy = policyFor(s.attrName);
        } else this.failAt('invalid character in attribute name', 'MALFORMED_TEMPLATE_HTML', loc);
        return;

      case MODE.AFTER_ATTR_NAME:
        if (isWhitespace(char)) return;
        if (char === '=') this.enterAttributeValue(loc);
        if (char === '>') this.closeStartTag(loc);
        else if (char === '/') s.mode = MODE.SELF_CLOSING_START;
        else if (/[A-Za-z_:]/.test(char)) this.startAttribute(char);
        else this.failAt('invalid character after attribute name', 'MALFORMED_TEMPLATE_HTML', loc);
        return;

      case MODE.BEFORE_ATTR_VALUE:
        if (isWhitespace(char)) return;
        if (char === '"' || char === "'") {
          s.mode = char === '"' ? MODE.ATTR_VALUE_DOUBLE : MODE.ATTR_VALUE_SINGLE;
        } else if (char === '>') {
          this.failAt('attribute is missing a value', 'MALFORMED_TEMPLATE_HTML', loc);
        } else {
          s.mode = MODE.ATTR_VALUE_UNQUOTED;
          this.attributeLiteral(char, loc);
        }
        return;

      case MODE.ATTR_VALUE_DOUBLE:
        if (char === '"') s.mode = MODE.AFTER_ATTR_VALUE;
        else this.attributeLiteral(char, loc);
        return;

      case MODE.ATTR_VALUE_SINGLE:
        if (char === "'") s.mode = MODE.AFTER_ATTR_VALUE;
        else this.attributeLiteral(char, loc);
        return;

      case MODE.ATTR_VALUE_UNQUOTED:
        if (isWhitespace(char)) {
          this.clearAttribute();
          s.mode = MODE.BEFORE_ATTR;
        } else if (char === '>') {
          this.closeStartTag(loc);
        } else if (char === '"' || char === "'" || char === '<' || char === '`') {
          this.failAt('unsafe character in unquoted attribute value', 'MALFORMED_TEMPLATE_HTML', loc);
        } else {
          this.attributeLiteral(char, loc);
        }
        return;

      case MODE.AFTER_ATTR_VALUE:
        if (isWhitespace(char)) {
          this.clearAttribute();
          s.mode = MODE.BEFORE_ATTR;
        } else if (char === '>') this.closeStartTag(loc);
        else if (char === '/') s.mode = MODE.SELF_CLOSING_START;
        else this.failAt('invalid character after quoted attribute value', 'MALFORMED_TEMPLATE_HTML', loc);
        return;

      case MODE.SELF_CLOSING_START:
        if (char === '>') this.closeStartTag(loc, true);
        else if (isWhitespace(char)) return;
        else this.failAt('invalid self-closing tag syntax', 'MALFORMED_TEMPLATE_HTML', loc);
        return;

      case MODE.MARKUP_DECL:
        this.markupDeclarationChar(char, loc);
        return;

      case MODE.PI:
        if (char === '>') this.failAt('processing instructions are not supported', 'UNSUPPORTED_MARKUP_DECLARATION', loc);
        return;

      case MODE.COMMENT:
        if (char === '-') s.mode = MODE.COMMENT_DASH;
        return;

      case MODE.COMMENT_DASH:
        if (char === '-') s.mode = MODE.COMMENT_BANG_DASH;
        else s.mode = MODE.COMMENT;
        return;

      case MODE.COMMENT_BANG_DASH:
        if (char === '>' || char === '!') s.mode = char === '!' ? MODE.COMMENT_BANG_DASH : MODE.DATA;
        else if (char === '-') s.mode = MODE.COMMENT_DASH;
        else s.mode = MODE.COMMENT;
        return;

      case MODE.RAW:
        if (char === '<') s.mode = MODE.RAW_LT;
        return;

      case MODE.RAW_LT:
        if (char === '/') {
          s.mode = MODE.RAW_END_NAME;
          s.rawEndName = '';
        } else {
          s.mode = MODE.RAW;
        }
        return;

      case MODE.RAW_END_NAME:
        if (/[A-Za-z0-9]/.test(char)) {
          s.rawEndName += char;
        } else if (s.rawEndName.toLowerCase() === s.rawType &&
          (isWhitespace(char) || char === '>')) {
          if (isWhitespace(char)) s.mode = MODE.RAW_END_TAG;
          else this.closeRawEnd(loc);
        } else {
          s.mode = MODE.RAW;
        }
        return;

      case MODE.RAW_END_TAG:
        if (isWhitespace(char)) return;
        if (char === '>') this.closeRawEnd(loc);
        else this.failAt(`invalid character in closing ${s.rawType} tag`, 'MALFORMED_TEMPLATE_HTML', loc);
        return;

      default:
        this.failAt('internal scanner error', 'INTERNAL_ERROR', loc);
    }
  }

  finishStartTagName(loc) {
    const s = this.state;
    const lower = s.tagName.toLowerCase();
    if (FORBIDDEN_TAGS.has(lower)) {
      this.failAt(`${lower} elements are not supported`, 'FORBIDDEN_ELEMENT', loc);
    }
    if (RAW_TAGS.has(lower)) {
      s.rawType = lower;
      }
    s.mode = MODE.BEFORE_ATTR;
  }

  startAttribute(char) {
    const s = this.state;
    s.mode = MODE.ATTR_NAME;
    s.attrName = char;
    s.attrPolicy = policyFor(char);
    s.hasAttrLiteral = false;
    s.hasUrlOutput = false;
  }

  clearAttribute() {
    const s = this.state;
    s.attrName = '';
    s.attrPolicy = null;
    s.hasAttrLiteral = false;
    s.hasUrlOutput = false;
  }

  enterAttributeValue() {
    this.state.mode = MODE.BEFORE_ATTR_VALUE;
  }

  attributeLiteral(char, loc) {
    const s = this.state;
    if (s.attrPolicy === 'url') {
      if (s.hasUrlOutput) {
        this.failAt('dynamic href/src value must occupy the complete quoted attribute value', 'PARTIAL_DYNAMIC_URL', loc);
      }
      s.hasAttrLiteral = true;
    }
  }

  output(loc) {
    const s = this.state;
    s.lastLoc = loc;
    switch (s.mode) {
      case MODE.DATA:
        return { kind: 'text' };
      case MODE.ATTR_VALUE_DOUBLE:
      case MODE.ATTR_VALUE_SINGLE:
        return this.attributeOutput(loc);
      case MODE.BEFORE_ATTR_VALUE:
      case MODE.ATTR_VALUE_UNQUOTED:
        this.failAt('interpolation in an unquoted attribute value is forbidden', 'UNQUOTED_DYNAMIC_ATTRIBUTE', loc);
        break;
      case MODE.TAG_NAME:
      case MODE.END_TAG_NAME:
        this.failAt('interpolation in a tag name is forbidden', 'DYNAMIC_TAG_NAME', loc);
        break;
      case MODE.ATTR_NAME:
      case MODE.AFTER_ATTR_NAME:
      case MODE.BEFORE_ATTR:
        this.failAt('interpolation in an attribute name is forbidden', 'DYNAMIC_ATTRIBUTE_NAME', loc);
        break;
      case MODE.COMMENT:
      case MODE.COMMENT_DASH:
      case MODE.COMMENT_BANG_DASH:
        this.failAt('interpolation in an HTML comment is forbidden', 'DYNAMIC_COMMENT', loc);
        break;
      case MODE.RAW:
      case MODE.RAW_LT:
      case MODE.RAW_END_NAME:
      case MODE.RAW_END_TAG:
        this.failAt(`interpolation inside ${s.rawType || 'raw text'} is forbidden`, 'DYNAMIC_RAW_ELEMENT', loc);
        break;
      case MODE.PI:
        this.failAt('interpolation in a processing instruction is forbidden', 'DYNAMIC_MARKUP_DECLARATION', loc);
        break;
      case MODE.MARKUP_DECL:
        this.failAt('interpolation in a markup declaration is forbidden', 'DYNAMIC_MARKUP_DECLARATION', loc);
        break;
      default:
        this.failAt('interpolation is forbidden in this HTML context', 'FORBIDDEN_DYNAMIC_CONTEXT', loc);
    }
  }

  attributeOutput(loc) {
    const s = this.state;
    if (s.attrPolicy === 'event' || s.attrPolicy === 'dangerous') {
      this.failAt(`${s.attrName} attributes cannot contain interpolation`, 'FORBIDDEN_DYNAMIC_ATTRIBUTE', loc);
    }
    if (s.attrPolicy === 'url') {
      if (s.hasAttrLiteral) {
        this.failAt('dynamic href/src value must occupy the complete quoted attribute value', 'PARTIAL_DYNAMIC_URL', loc);
      }
      if (s.hasUrlOutput) {
        this.failAt('href/src can contain only one complete dynamic value', 'MULTIPLE_DYNAMIC_URL_VALUES', loc);
      }
      s.hasUrlOutput = true;
      return { kind: 'url' };
    }
    return { kind: 'attribute' };
  }

  closeStartTag(loc, selfClosing = false) {
    const s = this.state;
    const rawType = s.rawType;
    s.tagName = '';
    this.clearAttribute();
    s.mode = rawType && !selfClosing ? MODE.RAW : MODE.DATA;
    if (selfClosing) s.rawType = '';
  }

  closeEndTag(loc) {
    Object.assign(this.state, initialState());
  }

  closeRawEnd(loc) {
    Object.assign(this.state, initialState());
  }

  markupDeclarationChar(char, loc) {
    const s = this.state;
    s.declaration += char;
    const value = s.declaration;
    if (value === '--') {
      s.mode = MODE.COMMENT;
      s.declaration = '';
      return;
    }
    if (value.startsWith('--')) {
      this.failAt('malformed HTML comment', 'MALFORMED_TEMPLATE_HTML', loc);
    }
    if (value.startsWith('[CDATA[')) {
      this.failAt('CDATA sections are not supported', 'UNSUPPORTED_MARKUP_DECLARATION', loc);
    }
    if (char === '>') {
      if (!/^DOCTYPE(?=[\s/])/i.test(value)) {
        this.failAt('only DOCTYPE declarations and comments are supported', 'UNSUPPORTED_MARKUP_DECLARATION', loc);
      }
      s.mode = MODE.DATA;
      s.declaration = '';
    }
  }
}

export function createScanner() {
  return new HtmlScanner();
}
