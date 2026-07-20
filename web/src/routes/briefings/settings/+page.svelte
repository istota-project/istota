<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		getMe,
		getBriefingConfig,
		putBriefingBlock,
		deleteBriefingBlock,
		putBriefingSource,
		deleteBriefingSource,
		getBrowsePresets,
		getFeedOptions,
		type BriefingBlock,
		type BriefingConfigResponse,
		type BrowsePreset,
		type FeedOptions
	} from '$lib/api';

	let loading = $state(true);
	let error = $state<string | null>(null);
	let moduleEnabled = $state(true);
	let config = $state<BriefingConfigResponse | null>(null);
	let presets = $state<BrowsePreset[]>([]);
	let feedOptions = $state<FeedOptions | null>(null);

	let selectedName = $state<string>('');
	let newBlockTitle = $state('');

	const KINDS = ['email', 'rss', 'browse', 'markets', 'calendar', 'todos', 'reminders', 'notes'];

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

	async function reload() {
		error = null;
		try {
			config = await getBriefingConfig();
			if (!selectedName && allNames.length) selectedName = allNames[0];
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load';
		}
	}

	onMount(async () => {
		try {
			const me = await getMe();
			moduleEnabled = me.features.briefings;
			if (!moduleEnabled) {
				loading = false;
				return;
			}
			[presets, feedOptions] = await Promise.all([
				getBrowsePresets().then((r) => r.presets),
				getFeedOptions()
			]);
			await reload();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load';
		} finally {
			loading = false;
		}
	});

	async function addBlock() {
		const title = newBlockTitle.trim();
		if (!title || !selectedName) return;
		await putBriefingBlock({ briefing_name: selectedName, title });
		newBlockTitle = '';
		await reload();
	}

	async function renameBlock(block: BriefingBlock, title: string) {
		await putBriefingBlock({ id: block.id, title });
		await reload();
	}

	async function setRenderMode(block: BriefingBlock, render_mode: string) {
		await putBriefingBlock({ id: block.id, render_mode });
		await reload();
	}

	async function setDirective(block: BriefingBlock, directive: string) {
		await putBriefingBlock({ id: block.id, directive });
		await reload();
	}

	async function removeBlock(block: BriefingBlock) {
		if (!confirm(`Delete block "${block.title}"?`)) return;
		await deleteBriefingBlock(block.id);
		await reload();
	}

	async function move(block: BriefingBlock, dir: -1 | 1) {
		const ids = currentBlocks.map((b) => b.id);
		const idx = ids.indexOf(block.id);
		const swap = idx + dir;
		if (swap < 0 || swap >= ids.length) return;
		[ids[idx], ids[swap]] = [ids[swap], ids[idx]];
		await putBriefingBlock({ reorder: { briefing_name: selectedName, ordered_ids: ids } });
		await reload();
	}

	async function addSource(block: BriefingBlock, kind: string) {
		const config: Record<string, unknown> = {};
		if (kind === 'email') config.mode = 'shared';
		await putBriefingSource({ block_id: block.id, kind, config });
		await reload();
	}

	async function removeSource(id: number) {
		await deleteBriefingSource(id);
		await reload();
	}

	async function updateSourceConfig(id: number, cfg: Record<string, unknown>) {
		await putBriefingSource({ id, config: cfg });
		await reload();
	}

	async function toggleSource(id: number, enabled: boolean) {
		await putBriefingSource({ id, enabled });
		await reload();
	}
</script>

<svelte:head>
	<title>Briefings settings</title>
</svelte:head>

<div class="settings">
	<h1>Briefings settings</h1>

	{#if loading}
		<p class="status">Loading…</p>
	{:else if !moduleEnabled}
		<div class="banner">
			Module disabled — enable it in <a href="{base}/settings">Settings → Preferences</a>.
		</div>
	{:else if error}
		<p class="status error">{error}</p>
	{:else if config}
		<p class="muted">
			Content blocks are synthesized into your briefing sections. Schedule and delivery
			are set on the <a href="{base}/settings">Settings</a> page.
		</p>

		<div class="briefing-pick">
			<label>
				Briefing:
				<select bind:value={selectedName}>
					{#each allNames as n (n)}
						<option value={n}>{n}</option>
					{/each}
				</select>
			</label>
			{#if allNames.length === 0}
				<span class="muted">No briefings scheduled yet — create one in Settings first.</span>
			{/if}
		</div>

		{#if selectedName}
			<div class="blocks">
				{#each currentBlocks as block (block.id)}
					<div class="block-card">
						<div class="block-head">
							<input
								class="block-title"
								value={block.title}
								onchange={(e) => renameBlock(block, (e.target as HTMLInputElement).value)}
							/>
							<div class="block-actions">
								<button title="Move up" onclick={() => move(block, -1)}>↑</button>
								<button title="Move down" onclick={() => move(block, 1)}>↓</button>
								<button class="danger" title="Delete" onclick={() => removeBlock(block)}>✕</button>
							</div>
						</div>

						<label class="row">
							Directive:
							<input
								class="directive"
								value={block.directive}
								placeholder="e.g. 3–5 stories, neutral tone"
								onchange={(e) => setDirective(block, (e.target as HTMLInputElement).value)}
							/>
						</label>

						<label class="row">
							Render:
							<select
								value={block.render_mode}
								onchange={(e) => setRenderMode(block, (e.target as HTMLSelectElement).value)}
							>
								<option value="synthesis">Synthesis (summarize)</option>
								<option value="structured">Structured (verbatim)</option>
							</select>
						</label>

						<div class="sources">
							<h4>Sources</h4>
							{#each block.sources as source (source.id)}
								<div class="source-row">
									<span class="source-kind">{source.kind}</span>
									{#if source.kind === 'email'}
										<select
											value={(source.config.mode as string) ?? 'shared'}
											onchange={(e) =>
												updateSourceConfig(source.id, {
													...source.config,
													mode: (e.target as HTMLSelectElement).value
												})}
										>
											<option value="shared">Shared pool</option>
											<option value="senders">Selected senders</option>
										</select>
										{#if source.config.mode === 'senders'}
											<input
												class="grow"
												placeholder="*@semafor.com, news@axios.com"
												value={((source.config.senders as string[]) ?? []).join(', ')}
												onchange={(e) =>
													updateSourceConfig(source.id, {
														...source.config,
														senders: (e.target as HTMLInputElement).value
															.split(',')
															.map((s) => s.trim())
															.filter(Boolean)
													})}
											/>
										{/if}
									{:else if source.kind === 'browse'}
										<select
											value={(source.config.preset as string) ?? ''}
											onchange={(e) =>
												updateSourceConfig(source.id, {
													preset: (e.target as HTMLSelectElement).value || null,
													url: null
												})}
										>
											<option value="">Custom URL…</option>
											{#each presets as p (p.key)}
												<option value={p.key}>{p.name}</option>
											{/each}
										</select>
										{#if !source.config.preset}
											<input
												class="grow"
												placeholder="https://…"
												value={(source.config.url as string) ?? ''}
												onchange={(e) =>
													updateSourceConfig(source.id, {
														url: (e.target as HTMLInputElement).value
													})}
											/>
										{/if}
									{:else if source.kind === 'rss'}
										<select
											onchange={(e) => {
												const v = (e.target as HTMLSelectElement).value;
												if (!v) return;
												const [kind, value] = v.split(':');
												updateSourceConfig(source.id, {
													...source.config,
													feed_ref: { kind, value: Number(value) }
												});
											}}
										>
											<option value="">Pick a feed / category…</option>
											{#each feedOptions?.categories ?? [] as c (c.value)}
												<option value={`category:${c.value}`}>Category: {c.label}</option>
											{/each}
											{#each feedOptions?.subscriptions ?? [] as s (s.value)}
												<option value={`subscription:${s.value}`}>Feed: {s.label}</option>
											{/each}
										</select>
										{#if source.config.feed_ref}
											<span class="muted"
												>{(source.config.feed_ref as { kind: string }).kind}</span
											>
										{/if}
									{/if}
									<label class="toggle">
										<input
											type="checkbox"
											checked={source.enabled}
											onchange={(e) =>
												toggleSource(source.id, (e.target as HTMLInputElement).checked)}
										/>
										on
									</label>
									<button class="danger" onclick={() => removeSource(source.id)}>✕</button>
								</div>
							{/each}

							<div class="add-source">
								<select
									onchange={(e) => {
										const v = (e.target as HTMLSelectElement).value;
										if (v) {
											addSource(block, v);
											(e.target as HTMLSelectElement).value = '';
										}
									}}
								>
									<option value="">+ Add source…</option>
									{#each KINDS as k (k)}
										<option value={k}>{k}</option>
									{/each}
								</select>
							</div>
						</div>
					</div>
				{/each}

				<div class="add-block">
					<input placeholder="New block title (e.g. World News)" bind:value={newBlockTitle} />
					<button onclick={addBlock} disabled={!newBlockTitle.trim()}>Add block</button>
				</div>
			</div>
		{/if}
	{/if}
</div>

<style>
	.settings {
		max-width: 44rem;
		margin: 0 auto;
		padding: 1.5rem;
	}
	.banner {
		background: var(--surface-warning, rgba(234, 179, 8, 0.15));
		border: 1px solid var(--warning, #eab308);
		border-radius: 0.5rem;
		padding: 0.75rem 1rem;
	}
	.muted {
		color: var(--text-muted, #888);
	}
	.status.error {
		color: var(--danger, #ef4444);
	}
	.briefing-pick {
		margin: 1rem 0;
		display: flex;
		gap: 0.75rem;
		align-items: center;
	}
	.block-card {
		border: 1px solid var(--border, #333);
		border-radius: 0.6rem;
		padding: 0.85rem 1rem;
		margin-bottom: 0.85rem;
	}
	.block-head {
		display: flex;
		gap: 0.5rem;
		align-items: center;
	}
	.block-title {
		flex: 1;
		font-weight: 600;
		font-size: 1rem;
		background: transparent;
		border: 1px solid transparent;
		border-radius: 0.3rem;
		padding: 0.25rem 0.4rem;
		color: inherit;
	}
	.block-title:focus {
		border-color: var(--border, #444);
		outline: none;
	}
	.block-actions button,
	.source-row button,
	.add-block button {
		background: transparent;
		border: 1px solid var(--border, #333);
		border-radius: 0.35rem;
		color: inherit;
		cursor: pointer;
		padding: 0.2rem 0.5rem;
	}
	.danger {
		color: var(--danger, #ef4444);
	}
	.row {
		display: flex;
		gap: 0.5rem;
		align-items: center;
		margin-top: 0.5rem;
		font-size: 0.9rem;
	}
	.directive {
		flex: 1;
		background: transparent;
		border: 1px solid var(--border, #333);
		border-radius: 0.3rem;
		padding: 0.3rem 0.4rem;
		color: inherit;
	}
	.sources {
		margin-top: 0.75rem;
		padding-top: 0.5rem;
		border-top: 1px dashed var(--border, #333);
	}
	.sources h4 {
		margin: 0 0 0.4rem;
		font-size: 0.8rem;
		text-transform: uppercase;
		color: var(--text-muted, #888);
	}
	.source-row {
		display: flex;
		gap: 0.4rem;
		align-items: center;
		margin-bottom: 0.35rem;
		flex-wrap: wrap;
	}
	.source-kind {
		font-family: monospace;
		font-size: 0.8rem;
		min-width: 4.5rem;
	}
	.grow {
		flex: 1;
		min-width: 8rem;
		background: transparent;
		border: 1px solid var(--border, #333);
		border-radius: 0.3rem;
		padding: 0.25rem 0.4rem;
		color: inherit;
	}
	.toggle {
		display: flex;
		align-items: center;
		gap: 0.2rem;
		font-size: 0.8rem;
	}
	.add-block {
		display: flex;
		gap: 0.5rem;
		margin-top: 0.5rem;
	}
	.add-block input {
		flex: 1;
		background: transparent;
		border: 1px solid var(--border, #333);
		border-radius: 0.3rem;
		padding: 0.4rem;
		color: inherit;
	}
	select {
		background: var(--surface, #1a1a1a);
		color: inherit;
		border: 1px solid var(--border, #333);
		border-radius: 0.3rem;
		padding: 0.25rem 0.4rem;
	}
</style>
