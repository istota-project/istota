<script lang="ts">
	// Test-only harness: reproduces the /chat page's keyed `{#each}` over the
	// messages store, so a component test can prove that a live (in-place) stream
	// update reaches the DOM. Not imported by any route, so it's tree-shaken from
	// the production build.
	import type { Writable } from 'svelte/store';
	import type { ChatMessage } from '$lib/stores/chat';
	import Message from './Message.svelte';

	let { store }: { store: Writable<ChatMessage[]> } = $props();
</script>

{#each $store as message (message.cid)}
	<Message {message} onConfirm={() => {}} onReject={() => {}} />
{/each}
