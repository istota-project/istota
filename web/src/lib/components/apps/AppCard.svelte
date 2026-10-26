<script lang="ts">
  /* One installed app in "All apps": its icon, name and description, where it
     came from, and the way into it. A waiting install is badged here and
     reviewed from the "Waiting for review" list, so it has one Review button. */
  import { base } from '$app/paths';
  import { Badge, Button, Card, Icon } from '@minot/ui';
  import { appPath, type AppSummary } from '$lib/apps/api';
  import { fitsRange } from '$lib/apps/display';
  import { appIcon } from '$lib/apps/icons';

  /** `paneWidth` is the section's measured pane, for the "Wider screen" badge. */
  let { app, paneWidth = null }: { app: AppSummary; paneWidth?: number | null } = $props();

  const tooNarrow = $derived(!fitsRange(app.narrowest, paneWidth));

  const asks = $derived(app.ungranted.length);
</script>

<Card title={app.name} level={3} description={app.description || undefined}>
  {#snippet icon()}<Icon icon={appIcon(app.icon)} size="md" />{/snippet}
  {#snippet status()}
    {#if app.origin === 'starter'}
      <Badge size="sm" tone="subtle" uppercase={false}>Shipped with Istota</Badge>
    {/if}
    {#if app.pending_id}
      <Badge size="sm" variant="info" uppercase={false}>Update waiting</Badge>
    {/if}
    {#if tooNarrow}
      <Badge size="sm" tone="subtle" uppercase={false}>Wider screen</Badge>
    {/if}
  {/snippet}
  {#snippet actions()}
    <Button
      size="sm"
      variant="secondary"
      href="{base}{appPath(app.app_id)}"
      ariaLabel="Open {app.name}">Open</Button
    >
  {/snippet}
  {#if asks}
    <p class="caption">
      Asks for {asks === 1 ? 'one more permission' : `${asks} more permissions`} when you open it.
    </p>
  {/if}
</Card>
