<script lang="ts">
  import type { RateProvenance } from '$lib/money/api';

  // Where a set of figures came from, and whether it can be trusted for the
  // year being computed. Three signals, and they are the substantive answer to
  // "keep the numbers current" — none of them needs a network call, and each is
  // correct even when the data is fine, which is what makes them trustworthy.

  interface Props {
    provenance: RateProvenance;
    /** The year the user asked for, which is not always the year in use. */
    taxYear: number;
  }

  let { provenance, taxYear }: Props = $props();

  let hasSource = $derived(!!provenance.source);
</script>

<div class="provenance">
  {#if provenance.is_fallback}
    <!-- The signal that would have caught the original bug: figures for one
         year silently standing in for another. -->
    <p class="warn">
      No {taxYear} figures are bundled. Showing {provenance.year} figures instead — the estimate will
      be wrong wherever the law changed.
    </p>
  {:else if provenance.is_stale}
    <p class="warn">
      These figures were last checked on {provenance.verified_on}, before the {taxYear} tax year began.
      They may predate the year's inflation adjustments.
    </p>
  {/if}

  {#if hasSource}
    <p class="source">
      {#if provenance.source_url}
        <a href={provenance.source_url} target="_blank" rel="noopener noreferrer">
          {provenance.source}
        </a>
      {:else}
        {provenance.source}
      {/if}
      {#if provenance.verified_on}
        <span class="checked">· checked {provenance.verified_on}</span>
      {/if}
    </p>
  {:else}
    <p class="source">No bundled figures — these are your own values.</p>
  {/if}
</div>

<style>
  .provenance {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin-bottom: var(--space-2);
  }

  .warn {
    margin: 0;
    font-size: var(--text-xs);
    color: var(--status-warn-fg);
  }

  .source {
    margin: 0;
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .source a {
    color: var(--link);
  }

  .checked {
    color: var(--text-dim);
  }
</style>
