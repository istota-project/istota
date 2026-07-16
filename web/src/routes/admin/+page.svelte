<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { getAdminStats, type AdminStats, type AdminStatsJob, type AdminStatsUser, type AdminStatsUserSource } from '$lib/api';

	let stats: AdminStats | null = $state(null);
	let loading = $state(true);
	let error = $state('');
	let expandedJobs: Record<number, boolean> = $state({});
	let modulesExpanded = $state(false);

	const REFRESH_MS = 60_000;
	let timer: ReturnType<typeof setInterval> | null = null;

	async function refresh() {
		try {
			stats = await getAdminStats();
			error = '';
		} catch (e) {
			error = (e as Error).message || 'Failed to load admin stats';
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		void refresh();
		timer = setInterval(refresh, REFRESH_MS);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	function formatBytes(n: number): string {
		if (!n) return '0 B';
		const units = ['B', 'KB', 'MB', 'GB', 'TB'];
		const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
		return `${(n / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
	}

	function formatDuration(seconds: number): string {
		if (!seconds) return '—';
		const d = Math.floor(seconds / 86400);
		const h = Math.floor((seconds % 86400) / 3600);
		const m = Math.floor((seconds % 3600) / 60);
		if (d) return `${d}d ${h}h`;
		if (h) return `${h}h ${m}m`;
		if (m) return `${m}m`;
		return `${seconds}s`;
	}

	function formatTimestamp(ts: string | null): string {
		if (!ts) return '—';
		const d = new Date(ts);
		if (Number.isNaN(d.getTime())) return ts;
		const diff = (Date.now() - d.getTime()) / 1000;
		if (diff < 0) return 'just now';
		if (diff < 60) return `${Math.floor(diff)}s ago`;
		if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
		if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
		return `${Math.floor(diff / 86400)}d ago`;
	}

	function formatNumber(n: number): string {
		return n.toLocaleString();
	}

	function moduleErrorCount(mod: Record<string, unknown>): number {
		const v = mod['poll_errors_24h'] ?? mod['sync_errors_24h'] ?? mod['resolve_errors'];
		return typeof v === 'number' ? v : 0;
	}

	const FIELD_LABELS: Record<string, string> = {
		status: 'Status',
		users_configured: 'Users configured',
		users_resolved: 'Users resolved',
		resolve_errors: 'Resolve errors',
		feeds_total: 'Feeds',
		entries_total: 'Entries',
		entries_unread: 'Unread',
		last_poll: 'Last poll',
		poll_errors_24h: 'Poll errors (24h)',
		transactions_total: 'Transactions',
		last_sync: 'Last sync',
		sync_errors_24h: 'Sync errors (24h)',
		visits_total: 'Visits',
		places_total: 'Places',
		last_update: 'Last update',
	};

	function fieldLabel(key: string): string {
		return FIELD_LABELS[key] ?? key.replace(/_/g, ' ');
	}

	const TIMESTAMP_KEYS = new Set([
		'last_poll',
		'last_sync',
		'last_update',
		'last_active',
		'last_run_at',
		'last_success_at',
		'last_backup',
		'last_scheduler_run',
	]);

	function toggleJob(id: number) {
		expandedJobs = { ...expandedJobs, [id]: !expandedJobs[id] };
	}

	// Source colours — semantic, kept in sync with classifyClass below.
	// Greys for automated noise (scheduled / briefing / heartbeat) and
	// brighter colours for the interactive sources we actually care about.
	const SOURCE_COLOR: Record<string, string> = {
		talk: '#6c8ebf',
		email: '#d6a000',
		cli: '#6eb884',
		tasks_file: '#a78bd6',
		scheduled: '#444',
		briefing: '#555',
		heartbeat: '#3a3a3a',
		subtask: '#666',
	};

	function sourceColor(name: string): string {
		return SOURCE_COLOR[name] ?? '#777';
	}

	const INTERACTIVE_SOURCES = new Set(['talk', 'email', 'tasks_file', 'cli']);

	interface SourceSegment {
		source: string;
		count: number;
		failed: number;
		avg: number | null;
	}

	function userSegments(u: AdminStatsUser): SourceSegment[] {
		const entries = Object.entries(u.tasks_by_source_24h ?? {}) as [string, AdminStatsUserSource][];
		return entries
			.filter(([, v]) => v.count > 0)
			.sort((a, b) => {
				// Interactive first, then by count descending — keeps the
				// useful sources at the visible left edge of the bar.
				const ai = INTERACTIVE_SOURCES.has(a[0]) ? 0 : 1;
				const bi = INTERACTIVE_SOURCES.has(b[0]) ? 0 : 1;
				if (ai !== bi) return ai - bi;
				return b[1].count - a[1].count;
			})
			.map(([source, v]) => ({
				source,
				count: v.count,
				failed: v.failed,
				avg: v.avg_duration_seconds,
			}));
	}

	function segmentTooltip(seg: SourceSegment): string {
		const parts = [`${seg.source}: ${seg.count}`];
		if (seg.failed > 0) parts.push(`${seg.failed} failed`);
		if (seg.avg !== null) parts.push(`avg ${seg.avg.toFixed(1)}s`);
		return parts.join(' · ');
	}

	function isModuleJob(name: string): boolean {
		return name.startsWith('_module.');
	}

	interface PartitionedJobs {
		regular: AdminStatsJob[];
		moduleJobs: AdminStatsJob[];
	}

	function partitionJobs(jobs: AdminStatsJob[]): PartitionedJobs {
		const regular: AdminStatsJob[] = [];
		const moduleJobs: AdminStatsJob[] = [];
		for (const j of jobs) {
			(isModuleJob(j.name) ? moduleJobs : regular).push(j);
		}
		return { regular, moduleJobs };
	}

	function moduleJobSummary(jobs: AdminStatsJob[]): { failures: number; lastRun: string | null } {
		let failures = 0;
		let lastRun: string | null = null;
		for (const j of jobs) {
			failures += j.consecutive_failures;
			if (j.last_run_at && (!lastRun || j.last_run_at > lastRun)) {
				lastRun = j.last_run_at;
			}
		}
		return { failures, lastRun };
	}
</script>

<div class="settings admin-page">
	<header class="settings-header">
		<div>
			<h1>Admin</h1>
			{#if !loading && stats}
				<p class="hint">Auto-refresh every 60s · {formatTimestamp(new Date().toISOString())}</p>
			{/if}
		</div>
	</header>

	{#if loading && !stats}
		<div class="loading">Loading…</div>
	{:else if error}
		<div class="banner error">{error}</div>
	{:else if stats}
		<!-- System banner -->
		<section class="card system-banner card-grid">
			<div class="banner-cell">
				<div class="cell-label">Status</div>
				<div class="cell-value">
					<span class="dot" class:dot-ok={stats.system.scheduler_healthy} class:dot-bad={!stats.system.scheduler_healthy}></span>
					{stats.system.scheduler_healthy ? 'Healthy' : 'Stale'}
				</div>
				<div class="cell-sub">last activity {formatTimestamp(stats.system.last_scheduler_run)}</div>
			</div>
			<div class="banner-cell">
				<div class="cell-label">Version</div>
				<div class="cell-value">{stats.system.version}</div>
				<div class="cell-sub">Python {stats.system.python_version}</div>
			</div>
			<div class="banner-cell">
				<div class="cell-label">Web uptime</div>
				<div class="cell-value">{formatDuration(stats.system.uptime_seconds)}</div>
			</div>
			<div class="banner-cell">
				<div class="cell-label">Database</div>
				<div class="cell-value">{formatBytes(stats.system.db_size_bytes)}</div>
				<div class="cell-sub">mount {stats.storage.nextcloud_mount_healthy ? '✓' : '✗'}</div>
			</div>
		</section>

		<!-- Users -->
		<section class="card">
			<header class="section-header">
				<h2>Users</h2>
			</header>
			<div class="table-scroll">
				<table class="grid users-grid">
					<thead>
						<tr>
							<th>User</th>
							<th class="num col-total">Total</th>
							<th>24h activity</th>
							<th class="num">Failed</th>
							<th class="num col-avg">Avg/day</th>
							<th class="col-active">Last active</th>
						</tr>
					</thead>
					<tbody>
						{#each stats.users as u (u.username)}
							{@const segments = userSegments(u)}
							{@const totalSeg = segments.reduce((acc, s) => acc + s.count, 0)}
							<tr>
								<td>
									<span class="username">{u.display_name || u.username}</span>
									{#if u.is_admin}<span class="badge">admin</span>{/if}
								</td>
								<td class="num col-total">{formatNumber(u.tasks_total)}</td>
								<td class="source-cell">
									<div class="source-summary">
										<span class="muted">int</span>
										<strong>{u.tasks_interactive_24h}</strong>
										<span class="sep">·</span>
										<span class="muted">auto</span>
										<strong>{formatNumber(u.tasks_automated_24h)}</strong>
									</div>
									{#if totalSeg > 0}
										<div class="stack-bar" aria-label="24h source breakdown">
											{#each segments as seg (seg.source)}
												<span
													class="stack-seg"
													style="width: {(seg.count / totalSeg) * 100}%; background: {sourceColor(seg.source)};"
													title={segmentTooltip(seg)}
												></span>
											{/each}
										</div>
										<div class="source-list">
											{#each segments as seg (seg.source)}
												<span class="source-pill" title={segmentTooltip(seg)}>
													<span class="dot dot-source" style="background: {sourceColor(seg.source)};"></span>
													{seg.source} {formatNumber(seg.count)}
												</span>
											{/each}
										</div>
									{/if}
								</td>
								<td class="num">
									{#if u.tasks_failed_24h > 0}
										<span class="failed-pill">{u.tasks_failed_24h}</span>
									{:else}
										0
									{/if}
								</td>
								<td class="num col-avg">{u.tasks_avg_per_day}</td>
								<td class="col-active">{formatTimestamp(u.last_active)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</section>

		<!-- Tasks -->
		<section class="card">
			<header class="section-header">
				<h2>Task activity</h2>
			</header>
			<div class="kpi-grid card-grid">
				<div class="kpi">
					<div class="kpi-label">Interactive 24h</div>
					<div class="kpi-value">{formatNumber(stats.tasks.interactive_24h)}</div>
					<div class="kpi-sub">{stats.tasks.interactive_avg_per_day_30d}/day (30d)</div>
				</div>
				<div class="kpi">
					<div class="kpi-label">Automated 24h</div>
					<div class="kpi-value muted">{formatNumber(stats.tasks.automated_24h)}</div>
					<div class="kpi-sub">{formatNumber(stats.tasks.automated_avg_per_day_30d)}/day (30d)</div>
				</div>
				<div class="kpi">
					<div class="kpi-label">Avg duration</div>
					<div class="kpi-value">{stats.tasks.avg_duration_seconds}s</div>
				</div>
				<div class="kpi" class:kpi-warn={stats.tasks.failed_24h > 0}>
					<div class="kpi-label">Failed 24h</div>
					<div class="kpi-value">{stats.tasks.failed_24h}</div>
					<div class="kpi-sub">{(stats.tasks.error_rate_24h * 100).toFixed(2)}% error rate</div>
				</div>
				<div class="kpi col-total-kpi">
					<div class="kpi-label">Total tasks</div>
					<div class="kpi-value">{formatNumber(stats.tasks.total)}</div>
				</div>
			</div>
			{#if Object.keys(stats.tasks.by_source).length > 0}
				{@const maxN = Math.max(...Object.values(stats.tasks.by_source))}
				<div class="source-bars">
					{#each Object.entries(stats.tasks.by_source).sort((a, b) => b[1] - a[1]) as [src, count] (src)}
						{@const failed = stats.tasks.failed_by_source_24h?.[src] ?? 0}
						<div class="source-row">
							<div class="source-label">
								<span class="dot dot-source" style="background: {sourceColor(src)};"></span>
								{src}
							</div>
							<div class="source-bar">
								<div class="source-fill" style="width: {(count / maxN) * 100}%; background: {sourceColor(src)};"></div>
							</div>
							<div class="source-count">
								{formatNumber(count)}{#if failed > 0}<span class="failed-inline" title="failed in 24h">·{failed} failed</span>{/if}
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</section>

		<!-- Modules -->
		{#if Object.keys(stats.modules).length > 0}
			<section class="card">
				<header class="section-header">
					<h2>Modules</h2>
				</header>
				<div class="module-grid card-grid">
					{#each Object.entries(stats.modules) as [name, mod] (name)}
						<div class="module-card" class:module-warn={moduleErrorCount(mod) > 0}>
							<div class="module-name">{name}</div>
							<dl class="module-fields">
								{#each Object.entries(mod) as [k, v] (k)}
									<dt>{fieldLabel(k)}</dt>
									<dd>{v === null ? '—' : TIMESTAMP_KEYS.has(k) ? formatTimestamp(String(v)) : String(v)}</dd>
								{/each}
							</dl>
						</div>
					{/each}
				</div>
			</section>
		{/if}

		<!-- Scheduler -->
		<section class="card">
			<header class="section-header">
				<h2>Scheduler</h2>
				<span class="muted meta">{stats.scheduler.jobs_active} active · {stats.scheduler.jobs_paused} paused</span>
			</header>
			{#if stats.scheduler.jobs.length === 0}
				<p class="empty">No scheduled jobs.</p>
			{:else}
				{@const parts = partitionJobs(stats.scheduler.jobs)}
				<div class="table-scroll">
					<table class="grid jobs-grid">
						<thead>
							<tr>
								<th>Job</th>
								<th class="col-cron">Cron</th>
								<th class="col-status">Status</th>
								<th class="col-lastrun">Last run</th>
								<th class="num">Failures</th>
							</tr>
						</thead>
						<tbody>
							{#each parts.regular as j (j.id)}
								{@const expandable = !!j.last_error}
								<tr
									class:row-error={j.consecutive_failures > 0}
									class:row-clickable={expandable}
									onclick={() => expandable && toggleJob(j.id)}
								>
									<td>
										<span class="username">{j.user_id}</span>
										<span class="muted">/</span>
										<span class="job-name">{j.name}</span>
									</td>
									<td class="col-cron"><code>{j.cron}</code></td>
									<td class="col-status">
										<span class="dot" class:dot-ok={j.enabled} class:dot-mute={!j.enabled}></span>
										<span class="status-label">{j.enabled ? 'enabled' : 'paused'}</span>
									</td>
									<td class="col-lastrun">{formatTimestamp(j.last_run_at)}</td>
									<td class="num">{j.consecutive_failures}</td>
								</tr>
								{#if expandable && expandedJobs[j.id]}
									<tr class="error-row">
										<td colspan="5"><pre>{j.last_error}</pre></td>
									</tr>
								{/if}
							{/each}
							{#if parts.moduleJobs.length > 0}
								{@const summary = moduleJobSummary(parts.moduleJobs)}
								<tr
									class:row-error={summary.failures > 0}
									class="row-clickable module-summary-row"
									onclick={() => (modulesExpanded = !modulesExpanded)}
								>
									<td>
										<span class="disclosure">{modulesExpanded ? '▾' : '▸'}</span>
										<span class="muted">Module pollers</span>
										<span class="badge">{parts.moduleJobs.length}</span>
									</td>
									<td class="col-cron"><span class="muted">—</span></td>
									<td class="col-status"><span class="muted">—</span></td>
									<td class="col-lastrun">{formatTimestamp(summary.lastRun)}</td>
									<td class="num">{summary.failures}</td>
								</tr>
								{#if modulesExpanded}
									{#each parts.moduleJobs as j (j.id)}
										{@const expandable = !!j.last_error}
										<tr
											class:row-error={j.consecutive_failures > 0}
											class:row-clickable={expandable}
											class="module-child-row"
											onclick={() => expandable && toggleJob(j.id)}
										>
											<td>
												<span class="username">{j.user_id}</span>
												<span class="muted">/</span>
												<span class="job-name">{j.name}</span>
											</td>
											<td class="col-cron"><code>{j.cron}</code></td>
											<td class="col-status">
												<span class="dot" class:dot-ok={j.enabled} class:dot-mute={!j.enabled}></span>
												<span class="status-label">{j.enabled ? 'enabled' : 'paused'}</span>
											</td>
											<td class="col-lastrun">{formatTimestamp(j.last_run_at)}</td>
											<td class="num">{j.consecutive_failures}</td>
										</tr>
										{#if expandable && expandedJobs[j.id]}
											<tr class="error-row">
												<td colspan="5"><pre>{j.last_error}</pre></td>
											</tr>
										{/if}
									{/each}
								{/if}
							{/if}
						</tbody>
					</table>
				</div>
			{/if}
		</section>

		<!-- Storage -->
		<section class="card">
			<header class="section-header">
				<h2>Storage</h2>
			</header>
			<dl class="kv">
				<dt>Database size</dt>
				<dd>{formatBytes(stats.storage.db_size_bytes)}</dd>
				<dt>Backups</dt>
				<dd>{stats.storage.backups_count}</dd>
				<dt>Last backup</dt>
				<dd>{formatTimestamp(stats.storage.last_backup)}</dd>
				<dt>Nextcloud mount</dt>
				<dd>
					<span class="dot" class:dot-ok={stats.storage.nextcloud_mount_healthy} class:dot-bad={!stats.storage.nextcloud_mount_healthy}></span>
					{stats.storage.nextcloud_mount_healthy ? 'healthy' : 'unavailable'}
				</dd>
			</dl>
		</section>

		{#if stats.error}
			<div class="banner error">Partial data: {stats.error}</div>
		{/if}
	{/if}
</div>

<style>
	/* Layout primitives (.settings / .card / .grid / .banner / .placeholder /
	   .section-header / .hint) come from web/src/lib/styles/settings.css.
	   Admin-specific bits below: KPIs, source bars, dot indicators. */

	.admin-page {
		max-width: 1100px;
	}

	.admin-page .hint {
		margin: 0;
	}

	/* `.settings .card` (from settings.css) sets `display: flex; flex-direction:
	   column` at specificity (0,2,0), which beats the global `.card-grid`
	   layout (0,1,0). The banner is itself a `.card`, so it needs `display: grid`
	   restated here (same specificity as `.settings .card`, but scoped, so it
	   wins) — otherwise the cells stack in a column. The grid track sizing still
	   comes from `.card-grid` via `--card-min` / `--card-gap`. */
	.admin-page .system-banner {
		display: grid;
		--card-min: 150px;
		--card-gap: 0.75rem 1.5rem;
	}

	.banner-cell {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}

	.cell-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.cell-value {
		font-size: 1rem;
		font-weight: 600;
		color: var(--text-primary);
	}

	.cell-sub {
		font-size: var(--text-xs);
		color: var(--text-muted);
	}

	.dot {
		display: inline-block;
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--text-dim);
		margin-right: 0.25rem;
		flex-shrink: 0;
	}
	.dot-ok { background: #4aff7f; }
	.dot-bad { background: #ff5a5a; }
	.dot-mute { background: var(--text-dim); }
	.dot-source { width: 6px; height: 6px; }

	.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}

	.username {
		font-weight: 500;
	}

	.badge {
		display: inline-block;
		margin-left: 0.4rem;
		font-size: var(--text-xs);
		padding: 0.05rem 0.4rem;
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		color: var(--text-muted);
	}

	.kpi-grid {
		--card-min: 140px;
		--card-gap: 0.75rem 1.5rem;
	}

	.kpi {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		min-width: 0;
	}

	.kpi-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.kpi-value {
		font-size: 1.2rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}

	.kpi-value.muted {
		color: var(--text-muted);
	}

	.kpi-sub {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.kpi-warn .kpi-value {
		color: #ff9b5a;
	}

	/* Source distribution bars (horizontal, one per source_type). */
	.source-bars {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		margin-top: 0.25rem;
	}

	.source-row {
		display: grid;
		grid-template-columns: minmax(80px, 100px) 1fr minmax(70px, max-content);
		gap: 0.75rem;
		align-items: center;
		font-size: var(--text-sm);
	}

	.source-label {
		color: var(--text-muted);
		display: flex;
		align-items: center;
		gap: 0.25rem;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.source-bar {
		height: 6px;
		background: var(--surface-base);
		border-radius: 3px;
		overflow: hidden;
	}

	.source-fill {
		height: 100%;
	}

	.source-count {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.failed-inline {
		margin-left: 0.4rem;
		color: #ff9b5a;
		font-size: var(--text-xs);
	}

	/* Per-user 24h breakdown — stacked bar + tag list. */
	.users-grid {
		min-width: 540px;
	}

	.source-cell {
		min-width: 200px;
	}

	.source-summary {
		font-size: var(--text-sm);
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 0.25rem;
	}

	.source-summary strong {
		font-variant-numeric: tabular-nums;
	}

	.sep {
		color: var(--text-dim);
		margin: 0 0.15rem;
	}

	.stack-bar {
		display: flex;
		height: 5px;
		border-radius: 3px;
		overflow: hidden;
		margin: 0.25rem 0;
		background: var(--surface-base);
	}

	.stack-seg {
		display: block;
		height: 100%;
	}

	.source-list {
		display: flex;
		flex-wrap: wrap;
		gap: 0.4rem;
		margin-top: 0.15rem;
	}

	.source-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		font-size: var(--text-xs);
		color: var(--text-muted);
	}

	.failed-pill {
		display: inline-block;
		padding: 0.05rem 0.4rem;
		background: rgba(255, 90, 90, 0.15);
		color: #ff9b5a;
		border-radius: var(--radius-pill);
		font-size: var(--text-xs);
	}

	.muted {
		color: var(--text-dim);
		font-size: var(--text-sm);
		font-weight: 400;
	}

	/* Scheduler table */
	.jobs-grid {
		min-width: 580px;
	}

	.job-name {
		word-break: break-word;
	}

	.row-clickable {
		cursor: pointer;
	}

	.row-clickable:hover {
		background: var(--surface-raised);
	}

	.row-error td:first-child::before {
		content: '!';
		display: inline-block;
		color: #ff9b5a;
		margin-right: 0.4rem;
		font-weight: 700;
	}

	.module-summary-row td {
		color: var(--text-muted);
	}

	.module-child-row td:first-child {
		padding-left: 1.5rem;
	}

	.disclosure {
		display: inline-block;
		width: 1em;
		color: var(--text-dim);
		margin-right: 0.25rem;
	}

	.error-row td {
		background: var(--surface-base);
		padding: 0.5rem 0.75rem;
	}

	.error-row pre {
		margin: 0;
		font-size: var(--text-xs);
		color: #ff9b5a;
		white-space: pre-wrap;
	}

	code {
		font-size: var(--text-xs);
		color: var(--text-secondary);
	}

	.module-grid {
		--card-min: 220px;
	}

	.module-card {
		background: var(--surface-base);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-card);
		padding: 0.75rem 0.9rem;
	}

	.module-warn {
		border-color: #663520;
	}

	.module-name {
		font-weight: 600;
		font-size: var(--text-base);
		margin-bottom: 0.4rem;
	}

	.module-fields {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.1rem 0.75rem;
		margin: 0;
		font-size: var(--text-xs);
	}

	.module-fields dt {
		color: var(--text-dim);
	}

	.module-fields dd {
		margin: 0;
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.kv {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.25rem 1.5rem;
		margin: 0;
		font-size: var(--text-sm);
	}

	.kv dt {
		color: var(--text-dim);
	}

	.kv dd {
		margin: 0;
	}

	/* Mobile: drop low-priority columns and tighten the source cell.
	   Ordered widest-to-narrowest. The Total column is hidden first because
	   the per-source breakdown carries 24h activity (the more interesting
	   number) and the headline tasks card already shows the grand total. */
	@media (max-width: 768px) {
		.col-total,
		.col-active {
			display: none;
		}
		.users-grid {
			min-width: 0;
		}
		.kpi-value {
			font-size: 1.1rem;
		}
	}

	@media (max-width: 640px) {
		.col-avg,
		.col-cron {
			display: none;
		}
		.col-total-kpi {
			display: none;
		}
		.jobs-grid {
			min-width: 0;
		}
		/* Source greys are hard to tell apart in the stack-bar at any width;
		   on mobile the bar is even narrower, so keep the labelled chips
		   visible — they're the only colour-independent legend the user
		   gets when hover tooltips aren't available. */
		.source-list {
			gap: 0.3rem;
		}
		.source-pill {
			font-size: 0.7rem;
		}
	}

	@media (max-width: 480px) {
		.col-status .status-label {
			display: none;
		}
		.col-lastrun {
			max-width: 6rem;
		}
		.admin-page .system-banner {
			grid-template-columns: 1fr 1fr;
		}
	}

	/* Light theme overrides — dark rules above untouched. JS chart/dot color
	   constants (SOURCE_COLOR, .dot-ok/.dot-bad fills) are data-viz on their own
	   colored swatches and are left as-is. Only CSS text/borders fixed here. */
	:global(:root[data-theme='light']) .kpi-warn .kpi-value { color: #946a00; }
	:global(:root[data-theme='light']) .failed-inline { color: #946a00; }
	:global(:root[data-theme='light']) .failed-pill { color: #946a00; }
	:global(:root[data-theme='light']) .row-error td:first-child::before { color: #946a00; }
	:global(:root[data-theme='light']) .error-row pre { color: #946a00; }
	:global(:root[data-theme='light']) .module-warn { border-color: #e3c08a; }
</style>
