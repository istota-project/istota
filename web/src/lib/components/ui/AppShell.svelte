<script lang="ts" module>
  import { getContext, setContext } from 'svelte';

  const SHELL_SCROLL_ROOT = Symbol('shell-scroll-root');

  export function setShellScrollRoot(getter: () => HTMLElement | undefined): void {
    setContext(SHELL_SCROLL_ROOT, getter);
  }

  export function getShellScrollRoot(): (() => HTMLElement | undefined) | undefined {
    return getContext<() => HTMLElement | undefined>(SHELL_SCROLL_ROOT);
  }
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    header: Snippet;
    sidebar?: Snippet;
    children: Snippet;
    extras?: Snippet;
    /** Whether the shell keeps its scroll area clear of the device's bottom
     * safe area (rounded corners / home indicator). Pass `false` when the page's
     * own bottom-most element carries that inset instead — chat does, so its
     * composer fill can bleed into the strip while its controls sit above it. */
    insetBottom?: boolean;
  }

  let { header, sidebar, children, extras, insetBottom = true }: Props = $props();

  let mainEl: HTMLDivElement | undefined = $state();
  setShellScrollRoot(() => mainEl);
</script>

<div class="shell">
  <div class="shell-header">{@render header()}</div>
  {#if extras}{@render extras()}{/if}
  <div class="shell-body">
    {#if sidebar}{@render sidebar()}{/if}
    <div class="shell-main" class:inset-bottom={insetBottom} bind:this={mainEl}>
      {@render children()}
    </div>
  </div>
</div>

<style>
  /* Fills whatever the page layout hands it: `.app-content.app-content-fill` is
	   a `flex: 1` child of a `100dvh` body and carries no padding, so the shell is
	   edge to edge with no negative margin and no `100vh - <nav height>` guess.
	   That guess was a hardcoded 42/36px against a nav whose real height moves
	   with the user's text-scale setting, and it read the *large* viewport on
	   mobile, so the bottom row could sit under a collapsing browser toolbar.

	   The horizontal safe-area insets sit here rather than on an inner element so
	   the header, sidebar and main area all clear a landscape notch together. */
  .shell {
    display: flex;
    flex-direction: column;
    /* Sized by flex rather than height:100% — the parent is always the column
		   flex container `.app-content.app-content-fill`, and growing into it needs
		   no percentage to resolve against a flex-derived height. */
    flex: 1;
    min-height: 0;
    padding-left: var(--safe-left);
    padding-right: var(--safe-right);
    overflow: hidden;
  }

  .shell-header {
    flex-shrink: 0;
  }

  .shell-body {
    display: flex;
    flex: 1;
    min-height: 0;
    position: relative;
  }

  .shell-main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    overflow-y: auto;
  }

  /* Keeps the last row of a scrolling list off the home indicator. Chat opts out
	   (insetBottom={false}) because its composer holds the inset instead. */
  .shell-main.inset-bottom {
    padding-bottom: var(--safe-bottom);
  }
</style>
