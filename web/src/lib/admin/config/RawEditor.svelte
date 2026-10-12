<script lang="ts">
  import { base } from '$app/paths';
  import { Button, Field, TextArea } from '$lib/components/ui';
  import { SENTINEL } from './changeSet';

  interface Props {
    /** The file as the view rendered it, a sentinel at every secret leaf;
     *  null when there is no file or it does not parse. */
    initial: string | null;
    /** The pending text, or null when the raw editor holds no change. */
    pending: string | null;
    /** Why the text cannot be edited now (form changes pending, a managed
     *  config), or null. */
    lockedReason: string | null;
    onChange: (text: string | null) => void;
  }

  let { initial, pending, lockedReason, onChange }: Props = $props();

  let open = $state(false);
  let shown = $derived(open || pending !== null);
  let text = $derived(pending ?? initial ?? '');

  function onInput(value: string) {
    onChange(value === (initial ?? '') ? null : value);
  }
</script>

<!--
  The whole of config.toml as text. Every credential is the sentinel, which
  the server puts the stored value back for; a new value typed over one is
  moved out of the file and stored sealed, like any other credential. The
  text replaces the whole file, so it and the form's field edits are never
  sent together: the page locks each while the other holds changes.
-->
<section class="card">
  <header class="section-header"><h2>config.toml as text</h2></header>
  {#if !shown}
    <p class="caption">
      Edit the whole file at once: for a key the form does not show, a key inside an array of
      tables, or a change across several sections.
    </p>
    <Button variant="secondary" onclick={() => (open = true)} disabled={!!lockedReason}
      >Edit as text</Button
    >
    {#if lockedReason}<p class="caption raw-note">{lockedReason}</p>{/if}
  {:else}
    {#if lockedReason}<p class="banner warn raw-note">{lockedReason}</p>{/if}
    {#if initial === null}
      <p class="banner warn raw-note">
        config.toml does not parse, so it cannot be changed from here. Fix it on the host with
        <code>istota-stack apply</code>, or copy back a
        <code>config.toml.bak.&lt;timestamp&gt;</code>.
      </p>
    {/if}
    <Field label="config.toml" wide>
      <TextArea
        monospace
        rows={24}
        value={text}
        readonly={!!lockedReason}
        spellcheck={false}
        oninput={(e: Event) => onInput((e.currentTarget as HTMLTextAreaElement).value)}
      />
    </Field>
    <ul class="caption raw-notes">
      <li>
        <code>"{SENTINEL}"</code> stands for a stored credential and keeps it. Type over it to set a new
        value; delete the line to remove it. Credentials with a secrets file are changed in the form,
        not here.
      </li>
      <li>
        <code>[users.*]</code> only seeds users on first start. Per-user settings are changed on the
        <a href="{base}/admin/users">Users</a> page.
      </li>
      <li>
        Applying rewrites the file in Istota's own layout, so comments and formatting are not kept.
      </li>
    </ul>
    <div class="raw-actions">
      {#if pending !== null}
        <Button variant="ghost" size="sm" onclick={() => onChange(null)}>Undo text changes</Button>
      {:else}
        <Button variant="ghost" size="sm" onclick={() => (open = false)}>Close</Button>
      {/if}
    </div>
  {/if}
</section>

<style>
  .raw-note {
    margin: var(--space-2) 0 0;
  }

  .raw-notes {
    margin: var(--space-2) 0;
    padding-left: var(--space-4);
  }

  .raw-actions {
    display: flex;
    gap: var(--space-2);
  }
</style>
