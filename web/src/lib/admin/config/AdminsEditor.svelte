<script lang="ts">
  import { Badge, Button, Field, IconButton, Input } from '$lib/components/ui';

  interface Props {
    /** The allowlist the running process loaded. */
    users: string[];
    /** The pending new list, or null for unchanged. */
    pending: string[] | null;
    /** The signed-in admin, who cannot remove themselves. */
    self: string;
    editable: boolean;
    readOnlyReason?: string | null;
    onChange: (admins: string[] | null) => void;
  }

  let { users, pending, self, editable, readOnlyReason = null, onChange }: Props = $props();

  let adding = $state('');
  let addError = $state('');

  let shown = $derived(pending ?? users);
  let canEdit = $derived(editable && !readOnlyReason);

  function sameSet(a: string[], b: string[]): boolean {
    return a.length === b.length && [...a].sort().join('\n') === [...b].sort().join('\n');
  }

  function update(next: string[]) {
    onChange(sameSet(next, users) ? null : next);
  }

  function add() {
    const id = adding.trim();
    if (!id) return;
    if (!/^[A-Za-z0-9._@-]{1,128}$/.test(id)) {
      addError = 'A user id is letters, digits and . _ @ - only.';
      return;
    }
    if (shown.includes(id)) {
      addError = `${id} is already an admin.`;
      return;
    }
    addError = '';
    adding = '';
    update([...shown, id]);
  }
</script>

<!--
  The admin allowlist (`config/admins`). The server makes the checks that
  matter — every id needs a live sign-in identity, the list cannot be empty
  and must keep the acting admin — and this only stops the one mistake that
  is obvious from here: removing yourself.
-->
<div class="admins-editor">
  <ul class="admin-list">
    {#each shown as id (id)}
      <li>
        <span class="admin-id">{id}</span>
        {#if id === self}<Badge size="sm">you</Badge>{/if}
        {#if pending && !users.includes(id)}<Badge size="sm" variant="info">added</Badge>{/if}
        {#if canEdit && id !== self}
          <IconButton
            label="Remove {id}"
            size="sm"
            danger
            onclick={() => update(shown.filter((x) => x !== id))}>×</IconButton
          >
        {/if}
      </li>
    {/each}
  </ul>
  {#if pending}
    {@const removed = users.filter((id) => !pending.includes(id))}
    {#if removed.length}
      <p class="caption">Removed: {removed.join(', ')}</p>
    {/if}
  {/if}

  {#if canEdit}
    <div class="add-row control-row">
      <Field label="Add an admin" error={addError}>
        <Input
          bind:value={adding}
          monospace
          placeholder="user id"
          onkeydown={(e: KeyboardEvent) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              add();
            }
          }}
        />
      </Field>
      <Button variant="secondary" onclick={add} disabled={!adding.trim()}>Add</Button>
      {#if pending}
        <Button variant="ghost" onclick={() => onChange(null)}>Undo</Button>
      {/if}
    </div>
    <p class="caption">
      An admin needs a sign-in identity first; add one from the Users pane before adding them here.
    </p>
  {:else if editable && readOnlyReason}
    <p class="caption">{readOnlyReason}</p>
  {/if}
</div>

<style>
  .admin-list {
    list-style: none;
    margin: 0 0 var(--space-2);
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .admin-list li {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .admin-id {
    font-family: var(--font-mono);
    font-size: var(--text-sm);
  }

  .add-row {
    display: flex;
    align-items: flex-end;
    gap: var(--space-2);
  }
</style>
