<script lang="ts">
	import { onMount } from 'svelte';
	import { ChevronLeft, ChevronRight } from 'lucide-svelte';

	let {
		images = [],
		index = null,
		onClose,
	}: {
		images?: string[];
		index?: number | null;
		onClose: () => void;
	} = $props();

	let current = $state<number | null>(null);
	$effect(() => {
		current = index;
	});

	function next(e?: Event) {
		e?.stopPropagation();
		if (current === null || images.length === 0) return;
		current = (current + 1) % images.length;
	}

	function prev(e?: Event) {
		e?.stopPropagation();
		if (current === null || images.length === 0) return;
		current = (current - 1 + images.length) % images.length;
	}

	function handleKeydown(e: KeyboardEvent) {
		if (current === null) return;
		if (e.key === 'Escape') onClose();
		else if (e.key === 'ArrowRight') next();
		else if (e.key === 'ArrowLeft') prev();
	}

	onMount(() => {
		document.addEventListener('keydown', handleKeydown);
		return () => document.removeEventListener('keydown', handleKeydown);
	});
</script>

{#if current !== null && images.length > 0}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="lightbox open" onclick={onClose}>
		<img src={images[current]} alt="" />
		{#if images.length > 1}
			<button class="nav prev" onclick={prev} aria-label="Previous image">
				<ChevronLeft size={32} />
			</button>
			<button class="nav next" onclick={next} aria-label="Next image">
				<ChevronRight size={32} />
			</button>
			<div class="counter">{current + 1} / {images.length}</div>
		{/if}
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
	.nav {
		position: absolute;
		top: 50%;
		transform: translateY(-50%);
		display: flex;
		align-items: center;
		justify-content: center;
		width: 3rem;
		height: 3rem;
		border: none;
		border-radius: 50%;
		background: rgba(0, 0, 0, 0.5);
		color: #fff;
		cursor: pointer;
		transition: background 120ms;
	}
	.nav:hover {
		background: rgba(0, 0, 0, 0.75);
	}
	.nav.prev {
		left: 1rem;
	}
	.nav.next {
		right: 1rem;
	}
	.counter {
		position: absolute;
		bottom: 1rem;
		left: 50%;
		transform: translateX(-50%);
		padding: 0.25rem 0.6rem;
		background: rgba(0, 0, 0, 0.5);
		color: #fff;
		font-size: 0.8rem;
		border-radius: 0.25rem;
		pointer-events: none;
	}
</style>
