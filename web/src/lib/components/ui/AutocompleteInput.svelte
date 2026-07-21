<script module lang="ts">
	let _acUid = 0;
</script>

<script lang="ts">
	// A text input with a suggestion dropdown. The menu is position:fixed,
	// anchored to the input's live rect, so it matches the input's width, drops
	// directly from its bottom, and escapes any ancestor overflow:hidden
	// clipping. Controlled (pass `value` + `onValueChange`) or bindable
	// (`bind:value`). Backend/async validation is the caller's job via
	// `onCommit` (fired on blur).

	interface Props {
		/** Current text value. Bindable, or pair with onValueChange for controlled use. */
		value?: string;
		/** Suggestion pool. */
		options: string[];
		placeholder?: string;
		disabled?: boolean;
		/** Marks the field invalid (aria-invalid + a styling hook). */
		invalid?: boolean;
		/** Render the dropdown options in a monospace face (paths, tokens, ids). */
		monospace?: boolean;
		ariaLabel?: string;
		id?: string;
		/** Max suggestions shown after filtering. */
		limit?: number;
		/** Custom filter; default is a case-insensitive substring match. */
		filter?: (options: string[], query: string) => string[];
		/** Fired on every edit and when a suggestion is chosen. */
		onValueChange?: (value: string) => void;
		/** Fired on blur — the hook for validation. */
		onCommit?: (value: string) => void;
	}

	let {
		value = $bindable(''),
		options,
		placeholder,
		disabled = false,
		invalid = false,
		monospace = false,
		ariaLabel,
		id,
		limit = 50,
		filter,
		onValueChange,
		onCommit,
	}: Props = $props();

	const menuId = `ac-menu-${(_acUid += 1)}`;

	let inputEl = $state<HTMLInputElement | null>(null);
	let open = $state(false);
	let highlight = $state(-1);
	let menuStyle = $state('');

	function defaultFilter(opts: string[], q: string): string[] {
		const needle = q.trim().toLowerCase();
		return needle ? opts.filter((o) => o.toLowerCase().includes(needle)) : opts;
	}

	function matches(): string[] {
		return (filter ?? defaultFilter)(options, value).slice(0, limit);
	}

	function position() {
		if (!inputEl) return;
		const r = inputEl.getBoundingClientRect();
		menuStyle = `top:${r.bottom + 2}px; left:${r.left}px; width:${r.width}px;`;
	}

	function openMenu() {
		if (disabled || !options.length) return;
		highlight = -1;
		position();
		open = true;
	}

	function closeMenu() {
		open = false;
		highlight = -1;
	}

	function emit(v: string) {
		value = v;
		onValueChange?.(v);
	}

	function choose(v: string) {
		emit(v);
		closeMenu();
		inputEl?.focus();
	}

	function onInput(e: Event) {
		emit((e.target as HTMLInputElement).value);
		openMenu();
	}

	function onKeydown(e: KeyboardEvent) {
		const items = matches();
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			if (!open) return openMenu();
			highlight = Math.min(highlight + 1, items.length - 1);
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			highlight = Math.max(highlight - 1, 0);
		} else if (e.key === 'Enter') {
			if (open && highlight >= 0 && items[highlight]) {
				e.preventDefault();
				choose(items[highlight]);
			}
		} else if (e.key === 'Escape') {
			closeMenu();
		}
	}

	function onBlur() {
		closeMenu();
		onCommit?.(value);
	}

	// Keep the fixed menu glued to the input while the page scrolls / resizes.
	$effect(() => {
		if (!open) return;
		const reflow = () => position();
		window.addEventListener('scroll', reflow, true);
		window.addEventListener('resize', reflow);
		return () => {
			window.removeEventListener('scroll', reflow, true);
			window.removeEventListener('resize', reflow);
		};
	});
</script>

<div class="ac-field">
	<input
		bind:this={inputEl}
		{id}
		{placeholder}
		{disabled}
		{value}
		type="text"
		autocomplete="off"
		role="combobox"
		aria-expanded={open}
		aria-controls={menuId}
		aria-autocomplete="list"
		aria-label={ariaLabel}
		aria-invalid={invalid ? 'true' : undefined}
		oninput={onInput}
		onfocus={openMenu}
		onblur={onBlur}
		onkeydown={onKeydown}
	/>
	{#if open}
		{@const items = matches()}
		{#if items.length}
			<ul id={menuId} class="ac-menu" role="listbox" style={menuStyle}>
				{#each items as opt, i (opt)}
					<li>
						<button
							type="button"
							role="option"
							aria-selected={i === highlight}
							class="ac-option"
							class:mono={monospace}
							class:active={i === highlight}
							onmousedown={(e) => {
								e.preventDefault();
								choose(opt);
							}}
							onmouseenter={() => (highlight = i)}
						>
							{opt}
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	{/if}
</div>

<style>
	.ac-field {
		position: relative;
	}

	.ac-menu {
		position: fixed;
		z-index: 50;
		margin: 0;
		padding: 0.25rem;
		list-style: none;
		max-height: 12rem;
		overflow-y: auto;
		background: var(--surface-overlay, var(--surface-card));
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card, 0.5rem);
		box-shadow: var(--shadow-md, 0 8px 24px rgba(0, 0, 0, 0.4));
	}

	.ac-option {
		display: block;
		width: 100%;
		text-align: left;
		padding: 0.35rem 0.5rem;
		border: none;
		border-radius: var(--radius-sm, 0.3rem);
		background: none;
		color: var(--text-secondary);
		font-size: var(--text-sm);
		cursor: pointer;
	}

	.ac-option.mono {
		font-family: var(--font-mono, ui-monospace, SFMono-Regular, monospace);
	}

	.ac-option.active,
	.ac-option:hover {
		background: var(--surface-hover, var(--surface-raised));
		color: var(--text-primary);
	}
</style>
