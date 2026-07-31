<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import {
    adminLogStreamUrl,
    getAdminLogPage,
    getAdminLogSources,
    type AdminLogFilters,
    type AdminLogRecord,
    type AdminLogSource,
  } from '$lib/api';
  import { Button, Chip, Input, Select } from '$lib/components/ui';
  import { mergeRecords, trimBuffer } from '$lib/admin/logRecords';
  import { copyText } from '$lib/clipboard';
  import { notifyError } from '$lib/stores/notices';

  const PAGE = 200;

  const LEVELS = [
    { value: '', label: 'All levels' },
    { value: 'DEBUG', label: 'Debug and up' },
    { value: 'INFO', label: 'Info and up' },
    { value: 'WARNING', label: 'Warning and up' },
    { value: 'ERROR', label: 'Error and up' },
  ];

  let sources = $state<AdminLogSource[]>([]);
  let sourceId = $state('app');
  let records = $state<AdminLogRecord[]>([]);
  let loading = $state(true);
  let loadingOlder = $state(false);
  let error = $state('');
  let truncated = $state(false);
  let nextBefore = $state<string | null>(null);
  let tailCursor = $state<string | null>(null);

  // Filters. `applied` is what the last fetch used; the inputs are free to
  // diverge until Apply, so typing a search does not fire a request per
  // keystroke against a file scan.
  let level = $state('');
  let query = $state('');
  let loggerFilter = $state('');
  let userFilter = $state('');
  let taskFilter = $state<number | null>(null);
  let applied = $state<AdminLogFilters>({});

  let live = $state(false);
  let stream: EventSource | null = null;
  let streamRetries = 0;
  let streamRetryTimer: ReturnType<typeof setTimeout> | null = null;
  let logEl = $state<HTMLDivElement | null>(null);
  let atBottom = $state(true);

  const MAX_STREAM_RETRIES = 5;

  let activeSource = $derived(sources.find((s) => s.id === sourceId) ?? null);
  let sourceOptions = $derived(
    sources.map((s) => ({
      value: s.id,
      label: s.available ? s.label : `${s.label} (unavailable)`,
    })),
  );
  let isTaskSource = $derived(sourceId === 'tasks');
  // Compared against the filters the *current* source actually uses. A logger
  // prefix typed on the app source is not rendered on the tasks source, so
  // including it unconditionally would latch the button on "Apply" for the rest
  // of the session with no control able to clear it.
  let filtersDirty = $derived(
    (applied.level ?? '') !== level ||
      (applied.q ?? '') !== query.trim() ||
      (isTaskSource
        ? (applied.user_id ?? '') !== userFilter.trim()
        : (applied.logger ?? '') !== loggerFilter.trim()),
  );

  function currentFilters(): AdminLogFilters {
    const f: AdminLogFilters = {};
    if (level) f.level = level;
    if (query.trim()) f.q = query.trim();
    if (loggerFilter.trim() && !isTaskSource) f.logger = loggerFilter.trim();
    if (userFilter.trim() && isTaskSource) f.user_id = userFilter.trim();
    if (taskFilter !== null && isTaskSource) f.task_id = taskFilter;
    return f;
  }

  function filterByTask(id: number) {
    taskFilter = id;
    void load();
  }

  function clearTaskFilter() {
    taskFilter = null;
    void load();
  }

  function describeError(e: unknown): string {
    const msg = e instanceof Error ? e.message : String(e);
    // The endpoint 409s an unavailable source with its reason; surface the
    // source's own detail rather than a bare status, which reads as a bug.
    if (msg.includes('409') && activeSource?.detail) return activeSource.detail;
    return msg || 'Failed to load logs';
  }

  async function loadSources() {
    try {
      const resp = await getAdminLogSources();
      sources = resp.sources;
      if (!sources.some((s) => s.id === sourceId)) sourceId = sources[0]?.id ?? 'app';
    } catch (e) {
      error = describeError(e);
    }
  }

  // Monotonic request token. Two rapid Apply/Refresh/source clicks can resolve
  // out of order, which would otherwise pair one query's records with another
  // query's `applied` filters — or prepend an unfiltered older page onto a
  // filtered transcript.
  let requestSeq = 0;

  async function load({ keepLive = false } = {}) {
    if (!keepLive) stopStream();
    const seq = ++requestSeq;
    loading = true;
    error = '';
    applied = currentFilters();
    try {
      const page = await getAdminLogPage(sourceId, { ...applied, limit: PAGE });
      if (seq !== requestSeq) return;
      records = page.records;
      nextBefore = page.next_before;
      tailCursor = page.tail_cursor;
      truncated = page.truncated;
      await tick();
      scrollToBottom();
      if (live) startStream();
    } catch (e) {
      if (seq !== requestSeq) return;
      records = [];
      nextBefore = null;
      tailCursor = null;
      error = describeError(e);
      live = false;
    } finally {
      if (seq === requestSeq) loading = false;
    }
  }

  async function loadOlder() {
    if (!nextBefore || loadingOlder || loading) return;
    const seq = requestSeq;
    loadingOlder = true;
    // Capture the scroll offset, not just the height: the assignment below must
    // *overwrite* whatever the browser's own scroll anchoring did, not add to
    // it. Same shape as the chat transcript's history prepend.
    const prevTop = logEl?.scrollTop ?? 0;
    const prevHeight = logEl?.scrollHeight ?? 0;
    try {
      const page = await getAdminLogPage(sourceId, {
        ...applied,
        limit: PAGE,
        before: nextBefore,
      });
      // A filter change or source switch landed while this was in flight; its
      // records belong to a query the transcript no longer shows.
      if (seq !== requestSeq) return;
      records = [...page.records, ...records];
      nextBefore = page.next_before;
      truncated = page.truncated;
      await tick();
      // Hold the reading position: prepending shifts everything down by the
      // height of what was added, and losing your place is the one thing a log
      // reader must not do while you are walking backwards through it.
      if (logEl) logEl.scrollTop = logEl.scrollHeight - prevHeight + prevTop;
    } catch (e) {
      if (seq === requestSeq) notifyError(describeError(e), { key: 'admin-logs:older' });
    } finally {
      loadingOlder = false;
    }
  }

  function startStream() {
    stopStream();
    if (!tailCursor || !activeSource?.available) return;
    // Built from the *current* cursor every time, so a reconnect resumes where
    // the client actually is. The URL an EventSource retries is fixed at
    // construction, which is why reconnects are handled by opening a new one
    // rather than letting the browser retry this one.
    const es = new EventSource(adminLogStreamUrl(sourceId, tailCursor, applied), {
      withCredentials: true,
    });
    stream = es;

    es.addEventListener('records', (ev) => {
      let payload: { records: AdminLogRecord[]; cursor: string; reset: boolean };
      try {
        payload = JSON.parse((ev as MessageEvent).data);
      } catch {
        return;
      }
      tailCursor = payload.cursor;
      streamRetries = 0;
      // A reset means the live file rotated: the transcript no longer
      // continues, so replace rather than append.
      const merged = payload.reset ? payload.records : mergeRecords(records, payload.records);
      const { records: kept, trimmed } = trimBuffer(merged);
      records = kept;
      if (payload.reset || trimmed) {
        // Trimming drops the oldest rows, so `nextBefore` no longer abuts the
        // top of the buffer — paging older would leave an invisible hole.
        nextBefore = null;
      }
      void tick().then(() => {
        if (atBottom) scrollToBottom();
      });
    });

    es.addEventListener('reset', () => {
      // The cursor outlived its file. Re-seed from a fresh page.
      stopStream();
      void load({ keepLive: true });
    });

    // Named `stream_error` server-side: a frame named `error` would land on the
    // built-in connection-error listener below, where readyState is OPEN, and
    // be discarded unread.
    es.addEventListener('stream_error', () => {
      stopStream();
      live = false;
      notifyError('Live tail stopped: the log could not be read.', {
        key: 'admin-logs:stream',
      });
    });

    es.addEventListener('error', () => {
      // The browser's own retry would re-request the URL as built — i.e. from
      // the seed cursor — replaying everything since. Close it and re-open from
      // the cursor we have actually reached.
      es.close();
      if (stream !== es || !live) return;
      stream = null;
      streamRetries += 1;
      if (streamRetries > MAX_STREAM_RETRIES) {
        live = false;
        notifyError('Live tail disconnected.', { key: 'admin-logs:stream' });
        return;
      }
      const delay = Math.min(1000 * 2 ** (streamRetries - 1), 15_000);
      streamRetryTimer = setTimeout(() => {
        streamRetryTimer = null;
        if (live) startStream();
      }, delay);
    });
  }

  function stopStream() {
    if (streamRetryTimer !== null) {
      clearTimeout(streamRetryTimer);
      streamRetryTimer = null;
    }
    streamRetries = 0;
    stream?.close();
    stream = null;
  }

  function toggleLive() {
    live = !live;
    if (live) startStream();
    else stopStream();
  }

  function scrollToBottom() {
    if (logEl) logEl.scrollTop = logEl.scrollHeight;
  }

  function onScroll() {
    if (!logEl) return;
    atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  }

  function pickSource(id: string) {
    if (id === sourceId) return;
    sourceId = id;
    records = [];
    // A task id means nothing to the app-log source, and leaving it set would
    // show a chip the new source cannot honour.
    taskFilter = null;
    void load();
  }

  function levelClass(l: string): string {
    const upper = l.toUpperCase();
    if (upper === 'ERROR' || upper === 'CRITICAL' || upper === 'FATAL') return 'lvl-error';
    if (upper === 'WARNING' || upper === 'WARN') return 'lvl-warn';
    if (upper === 'DEBUG') return 'lvl-debug';
    return 'lvl-info';
  }

  function shortTime(ts: string | null): string {
    if (!ts) return '';
    // Deliberately not converted to the viewer's zone: the value is what the
    // server wrote, and shifting it makes a line un-greppable against the file
    // it came from. The source's `time_basis` is labelled in the toolbar.
    return ts.replace('T', ' ').replace(/\.\d+$/, '');
  }

  function copyAll() {
    const text = records
      .map((r) =>
        [
          shortTime(r.timestamp),
          r.level,
          r.logger ?? (r.task_id ? `task ${r.task_id}` : ''),
          r.message,
        ]
          .filter(Boolean)
          .join(' '),
      )
      .join('\n');
    void copyText(text, { label: `Copied ${records.length} lines` });
  }

  onMount(async () => {
    await loadSources();
    await load();
  });

  onDestroy(stopStream);
</script>

<div class="settings logs-page">
  <div class="log-toolbar">
    <Select
      value={sourceId}
      options={sourceOptions}
      onValueChange={pickSource}
      ariaLabel="Log source"
      size="md"
    />
    <Select
      value={level}
      options={LEVELS}
      onValueChange={(v) => (level = v)}
      ariaLabel="Minimum level"
      size="md"
    />
    <Input bind:value={query} placeholder="Search…" aria-label="Search log text" />
    {#if isTaskSource}
      <Input bind:value={userFilter} placeholder="User" aria-label="Filter by user" monospace />
    {:else}
      <Input
        bind:value={loggerFilter}
        placeholder="Logger prefix"
        aria-label="Filter by logger prefix"
        monospace
      />
    {/if}
    <Button onclick={() => load()} {loading} loadingLabel="Loading…">
      {filtersDirty ? 'Apply' : 'Refresh'}
    </Button>
    <Chip checked={live} onclick={toggleLive} title="Follow new entries as they arrive">
      {live ? 'Live' : 'Follow'}
    </Chip>
    {#if taskFilter !== null}
      <!-- Set by clicking a `task N` cell; there is no input for it, because
           picking a task off a row you are already reading is the way you
           actually arrive at one. -->
      <Chip checked onclick={clearTaskFilter} title="Clear the task filter">
        task {taskFilter} ×
      </Chip>
    {/if}
  </div>

  {#if activeSource}
    <p class="source-note">
      {activeSource.description}
      {#if activeSource.available}
        <span class="source-meta">
          {activeSource.time_basis === 'utc' ? 'Timestamps in UTC.' : 'Timestamps in server time.'}
          {#if activeSource.path}<code>{activeSource.path}</code>{/if}
        </span>
      {/if}
    </p>
  {/if}

  {#if error}
    <div class="banner error">{error}</div>
  {/if}

  {#if !error && activeSource && !activeSource.available}
    <div class="banner info">{activeSource.detail}</div>
  {/if}

  <div class="log-pane" bind:this={logEl} onscroll={onScroll}>
    {#if loading && records.length === 0}
      <div class="center-msg">Loading…</div>
    {:else if records.length === 0 && !error}
      <div class="center-msg">
        {applied.q || applied.level || applied.logger || applied.user_id
          ? 'No entries match these filters.'
          : 'No entries yet.'}
      </div>
    {:else}
      {#if nextBefore}
        <div class="older-row">
          <Button
            variant="subtle"
            size="sm"
            onclick={loadOlder}
            loading={loadingOlder}
            loadingLabel="Loading…">Load older</Button
          >
        </div>
      {:else if truncated}
        <p class="older-note">
          Stopped after scanning the size limit without a match. Narrow the search or pick a higher
          level.
        </p>
      {/if}

      {#each records as record (record.cursor)}
        <div class="log-row {levelClass(record.level)}">
          <span class="log-time">{shortTime(record.timestamp)}</span>
          <span class="log-level">{record.level}</span>
          <span class="log-origin">
            {#if record.task_id !== null}
              <button
                type="button"
                class="log-task"
                title="Show only this task"
                onclick={() => filterByTask(record.task_id as number)}
              >
                task {record.task_id}
              </button>
              {#if record.user_id}<span class="log-user">{record.user_id}</span>{/if}
            {:else if record.logger}
              {record.logger}
            {/if}
          </span>
          <span class="log-message">{record.message}</span>
        </div>
      {/each}
    {/if}
  </div>

  <div class="log-footer">
    <span class="log-count">{records.length} shown</span>
    {#if records.length > 0}
      <Button variant="subtle" size="sm" onclick={copyAll}>Copy</Button>
    {/if}
    {#if !atBottom && records.length > 0}
      <Button variant="subtle" size="sm" onclick={scrollToBottom}>Jump to latest</Button>
    {/if}
  </div>
</div>

<style>
  .logs-page {
    max-width: 1100px;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  .log-toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }

  /* Input stretches to its container by default, which puts each text field on
     a row of its own and turns a six-control toolbar into a column. Bounded
     here rather than in the component: full-width is right in a settings form.
     :global() because the subject is inside a component — scoped to the
     toolbar, so it is placement, not a leak. */
  .log-toolbar :global(input) {
    min-width: 9rem;
    max-width: 16rem;
  }

  .source-note {
    margin: 0 0 var(--space-3);
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .source-meta {
    display: block;
    margin-top: var(--space-1);
  }

  .source-meta code {
    font-family: var(--font-mono);
  }

  /* The reader itself: a fixed-height scroll pane rather than a growing page,
     so the toolbar and footer stay reachable with thousands of lines loaded. */
  .log-pane {
    flex: 1;
    min-height: 20rem;
    max-height: 70vh;
    overflow-y: auto;
    overflow-x: hidden;
    background: var(--surface-card);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--space-2);
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    line-height: 1.5;
  }

  .log-row {
    display: grid;
    grid-template-columns: auto auto minmax(0, 12rem) minmax(0, 1fr);
    gap: var(--space-2);
    /* No vertical padding: line-height already supplies the leading, and a log
       pane earns its keep by fitting lines on screen. */
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
  }

  .log-row:hover {
    background: var(--surface-raised);
  }

  .log-time {
    color: var(--text-dim);
    white-space: nowrap;
  }

  .log-level {
    /* Fixed width, or a WARNING row shifts every column after it (the same
       reason a status chip needs a min-width). */
    min-width: 4.5rem;
    font-weight: 600;
  }

  .lvl-error .log-level {
    color: var(--status-danger-fg);
  }

  .lvl-warn .log-level {
    color: var(--status-warn-fg);
  }

  .lvl-info .log-level {
    color: var(--status-info-fg);
  }

  .lvl-debug .log-level {
    color: var(--text-dim);
  }

  .log-origin {
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* A `task N` cell filters to that task. Styled as inherited text rather than
     a control: it sits inside a dense monospace grid where a button box would
     add a row of visual noise per line. */
  .log-task {
    background: none;
    border: none;
    padding: 0;
    font: inherit;
    color: var(--link);
    cursor: pointer;
  }

  .log-task:hover {
    text-decoration: underline;
  }

  .log-user {
    margin-left: var(--space-1);
    color: var(--text-dim);
  }

  /* Wrap rather than scroll horizontally: a traceback is the payload, and a
     pane that scrolls sideways hides the end of every line that matters. */
  .log-message {
    color: var(--text-primary);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .older-row {
    display: flex;
    justify-content: center;
    padding-bottom: var(--space-2);
  }

  .older-note {
    margin: 0 0 var(--space-2);
    text-align: center;
    color: var(--text-dim);
  }

  .log-footer {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }

  .log-count {
    flex: 1;
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  @media (max-width: 640px) {
    .log-row {
      grid-template-columns: minmax(0, 1fr);
      gap: 0;
      padding-bottom: var(--space-2);
    }

    .log-origin {
      font-size: var(--text-2xs);
    }
  }
</style>
