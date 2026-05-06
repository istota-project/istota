<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		description?: string;
		actions?: Snippet;
		children: Snippet;
	}

	let { title, description, actions, children }: Props = $props();
</script>

<section class="card">
	<header class="section-header">
		<h2>{title}</h2>
		{#if actions}
			<div class="header-actions">{@render actions()}</div>
		{/if}
	</header>
	{#if description}<p class="hint">{description}</p>{/if}
	{@render children()}
</section>
