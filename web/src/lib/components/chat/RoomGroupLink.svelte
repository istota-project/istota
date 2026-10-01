<script lang="ts">
  import { getRoomGroup, putRoomGroup, type RoomGroupLink } from '$lib/api';
  import { Select, type SelectOption } from '$lib/components/ui';

  interface Props {
    roomId: number;
  }

  let { roomId }: Props = $props();

  let link = $state<RoomGroupLink | null>(null);
  let error = $state('');
  let saving = $state(false);

  $effect(() => {
    const forRoom = roomId;
    link = null;
    error = '';
    getRoomGroup(forRoom)
      .then((l) => {
        if (forRoom === roomId) link = l;
      })
      .catch(() => {
        if (forRoom === roomId) error = 'Couldn’t load this room’s group.';
      });
  });

  // The current link stays in the list even when it is not one of the
  // caller's groups, so the select shows what the room holds.
  const options = $derived.by<SelectOption[]>(() => {
    if (!link) return [];
    const opts: SelectOption[] = [{ value: '', label: 'No group' }];
    for (const g of link.choices) opts.push({ value: g.group_id, label: g.display_name });
    if (link.group_id && !link.choices.some((g) => g.group_id === link?.group_id)) {
      opts.push({ value: link.group_id, label: link.group_name ?? link.group_id });
    }
    return opts;
  });

  // Saved on change, like the grants: the link is its own write, not part of
  // the settings PATCH.
  async function choose(value: string) {
    if (!link || saving || value === (link.group_id ?? '')) return;
    const forRoom = roomId;
    saving = true;
    error = '';
    try {
      const saved = await putRoomGroup(forRoom, value || null);
      if (forRoom === roomId) link = saved;
    } catch (e) {
      if (forRoom === roomId) error = e instanceof Error ? e.message : 'Couldn’t save that.';
    } finally {
      saving = false;
    }
  }
</script>

<div class="group-link">
  {#if link}
    <Select
      value={link.group_id ?? ''}
      {options}
      onValueChange={choose}
      ariaLabel="Group this room is linked to"
      disabled={!link.can_set || saving}
      fullWidth
    />
    <p class="caption">
      {#if link.group_id}
        Members' turns here carry this group's memory, while everyone in the room is in the group
        and no guest is present.
      {:else}
        Link a group so members' turns here carry its memory.
      {/if}
      {#if !link.can_set && link.refusal}
        {link.refusal}
      {/if}
    </p>
  {:else if !error}
    <p class="caption">Loading…</p>
  {/if}
  {#if error}<p class="form-error">{error}</p>{/if}
</div>

<style>
  p {
    margin: var(--space-1) 0 0;
  }
</style>
