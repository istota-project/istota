<script lang="ts">
  import { Alert } from '@minot/ui';
  import {
    TAX_DISCLAIMER_GAPS,
    TAX_DISCLAIMER_LEAD,
    TAX_DISCLAIMER_TITLE,
  } from '$lib/money/taxDisclaimer';

  // Persistent and not dismissible, following the health module's disclaimer.
  // `collapsed={false}` so the gaps are open on arrival: the list is the part
  // that does the work, and a reader who has to click to find out what is
  // missing will not click.
</script>

<Alert title={TAX_DISCLAIMER_TITLE} variant="info" collapsed={false}>
  <p class="lead">{TAX_DISCLAIMER_LEAD}</p>
  <p class="gaps-label">Not modelled:</p>
  <ul>
    {#each TAX_DISCLAIMER_GAPS as gap (gap)}
      <li>{gap}</li>
    {/each}
  </ul>
</Alert>

<style>
  /* Weight, colour and layout only — the type sizes belong to Alert. */
  .lead {
    margin: 0 0 var(--space-2);
  }

  .gaps-label {
    margin: 0 0 var(--space-1);
    font-weight: var(--weight-semibold);
  }

  ul {
    margin: 0;
    padding-left: var(--space-3);
    color: var(--text-muted);
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
</style>
