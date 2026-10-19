<script lang="ts">
  import CopyButton from './CopyButton.svelte';
  import Field from './Field.svelte';
  import Input from './Input.svelte';

  interface Props {
    /** The value shown and copied. */
    value: string;
    /** The field's caption; the button is "Copy <label>". */
    label: string;
    /** For a token, a URL or an id, where character shape carries meaning. */
    monospace?: boolean;
    /** Guidance behind the "?" beside the label. */
    hint?: string;
    /** Visible help under the value. */
    description?: string;
  }

  let { value, label, monospace = false, hint, description }: Props = $props();
</script>

{#snippet copy()}
  <CopyButton text={value} label="Copy {label}" />
{/snippet}

<!--
  A value to read and copy: a read-only Input with a CopyButton in its box, so
  the text stays selectable by hand and the button is the quick way.
-->
<Field {label} {hint} {description}>
  <Input {value} readonly {monospace} suffix={copy} />
</Field>
