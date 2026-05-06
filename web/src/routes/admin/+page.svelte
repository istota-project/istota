<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { getAdminStats, type AdminStats, type AdminStatsJob } from '$lib/api';

	let stats: AdminStats | null = $state(null);
	let loading = $state(true);
	let error = $state('');
	let expandedJobs: Record<number, boolean> = $state({});

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
</script>

<div class="admin">
	<header class="page-head">
		<h1>Admin</h1>
		{#if !loading && stats}
			<span class="refresh-hint">Auto-refresh every 60s · {formatTimestamp(new Date().toISOString())}</span>
		{/if}
	</header>

	{#if loading && !stats}
		<div class="state">Loading…</div>
	{:else if error}
		<div class="state error">{error}</div>
	{:else if stats}
		<!-- System banner -->
		<section class="system-banner card">
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
				<div class="cell-sub">
					mount {stats.storage.nextcloud_mount_healthy ? '✓' : '✗'}
				</div>
			</div>
		</section>

		<!-- Users -->
		<section class="card">
			<h2>Users</h2>
			<div class="table-scroll">
				<table class="users-table">
					<thead>
						<tr>
							<th>User</th>
							<th class="num">Total</th>
							<th class="num">24h</th>
							<th class="num">Avg/day (30d)</th>
							<th>Last active</th>
						</tr>
					</thead>
					<tbody>
						{#each stats.users as u (u.username)}
							<tr>
								<td>
									<span class="username">{u.display_name || u.username}</span>
									{#if u.is_admin}<span class="badge">admin</span>{/if}
								</td>
								<td class="num">{u.tasks_total.toLocaleString()}</td>
								<td class="num">{u.tasks_last_24h}</td>
								<td class="num">{u.tasks_avg_per_day}</td>
								<td>{formatTimestamp(u.last_active)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</section>

		<!-- Tasks -->
		<section class="card">
			<h2>Task activity</h2>
			<div class="kpi-grid">
				<div class="kpi">
					<div class="kpi-label">Total</div>
					<div class="kpi-value">{stats.tasks.total.toLocaleString()}</div>
				</div>
				<div class="kpi">
					<div class="kpi-label">Last 24h</div>
					<div class="kpi-value">{stats.tasks.last_24h}</div>
				</div>
				<div class="kpi">
					<div class="kpi-label">Avg/day (30d)</div>
					<div class="kpi-value">{stats.tasks.avg_per_day_30d}</div>
				</div>
				<div class="kpi">
					<div class="kpi-label">Avg duration</div>
					<div class="kpi-value">{stats.tasks.avg_duration_seconds}s</div>
				</div>
				<div class="kpi" class:kpi-warn={stats.tasks.error_rate_24h > 0.1}>
					<div class="kpi-label">Error rate (24h)</div>
					<div class="kpi-value">{(stats.tasks.error_rate_24h * 100).toFixed(1)}%</div>
				</div>
			</div>
			{#if Object.keys(stats.tasks.by_source).length > 0}
				<div class="source-bars">
					{#each Object.entries(stats.tasks.by_source).sort((a, b) => b[1] - a[1]) as [src, count] (src)}
						{@const maxN = Math.max(...Object.values(stats.tasks.by_source))}
						<div class="source-row">
							<div class="source-label">{src}</div>
							<div class="source-bar">
								<div class="source-fill" style="width: {(count / maxN) * 100}%"></div>
							</div>
							<div class="source-count">{count}</div>
						</div>
					{/each}
				</div>
			{/if}
		</section>

		<!-- Modules -->
		{#if Object.keys(stats.modules).length > 0}
			<section class="card">
				<h2>Modules</h2>
				<div class="module-grid">
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
			<h2>
				Scheduler
				<span class="muted">
					{stats.scheduler.jobs_active} active · {stats.scheduler.jobs_paused} paused
				</span>
			</h2>
			{#if stats.scheduler.jobs.length === 0}
				<div class="empty">No scheduled jobs.</div>
			{:else}
				<div class="table-scroll">
					<table class="jobs-table">
						<thead>
							<tr>
								<th>Job</th>
								<th>Cron</th>
								<th>Status</th>
								<th>Last run</th>
								<th class="num">Failures</th>
							</tr>
						</thead>
						<tbody>
							{#each stats.scheduler.jobs as j (j.id)}
								{@const expandable = !!j.last_error}
								<tr
									class:row-error={j.consecutive_failures > 0}
									class:row-clickable={expandable}
									onclick={() => expandable && toggleJob(j.id)}
								>
									<td>
										<span class="username">{j.user_id}</span>
										<span class="muted">/</span>
										{j.name}
									</td>
									<td><code>{j.cron}</code></td>
									<td>
										<span class="dot" class:dot-ok={j.enabled} class:dot-mute={!j.enabled}></span>
										{j.enabled ? 'enabled' : 'paused'}
									</td>
									<td>{formatTimestamp(j.last_run_at)}</td>
									<td class="num">{j.consecutive_failures}</td>
								</tr>
								{#if expandable && expandedJobs[j.id]}
									<tr class="error-row">
										<td colspan="5"><pre>{j.last_error}</pre></td>
									</tr>
								{/if}
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</section>

		<!-- Storage -->
		<section class="card">
			<h2>Storage</h2>
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
			<div class="state warn">Partial data: {stats.error}</div>
		{/if}
	{/if}
</div>

<style>
	.admin {
		width: 100%;
		max-width: 1100px;
		margin: 0 auto;
		padding: 1.5rem 1rem 4rem;
		display: flex;
		flex-direction: column;
		gap: 1rem;
		box-sizing: border-box;
		min-width: 0;
	}

	.page-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 1rem;
	}

	h1 {
		font-size: 1.1rem;
		font-weight: 600;
		margin: 0;
	}

	.refresh-hint {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.card {
		background: var(--surface-card);
		border-radius: var(--radius-card);
		padding: 1rem 1.25rem;
		min-width: 0;
	}

	.table-scroll {
		width: 100%;
		overflow-x: auto;
		-webkit-overflow-scrolling: touch;
	}

	.card h2 {
		margin: 0 0 0.75rem;
		font-size: var(--text-base);
		font-weight: 600;
		display: flex;
		gap: 0.5rem;
		align-items: baseline;
	}

	.muted {
		font-weight: 400;
		color: var(--text-muted);
		font-size: var(--text-sm);
	}

	.system-banner {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 0.5rem 2rem;
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
	}
	.dot-ok { background: #4aff7f; }
	.dot-bad { background: #ff5a5a; }
	.dot-mute { background: var(--text-dim); }

	.users-table,
	.jobs-table {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--text-sm);
	}

	.users-table th,
	.users-table td,
	.jobs-table th,
	.jobs-table td {
		text-align: left;
		padding: 0.4rem 0.5rem;
		border-bottom: 1px solid var(--border-subtle);
	}

	.users-table th,
	.jobs-table th {
		color: var(--text-dim);
		font-weight: 500;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.username {
		font-weight: 500;
	}

	.badge {
		display: inline-block;
		margin-left: 0.5rem;
		font-size: var(--text-xs);
		padding: 0.05rem 0.4rem;
		border: 1px solid var(--border-default);
		border-radius: var(--radius-pill);
		color: var(--text-muted);
	}

	.kpi-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
		gap: 0.5rem 1.5rem;
		margin-bottom: 1rem;
	}

	.kpi-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.kpi-value {
		font-size: 1.2rem;
		font-weight: 600;
	}

	.kpi-warn .kpi-value {
		color: #ff9b5a;
	}

	.source-bars {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}

	.source-row {
		display: grid;
		grid-template-columns: 80px 1fr 50px;
		gap: 0.75rem;
		align-items: center;
		font-size: var(--text-sm);
	}

	.source-label {
		color: var(--text-muted);
	}

	.source-bar {
		height: 6px;
		background: var(--surface-base);
		border-radius: 3px;
		overflow: hidden;
	}

	.source-fill {
		height: 100%;
		background: var(--map-path);
	}

	.source-count {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.module-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
		gap: 0.75rem;
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

	.state {
		padding: 1rem;
		font-size: var(--text-sm);
		color: var(--text-muted);
	}

	.state.error { color: #ff5a5a; }
	.state.warn { color: #ff9b5a; font-size: var(--text-xs); }

	.empty {
		font-size: var(--text-sm);
		color: var(--text-muted);
	}

	@media (max-width: 768px) {
		.admin {
			padding: 1rem 0.75rem 3rem;
		}
		.card {
			padding: 0.75rem;
		}
	}

	@media (max-width: 640px) {
		.admin {
			padding: 0.75rem 0.5rem 3rem;
		}
		.card {
			padding: 0.6rem;
		}
	}
</style>
