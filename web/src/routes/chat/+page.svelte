<script lang="ts">
	import { onMount, onDestroy, tick } from 'svelte';
	import { page } from '$app/state';
	import { Plus, MessageSquare } from 'lucide-svelte';
	import AppShell from '$lib/components/ui/AppShell.svelte';
	import ShellHeader from '$lib/components/ui/ShellHeader.svelte';
	import Sidebar from '$lib/components/ui/Sidebar.svelte';
	import SidebarToggle from '$lib/components/ui/SidebarToggle.svelte';
	import KebabMenu from '$lib/components/ui/KebabMenu.svelte';
	import Message from '$lib/components/chat/Message.svelte';
	import Composer from '$lib/components/chat/Composer.svelte';
	import RoomSettings from '$lib/components/chat/RoomSettings.svelte';
	import { getChatSession } from '$lib/stores/chat';
	import { getMe, type ChatRoom } from '$lib/api';

	const session = getChatSession();
	const { rooms, activeRoomId, messages, status, loaded } = session;

	// The room whose settings modal is open (null = closed).
	let settingsRoom = $state<ChatRoom | null>(null);

	let sidebarOpen = $state(false);
	// Author labels for message headers; fall back to generic labels until /me
	// resolves (or if it fails).
	let userName = $state('You');
	let botName = $state('Istota');
	let creatingRoom = $state(false);
	let newRoomName = $state('');
	let listEl: HTMLDivElement | undefined = $state();

	const activeRoom = $derived($rooms.find((r) => r.id === $activeRoomId) ?? null);
	const busy = $derived($status === 'sending' || $status === 'streaming');

	// Discord/Slack-style grouping: a message continues the previous author's
	// run (collapsing its avatar + header) when it's the same non-system author
	// within a short window.
	const GROUP_WINDOW_MS = 5 * 60 * 1000;
	function isContinuation(i: number): boolean {
		if (i <= 0) return false;
		const prev = $messages[i - 1];
		const cur = $messages[i];
		if (!prev || prev.role !== cur.role || cur.role === 'system') return false;
		if (prev.createdAt && cur.createdAt) {
			const gap = new Date(cur.createdAt).getTime() - new Date(prev.createdAt).getTime();
			if (Number.isFinite(gap) && gap > GROUP_WINDOW_MS) return false;
		}
		return true;
	}

	onMount(() => {
		session.init().then(() => {
			// Deep link: /chat?room=<token> selects that room for this load,
			// overriding the persisted-room default. An unknown / not-owned token
			// isn't in the per-user list → silent fallback to the default.
			const token = page.url.searchParams.get('room');
			if (token) session.selectRoomByToken(token);
		});
		getMe()
			.then((me) => {
				if (me.display_name) userName = me.display_name;
				if (me.bot_name) botName = me.bot_name;
			})
			.catch(() => {});
	});

	// Stop the active stream when leaving /chat so the EventSource / poll timer
	// doesn't linger; remounting re-subscribes from persisted events.
	onDestroy(() => session.teardown());

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

	async function saveRoomName(name: string) {
		if (!settingsRoom) return;
		await session.renameRoom(settingsRoom.id, name);
		settingsRoom = null;
	}

	async function deleteRoom() {
		if (!settingsRoom) return;
		const id = settingsRoom.id;
		settingsRoom = null;
		await session.deleteRoom(id);
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
				<div class="room-row" class:active={room.id === $activeRoomId}>
					<button
						class="room-btn"
						onclick={() => selectRoom(room.id)}
						type="button"
					>
						<MessageSquare size={13} />
						<span class="room-name">{room.name}</span>
					</button>
					<KebabMenu
						ariaLabel="Room actions"
						items={[{ label: 'Settings…', onSelect: () => (settingsRoom = room) }]}
					/>
				</div>
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
				{#each $messages as message, i (message.cid)}
					<Message
						{message}
						continuation={isContinuation(i)}
						{userName}
						{botName}
						onConfirm={session.confirm}
						onReject={session.reject}
					/>
				{/each}
			{/if}
		</div>
		<Composer
			onSend={(t, atts) => session.send(t, atts)}
			onCancel={() => session.cancel()}
			busy={busy}
			placeholder={activeRoom ? `Message #${activeRoom.name}…` : 'Message Istota…'}
		/>
	</div>

	{#if settingsRoom}
		<RoomSettings
			room={settingsRoom}
			onSave={saveRoomName}
			onDelete={deleteRoom}
			onClose={() => (settingsRoom = null)}
		/>
	{/if}
</AppShell>

<style>
	.chat-pane {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		/* Lighter gray than the app base (#111) so the white text reads with
		   softer contrast — matches the message hover-highlight shade. */
		background: var(--surface-card);
		/* Soften body text a touch (scoped to chat) to further ease the
		   light-on-dark contrast. */
		--text-primary: #cfcfcf;
	}
	/* Light theme: the chat-scoped soften must flip to a soft *dark* text,
	   otherwise the #cfcfcf above is unreadable on the light pane. */
	:global(:root[data-theme='light']) .chat-pane {
		--text-primary: #2a2a2e;
	}
	.messages {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		/* Row padding lives in Message (so the hover highlight spans the full
		   channel width, Discord-style). Just a little breathing room here. */
		padding: 0.5rem 0 1rem;
		width: 100%;
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

	.room-row {
		display: flex;
		align-items: center;
		gap: 0.15rem;
		border-radius: 0.3rem;
		padding-right: 0.2rem;
		transition: background var(--transition-fast);
	}
	.room-row:hover { background: var(--surface-raised); }
	.room-row.active { background: var(--surface-raised); }

	.room-btn {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		flex: 1;
		min-width: 0;
		background: none;
		border: none;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-base);
		cursor: pointer;
		padding: 0.35rem 0.6rem;
		border-radius: 0.3rem;
		text-align: left;
		transition: color var(--transition-fast);
	}
	.room-row:hover .room-btn { color: var(--text-secondary); }
	.room-row.active .room-btn { color: var(--text-primary); }
	.room-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
