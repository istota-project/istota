<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		getMe,
		getBriefings,
		upsertBriefing,
		deleteBriefing,
		getBriefingConfig,
		putBriefingBlock,
		deleteBriefingBlock,
		putBriefingSource,
		deleteBriefingSource,
		getBrowsePresets,
		getFeedOptions,
		checkBriefingPath,
		getBriefingPathSuggestions,
		type UserBriefingRow,
		type BriefingBlock,
		type BriefingSource,
		type BriefingConfigResponse,
		type BrowsePreset,
		type FeedOptions,
	} from '$lib/api';
	import { Button, Modal, Select, AutocompleteInput, type SelectOption } from '$lib/components/ui';
	import { SettingsLayout, SettingsCard, SettingsField } from '$lib/components/settings';
	import { briefingsRefreshNonce } from '$lib/stores/briefings';

	let loading = $state(true);
	let error = $state('');
	let moduleEnabled = $state(true);

	// --- Schedule & delivery ---
	let briefings: UserBriefingRow[] = $state([]);
	let briefingOutputs: string[] = $state(['talk', 'email', 'ntfy', 'web']);
	let newBriefing = $state({
		name: '',
		cron: '0 7 * * *',
		conversation_token: '',
		output: 'talk' as string,
		enabled: true,
	});
	let briefingError = $state('');
	let briefingSaving = $state(false);

	const briefingOutputOptions: SelectOption[] = $derived(
		briefingOutputs.map((o) => ({ value: o, label: o })),
	);

	// --- Content blocks ---
	let config = $state<BriefingConfigResponse | null>(null);
	let presets = $state<BrowsePreset[]>([]);
	let feedOptions = $state<FeedOptions | null>(null);
	let selectedName = $state<string>('');
	let newBlockTitle = $state('');
	let expandedId = $state<number | null>(null);
	let addingSaving = $state(false);

	// The source currently open in the inline config editor (a draft copy — only
	// committed to the backend on Save).
	let sourceDraft = $state<{ id: number; kind: string; config: Record<string, unknown> } | null>(
		null,
	);
	let newSender = $state('');

	// File-path picker state for todos/reminders/notes sources. Suggestions are
	// fetched server-side as the user types (so deep/late files surface), and
	// existence is verified as an advisory hint — never a save blocker (the
	// resolver is fail-soft: a missing file just contributes a provenance note).
	let pathSuggestions = $state<string[]>([]);
	// '' idle | 'checking' | 'ok' | 'missing' — advisory only.
	let pathStatus = $state<'' | 'checking' | 'ok' | 'missing'>('');
	let pathResolved = $state('');
	// Debounce + last-write-wins guards for the two async calls on each keystroke.
	let pathDebounce: ReturnType<typeof setTimeout> | undefined;
	let suggestSeq = 0;
	let verifySeq = 0;

	// A unified delete confirmation across briefings / blocks / sources.
	let confirmDelete = $state<{
		kind: 'briefing' | 'block' | 'source';
		id: number;
		label: string;
	} | null>(null);

	const KIND_LABELS: Record<string, string> = {
		rss: 'RSS feed',
		email: 'Newsletters',
		browse: 'Web page',
		markets: 'Markets',
		calendar: 'Calendar',
		todos: 'Todos',
		reminders: 'Reminders',
		notes: 'Notes',
	};
	const FILE_PLACEHOLDER: Record<string, string> = {
		todos: 'shared/team-todo.md',
		reminders: 'istota/config/reminders.md',
		notes: 'istota/notes/agenda.md',
	};
	const RENDER_OPTIONS: SelectOption[] = [
		{ value: 'synthesis', label: 'Synthesis (summarize)' },
		{ value: 'structured', label: 'Structured (verbatim)' },
	];
	const EMAIL_MODE_OPTIONS: SelectOption[] = [
		{ value: 'shared', label: 'Shared newsletter pool' },
		{ value: 'senders', label: 'Selected senders' },
	];

	const sourceKinds = $derived(config?.source_kinds ?? Object.keys(KIND_LABELS));

	const allNames = $derived.by(() => {
		if (!config) return [] as string[];
		const set = new Set<string>();
		config.schedule_names.forEach((n) => set.add(n));
		config.briefings.forEach((b) => set.add(b.name));
		return [...set];
	});

	const currentBlocks = $derived.by(() => {
		if (!config) return [] as BriefingBlock[];
		return config.briefings.find((b) => b.name === selectedName)?.blocks ?? [];
	});

	const rssOptions: SelectOption[] = $derived([
		...(feedOptions?.categories ?? []).map((c) => ({
			value: `category:${c.value}`,
			label: `Category: ${c.label}`,
		})),
		...(feedOptions?.subscriptions ?? []).map((s) => ({
			value: `subscription:${s.value}`,
			label: `Feed: ${s.label}`,
		})),
	]);
	const browseOptions: SelectOption[] = $derived([
		{ value: '__custom__', label: 'Custom URL…' },
		...presets.map((p) => ({ value: `preset:${p.key}`, label: p.name })),
	]);

	async function reloadSchedule() {
		const resp = await getBriefings();
		briefings = resp.briefings;
		briefingOutputs = resp.outputs?.length ? resp.outputs : ['talk', 'email', 'ntfy', 'web'];
	}

	async function reloadContent() {
		config = await getBriefingConfig();
		if (!selectedName && allNames.length) selectedName = allNames[0];
	}

	onMount(async () => {
		try {
			const me = await getMe();
			moduleEnabled = me.features.briefings;
			if (!moduleEnabled) return;
			await Promise.all([
				reloadSchedule(),
				(async () => {
					[presets, feedOptions] = await Promise.all([
						getBrowsePresets().then((r) => r.presets),
						getFeedOptions(),
					]);
					await reloadContent();
				})(),
				getBriefingPathSuggestions()
					.then((r) => (pathSuggestions = r.paths))
					.catch(() => (pathSuggestions = [])),
			]);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load settings';
		} finally {
			loading = false;
		}
	});

	// ---- Schedule handlers ----
	async function submitBriefing(e: SubmitEvent) {
		e.preventDefault();
		briefingError = '';
		const name = newBriefing.name.trim();
		const cron = newBriefing.cron.trim();
		if (!name || !cron) {
			briefingError = 'Name and cron are required.';
			return;
		}
		if (newBriefing.output === 'talk' && !newBriefing.conversation_token.trim()) {
			briefingError = `Conversation token is required when output is "${newBriefing.output}".`;
			return;
		}
		briefingSaving = true;
		try {
			await upsertBriefing({
				name,
				cron,
				conversation_token: newBriefing.conversation_token.trim() || undefined,
				output: newBriefing.output,
				enabled: newBriefing.enabled,
			});
			newBriefing = {
				name: '',
				cron: '0 7 * * *',
				conversation_token: '',
				output: 'talk',
				enabled: true,
			};
			await Promise.all([reloadSchedule(), reloadContent()]);
			briefingsRefreshNonce.update((n) => n + 1);
		} catch (e) {
			briefingError = (e as Error).message || 'Save failed';
		} finally {
			briefingSaving = false;
		}
	}

	// ---- Block handlers ----
	async function addBlock() {
		const title = newBlockTitle.trim();
		if (!title || !selectedName) return;
		addingSaving = true;
		try {
			const resp = await putBriefingBlock({ briefing_name: selectedName, title });
			newBlockTitle = '';
			await reloadContent();
			if (resp.block?.id) expandedId = resp.block.id;
		} finally {
			addingSaving = false;
		}
	}

	function toggleExpand(block: BriefingBlock) {
		expandedId = expandedId === block.id ? null : block.id;
		sourceDraft = null;
	}

	async function updateBlock(block: BriefingBlock, patch: Record<string, unknown>) {
		await putBriefingBlock({ id: block.id, ...patch });
		await reloadContent();
	}

	function askRemoveBlock(block: BriefingBlock) {
		confirmDelete = { kind: 'block', id: block.id, label: block.title };
	}

	async function move(block: BriefingBlock, dir: -1 | 1) {
		const ids = currentBlocks.map((b) => b.id);
		const idx = ids.indexOf(block.id);
		const swap = idx + dir;
		if (swap < 0 || swap >= ids.length) return;
		[ids[idx], ids[swap]] = [ids[swap], ids[idx]];
		await putBriefingBlock({ reorder: { briefing_name: selectedName, ordered_ids: ids } });
		await reloadContent();
	}

	// ---- Source handlers ----
	async function addSource(block: BriefingBlock, kind: string) {
		const cfg: Record<string, unknown> = kind === 'email' ? { mode: 'shared' } : {};
		const resp = await putBriefingSource({ block_id: block.id, kind, config: cfg });
		await reloadContent();
		newSender = '';
		if (resp.id) sourceDraft = { id: resp.id, kind, config: cfg };
		resetPathState();
	}

	function startEditSource(source: BriefingSource) {
		sourceDraft = {
			id: source.id,
			kind: source.kind,
			// JSON round-trip deep-clones and strips the reactive $state proxy
			// (structuredClone rejects proxies); source config is plain JSON.
			config: JSON.parse(JSON.stringify(source.config ?? {})),
		};
		newSender = '';
		resetPathState();
		// Verify the existing path up front so the hint reflects reality on open.
		if (FILE_KINDS.includes(source.kind)) {
			const p = String(sourceDraft.config.path ?? '').trim();
			if (p) refreshPathHints(p);
		}
	}

	function cancelSource() {
		sourceDraft = null;
		newSender = '';
		resetPathState();
	}

	const FILE_KINDS = ['todos', 'reminders', 'notes'];

	function resetPathState() {
		clearTimeout(pathDebounce);
		pathDebounce = undefined;
		suggestSeq += 1;
		verifySeq += 1;
		pathStatus = '';
		pathResolved = '';
	}

	// Fetch matching suggestions + verify existence for the current path. Both
	// are advisory (verification never blocks the save) and both are guarded by
	// a per-call sequence so a slower earlier response can't clobber a newer one.
	async function refreshPathHints(path: string) {
		const query = path.trim();

		const sSeq = (suggestSeq += 1);
		getBriefingPathSuggestions(query)
			.then((r) => {
				if (sSeq === suggestSeq) pathSuggestions = r.paths;
			})
			.catch(() => {});

		const vSeq = (verifySeq += 1);
		if (!query) {
			pathStatus = '';
			pathResolved = '';
			return;
		}
		pathStatus = 'checking';
		try {
			const res = await checkBriefingPath(query);
			if (vSeq !== verifySeq) return; // superseded by a later keystroke
			pathStatus = res.ok ? 'ok' : 'missing';
			pathResolved = res.ok ? res.resolved ?? '' : '';
		} catch {
			// A transient verification failure is not the user's problem — leave
			// the hint idle rather than showing a scary error or blocking save.
			if (vSeq === verifySeq) {
				pathStatus = '';
				pathResolved = '';
			}
		}
	}

	// Debounced input handler for the file-path field.
	function onPathInput(v: string) {
		setDraftConfig({ path: v.trim() || undefined });
		clearTimeout(pathDebounce);
		pathDebounce = setTimeout(() => refreshPathHints(v), 150);
	}

	async function saveSource() {
		if (!sourceDraft) return;
		// Verification is advisory — the resolver is fail-soft (a missing file
		// contributes a provenance note, never an error), so a not-yet-created
		// or transiently-unreachable path must not trap the user. Save always
		// proceeds; the inline hint tells them whether it currently resolves.
		await putBriefingSource({ id: sourceDraft.id, config: sourceDraft.config });
		sourceDraft = null;
		newSender = '';
		resetPathState();
		await reloadContent();
	}

	async function toggleSource(source: BriefingSource, enabled: boolean) {
		await putBriefingSource({ id: source.id, enabled });
		await reloadContent();
	}

	function askRemoveSource(source: BriefingSource) {
		confirmDelete = { kind: 'source', id: source.id, label: KIND_LABELS[source.kind] ?? source.kind };
	}

	async function performDelete() {
		if (!confirmDelete) return;
		const target = confirmDelete;
		confirmDelete = null;
		try {
			if (target.kind === 'briefing') {
				await deleteBriefing(target.id);
				await Promise.all([reloadSchedule(), reloadContent()]);
				briefingsRefreshNonce.update((n) => n + 1);
			} else if (target.kind === 'block') {
				await deleteBriefingBlock(target.id);
				if (expandedId === target.id) expandedId = null;
				await reloadContent();
			} else {
				await deleteBriefingSource(target.id);
				if (sourceDraft?.id === target.id) sourceDraft = null;
				await reloadContent();
			}
		} catch (e) {
			error = (e as Error).message || 'Delete failed';
		}
	}

	// ---- Draft config helpers ----
	function setDraftConfig(patch: Record<string, unknown>) {
		if (!sourceDraft) return;
		const next = { ...sourceDraft.config, ...patch };
		for (const [k, v] of Object.entries(patch)) {
			if (v === undefined || v === null) delete next[k];
		}
		sourceDraft = { ...sourceDraft, config: next };
	}

	function setDraftNumber(field: string, raw: string) {
		const t = raw.trim();
		if (t === '') {
			setDraftConfig({ [field]: undefined });
			return;
		}
		const n = Number(t);
		if (Number.isFinite(n)) setDraftConfig({ [field]: n });
	}

	function draftSenders(): string[] {
		return (sourceDraft?.config.senders as string[]) ?? [];
	}

	function addSender() {
		const v = newSender.trim();
		if (!v) return;
		const cur = draftSenders();
		if (!cur.includes(v)) setDraftConfig({ senders: [...cur, v] });
		newSender = '';
	}

	function removeSender(v: string) {
		setDraftConfig({ senders: draftSenders().filter((s) => s !== v) });
	}

	// ---- Summaries / labels ----
	function feedRefLabel(ref: { kind: string; value: number }): string {
		const list = ref.kind === 'category' ? feedOptions?.categories : feedOptions?.subscriptions;
		const hit = list?.find((o) => o.value === ref.value);
		const prefix = ref.kind === 'category' ? 'Category' : 'Feed';
		return hit ? `${prefix}: ${hit.label}` : `${prefix} #${ref.value}`;
	}

	function rssDraftValue(): string {
		const ref = sourceDraft?.config.feed_ref as { kind: string; value: number } | undefined;
		return ref ? `${ref.kind}:${ref.value}` : '';
	}

	function browseDraftValue(): string {
		const preset = sourceDraft?.config.preset;
		return preset ? `preset:${preset}` : '__custom__';
	}

	function sourceSummary(s: BriefingSource): string {
		const c = s.config ?? {};
		switch (s.kind) {
			case 'email':
				return c.mode === 'senders'
					? `Senders: ${((c.senders as string[]) ?? []).join(', ') || '(none set)'}`
					: 'Shared newsletter pool';
			case 'rss': {
				const ref = c.feed_ref as { kind: string; value: number } | undefined;
				return ref ? feedRefLabel(ref) : 'No feed selected';
			}
			case 'browse':
				if (c.preset) return presets.find((p) => p.key === c.preset)?.name ?? String(c.preset);
				if (c.url) return String(c.url);
				return 'No page set';
			case 'markets': {
				const parts = [c.indices, c.futures].filter((x) => Array.isArray(x) && x.length);
				return parts.length ? 'Custom tickers' : 'Default indices & futures';
			}
			case 'calendar':
				return 'Your connected calendars';
			case 'todos':
			case 'reminders':
			case 'notes':
				return c.path ? String(c.path) : 'No path set';
			default:
				return '';
		}
	}

	const addSourceOptions: SelectOption[] = $derived(
		sourceKinds.map((k) => ({ value: k, label: KIND_LABELS[k] ?? k })),
	);
</script>

<svelte:head>
	<title>Briefings settings</title>
</svelte:head>

<SettingsLayout
	title="Briefings settings"
	description="Schedule and deliver briefings, and shape their content blocks. A briefing runs on its cron in your timezone and is synthesized from the blocks below."
	{loading}
	{error}
>
	{#if !moduleEnabled}
		<div class="banner info">
			Briefings module is disabled. Enable it in
			<a href="{base}/settings">Settings → Preferences</a> to manage schedules and content.
		</div>
	{:else}
		<SettingsCard
			title="Schedule &amp; delivery ({briefings.length})"
			description="Cron-scheduled summaries posted to a Talk room or sent by email. Operator-managed entries (from config.toml) are read-only here."
		>
			{#if briefings.length === 0}
				<p class="empty">No briefings scheduled yet.</p>
			{:else}
				<div class="table-scroll">
					<table class="grid">
						<thead>
							<tr>
								<th class="col-name">Name</th>
								<th>Cron</th>
								<th>Output</th>
								<th>Token</th>
								<th class="col-source">Source</th>
								<th class="actions"></th>
							</tr>
						</thead>
						<tbody>
							{#each briefings as b (`${b.managed}-${b.id ?? b.name}`)}
								<tr>
									<td class="col-name">
										{b.name}
										{#if !b.enabled}<span class="muted"> (disabled)</span>{/if}
									</td>
									<td><code>{b.cron}</code></td>
									<td>{b.output}</td>
									<td class="muted"><code>{b.conversation_token || '—'}</code></td>
									<td class="col-source muted">
										{b.managed === 'config' ? 'config.toml' : 'user'}
									</td>
									<td class="actions">
										{#if b.managed === 'db' && b.id !== undefined}
											<button
												class="icon-btn danger"
												title="Remove"
												type="button"
												onclick={() =>
													(confirmDelete = { kind: 'briefing', id: b.id!, label: b.name })}>×</button
											>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}

			<form class="add-form" onsubmit={submitBriefing}>
				<h3>Add briefing</h3>
				<div class="add-grid">
					<SettingsField label="Name">
						<input type="text" placeholder="morning" bind:value={newBriefing.name} />
					</SettingsField>
					<SettingsField label="Cron (user TZ)">
						<input type="text" placeholder="0 7 * * 1-5" bind:value={newBriefing.cron} />
					</SettingsField>
					<SettingsField label="Output">
						<Select
							value={newBriefing.output}
							options={briefingOutputOptions}
							onValueChange={(v) => (newBriefing.output = v)}
							ariaLabel="Output"
							fullWidth
						/>
					</SettingsField>
					<SettingsField label="Conversation token">
						<input
							type="text"
							placeholder="Talk room token"
							bind:value={newBriefing.conversation_token}
						/>
					</SettingsField>
					<SettingsField label="Enabled" checkbox>
						<input type="checkbox" bind:checked={newBriefing.enabled} />
					</SettingsField>
				</div>
				<div class="add-actions">
					<Button variant="primary" size="sm" type="submit" disabled={briefingSaving}>
						{briefingSaving ? 'Saving…' : 'Add briefing'}
					</Button>
				</div>
				{#if briefingError}
					<div class="banner error">{briefingError}</div>
				{/if}
			</form>
		</SettingsCard>

		<SettingsCard
			title="Content blocks"
			description="Blocks become the sections of your briefing, in order. Each block has a directive and one or more sources. Click a block to edit it."
		>
			{#if config}
				<div class="briefing-pick">
					<SettingsField label="Briefing">
						<Select
							value={selectedName}
							options={allNames.map((n) => ({ value: n, label: n }))}
							onValueChange={(v) => {
								selectedName = v;
								expandedId = null;
								sourceDraft = null;
							}}
							ariaLabel="Briefing"
						/>
					</SettingsField>
					{#if allNames.length === 0}
						<span class="muted">No briefings scheduled yet — add one above first.</span>
					{/if}
				</div>

				{#if selectedName}
					{#if currentBlocks.length === 0}
						<p class="empty">No blocks yet. Add one below to start shaping this briefing.</p>
					{:else}
						<div class="table-scroll">
							<table class="grid">
								<thead>
									<tr>
										<th class="col-order">Order</th>
										<th>Block</th>
										<th class="col-render">Render</th>
										<th class="col-src">Sources</th>
										<th class="actions"></th>
									</tr>
								</thead>
								<tbody>
									{#each currentBlocks as block, idx (block.id)}
										<tr class:expanded={expandedId === block.id}>
											<td class="col-order actions">
												<button
													class="icon-btn"
													title="Move up"
													type="button"
													disabled={idx === 0}
													onclick={() => move(block, -1)}>↑</button
												>
												<button
													class="icon-btn"
													title="Move down"
													type="button"
													disabled={idx === currentBlocks.length - 1}
													onclick={() => move(block, 1)}>↓</button
												>
											</td>
											<td>
												<button class="block-name" type="button" onclick={() => toggleExpand(block)}>
													<span class="chevron">{expandedId === block.id ? '▾' : '▸'}</span>
													<span class="block-title-text">{block.title}</span>
												</button>
												{#if block.directive}
													<div class="block-directive muted">{block.directive}</div>
												{/if}
											</td>
											<td class="col-render">
												<span class="render-pill">{block.render_mode}</span>
											</td>
											<td class="col-src">
												{#if block.sources.length}
													<div class="kind-pills">
														{#each block.sources as s (s.id)}
															<span class="kind-pill" class:off={!s.enabled}>{s.kind}</span>
														{/each}
													</div>
												{:else}
													<span class="muted">none</span>
												{/if}
											</td>
											<td class="actions">
												<button
													class="icon-btn"
													title={expandedId === block.id ? 'Collapse' : 'Edit'}
													type="button"
													onclick={() => toggleExpand(block)}>✎</button
												>
												<button
													class="icon-btn danger"
													title="Delete block"
													type="button"
													onclick={() => askRemoveBlock(block)}>×</button
												>
											</td>
										</tr>
										{#if expandedId === block.id}
											<tr class="detail-row">
												<td colspan="5">
													<div class="block-detail">
														<div class="detail-grid">
															<SettingsField label="Title">
																<input
																	type="text"
																	value={block.title}
																	onchange={(e) =>
																		updateBlock(block, {
																			title: (e.target as HTMLInputElement).value,
																		})}
																/>
															</SettingsField>
															<SettingsField label="Render mode">
																<Select
																	value={block.render_mode}
																	options={RENDER_OPTIONS}
																	onValueChange={(v) => updateBlock(block, { render_mode: v })}
																	ariaLabel="Render mode"
																	fullWidth
																/>
															</SettingsField>
															<SettingsField
																label="Directive"
																wide
																hint="How the model should treat this block's sources."
															>
																<textarea
																	rows="2"
																	value={block.directive}
																	placeholder="e.g. 3–5 stories, neutral tone"
																	onchange={(e) =>
																		updateBlock(block, {
																			directive: (e.target as HTMLTextAreaElement).value,
																		})}
																></textarea>
															</SettingsField>
														</div>

														<div class="sources-block">
															<div class="sources-head">
																<h4>Sources</h4>
																<Select
																	value=""
																	options={addSourceOptions}
																	placeholder="+ Add source…"
																	onValueChange={(v) => {
																		if (v) addSource(block, v);
																	}}
																	ariaLabel="Add source"
																/>
															</div>

															{#if block.sources.length === 0}
																<p class="empty small">No sources yet — add one above.</p>
															{:else}
																<table class="grid sub">
																	<tbody>
																		{#each block.sources as source (source.id)}
																			{#if sourceDraft?.id === source.id}
																				<tr class="src-form-row">
																					<td colspan="4" class="src-form-cell">
																						<div class="source-form">
																							<div class="source-form-head">
																								<span class="kind-pill">{source.kind}</span>
																								<span class="kind-name"
																									>{KIND_LABELS[source.kind] ?? source.kind}</span
																								>
																								<span class="source-form-editing">Editing</span>
																							</div>

																							{#if source.kind === 'email'}
																								<SettingsField label="Mode">
																									<Select
																										value={(sourceDraft.config.mode as string) ??
																											'shared'}
																										options={EMAIL_MODE_OPTIONS}
																										onValueChange={(v) => setDraftConfig({ mode: v })}
																										ariaLabel="Email mode"
																										fullWidth
																									/>
																								</SettingsField>
																								{#if sourceDraft.config.mode === 'senders'}
																									<SettingsField
																										label="Sender filters"
																										hint="fnmatch patterns, e.g. *@semafor.com or news@axios.com"
																									>
																										<div class="chip-list">
																											{#each draftSenders() as s (s)}
																												<span class="chip">
																													{s}
																													<button
																														type="button"
																														title="Remove"
																														onclick={() => removeSender(s)}>×</button
																													>
																												</span>
																											{/each}
																											{#if draftSenders().length === 0}
																												<span class="muted small">No senders yet.</span>
																											{/if}
																										</div>
																										<div class="chip-add">
																											<input
																												type="text"
																												placeholder="*@example.com"
																												bind:value={newSender}
																												onkeydown={(e) => {
																													if (e.key === 'Enter') {
																														e.preventDefault();
																														addSender();
																													}
																												}}
																											/>
																											<Button
																												variant="secondary"
																												size="sm"
																												onclick={addSender}
																												disabled={!newSender.trim()}>Add</Button
																											>
																										</div>
																									</SettingsField>
																								{/if}
																								<SettingsField
																									label="Lookback (hours)"
																									hint="Leave blank to use the briefing default."
																								>
																									<input
																										type="number"
																										min="1"
																										value={(sourceDraft.config.lookback_hours as number) ??
																											''}
																										placeholder="default"
																										oninput={(e) =>
																											setDraftNumber(
																												'lookback_hours',
																												(e.target as HTMLInputElement).value,
																											)}
																									/>
																								</SettingsField>
																							{:else if source.kind === 'rss'}
																								<SettingsField label="Feed or category">
																									<Select
																										value={rssDraftValue()}
																										options={rssOptions}
																										placeholder="Pick a feed / category…"
																										onValueChange={(v) => {
																											const [kind, value] = v.split(':');
																											setDraftConfig({
																												feed_ref: { kind, value: Number(value) },
																											});
																										}}
																										ariaLabel="Feed or category"
																										fullWidth
																									/>
																								</SettingsField>
																								{#if rssOptions.length === 0}
																									<p class="muted small">
																										No feeds available — subscribe in the Feeds module first.
																									</p>
																								{/if}
																								<div class="inline-fields">
																									<SettingsField label="Max entries">
																										<input
																											type="number"
																											min="1"
																											value={(sourceDraft.config.limit as number) ?? ''}
																											placeholder="10"
																											oninput={(e) =>
																												setDraftNumber(
																													'limit',
																													(e.target as HTMLInputElement).value,
																												)}
																										/>
																									</SettingsField>
																									<SettingsField label="Unread only" checkbox>
																										<input
																											type="checkbox"
																											checked={!!sourceDraft.config.unread_only}
																											onchange={(e) =>
																												setDraftConfig({
																													unread_only: (e.target as HTMLInputElement)
																														.checked,
																												})}
																										/>
																									</SettingsField>
																								</div>
																							{:else if source.kind === 'browse'}
																								<SettingsField label="Source">
																									<Select
																										value={browseDraftValue()}
																										options={browseOptions}
																										onValueChange={(v) => {
																											if (v === '__custom__') {
																												setDraftConfig({
																													preset: undefined,
																													url:
																														(sourceDraft?.config.url as string) ?? '',
																												});
																											} else {
																												setDraftConfig({
																													preset: v.split(':')[1],
																													url: undefined,
																												});
																											}
																										}}
																										ariaLabel="Browse source"
																										fullWidth
																									/>
																								</SettingsField>
																								{#if !sourceDraft.config.preset}
																									<SettingsField label="URL">
																										<input
																											type="text"
																											placeholder="https://…"
																											value={(sourceDraft.config.url as string) ?? ''}
																											oninput={(e) =>
																												setDraftConfig({
																													url: (e.target as HTMLInputElement).value,
																												})}
																										/>
																									</SettingsField>
																								{/if}
																							{:else if source.kind === 'markets'}
																								<p class="muted small">
																									Leave blank for the default index & futures set.
																								</p>
																								<SettingsField
																									label="Indices"
																									hint="Comma-separated symbols, e.g. ^GSPC, ^IXIC"
																								>
																									<input
																										type="text"
																										value={((sourceDraft.config.indices as string[]) ??
																											[]).join(', ')}
																										placeholder="default"
																										oninput={(e) =>
																											setDraftConfig({
																												indices: (e.target as HTMLInputElement).value
																													.split(',')
																													.map((x) => x.trim())
																													.filter(Boolean),
																											})}
																									/>
																								</SettingsField>
																								<SettingsField
																									label="Futures"
																									hint="Comma-separated symbols."
																								>
																									<input
																										type="text"
																										value={((sourceDraft.config.futures as string[]) ??
																											[]).join(', ')}
																										placeholder="default"
																										oninput={(e) =>
																											setDraftConfig({
																												futures: (e.target as HTMLInputElement).value
																													.split(',')
																													.map((x) => x.trim())
																													.filter(Boolean),
																											})}
																									/>
																								</SettingsField>
																							{:else if source.kind === 'calendar'}
																								<p class="muted small">
																									Pulls from your connected calendars. No configuration
																									needed.
																								</p>
																							{:else if source.kind === 'todos' || source.kind === 'reminders' || source.kind === 'notes'}
																								<SettingsField
																									label="File path"
																									hint="Relative to your user folder. Use shared/… for a file shared with the bot, or istota/… for the bot's workspace. Required — no default."
																								>
																									<AutocompleteInput
																										value={(sourceDraft.config.path as string) ?? ''}
																										options={pathSuggestions}
																										placeholder={FILE_PLACEHOLDER[source.kind]}
																										invalid={pathStatus === 'missing'}
																										monospace
																										ariaLabel="File path"
																										filter={(opts) => opts}
																										onValueChange={onPathInput}
																									/>
																									{#if pathStatus === 'checking'}
																										<p class="path-msg muted">Checking…</p>
																									{:else if pathStatus === 'ok'}
																										<p class="path-msg path-ok">✓ Resolves to {pathResolved}</p>
																									{:else if pathStatus === 'missing'}
																										<p class="path-msg path-warn">
																											No file there yet — this source is skipped until the file exists.
																										</p>
																									{/if}
																								</SettingsField>
																							{/if}

																							<div class="source-form-actions">
																								<Button variant="ghost" size="sm" onclick={cancelSource}
																									>Cancel</Button
																								>
																								<Button variant="primary" size="sm" onclick={saveSource}
																									>Save source</Button
																								>
																							</div>
																						</div>
																					</td>
																				</tr>
																			{:else}
																				<tr>
																					<td class="col-kind">
																						<span class="kind-pill" class:off={!source.enabled}
																							>{source.kind}</span
																						>
																					</td>
																					<td class="src-summary muted">{sourceSummary(source)}</td>
																					<td class="col-toggle">
																						<label class="toggle">
																							<input
																								type="checkbox"
																								checked={source.enabled}
																								onchange={(e) =>
																									toggleSource(
																										source,
																										(e.target as HTMLInputElement).checked,
																									)}
																							/>
																							on
																						</label>
																					</td>
																					<td class="actions">
																						<button
																							class="icon-btn"
																							title="Edit source"
																							type="button"
																							onclick={() => startEditSource(source)}>✎</button
																						>
																						<button
																							class="icon-btn danger"
																							title="Remove source"
																							type="button"
																							onclick={() => askRemoveSource(source)}>×</button
																						>
																					</td>
																				</tr>
																			{/if}
																		{/each}
																	</tbody>
																</table>
															{/if}
														</div>
													</div>
												</td>
											</tr>
										{/if}
									{/each}
								</tbody>
							</table>
						</div>
					{/if}

					<div class="add-block">
						<input
							placeholder="New block title (e.g. World News)"
							bind:value={newBlockTitle}
							onkeydown={(e) => {
								if (e.key === 'Enter') {
									e.preventDefault();
									addBlock();
								}
							}}
						/>
						<Button
							variant="secondary"
							size="sm"
							onclick={addBlock}
							disabled={!newBlockTitle.trim() || addingSaving}
						>
							{addingSaving ? 'Adding…' : 'Add block'}
						</Button>
					</div>
				{/if}
			{/if}
		</SettingsCard>
	{/if}
</SettingsLayout>

{#if confirmDelete}
	<Modal
		open={true}
		title={confirmDelete.kind === 'briefing'
			? 'Remove briefing?'
			: confirmDelete.kind === 'block'
				? 'Delete block?'
				: 'Remove source?'}
		onOpenChange={(o) => {
			if (!o) confirmDelete = null;
		}}
	>
		<p>Remove <strong>{confirmDelete.label}</strong>?</p>
		{#snippet footer()}
			<Button variant="ghost" onclick={() => (confirmDelete = null)}>Cancel</Button>
			<Button variant="primary" onclick={performDelete}>Remove</Button>
		{/snippet}
	</Modal>
{/if}

<style>
	/* Shared .settings/.card/.field/.grid/.banner/.icon-btn/.empty/.muted
	   primitives live in web/src/lib/styles/settings.css. Only page-specific
	   layout stays here. */

	.col-name {
		width: auto;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.col-source {
		width: 6rem;
	}

	.add-form {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		padding-top: 0.6rem;
		margin-top: 0.6rem;
		border-top: 1px solid var(--border-subtle);
	}

	.add-form h3 {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--text-muted);
	}

	.add-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
		gap: 0.6rem;
		/* Bottom-align so the Enabled checkbox row lines up with the other
		   fields' inputs (which sit below their labels), not their labels. */
		align-items: end;
	}

	/* Nudge the checkbox down so it visually rests on the inputs' baseline
	   row rather than floating a touch high in its bottom-aligned cell. */
	.add-grid :global(.field.checkbox) {
		padding-bottom: 0.35rem;
	}

	.add-actions {
		display: flex;
		justify-content: flex-end;
	}

	.briefing-pick {
		display: flex;
		gap: 0.75rem;
		align-items: flex-end;
		margin-bottom: 1rem;
	}

	/* ---- Blocks table ---- */
	.col-order {
		width: 4.5rem;
	}
	.col-render {
		width: 7rem;
	}
	.col-src {
		width: 10rem;
	}

	.grid td.col-order.actions {
		text-align: left;
		white-space: nowrap;
	}

	.grid td.actions,
	.grid th.actions {
		width: 4.5rem;
	}

	.block-name {
		display: inline-flex;
		align-items: baseline;
		gap: 0.4rem;
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		font-size: var(--text-sm);
		font-weight: 600;
		color: var(--text-primary);
		cursor: pointer;
		text-align: left;
	}

	.block-name:hover .block-title-text {
		text-decoration: underline;
	}

	.chevron {
		color: var(--text-dim);
		font-size: var(--text-xs);
	}

	.block-directive {
		font-size: var(--text-xs);
		margin-top: 0.15rem;
		max-width: 40ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.render-pill {
		display: inline-block;
		font-size: var(--text-xs);
		padding: 0.05rem 0.45rem;
		border-radius: var(--radius-pill);
		background: var(--surface-raised);
		color: var(--text-muted);
	}

	.kind-pills {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem;
	}

	.kind-pill {
		display: inline-block;
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: var(--text-xs);
		padding: 0.05rem 0.4rem;
		border-radius: var(--radius-pill);
		background: var(--surface-raised);
		color: var(--text-secondary);
	}

	.kind-pill.off {
		opacity: 0.45;
		text-decoration: line-through;
	}

	.expanded > td {
		border-bottom-color: transparent;
	}

	/* ---- Block detail (expanded row) ---- */
	.detail-row > td {
		padding: 0;
		background: var(--surface-base);
	}

	.block-detail {
		padding: 0.85rem 1rem 1rem;
		border-left: 2px solid var(--border-default);
	}

	.detail-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(200px, 100%), 1fr));
		gap: 0.6rem 1rem;
	}

	.sources-block {
		margin-top: 1rem;
		padding-top: 0.75rem;
		border-top: 1px dashed var(--border-subtle);
	}

	.sources-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		margin-bottom: 0.5rem;
	}

	.sources-head h4 {
		margin: 0;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-dim);
	}

	/* The nested sources table shares the .grid look but drops the heavy header. */
	.grid.sub {
		background: var(--surface-raised);
		border-radius: var(--radius-card, 0.5rem);
		overflow: hidden;
	}

	.grid.sub td {
		border-bottom: 1px solid var(--border-subtle);
	}

	.grid.sub tr:last-child td {
		border-bottom: none;
	}

	.col-kind {
		width: 6rem;
	}
	.col-toggle {
		width: 4rem;
	}
	.grid.sub td.actions {
		width: 4.5rem;
	}

	.src-summary {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 0;
	}

	.toggle {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		font-size: var(--text-xs);
		color: var(--text-muted);
	}

	.empty.small {
		font-size: var(--text-sm);
		padding: 0.5rem 0.25rem;
	}

	.muted.small,
	p.muted.small {
		font-size: var(--text-xs);
	}

	/* ---- Source config form (inline in the sources table) ----
	   The form replaces a collapsed source row in place, so its content must
	   share the row's left inset. The colspan cell drops the table padding and
	   the form re-adds the same 0.5rem horizontal inset the collapsed cells use
	   (.settings .grid td) — no jump on entering edit mode. A subtle inset
	   background + a header divider signal the edit state. */
	.grid.sub .src-form-cell {
		padding: 0;
		background: var(--surface-card);
	}

	.source-form {
		display: flex;
		flex-direction: column;
		gap: 0.7rem;
		padding: 0.7rem 0.5rem 0.75rem;
	}

	.source-form-head {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding-bottom: 0.6rem;
		border-bottom: 1px solid var(--border-subtle);
	}

	.source-form-editing {
		margin-left: auto;
		font-size: var(--text-xs);
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--text-dim);
	}

	.kind-name {
		font-size: var(--text-sm);
		font-weight: 600;
		color: var(--text-primary);
	}

	.inline-fields {
		display: flex;
		gap: 1.25rem;
		align-items: flex-end;
		flex-wrap: wrap;
	}

	.source-form-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
		margin-top: 0.2rem;
	}

	.path-msg {
		margin: 0.3rem 0 0;
		font-size: 0.85rem;
	}

	.path-ok {
		color: var(--color-success, #2a8);
	}

	.path-warn {
		color: var(--color-warning, #b80);
	}

	.chip-list {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
		margin-bottom: 0.4rem;
	}

	.chip {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		font-size: var(--text-xs);
		padding: 0.1rem 0.2rem 0.1rem 0.5rem;
		border-radius: var(--radius-pill);
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		color: var(--text-secondary);
	}

	.chip button {
		background: none;
		border: none;
		color: var(--text-dim);
		cursor: pointer;
		font: inherit;
		font-size: var(--text-sm);
		line-height: 1;
		padding: 0 0.15rem;
	}

	.chip button:hover {
		color: #e88;
	}

	.chip-add {
		display: flex;
		gap: 0.4rem;
		align-items: center;
	}

	.chip-add input {
		flex: 1;
		max-width: 20rem;
	}

	.add-block {
		display: flex;
		gap: 0.5rem;
		align-items: center;
		margin-top: 0.75rem;
	}

	.add-block input {
		flex: 1;
		background: var(--surface-base);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		padding: 0.4rem 0.5rem;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
	}

	@container settings (max-width: 720px) {
		.col-src {
			display: none;
		}
	}

	@container settings (max-width: 560px) {
		.col-render {
			display: none;
		}
		.col-source {
			display: none;
		}
	}
</style>
