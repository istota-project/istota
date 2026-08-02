// The pure half of the design lint: the rule table and a single-source scanner.
// Split out of design-lint.mjs so the rules can be unit-tested against inline
// sources — a rule that silently stops matching is the failure mode this whole
// check exists to prevent, and until now nothing verified the regexes.
//
// design-lint.mjs owns the filesystem walk, the baseline, and the CLI.

// Where tokens are defined. This was one file, `src/app.css`, until it was
// split into four layers behind @imports — the literals did not move out of
// the token home, the token home became four files, so each layer keeps the
// exemption the single file had. Deliberately NOT all of lib/styles:
// settings.css, cards.css and sidebar.css were always separate module sheets
// and were always linted, and widening the exemption to the directory would
// silently stop checking them.
export const STYLE_LAYERS = [
  'src/app.css',
  'src/lib/styles/tokens.css',
  'src/lib/styles/primitives.css',
  'src/lib/styles/app-shell.css',
  'src/lib/styles/markdown.css',
];
// app.html holds the pre-paint theme script.
export const EXEMPT_FILES = new Set([...STYLE_LAYERS, 'src/app.html']);
export const SCAN_EXTENSIONS = ['.svelte', '.ts', '.css'];
export const SKIP_DIRS = new Set(['node_modules', 'build', '.svelte-kit', 'vitest-stubs']);
// Tests are not shipped UI. They assert on the very literals the rules forbid —
// theme.test.ts checks that the chrome meta lands on #111111 — so linting them
// measures the fixture rather than the design language.
const EXEMPT_SUFFIXES = ['.test.ts'];

// `(?<!&)` rejects an HTML numeric entity: the caret glyph `&#9654;` is four
// valid hex digits behind a `#`, and appears in every collapsible list header.
//
// The functional notations matter as much as hex, and for a while only hex was
// checked: the tree accumulated one danger red written 24 times across 9
// different alpha values, invisible to the rule and frozen to its dark-theme
// value, so every one of them rendered a dark tint on white. A literal is a
// literal whichever notation it is spelled in.
//
// `rgb(var(--x) / 0.5)` and `color-mix()` are *derivations* of a token, not
// literals, so a call whose arguments are entirely `var()` references is
// allowed through — that is the sanctioned way to take a token to an alpha.
const COLOR = /(?<!&)#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\s*\(/;
const COLOR_FROM_TOKEN = /\b(?:rgba?|hsla?)\s*\(\s*(?:var\(|from\s+var\()/g;
const THEME_OVERRIDE = /\[data-theme=/;
// Bare `confirm(` is the native dialog, but this codebase also declares local
// functions named `confirm`. The lookbehind rejects `.confirm(` (a method) and
// `javascript:alert(` (XSS test fixtures); `unless` rejects the declaration.
const NATIVE_DIALOG = /\bwindow\.(?:confirm|alert)\s*\(|(?<![.:\w$])(?:confirm|alert)\s*\(/;
const NATIVE_DIALOG_DECL = /\bfunction\s+(?:confirm|alert)\b/;
// Default-import only. A *named* import from a component file (getShellScrollRoot
// from AppShell.svelte) is by definition something the barrel does not re-export.
const DEEP_IMPORT = /^\s*import\s+[A-Za-z]\w*\s+from '\$lib\/components\/ui\/[A-Za-z]+\.svelte'/;
const MONEY_GLOBAL = /:global\(\.money-/;
const ALLOW_LINE = /design-lint-allow\b/;
const ALLOW_FILE = /design-lint-allow-file\b/;
// A categorical palette is a contiguous block — the admin SOURCE_COLOR map is
// eleven lines — and per-line comments would bury it. The region form exempts a
// span without reaching for the file-level escape hatch, which would also
// blanket anything added to the file later.
const ALLOW_BEGIN = /design-lint-allow-begin\b/;
const ALLOW_END = /design-lint-allow-end\b/;

// A `var()` naming a property nothing defines is not a style choice, it is a
// bug — and a silent one. `font-size: var(--text-lg)` on an undefined token is
// "invalid at computed-value time", which does not fall back to the UA default:
// the property computes to the *inherited* value, so a heading quietly renders
// at body-text size. Seven headings shipped that way.
//
// A written fallback (`var(--text-lg, 1.05rem)`) renders correctly, so it is a
// separate, softer finding: the page is fine, but the token is a fiction and
// the fallback is a value that never reaches the theme blocks. Two rule ids
// rather than a severity axis, so each can be baselined on its own.
const TOKEN_REF = /var\(\s*(--[\w-]+)\s*(,)?/g;
// `[:=]` so Svelte's `style:--composer-h={h}` directive counts as a definition
// alongside a plain `--composer-h: 3rem` declaration. A reference never matches:
// `var(--x)` and `var(--x, y)` are followed by `)` and `,`.
const TOKEN_DEF = /(--[\w-]+)\s*[:=]/g;

/** Custom properties a source file defines itself, in CSS or an inline style. */
export function tokensDefinedIn(source) {
  const found = new Set();
  for (const [, name] of source.matchAll(TOKEN_DEF)) found.add(name);
  return found;
}

// `ctx.tokens` is the global roster (app.css + lib/styles/*.css); a component
// may also define its own on a selector or an inline `style=`, which is how
// `--card-padding` and the composer's overlay padding work, so the file's own
// definitions count as defined regardless of where in the file they appear.
function undefinedTokensOn(line, ctx, wantFallback) {
  if (!ctx?.tokens) return null;
  const names = [];
  for (const [, name, comma] of line.matchAll(TOKEN_REF)) {
    if (Boolean(comma) !== wantFallback) continue;
    if (ctx.tokens.has(name) || ctx.localTokens?.has(name)) continue;
    names.push(name);
  }
  return names.length ? names : null;
}

// Spacing off the 4px ramp. The tree had 33 distinct rem values across 1,218
// spacing declarations before the scale existed, roughly half on no ramp at
// all — which is how the same gap ended up written as 0.3, 0.35 and 0.4rem on
// three pages that meant the same thing.
//
// Only rem is checked. px is correct for a border or a hairline nudge, em is
// relative to the control it sits on, and % / vh / dvh are layout rather than
// spacing. A value inside calc()/clamp()/min()/max() is skipped: it is
// arithmetic, and the operands are not independently meaningful.
const SPACE_PROPS =
  '(?:padding|margin|gap|row-gap|column-gap|inset)' +
  '(?:-(?:top|right|bottom|left|inline|block)(?:-(?:start|end))?)?';
const SPACE_DECL = new RegExp(`^\\s*${SPACE_PROPS}\\s*:\\s*([^;]+);`);
const SPACE_VALUE = /(?<![\w.-])(\d*\.?\d+)rem(?![\w-])/g;
const SPACE_FN = /(?:calc|clamp|min|max)\(/;
const SPACE_SCALE = new Set(['0.25', '0.5', '0.75', '1', '1.5', '2']);

function offScaleSpacing(line) {
  const decl = SPACE_DECL.exec(line);
  if (!decl || SPACE_FN.test(decl[1])) return null;
  const found = [];
  for (const [, raw] of decl[1].matchAll(SPACE_VALUE)) {
    const value = raw.includes('.') ? raw.replace(/0+$/, '').replace(/\.$/, '') : raw;
    if (!SPACE_SCALE.has(value)) found.push(`${raw}rem`);
  }
  return found.length ? found : null;
}

// A page redefining a primitive's class name is how health ended up with a
// second .btn system that inverted what `primary` meant — filled in one, an
// outlined blue in the other — and with .btn.danger rendering red on three
// pages and grey-until-hover on two. The rule names the classes the ui/
// primitives own; each is exempt in the component that defines it.
const REDEFINED_PRIMITIVE =
  /^\s*(?::global\()?\.(?:btn|badge|icon-btn|field|chip)\b(?![\w-])[^{]*\{\s*$/;

// `exempt` is the file that legitimately owns a pattern — the shell definition
// site, as opposed to EXEMPT_FILES which is global across every rule.
// `strip` blanks a span before the rule is tested, so a line carrying both a
// sanctioned form and a literal still reports the literal — which `unless`,
// being whole-line, would swallow.
export const RULES = [
  {
    id: 'raw-color',
    test: COLOR,
    strip: COLOR_FROM_TOKEN,
    hint: 'use a token from app.css',
  },
  { id: 'theme-override', test: THEME_OVERRIDE, hint: 'define a token pair in app.css instead' },
  {
    id: 'native-dialog',
    test: NATIVE_DIALOG,
    unless: NATIVE_DIALOG_DECL,
    hint: 'use ConfirmDialog from $lib/components/ui',
  },
  {
    id: 'deep-import',
    test: DEEP_IMPORT,
    hint: "import from the barrel: '$lib/components/ui'",
  },
  {
    id: 'stray-money-global',
    test: MONEY_GLOBAL,
    exempt: ['src/routes/money/+layout.svelte'],
    hint: 'the money table shell is defined once in routes/money/+layout.svelte',
  },
  {
    id: 'redefined-primitive',
    test: REDEFINED_PRIMITIVE,
    exempt: [
      'src/lib/components/ui/Button.svelte',
      'src/lib/components/ui/Badge.svelte',
      'src/lib/components/ui/IconButton.svelte',
      'src/lib/components/ui/Field.svelte',
      'src/lib/components/ui/Chip.svelte',
    ],
    hint: 'this class belongs to a ui/ primitive — use the component instead',
  },
  {
    id: 'off-scale-space',
    match: (line) => offScaleSpacing(line),
    exempt: STYLE_LAYERS,
    hint: 'use a --space-* token, or mark the exception with a reason',
  },
  {
    id: 'undefined-token',
    match: (line, ctx) => undefinedTokensOn(line, ctx, false),
    hint: 'this var() resolves to nothing — define the token pair in app.css',
  },
  {
    id: 'undefined-token-fallback',
    match: (line, ctx) => undefinedTokensOn(line, ctx, true),
    hint: 'the fallback works, but the token does not exist — define it in app.css',
  },
];

// Blank out comment spans so a color *named in prose* ("(#c66 dark / #c0271d
// light)") reads as documentation rather than a declaration. Block comments run
// across lines, so the open state is carried by the caller as it walks the file.
// Spans are replaced by spaces rather than removed, keeping column positions
// intact for anything that later wants them.
//
// Quoted spans matter here for one reason: `accept="image/*,application/pdf"`
// contains a `/*` that opens a block comment which never closes, so every line
// after it in the file was blanked and silently exempt from every rule. Four
// upload pages were invisible to the lint that way, including the seventh
// broken heading this rule exists to find. Quote state is per-line, because a
// stuck *string* state would only mis-read the rest of one line, while a stuck
// *comment* state disables the file — and an unpaired apostrophe in prose
// ("don't") is common enough that carrying it across lines would do just that.
// A quoted span also shields `//`, so a URL in a string no longer swallows the
// rest of its line; a bare `//` outside quotes still does, which is the case
// the swallow was for.
function stripComments(line, state) {
  let out = '';
  let i = 0;
  let quote = '';
  while (i < line.length) {
    if (!state.block && !state.html) {
      if (quote) {
        if (line[i] === quote && line[i - 1] !== '\\') quote = '';
        out += line[i];
        i++;
        continue;
      }
      if (line[i] === '"' || line[i] === "'" || line[i] === '`') {
        quote = line[i];
        out += line[i];
        i++;
        continue;
      }
    }
    if (state.block || state.html) {
      const close = state.block ? '*/' : '-->';
      const end = line.indexOf(close, i);
      if (end === -1) return out + ' '.repeat(line.length - i);
      out += ' '.repeat(end + close.length - i);
      i = end + close.length;
      state.block = false;
      state.html = false;
      continue;
    }
    if (line.startsWith('/*', i)) {
      state.block = true;
      out += '  ';
      i += 2;
      continue;
    }
    if (line.startsWith('<!--', i)) {
      state.html = true;
      out += '    ';
      i += 4;
      continue;
    }
    // Also swallows a URL's `//`, which is the right call: a hex-looking
    // fragment in a link is not a color declaration either.
    if (line.startsWith('//', i)) return out + ' '.repeat(line.length - i);
    out += line[i];
    i++;
  }
  return out;
}

// Scan one file's source. `rel` is the web-root-relative path, used by the
// per-file exemptions; the source is passed in so a caller can lint a string.
// `ctx.tokens` is the global custom-property roster, supplied by the caller
// that read app.css — omit it and the token rules simply stand down.
export function scanSource(rel, source, ctx = {}) {
  if (EXEMPT_FILES.has(rel)) return [];
  if (EXEMPT_SUFFIXES.some((suffix) => rel.endsWith(suffix))) return [];
  if (ALLOW_FILE.test(source)) return [];

  const scanCtx = { ...ctx, localTokens: tokensDefinedIn(source) };
  const violations = [];
  const lines = source.split('\n');
  const commentState = { block: false, html: false };
  let inAllowRegion = false;
  let pendingAllow = false;
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    // Allow comments live *in* comments, so they are matched against the raw
    // line; the rules are matched against the comment-stripped one. The strip
    // must run on every line to keep the block-comment state in sync, even on
    // lines the allow markers skip.
    const line = stripComments(raw, commentState);
    if (ALLOW_END.test(raw)) {
      inAllowRegion = false;
      continue;
    }
    if (ALLOW_BEGIN.test(raw)) {
      inAllowRegion = true;
      continue;
    }
    if (inAllowRegion) continue;
    // An allow comment covers the next line carrying code, not the next line
    // full stop — a reason worth writing usually needs more than one line, and
    // requiring the marker to sit on the literal line above would force every
    // exemption onto a single cramped comment.
    if (ALLOW_LINE.test(raw)) {
      pendingAllow = true;
      continue;
    }
    const isCode = line.trim().length > 0;
    if (pendingAllow) {
      if (!isCode) continue;
      pendingAllow = false;
      continue;
    }
    for (const rule of RULES) {
      // Substring match so a rule can exempt one owning file ("routes/money/+layout.svelte")
      // or a whole class of file (".test.ts", whose fixtures deliberately contain the pattern).
      if (rule.exempt?.some((p) => rel.includes(p))) continue;
      if (rule.unless?.test(line)) continue;
      const subject = rule.strip ? line.replace(rule.strip, (m) => ' '.repeat(m.length)) : line;
      // `match` reports one violation per offending name, so a line naming two
      // dead tokens is two findings and the message can say which.
      if (rule.match) {
        for (const name of rule.match(subject, scanCtx) ?? []) {
          violations.push({
            file: rel,
            line: i + 1,
            rule: rule.id,
            text: raw.trim(),
            hint: `${name}: ${rule.hint}`,
          });
        }
        continue;
      }
      if (rule.test.test(subject)) {
        violations.push({
          file: rel,
          line: i + 1,
          rule: rule.id,
          text: raw.trim(),
          hint: rule.hint,
        });
      }
    }
  }
  return violations;
}
