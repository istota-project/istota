<script lang="ts">
  import { untrack } from 'svelte';
  import type { ChatRoom } from '$lib/api';
  import { Modal, Button, ConfirmDialog, Select, type SelectOption } from '$lib/components/ui';
  import { getBaseModelChoices } from '$lib/components/chat/autocomplete/providers';

  interface Props {
    open?: boolean;
    room: ChatRoom;
    onSave: (patch: { name?: string; model?: string | null; effort?: string | null }) => void;
    onDelete: () => void;
    onPromote?: () => void;
    onClose: () => void;
  }

  let { open = $bindable(true), room, onSave, onDelete, onPromote, onClose }: Props = $props();

  // Model + effort defaults for this room (canonical values, shared Talk+web).
  // "" is the "instance default" sentinel (cleared on the backend as null).
  const EFFORT_OPTIONS: SelectOption[] = [
    { value: '', label: 'Default effort' },
    { value: 'low', label: 'low' },
    { value: 'medium', label: 'medium' },
    { value: 'high', label: 'high' },
    { value: 'xhigh', label: 'xhigh' },
    { value: 'max', label: 'max' },
  ];
  let modelOptions = $state<SelectOption[]>([{ value: '', label: 'Default model' }]);
  let modelValue = $state(untrack(() => room.model ?? ''));
  let effortValue = $state(untrack(() => room.effort ?? ''));

  // Base model choices (dedup + provider-alias-preferred labels) shared with
  // the room header badge, so the dropdown and the badge name a model the same.
  $effect(() => {
    getBaseModelChoices().then((choices) => {
      // Show the canonical model id in parens next to the alias, so the pick
      // is unambiguous (e.g. `opus (claude-opus-4-8)`).
      modelOptions = [
        { value: '', label: 'Default model' },
        ...choices.map((c) => ({ value: c.value, label: `${c.label} (${c.value})` })),
      ];
    });
  });

  // A room is on Talk when it originated there or has been promoted.
  const onTalk = $derived(room.origin === 'talk' || !!room.talk_token);
  // A *promoted* room keeps the control, relabelled (ISSUE-401). Its binding
  // can go stale — the Talk conversation deleted out from under it — and the
  // button is the only way back; hiding it once `talk_token` was set is what
  // made that state permanent from the app. The server decides whether
  // anything actually happens: it probes the bound conversation and refuses
  // unless Nextcloud says it is gone, so pressing this on a healthy room is
  // answered with "already connected" rather than a second Talk room.
  // Both keyed off one predicate: `origin` is optional in the type, and an
  // origin-less room with a talk_token otherwise passed canPromote while
  // failing isPromoted, mislabelling the button.
  const canPromote = $derived(room.origin !== 'talk');
  const isPromoted = $derived(canPromote && !!room.talk_token);
  // An imported (Talk-origin) room is hidden per-user, not destroyed — this
  // must match the backend's hide condition (`reg.origin == 'talk'`), NOT
  // `onTalk`: a promoted web room (origin='web' + talk_token) is still hard-
  // deleted, so it must read as a delete, not a hide.
  const isImported = $derived(room.origin === 'talk');
  let promoting = $state(false);
  async function handlePromote() {
    if (!onPromote || promoting) return;
    promoting = true;
    try {
      await onPromote();
    } finally {
      promoting = false;
    }
  }

  // Local edit state. Re-seeded whenever the modal is opened for a different
  // room so reusing one component instance across rooms never leaks state.
  let name = $state(untrack(() => room.name));
  let showDeleteConfirm = $state(false);
  let copied = $state(false);
  let copyError = $state('');
  let lastRoomId = $state(untrack(() => room.id));

  $effect(() => {
    if (room.id !== lastRoomId) {
      lastRoomId = room.id;
      name = room.name;
      modelValue = room.model ?? '';
      effortValue = room.effort ?? '';
      showDeleteConfirm = false;
      copied = false;
      copyError = '';
    }
  });

  const trimmed = $derived(name.trim());
  const nameChanged = $derived(trimmed.length > 0 && trimmed !== room.name);
  const modelChanged = $derived(modelValue !== (room.model ?? ''));
  const effortChanged = $derived(effortValue !== (room.effort ?? ''));
  // Saveable when anything changed, and the name is never blanked.
  const canSave = $derived(trimmed.length > 0 && (nameChanged || modelChanged || effortChanged));

  let copyTimer: ReturnType<typeof setTimeout> | undefined;
  async function copyToken() {
    copyError = '';
    try {
      await navigator.clipboard.writeText(room.token);
      copied = true;
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => (copied = false), 1500);
    } catch {
      copyError = 'Copy failed — select and copy manually.';
    }
  }

  function handleSave() {
    if (!canSave) return;
    // Send only what changed. A name-only rename must not re-send a model
    // the backend might now reject (e.g. one retired from the alias table),
    // which would 400 the whole PATCH; the backend leaves absent fields
    // untouched.
    const patch: { name?: string; model?: string | null; effort?: string | null } = {};
    if (nameChanged) patch.name = trimmed;
    if (modelChanged) patch.model = modelValue || null;
    if (effortChanged) patch.effort = effortValue || null;
    onSave(patch);
  }

  function handleOpenChange(next: boolean) {
    if (!next) onClose();
  }
</script>

<Modal bind:open title="Room settings" onOpenChange={handleOpenChange} width="380px">
  <label class="field">
    <span>Name</span>
    <input
      type="text"
      bind:value={name}
      maxlength="80"
      placeholder="Room name"
      onkeydown={(e) => {
        if (e.key === 'Enter') handleSave();
      }}
    />
  </label>

  <div class="field">
    <span>Model</span>
    <Select
      value={modelValue}
      options={modelOptions}
      onValueChange={(v) => (modelValue = v)}
      ariaLabel="Room model default"
      fullWidth
    />
    <p class="caption">
      Applies to every message in this room, on both web and Nextcloud Talk. A
      <code>!model</code> prefix still overrides it for a single message.
    </p>
  </div>

  <div class="field">
    <span>Effort</span>
    <Select
      value={effortValue}
      options={EFFORT_OPTIONS}
      onValueChange={(v) => (effortValue = v)}
      ariaLabel="Room effort default"
      fullWidth
    />
  </div>

  <div class="field">
    <span>Room token</span>
    <div class="token-row">
      <input class="token" type="text" readonly value={room.token} />
      <button class="copy-btn" type="button" onclick={copyToken}>
        {copied ? 'Copied!' : 'Copy'}
      </button>
    </div>
    <p class="caption">Use this to link to or route output to this room.</p>
    {#if copyError}<p class="copy-error">{copyError}</p>{/if}
  </div>

  <div class="field">
    <span>Nextcloud Talk</span>
    {#if onTalk}
      <p class="caption talk-on">
        This room is also open in Nextcloud Talk — replies sync to your phone.
      </p>
    {/if}
    {#if canPromote && onPromote}
      <button class="talk-btn" type="button" disabled={promoting} onclick={handlePromote}>
        {#if promoting}
          {isPromoted ? 'Checking…' : 'Opening…'}
        {:else}
          {isPromoted ? 'Reconnect to Talk' : 'Also open in Talk'}
        {/if}
      </button>
      <p class="caption">
        {#if isPromoted}
          If the Talk conversation for this room was deleted, this creates a new one and points the
          room at it. Nothing changes while the existing conversation is still there.
        {:else}
          Creates a Nextcloud Talk conversation so this chat is reachable from the Talk apps.
        {/if}
      </p>
    {/if}
  </div>

  {#if isImported}
    <p class="caption hide-hint">
      Hiding only removes this room from your web chat list. The Nextcloud Talk conversation and its
      messages aren't deleted, and it reappears here if you post in it again.
    </p>
  {/if}

  {#snippet footer()}
    {#if isImported}
      <!-- A hide is reversible (re-engagement un-hides), so it's a one-click
			     action with no type-the-name confirm — unlike a real delete. -->
      <button class="delete-link" type="button" onclick={onDelete}> Hide </button>
    {:else}
      <button class="delete-link" type="button" onclick={() => (showDeleteConfirm = true)}>
        Delete
      </button>
    {/if}
    <Button variant="ghost" onclick={onClose}>Cancel</Button>
    <Button variant="primary" onclick={handleSave} disabled={!canSave}>Save</Button>
  {/snippet}
</Modal>

<ConfirmDialog
  bind:open={showDeleteConfirm}
  title="Delete room"
  message={`Permanently deletes "${room.name}" and all its messages. This cannot be undone.`}
  challenge={room.name}
  confirmLabel="Delete this room"
  onConfirm={onDelete}
/>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin-bottom: var(--space-3);
  }

  .field > span {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .field input[type='text'] {
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-sm);
  }

  .token-row {
    display: flex;
    gap: var(--space-2);
    align-items: stretch;
  }

  .token-row .token {
    flex: 1;
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .copy-btn {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    padding: 0 var(--space-2);
    border-radius: var(--radius-sm);
    cursor: pointer;
    white-space: nowrap;
    transition:
      color var(--transition-fast),
      background var(--transition-fast);
  }
  .copy-btn:hover {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  /* Typography is the global .caption; only the <p> reset stays here. */
  p.caption {
    margin: 0.1rem 0 0;
  }

  .talk-on {
    color: var(--text-muted);
  }

  .hide-hint {
    margin: 0 0 var(--space-2);
  }

  .talk-btn {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition:
      background var(--transition-fast),
      color var(--transition-fast);
  }
  .talk-btn:hover:not(:disabled) {
    background: var(--surface-raised);
  }
  .talk-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .copy-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    margin: 0.1rem 0 0;
  }

  .delete-link {
    margin-right: auto;
    background: none;
    border: none;
    color: var(--text-dim);
    font: inherit;
    font-size: var(--text-sm);
    cursor: pointer;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-pill);
    transition: color var(--transition-fast);
  }
  .delete-link:hover {
    color: var(--status-danger-fg);
  }
</style>
