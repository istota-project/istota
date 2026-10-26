import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { rules, styleFiles, styleBlocks } from './cascade';

/**
 * iOS zooms the page whenever a focused text control computes under 16px, and
 * does not cleanly zoom back out (ISSUE-219). The floor that stops it is a
 * coarse-pointer redefinition of the type tokens *on the control itself*, in
 * minot's primitives.css, and minot's touchZoom test pins it, along with the
 * page-wide `touch-action: manipulation`.
 *
 * This is the app half: no stylesheet here sizes a control in a way that reads
 * no token and so slips underneath the floor, and none re-allows double-tap
 * zoom.
 */

const SRC = resolve(process.cwd(), 'src');
/**
 * A double tap anywhere in the app is a tap, never a zoom (ISSUE-818). The
 * opt-out is page-wide rather than per control: the first tap of a double tap
 * can change what sits under the finger (the sidebar toggle opens a drawer), so
 * the second tap lands on an element nobody thought to opt out.
 */
describe('double-tap zoom is off page-wide', () => {
  it('lets no stylesheet re-allow double-tap zoom on an element', () => {
    // `auto` is the only value that allows double-tap zoom; `none`, `pan-*` and
    // `pinch-zoom` all exclude it, for a component handling its own gesture.
    // Stylesheets only: an inline style or `el.style.touchAction` is not seen.
    const offenders: string[] = [];
    for (const file of styleFiles(SRC)) {
      const rel = file.slice(SRC.length);
      const source = readFileSync(file, 'utf8');
      for (const block of styleBlocks(file, source)) {
        for (const rule of rules(block)) {
          for (const m of rule.body.matchAll(/(?:^|[;\s])touch-action:\s*([^;}]+)/g)) {
            const value = m[1].trim();
            if (!/\bauto\b/.test(value)) continue;
            offenders.push(`${rel}: ${rule.selector} → ${value}`);
          }
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});

/**
 * A control whose size is inherited or written as a literal reads no token, so
 * the floor above cannot reach it. Each exemption states why it is one.
 *
 * Keyed on file *and* selector rather than file alone: a file-wide entry would
 * blanket a control added to that component later, which is the failure
 * `web/AGENTS.md` records for `design-lint-allow-file`.
 */
const SIZED_ELSEWHERE = new Map([
  [
    '/lib/components/chat/Composer.svelte: textarea',
    'the pill around the textarea owns the font-size and every internal metric ' +
      'is em-relative to it, so the composer carries its own focus-time viewport ' +
      'guard instead',
  ],
  [
    '/lib/components/TextFileEditor.svelte: .text-file-editor :global(textarea)',
    'a file editor keeps its desktop density on a phone, resolved on the wrapper ' +
      'outside the floor, and pins the zoom while focused instead',
  ],
  [
    '/lib/components/viewer/TextPreview.svelte: .file-body :global(textarea)',
    'the plain-text source view, sized like the file editor for the same reason',
  ],
]);

describe('stylesheets do not slip under the floor', () => {
  // The `(` in the leading class is load-bearing: `:global(input)` is this
  // codebase's dominant idiom for styling a control from a wrapper — it is how
  // `ui/Field.svelte` sizes most of the app's inputs — and it also covers
  // `:is()`/`:where()`. Only element-selected rules are checked: a class name
  // says nothing about which element it lands on, so a broader scan would be
  // guesswork. That makes this a net rather than a proof.
  const CONTROL_SELECTOR = /(^|[\s,>+~(])(input|textarea|select)(?![\w-])/;

  it('sizes every element-selected control through a floored token', () => {
    const offenders: string[] = [];
    for (const file of styleFiles(SRC)) {
      const rel = file.slice(SRC.length);
      const source = readFileSync(file, 'utf8');
      for (const block of styleBlocks(file, source)) {
        for (const rule of rules(block)) {
          if (!CONTROL_SELECTOR.test(rule.selector)) continue;
          if (SIZED_ELSEWHERE.has(`${rel}: ${rule.selector}`)) continue;
          const size = /(?:^|[;\s])font-size:\s*([^;}]+)/.exec(rule.body)?.[1]?.trim();
          const shorthand = /(?:^|[;\s])font:\s*([^;}]+)/.exec(rule.body)?.[1]?.trim();
          // The shorthand resets font-size, so a rule carrying only `font:`
          // takes its size from whatever it names (`inherit`, in practice).
          const effective = size ?? (shorthand ? `font: ${shorthand}` : undefined);
          if (!effective) continue;
          if (effective.startsWith('var(--text-')) continue;
          // The apps kit styles a document without primitives.css, so it writes the floor out.
          if (/^max\(16px,\s*var\(--text-/.test(effective)) continue;
          offenders.push(`${rel}: ${rule.selector} → ${effective}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it('catches a control sized past the floor however the rule is written', () => {
    // A guard on the guard: the scan is a regex over stylesheet text, and its
    // failure mode is matching nothing at all. These are the four shapes the
    // tree actually uses.
    for (const selector of [
      '.field input',
      ".field :global(input:not([type='checkbox'])), .field :global(textarea)",
      'input, select',
      '.value-row select.unit-select',
    ]) {
      expect(CONTROL_SELECTOR.test(selector), selector).toBe(true);
    }
    expect(CONTROL_SELECTOR.test('.nav-select, .select-trigger')).toBe(false);
  });

  it('keeps every exemption pointed at a rule that still exists', () => {
    // An exemption outliving its rule is an exemption nobody is reading, and
    // the next control matching that selector inherits it silently.
    const live = new Set<string>();
    for (const file of styleFiles(SRC)) {
      const rel = file.slice(SRC.length);
      const source = readFileSync(file, 'utf8');
      for (const block of styleBlocks(file, source)) {
        for (const rule of rules(block)) live.add(`${rel}: ${rule.selector}`);
      }
    }
    for (const key of SIZED_ELSEWHERE.keys()) expect(live).toContain(key);
  });

  it('pins the zoom in every component an exemption names', () => {
    // An exempt control renders under 16px on purpose, so the floor no longer
    // stops iOS zooming on focus; the viewport pin is what does.
    for (const key of SIZED_ELSEWHERE.keys()) {
      const file = key.slice(0, key.indexOf(': '));
      const source = readFileSync(resolve(SRC, '.' + file), 'utf8');
      expect(source, file).toContain('pinViewportZoom');
      expect(source, file).toContain('releaseViewportZoom');
    }
  });
});
