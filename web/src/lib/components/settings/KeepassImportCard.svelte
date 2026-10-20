<script lang="ts">
  import { onDestroy } from 'svelte';
  import {
    previewKeepassImport,
    applyKeepassImport,
    type KeepassImportPreview,
    type KeepassImportItem,
  } from '$lib/api';
  import { Button, Field, FormStack, Input, Card, Checkbox } from '$lib/components/ui';
  import { latest } from '$lib/ui/latest';

  let { onImported }: { onImported?: () => void } = $props();
  let file: File | null = $state(null);
  let keyfile: File | null = $state(null);
  let passphrase = $state('');
  let preview: KeepassImportPreview | null = $state(null);
  let selected: string[] = $state([]);
  let error = $state('');
  let result = $state('');
  let busy = $state(false);
  let pickerVersion = $state(0);
  let pending: AbortController | null = null;
  const runs = latest();
  const groups: { status: KeepassImportItem['status']; label: string }[] = [
    { status: 'new', label: 'New' },
    { status: 'changed', label: 'Changed' },
    { status: 'unchanged', label: 'Unchanged' },
    { status: 'conflict', label: 'Conflicts' },
    { status: 'skipped', label: 'Skipped' },
  ];

  function clear() {
    pending?.abort();
    pending = null;
    runs.invalidate();
    file = keyfile = null;
    passphrase = '';
    preview = null;
    selected = [];
    busy = false;
    pickerVersion += 1;
  }
  onDestroy(() => {
    clear();
    runs.dispose();
  });

  async function run(apply: boolean) {
    if (!file) return;
    const controller = new AbortController();
    const current = runs.next();
    pending = controller;
    busy = true;
    error = result = '';
    try {
      if (apply && preview) {
        const response = await applyKeepassImport(
          file,
          passphrase,
          keyfile,
          selected,
          preview.digest,
          controller.signal,
        );
        if (!current()) return;
        const count = response.imported.length;
        const refused = Object.entries(response.not_imported).map(
          ([name, status]) => `${name}: ${status}`,
        );
        clear();
        result = `Imported ${count} credential${count === 1 ? '' : 's'}.`;
        if (refused.length) result += ` Not imported: ${refused.join(', ')}.`;
        onImported?.();
      } else {
        const response = await previewKeepassImport(file, passphrase, keyfile, controller.signal);
        if (!current()) return;
        preview = response;
        selected = response.items.filter((item) => item.default_selected).map((item) => item.name);
      }
    } catch (e) {
      if (current()) error = (e as Error).message;
    } finally {
      if (current()) {
        pending = null;
        busy = false;
      }
    }
  }
</script>

<Card
  title="Import from KeePass"
  description="Choose entries from a KeePass file to copy into your credentials."
>
  {#key pickerVersion}
    <Field
      label="KeePass file"
      hint="Choose a .kdbx file. Preview lets you select entries before anything is imported."
    >
      <!-- A file input is not a primitive, so it names itself. -->
      <input
        type="file"
        accept=".kdbx"
        aria-label="KeePass file"
        disabled={busy || !!preview}
        onchange={(event) => {
          file = event.currentTarget.files?.[0] ?? null;
        }}
      />
    </Field>
    <Field
      label="Source key file (optional)"
      hint="Only needed if the source KeePass file requires a key file as well as its passphrase."
    >
      <input
        type="file"
        aria-label="Source key file (optional)"
        disabled={busy || !!preview}
        onchange={(event) => {
          keyfile = event.currentTarget.files?.[0] ?? null;
        }}
      />
    </Field>
  {/key}
  <Field
    label="File passphrase"
    hint="The passphrase that unlocks this KeePass file. It is not saved with your credentials."
  >
    <Input
      type="password"
      bind:value={passphrase}
      autocomplete="off"
      disabled={busy || !!preview}
    />
  </Field>
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  {#if result}<p role="status">{result}</p>{/if}
  {#if preview}
    {#if !preview.scoped}<p>This file has no istota group, so every entry is listed.</p>{/if}
    {#if preview.truncated}<p>
        Only part of this file was read: {preview.truncated} limit reached.
      </p>{/if}
    {#each groups as group}
      {@const items = preview.items.filter((item) => item.status === group.status)}
      {#if items.length}
        <FormStack legend={group.label} gap={2}>
          {#each items as item}
            <div class="item">
              <Field label={item.name} checkbox>
                <Checkbox
                  checked={selected.includes(item.name)}
                  onchange={(on) =>
                    (selected = on
                      ? [...selected, item.name]
                      : selected.filter((name) => name !== item.name))}
                  disabled={busy || !['new', 'changed'].includes(item.status)}
                />
              </Field>
              {#if item.changed_fields.length}<p class="caption">
                  Changed since last import: {item.changed_fields.join(', ')}
                </p>{/if}
              {#if item.hosts.length}<p class="caption">{item.hosts.join(', ')}</p>{/if}
              {#if item.reason}<p class="caption">{item.reason}</p>{/if}
            </div>
          {/each}
        </FormStack>
      {/if}
    {/each}
  {/if}
  <div class="hstack wrap">
    {#if preview}
      <Button
        variant="primary"
        size="sm"
        disabled={busy || !selected.length}
        onclick={() => run(true)}>Import selected</Button
      >
    {:else}
      <Button variant="primary" size="sm" disabled={busy || !file} onclick={() => run(false)}
        >Preview</Button
      >
    {/if}
    <Button
      variant="secondary"
      size="sm"
      onclick={() => {
        clear();
        error = result = '';
      }}>Cancel</Button
    >
  </div>
</Card>

<style>
  .item {
    overflow-wrap: anywhere;
  }
</style>
