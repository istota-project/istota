#!/usr/bin/env node
// Design-language lint. Rules drawn from web/AGENTS.md:
//
//   raw-color          a color literal outside app.css (the token home) — hex or
//                      any rgb()/rgba()/hsl()/hsla() notation
//   theme-override     a hand-written :root[data-theme='light'] rule outside app.css
//   native-dialog      confirm / alert / prompt, bare or on window, instead of
//                      Modal or ConfirmDialog
//   deep-import        a ui primitive imported by file path instead of the barrel
//   stray-money-global a .money-* shell class defined outside routes/money/+layout.svelte
//   undefined-token    a bare var(--x) naming a property nothing defines
//   undefined-token-fallback  the same, but with a written fallback, so it renders
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
import {
  RULES,
  SCAN_EXTENSIONS,
  SKIP_DIRS,
  scanSource,
  tokensDefinedIn,
} from './design-lint-rules.mjs';

const WEB_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(WEB_ROOT, 'src');
const BASELINE = join(WEB_ROOT, 'scripts', 'design-lint-baseline.json');
// The shared roster: app.css is the token home, and lib/styles/*.css carries a
// few section-scoped ones. A component's own custom properties are resolved
// per-file inside scanSource.
const TOKEN_SOURCES = ['src/app.css', 'src/lib/styles'];

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (SCAN_EXTENSIONS.some((ext) => entry.endsWith(ext))) out.push(full);
  }
  return out;
}

function collectTokens() {
  const tokens = new Set();
  for (const entry of TOKEN_SOURCES) {
    const full = join(WEB_ROOT, entry);
    const files = statSync(full).isDirectory() ? walk(full) : [full];
    for (const file of files) {
      for (const name of tokensDefinedIn(readFileSync(file, 'utf8'))) tokens.add(name);
    }
  }
  return tokens;
}

const TOKENS = collectTokens();

function scanFile(path) {
  return scanSource(relative(WEB_ROOT, path), readFileSync(path, 'utf8'), { tokens: TOKENS });
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
