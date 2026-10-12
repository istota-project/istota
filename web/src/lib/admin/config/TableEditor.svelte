<script lang="ts">
  import type { AdminConfigField, ConfigOp } from '$lib/api';
  import { Field, TextArea } from '$lib/components/ui';
  import { SENTINEL } from './changeSet';

  interface Props {
    field: AdminConfigField;
    /** This table's pending `set_toml`, if any. */
    op: ConfigOp | undefined;
    badge?: string;
    hint?: string;
    problem?: string;
    onOp: (op: ConfigOp | null, path: string[]) => void;
  }

  let { field, op, badge, hint, problem, onOp }: Props = $props();

  let original = $derived(field.toml ?? '');
  let text = $derived(op?.op === 'set_toml' ? op.toml : original);
  let emptyError = $state('');
  let holdsSentinel = $derived(text.includes(`"${SENTINEL}"`));
  let sharedBlocks = $derived(
    field.path.length === 1 && field.path[0] === 'briefing_shared_blocks',
  );

  function onInput(value: string) {
    if (value === original) {
      emptyError = '';
      onOp(null, field.path);
      return;
    }
    // An empty snippet defines nothing; removing the key is its own edit.
    if (!value.trim()) {
      emptyError = 'Empty TOML defines nothing. Use Reset to remove the key.';
      onOp(null, field.path);
      return;
    }
    emptyError = '';
    onOp({ op: 'set_toml', path: field.path, toml: value }, field.path);
  }
</script>

<!--
  One table edited as TOML, starting from what config.toml holds for it. The
  snippet's only top-level key is the field's own name, which is what keeps
  "remove the key" (Reset) and "set it empty" (`name = []`) two
  different edits. The server parses it; a snippet that does not parse comes
  back from the review as a refusal with the parser's message.
-->
<Field label={field.name} {hint} {badge} inline error={emptyError || problem} wide>
  <TextArea
    monospace
    rows={Math.min(Math.max(text.split('\n').length + 1, 4), 16)}
    value={text}
    invalid={!!emptyError}
    placeholder={`[${field.name}]\n…`}
    oninput={(e: Event) => onInput((e.currentTarget as HTMLTextAreaElement).value)}
  />
</Field>
<p class="caption table-note">
  The TOML defines <code>{field.name}</code> and nothing else: a <code>[{field.name}]</code> table,
  <code>[[{field.name}]]</code> entries, or <code>{field.name} = […]</code>.
</p>
{#if holdsSentinel}
  <p class="caption table-note">
    <code>"{SENTINEL}"</code> stands for a stored credential and keeps it. Replace it to set a new value.
  </p>
{/if}
{#if sharedBlocks}
  <p class="caption table-note">
    A shared block saved from the Briefings settings with the same name wins over one here. Without
    this key the shipped default blocks apply; <code>briefing_shared_blocks = []</code> means none.
  </p>
{/if}

<style>
  .table-note {
    margin: var(--space-1) 0 0;
  }

  /* Under the control, in its column, as FieldControl's notes are. */
  @media (min-width: 641px) {
    .table-note {
      margin-left: calc(var(--field-label-width) + var(--space-3));
    }
  }
</style>
