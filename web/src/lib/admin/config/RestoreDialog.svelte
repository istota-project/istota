<script lang="ts">
  import { onDestroy } from 'svelte';
  import {
    ConfigChangeError,
    previewConfigRestore,
    submitConfigRestore,
    type ConfigRestoreCredential,
    type ConfigRestorePreview,
    type ConfigRestoreStatus,
    type ConfigRestoreUpload,
    type StepUpProof,
  } from '$lib/api';
  import { Alert, Button, Checkbox, Field, FormStack, Input, Modal } from '@minot/ui';
  import { latest } from '@minot/ui/ui/latest';
  import ReviewDialog from './ReviewDialog.svelte';
  import { looksEncrypted, toBase64 } from './backupFile';

  interface Props {
    /** sha256 of the config.toml the page shows; the restore is checked against it. */
    baseDigest: string;
    /** Why this installation cannot apply a restore; it can still be inspected. */
    applyBlockedReason?: string | null;
    onClose: () => void;
    onSubmitted: (id: number) => void;
    /** config.toml changed under the page: reload the view. */
    onConflict: () => void | Promise<void>;
    /** A request is already open; the page follows that one instead. */
    onRequestOpen: (id: number) => void;
  }

  let {
    baseDigest,
    applyBlockedReason = null,
    onClose,
    onSubmitted,
    onConflict,
    onRequestOpen,
  }: Props = $props();

  const STATUS_LABELS: Record<ConfigRestoreStatus, string> = {
    new: 'new',
    changed: 'changed',
    unchanged: 'unchanged',
    host_only: 'host only',
  };
  /** Refusals that mean the reviewed preview no longer stands. */
  const STALE = new Set(['preview_changed', 'invalid_selection']);

  let stage = $state<'choose' | 'review'>('choose');
  let format = $state<'toml' | 'kdbx' | null>(null);
  let password = $state('');
  let restoreConfig = $state(true);
  let selected = $state<string[]>([]);
  /** The last preview, and the selection it was made for. */
  let opened = $state<ConfigRestorePreview | null>(null);
  let openedFor = $state('');
  let busy = $state(false);
  let refusal = $state('');
  let pickerVersion = $state(0);
  let hasKeyfile = $state(false);
  // Plain variables, not state: neither is drawn, and neither should outlive the dialog.
  let fileData: string | null = null;
  let keyfileData: string | null = null;
  let pending: AbortController | null = null;
  const reads = latest();

  let selectionKey = $derived(JSON.stringify([restoreConfig, [...selected].sort()]));
  let credentials = $derived(opened?.credentials ?? []);
  let selectable = $derived(credentials.filter((row) => row.selectable).map((row) => row.id));
  let nothingChosen = $derived(!restoreConfig && selected.length === 0);
  let reviewNote = $derived.by(() => {
    if (!opened) return '';
    const replaced = credentials
      .filter((row) => row.status === 'changed' && selected.includes(row.id))
      .map((row) => row.name);
    return replaced.length
      ? `${opened.note} Replaces the current value of ${replaced.join(', ')}.`
      : opened.note;
  });

  function upload(): ConfigRestoreUpload {
    const encrypted = format === 'kdbx';
    return {
      format: encrypted ? 'kdbx' : 'toml',
      file: fileData ?? '',
      password: encrypted && password ? password : null,
      keyfile: encrypted ? keyfileData : null,
      base_digest: baseDigest,
      restore_config: encrypted ? restoreConfig : true,
      selected: encrypted ? [...selected].sort() : [],
    };
  }

  function forgetPreview() {
    pending?.abort();
    pending = null;
    opened = null;
    openedFor = '';
  }

  /** Everything the dialog holds from the upload, dropped. */
  function clear() {
    forgetPreview();
    reads.invalidate();
    fileData = keyfileData = null;
    hasKeyfile = false;
    format = null;
    password = '';
    selected = [];
    restoreConfig = true;
  }

  function close() {
    clear();
    onClose();
  }

  onDestroy(() => {
    clear();
    reads.dispose();
  });

  async function readFile(file: File | undefined): Promise<Uint8Array | null> {
    if (!file) return null;
    return new Uint8Array(await file.arrayBuffer());
  }

  async function chooseFile(file: File | undefined) {
    forgetPreview();
    refusal = '';
    fileData = null;
    format = null;
    selected = [];
    restoreConfig = true;
    const current = reads.next();
    const bytes = await readFile(file);
    if (!current() || !bytes) return;
    fileData = toBase64(bytes);
    format = looksEncrypted(bytes) ? 'kdbx' : 'toml';
  }

  async function chooseKeyfile(file: File | undefined) {
    forgetPreview();
    keyfileData = null;
    const bytes = await readFile(file);
    keyfileData = bytes ? toBase64(bytes) : null;
    hasKeyfile = keyfileData !== null;
  }

  async function fetchPreview(): Promise<ConfigRestorePreview> {
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    const forSelection = selectionKey;
    try {
      const result = await previewConfigRestore(upload(), controller.signal);
      if (controller.signal.aborted) throw new DOMException('Withdrawn', 'AbortError');
      opened = result;
      openedFor = forSelection;
      return result;
    } finally {
      if (pending === controller) pending = null;
    }
  }

  function conflict() {
    forgetPreview();
    stage = 'choose';
    refusal =
      'config.toml changed on the host since this page loaded. Preview the restore again ' +
      'against the current file.';
    void onConflict();
  }

  function report(e: unknown) {
    if (e instanceof DOMException && e.name === 'AbortError') return;
    if (e instanceof ConfigChangeError) {
      if (e.error === 'config_changed') return conflict();
      if (e.error === 'request_open' && typeof e.extra.id === 'number') {
        onRequestOpen(e.extra.id);
        return;
      }
    }
    refusal = e instanceof Error ? e.message : 'The backup could not be read.';
  }

  /** Open the file: a plain one goes straight to its review, an encrypted
   *  one lists what it holds, restoring the configuration and nothing else. */
  async function open() {
    if (!format) return;
    busy = true;
    refusal = '';
    selected = [];
    restoreConfig = true;
    try {
      await fetchPreview();
      if (format === 'toml') stage = 'review';
    } catch (e) {
      report(e);
    } finally {
      busy = false;
    }
  }

  /** The review of the current selection, previewed again if it changed. */
  async function review() {
    if (!opened || openedFor !== selectionKey) {
      busy = true;
      refusal = '';
      try {
        await fetchPreview();
      } catch (e) {
        report(e);
        return;
      } finally {
        busy = false;
      }
    }
    stage = 'review';
  }

  async function submit(proof: StepUpProof, confirmed: string[]): Promise<{ id: number }> {
    if (!opened) throw new Error('Preview the restore again.');
    const controller = new AbortController();
    pending = controller;
    try {
      return await submitConfigRestore(
        {
          ...upload(),
          input_digest: opened.input_digest,
          preview_digest: opened.preview_digest,
          confirm: confirmed,
          step_up: proof,
        },
        controller.signal,
      );
    } catch (e) {
      // The file stays; the preview it was reviewed against does not.
      if (e instanceof ConfigChangeError && e.error && STALE.has(e.error)) {
        forgetPreview();
        stage = 'choose';
        refusal = e.message;
      }
      throw e;
    } finally {
      if (pending === controller) pending = null;
    }
  }

  function toggle(row: ConfigRestoreCredential, on: boolean) {
    selected = on ? [...selected, row.id] : selected.filter((id) => id !== row.id);
  }

  function kindLine(row: ConfigRestoreCredential): string {
    if (row.kind === 'inline') return 'Held in config.toml.';
    if (row.kind === 'push_key') return 'Restored to push/signing.pem in the config directory.';
    return row.source === 'inline'
      ? 'Deployment credential, saved in config.toml when backed up; restored to its secret file.'
      : 'Deployment credential.';
  }
</script>

<Modal
  open={stage === 'choose'}
  title="Restore configuration"
  width="640px"
  {busy}
  onOpenChange={(next) => {
    if (!next && stage === 'choose') close();
  }}
>
  <p>
    Restore from a backup made on this page: a plain <code>.toml</code> file restores the
    configuration, an encrypted <code>.kdbx</code> file can also restore the credentials you select. You
    review the change before anything is sent to the host.
  </p>

  {#key pickerVersion}
    <Field label="Backup file">
      <!-- A file input is not a primitive, so it names itself. -->
      <input
        type="file"
        accept=".toml,.kdbx"
        aria-label="Backup file"
        disabled={busy || !!opened}
        onchange={(event) => void chooseFile(event.currentTarget.files?.[0])}
      />
    </Field>
    {#if format === 'kdbx'}
      <Field label="Backup password">
        <Input
          type="password"
          bind:value={password}
          autocomplete="off"
          revealable
          disabled={busy || !!opened}
        />
      </Field>
      <Field label="Key file" optional hint="Only needed if the backup was made with a key file.">
        <input
          type="file"
          aria-label="Key file (optional)"
          disabled={busy || !!opened}
          onchange={(event) => void chooseKeyfile(event.currentTarget.files?.[0])}
        />
      </Field>
    {:else if format === 'toml'}
      <p class="caption">A plain backup restores the configuration only.</p>
    {/if}
  {/key}

  {#if opened && format === 'kdbx'}
    <FormStack legend="What to restore" gap={2}>
      <Field
        label="Restore the configuration"
        checkbox
        description="Replaces config.toml with the backup's copy."
      >
        <Checkbox bind:checked={restoreConfig} disabled={busy} />
      </Field>
      {#each credentials as row (row.id)}
        <Field label={row.name} checkbox badge={STATUS_LABELS[row.status]}>
          <Checkbox
            checked={selected.includes(row.id)}
            onchange={(on) => toggle(row, on)}
            disabled={busy || !row.selectable}
          />
          {#snippet description()}
            <span class="restore-kind">{kindLine(row)}</span>
            {#if row.status === 'changed'}
              <span class="restore-kind">Replaces the value this installation has.</span>
            {:else if row.status === 'unchanged'}
              <span class="restore-kind">Same as this installation.</span>
            {/if}
            {#if row.reason}<span class="restore-kind">{row.reason}</span>{/if}
          {/snippet}
        </Field>
      {/each}
    </FormStack>
    {#if credentials.length === 0}
      <p class="caption">The backup holds no credentials.</p>
    {:else if selectable.length}
      <div>
        <Button
          variant="secondary"
          size="sm"
          disabled={busy}
          onclick={() => (selected = [...selectable])}>Select all that can be restored</Button
        >
      </div>
    {/if}
  {/if}

  {#if refusal}
    <Alert appearance="inline" variant="danger" role="alert">{refusal}</Alert>
  {/if}

  {#snippet footer()}
    {#if opened}
      <Button
        variant="secondary"
        disabled={busy}
        onclick={() => {
          clear();
          refusal = '';
          pickerVersion += 1;
        }}>Choose another file</Button
      >
    {/if}
    <Button variant="secondary" onclick={close} disabled={busy}>Cancel</Button>
    {#if opened && format === 'kdbx'}
      <Button
        variant="primary"
        onclick={review}
        loading={busy}
        loadingLabel="Checking…"
        disabled={nothingChosen}>Review restore…</Button
      >
    {:else}
      <Button
        variant="primary"
        onclick={open}
        loading={busy}
        loadingLabel={format === 'kdbx' ? 'Unlocking…' : 'Checking…'}
        disabled={!format || (format === 'kdbx' && !password && !hasKeyfile)}
        >{format === 'kdbx' ? 'Unlock' : 'Preview'}</Button
      >
    {/if}
  {/snippet}
</Modal>

{#if stage === 'review' && opened}
  <ReviewDialog
    initialPreview={opened.preview}
    note={reviewNote}
    {applyBlockedReason}
    onPreview={async () => (await fetchPreview()).preview}
    onSubmit={submit}
    onClose={() => (stage = 'choose')}
    onSubmitted={(id) => {
      clear();
      onSubmitted(id);
    }}
    onConflict={conflict}
    {onRequestOpen}
  />
{/if}

<style>
  .restore-kind {
    display: block;
  }
</style>
