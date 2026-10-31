<script lang="ts">
  import { Button, Field, Input, Modal } from '@minot/ui';
  import { isImeComposing } from '$lib/platform/input';

  interface Props {
    open: boolean;
    onCreate: (name: string) => Promise<void>;
  }

  let { open = $bindable(false), onCreate }: Props = $props();

  let name = $state('');
  let creating = $state(false);
  let error = $state('');

  const trimmed = $derived(name.trim());

  async function create() {
    if (!trimmed || creating) return;
    creating = true;
    error = '';
    try {
      await onCreate(trimmed);
      name = '';
      open = false;
    } catch {
      error = 'The room could not be created.';
    } finally {
      creating = false;
    }
  }
</script>

<Modal bind:open title="New incognito room">
  <div class="incognito-dialog">
    <Field label="Room name" {error}>
      <!-- svelte-ignore a11y_autofocus -->
      <Input
        bind:value={name}
        placeholder="Room name…"
        autofocus
        onkeydown={(e) => {
          if (e.key === 'Enter' && !isImeComposing(e)) create();
        }}
      />
    </Field>
    <ul class="caption">
      <li>None of your memory is loaded here, and nothing said here is added to it.</li>
      <li>
        This room's own memory is the only thing it keeps. Deleting the room removes that and the
        whole conversation.
      </li>
      <li>You are its only member, and it stays in web chat. It can't be opened in Talk.</li>
      <li>Your files, calendar and other tools still work here when you ask for them.</li>
    </ul>
    <p class="permanent"><strong>This is permanent.</strong> A room can't stop being incognito.</p>
  </div>

  {#snippet footer()}
    <Button variant="secondary" onclick={() => (open = false)}>Cancel</Button>
    <Button variant="primary" disabled={!trimmed || creating} onclick={create}>
      Create incognito room
    </Button>
  {/snippet}
</Modal>

<style>
  .incognito-dialog {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  ul {
    margin: 0;
    padding-inline-start: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  .permanent {
    margin: 0;
  }
</style>
