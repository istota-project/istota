// vitest-setup.ts imports the jest-dom matchers at runtime, but nothing pulled
// their *types* into the program, so `expect(el).toBeDisabled()` ran fine and
// failed svelte-check. A side-effect import in a .d.ts applies the module
// augmentation globally without touching `compilerOptions.types`, which on a
// SvelteKit project would replace the generated type list rather than add to it.
import '@testing-library/jest-dom/vitest';
