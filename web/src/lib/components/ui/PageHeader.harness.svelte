<script lang="ts">
  import PageHeader from './PageHeader.svelte';

  let {
    title,
    back,
    description,
    align,
    withBadge = false,
    withActions = false,
  }: {
    title?: string;
    back?: { href: string; label: string };
    description?: string;
    align?: 'center' | 'start';
    withBadge?: boolean;
    withActions?: boolean;
  } = $props();
</script>

{#snippet tag()}<span data-testid="badge">Draft</span>{/snippet}
{#snippet tools()}<button type="button">Add</button>{/snippet}

<PageHeader
  {title}
  {back}
  {description}
  {align}
  badge={withBadge ? tag : undefined}
  actions={withActions ? tools : undefined}
/>
