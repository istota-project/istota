<script lang="ts">
	import { SendHorizontal, Square } from 'lucide-svelte';

	let {
		onSend,
		onCancel,
		busy = false,
		placeholder = 'Message Istota…',
	}: {
		onSend: (text: string) => void;
		onCancel?: () => void;
		busy?: boolean;
		placeholder?: string;
	} = $props();

	let text = $state('');
	let textarea: HTMLTextAreaElement | undefined = $state();

	function autoGrow() {
		if (!textarea) return;
		textarea.style.height = 'auto';
		textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
	}

	function submit() {
		const t = text.trim();
		if (!t) return;
		onSend(t);
		text = '';
		queueMicrotask(autoGrow);
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			submit();
		}
	}
</script>

<div class="composer">
	<textarea
		bind:this={textarea}
		bind:value={text}
		oninput={autoGrow}
		onkeydown={onKeydown}
		{placeholder}
		rows="1"
		aria-label="Message"
	></textarea>
	{#if busy && onCancel}
		<button class="send-btn stop" onclick={onCancel} type="button" aria-label="Stop" title="Stop">
			<Square size={16} />
		</button>
	{:else}
		<button
			class="send-btn"
			onclick={submit}
			type="button"
			disabled={!text.trim()}
			aria-label="Send"
			title="Send"
		>
			<SendHorizontal size={16} />
		</button>
	{/if}
</div>

<style>
	.composer {
		display: flex;
		align-items: flex-end;
		gap: 0.5rem;
		padding: 0.6rem 0.75rem;
		border-top: 1px solid var(--border-subtle);
		background: var(--surface-base);
	}
	textarea {
		flex: 1;
		resize: none;
		max-height: 200px;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-base);
		line-height: 1.4;
		padding: 0.5rem 0.65rem;
		outline: none;
	}
	textarea:focus { border-color: var(--text-dim); }
	.send-btn {
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		border-radius: var(--radius-card);
		border: 1px solid var(--border-default);
		background: var(--surface-raised);
		color: var(--text-primary);
		cursor: pointer;
		transition: background var(--transition-fast), color var(--transition-fast);
	}
	.send-btn:hover:not(:disabled) { background: var(--surface-badge); color: var(--accent-hover); }
	.send-btn:disabled { opacity: 0.4; cursor: default; }
	.send-btn.stop { color: #e0a0a0; }
</style>
