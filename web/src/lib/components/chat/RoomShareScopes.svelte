<script lang="ts">
  import { getRoomGrants, putRoomGrants, type GrantState, type RoomGrants } from '$lib/api';

  interface Props {
    roomId: number;
  }

  let { roomId }: Props = $props();

  let grants = $state<RoomGrants | null>(null);
  let error = $state('');
  let saving = $state(false);

  // What a grant does right now, and the three reasons it does nothing.
  const STATE_NOTES: Record<GrantState, string> = {
    active:
      'What you share here, answers to your messages may use — and everyone in the room reads those answers.',
    private: 'Only you read this room now. What you share applies once someone else joins.',
    guests_present:
      'A guest reads this room, so nothing you share is used here until no guest is present. Answers that need your data go to your side room.',
    policy_off: 'This deployment withholds nothing from shared rooms, so these have no effect.',
  };

  $effect(() => {
    const forRoom = roomId;
    grants = null;
    error = '';
    getRoomGrants(forRoom)
      .then((g) => {
        if (forRoom === roomId) grants = g;
      })
      .catch(() => {
        if (forRoom === roomId) error = 'Couldn’t load what you share here.';
      });
  });

  // Optimistic: the box flips at once, and goes back if the server refuses.
  async function toggle(name: string, on: boolean) {
    if (!grants || saving) return;
    const before = grants;
    const scopes = before.scopes.map((s) => (s.name === name ? { ...s, granted: on } : s));
    grants = { ...before, scopes };
    saving = true;
    error = '';
    try {
      grants = await putRoomGrants(
        roomId,
        scopes.filter((s) => s.granted).map((s) => s.name),
      );
    } catch (e) {
      grants = before;
      error = e instanceof Error ? e.message : 'Couldn’t save that.';
    } finally {
      saving = false;
    }
  }
</script>

<div class="scopes">
  {#if grants}
    <p class="caption">{STATE_NOTES[grants.state]}</p>
    <div class="grid">
      {#each grants.scopes as s (s.name)}
        <label class="scope">
          <input
            type="checkbox"
            checked={s.granted}
            disabled={saving}
            onchange={(e) => toggle(s.name, e.currentTarget.checked)}
          />
          <span>{s.name}</span>
        </label>
      {/each}
    </div>
  {:else if !error}
    <p class="caption">Loading…</p>
  {/if}
  {#if error}<p class="form-error">{error}</p>{/if}
</div>

<style>
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(7rem, 1fr));
    gap: var(--space-1) var(--space-2);
    margin-top: var(--space-2);
  }
  .scope {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--text-sm);
    color: var(--text-primary);
  }
  p {
    margin: var(--space-1) 0 0;
  }
</style>
