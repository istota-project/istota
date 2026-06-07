<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { Plus, MessageSquare } from 'lucide-svelte';
	import AppShell from '$lib/components/ui/AppShell.svelte';
	import ShellHeader from '$lib/components/ui/ShellHeader.svelte';
	import Sidebar from '$lib/components/ui/Sidebar.svelte';
	import SidebarToggle from '$lib/components/ui/SidebarToggle.svelte';
	import Message from '$lib/components/chat/Message.svelte';
	import Composer from '$lib/components/chat/Composer.svelte';
	import { getChatSession } from '$lib/stores/chat';

	const session = getChatSession();
	const { rooms, activeRoomId, messages, status, loaded } = session;

	let sidebarOpen = $state(false);
	let creatingRoom = $state(false);
	let newRoomName = $state('');
	let listEl: HTMLDivElement | undefined = $state();

	const activeRoom = $derived($rooms.find((r) => r.id === $activeRoomId) ?? null);
	const busy = $derived($status === 'sending' || $status === 'streaming');

	onMount(() => {
		session.init();
	});

	// Auto-scroll to the newest message whenever the list changes.
	$effect(() => {
		$messages;
		tick().then(() => {
			if (listEl) listEl.scrollTop = listEl.scrollHeight;
		});
	});

	function selectRoom(id: number) {
		session.selectRoom(id);
		sidebarOpen = false;
	}

	async function createRoom() {
		const name = newRoomName.trim();
		if (!name) return;
		newRoomName = '';
		creatingRoom = false;
		await session.newRoom(name);
		sidebarOpen = false;
	}
</script>

<AppShell>
	{#snippet header()}
		<ShellHeader title={activeRoom ? activeRoom.name : 'Chat'}>
			{#snippet tools()}
				<SidebarToggle
					open={sidebarOpen}
					label="Rooms"
					count={$rooms.length}
					onclick={() => (sidebarOpen = !sidebarOpen)}
				/>
			{/snippet}
		</ShellHeader>
	{/snippet}

	{#snippet sidebar()}
		<Sidebar
			title="Rooms"
			count={$rooms.length}
			open={sidebarOpen}
			onClose={() => (sidebarOpen = false)}
		>
			{#snippet extras()}
				<div class="room-new">
					{#if creatingRoom}
						<!-- svelte-ignore a11y_autofocus -->
						<input
							class="room-input"
							bind:value={newRoomName}
							placeholder="Room name…"
							autofocus
							onkeydown={(e) => {
								if (e.key === 'Enter') createRoom();
								if (e.key === 'Escape') { creatingRoom = false; newRoomName = ''; }
							}}
							onblur={() => { if (!newRoomName.trim()) creatingRoom = false; }}
						/>
					{:else}
						<button class="room-add" onclick={() => (creatingRoom = true)} type="button">
							<Plus size={14} /> New room
						</button>
					{/if}
				</div>
			{/snippet}

			{#each $rooms as room (room.id)}
				<button
					class="room-btn"
					class:active={room.id === $activeRoomId}
					onclick={() => selectRoom(room.id)}
					type="button"
				>
					<MessageSquare size={13} />
					<span class="room-name">{room.name}</span>
				</button>
			{/each}
		</Sidebar>
	{/snippet}

	<div class="chat-pane">
		<div class="messages" bind:this={listEl} role="log" aria-live="polite">
			{#if !$loaded}
				<div class="chat-empty">Loading…</div>
			{:else if $messages.length === 0}
				<div class="chat-empty">
					<MessageSquare size={28} />
					<p>Ask {activeRoom ? `in #${activeRoom.name}` : 'Istota'} anything.</p>
					<span class="hint">Configuration help, quick tasks, or one-off questions.</span>
				</div>
			{:else}
				{#each $messages as message (message.cid)}
					<Message {message} onConfirm={session.confirm} onReject={session.reject} />
				{/each}
			{/if}
		</div>
		<Composer
			onSend={(t) => session.send(t)}
			onCancel={() => session.cancel()}
			busy={busy}
			placeholder={activeRoom ? `Message #${activeRoom.name}…` : 'Message Istota…'}
		/>
	</div>
</AppShell>

<style>
	.chat-pane {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	.messages {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 0.75rem 1rem;
		max-width: 820px;
		width: 100%;
		margin: 0 auto;
	}
	.messages::-webkit-scrollbar { width: 4px; }
	.messages::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 2px; }

	.chat-empty {
		height: 100%;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 0.4rem;
		color: var(--text-dim);
		text-align: center;
	}
	.chat-empty p { margin: 0.2rem 0 0; color: var(--text-muted); font-size: var(--text-base); }
	.chat-empty .hint { font-size: var(--text-sm); }

	.room-new { padding: 0 0.25rem 0.4rem; }
	.room-add {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		width: 100%;
		background: none;
		border: 1px dashed var(--border-default);
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.35rem 0.6rem;
		border-radius: 0.35rem;
		cursor: pointer;
	}
	.room-add:hover { color: var(--text-primary); border-color: var(--text-dim); }
	.room-input {
		width: 100%;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.35rem 0.5rem;
		border-radius: 0.35rem;
		outline: none;
	}

	.room-btn {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		width: 100%;
		background: none;
		border: none;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-base);
		cursor: pointer;
		padding: 0.35rem 0.6rem;
		border-radius: 0.3rem;
		text-align: left;
		transition: background var(--transition-fast), color var(--transition-fast);
	}
	.room-btn:hover { background: var(--surface-raised); color: var(--text-secondary); }
	.room-btn.active { background: var(--surface-raised); color: var(--text-primary); }
	.room-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
