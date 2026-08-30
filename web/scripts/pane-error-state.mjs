// One rule, checked across every route: a load failure that replaces the whole
// pane renders `.center-msg error`, the same box as the `.center-msg` loading
// placeholder it stands in for.
//
// It is not in design-lint-rules.mjs because that lint is line-based, and the
// thing being checked spans a whole `{#if}` chain: the tell for "this error IS
// the pane" is that its branch is the exclusive twin of the loading branch, not
// anything visible on the line itself. A line-based rule cannot separate that
// from `{#if error}<div class="banner error">` sitting above a layout that is
// still rendered, which is the legitimate case and outnumbers it.
//
// It drifted three ways before anything checked it. Money used `.error-msg`
// (0.85rem, top-aligned), health and admin used `.banner error` (a tinted
// full-width strip at the top of the pane), and only feeds and location used
// the centered box. All three render on the same trigger — the module's data
// did not load — so a user moving between tabs saw the same failure three
// different sizes, colors and positions.

/** Block tags that open a nesting level. Only `if` starts a branch chain. */
const OPENERS = new Set(['if', 'each', 'await', 'key', 'snippet']);
const CENTER_MSG = /class="[^"]*\bcenter-msg\b/;
const ANY_CLASS = /class="/;
const MENTIONS_ERROR = /\berror\b/;

/**
 * Split Svelte markup into its `{#…}` / `{:…}` / `{/…}` markers.
 *
 * Braces are matched by balance rather than by `[^}]*`, so an expression
 * carrying its own braces — `{#each rows as { id }}` — closes where it really
 * closes instead of one character into itself. Anything not starting with
 * `#`, `:` or `/` is an interpolation, not a block, and is left in the content.
 */
function markers(source) {
  const out = [];
  let i = 0;
  while (i < source.length) {
    const open = source.indexOf('{', i);
    if (open < 0) break;
    const sigil = source[open + 1];
    if (sigil !== '#' && sigil !== ':' && sigil !== '/') {
      i = open + 1;
      continue;
    }
    let depth = 0;
    let close = open;
    for (; close < source.length; close++) {
      if (source[close] === '{') depth++;
      else if (source[close] === '}' && --depth === 0) break;
    }
    if (close >= source.length) break;
    out.push({ sigil, body: source.slice(open + 2, close).trim(), start: open, end: close + 1 });
    i = close + 1;
  }
  return out;
}

function lineOf(source, offset) {
  let line = 1;
  for (let i = 0; i < offset; i++) if (source[i] === '\n') line++;
  return line;
}

/**
 * Every `{#if}` chain in the source, with each branch's condition and the
 * markup written *directly* in it. Content inside a nested block belongs to
 * that block, not to the branch around it — otherwise a branch rendering a
 * subtree that happens to contain its own `.center-msg` would read as the
 * loading branch.
 */
export function ifChains(source) {
  const chains = [];
  const stack = [];
  const marks = markers(source);

  const addContent = (text) => {
    const top = stack[stack.length - 1];
    if (top?.chain) top.chain.branches[top.chain.branches.length - 1].content += text;
  };

  let cursor = 0;
  for (const mark of marks) {
    addContent(source.slice(cursor, mark.start));
    cursor = mark.end;

    if (mark.sigil === '#') {
      const tag = mark.body.split(/\s/, 1)[0];
      if (!OPENERS.has(tag)) continue;
      if (tag === 'if') {
        const chain = {
          branches: [
            { cond: mark.body.slice(2).trim(), content: '', line: lineOf(source, mark.start) },
          ],
        };
        chains.push(chain);
        stack.push({ tag, chain });
      } else {
        stack.push({ tag, chain: null });
      }
    } else if (mark.sigil === ':') {
      const top = stack[stack.length - 1];
      if (!top?.chain || !mark.body.startsWith('else')) continue;
      const cond = mark.body
        .slice(4)
        .replace(/^\s*if\b/, '')
        .trim();
      top.chain.branches.push({ cond, content: '', line: lineOf(source, mark.start) });
    } else if (mark.sigil === '/') {
      const tag = mark.body.trim();
      if (OPENERS.has(tag)) stack.pop();
    }
  }
  return chains;
}

/**
 * Chains where the loading placeholder and its error twin disagree.
 *
 * A chain qualifies only if some branch renders a `.center-msg` — that is what
 * establishes the slot as a whole-pane state. The error branch of that same
 * chain then has to render one too. A branch rendering no element at all is
 * left alone: it is a snippet call or nothing, not a status box.
 */
export function findPaneErrorViolations(source) {
  const found = [];
  for (const chain of ifChains(source)) {
    const hasCentered = chain.branches.some((b) => CENTER_MSG.test(b.content));
    if (!hasCentered) continue;
    for (const branch of chain.branches) {
      if (!MENTIONS_ERROR.test(branch.cond)) continue;
      if (CENTER_MSG.test(branch.content) || !ANY_CLASS.test(branch.content)) continue;
      found.push({
        line: branch.line,
        cond: branch.cond,
        markup: branch.content.trim().split('\n')[0].trim(),
      });
    }
  }
  return found;
}
