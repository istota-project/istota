<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';

	let { children } = $props();

	function isActive(path: string): boolean {
		return page.url.pathname.startsWith(`${base}${path}`);
	}
</script>

<div class="page-shell">
	<div class="page-header">
		<div class="sub-nav">
			<a href="{base}/business/invoices" class:active={isActive('/business/invoices')}>Invoices</a>
			<a href="{base}/business/clients" class:active={isActive('/business/clients')}>Clients</a>
			<a href="{base}/business/settings" class:active={isActive('/business/settings')}>Settings</a>
		</div>
	</div>

	<div class="page-body">
		{@render children()}
	</div>
</div>
