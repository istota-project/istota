<script lang="ts">
  import { untrack } from 'svelte';
  import type {
    BriefingBlock,
    BriefingBlockSave,
    BriefingSourceDraft,
    EmailAccount,
  } from '$lib/api';
  import { Trash2 } from '@lucide/svelte';
  import {
    Button,
    ConfirmDialog,
    EmptyState,
    FormModal,
    IconButton,
    Input,
    OptionCards,
    Select,
    TextArea,
    type SelectOption,
    Field,
    Checkbox,
  } from '$lib/components/ui';
  import SourceConfigFields from './SourceConfigFields.svelte';
  import BriefingPreview from './BriefingPreview.svelte';
  import { sourceKindLabel } from './settings/briefingSettings';

  interface Props {
    initial: BriefingBlockSave;
    savedBlocks: BriefingBlock[];
    sourceKinds: string[];
    browseOptions: SelectOption[];
    rssOptions: SelectOption[];
    sharedBlockOptions: SelectOption[];
    feedsAvailable: boolean;
    allowPreview?: boolean;
    allowSourceDisable?: boolean;
    userId?: string;
    fileOptions?: string[];
    calendarOptions?: SelectOption[];
    emailAccounts?: EmailAccount[];
    onSave: (payload: BriefingBlockSave) => Promise<void>;
    onCancel: () => void;
  }
  let {
    initial,
    savedBlocks,
    sourceKinds,
    browseOptions,
    rssOptions,
    sharedBlockOptions,
    feedsAvailable,
    allowPreview = true,
    allowSourceDisable = true,
    userId = '',
    fileOptions = [],
    calendarOptions = [],
    emailAccounts = [],
    onSave,
    onCancel,
  }: Props = $props();

  const original = untrack(() => JSON.parse(JSON.stringify(initial)) as BriefingBlockSave);
  let draft = $state({
    ...original,
    title: original.title ?? '',
    directive: original.directive ?? '',
    render_mode: original.render_mode ?? 'synthesis',
    options: original.options ?? {},
    sources: original.sources ?? [],
  });
  const baseline = untrack(() => JSON.stringify(draft));
  const dirty = $derived(JSON.stringify(draft) !== baseline);
  let saving = $state(false);
  let previewing = $state(false);
  let titleError = $state('');
  let discard = $state(false);
  let open = $state(true);
  const width = $derived.by(() => {
    const value = (draft.options.presentation as { width?: string } | null)?.width;
    return value === 'wide' || value === 'full' ? value : 'auto';
  });
  const kindOptions: SelectOption[] = $derived(
    sourceKinds.map((kind) => ({
      value: kind,
      label: sourceKindLabel(kind),
      disabled: kind === 'rss' && !feedsAvailable,
    })),
  );
  const widths = [
    { value: 'auto', title: 'Automatic', description: 'One column' },
    { value: 'wide', title: 'Wide', description: 'Two columns' },
    { value: 'full', title: 'Full width', description: 'Every column' },
  ];
  const renderOptions = [
    { value: 'synthesis', label: 'Synthesis (summarize)' },
    { value: 'structured', label: 'Structured (verbatim)' },
  ];

  function close() {
    if (dirty) {
      open = true;
      discard = true;
    } else onCancel();
  }

  function setWidth(value: string) {
    const presentation = draft.options.presentation;
    draft.options = {
      ...draft.options,
      presentation: {
        ...(presentation && typeof presentation === 'object' && !Array.isArray(presentation)
          ? presentation
          : {}),
        width: value,
      },
    };
  }

  function patchSource(index: number, patch: Record<string, unknown>) {
    const config = { ...draft.sources[index].config, ...patch };
    for (const [key, value] of Object.entries(patch)) {
      if (value === undefined || value === null) delete config[key];
    }
    draft.sources[index] = { ...draft.sources[index], config };
  }

  function addSource(kind: string) {
    draft.sources = [
      ...draft.sources,
      { kind, config: kind.startsWith('personal_') ? { owner_id: userId } : {}, enabled: true },
    ];
  }

  function setOption(key: string, value: unknown) {
    const options = { ...draft.options };
    if (value === '' || value === undefined) delete options[key];
    else options[key] = value;
    draft.options = options;
  }

  function sourceError(source: BriefingSourceDraft): string {
    if (source.enabled === false) return '';
    const config = source.config ?? {};
    if (source.kind === 'rss' && !config.feed_ref) return 'Choose a feed or category.';
    if (source.kind === 'browse' && !config.preset && !String(config.url ?? '').trim())
      return 'Choose a site or enter a URL.';
    if (['todos', 'notes', 'reminders'].includes(source.kind) && !String(config.path ?? '').trim())
      return 'Enter a file path.';
    if (source.kind === 'shared_block' && !config.name) return 'Choose a shared block.';
    return '';
  }

  async function save() {
    titleError = draft.title.trim() ? '' : 'Title is required.';
    if (titleError) throw new Error(titleError);
    if (!draft.id && draft.sources.length === 0) throw new Error('Add at least one source.');
    for (let index = 0; index < draft.sources.length; index++) {
      const message = sourceError(draft.sources[index]);
      if (message) throw new Error(`Source ${index + 1}: ${message}`);
    }
    const count = draft.options.story_count;
    if (count !== undefined && (!Number.isInteger(count) || Number(count) < 1)) {
      throw new Error('Story count must be a positive whole number.');
    }
    saving = true;
    try {
      const payload = JSON.parse(JSON.stringify(draft)) as BriefingBlockSave;
      payload.title = draft.title.trim();
      // Reading old malformed presentation defaults to auto; new writes are valid.
      const presentation = payload.options?.presentation;
      payload.options = {
        ...payload.options,
        presentation: {
          ...(presentation && typeof presentation === 'object' && !Array.isArray(presentation)
            ? presentation
            : {}),
          width,
        },
      };
      await onSave(payload);
    } finally {
      saving = false;
    }
  }
</script>

<FormModal
  bind:open
  title={draft.id ? 'Edit block' : 'New block'}
  width="680px"
  submitLabel="Save block"
  onSubmit={save}
  onCancel={close}
>
  <fieldset disabled={saving} class="draft-fields">
    <Field label="Title" error={titleError}>
      <Input bind:value={draft.title} />
    </Field>
    <section aria-label="Block sources" class="sources">
      <div class="section-head">
        <h3 class="micro-label">Sources</h3>
        <!-- Remounted after each add: the trigger is an action, so it must go
             back to its placeholder rather than show the kind just picked. -->
        {#key draft.sources.length}
          <Select
            value=""
            options={kindOptions}
            placeholder="Add source"
            ariaLabel="Add source"
            align="end"
            onValueChange={addSource}
          />
        {/key}
      </div>
      {#if draft.sources.length === 0}
        <EmptyState size="sm">No sources yet. Add one to fill this block.</EmptyState>
      {/if}
      {#each draft.sources as source, index}
        <div class="source-entry" class:off={source.enabled === false}>
          <div class="source-head">
            <span class="source-kind">{sourceKindLabel(source.kind)}</span>
            {#if allowSourceDisable || source.enabled === false}
              <Checkbox
                label="Enabled"
                tone="muted"
                checked={source.enabled !== false}
                onchange={(on) => {
                  draft.sources[index] = { ...source, enabled: on };
                }}
              />
            {/if}
            <IconButton
              size="sm"
              danger
              label={`Remove source ${index + 1}`}
              title="Remove source"
              onclick={() => {
                draft.sources = draft.sources.filter((_, i) => i !== index);
              }}><Trash2 size={14} /></IconButton
            >
          </div>
          <div class="source-body">
            <SourceConfigFields
              kind={source.kind}
              config={source.config ?? {}}
              onChange={(patch) => patchSource(index, patch)}
              {browseOptions}
              {rssOptions}
              {sharedBlockOptions}
              {userId}
              {fileOptions}
              {calendarOptions}
              {emailAccounts}
            />
          </div>
        </div>
      {/each}
      {#if !feedsAvailable && sourceKinds.includes('rss')}
        <p class="caption">Enable Feeds and subscribe to a feed first to use a feed source.</p>
      {/if}
    </section>
    <section
      class="width-options vstack"
      style:--vstack-gap="var(--space-2)"
      aria-label="Block width"
    >
      <h3 class="micro-label">Width</h3>
      <OptionCards options={widths} value={width} label="Width" onchange={setWidth} />
      <p class="caption">Applies to future editions.</p>
    </section>
    <details class="advanced">
      <summary>Advanced</summary>
      <div class="advanced-fields">
        <Field label="Directive" hint="How to treat this block's sources.">
          <TextArea bind:value={draft.directive} rows={3} />
        </Field>
        <Field label="Render mode">
          <Select
            value={draft.render_mode}
            options={renderOptions}
            fullWidth
            ariaLabel="Render mode"
            onValueChange={(value) => {
              draft.render_mode = value;
            }}
          />
        </Field>
        <Field label="Story count">
          <Input
            type="number"
            min="1"
            step="1"
            value={(draft.options.story_count as number) ?? ''}
            oninput={(event) =>
              setOption(
                'story_count',
                event.currentTarget.value === '' ? undefined : Number(event.currentTarget.value),
              )}
          />
        </Field>
        <Field label="Tone">
          <Input
            value={(draft.options.tone as string) ?? ''}
            oninput={(event) => setOption('tone', event.currentTarget.value)}
          />
        </Field>
      </div>
    </details>
  </fieldset>
  {#snippet extraActions()}
    {#if allowPreview}
      <Button
        variant="secondary"
        onclick={() => {
          previewing = true;
        }}
        disabled={saving}>Preview</Button
      >
    {/if}
  {/snippet}
</FormModal>

{#if previewing}
  <BriefingPreview
    briefingName={draft.briefing_name ?? ''}
    blocks={savedBlocks}
    {draft}
    onClose={() => {
      previewing = false;
    }}
  />
{/if}

{#if discard}
  <ConfirmDialog
    open={true}
    title="Discard block changes"
    message="Are you sure you want to discard your unsaved block changes?"
    confirmLabel="Discard changes"
    cancelLabel="Keep editing"
    onConfirm={onCancel}
    onCancel={() => {
      discard = false;
      open = true;
    }}
  />
{/if}

<style>
  .draft-fields,
  .sources,
  .advanced-fields {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
  }
  .draft-fields {
    gap: var(--space-6);
    border: 0;
    padding: 0;
    margin: 0;
  }
  h3,
  p {
    margin: 0;
  }
  .section-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
  }
  .sources {
    gap: var(--space-2);
  }
  .source-entry {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    background: var(--surface-card);
    min-width: 0;
  }
  .source-head {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-2) var(--space-2) var(--space-3);
  }
  .source-kind {
    flex: 1;
    min-width: 0;
    font-size: var(--text-sm);
    font-weight: var(--weight-semibold);
  }
  .source-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding: 0 var(--space-3);
  }
  /* A kind with no settings (markets, calendar) leaves the card as its header. */
  .source-body:not(:empty) {
    padding-bottom: var(--space-3);
  }
  .source-entry.off .source-kind,
  .source-entry.off .source-body {
    opacity: 0.6;
  }
  /* Three short choices sit side by side rather than as three tall cards. */
  @media (min-width: 641px) {
    .width-options :global(.option-cards) {
      flex-direction: row;
    }
    .width-options :global(.option-card) {
      flex: 1;
    }
  }
  .advanced {
    border-top: 1px solid var(--border-subtle);
    padding-top: var(--space-4);
  }
  summary {
    cursor: pointer;
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  .advanced-fields {
    margin-top: var(--space-3);
  }
</style>
