<!--
  A page's own heading row inside its pane: an optional back link above the
  title, the title (`.title-page`) with an optional badge beside it, a
  description under it, and actions at the far end. Below 768px the actions
  take a line of their own rather than wrapping raggedly against the title.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import BackLink from './BackLink.svelte';

  interface Props {
    title?: string;
    back?: { href: string; label: string };
    description?: string;
    /** `start` tops the actions against a back link and title; `center` (default) centres them. */
    align?: 'center' | 'start';
    badge?: Snippet;
    actions?: Snippet;
    /** Lines under the title that need markup (a marker's category and unit). */
    children?: Snippet;
  }

  let { title, back, description, align = 'center', badge, actions, children }: Props = $props();
</script>

<header class="ui-page-header" class:align-start={align === 'start'}>
  <div class="ui-page-header-main">
    {#if back}<BackLink href={back.href} label={back.label} />{/if}
    {#if title || badge}
      <div class="ui-page-header-title">
        {#if title}<h1 class="title-page">{title}</h1>{/if}
        {@render badge?.()}
      </div>
    {/if}
    {#if description}<p class="description">{description}</p>{/if}
    {@render children?.()}
  </div>
  {#if actions}
    <div class="ui-page-header-actions hstack wrap">{@render actions()}</div>
  {/if}
</header>

<style>
  .ui-page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-3) var(--space-4);
    /* A frame that already spaces its children (the settings column's gap)
       sets this to 0. */
    margin-bottom: var(--page-header-gap, var(--space-4));
  }

  .ui-page-header.align-start {
    align-items: flex-start;
  }

  .ui-page-header-main {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }

  .ui-page-header-title {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .ui-page-header .description {
    margin: 0;
  }

  @media (max-width: 768px) {
    .ui-page-header {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
