import { describe, expect, it } from 'vitest';
import { RULES, scanSource } from '@minot/ui/lint';
import config from '../design-lint.config.mjs';

// This app's one product rule, run the way minot-lint runs it: minot's rules
// plus the config's extraRules, against the real config file.
const rules = [...RULES, ...config.extraRules];
const source = '<style>\n  :global(.money-x) {\n    color: red;\n  }\n</style>\n';
const stray = (rel: string) =>
  scanSource(rel, source, { rules }).filter((v) => v.rule === 'stray-money-global');

describe('stray-money-global', () => {
  it('flags a money shell class defined outside the money layout', () => {
    expect(stray('src/routes/feeds/+page.svelte')).toHaveLength(1);
  });

  it('allows it in the layout that owns the shell', () => {
    expect(stray('src/routes/money/+layout.svelte')).toEqual([]);
  });
});
