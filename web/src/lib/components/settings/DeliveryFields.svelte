<script lang="ts">
  import { channelLabel } from '$lib/components/briefings/settings/briefingSettings';
  import {
    talkRoomOptions,
    webRoomOptions,
    type TalkRoom,
    type WebRoom,
  } from '$lib/deliveryDescriptor';
  import { Field, Select } from '@minot/ui';

  let {
    outputs,
    talkRooms,
    webRooms,
    output,
    room,
    onOutputChange,
    onRoomChange,
    outputError,
    roomError,
  }: {
    outputs: string[];
    talkRooms: TalkRoom[];
    /** Leave out where `web` names no room to pick. */
    webRooms?: WebRoom[];
    output: string;
    room: string;
    onOutputChange: (output: string) => void;
    onRoomChange: (room: string) => void;
    outputError?: string;
    roomError?: string;
  } = $props();

  // A stored output the list lacks stays selected, or the control would show blank.
  const outputOptions = $derived([
    ...outputs.map((value) => ({ value, label: channelLabel(value) })),
    ...(outputs.includes(output) ? [] : [{ value: output, label: output || 'Default' }]),
  ]);
</script>

<div class="delivery-fields">
  <Field label="Delivery" error={outputError}>
    <Select
      value={output}
      options={outputOptions}
      onValueChange={onOutputChange}
      ariaLabel="Delivery"
      fullWidth
    />
  </Field>
  {#if output === 'talk'}
    <Field label="Talk room" error={roomError}>
      <Select
        value={room}
        options={talkRoomOptions(talkRooms, room, 'Choose a room')}
        onValueChange={onRoomChange}
        ariaLabel="Talk room"
        fullWidth
      />
    </Field>
  {:else if output === 'web' && webRooms}
    <Field label="Room" error={roomError}>
      <Select
        value={room}
        options={webRoomOptions(webRooms, room)}
        onValueChange={onRoomChange}
        ariaLabel="Room"
        fullWidth
      />
    </Field>
  {/if}
</div>

<style>
  .delivery-fields {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(14rem, 100%), 1fr));
    gap: var(--space-3);
    align-items: start;
  }
</style>
