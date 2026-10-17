<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getAdminConfig,
    listConfigRequests,
    type AdminConfigView,
    type ConfigProblem,
    type ConfigSecretFile,
  } from '$lib/api';
  import { Badge, Button, EmptyState, NoticeBanner, PageLoading } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import { notifyWarning } from '$lib/stores/notices';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import RequestProgress from '$lib/admin/config/RequestProgress.svelte';
  import ReviewDialog from '$lib/admin/config/ReviewDialog.svelte';
  import ConfigSecretField from '$lib/admin/config/SecretField.svelte';
  import {
    buildChangeSet,
    changeCount,
    emptyDraft,
    pathKey,
    retainExisting,
    withMove,
    withOp,
    withSecret,
    type ConfigDraft,
  } from '$lib/admin/config/changeSet';

  type Status = 'file' | 'inline' | 'environment' | 'set' | 'none';

  const STATUS_LABEL: Record<Status, string> = {
    file: 'Set',
    set: 'Set',
    inline: 'In config.toml',
    environment: 'Set outside the stack',
    none: 'Not set',
  };
  const STATUS_VARIANT = {
    file: 'success',
    set: 'success',
    inline: 'warn',
    environment: 'info',
    none: 'neutral',
  } as const;

  let view = $state<AdminConfigView | null>(null);
  let loading = $state(true);
  let error = $state('');
  let draft = $state<ConfigDraft>(emptyDraft());
  /** Rows whose write-only input is open. */
  let replacing = $state<string[]>([]);
  let reviewing = $state(false);
  let requestId = $state<number | null>(null);

  let editable = $derived(!!view?.editable);
  let configManaged = $derived(!!view?.managed.config.managed);
  let count = $derived(changeCount(draft));

  useSettingsSave(() =>
    editable
      ? {
          dirty: count > 0,
          saving: false,
          disabled: requestId !== null || !view?.document_digest,
          label: 'Review changes',
          dirtyLabel: count > 0 ? (count === 1 ? '1 change' : `${count} changes`) : undefined,
          save: () => {
            reviewing = true;
          },
          discard,
        }
      : null,
  );

  /** The rows by the config section that reads them, in the server's order. */
  let groups = $derived.by(() => {
    const byTitle = new Map<string, ConfigSecretFile[]>();
    for (const secret of view?.secrets ?? []) {
      byTitle.set(secret.used_by, [...(byTitle.get(secret.used_by) ?? []), secret]);
    }
    return [...byTitle];
  });

  let inlineCount = $derived((view?.secrets ?? []).filter((s) => s.source === 'inline').length);

  /** The view's "also set inline, where it is ignored" problem for a row. */
  let ignoredCopies = $derived.by(() => {
    const found = new Map<string, ConfigProblem>();
    for (const problem of view?.problems ?? []) {
      if (problem.action === 'unset') found.set(problem.path.join('.'), problem);
    }
    return found;
  });

  function statusOf(secret: ConfigSecretFile): Status {
    if (secret.source === 'none' && secret.set) return 'set';
    return secret.source;
  }

  function canChange(secret: ConfigSecretFile): boolean {
    return editable && !secret.read_only_reason;
  }

  function canMove(secret: ConfigSecretFile): boolean {
    return canChange(secret) && !configManaged && secret.source === 'inline' && !!secret.config_key;
  }

  function ignoredCopy(secret: ConfigSecretFile): ConfigProblem | undefined {
    return secret.config_key ? ignoredCopies.get(secret.config_key) : undefined;
  }

  function movePending(secret: ConfigSecretFile): boolean {
    return !!secret.config_key && draft.move_to_file.includes(secret.config_key);
  }

  function removalPending(problem: ConfigProblem): boolean {
    return draft.ops[pathKey(problem.path)]?.op === 'unset';
  }

  function isReplacing(secret: ConfigSecretFile): boolean {
    return replacing.includes(secret.name);
  }

  function toggleReplace(secret: ConfigSecretFile) {
    if (isReplacing(secret)) {
      replacing = replacing.filter((name) => name !== secret.name);
      draft = withSecret(draft, secret.name, null);
    } else {
      replacing = [...replacing, secret.name];
    }
  }

  function setClear(secret: ConfigSecretFile, clear: boolean) {
    replacing = replacing.filter((name) => name !== secret.name);
    draft = withSecret(draft, secret.name, clear ? '' : null);
  }

  function toggleMove(secret: ConfigSecretFile) {
    if (!secret.config_key) return;
    draft = withMove(draft, secret.config_key, !movePending(secret));
  }

  function toggleRemoval(problem: ConfigProblem) {
    draft = removalPending(problem)
      ? withOp(draft, null, problem.path)
      : withOp(draft, { op: 'unset', path: problem.path });
  }

  async function load() {
    loading = true;
    try {
      view = await getAdminConfig();
      error = '';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load the credentials';
    } finally {
      loading = false;
    }
    await followOpenRequest();
  }

  /** A change submitted from Configuration, another tab or before a reload is
   *  followed here too, so the page cannot start a second one. */
  async function followOpenRequest() {
    try {
      const history = await listConfigRequests();
      const open = history.find((r) => ['pending', 'taken', 'building'].includes(r.status));
      if (open && requestId === null) requestId = open.id;
    } catch {
      // The submit route refuses a second open request; this is only a hint.
    }
  }

  function discard() {
    draft = emptyDraft();
    replacing = [];
  }

  /** config.toml changed under the page: reload, keep what still applies. */
  async function onConflict() {
    reviewing = false;
    await load();
    if (!view) return;
    const kept = retainExisting(draft, view);
    draft = kept.draft;
    replacing = replacing.filter((name) => name in draft.secrets);
    notifyWarning(
      'config.toml changed on the host since this page loaded' +
        (kept.dropped
          ? `, and ${kept.dropped === 1 ? '1 of your changes no longer applies and was' : `${kept.dropped} of your changes no longer apply and were`} dropped.`
          : '; your changes are kept.') +
        ' Review them again.',
    );
  }

  function onSubmitted(id: number) {
    reviewing = false;
    discard();
    requestId = id;
  }

  function onRequestOpen(id: number) {
    reviewing = false;
    notifyWarning('Another change is already waiting for the host. It is shown above.');
    requestId = id;
  }

  onMount(load);
</script>

<div class="settings admin-page card-pane">
  {#if loading && !view}
    <PageLoading />
  {:else if error && !view}
    <div class="center-msg error">{error}</div>
  {:else if view}
    {#if !view.applier.available}
      <NoticeBanner title="Read-only: changes are made on the host" variant="info">
        <p>{view.applier.reason}</p>
        <p>Credentials are never sent to the browser. Each one shows only whether it is set.</p>
      </NoticeBanner>
    {:else if !view.editable}
      <NoticeBanner title="Read-only view of the running configuration" variant="info">
        <p>
          This process has no config.toml to change{#if view.config_path}
            at <code>{view.config_path}</code>{/if}.
        </p>
      </NoticeBanner>
    {/if}

    {#if inlineCount}
      <NoticeBanner
        title={inlineCount === 1
          ? '1 credential is stored in config.toml'
          : `${inlineCount} credentials are stored in config.toml`}
        variant="warn"
      >
        <p>
          They work, but config.toml holds them in plain text. Move each one to its secret file. The
          host moves the value itself, so it never reaches this page.
        </p>
      </NoticeBanner>
    {/if}

    {#if requestId !== null}
      {#key requestId}
        <RequestProgress
          {requestId}
          onApplied={(fresh) => (view = fresh)}
          onDismiss={() => {
            requestId = null;
            void load();
          }}
        />
      {/key}
    {/if}

    {#if groups.length === 0}
      <EmptyState>This install has no deployment credentials.</EmptyState>
    {/if}

    {#each groups as [title, secrets] (title)}
      <SettingsCard {title}>
        <div class="table-scroll">
          <table class="grid secrets-grid" aria-label={title}>
            <thead
              ><tr
                ><th scope="col">Credential</th>
                <th scope="col" class="col-status">Status</th>
                <th scope="col" class="col-actions">Actions</th></tr
              ></thead
            >
            <tbody>
              {#each secrets as secret (secret.name)}
                {@const status = statusOf(secret)}
                {@const copy = ignoredCopy(secret)}
                {@const pending = draft.secrets[secret.name]}
                <tr>
                  <td>
                    <div class="cell-main">{secret.label}</div>
                    <code class="caption">{secret.name}</code>
                  </td>
                  <td class="col-status">
                    <Badge size="sm" variant={STATUS_VARIANT[status]}>{STATUS_LABEL[status]}</Badge>
                    {#if copy}
                      <div class="caption">config.toml also holds a copy, which is ignored.</div>
                    {/if}
                  </td>
                  <td class="col-actions">
                    {#if secret.read_only_reason}
                      <span class="caption">{secret.read_only_reason}</span>
                    {:else if canChange(secret)}
                      <div class="row-actions">
                        {#if pending === ''}
                          <span class="caption">Will be cleared.</span>
                          <Button
                            variant="secondary"
                            size="sm"
                            onclick={() => setClear(secret, false)}>Undo</Button
                          >
                        {:else if movePending(secret)}
                          <span class="caption">Moves to its secret file.</span>
                          <Button variant="secondary" size="sm" onclick={() => toggleMove(secret)}
                            >Keep in config.toml</Button
                          >
                        {:else}
                          {#if canMove(secret) && !isReplacing(secret)}
                            <Button variant="secondary" size="sm" onclick={() => toggleMove(secret)}
                              >Move to secret file</Button
                            >
                          {/if}
                          <Button
                            variant="secondary"
                            size="sm"
                            onclick={() => toggleReplace(secret)}
                            >{isReplacing(secret) ? 'Cancel' : 'Replace'}</Button
                          >
                          {#if secret.source === 'file' && !isReplacing(secret)}
                            <Button
                              variant="secondary"
                              size="sm"
                              onclick={() => setClear(secret, true)}>Clear</Button
                            >
                          {/if}
                        {/if}
                        {#if copy && !configManaged}
                          <Button variant="secondary" size="sm" onclick={() => toggleRemoval(copy)}
                            >{removalPending(copy) ? 'Keep copy' : 'Remove copy'}</Button
                          >
                        {/if}
                      </div>
                    {/if}
                  </td>
                </tr>
                {#if canChange(secret) && isReplacing(secret)}
                  <tr class="replace-row">
                    <td colspan="3">
                      <ConfigSecretField
                        label="New value for {secret.label}"
                        configured={false}
                        {pending}
                        onChange={(value) => (draft = withSecret(draft, secret.name, value))}
                      />
                    </td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
        </div>
      </SettingsCard>
    {/each}

    {#if reviewing && view.document_digest}
      <ReviewDialog
        changeSet={buildChangeSet(draft, view.document_digest)}
        onClose={() => (reviewing = false)}
        {onSubmitted}
        {onConflict}
        {onRequestOpen}
      />
    {/if}
  {/if}
</div>

<style>
  .secrets-grid .col-status {
    width: 11rem;
  }

  .secrets-grid .col-actions {
    width: 40%;
    text-align: right;
  }

  .cell-main {
    overflow-wrap: anywhere;
  }

  .row-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: var(--space-2);
  }
</style>
