<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import { page } from '$app/state';
  import { Plus, MessageSquare, Cloud, ChevronDown, Circle, Star, CheckCheck } from 'lucide-svelte';
  import {
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    KebabMenu,
    Chip,
    ConfirmDialog,
  } from '$lib/components/ui';
  import Message from '$lib/components/chat/Message.svelte';
  import Composer from '$lib/components/chat/Composer.svelte';
  import RoomSettings from '$lib/components/chat/RoomSettings.svelte';
  import {
    isTap,
    nextActivation,
    UNCHANGED,
    type Activation,
    type PointerSample,
  } from '$lib/components/chat/tapActivation';
  import { getChatSession } from '$lib/stores/chat';
  import { getMe, type ChatRoom, type ChatView } from '$lib/api';

  const session = getChatSession();
  const {
    rooms,
    activeRoomId,
    messages,
    status,
    loaded,
    hasMore,
    loadingOlder,
    view,
    scrollTarget,
  } = session;

  // Cross-room views: the transcript pane renders either the active room
  // ('room') or a read-only aggregate stream (all/unread/starred).
  const inViewMode = $derived($view !== 'room');
  const VIEW_LABELS: Record<ChatView, string> = {
    all: 'All messages',
    unread: 'Unread',
    starred: 'Starred',
  };
  // Client-side total for the sidebar Unread badge (sum of per-room counts;
  // the active room is already held at 0 by the store).
  const unreadTotal = $derived($rooms.reduce((n, r) => n + (r.unread_count ?? 0), 0));

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
  // The docked composer floats over the transcript, so its height is a layout
  // input: it drives the transcript's bottom padding (keeping the newest message
  // clear of the pill) and the jump-to-latest offset. Measured rather than
  // guessed — the composer grows with attachments, error chips and wrapped text.
  let dockEl: HTMLDivElement | undefined = $state();
  let composerH = $state(0);

  const activeRoom = $derived($rooms.find((r) => r.id === $activeRoomId) ?? null);
  const busy = $derived($status === 'sending' || $status === 'streaming');

  // The room's standing model default as a header badge — the canonical model
  // name (e.g. `claude-opus-4-8`), not the alias, so it's unambiguous. null
  // when the room has no default (or in a cross-room view).
  const modelBadge = $derived.by(() => {
    if (inViewMode || !activeRoom) return null;
    const { model, effort } = activeRoom;
    if (!model && !effort) return null;
    let label = model ?? 'default model';
    if (effort) label += ` · ${effort}`;
    return label;
  });

  // Discord/Slack-style grouping: a message continues the previous author's
  // run (collapsing its avatar + header) when it's the same non-system author
  // within a short window.
  const GROUP_WINDOW_MS = 5 * 60 * 1000;
  function isContinuation(i: number): boolean {
    if (i <= 0) return false;
    const prev = $messages[i - 1];
    const cur = $messages[i];
    if (!prev || prev.role !== cur.role || cur.role === 'system') return false;
    // Aggregate views interleave rooms: a room change always starts a fresh
    // group (the header carries the room chip).
    if (prev.roomToken !== cur.roomToken) return false;
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
    return d.toLocaleDateString(
      [],
      sameYear
        ? { month: 'short', day: 'numeric' }
        : { year: 'numeric', month: 'short', day: 'numeric' },
    );
  }

  onMount(() => {
    session.init().then(() => {
      // Deep link: /chat?room=<token> selects that room for this load,
      // overriding the persisted-room default. An unknown / not-owned token
      // isn't in the per-user list → silent fallback to the default.
      // /chat?view=all|unread|starred opens an aggregate view instead; an
      // unknown value falls back silently, same as an unknown room token.
      const token = page.url.searchParams.get('room');
      const v = page.url.searchParams.get('view');
      const taskParam = page.url.searchParams.get('task');
      if (token) {
        // /chat?room=<token>&task=<id>: after selecting the room, jump to
        // the referenced turn (paging older history if needed). A bare
        // ?room= just selects the room. jumpToTask itself selects the room,
        // so a valid &task supersedes the plain select.
        const taskId = taskParam ? Number(taskParam) : NaN;
        if (Number.isFinite(taskId)) session.jumpToTask(token, taskId);
        else session.selectRoomByToken(token);
      } else if (v === 'all' || v === 'unread' || v === 'starred') session.selectView(v);
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
  onDestroy(() => {
    if (highlightTimer) clearTimeout(highlightTimer);
    session.teardown();
  });

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

  /**
   * Pin the transcript to its newest message.
   *
   * `repaint` is for the case where the scroller's whole content was just
   * replaced — a room or view switch. iOS Safari sometimes leaves that frame
   * unpainted: the DOM is there and the offset is right (the newest message
   * shows up exactly where it belongs the instant anything forces a repaint —
   * a swipe, or merely opening the rooms drawer), but the pane reads blank,
   * empty-state placeholder included. It only shows on rooms with enough
   * history to scroll, because only those move the offset at all — replacing
   * the content and jumping in the same frame is what loses the invalidation.
   *
   * So on those switches we make the scroll happen for real, across frames:
   * one pixel off the bottom, then back. Re-assigning the offset it already
   * holds would be a no-op and invalidate nothing, which is why this nudges
   * rather than just repeating the pin. Two frames at 1px is invisible, and it
   * doubles as a late correction if the composer or an image settled after the
   * first pin.
   */
  function pinToBottom(repaint = false) {
    if (!listEl) return;
    listEl.scrollTop = listEl.scrollHeight;
    if (!repaint || typeof requestAnimationFrame === 'undefined') return;
    requestAnimationFrame(() => {
      if (!listEl) return;
      // Relative to the max *scroll offset*, not scrollHeight: scrollHeight - 1
      // clamps straight back to the bottom on any scroller taller than a pixel,
      // so it would leave the offset unchanged and repaint nothing.
      const maxTop = listEl.scrollHeight - listEl.clientHeight;
      if (maxTop <= 1) return; // nothing to scroll, so nothing was jumped
      listEl.scrollTop = maxTop - 1;
      requestAnimationFrame(() => {
        if (listEl) listEl.scrollTop = listEl.scrollHeight;
      });
    });
  }

  async function onScroll() {
    if (!listEl) return;
    clearActivation();
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

  // Touch surrogate for hover (the per-message metadata + star). A touch device
  // has no hover, and iOS Safari's synthesized one sticks: it clears the pseudo
  // class only on the next tap, so every row tapped in a run kept its star
  // showing. One activated row instead — a mouse pointer is left alone, it has
  // real hover. Rules in tapActivation.ts; this owns the state and the clears.
  let activeCid: Activation = $state(null);
  let tapStart: PointerSample | null = null;

  function onListPointerDown(e: PointerEvent) {
    if (e.pointerType === 'mouse') return;
    tapStart = { x: e.clientX, y: e.clientY, t: e.timeStamp };
  }

  function onListPointerUp(e: PointerEvent) {
    if (e.pointerType === 'mouse') return;
    const start = tapStart;
    tapStart = null;
    // A scroll flick also ends with a pointerup over a row, and a long press is
    // a text selection. Neither activates.
    if (!start || !isTap(start, { x: e.clientX, y: e.clientY, t: e.timeStamp })) return;
    const next = nextActivation(e.target as Element | null, activeCid);
    if (next !== UNCHANGED) activeCid = next;
  }

  // Everything that ends an activation without a tap on the list. Scrolling
  // carries the row off-screen (and starts as a touch on it, so it would
  // otherwise stay lit), and a tap anywhere else on the page — composer,
  // sidebar, header — is the user moving on. Capture phase so a handler that
  // stops propagation can't strand it.
  function clearActivation() {
    if (activeCid !== null) activeCid = null;
  }

  // Last pointer to touch the page. `@media (hover: hover)` is the first line of
  // defence against synthesized hover, but it answers for the device, not the
  // gesture: a touchscreen laptop or an iPad with a trackpad reports hover, and
  // a finger on one of those still leaves a sticky :hover behind. So the rows
  // also defer to what was last used, and hover reveals go quiet after a touch.
  let pointerIsTouch = $state(false);

  $effect(() => {
    const onDocPointerDown = (e: PointerEvent) => {
      pointerIsTouch = e.pointerType !== 'mouse';
      if (!pointerIsTouch) return;
      const t = e.target as Node | null;
      if (t && listEl?.contains(t)) return; // list taps are the list's own business
      clearActivation();
    };
    document.addEventListener('pointerdown', onDocPointerDown, true);
    return () => document.removeEventListener('pointerdown', onDocPointerDown, true);
  });

  // Leaving the room takes the activation with it — the rows it referred to are
  // gone, and a cid from the old transcript could collide with one in the new.
  $effect(() => {
    $activeRoomId;
    $view;
    activeCid = null;
  });

  // A room / view switch replaces the transcript wholesale, so the next
  // non-empty render is a fresh conversation that opens at its newest message —
  // wherever the user happened to be scrolled in the room they left. Without
  // the latch reset, leaving a room mid-history skipped the pin entirely and the
  // new room opened at the top of its first page. Plain (non-reactive) lets, so
  // neither this effect nor the one below re-runs on them.
  let switchPending = false;
  $effect(() => {
    $activeRoomId;
    $view;
    switchPending = true;
    atBottom = true;
  });

  // Auto-scroll to the newest message when the list changes — but only if we
  // were at the bottom before the change (a streamed delta, a new send, a
  // notification append while reading the latest). A scroll-up prepend leaves
  // atBottom false, so the anchor restore in onScroll owns the viewport instead.
  $effect(() => {
    const msgs = $messages;
    if (!atBottom) return;
    // First content after a switch: pin with the repaint pass. The empty render
    // the switch passes through on its way there doesn't count — it has nothing
    // to paint and no offset to lose.
    const afterSwitch = switchPending && msgs.length > 0;
    if (afterSwitch) switchPending = false;
    tick().then(() => pinToBottom(afterSwitch));
  });

  // Track the docked composer's height. The transcript reserves it as bottom
  // padding *inside* the scroller, so scrollHeight already accounts for it and
  // the bottom-pin below stays plain `scrollTop = scrollHeight` — no offset
  // arithmetic. What does need handling is the composer growing (or the dock
  // disappearing in an aggregate view) while pinned: the reserved band changes
  // under a viewport that was at the bottom, so re-pin after each measurement.
  $effect(() => {
    if (!dockEl) {
      composerH = 0;
      return;
    }
    const el = dockEl;
    const measure = () => {
      composerH = el.offsetHeight;
      if (atBottom) tick().then(() => pinToBottom());
    };
    measure();
    // jsdom has no ResizeObserver; the one-shot measure above is enough there.
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  });

  // Jump-to-response: the store resolves a search result to a transcript cid
  // and bumps `scrollTarget`; here we do the DOM scroll + a transient highlight
  // pulse. The nonce makes a repeated jump to the same row re-fire.
  let highlightTimer: ReturnType<typeof setTimeout> | undefined;
  $effect(() => {
    const t = $scrollTarget;
    if (!t) return;
    tick().then(() => {
      const el = listEl?.querySelector(`[data-cid="${t.cid}"]`) as HTMLElement | null;
      if (!el) return;
      // The row just paged in / room just switched — don't let the
      // stick-to-bottom effect fight the jump. The jump target is centered
      // (off the bottom), so reveal the jump-to-latest affordance; a real
      // scroll event re-samples if it happens to land at the bottom.
      atBottom = false;
      showJumpToLatest = true;
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('jump-highlight');
      if (highlightTimer) clearTimeout(highlightTimer);
      highlightTimer = setTimeout(() => el.classList.remove('jump-highlight'), 2000);
    });
  });

  function selectRoom(id: number) {
    session.selectRoom(id);
    sidebarOpen = false;
  }

  function selectView(v: ChatView) {
    session.selectView(v);
    sidebarOpen = false;
  }

  // Mark every room read (header chip). Confirmed like the feeds equivalent —
  // it's a bulk, not-really-undoable cursor advance.
  let confirmMarkAllRead = $state(false);

  async function performMarkAllRead() {
    confirmMarkAllRead = false;
    await session.markAllRead();
  }

  async function createRoom() {
    const name = newRoomName.trim();
    if (!name) return;
    newRoomName = '';
    creatingRoom = false;
    await session.newRoom(name);
    sidebarOpen = false;
  }

  async function saveRoomSettings(patch: {
    name?: string;
    model?: string | null;
    effort?: string | null;
  }) {
    if (!settingsRoom) return;
    await session.updateRoomSettings(settingsRoom.id, patch);
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

<!-- insetBottom={false}: the Composer holds the bottom safe-area inset itself, so
     its fill reaches the screen edge while its controls stay above the indicator. -->
<AppShell insetBottom={false}>
  {#snippet header()}
    <ShellHeader
      title={inViewMode ? VIEW_LABELS[$view as ChatView] : activeRoom ? activeRoom.name : 'Chat'}
      onTitleClick={() => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="open rooms"
    >
      {#snippet leading()}
        <SidebarToggle
          open={sidebarOpen}
          label="Rooms"
          count={$rooms.length}
          onclick={() => (sidebarOpen = !sidebarOpen)}
        />
      {/snippet}
      {#snippet nav()}
        {#if modelBadge}
          <button
            class="model-badge"
            type="button"
            title="Room model default — click to change"
            onclick={() => activeRoom && (settingsRoom = activeRoom)}
          >
            {modelBadge}
          </button>
        {/if}
      {/snippet}
      {#snippet tools()}
        <Chip icon onclick={() => (confirmMarkAllRead = true)} title="Mark all rooms as read">
          <CheckCheck size={14} />
        </Chip>
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
      <!-- Cross-room views, above the rooms list (mirrors the feeds sidebar's
			     All / Unread / Starred entries). Selecting one deselects the room. -->
      <div class="views">
        <button
          class="view-btn"
          class:active={$view === 'all'}
          onclick={() => selectView('all')}
          type="button"
        >
          <span class="view-name">All</span>
        </button>
        <button
          class="view-btn"
          class:active={$view === 'unread'}
          onclick={() => selectView('unread')}
          type="button"
        >
          <Circle size={12} />
          <span class="view-name">Unread</span>
          {#if unreadTotal > 0}
            <span class="unread-chip" title={`${unreadTotal} unread`}>
              {unreadTotal > 99 ? '99+' : unreadTotal}
            </span>
          {/if}
        </button>
        <button
          class="view-btn"
          class:active={$view === 'starred'}
          onclick={() => selectView('starred')}
          type="button"
        >
          <Star size={12} />
          <span class="view-name">Starred</span>
        </button>
      </div>

      <!-- Sits with the rooms list it adds to, below the cross-room views. -->
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
              if (e.key === 'Escape') {
                creatingRoom = false;
                newRoomName = '';
              }
            }}
            onblur={() => {
              if (!newRoomName.trim()) creatingRoom = false;
            }}
          />
        {:else}
          <button class="room-add" onclick={() => (creatingRoom = true)} type="button">
            <Plus size={14} /> New room
          </button>
        {/if}
      </div>

      {#each $rooms as room (room.id)}
        {@const isTalk = room.origin === 'talk' || !!room.talk_token}
        {@const unreadCount = room.unread_count ?? 0}
        {@const unread = unreadCount > 0 && room.id !== $activeRoomId}
        <div class="list-row room-row" class:active={room.id === $activeRoomId}>
          <button class="room-btn" onclick={() => selectRoom(room.id)} type="button">
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
            <span class="room-text">
              <span class="room-line">
                <span class="room-name" class:unread>{room.name}</span>
                {#if unread}
                  <span class="unread-chip" title={`${unreadCount} unread`}>
                    {unreadCount > 99 ? '99+' : unreadCount}
                  </span>
                {/if}
              </span>
            </span>
          </button>
          <KebabMenu
            ariaLabel="Room actions"
            items={[{ label: 'Settings', onSelect: () => (settingsRoom = room) }]}
          />
        </div>
      {/each}
    </Sidebar>
  {/snippet}

  <div class="chat-pane" style:--composer-h="{composerH}px">
    <div class="messages-wrap">
      <div
        class="messages"
        bind:this={listEl}
        role="log"
        aria-live="polite"
        onscroll={onScroll}
        onpointerdown={onListPointerDown}
        onpointerup={onListPointerUp}
        onpointercancel={() => (tapStart = null)}
      >
        {#if !$loaded}
          <div class="chat-empty">Loading…</div>
        {:else if $messages.length === 0}
          <div class="chat-empty">
            {#if $view === 'unread'}
              <CheckCheck size={28} />
              <p>All caught up</p>
            {:else if $view === 'starred'}
              <Star size={28} />
              <p>Nothing starred yet.</p>
              <span class="hint">Hover a message and hit the star.</span>
            {:else if $view === 'all'}
              <MessageSquare size={28} />
              <p>No messages yet</p>
            {:else}
              <MessageSquare size={28} />
              <p>
                {activeRoom
                  ? `Ask anything in #${activeRoom.name.replace(/^#+/, '')}.`
                  : 'Ask Istota anything.'}
              </p>
              <span class="hint">Configuration help, quick tasks, or one-off questions.</span>
            {/if}
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
              onToggleStar={session.toggleStar}
              onRoomClick={inViewMode ? (token) => session.selectRoomByToken(token) : undefined}
              onJump={(token, taskId) => session.jumpToTask(token, taskId)}
              aggregate={inViewMode}
              active={message.cid === activeCid}
              touch={pointerIsTouch}
            />
          {/each}
        {/if}
        <!-- Bottom reserve: keeps the newest message clear of the docked
             composer. A spacer rather than padding on the scroller, because the
             fade below is a sticky child and sticky is constrained to its
             containing block's *content* box — as padding, the reserve would
             park the fade that far above the scrollport's bottom edge. Either
             way it is inside the scroller, so scrollHeight accounts for it and
             the stick-to-bottom pin stays a plain `scrollTop = scrollHeight`.

             Only with messages present: with none, there is nothing to keep
             clear of the pill, and the reserve made the scroller taller than
             its own viewport, so the bottom-pin scrolled the empty-state notice
             up by the reserve's height — it read as sitting above centre for
             exactly the composer's worth of space. Without it the empty
             scroller has no scroll range and `height: 100%` centres in the
             scrollport. -->
        {#if $loaded && $messages.length > 0}
          <div class="composer-reserve" aria-hidden="true"></div>
        {/if}
        {#if !inViewMode && $loaded && $messages.length > 0}
          <!-- Fade layer, sized to the composer band it sits behind: content
               scrolling into that band dissolves into the pane fill instead of
               running under the pill at full strength. It is a child of the
               scroller (sticky, pinned to the bottom of the scrollport) rather
               than an overlay over it, because a scroller paints its scrollbar
               above its own content — an overlay sibling painted over the
               bottom of the scrollbar too, so the thumb dissolved along with
               the text. -->
          <div class="composer-fade" aria-hidden="true"></div>
        {/if}
      </div>
      <!-- Jump-to-latest: shown only when scrolled up off the bottom. -->
      {#if showJumpToLatest}
        <button
          class="jump-latest"
          onclick={jumpToLatest}
          aria-label="Scroll to latest message"
          title="Scroll to latest"
        >
          <ChevronDown size={20} />
        </button>
      {/if}
    </div>
    {#if !inViewMode}
      <!-- Sending is room-scoped; aggregate views are read-only panes.
           Docked over the transcript rather than sharing the column with it, so
           the message list runs the full height of the pane and content passes
           under the composer instead of stopping short of it. -->
      <div class="composer-dock" bind:this={dockEl}>
        <Composer
          onSend={(t, atts) => {
            // Sending is the end of reading back: whatever the user had scrolled
            // up to look at, the message they just wrote — and the reply to it —
            // is what they want to see. So the send re-arms the stick-to-bottom
            // latch rather than respecting it, which is the one case where the
            // "only if you were already at the bottom" rule is wrong.
            //
            // Pinned immediately as well as latched: `send` is async, so the
            // message may be a network round trip away, and the transcript
            // should be waiting at the bottom for it rather than jumping when it
            // lands. The $messages effect covers the landing itself.
            atBottom = true;
            showJumpToLatest = false;
            void session.send(t, atts);
            tick().then(() => pinToBottom());
          }}
          onCancel={() => session.cancel()}
          {busy}
          placeholder="Your message…"
        />
      </div>
    {/if}
  </div>

  {#if settingsRoom}
    <RoomSettings
      room={settingsRoom}
      onSave={saveRoomSettings}
      onDelete={deleteRoom}
      onPromote={promoteRoom}
      onClose={() => (settingsRoom = null)}
    />
  {/if}

  <ConfirmDialog
    bind:open={confirmMarkAllRead}
    title="Mark all rooms as read"
    message="Are you sure you want to mark all rooms as read? This can't be undone."
    confirmLabel="Mark all read"
    confirmVariant="primary"
    onConfirm={performMarkAllRead}
  />
</AppShell>

<style>
  /* Room model-default badge beside the header title. Clickable → opens the
	   room-settings modal. Only shown when the room has a standing default. */
  .model-badge {
    display: inline-flex;
    align-items: center;
    font: inherit;
    font-size: var(--text-xs);
    line-height: 1;
    color: var(--text-muted);
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    padding: 0.2rem 0.5rem;
    cursor: pointer;
    white-space: nowrap;
    transition:
      color var(--transition-fast),
      border-color var(--transition-fast);
  }
  .model-badge:hover {
    color: var(--text-primary);
    border-color: var(--border-hover);
  }

  .chat-pane {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    /* Anchors the docked composer. */
    position: relative;
    /* Published as a variable as well as applied: the docked composer paints
       this same fill behind itself and fades the transcript out into it, and it
       can't read a sibling component's `background`. */
    --chat-bg: var(--surface-reading);
    background: var(--chat-bg);
    /* Soften body text a step (scoped to chat) to ease sustained reading. The
		   token flips to a soft dark in light, so this needs no override. */
    --text-primary: var(--text-reading);
  }

  /* The composer floats over the transcript instead of taking a row of its own,
	   so the message list keeps the full pane height and content scrolls under it.
	   The composer itself is transparent — the fade layer below is the backdrop —
	   and the transcript reserves the dock's measured height as bottom padding. */
  .composer-dock {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 6;
  }

  /* Covers the composer's band plus a short run-up above it, so the dissolve is
	   already under way before content reaches the pill. The gradient's solid stop
	   is an absolute length rather than a percentage: the band's height moves with
	   the composer (attachments, wrapped text), and a percentage would stretch the
	   soft part with it — the fade would start over the transcript proper on a
	   tall composer. z-index keeps it under the jump-to-latest FAB (5) and the
	   dock (6); pointer-events: none so it never swallows a click. */
  .composer-fade {
    position: sticky;
    bottom: 0;
    height: calc(var(--composer-h, 0px) + 1.5rem);
    /* Cancels its own height so it overlaps the reserve above rather than
       extending the scroll range. */
    margin-top: calc(-1 * (var(--composer-h, 0px) + 1.5rem));
    background: linear-gradient(to bottom, transparent, var(--chat-bg) 2.5rem);
    pointer-events: none;
  }

  .composer-reserve {
    height: calc(var(--composer-h, 0px) + 1rem);
  }
  /* Wrapper anchors the floating jump-to-latest button to the bottom of the
	   scroll area; the button offsets itself above the docked composer. */
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
    /* The transcript owns the vertical gesture. Without this, a pan that runs
		   past the top or bottom of the list chains to the page, which on mobile
		   reads as the page bouncing while the messages sit still. */
    overscroll-behavior: contain;
    /* Row padding lives in Message (so the hover highlight spans the full
		   channel width, Discord-style). Just a little breathing room here. */
    /* The bottom reserve is the `.composer-reserve` spacer at the end of the
			 list rather than padding here — see its comment in the markup. */
    padding: 0.5rem 0 0;
    width: 100%;
  }

  /* Jump-to-latest FAB — appears bottom-right when the user scrolls up off the
	   newest message; click smooth-scrolls back to the bottom. */
  .jump-latest {
    position: absolute;
    /* Centered over the scroll area. It used to hang off the right edge to line
		   up with the old square send button; against the composer's round send
		   circle that reads as two mismatched arrows stacked in the same corner,
		   so it sits in the middle instead — which also keeps it clear of the
		   text as the pill grows. The centering translate is folded into the
		   hover/active transforms below, or they would cancel it. */
    left: 50%;
    transform: translateX(-50%);
    /* Rides above the docked composer, which now overlaps this wrapper's bottom
			 edge; without the offset it would sit behind the pill. */
    bottom: calc(var(--composer-h, 0px) + 0.75rem);
    z-index: 5;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border-radius: 999px;
    border: 1px solid var(--border-default);
    background: var(--surface-overlay);
    color: var(--text-primary);
    box-shadow: var(--shadow-overlay);
    cursor: pointer;
    /* Fully opaque: centered it floats over message text rather than over the
		   right margin, and the old 0.9 let that text read straight through. */
    opacity: 1;
    transition: transform 0.12s ease;
  }
  .jump-latest:hover {
    transform: translate(-50%, -1px);
  }
  .jump-latest:active {
    transform: translate(-50%, 0);
  }

  .chat-empty {
    height: 100%;
    /* The composer is docked *over* the scrollport, so centring in the full
		   height puts the notice half a composer below the middle of the space the
		   user can actually see. Reserving the pill's height discounts it from the
		   centring instead. Inside the 100% (border-box globally), so unlike the
		   `.composer-reserve` spacer this adds no scroll range for the
		   stick-to-bottom pin to act on — the reason the notice used to read high
		   by the same measure. 0 in an aggregate view, which has no dock. */
    padding-bottom: var(--composer-h, 0px);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    color: var(--text-dim);
    /* Matches the whole-pane loading message every other section uses
	     (`.center-msg`, app.css). The empty states set their own larger type on
	     the `<p>` below, so this only lands on the bare "Loading…" line. */
    font-size: var(--text-sm);
    text-align: center;
  }
  .chat-empty p {
    margin: 0.2rem 0 0;
    color: var(--text-muted);
    font-size: var(--text-base);
  }
  .chat-empty .hint {
    font-size: var(--text-sm);
  }

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

  /* Cross-room view entries above the rooms list — styled like the feeds
	   sidebar's All / Unread / Starred buttons (.feed-btn.special). */
  /* .views / .view-btn / .view-name (the All / Unread / Starred block) come
	   from web/src/lib/styles/sidebar.css, shared with the feeds sidebar. */

  /* No horizontal padding: this sits inside .sidebar-list, which already
	   insets its children, so the button lines up with the rows around it. */
  .room-new {
    padding: 0 0 0.4rem;
  }
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
  .room-add:hover {
    color: var(--text-primary);
    border-color: var(--text-dim);
  }
  .room-input {
    width: 100%;
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    padding: 0.35rem 0.5rem;
    border-radius: 0.35rem;
  }

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
  .room-row:hover .room-btn {
    color: var(--text-secondary);
  }
  .room-row.active .room-btn {
    color: var(--text-primary);
  }
  /* Title + badge on one line. The origin glyph stays a sibling of this column
	   so it keeps its own fixed slot. */
  .room-text {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
  }
  .room-line {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    min-width: 0;
  }
  .room-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* A room with unseen bot/system messages reads bolder; the active room never
	   bolds (looking at it is reading it). */
  .room-name.unread {
    font-weight: 700;
    color: var(--text-primary);
  }
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
  .room-origin.talk {
    color: var(--accent-amber);
  }

  /* Jump-to-response: a brief pulse on the row a search result jumps to. The
	   class is toggled on the Message component's root (data-cid anchor), so the
	   rule is :global; it fades a soft accent wash under the row for ~2s. */
  :global(.jump-highlight) {
    animation: jump-pulse 2s ease-out;
    border-radius: var(--radius-card);
  }
  @keyframes jump-pulse {
    0% {
      background: color-mix(in srgb, var(--accent-amber) 26%, transparent);
    }
    100% {
      background: transparent;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    :global(.jump-highlight) {
      animation-duration: 0.01ms;
    }
  }
</style>
