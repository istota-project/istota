<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getFeedsConfig,
		putFeedsConfig,
		importOpml,
		exportOpmlUrl,
		type FeedsConfigPayload,
		type FeedsConfigFeed,
		type FeedsConfigCategory,
		type FeedsDiagnostics,
		type FeedsFeedState,
	} from '$lib/api';
	import { Button, Modal } from '$lib/components/ui';

	let loading = $state(true);
	let saving = $state(false);
	let importing = $state(false);
	let error = $state('');
	let info = $state('');

	let config: FeedsConfigPayload = $state({
		settings: {},
		categories: [],
		feeds: [],
	});
	let diagnostics: FeedsDiagnostics | null = $state(null);
	let feedStateByUrl: Record<string, FeedsFeedState> = $state({});

	// Track whether the loaded config has been mutated (dirty flag).
	let initialJson = $state('');
	let dirty = $derived(JSON.stringify(config) !== initialJson);

	type FeedDraft = {
		url: string;
		title: string;
		category: string;
		poll_interval_minutes: string; // raw input — empty = inherit default
	};
	let editing: { idx: number; draft: FeedDraft } | null = $state(null);
	let adding: FeedDraft | null = $state(null);
	let modalError = $state('');
	let confirmDelete: { kind: 'feed' | 'category'; idx: number; label: string } | null =
		$state(null);

	let fileInput: HTMLInputElement | undefined = $state();

	async function refresh() {
		loading = true;
		error = '';
		try {
			const data = await getFeedsConfig();
			config = data.config;
			diagnostics = data.diagnostics;
			feedStateByUrl = Object.fromEntries(
				data.feed_state.map((s) => [s.url, s]),
			);
			initialJson = JSON.stringify(config);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load settings';
		} finally {
			loading = false;
		}
	}

	onMount(refresh);

	async function save() {
		saving = true;
		error = '';
		info = '';
		try {
			const result = await putFeedsConfig(config);
			info = `Saved (${result.sync.feeds_added} added, ${result.sync.feeds_updated} updated, ${result.sync.categories_added} new categories).`;
			initialJson = JSON.stringify(config);
			// Pull diagnostics back so last_poll_at etc. update
			const data = await getFeedsConfig();
			diagnostics = data.diagnostics;
			feedStateByUrl = Object.fromEntries(
				data.feed_state.map((s) => [s.url, s]),
			);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	function detectSourceType(url: string): string {
		const u = url.trim().toLowerCase();
		if (u.startsWith('tumblr:')) return 'tumblr';
		if (u.startsWith('arena:')) return 'arena';
		if (u.startsWith('http://') || u.startsWith('https://')) return 'rss';
		return '?';
	}

	function defaultInterval(): number {
		return Number(config.settings.default_poll_interval_minutes) || 30;
	}

	function feedToDraft(feed: FeedsConfigFeed): FeedDraft {
		return {
			url: feed.url,
			title: feed.title ?? '',
			category: feed.category ?? '',
			poll_interval_minutes: feed.poll_interval_minutes
				? String(feed.poll_interval_minutes)
				: '',
		};
	}

	function draftToFeed(draft: FeedDraft): FeedsConfigFeed | string {
		const url = draft.url.trim();
		if (!url) return 'URL is required';
		const out: FeedsConfigFeed = { url };
		if (draft.title.trim()) out.title = draft.title.trim();
		if (draft.category.trim()) out.category = draft.category.trim();
		if (draft.poll_interval_minutes.trim()) {
			const n = Number(draft.poll_interval_minutes);
			if (!Number.isInteger(n) || n <= 0) {
				return 'Poll interval must be a positive integer';
			}
			out.poll_interval_minutes = n;
		}
		return out;
	}

	function startAdd() {
		modalError = '';
		adding = { url: '', title: '', category: '', poll_interval_minutes: '' };
	}

	function startEdit(idx: number) {
		modalError = '';
		editing = { idx, draft: feedToDraft(config.feeds[idx]) };
	}

	function cancelModal() {
		editing = null;
		adding = null;
		modalError = '';
	}

	function saveAdd() {
		if (!adding) return;
		const result = draftToFeed(adding);
		if (typeof result === 'string') {
			modalError = result;
			return;
		}
		// Reject duplicate URLs.
		if (config.feeds.some((f) => f.url === result.url)) {
			modalError = 'A subscription with that URL already exists.';
			return;
		}
		config.feeds = [...config.feeds, result];
		adding = null;
	}

	function saveEdit() {
		if (!editing) return;
		const result = draftToFeed(editing.draft);
		if (typeof result === 'string') {
			modalError = result;
			return;
		}
		const dup = config.feeds.findIndex(
			(f, i) => i !== editing!.idx && f.url === result.url,
		);
		if (dup >= 0) {
			modalError = 'A different subscription already uses that URL.';
			return;
		}
		config.feeds = config.feeds.map((f, i) => (i === editing!.idx ? result : f));
		editing = null;
	}

	function deleteFeed(idx: number) {
		const feed = config.feeds[idx];
		confirmDelete = {
			kind: 'feed',
			idx,
			label: feed.title || feed.url,
		};
	}

	function deleteCategory(idx: number) {
		const cat = config.categories[idx];
		confirmDelete = {
			kind: 'category',
			idx,
			label: cat.title || cat.slug,
		};
	}

	function performDelete() {
		if (!confirmDelete) return;
		if (confirmDelete.kind === 'feed') {
			config.feeds = config.feeds.filter((_, i) => i !== confirmDelete!.idx);
		} else {
			const slug = config.categories[confirmDelete.idx]?.slug;
			config.categories = config.categories.filter(
				(_, i) => i !== confirmDelete!.idx,
			);
			// Detach feeds from the deleted category (don't drop the feeds).
			if (slug) {
				config.feeds = config.feeds.map((f) =>
					f.category === slug ? { ...f, category: undefined } : f,
				);
			}
		}
		confirmDelete = null;
	}

	function addCategory() {
		const slug = prompt('New category slug (e.g. "blogs"):');
		if (!slug) return;
		const trimmed = slug.trim();
		if (!trimmed) return;
		if (config.categories.some((c) => c.slug === trimmed)) {
			error = `Category "${trimmed}" already exists.`;
			return;
		}
		config.categories = [
			...config.categories,
			{ slug: trimmed, title: trimmed },
		];
	}

	function moveCategory(idx: number, delta: number) {
		const target = idx + delta;
		if (target < 0 || target >= config.categories.length) return;
		const next = config.categories.slice();
		[next[idx], next[target]] = [next[target], next[idx]];
		config.categories = next;
	}

	async function onImport(e: Event) {
		const input = e.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file) return;
		importing = true;
		error = '';
		info = '';
		try {
			const result = await importOpml(file);
			info = `Imported: ${result.feeds_added} feeds added, ${result.feeds_updated} updated, ${result.categories_added} new categories, ${result.rewritten_bridger_urls} bridger URLs rewritten.`;
			await refresh();
		} catch (e) {
			error = e instanceof Error ? e.message : 'OPML import failed';
		} finally {
			importing = false;
		}
	}

	function categoryOptions(): string[] {
		return config.categories.map((c) => c.slug);
	}

	function formatDate(iso: string | null): string {
		if (!iso) return '—';
		try {
			return new Date(iso).toLocaleString();
		} catch {
			return iso;
		}
	}
</script>

<div class="settings">
	<header class="settings-header">
		<div>
			<h1>Feed settings</h1>
			<p class="hint">
				Edit subscriptions, categories, and poll defaults. Saving writes
				<code>FEEDS.toml</code> and resyncs the local SQLite.
			</p>
		</div>
		<div class="header-actions">
			{#if dirty}
				<span class="dirty-badge">Unsaved changes</span>
			{/if}
			<Button variant="primary" onclick={save} disabled={!dirty || saving}>
				{saving ? 'Saving…' : 'Save changes'}
			</Button>
		</div>
	</header>

	{#if error}
		<div class="banner error">{error}</div>
	{/if}
	{#if info}
		<div class="banner info">{info}</div>
	{/if}

	{#if loading}
		<div class="placeholder">Loading…</div>
	{:else}
		{#if diagnostics}
			<section class="card">
				<h2>Diagnostics</h2>
				<div class="diag-grid">
					<div class="diag">
						<span class="diag-value">{diagnostics.total_feeds}</span>
						<span class="diag-label">subscriptions</span>
					</div>
					<div class="diag">
						<span class="diag-value">{diagnostics.total_entries}</span>
						<span class="diag-label">entries stored</span>
					</div>
					<div class="diag">
						<span class="diag-value">{diagnostics.unread_entries}</span>
						<span class="diag-label">unread</span>
					</div>
					<div class="diag" class:bad={diagnostics.error_feeds > 0}>
						<span class="diag-value">{diagnostics.error_feeds}</span>
						<span class="diag-label">in error</span>
					</div>
					<div class="diag wide">
						<span class="diag-value">{formatDate(diagnostics.last_poll_at)}</span>
						<span class="diag-label">last poll</span>
					</div>
				</div>
			</section>
		{/if}

		<section class="card">
			<header class="section-header">
				<h2>Settings</h2>
			</header>
			<label class="field">
				<span>Default poll interval (minutes)</span>
				<input
					type="number"
					min="1"
					value={config.settings.default_poll_interval_minutes ?? ''}
					placeholder="30"
					oninput={(e) => {
						const v = (e.currentTarget as HTMLInputElement).value;
						const next = { ...config.settings };
						if (v === '') {
							delete next.default_poll_interval_minutes;
						} else {
							const n = Number(v);
							if (Number.isInteger(n) && n > 0) {
								next.default_poll_interval_minutes = n;
							}
						}
						config.settings = next;
					}}
				/>
			</label>
		</section>

		<section class="card">
			<header class="section-header">
				<h2>Categories ({config.categories.length})</h2>
				<Button variant="pill" size="sm" onclick={addCategory}>+ Add category</Button>
			</header>
			{#if config.categories.length === 0}
				<p class="empty">No categories yet. Categories group feeds in the sidebar.</p>
			{:else}
				<table class="grid">
					<thead>
						<tr>
							<th>Slug</th>
							<th>Title</th>
							<th class="num">Feeds</th>
							<th class="actions">Order</th>
							<th class="actions"></th>
						</tr>
					</thead>
					<tbody>
						{#each config.categories as cat, idx (cat.slug)}
							{@const count = config.feeds.filter((f) => f.category === cat.slug).length}
							<tr>
								<td><code>{cat.slug}</code></td>
								<td>
									<input
										type="text"
										value={cat.title ?? ''}
										placeholder={cat.slug}
										oninput={(e) => {
											config.categories = config.categories.map((c, i) =>
												i === idx
													? { ...c, title: (e.currentTarget as HTMLInputElement).value }
													: c,
											);
										}}
									/>
								</td>
								<td class="num">{count}</td>
								<td class="actions">
									<button
										class="icon-btn"
										title="Move up"
										onclick={() => moveCategory(idx, -1)}
										disabled={idx === 0}
										type="button">↑</button
									>
									<button
										class="icon-btn"
										title="Move down"
										onclick={() => moveCategory(idx, 1)}
										disabled={idx === config.categories.length - 1}
										type="button">↓</button
									>
								</td>
								<td class="actions">
									<button
										class="icon-btn danger"
										title="Delete category"
										onclick={() => deleteCategory(idx)}
										type="button">×</button
									>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</section>

		<section class="card">
			<header class="section-header">
				<h2>Subscriptions ({config.feeds.length})</h2>
				<Button variant="pill" size="sm" onclick={startAdd}>+ Add subscription</Button>
			</header>
			{#if config.feeds.length === 0}
				<p class="empty">
					No subscriptions yet. Add one above, or import an OPML file from
					Miniflux.
				</p>
			{:else}
				<table class="grid">
					<thead>
						<tr>
							<th class="type">Type</th>
							<th>Title</th>
							<th>URL</th>
							<th>Category</th>
							<th class="num">Interval</th>
							<th>Last fetch</th>
							<th class="actions"></th>
						</tr>
					</thead>
					<tbody>
						{#each config.feeds as feed, idx (feed.url + idx)}
							{@const state = feedStateByUrl[feed.url]}
							<tr class:has-error={state && state.error_count > 0}>
								<td class="type"><span class="type-pill">{detectSourceType(feed.url)}</span></td>
								<td>{feed.title || ''}</td>
								<td><code class="url">{feed.url}</code></td>
								<td>{feed.category || ''}</td>
								<td class="num">{feed.poll_interval_minutes ?? defaultInterval()}m</td>
								<td>
									{#if state}
										<div class="state">
											<span>{formatDate(state.last_fetched_at)}</span>
											{#if state.last_error}
												<span class="state-error" title={state.last_error}>
													{state.error_count}× err: {state.last_error.slice(0, 60)}
												</span>
											{/if}
										</div>
									{:else}
										<span class="muted">never</span>
									{/if}
								</td>
								<td class="actions">
									<button
										class="icon-btn"
										title="Edit"
										onclick={() => startEdit(idx)}
										type="button">✎</button
									>
									<button
										class="icon-btn danger"
										title="Delete"
										onclick={() => deleteFeed(idx)}
										type="button">×</button
									>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</section>

		<section class="card">
			<h2>OPML</h2>
			<p class="hint">
				Import subscriptions from a Miniflux export, or download the current
				list as OPML. Bridger URLs (<code>localhost:8900/tumblr/…</code>,
				<code>localhost:8900/arena/…</code>) are rewritten to <code>tumblr:</code>
				/ <code>arena:</code> on import.
			</p>
			<div class="opml-actions">
				<Button onclick={() => fileInput?.click()} disabled={importing}>
					{importing ? 'Importing…' : 'Import OPML…'}
				</Button>
				<a class="opml-link" href={exportOpmlUrl()} download="feeds.opml">
					Download OPML
				</a>
			</div>
			<input
				bind:this={fileInput}
				type="file"
				accept=".opml,.xml,text/x-opml,application/xml,text/xml"
				onchange={onImport}
				hidden
			/>
		</section>

		<section class="card">
			<h2>Tumblr API</h2>
			<p class="hint">
				The Tumblr API key for <code>tumblr:</code> sources is read from the
				<code>extra.tumblr_api_key</code> field on the user's
				<code>[[resources]] type = "feeds"</code> entry, with
				<code>TUMBLR_API_KEY</code> as a fallback. To rotate it, edit
				<code>config/users/&lt;user&gt;.toml</code> on the server. This UI
				doesn't store credentials.
			</p>
		</section>
	{/if}
</div>

{#if adding}
	<Modal
		open={true}
		title="Add subscription"
		onOpenChange={(o) => {
			if (!o) cancelModal();
		}}
	>
		<div class="modal-body">
			<label class="field">
				<span>URL</span>
				<input
					type="text"
					placeholder="https://example.com/feed.xml | tumblr:user | arena:slug"
					bind:value={adding.url}
				/>
			</label>
			<label class="field">
				<span>Title (optional)</span>
				<input type="text" bind:value={adding.title} />
			</label>
			<label class="field">
				<span>Category</span>
				<select bind:value={adding.category}>
					<option value="">(none)</option>
					{#each categoryOptions() as slug (slug)}
						<option value={slug}>{slug}</option>
					{/each}
				</select>
			</label>
			<label class="field">
				<span>Poll interval (minutes, optional)</span>
				<input
					type="number"
					min="1"
					placeholder={String(defaultInterval())}
					bind:value={adding.poll_interval_minutes}
				/>
			</label>
			{#if modalError}
				<div class="banner error">{modalError}</div>
			{/if}
		</div>
		{#snippet footer()}
			<Button variant="ghost" onclick={cancelModal}>Cancel</Button>
			<Button variant="primary" onclick={saveAdd}>Add</Button>
		{/snippet}
	</Modal>
{/if}

{#if editing}
	<Modal
		open={true}
		title="Edit subscription"
		onOpenChange={(o) => {
			if (!o) cancelModal();
		}}
	>
		<div class="modal-body">
			<label class="field">
				<span>URL</span>
				<input type="text" bind:value={editing.draft.url} />
			</label>
			<label class="field">
				<span>Title</span>
				<input type="text" bind:value={editing.draft.title} />
			</label>
			<label class="field">
				<span>Category</span>
				<select bind:value={editing.draft.category}>
					<option value="">(none)</option>
					{#each categoryOptions() as slug (slug)}
						<option value={slug}>{slug}</option>
					{/each}
				</select>
			</label>
			<label class="field">
				<span>Poll interval (minutes)</span>
				<input
					type="number"
					min="1"
					placeholder={String(defaultInterval())}
					bind:value={editing.draft.poll_interval_minutes}
				/>
			</label>
			{#if modalError}
				<div class="banner error">{modalError}</div>
			{/if}
		</div>
		{#snippet footer()}
			<Button variant="ghost" onclick={cancelModal}>Cancel</Button>
			<Button variant="primary" onclick={saveEdit}>Save</Button>
		{/snippet}
	</Modal>
{/if}

{#if confirmDelete}
	<Modal
		open={true}
		title="Delete {confirmDelete.kind}?"
		onOpenChange={(o) => {
			if (!o) confirmDelete = null;
		}}
	>
		<p>
			Remove <strong>{confirmDelete.label}</strong>?
			{#if confirmDelete.kind === 'category'}
				Subscriptions in this category will keep their entries but become
				uncategorised.
			{/if}
			Changes apply when you save.
		</p>
		{#snippet footer()}
			<Button variant="ghost" onclick={() => (confirmDelete = null)}>Cancel</Button>
			<Button variant="primary" onclick={performDelete}>Delete</Button>
		{/snippet}
	</Modal>
{/if}

<style>
	.settings {
		max-width: 980px;
		margin: 0 auto;
		padding: 1.5rem 1rem 4rem;
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.settings-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1rem;
		flex-wrap: wrap;
	}

	.settings-header h1 {
		margin: 0;
		font-size: var(--text-lg, 1.05rem);
		color: var(--text-primary);
	}

	.hint {
		margin: 0.25rem 0 0;
		font-size: var(--text-sm);
		color: var(--text-muted);
		max-width: 60ch;
	}

	.hint code,
	.url,
	code {
		background: var(--surface-raised);
		padding: 0 0.3rem;
		border-radius: 0.2rem;
		font-size: 0.8em;
	}

	.header-actions {
		display: flex;
		align-items: center;
		gap: 0.6rem;
	}

	.dirty-badge {
		font-size: var(--text-xs);
		color: #d6a000;
	}

	.banner {
		padding: 0.4rem 0.75rem;
		border-radius: var(--radius-card);
		font-size: var(--text-sm);
	}
	.banner.error {
		background: rgba(204, 102, 102, 0.15);
		color: #e88;
	}
	.banner.info {
		background: rgba(110, 184, 132, 0.15);
		color: #8d8;
	}

	.placeholder {
		color: var(--text-dim);
		padding: 2rem 0;
		text-align: center;
	}

	.card {
		background: var(--surface-card);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-card);
		padding: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.card h2 {
		margin: 0;
		font-size: var(--text-base);
		color: var(--text-primary);
	}

	.section-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.75rem;
	}

	.empty {
		font-size: var(--text-sm);
		color: var(--text-dim);
		margin: 0;
	}

	.diag-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
		gap: 0.5rem;
	}

	.diag {
		background: var(--surface-raised);
		border-radius: var(--radius-card);
		padding: 0.5rem 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}

	.diag.wide {
		grid-column: span 2;
	}

	.diag.bad .diag-value {
		color: #e88;
	}

	.diag-value {
		font-size: var(--text-base);
		font-weight: 600;
		color: var(--text-primary);
	}

	.diag-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: var(--text-sm);
	}

	.field span {
		color: var(--text-muted);
	}

	.field input,
	.field select {
		background: var(--surface-base);
		color: var(--text-primary);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		padding: 0.3rem 0.5rem;
		font: inherit;
		font-size: var(--text-sm);
	}

	.field input:focus,
	.field select:focus {
		outline: 1px solid var(--accent, #6c8ebf);
	}

	.grid {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--text-sm);
	}

	.grid th,
	.grid td {
		text-align: left;
		padding: 0.4rem 0.5rem;
		border-bottom: 1px solid var(--border-subtle);
		vertical-align: middle;
	}

	.grid th {
		color: var(--text-dim);
		font-weight: 500;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.grid tbody tr.has-error {
		background: rgba(204, 102, 102, 0.05);
	}

	.grid td.type,
	.grid th.type {
		width: 4.5rem;
	}

	.grid td.num,
	.grid th.num {
		text-align: right;
		white-space: nowrap;
	}

	.grid td.actions,
	.grid th.actions {
		text-align: right;
		width: 4.5rem;
		white-space: nowrap;
	}

	.grid input[type='text'] {
		width: 100%;
		background: transparent;
		color: var(--text-primary);
		border: 1px solid transparent;
		border-radius: 0.2rem;
		padding: 0.2rem 0.3rem;
		font: inherit;
		font-size: var(--text-sm);
	}

	.grid input[type='text']:focus,
	.grid input[type='text']:hover {
		border-color: var(--border-default);
		background: var(--surface-base);
		outline: none;
	}

	.type-pill {
		display: inline-block;
		font-size: var(--text-xs);
		padding: 0.05rem 0.4rem;
		border-radius: var(--radius-pill);
		background: var(--surface-raised);
		color: var(--text-muted);
	}

	.url {
		display: inline-block;
		max-width: 28ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		vertical-align: middle;
	}

	.state {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		font-size: var(--text-xs);
	}

	.state-error {
		color: #e88;
		max-width: 32ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.muted {
		color: var(--text-dim);
		font-size: var(--text-xs);
	}

	.icon-btn {
		background: transparent;
		border: none;
		color: var(--text-dim);
		cursor: pointer;
		padding: 0.1rem 0.35rem;
		border-radius: 0.2rem;
		font: inherit;
		font-size: var(--text-base);
		line-height: 1;
	}

	.icon-btn:hover:not(:disabled) {
		color: var(--text-primary);
		background: var(--surface-raised);
	}

	.icon-btn.danger:hover:not(:disabled) {
		color: #e88;
	}

	.icon-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	.opml-actions {
		display: flex;
		gap: 0.6rem;
		align-items: center;
		flex-wrap: wrap;
	}

	.opml-link {
		font-size: var(--text-sm);
		color: var(--text-muted);
		text-decoration: underline;
	}

	.opml-link:hover {
		color: var(--text-primary);
	}

	.modal-body {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
</style>
