<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { AdminConfigField, ConfigOp } from '$lib/api';
  import { Field, TextArea } from '@minot/ui';
  import { SENTINEL } from './changeSet';

  interface Props {
    field: AdminConfigField;
    /** This table's pending `set_toml`, if any. */
    op: ConfigOp | undefined;
    badge?: string;
    labelMark?: Snippet;
    hint?: string;
    problem?: string;
    onOp: (op: ConfigOp | null, path: string[]) => void;
  }

  let { field, op, badge, labelMark, hint, problem, onOp }: Props = $props();

  let original = $derived(field.toml ?? '');
  let text = $derived(op?.op === 'set_toml' ? op.toml : original);
  let emptyError = $state('');
  let holdsSentinel = $derived(text.includes(`"${SENTINEL}"`));
  let sharedBlocks = $derived(
    field.path.length === 1 && field.path[0] === 'briefing_shared_blocks',
  );

  // Behind the label's "?", as every other field's guidance is, so the row
  // holds only the control and what acts on it.
  let fullHint = $derived(
    [
      hint,
      `The TOML defines ${field.name} and nothing else: a [${field.name}] table, ` +
        `[[${field.name}]] entries, or ${field.name} = […].`,
      holdsSentinel
        ? `"${SENTINEL}" stands for a stored credential and keeps it. Replace it to set a new value.`
        : '',
      sharedBlocks
        ? 'A shared block saved from the Briefings settings with the same name wins over one ' +
          'here. Without this key the shipped default blocks apply; ' +
          'briefing_shared_blocks = [] means none.'
        : '',
    ]
      .filter(Boolean)
      .join('\n\n'),
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
<Field
  label={field.name}
  hint={fullHint}
  {badge}
  {labelMark}
  layout="inline"
  error={emptyError || problem}
>
  <TextArea
    monospace
    rows={Math.min(Math.max(text.split('\n').length + 1, 4), 16)}
    value={text}
    invalid={!!emptyError}
    placeholder={`[${field.name}]\n…`}
    oninput={(e: Event) => onInput((e.currentTarget as HTMLTextAreaElement).value)}
  />
</Field>
