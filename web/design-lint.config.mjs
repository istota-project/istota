// The design lint's view of this app, read by `minot-lint` (`npm run lint:design`).
// Paths are relative to web/. The rules themselves are minot's; see
// node_modules/@minot/ui/AGENTS.md, "Checks".

/** @type {import('@minot/ui/lint').LintConfig} */
export default {
  src: 'src',
  tokenSources: ['src/app.css', 'src/lib/styles'],
  // The app's token home, file by file. Not all of lib/styles: moneyTable.css
  // is a module sheet and stays linted.
  styleLayers: [
    'src/app.css',
    'src/lib/styles/app-tokens.css',
    'src/lib/styles/app-shell.css',
    'src/lib/styles/markdown.css',
  ],
  baseline: 'scripts/design-lint-baseline.json',
  barrels: ['$lib/components/notifications', '$lib/components/search'],
  extraRules: [
    {
      id: 'stray-money-global',
      test: /:global\(\.money-/,
      exempt: ['src/routes/money/+layout.svelte'],
      hint: 'the money table shell is lib/styles/moneyTable.css — do not redefine a .money-* class',
    },
  ],
};
