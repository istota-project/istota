<script lang="ts">
  import ListRow from './ListRow.svelte';

  let {
    label = 'General',
    href,
    onclick,
    active = false,
    density,
    tint,
    withIcon = false,
    withMark = false,
    withBadge = false,
    withMeta = false,
    withTrailing = false,
    ariaCurrent,
  }: {
    label?: string;
    href?: string;
    onclick?: (e: MouseEvent) => void;
    active?: boolean;
    density?: 'base' | 'sm';
    tint?: string;
    withIcon?: boolean;
    withMark?: boolean;
    withBadge?: boolean;
    withMeta?: boolean;
    withTrailing?: boolean;
    ariaCurrent?: 'page' | 'location';
  } = $props();
</script>

{#snippet icon()}<svg data-testid="icon"></svg>{/snippet}
{#snippet mark()}<span data-testid="mark">*</span>{/snippet}
{#snippet badge()}<span data-testid="badge">3</span>{/snippet}
{#snippet meta()}<span data-testid="meta">Second line</span>{/snippet}
{#snippet trailing()}<button type="button" data-testid="trailing">More</button>{/snippet}

<ListRow
  {href}
  {onclick}
  {active}
  {density}
  {tint}
  icon={withIcon ? icon : undefined}
  mark={withMark ? mark : undefined}
  badge={withBadge ? badge : undefined}
  meta={withMeta ? meta : undefined}
  trailing={withTrailing ? trailing : undefined}
  aria-current={ariaCurrent}
>
  {label}
</ListRow>
