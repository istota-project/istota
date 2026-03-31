<script lang="ts">
	import { onMount } from 'svelte';

	let { src = '', onClose }: { src: string; onClose: () => void } = $props();

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') onClose();
	}

	onMount(() => {
		document.addEventListener('keydown', handleKeydown);
		return () => document.removeEventListener('keydown', handleKeydown);
	});
</script>

{#if src}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="lightbox open" onclick={onClose}>
		<img {src} alt="" />
	</div>
{/if}

<style>
	.lightbox {
		position: fixed;
		inset: 0;
		z-index: 100;
		background: rgba(0, 0, 0, 0.9);
		display: flex;
		justify-content: center;
		align-items: center;
		cursor: zoom-out;
	}
	.lightbox img {
		max-width: 90vw;
		max-height: 90vh;
		object-fit: contain;
	}
</style>
