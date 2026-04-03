<script lang="ts">
	import { onMount } from 'svelte';
	import { getMoneymanLedgers, getMoneymanFava } from '$lib/api';
	import { selectedService, type ServiceDetail } from '$lib/stores/services';

	let { children } = $props();

	let services: ServiceDetail[] = $state([]);
	let selected: string | null = $state(null);
	let sidebarOpen = $state(false);

	onMount(async () => {
		const svc: ServiceDetail = {
			id: 'fava',
			name: 'Fava',
			description: 'Beancount ledger viewer',
			status: 'loading',
			detail: null,
		};
		services = [svc];
		selected = 'fava';
		selectedService.set(svc);

		try {
			const [ledgerData, favaData] = await Promise.all([
				getMoneymanLedgers(),
				getMoneymanFava(),
			]);
			svc.detail = { ledgers: ledgerData.ledgers, favaPrefix: favaData.prefix };
			svc.status = 'active';
		} catch {
			svc.status = 'error';
		}
		services = [...services];
		selectedService.set({ ...svc });
	});

	function handleServiceClick(id: string) {
		if (selected === id) {
			selected = null;
			selectedService.set(null);
		} else {
			selected = id;
			const svc = services.find((s) => s.id === id) ?? null;
			selectedService.set(svc);
		}
		sidebarOpen = false;
	}
</script>

<div class="svc-shell">
	<div class="svc-header">
		<h1>Services</h1>
		<button class="sidebar-toggle" onclick={() => (sidebarOpen = !sidebarOpen)} type="button">
			{sidebarOpen ? 'Close' : 'Services'} ({services.length})
		</button>
	</div>

	<div class="svc-body">
		<aside class="svc-sidebar" class:open={sidebarOpen}>
			<div class="sidebar-header">
				<span class="sidebar-title">Services</span>
				<span class="sidebar-count">{services.length}</span>
			</div>
			<div class="sidebar-list">
				{#each services as svc}
					<button
						class="svc-btn"
						class:active={selected === svc.id}
						onclick={() => handleServiceClick(svc.id)}
						type="button"
					>
						<span class="svc-name">{svc.name}</span>
						<span class="svc-status" class:ok={svc.status === 'active'} class:err={svc.status === 'error'}></span>
					</button>
				{/each}
			</div>
		</aside>

		<div class="svc-main">
			{@render children()}
		</div>
	</div>
</div>

<style>
	.svc-shell {
		display: flex;
		flex-direction: column;
		margin: -1.5rem;
		height: calc(100vh - 42px);
		overflow: hidden;
	}

	.svc-header {
		display: flex;
		align-items: baseline;
		gap: 1rem;
		padding: 0.75rem 1.5rem;
		border-bottom: 1px solid var(--border-subtle);
		flex-shrink: 0;
	}

	.svc-header h1 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0;
	}

	.sidebar-toggle {
		display: none;
		margin-left: auto;
		background: var(--surface-card);
		border: none;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.25rem 0.6rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
	}

	.svc-body {
		display: flex;
		flex: 1;
		min-height: 0;
	}

	.svc-sidebar {
		width: 200px;
		flex-shrink: 0;
		border-right: 1px solid var(--border-subtle);
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	.sidebar-header {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
		padding: 0.6rem 1rem 0.6rem 1.5rem;
		flex-shrink: 0;
	}

	.sidebar-title {
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.sidebar-count {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.sidebar-list {
		flex: 1;
		overflow-y: auto;
		padding: 0 0.5rem 0.5rem;
	}

	.sidebar-list::-webkit-scrollbar { width: 4px; }
	.sidebar-list::-webkit-scrollbar-track { background: transparent; }
	.sidebar-list::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 2px; }

	.svc-btn {
		display: flex;
		justify-content: space-between;
		align-items: center;
		width: 100%;
		background: none;
		border: none;
		color: inherit;
		font: inherit;
		cursor: pointer;
		padding: 0.3rem 1rem;
		border-radius: 0.3rem;
		transition: background var(--transition-fast);
		text-align: left;
	}

	.svc-btn:hover { background: var(--surface-raised); }
	.svc-btn.active { background: var(--surface-raised); color: var(--text-primary); }

	.svc-name {
		font-size: var(--text-sm);
	}

	.svc-status {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--text-dim);
		flex-shrink: 0;
	}

	.svc-status.ok { background: #4a8; }
	.svc-status.err { background: #c66; }

	.svc-main {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		overflow-y: auto;
	}

	@media (max-width: 768px) {
		.svc-shell {
			margin: -1rem -0.75rem;
			height: calc(100vh - 36px);
		}

		.svc-header { padding: 0.5rem 0.75rem; }

		.sidebar-toggle { display: block; }

		.svc-sidebar {
			display: none;
			position: absolute;
			top: 0;
			left: 0;
			bottom: 0;
			z-index: 20;
			width: 220px;
			background: var(--surface-base);
			border-right: 1px solid var(--border-default);
		}

		.svc-sidebar.open { display: flex; }
		.svc-body { position: relative; }
	}
</style>
