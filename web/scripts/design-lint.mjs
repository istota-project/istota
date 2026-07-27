#!/usr/bin/env node
// Design-language lint. Rules drawn from web/AGENTS.md:
//
//   raw-color          a hex literal outside app.css (the token home)
//   theme-override     a hand-written :root[data-theme='light'] rule outside app.css
//   native-dialog      window.confirm / window.alert instead of ConfirmDialog
//   deep-import        a ui primitive imported by file path instead of the barrel
//   stray-money-global a .money-* shell class defined outside routes/money/+layout.svelte
//
// Both are legitimate in a few places (categorical palettes, data viz, fixed
// chrome), so violations are suppressed by an explicit allow comment rather
// than by the rule being loose. Everything else is measured against a baseline
// of what already exists, so the check is actionable on a codebase that
// already drifted: a file may keep its current count, but may not gain one,
// and a clean file may not become dirty.

import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(WEB_ROOT, 'src');
const BASELINE = join(WEB_ROOT, 'scripts', 'design-lint-baseline.json');

// app.css is where tokens are defined; app.html holds the pre-paint theme script.
const EXEMPT_FILES = new Set(['src/app.css', 'src/app.html']);
const SCAN_EXTENSIONS = ['.svelte', '.ts', '.css'];
const SKIP_DIRS = new Set(['node_modules', 'build', '.svelte-kit', 'vitest-stubs']);
// Tests are not shipped UI. They assert on the very literals the rules forbid —
// theme.test.ts checks that the chrome meta lands on #111111 — so linting them
// measures the fixture rather than the design language.
const EXEMPT_SUFFIXES = ['.test.ts'];

// `(?<!&)` rejects an HTML numeric entity: the caret glyph `&#9654;` is four
// valid hex digits behind a `#`, and appears in every collapsible list header.
const HEX = /(?<!&)#[0-9a-fA-F]{3,8}\b/;
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

// `exempt` is the file that legitimately owns a pattern — the shell definition
// site, as opposed to EXEMPT_FILES which is global across every rule.
const RULES = [
  { id: 'raw-color', test: HEX, hint: 'use a token from app.css' },
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
];

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (SCAN_EXTENSIONS.some((ext) => entry.endsWith(ext))) out.push(full);
  }
  return out;
}

// Blank out comment spans so a color *named in prose* ("(#c66 dark / #c0271d
// light)") reads as documentation rather than a declaration. Block comments run
// across lines, so the open state is carried by the caller as it walks the file.
// Spans are replaced by spaces rather than removed, keeping column positions
// intact for anything that later wants them.
function stripComments(line, state) {
  let out = '';
  let i = 0;
  while (i < line.length) {
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

function scanFile(path) {
  const rel = relative(WEB_ROOT, path);
  if (EXEMPT_FILES.has(rel)) return [];
  if (EXEMPT_SUFFIXES.some((suffix) => rel.endsWith(suffix))) return [];

  const source = readFileSync(path, 'utf8');
  if (ALLOW_FILE.test(source)) return [];

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
      if (rule.test.test(line)) {
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

function tally(violations) {
  const counts = {};
  for (const v of violations) {
    counts[v.file] ??= {};
    counts[v.file][v.rule] = (counts[v.file][v.rule] ?? 0) + 1;
  }
  return counts;
}

function loadBaseline() {
  try {
    return JSON.parse(readFileSync(BASELINE, 'utf8'));
  } catch {
    return {};
  }
}

const violations = walk(SRC).flatMap(scanFile);
const counts = tally(violations);

// --list [rule-id] dumps every violation regardless of baseline. The triage
// view: use it when adding a rule, before deciding what the baseline should be.
if (process.argv.includes('--list')) {
  const wanted = process.argv[process.argv.indexOf('--list') + 1];
  const shown =
    wanted && !wanted.startsWith('--') ? violations.filter((v) => v.rule === wanted) : violations;
  for (const v of shown) {
    console.log(`${v.rule}\t${v.file}:${v.line}\t${v.text.slice(0, 120)}`);
  }
  console.log(`\n${shown.length} violation(s)${wanted ? ` for rule "${wanted}"` : ''}`);
  process.exit(0);
}

if (process.argv.includes('--update-baseline')) {
  const sorted = Object.fromEntries(Object.entries(counts).sort(([a], [b]) => a.localeCompare(b)));
  writeFileSync(BASELINE, JSON.stringify(sorted, null, 2) + '\n');
  const total = violations.length;
  console.log(
    `design-lint: baseline written — ${total} existing violation(s) across ${Object.keys(sorted).length} file(s)`,
  );
  process.exit(0);
}

const baseline = loadBaseline();
const regressions = [];

for (const [file, rules] of Object.entries(counts)) {
  for (const [rule, count] of Object.entries(rules)) {
    const allowed = baseline[file]?.[rule] ?? 0;
    if (count > allowed) {
      regressions.push({ file, rule, count, allowed });
    }
  }
}

if (regressions.length === 0) {
  const remaining = violations.length;
  console.log(
    remaining === 0
      ? 'design-lint: clean'
      : `design-lint: ok — ${remaining} baselined violation(s) remaining, none new`,
  );
  process.exit(0);
}

console.error('design-lint: new violations\n');
for (const r of regressions) {
  const rule = RULES.find((x) => x.id === r.rule);
  console.error(
    `  ${r.file}  [${r.rule}]  ${r.count} found, ${r.allowed} baselined — ${rule.hint}`,
  );
  for (const v of violations.filter((v) => v.file === r.file && v.rule === r.rule).slice(0, 5)) {
    console.error(`      ${r.file}:${v.line}  ${v.text.slice(0, 90)}`);
  }
}
console.error(`
See web/AGENTS.md "Color". Fixes, in order of preference:
  1. Use an existing token from src/app.css.
  2. Add a new token pair to BOTH the :root and :root[data-theme='light'] blocks.
  3. If this is a categorical palette, data viz, or fixed chrome, mark it:
       /* design-lint-allow: <reason> */        on or above the line
       <!-- design-lint-allow-file: <reason> -->  for the whole file
`);
process.exit(1);
