<script lang="ts">
	import { getAccounts, type AccountRow } from '$lib/money/api';
	import { selectedAccount, selectedYear, filterText } from '$lib/money/stores/transactions';
	import { selectedLedger } from '$lib/money/stores/ledger';
	import { Sidebar, SidebarToggle, Select } from '$lib/components/ui';

	let { children } = $props();

	let accounts: AccountRow[] = $state([]);
	let sidebarOpen = $state(false);
	let filterTimeout: ReturnType<typeof setTimeout> | null = null;

	const currentYear = new Date().getFullYear();
	const years = Array.from({ length: 11 }, (_, i) => currentYear - i);

	interface AccountNode {
		name: string;
		fullName: string;
		children: AccountNode[];
	}

	let tree: AccountNode[] = $state([]);
	let expandedSet = $state(new Set<string>());

	function buildTree(rows: AccountRow[]): AccountNode[] {
		const root: AccountNode[] = [];
		const nodeMap = new Map<string, AccountNode>();

		for (const row of rows) {
			const parts = row.account.split(':');
			let current = root;
			let path = '';

			for (let i = 0; i < parts.length; i++) {
				path = path ? `${path}:${parts[i]}` : parts[i];
				let node = nodeMap.get(path);
				if (!node) {
					node = { name: parts[i], fullName: path, children: [] };
					nodeMap.set(path, node);
					current.push(node);
				}
				current = node.children;
			}
		}

		for (const node of root) {
			expandedSet.add(node.fullName);
		}

		return root;
	}

	function toggleExpand(e: MouseEvent, fullName: string) {
		e.stopPropagation();
		const next = new Set(expandedSet);
		if (next.has(fullName)) {
			next.delete(fullName);
		} else {
			next.add(fullName);
		}
		expandedSet = next;
	}

	function selectAccount(fullName: string) {
		selectedAccount.update((current) => (current === fullName ? '' : fullName));
		sidebarOpen = false;
	}

	function handleFilterInput(value: string) {
		if (filterTimeout) clearTimeout(filterTimeout);
		filterTimeout = setTimeout(() => filterText.set(value), 300);
	}

	async function loadAccounts(ledger: string) {
		expandedSet = new Set<string>();
		try {
			const resp = await getAccounts({ ledger: ledger || undefined });
			accounts = resp.accounts;
			tree = buildTree(accounts);
		} catch {
			// page handles its own loading/error
		}
	}

	let prevLedger = $state($selectedLedger);

	$effect(() => {
		loadAccounts($selectedLedger);
	});

	$effect(() => {
		const current = $selectedLedger;
		if (current !== prevLedger) {
			prevLedger = current;
			selectedAccount.set('');
		}
	});

	const yearOptions = $derived([
		{ value: '', label: 'All years' },
		...years.map((y) => ({ value: String(y), label: String(y) })),
	]);

	const selectedYearValue = $derived($selectedYear ? String($selectedYear) : '');
</script>

<div class="money-section-header">
	{#if $selectedAccount}
		<button class="active-filter" onclick={() => selectedAccount.set('')} type="button">
			{$selectedAccount} <span class="clear">&times;</span>
		</button>
	{/if}
	<div class="money-section-tools">
		<Select
			value={selectedYearValue}
			options={yearOptions}
			onValueChange={(v) => selectedYear.set(v === '' ? 0 : Number(v))}
			ariaLabel="Year"
		/>
		<input
			type="text"
			class="money-control-input"
			placeholder="Filter by tag, payee..."
			value={$filterText}
			oninput={(e) => handleFilterInput(e.currentTarget.value)}
		/>
		<SidebarToggle
			open={sidebarOpen}
			label="Accounts"
			onclick={() => (sidebarOpen = !sidebarOpen)}
		/>
	</div>
</div>

<div class="txn-body">
	<Sidebar
		title="Accounts"
		count={accounts.length}
		open={sidebarOpen}
		onClose={() => (sidebarOpen = false)}
	>
		{#each tree as node (node.fullName)}
			{@render accountNode(node, 0)}
		{/each}
	</Sidebar>

	<div class="txn-main">
		{@render children()}
	</div>
</div>

{#snippet accountNode(node: AccountNode, depth: number)}
	<div class="acct-row" style="padding-left: {0.5 + depth * 0.75}rem">
		{#if node.children.length > 0}
			<button class="acct-caret" onclick={(e) => toggleExpand(e, node.fullName)} type="button">
				<span class="caret" class:open={expandedSet.has(node.fullName)}>&#9654;</span>
			</button>
		{:else}
			<span class="caret-spacer"></span>
		{/if}
		<button
			class="acct-btn"
			class:selected={$selectedAccount === node.fullName}
			onclick={() => selectAccount(node.fullName)}
			type="button"
		>
			{node.name}
		</button>
	</div>
	{#if node.children.length > 0 && expandedSet.has(node.fullName)}
		{#each node.children as child (child.fullName)}
			{@render accountNode(child, depth + 1)}
		{/each}
	{/if}
{/snippet}

<style>
	.active-filter {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		background: var(--surface-raised);
		border: none;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-xs);
		padding: 0.2rem 0.55rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
		transition: background var(--transition-fast);
	}

	.active-filter:hover {
		background: var(--border-default);
	}

	.active-filter .clear {
		color: var(--text-dim);
		font-size: var(--text-sm);
		line-height: 1;
	}

	.txn-body {
		display: flex;
		flex: 1;
		min-height: 0;
		position: relative;
	}

	.acct-row {
		display: flex;
		align-items: center;
	}

	.acct-caret {
		background: none;
		border: none;
		color: var(--text-dim);
		cursor: pointer;
		padding: 0.15rem 0.25rem;
		display: inline-flex;
		align-items: center;
		flex-shrink: 0;
	}

	.caret {
		font-size: 0.45rem;
		display: inline-block;
		transition: transform var(--transition-fast);
	}

	.caret.open {
		transform: rotate(90deg);
	}

	.caret-spacer {
		width: 1.15rem;
		display: inline-block;
		flex-shrink: 0;
	}

	.acct-btn {
		display: block;
		flex: 1;
		min-width: 0;
		background: none;
		border: none;
		color: var(--text-secondary);
		font: inherit;
		font-size: var(--text-sm);
		cursor: pointer;
		padding: 0.2rem 0.5rem;
		border-radius: 0.3rem;
		text-align: left;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		transition: background var(--transition-fast);
	}

	.acct-btn:hover {
		background: var(--surface-raised);
	}

	.acct-btn.selected {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.txn-main {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}
</style>
