<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getAdminConfig,
    listConfigRequests,
    restoreConfigRequest,
    type AdminConfigField,
    type AdminConfigView,
    type ConfigOp,
    type ConfigPreview,
    type ConfigProblem,
    type ConfigRequest,
    type ConfigResource,
  } from '$lib/api';
  import { Button, Input, NoticeBanner } from '$lib/components/ui';
  import { getCurrentUser } from '$lib/userContext';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import AdminsEditor from '$lib/admin/config/AdminsEditor.svelte';
  import FieldControl from '$lib/admin/config/FieldControl.svelte';
  import HistoryList from '$lib/admin/config/HistoryList.svelte';
  import RawEditor from '$lib/admin/config/RawEditor.svelte';
  import RequestProgress from '$lib/admin/config/RequestProgress.svelte';
  import ReviewDialog from '$lib/admin/config/ReviewDialog.svelte';
  import ConfigSecretField from '$lib/admin/config/SecretField.svelte';
  import {
    buildChangeSet,
    changeCount,
    emptyDraft,
    formatValue,
    pathKey,
    retainExisting,
    withAdmins,
    withOp,
    withRaw,
    withSecret,
    type ConfigDraft,
  } from '$lib/admin/config/changeSet';

  const current = getCurrentUser();
  let self = $derived(current.user.username);

  let view = $state<AdminConfigView | null>(null);
  let loading = $state(true);
  let error = $state('');
  let filter = $state('');
  let readOnlyCollapsed = $state(true);
  let draft = $state<ConfigDraft>(emptyDraft());
  let reviewing = $state(false);
  let requestId = $state<number | null>(null);
  let pageNotice = $state('');
  let history = $state<ConfigRequest[] | null>(null);
  let historyError = $state('');
  let restoring = $state<number | null>(null);
  /** A restore's preview, shown by the review it opens; its warnings name
   *  credentials cleared since, which a second preview could not see. */
  let restored = $state<{ preview: ConfigPreview; note: string } | null>(null);

  let editable = $derived(!!view?.editable);
  let count = $derived(changeCount(draft));
  let configManaged = $derived(!!view?.managed.config.managed);
  let opCount = $derived(Object.keys(draft.ops).length);
  /** The form's fields, locked while the raw editor holds the whole file. */
  let formEditable = $derived(editable && draft.raw === null);

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

  const RESOURCE_LABELS: Record<ConfigResource, string> = {
    config: 'config.toml',
    secrets: 'The deployment credentials',
    admins: 'The admin list',
  };

  let managedResources = $derived(
    view
      ? (Object.entries(view.managed) as [ConfigResource, AdminConfigView['managed']['config']][])
          .filter(([, entry]) => entry.managed)
          .map(([resource, entry]) => ({ resource, source: entry.source }))
      : [],
  );

  let blockingProblems = $derived(view ? view.problems.filter((p) => p.blocking) : []);

  /** The first blocking problem the current file has, by field label. */
  let problemByKey = $derived.by(() => {
    const found = new Map<string, string>();
    for (const problem of blockingProblems) {
      const key = problem.path.join('.');
      if (key && !found.has(key)) found.set(key, problem.message);
    }
    return found;
  });

  /** What the current file holds that the loader ignores or only warns about:
   *  unknown and retired keys, a credential also set inline. */
  let fileWarnings = $derived(view ? view.problems.filter((p) => !p.blocking) : []);

  let rawLockedReason = $derived(
    configManaged
      ? 'A plan manages config.toml, so it is not changed from here.'
      : opCount > 0
        ? `The text replaces the whole file. Review or discard the ${
            opCount === 1 ? 'change' : `${opCount} changes`
          } made in the form first.`
        : null,
  );

  let restoreBlockedReason = $derived(
    !editable
      ? 'Changes cannot be made from this page.'
      : configManaged
        ? 'A plan manages config.toml, so an earlier one is not restored from here.'
        : requestId !== null
          ? 'A change is in progress.'
          : count > 0
            ? 'Review or discard your pending changes to restore an earlier configuration.'
            : null,
  );

  /** File credentials no config key reads, so no section field shows them. */
  let standaloneSecrets = $derived(view ? view.secrets.filter((s) => !s.config_key) : []);

  function matches(field: AdminConfigField, needle: string): boolean {
    return (
      field.key.toLowerCase().includes(needle) ||
      formatValue(field.value).toLowerCase().includes(needle)
    );
  }

  let sections = $derived.by(() => {
    if (!view) return [];
    const needle = filter.trim().toLowerCase();
    if (!needle) return view.sections;
    return view.sections
      .map((s) => ({ ...s, fields: s.fields.filter((f) => matches(f, needle)) }))
      .filter((s) => s.fields.length > 0);
  });

  let matchCount = $derived(sections.reduce((n, s) => n + s.fields.length, 0));

  async function load() {
    loading = true;
    try {
      view = await getAdminConfig();
      error = '';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load configuration';
    } finally {
      loading = false;
    }
    await loadHistory();
  }

  /** The history, and the request still open in it: a change submitted from
   *  another tab, or before a reload, is followed here too. */
  async function loadHistory() {
    try {
      history = await listConfigRequests();
      historyError = '';
    } catch (e) {
      historyError = e instanceof Error ? e.message : 'The history could not be loaded.';
      return;
    }
    const open = history.find((r) => ['pending', 'taken', 'building'].includes(r.status));
    if (open && requestId === null) requestId = open.id;
  }

  function removalPending(problem: ConfigProblem): boolean {
    return draft.ops[pathKey(problem.path)]?.op === 'unset';
  }

  function toggleRemoval(problem: ConfigProblem) {
    draft = removalPending(problem)
      ? withOp(draft, null, problem.path)
      : withOp(draft, { op: 'unset', path: problem.path });
  }

  async function restore(id: number) {
    restoring = id;
    try {
      const found = await restoreConfigRequest(id);
      draft = withRaw(emptyDraft(), found.change_set.raw);
      restored = { preview: found.preview, note: `Restoring change #${id}. ${found.note}` };
      pageNotice = restored.note;
      reviewing = true;
    } catch (e) {
      historyError = e instanceof Error ? e.message : 'That change could not be restored.';
    } finally {
      restoring = null;
    }
  }

  function setOp(op: ConfigOp | null, path: string[]) {
    draft = withOp(draft, op, path);
  }

  function setSecret(name: string, value: string | null) {
    draft = withSecret(draft, name, value);
  }

  function discard() {
    draft = emptyDraft();
    restored = null;
    pageNotice = '';
  }

  function closeReview() {
    reviewing = false;
    restored = null;
  }

  /** config.toml changed under the page: reload, keep what still applies. */
  async function onConflict() {
    closeReview();
    await load();
    if (!view) return;
    const kept = retainExisting(draft, view);
    draft = kept.draft;
    pageNotice =
      'config.toml changed on the host since this page loaded. The page now shows the new values' +
      (kept.dropped
        ? `, and ${kept.dropped} of your changes no longer apply and were dropped.`
        : '; your changes are kept.') +
      ' Review them again.';
  }

  function onSubmitted(id: number) {
    closeReview();
    draft = emptyDraft();
    pageNotice = '';
    requestId = id;
    void loadHistory();
  }

  function onRequestOpen(id: number) {
    closeReview();
    pageNotice = 'Another change is already waiting for the host. It is shown below.';
    requestId = id;
  }

  onMount(load);
</script>

<div class="settings config-page">
  {#if loading && !view}
    <div class="center-msg">Loading…</div>
  {:else if error && !view}
    <div class="center-msg error">{error}</div>
  {:else if view}
    {#if !view.applier.available}
      <NoticeBanner title="Read-only: changes are made on the host" variant="info">
        <p>{view.applier.reason}</p>
        <p>Credentials are never sent to the browser. A secret shows only whether it is set.</p>
      </NoticeBanner>
    {:else if !view.editable}
      <NoticeBanner
        title="Read-only view of the running configuration"
        bind:collapsed={readOnlyCollapsed}
        variant="info"
      >
        <p>
          This process has no config.toml to change{#if view.config_path}
            at <code>{view.config_path}</code>{/if}.
        </p>
      </NoticeBanner>
    {/if}

    {#each managedResources as entry (entry.resource)}
      <NoticeBanner
        title="{RESOURCE_LABELS[entry.resource]} is managed by {entry.source ?? 'a plan'}"
        variant="warn"
        collapsed={false}
      >
        {#if entry.source === 'unreadable'}
          <p>
            config/managed.json cannot be read, so everything reads as managed. Re-run
            <code>istota-stack apply</code> on the host with the intended <code>[meta]</code> to repair
            it.
          </p>
        {:else}
          <p>
            It is read-only here: the next run of {entry.source ?? 'that plan'} would revert a change
            made from this page. Change it there, or remove it from that source to hand it to the web.
          </p>
        {/if}
      </NoticeBanner>
    {/each}

    {#if editable && view.has_comments}
      <NoticeBanner title="config.toml has comments" variant="info">
        <p>
          Applying a change rewrites the file in Istota's own layout, so its comments and formatting
          are not kept. The host keeps the previous file as
          <code>config/config.toml.bak.&lt;timestamp&gt;</code>.
        </p>
      </NoticeBanner>
    {/if}

    {#if editable && blockingProblems.length}
      <NoticeBanner
        title="config.toml has problems to fix first"
        variant="danger"
        collapsed={false}
      >
        <p>The host refuses a file with these, so the next change has to fix them too.</p>
        <ul class="problem-list">
          {#each blockingProblems as problem, i (i)}
            <li>
              {#if problem.path.length}<code>{problem.path.join('.')}</code>{/if}
              {problem.message}
            </li>
          {/each}
        </ul>
      </NoticeBanner>
    {/if}

    {#if pageNotice}
      <p class="banner warn" role="status">{pageNotice}</p>
    {/if}

    {#if requestId !== null}
      {#key requestId}
        <RequestProgress
          {requestId}
          onApplied={(fresh) => {
            view = fresh;
            void loadHistory();
          }}
          onDismiss={() => {
            requestId = null;
            void load();
          }}
        />
      {/key}
    {/if}

    <div class="config-toolbar control-row">
      <Input bind:value={filter} placeholder="Filter settings…" aria-label="Filter settings" />
      {#if filter.trim()}
        <span class="match-count">{matchCount} matching</span>
      {/if}
    </div>

    {#if !filter.trim()}
      <section class="card">
        <header class="section-header"><h2>Admins</h2></header>
        <AdminsEditor
          users={view.admins.users}
          pending={draft.admins}
          {self}
          {editable}
          readOnlyReason={view.admins.read_only_reason}
          onChange={(admins) => (draft = withAdmins(draft, admins))}
        />
      </section>

      {#if fileWarnings.length}
        <section class="card">
          <header class="section-header"><h2>Warnings about config.toml</h2></header>
          <p class="caption">
            Istota loads the file with these. A key it ignores is kept until it is removed.
          </p>
          <ul class="warning-list">
            {#each fileWarnings as problem, i (i)}
              <li class="warning-row">
                <span>
                  {#if problem.path.length}<code>{problem.path.join('.')}</code>{/if}
                  {problem.message}
                </span>
                {#if editable && problem.action === 'unset'}
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={!formEditable}
                    onclick={() => toggleRemoval(problem)}
                    >{removalPending(problem) ? 'Keep' : 'Remove'}</Button
                  >
                {/if}
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if standaloneSecrets.length}
        <section class="card">
          <header class="section-header"><h2>Credentials</h2></header>
          {#each standaloneSecrets as secret (secret.name)}
            {#if editable && !secret.read_only_reason}
              <ConfigSecretField
                label={secret.label}
                configured={secret.set}
                pending={draft.secrets[secret.name]}
                onChange={(value) => setSecret(secret.name, value)}
              />
            {:else}
              <ConfigSecretField
                label={secret.label}
                configured={secret.set}
                pending={undefined}
                readOnlyReason={secret.read_only_reason ?? 'Read-only'}
                onChange={() => {}}
              />
            {/if}
          {/each}
        </section>
      {/if}
    {/if}

    {#if sections.length === 0}
      <div class="center-msg">No settings match “{filter}”.</div>
    {/if}

    {#each sections as section (section.key)}
      <section class="card">
        <header class="section-header"><h2>{section.label}</h2></header>
        {#each section.fields as field (field.key)}
          <FieldControl
            {field}
            op={draft.ops[pathKey(field.path)]}
            secretPending={field.credential ? draft.secrets[field.credential.name] : undefined}
            editable={formEditable}
            problem={problemByKey.get(field.path.join('.'))}
            onOp={setOp}
            onSecret={setSecret}
          />
        {/each}
      </section>
    {/each}

    {#if editable && !filter.trim()}
      <RawEditor
        initial={view.raw_toml}
        pending={draft.raw}
        lockedReason={rawLockedReason}
        onChange={(text) => (draft = withRaw(draft, text))}
      />
    {/if}

    {#if !filter.trim()}
      <HistoryList
        requests={history}
        error={historyError}
        {restoreBlockedReason}
        {restoring}
        onRestore={restore}
      />
    {/if}

    {#if reviewing && view.document_digest}
      <ReviewDialog
        changeSet={buildChangeSet(draft, view.document_digest)}
        initialPreview={restored?.preview ?? null}
        note={restored?.note ?? ''}
        onClose={closeReview}
        {onSubmitted}
        {onConflict}
        {onRequestOpen}
      />
    {/if}
  {/if}
</div>

<style>
  .config-toolbar {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }

  /* See the same rule in admin/logs: Input is full-width by default, which is
     right in a settings form and wrong for a filter box. */
  .config-toolbar :global(input) {
    max-width: 22rem;
  }

  .match-count {
    font-size: var(--text-xs);
    color: var(--text-dim);
    white-space: nowrap;
  }

  .problem-list {
    margin: var(--space-2) 0 0;
    padding-left: var(--space-4);
  }

  .warning-list {
    margin: var(--space-2) 0 0;
    padding: 0;
    list-style: none;
  }

  .warning-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding: var(--space-1) 0;
  }
</style>
