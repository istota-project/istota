<script lang="ts">
	import { onMount, onDestroy, tick } from 'svelte';
	import { page } from '$app/state';
	import { Plus, MessageSquare, Cloud, ChevronDown } from 'lucide-svelte';
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
	const { rooms, activeRoomId, messages, status, loaded, hasMore, loadingOlder } = session;

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
		// A message that opens a new day starts a fresh group (full header) under
		// the day divider, even from the same author within the window.
		if (startsNewDay(i)) return false;
		if (prev.createdAt && cur.createdAt) {
			const gap = new Date(cur.createdAt).getTime() - new Date(prev.createdAt).getTime();
			if (Number.isFinite(gap) && gap > GROUP_WINDOW_MS) return false;
		}
		return true;
	}

	// Day-divider support (ISSUE-127). Time-only stamps are ambiguous once
	// backfilled history lands older messages in a room; a divider row between
	// days resolves "is this today or last month" without stamping a full date on
	// every bubble. Day boundaries use the viewer's local timezone, not UTC, so
	// "Today" matches the user's clock.
	function localDayKey(iso: string): string | null {
		const d = new Date(iso);
		if (Number.isNaN(d.getTime())) return null;
		return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
	}
	// True when message i is the first (rendered) message of its calendar day —
	// i.e. its day differs from the previous message's (or it's the very first).
	function startsNewDay(i: number): boolean {
		const cur = $messages[i]?.createdAt;
		if (!cur) return false;
		const curKey = localDayKey(cur);
		if (!curKey) return false;
		if (i === 0) return true;
		const prev = $messages[i - 1]?.createdAt;
		const prevKey = prev ? localDayKey(prev) : null;
		return curKey !== prevKey;
	}
	function dayLabel(iso: string): string {
		const d = new Date(iso);
		if (Number.isNaN(d.getTime())) return '';
		const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
		const today = startOfDay(new Date());
		const that = startOfDay(d);
		const days = Math.round((today.getTime() - that.getTime()) / 86400000);
		if (days === 0) return 'Today';
		if (days === 1) return 'Yesterday';
		if (days > 1 && days < 7) return d.toLocaleDateString([], { weekday: 'long' });
		const sameYear = d.getFullYear() === today.getFullYear();
		return d.toLocaleDateString([], sameYear
			? { month: 'short', day: 'numeric' }
			: { year: 'numeric', month: 'short', day: 'numeric' });
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

	// Stick-to-bottom only when the user is already at the bottom (B1). A plain
	// (non-reactive) latch sampled by the scroll handler *before* the store grows
	// the DOM — recomputing it inside the post-update effect would read the
	// already-grown height and always look "not at bottom". Starts true so the
	// first load and new sends pin to the newest message.
	let atBottom = true;
	const BOTTOM_THRESHOLD = 64; // px slack counted as "at the bottom"
	const TOP_THRESHOLD = 160; // px from the top that triggers an older-page load

	// Reactive mirror of `atBottom` for the jump-to-latest affordance. Kept
	// separate from the (non-reactive) `atBottom` latch so reading it never makes
	// the bottom-pin effect re-run on scroll.
	let showJumpToLatest = $state(false);

	function sampleAtBottom() {
		if (!listEl) return;
		atBottom = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight <= BOTTOM_THRESHOLD;
		showJumpToLatest = !atBottom;
	}

	function jumpToLatest() {
		if (!listEl) return;
		listEl.scrollTo({ top: listEl.scrollHeight, behavior: 'smooth' });
		atBottom = true;
		showJumpToLatest = false;
	}

	async function onScroll() {
		if (!listEl) return;
		sampleAtBottom();
		// Near the top with older history available → fetch the previous page and
		// restore the scroll anchor so the viewport stays put (scroll-anchored
		// prepend). The store's loadingOlder guard makes this re-entrancy-safe.
		if (listEl.scrollTop <= TOP_THRESHOLD && $hasMore && !$loadingOlder) {
			const prevHeight = listEl.scrollHeight;
			const prevTop = listEl.scrollTop;
			await session.loadOlder();
			await tick();
			if (listEl) listEl.scrollTop = listEl.scrollHeight - prevHeight + prevTop;
		}
	}

	// Auto-scroll to the newest message when the list changes — but only if we
	// were at the bottom before the change (a streamed delta, a new send, a
	// notification append while reading the latest). A scroll-up prepend leaves
	// atBottom false, so the anchor restore in onScroll owns the viewport instead.
	$effect(() => {
		$messages;
		if (!atBottom) return;
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

	async function promoteRoom() {
		if (!settingsRoom) return;
		const id = settingsRoom.id;
		await session.promoteRoom(id);
		// Reflect the new binding in the open modal (button → "On Talk").
		settingsRoom = $rooms.find((r) => r.id === id) ?? null;
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
				{@const isTalk = room.origin === 'talk' || !!room.talk_token}
				{@const unreadCount = room.unread_count ?? 0}
				{@const unread = unreadCount > 0 && room.id !== $activeRoomId}
				<div class="room-row" class:active={room.id === $activeRoomId}>
					<button
						class="room-btn"
						onclick={() => selectRoom(room.id)}
						type="button"
					>
						{#if isTalk}
							<!-- Leading origin glyph: a tinted cloud marks a room mirrored
							     to Nextcloud Talk. Sits in its own flex slot before the
							     title so it never eats name width or gets clipped by the
							     title's ellipsis (ISSUE-129). -->
							<span class="room-origin talk" title="Also on Nextcloud Talk">
								<Cloud size={13} />
							</span>
						{:else}
							<span class="room-origin" title="Web room">
								<MessageSquare size={13} />
							</span>
						{/if}
						<span class="room-name" class:unread>{room.name}</span>
						{#if unread}
							<span class="unread-chip" title={`${unreadCount} unread`}>
								{unreadCount > 99 ? '99+' : unreadCount}
							</span>
						{/if}
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
		<div class="messages-wrap">
		<div class="messages" bind:this={listEl} role="log" aria-live="polite" onscroll={onScroll}>
			{#if !$loaded}
				<div class="chat-empty">Loading…</div>
			{:else if $messages.length === 0}
				<div class="chat-empty">
					<MessageSquare size={28} />
					<p>Ask {activeRoom ? `in #${activeRoom.name}` : 'Istota'} anything.</p>
					<span class="hint">Configuration help, quick tasks, or one-off questions.</span>
				</div>
			{:else}
				<!-- Older-history affordance (B3): a spinner while a page loads, a
				     quiet marker once the start of the conversation is reached. -->
				{#if $loadingOlder}
					<div class="older-status" role="status">Loading older messages…</div>
				{:else if !$hasMore}
					<div class="older-status begin">Beginning of conversation</div>
				{/if}
				{#each $messages as message, i (message.cid)}
					{#if message.createdAt && startsNewDay(i)}
						<div class="day-divider" role="separator">
							<span class="day-label">{dayLabel(message.createdAt)}</span>
						</div>
					{/if}
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
		<!-- Jump-to-latest: shown only when scrolled up off the bottom. -->
		{#if showJumpToLatest}
			<button class="jump-latest" onclick={jumpToLatest} aria-label="Scroll to latest message" title="Scroll to latest">
				<ChevronDown size={20} />
			</button>
		{/if}
		</div>
		<Composer
			onSend={(t, atts) => session.send(t, atts)}
			onCancel={() => session.cancel()}
			busy={busy}
			placeholder="Your message…"
		/>
	</div>

	{#if settingsRoom}
		<RoomSettings
			room={settingsRoom}
			onSave={saveRoomName}
			onDelete={deleteRoom}
			onPromote={promoteRoom}
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
	   otherwise the #cfcfcf above is unreadable on the light pane. A white
	   message area (the composer paints its own --surface-card, so the input
	   section keeps the soft-gray fill). */
	:global(:root[data-theme='light']) .chat-pane {
		--text-primary: #2a2a2e;
		background: #ffffff;
	}
	/* Wrapper anchors the floating jump-to-latest button to the bottom-right of
	   the scroll area, above the composer, independent of composer height. */
	.messages-wrap {
		position: relative;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
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

	/* Jump-to-latest FAB — appears bottom-right when the user scrolls up off the
	   newest message; click smooth-scrolls back to the bottom. */
	.jump-latest {
		position: absolute;
		/* Right edge aligned with the composer's send button: the composer has
		   0.75rem of horizontal padding and the 36px send button sits flush
		   against it, so matching `right` + width lines the two up vertically. */
		right: 0.75rem;
		bottom: 0.75rem;
		z-index: 5;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		border-radius: 999px;
		border: 1px solid var(--border-default);
		background: var(--surface-overlay, var(--surface-card));
		color: var(--text-primary);
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
		cursor: pointer;
		opacity: 0.9;
		transition: opacity 0.12s ease, transform 0.12s ease;
	}
	.jump-latest:hover { opacity: 1; transform: translateY(-1px); }
	.jump-latest:active { transform: translateY(0); }

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

	/* Older-history affordance (ISSUE-131): a centered, low-key status row at the
	   top of the transcript while a previous page loads or once the start is
	   reached. */
	.older-status {
		text-align: center;
		color: var(--text-dim);
		font-size: var(--text-sm);
		padding: 0.5rem 0.75rem 0.7rem;
	}
	.older-status.begin {
		color: var(--text-dim);
		opacity: 0.6;
	}

	/* Day divider (ISSUE-127): a centered date pill on a hairline rule, marking
	   the boundary between calendar days in the transcript. */
	.day-divider {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		margin: 0.9rem 0 0.3rem;
		padding: 0 0.75rem;
	}
	.day-divider::before,
	.day-divider::after {
		content: '';
		flex: 1;
		height: 1px;
		background: var(--border-subtle);
	}
	.day-label {
		flex-shrink: 0;
		font-size: var(--text-xs);
		font-weight: 600;
		letter-spacing: 0.02em;
		color: var(--text-dim);
		text-transform: uppercase;
	}

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
	.room-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	/* A room with unseen bot/system messages reads bolder; the active room never
	   bolds (looking at it is reading it). */
	.room-name.unread { font-weight: 700; color: var(--text-primary); }
	/* Count chip in its own non-shrink slot so the name's ellipsis can't clip it
	   (same fixed-slot pattern as .room-origin). */
	.unread-chip {
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 1.15rem;
		height: 1.15rem;
		padding: 0 0.35rem;
		border-radius: var(--radius-pill);
		background: var(--accent);
		color: var(--surface-base);
		font-size: 0.7rem;
		font-weight: 600;
		line-height: 1;
	}
	/* Leading origin glyph. Fixed slot before the title so a long room name
	   still gets the full row width and the icon never enters the title's
	   truncation box. */
	.room-origin {
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		color: var(--text-dim);
	}
	.room-origin.talk { color: var(--accent-amber); }
</style>
