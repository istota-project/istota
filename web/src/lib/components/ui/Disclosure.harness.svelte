<script lang="ts">
  import Disclosure from './Disclosure.svelte';

  let {
    summary = 'More options',
    open = $bindable(false),
    variant,
    storageKey,
    withTrailing = false,
    withSummarySnippet = false,
  }: {
    summary?: string;
    open?: boolean;
    variant?: 'inline' | 'card' | 'section';
    storageKey?: string;
    withTrailing?: boolean;
    withSummarySnippet?: boolean;
  } = $props();
</script>

{#snippet richSummary()}<strong data-testid="rich">Rich summary</strong>{/snippet}
{#snippet total()}<span data-testid="total">$1,200.00</span>{/snippet}

<Disclosure
  summary={withSummarySnippet ? richSummary : summary}
  bind:open
  {variant}
  {storageKey}
  trailing={withTrailing ? total : undefined}
>
  <p>Hidden body</p>
</Disclosure>
<output data-testid="open">{open ? 'open' : 'closed'}</output>
