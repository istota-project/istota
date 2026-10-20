<script lang="ts">
  import { Button, Field, MultiSelect } from '$lib/components/ui';

  interface Props {
    /** The allowlist the running process loaded. */
    users: string[];
    /** Who may be picked: every user with a live sign-in identity. */
    candidates: string[];
    /** The pending new list, or null for unchanged. */
    pending: string[] | null;
    /** The signed-in admin, who cannot remove themselves. */
    self: string;
    editable: boolean;
    readOnlyReason?: string | null;
    onChange: (admins: string[] | null) => void;
  }

  let {
    users,
    candidates,
    pending,
    self,
    editable,
    readOnlyReason = null,
    onChange,
  }: Props = $props();

  let shown = $derived(pending ?? users);
  let canEdit = $derived(editable && !readOnlyReason);

  // A current admin with no identity is still listed, so the list says what
  // the file holds; the server refuses to keep them on submit.
  let options = $derived(
    [...new Set([...candidates, ...shown])].sort().map((id) => ({
      value: id,
      label: id === self ? `${id} (you)` : id,
      disabled: id === self,
    })),
  );

  function sameSet(a: string[], b: string[]): boolean {
    return a.length === b.length && [...a].sort().join('\n') === [...b].sort().join('\n');
  }

  function update(next: string[]) {
    const kept = next.includes(self) || !shown.includes(self) ? next : [self, ...next];
    onChange(sameSet(kept, users) ? null : kept);
  }
</script>

<!--
  The admin allowlist (`config/admins`), picked the way the Users pane picks
  disabled skills. The server makes the checks that matter — every id needs a
  live sign-in identity, the list cannot be empty and must keep the acting
  admin — and this only stops the one mistake that is obvious from here:
  removing yourself, whose pick is locked.
-->
<div class="admins-editor">
  <Field
    label="admins"
    layout="inline"
    hint="Each admin needs a sign-in identity; add one on the Users page first."
  >
    <MultiSelect
      value={shown}
      {options}
      onValueChange={update}
      disabled={!canEdit}
      placeholder="Search users to make admin…"
      emptyLabel="No admins."
      ariaLabel="Admins"
    />
  </Field>
  {#if pending}
    {@const removed = users.filter((id) => !pending.includes(id))}
    <div class="pending-row control-row">
      {#if removed.length}<p class="caption">Removed: {removed.join(', ')}</p>{/if}
      <Button variant="secondary" size="sm" onclick={() => onChange(null)}>Undo</Button>
    </div>
  {/if}
  {#if editable && readOnlyReason}
    <p class="caption">{readOnlyReason}</p>
  {/if}
</div>

<style>
  .pending-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }

  .pending-row .caption {
    margin: 0;
  }
</style>
