<script lang="ts">
  import { untrack } from 'svelte';
  import type { BriefingBlock, BriefingBlockSave, BriefingSourceDraft } from '$lib/api';
  import {
    Button,
    ConfirmDialog,
    EmptyState,
    Input,
    Modal,
    OptionCards,
    Select,
    TextArea,
    type SelectOption,
  } from '$lib/components/ui';
  import { SettingsField } from '$lib/components/settings';
  import SourceConfigFields from './SourceConfigFields.svelte';
  import BriefingPreview from './BriefingPreview.svelte';

  interface Props {
    initial: BriefingBlockSave;
    savedBlocks: BriefingBlock[];
    sourceKinds: string[];
    browseOptions: SelectOption[];
    rssOptions: SelectOption[];
    sharedBlockOptions: SelectOption[];
    feedsAvailable: boolean;
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
  let error = $state('');
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
      label: kind,
      disabled: kind === 'rss' && !feedsAvailable,
    })),
  );
  const widths = [
    { value: 'auto', title: 'Automatic', description: 'One column when space allows.' },
    { value: 'wide', title: 'Wide', description: 'Two columns when space allows.' },
    { value: 'full', title: 'Full width', description: 'Use every available column.' },
  ];
  const renderOptions = [
    { value: 'synthesis', label: 'Synthesis (summarize)' },
    { value: 'structured', label: 'Structured (verbatim)' },
  ];

  function close() {
    if (saving) {
      open = true;
      return;
    }
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
    if (saving) return;
    error = '';
    titleError = draft.title.trim() ? '' : 'Title is required.';
    if (titleError) {
      error = titleError;
      return;
    }
    if (!draft.id && draft.sources.length === 0) {
      error = 'Add at least one source.';
      return;
    }
    for (let index = 0; index < draft.sources.length; index++) {
      const message = sourceError(draft.sources[index]);
      if (message) {
        error = `Source ${index + 1}: ${message}`;
        return;
      }
    }
    const count = draft.options.story_count;
    if (count !== undefined && (!Number.isInteger(count) || Number(count) < 1)) {
      error = 'Story count must be a positive whole number.';
      return;
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
    } catch (e) {
      error = e instanceof Error ? e.message : 'Could not save block.';
    } finally {
      saving = false;
    }
  }
</script>

<Modal
  bind:open
  title={draft.id ? 'Edit block' : 'New block'}
  width="680px"
  dismissible={!saving}
  onOpenChange={(next) => {
    if (!next) close();
  }}
>
  <fieldset disabled={saving} class="draft-fields">
    <SettingsField label="Title" error={titleError}>
      <Input bind:value={draft.title} />
    </SettingsField>
    <section aria-label="Block sources" class="sources">
      <h3 class="micro-label">Sources</h3>
      {#if draft.sources.length === 0}<EmptyState size="sm">No sources</EmptyState>{/if}
      {#each draft.sources as source, index}
        <div class="source-entry">
          <div class="card-head source-head">
            <strong>Source {index + 1}</strong>
            <Button
              size="sm"
              variant="danger"
              onclick={() => {
                draft.sources = draft.sources.filter((_, i) => i !== index);
              }}
              ariaLabel={`Remove source ${index + 1}`}>Remove</Button
            >
          </div>
          <SettingsField label="Source type" labelled={false}>
            <Select
              value={source.kind}
              options={kindOptions}
              fullWidth
              ariaLabel={`Source ${index + 1} type`}
              onValueChange={(kind) => {
                draft.sources[index] = { ...source, kind, config: {} };
              }}
            />
          </SettingsField>
          <SettingsField label="Enabled" checkbox>
            <input
              type="checkbox"
              checked={source.enabled !== false}
              onchange={(event) => {
                draft.sources[index] = { ...source, enabled: event.currentTarget.checked };
              }}
            />
          </SettingsField>
          <SourceConfigFields
            kind={source.kind}
            config={source.config ?? {}}
            onChange={(patch) => patchSource(index, patch)}
            {browseOptions}
            {rssOptions}
            {sharedBlockOptions}
          />
        </div>
      {/each}
      <Select
        value=""
        options={kindOptions}
        placeholder="Add source…"
        ariaLabel="Add source"
        onValueChange={(kind) => {
          draft.sources = [...draft.sources, { kind, config: {}, enabled: true }];
        }}
      />
      {#if !feedsAvailable}<p class="caption">
          Enable Feeds and subscribe to a feed first to use a feed source.
        </p>{/if}
    </section>
    <section class="width-options" aria-label="Block width">
      <h3 class="micro-label">Width</h3>
      <OptionCards options={widths} value={width} label="Width" onchange={setWidth} />
      <p class="caption">Layout changes apply to future editions.</p>
    </section>
    <details class="advanced">
      <summary>Advanced</summary>
      <div class="advanced-fields">
        <SettingsField label="Directive" hint="How to treat this block's sources.">
          <TextArea bind:value={draft.directive} rows={3} />
        </SettingsField>
        <SettingsField label="Render mode" labelled={false}>
          <Select
            value={draft.render_mode}
            options={renderOptions}
            fullWidth
            ariaLabel="Render mode"
            onValueChange={(value) => {
              draft.render_mode = value;
            }}
          />
        </SettingsField>
        <SettingsField label="Story count">
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
        </SettingsField>
        <SettingsField label="Tone">
          <Input
            value={(draft.options.tone as string) ?? ''}
            oninput={(event) => setOption('tone', event.currentTarget.value)}
          />
        </SettingsField>
      </div>
    </details>
  </fieldset>
  {#if error}<div role="alert" class="banner error">{error}</div>{/if}
  {#snippet footer()}
    <Button
      variant="secondary"
      onclick={() => {
        previewing = true;
      }}
      disabled={saving}>Preview</Button
    >
    <Button variant="secondary" onclick={close} disabled={saving}>Cancel</Button>
    <Button variant="primary" onclick={save} loading={saving} loadingLabel="Saving…"
      >Save block</Button
    >
  {/snippet}
</Modal>

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
    border: 0;
    padding: 0;
    margin: 0;
  }
  h3,
  p {
    margin: 0;
  }
  .source-entry {
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3);
    min-width: 0;
  }
  .source-head {
    align-items: center;
    margin-bottom: var(--space-2);
  }
  .source-head strong,
  summary {
    font-size: var(--text-sm);
  }
  .width-options {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  .advanced {
    border-top: 1px solid var(--border-subtle);
    padding-top: var(--space-3);
  }
  summary {
    cursor: pointer;
  }
  .advanced-fields {
    margin-top: var(--space-3);
  }
</style>
