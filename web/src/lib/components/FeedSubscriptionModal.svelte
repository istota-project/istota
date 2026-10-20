<script lang="ts" module>
  export type FeedDraft = {
    url: string;
    title: string;
    category: string;
    poll_interval_minutes: string; // raw input — empty = inherit default
  };
</script>

<script lang="ts">
  import { untrack } from 'svelte';
  import { Field, FormModal, Input, Select, type SelectOption } from '$lib/components/ui';

  /**
   * Add or edit one feeds subscription. The two were near-identical modals on
   * the settings page; they differ only in wording, which `mode` picks. The
   * page validates and applies the draft in `onSubmit`, throwing to refuse it.
   */
  interface Props {
    mode: 'add' | 'edit';
    initial: FeedDraft;
    categoryOptions: SelectOption[];
    defaultInterval: number;
    onSubmit: (draft: FeedDraft) => void;
    onCancel: () => void;
  }

  let { mode, initial, categoryOptions, defaultInterval, onSubmit, onCancel }: Props = $props();

  // Mounted per open, so the opening draft is the whole story.
  let draft = $state<FeedDraft>(untrack(() => ({ ...initial })));
  const adding = $derived(mode === 'add');
</script>

<FormModal
  open={true}
  title={adding ? 'Add subscription' : 'Edit subscription'}
  submitLabel={adding ? 'Add' : 'Save'}
  onSubmit={() => onSubmit(draft)}
  {onCancel}
>
  <div class="vstack" style:--vstack-gap="var(--space-2)">
    <Field label="URL">
      <Input
        type="text"
        placeholder={adding ? 'https://example.com/feed.xml | tumblr:user | arena:slug' : undefined}
        bind:value={draft.url}
      />
    </Field>
    <Field label={adding ? 'Title (optional)' : 'Title'}>
      <Input type="text" bind:value={draft.title} />
    </Field>
    <Field label="Category">
      <Select
        bind:value={draft.category}
        options={categoryOptions}
        ariaLabel="Category"
        fullWidth
      />
    </Field>
    <Field label={adding ? 'Poll interval (minutes, optional)' : 'Poll interval (minutes)'}>
      <Input
        type="number"
        min="1"
        placeholder={String(defaultInterval)}
        bind:value={draft.poll_interval_minutes}
      />
    </Field>
  </div>
</FormModal>
