<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';

	let { children } = $props();

	function isActive(path: string): boolean {
		return page.url.pathname.startsWith(`${base}${path}`);
	}
</script>

<div class="money-section-header">
	<div class="money-section-nav">
		<a href="{base}/money/business/invoices" class:active={isActive('/money/business/invoices')}>Invoices</a>
		<a href="{base}/money/business/clients" class:active={isActive('/money/business/clients')}>Clients</a>
		<a href="{base}/money/business/settings" class:active={isActive('/money/business/settings')}>Settings</a>
	</div>
</div>

<div class="money-section-body">
	{@render children()}
</div>
