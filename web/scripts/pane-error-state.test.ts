import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
// @ts-expect-error — plain .mjs module, no types
import { findPaneErrorViolations, ifChains } from './pane-error-state.mjs';

const WEB_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(WEB_ROOT, 'src');
const SKIP_DIRS = new Set(['node_modules', 'build', '.svelte-kit', 'vitest-stubs']);

function svelteFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) svelteFiles(full, out);
    else if (entry.endsWith('.svelte')) out.push(full);
  }
  return out;
}

// The controls. A checker asserting over a tree that already passes tells you
// nothing about whether it can fail, so each shape it must catch and each shape
// it must leave alone is named here against an inline source.
describe('findPaneErrorViolations', () => {
  it('catches the money shape: a top-aligned .error-msg twin', () => {
    const found = findPaneErrorViolations(`
{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="error-msg">{error}</div>
{/if}`);
    expect(found).toHaveLength(1);
    expect(found[0].markup).toContain('error-msg');
  });

  it('catches the health shape: a .banner error twin', () => {
    const found = findPaneErrorViolations(`
{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error && !panel}
  <div class="banner error">{error}</div>
{/if}`);
    expect(found).toHaveLength(1);
    expect(found[0].cond).toBe('error && !panel');
  });

  it('catches it with the error branch written first', () => {
    const found = findPaneErrorViolations(`
{#if error}
  <p class="status error">{error}</p>
{:else if loading}
  <p class="center-msg">Loading…</p>
{/if}`);
    expect(found).toHaveLength(1);
  });

  it('passes the fixed shape', () => {
    expect(
      findPaneErrorViolations(`
{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  <div class="center-msg error">{error}</div>
{/if}`),
    ).toEqual([]);
  });

  it('leaves a banner above a still-rendered layout alone', () => {
    // Its own chain, no loading twin: the layout below it still renders, so the
    // banner is not the pane. This is the legitimate `.banner error` and it
    // outnumbers the whole-pane case in the tree.
    expect(
      findPaneErrorViolations(`
{#if error}
  <div class="banner error">{error}</div>
{/if}

{#if loading}
  <div class="center-msg">Loading…</div>
{:else}
  <div class="report">…</div>
{/if}`),
    ).toEqual([]);
  });

  it('does not read a nested chain as a branch of the one around it', () => {
    // money/taxes: the outer chain is fixed, and the inner `{#if error}` is a
    // banner above rendered data. Counting the inner markup as the outer
    // branch's own would report the outer branch clean and the inner one never.
    expect(
      findPaneErrorViolations(`
{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error && !data}
  <div class="center-msg error">{error}</div>
{:else}
  {#if error}
    <div class="error-msg">{error}</div>
  {/if}
  <div class="rows">…</div>
{/if}`),
    ).toEqual([]);
  });

  it('ignores a branch that renders no element', () => {
    expect(
      findPaneErrorViolations(`
{#if loading}
  <div class="center-msg">Loading…</div>
{:else if error}
  {@render fallback()}
{/if}`),
    ).toEqual([]);
  });

  it('closes a block whose expression carries its own braces', () => {
    // `{#each rows as { id }}` balances to the second `}`. Reading to the first
    // one leaves the scanner a marker short and every chain after it misparsed.
    const chains = ifChains(`
{#if loading}
  {#each rows as { id }}
    <span>{id}</span>
  {/each}
{:else if error}
  <div class="banner error">{error}</div>
{/if}`);
    expect(chains).toHaveLength(1);
    expect(chains[0].branches.map((b: { cond: string }) => b.cond)).toEqual(['loading', 'error']);
  });
});

describe('the routes themselves', () => {
  it('render every whole-pane load failure as .center-msg error', () => {
    const offenders: string[] = [];
    for (const file of svelteFiles(SRC)) {
      for (const v of findPaneErrorViolations(readFileSync(file, 'utf8'))) {
        offenders.push(`${relative(WEB_ROOT, file)}:${v.line}  {:else if ${v.cond}}  ${v.markup}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
