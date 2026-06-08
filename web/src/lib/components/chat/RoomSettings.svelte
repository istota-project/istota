<script lang="ts">
	import { untrack } from 'svelte';
	import type { ChatRoom } from '$lib/api';
	import { Modal, Button } from '$lib/components/ui';

	interface Props {
		open?: boolean;
		room: ChatRoom;
		onSave: (name: string) => void;
		onDelete: () => void;
		onClose: () => void;
	}

	let { open = $bindable(true), room, onSave, onDelete, onClose }: Props = $props();

	// Local edit state. Re-seeded whenever the modal is opened for a different
	// room so reusing one component instance across rooms never leaks state.
	let name = $state(untrack(() => room.name));
	let confirmText = $state('');
	let showDanger = $state(false);
	let copied = $state(false);
	let copyError = $state('');
	let lastRoomId = $state(untrack(() => room.id));

	$effect(() => {
		if (room.id !== lastRoomId) {
			lastRoomId = room.id;
			name = room.name;
			confirmText = '';
			showDanger = false;
			copied = false;
			copyError = '';
		}
	});

	const trimmed = $derived(name.trim());
	const canSave = $derived(trimmed.length > 0 && trimmed !== room.name);
	// Exact, case-sensitive match against the *saved* name (what the sidebar
	// still shows), not any unsaved edit in the Name field above.
	const canDelete = $derived(confirmText === room.name);

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
		onSave(trimmed);
	}

	function handleDelete() {
		if (!canDelete) return;
		onDelete();
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
			onkeydown={(e) => { if (e.key === 'Enter') handleSave(); }}
		/>
	</label>

	<div class="field">
		<span>Room token</span>
		<div class="token-row">
			<input class="token" type="text" readonly value={room.token} />
			<button class="copy-btn" type="button" onclick={copyToken}>
				{copied ? 'Copied!' : 'Copy'}
			</button>
		</div>
		<p class="hint">Use this to link to or route output to this room.</p>
		{#if copyError}<p class="copy-error">{copyError}</p>{/if}
	</div>

	{#if showDanger}
		<div class="danger-zone">
			<p class="danger-warn">
				This permanently deletes this room and all its messages. This cannot be undone.
			</p>
			<p class="danger-prompt">Type <code>{room.name}</code> to confirm.</p>
			<input type="text" bind:value={confirmText} placeholder={room.name} />
			<button class="danger-btn" type="button" disabled={!canDelete} onclick={handleDelete}>
				Delete this room
			</button>
		</div>
	{/if}

	{#snippet footer()}
		{#if !showDanger}
			<button class="delete-link" type="button" onclick={() => (showDanger = true)}>
				Delete
			</button>
		{:else}
			<button class="delete-link" type="button" onclick={() => { showDanger = false; confirmText = ''; }}>
				Keep room
			</button>
		{/if}
		<Button variant="ghost" onclick={onClose}>Cancel</Button>
		<Button variant="primary" onclick={handleSave} disabled={!canSave}>Save</Button>
	{/snippet}
</Modal>

<style>
	.field {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		margin-bottom: 0.85rem;
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
		padding: 0.35rem 0.5rem;
		border-radius: 0.25rem;
	}

	.token-row {
		display: flex;
		gap: 0.4rem;
		align-items: stretch;
	}

	.token-row .token {
		flex: 1;
		font-family: var(--font-mono, monospace);
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.copy-btn {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-xs);
		padding: 0 0.6rem;
		border-radius: 0.25rem;
		cursor: pointer;
		white-space: nowrap;
		transition: color var(--transition-fast), background var(--transition-fast);
	}
	.copy-btn:hover {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.hint {
		font-size: var(--text-xs);
		color: var(--text-dim);
		margin: 0.1rem 0 0;
	}

	.copy-error {
		font-size: var(--text-xs);
		color: #c66;
		margin: 0.1rem 0 0;
	}

	.danger-zone {
		border: 1px solid #6b2b2b;
		border-radius: 0.4rem;
		padding: 0.6rem 0.7rem;
		margin-bottom: 0.5rem;
		background: rgba(120, 40, 40, 0.08);
	}

	.danger-warn {
		font-size: var(--text-xs);
		color: var(--text-secondary);
		margin: 0 0 0.5rem;
	}

	.danger-prompt {
		font-size: var(--text-xs);
		color: var(--text-muted);
		margin: 0 0 0.35rem;
	}

	.danger-prompt code {
		font-family: var(--font-mono, monospace);
		color: var(--text-primary);
		background: var(--surface-base);
		padding: 0.05rem 0.3rem;
		border-radius: 0.2rem;
	}

	.danger-zone input[type='text'] {
		width: 100%;
		background: var(--surface-base);
		border: 1px solid var(--border-default);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.35rem 0.5rem;
		border-radius: 0.25rem;
		margin-bottom: 0.5rem;
	}

	.danger-btn {
		width: 100%;
		background: #8a3030;
		border: none;
		color: #fff;
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.4rem 0.6rem;
		border-radius: 0.25rem;
		cursor: pointer;
		transition: background var(--transition-fast);
	}
	.danger-btn:hover:not(:disabled) {
		background: #a23a3a;
	}
	.danger-btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.delete-link {
		margin-right: auto;
		background: none;
		border: none;
		color: var(--text-dim);
		font: inherit;
		font-size: var(--text-sm);
		cursor: pointer;
		padding: 0.25rem 0.4rem;
		border-radius: var(--radius-pill);
		transition: color var(--transition-fast);
	}
	.delete-link:hover {
		color: #c66;
	}
</style>
