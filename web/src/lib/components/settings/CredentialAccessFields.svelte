<script lang="ts">
  import { Field, Select } from '$lib/components/ui';

  /**
   * Who may use a credential: the room scope, scheduled tasks and the HTTP
   * override. Shared by the access editor and the add form, so the two cannot
   * offer different choices.
   */
  interface Props {
    rooms: { token: string; name: string }[];
    scope: 'all' | 'rooms';
    selected: string[];
    scheduled: boolean;
    allowHttp: boolean;
    /** Some previously chosen rooms are no longer available to this user. */
    omittedRooms?: boolean;
    error?: string;
  }

  let {
    rooms,
    scope = $bindable('all'),
    selected = $bindable([]),
    scheduled = $bindable(false),
    allowHttp = $bindable(false),
    omittedRooms = false,
    error = '',
  }: Props = $props();
</script>

<div class="access-fields">
  <Field label="Room scope" labelled={false}>
    <Select
      bind:value={scope}
      ariaLabel="Room scope"
      fullWidth
      options={[
        { value: 'all', label: 'All rooms' },
        { value: 'rooms', label: 'Selected rooms' },
      ]}
    />
  </Field>
  {#if omittedRooms}<p class="caption">
      Unavailable rooms have been removed from this selection.
    </p>{/if}
  {#if scope === 'rooms'}
    <fieldset class="access-rooms">
      <legend>Rooms</legend>
      <div class="room-options">
        {#each rooms as room (room.token)}
          <Field label={room.name} checkbox>
            <input type="checkbox" bind:group={selected} value={room.token} />
          </Field>
        {/each}
      </div>
      {#if !rooms.length}<p class="caption">No rooms available, so no task can use it.</p>{/if}
    </fieldset>
  {/if}
  <Field label="Allow scheduled tasks" checkbox>
    <input type="checkbox" bind:checked={scheduled} />
  </Field>
  <Field label="Allow HTTP (override HTTPS requirement)" checkbox>
    <input type="checkbox" bind:checked={allowHttp} />
  </Field>
  <p class="caption">
    HTTP sends credentials without encryption. Turn it on only for a service you trust on a trusted
    network.
  </p>
  {#if error}<p class="form-error" data-testid="access-error">{error}</p>{/if}
</div>

<style>
  .access-fields {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .access-fields .caption,
  .access-fields .form-error {
    margin: 0;
  }

  .access-rooms {
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .access-rooms legend {
    padding: 0;
    margin-bottom: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-muted);
  }

  .room-options {
    display: grid;
    gap: var(--space-2);
    overflow-wrap: anywhere;
  }
</style>
