<script lang="ts">
	import { Button } from '$lib/components/ui';
	import { setSecret, deleteSecret } from '$lib/api';

	interface Props {
		service: string;
		fieldKey: string;
		label: string;
		type?: 'text' | 'email' | 'password' | 'url';
		configured: boolean;
		onSaved?: () => void;
		onDeleted?: () => void;
	}

	let {
		service,
		fieldKey,
		label,
		type = 'password',
		configured,
		onSaved,
		onDeleted,
	}: Props = $props();

	let value = $state('');
	let saving = $state(false);
	let savedFlash = $state(false);
	let saveError = $state('');
	let confirmingClear = $state(false);

	async function save() {
		if (!value) return;
		saving = true;
		saveError = '';
		try {
			await setSecret(service, fieldKey, value);
			value = '';
			savedFlash = true;
			setTimeout(() => {
				savedFlash = false;
			}, 1500);
			onSaved?.();
		} catch (e) {
			saveError = e instanceof Error ? e.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	async function clear() {
		confirmingClear = false;
		saving = true;
		saveError = '';
		try {
			await deleteSecret(service, fieldKey);
			onDeleted?.();
		} catch (e) {
			saveError = e instanceof Error ? e.message : 'Delete failed';
		} finally {
			saving = false;
		}
	}
</script>

<label class="secret-field">
	<span class="secret-label">{label}</span>
	<div class="secret-row">
		<input
			class="secret-input"
			{type}
			autocomplete="new-password"
			placeholder={configured ? '•••• stored — enter to replace' : 'Enter value'}
			bind:value
			disabled={saving}
		/>
		<Button variant="primary" size="sm" disabled={saving || !value} onclick={save}>
			{saving ? 'Saving…' : 'Save'}
		</Button>
		{#if configured}
			<button
				class="secret-clear"
				title="Clear stored value"
				type="button"
				disabled={saving}
				onclick={() => (confirmingClear = true)}>×</button
			>
		{/if}
	</div>
	{#if savedFlash}
		<span class="secret-flash">Saved.</span>
	{/if}
	{#if saveError}
		<span class="secret-error">{saveError}</span>
	{/if}
	{#if confirmingClear}
		<div class="secret-confirm">
			<span>Clear stored value?</span>
			<Button variant="ghost" size="sm" onclick={() => (confirmingClear = false)}>
				Cancel
			</Button>
			<Button variant="primary" size="sm" onclick={clear}>Clear</Button>
		</div>
	{/if}
</label>

<style>
	.secret-field {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: var(--text-sm);
	}

	.secret-label {
		color: var(--text-muted);
	}

	.secret-row {
		display: flex;
		gap: 0.4rem;
		align-items: center;
	}

	.secret-input {
		background: var(--surface-base);
		color: var(--text-primary);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		padding: 0.3rem 0.5rem;
		font: inherit;
		font-size: var(--text-sm);
		width: 100%;
		max-width: 24rem;
		min-width: 0;
		box-sizing: border-box;
	}

	.secret-input:focus {
		outline: 1px solid var(--accent, #6c8ebf);
	}

	.secret-clear {
		background: transparent;
		border: none;
		color: var(--text-dim);
		cursor: pointer;
		padding: 0.1rem 0.35rem;
		border-radius: 0.2rem;
		font: inherit;
		font-size: var(--text-base);
		line-height: 1;
	}

	.secret-clear:hover:not(:disabled) {
		color: #e88;
		background: var(--surface-raised);
	}

	.secret-clear:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	.secret-flash {
		font-size: var(--text-xs);
		color: #6eb884;
	}

	.secret-error {
		font-size: var(--text-xs);
		color: #e88;
	}

	.secret-confirm {
		display: flex;
		gap: 0.4rem;
		align-items: center;
		font-size: var(--text-xs);
		color: var(--text-muted);
		margin-top: 0.2rem;
	}
</style>
