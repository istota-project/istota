<script lang="ts" module>
  import type { CapabilityRow } from '$lib/apps/api';

  /** A capability as a screen shows it; the approval screen adds the two marks. */
  export interface CapabilityListRow extends CapabilityRow {
    new?: boolean;
    always_granted?: boolean;
  }
</script>

<script lang="ts">
  /* What an app may do, one line per capability: the server's plain-language
     description (`apps.capabilities.describe`), the capability as the manifest
     names it, and the marks the approval screen needs. The wording and the
     sensitivity are the server's; nothing here re-derives them. Shared by the
     approval screen, the first-open screen and Settings → Apps. */
  import type { Snippet } from 'svelte';
  import { Alert, Badge } from '@minot/ui';

  interface Props {
    capabilities: CapabilityListRow[];
    /** Under a row, for a control that belongs to it (the first-open folder picker). */
    extra?: Snippet<[CapabilityListRow]>;
    label?: string;
  }

  let { capabilities, extra, label = 'Permissions' }: Props = $props();

  const sensitive = $derived(capabilities.some((row) => row.sensitive));
</script>

{#if sensitive}
  <Alert variant="warn" title="This app can read your health records">
    Health records are among the most personal data you keep. Allow this only for an app you trust
    with them.
  </Alert>
{/if}
<ul class="list-reset divided-list" aria-label={label}>
  {#each capabilities as row (row.capability)}
    <li class="vstack" style:--vstack-gap="var(--space-1)">
      <div class="hstack wrap capability-line">
        <span>{row.description}</span>
        {#if row.new}
          <Badge size="sm" variant="info" uppercase={false}>New</Badge>
        {/if}
        {#if row.always_granted}
          <Badge size="sm" tone="subtle" uppercase={false}>Always allowed</Badge>
        {/if}
        {#if row.sensitive}
          <Badge size="sm" variant="warn" uppercase={false}>Health</Badge>
        {/if}
      </div>
      <code class="caption">{row.capability}</code>
      {#if extra}{@render extra(row)}{/if}
    </li>
  {/each}
</ul>

<style>
  .capability-line {
    align-items: baseline;
  }
</style>
