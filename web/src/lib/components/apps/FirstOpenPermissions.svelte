<script lang="ts">
  /* The first open of a starter app that asks for something not granted yet:
     the approval screen's capability section alone, marked "Shipped with
     Istota", with no diff, since the source is in-tree and was reviewed there.

     A `files:read` capability's folder may be changed before granting (the
     only scope that may differ from the manifest's); the server runs the same
     deny rule over the new folder and refuses it with a message shown under
     the field. "Not now" opens the app with what it has; the bridge answers the
     rest with `not_granted`, and the app asks again on the next open. */
  import { Badge, Button, Card, Field, Input } from '@minot/ui';
  import { notifyError } from '@minot/ui/stores/notices';
  import { AuthError } from '$lib/api';
  import { AppsApiError, grantCapability, type AppDetail, type CapabilityRow } from '$lib/apps/api';
  import CapabilityList from './CapabilityList.svelte';

  interface Props {
    detail: AppDetail;
    /** Everything asked for was granted. */
    onGranted: () => void;
    onNotNow: () => void;
    onExpired: () => void;
  }

  let { detail, onGranted, onNotNow, onExpired }: Props = $props();

  function picksFolder(row: CapabilityRow): boolean {
    return row.capability.startsWith('files:read:');
  }

  // Seeded once from the manifest's folders; the user edits from there.
  const initialFolders = (): Record<string, string> =>
    Object.fromEntries(
      detail.ungranted.filter(picksFolder).map((row) => [row.capability, row.scope ?? '']),
    );
  let folders = $state<Record<string, string>>(initialFolders());
  let fieldErrors = $state<Record<string, string>>({});
  let busy = $state(false);

  async function allow() {
    busy = true;
    fieldErrors = {};
    let failed = false;
    try {
      for (const row of detail.ungranted) {
        const scope = picksFolder(row) ? (folders[row.capability] ?? '').trim() : null;
        try {
          await grantCapability(detail.app_id, row.capability, scope);
        } catch (e) {
          if (e instanceof AuthError) {
            onExpired();
            return;
          }
          failed = true;
          const text = e instanceof Error && e.message ? e.message : 'Could not allow this.';
          if (picksFolder(row) && e instanceof AppsApiError && e.status === 422) {
            fieldErrors = { ...fieldErrors, [row.capability]: text };
          } else {
            notifyError(`Could not allow ${row.capability}: ${text}`);
          }
        }
      }
    } finally {
      busy = false;
    }
    if (!failed) onGranted();
  }
</script>

<div class="first-open content-frame">
  <Card
    title="{detail.name} asks for more"
    description="These are not allowed yet. Allow them, or open the app without them: it then gets nothing for what you did not allow, and asks again next time."
  >
    {#snippet status()}
      <Badge size="sm" tone="subtle" uppercase={false}>Shipped with Istota</Badge>
    {/snippet}
    <CapabilityList capabilities={detail.ungranted} label="Asked for">
      {#snippet extra(row)}
        {#if picksFolder(row)}
          <Field
            label="Folder"
            description="A folder in your files, such as notes or notes/aviation."
            error={fieldErrors[row.capability]}
          >
            <Input
              monospace
              bind:value={folders[row.capability]}
              invalid={!!fieldErrors[row.capability]}
              disabled={busy}
            />
          </Field>
        {/if}
      {/snippet}
    </CapabilityList>
    {#snippet footer()}
      <Button variant="secondary" disabled={busy} onclick={onNotNow}>Not now</Button>
      <Button variant="primary" loading={busy} loadingLabel="Allowing…" onclick={allow}
        >Allow</Button
      >
    {/snippet}
  </Card>
</div>

<style>
  .first-open {
    padding: var(--space-4);
  }
</style>
