<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import {
    getAdminConfig,
    getAdminConnections,
    listConfigRequests,
    restoreConfigRequest,
    type AdminConfigField,
    type ConfigSecretFile,
    type AdminConfigView,
    type AdminServarrConnection,
    type ConfigOp,
    type ConfigPreview,
    type ConfigProblem,
    type ConfigRequest,
    type ConfigResource,
    type ServarrApp,
  } from '$lib/api';
  import { Button, Card, EmptyState, Input, NoticeBanner, LoadState } from '$lib/components/ui';
  import { getCurrentUser } from '$lib/userContext';
  import { notifyWarning } from '$lib/stores/notices';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import AdminsEditor from '$lib/admin/config/AdminsEditor.svelte';
  import FieldRow from '$lib/admin/config/FieldRow.svelte';
  import FieldControl from '$lib/admin/config/FieldControl.svelte';
  import HistoryList from '$lib/admin/config/HistoryList.svelte';
  import RawEditor from '$lib/admin/config/RawEditor.svelte';
  import RequestProgress from '$lib/admin/config/RequestProgress.svelte';
  import ReviewDialog from '$lib/admin/config/ReviewDialog.svelte';
  import ConfigSecretField from '$lib/admin/config/SecretField.svelte';
  import ServarrAppsEditor from '$lib/admin/config/ServarrAppsEditor.svelte';
  import {
    appProblems,
    rebaseApps,
    servarrAppsDraft,
    servarrAppsForm,
    withPrivateHost,
    type ServarrAppsForm,
  } from '$lib/admin/config/servarrApps';
  import { credentialStatus, secretsByConfigKey } from '$lib/admin/config/credentialStatus';
  import {
    buildChangeSet,
    changeCount,
    emptyDraft,
    formatValue,
    mergeDrafts,
    pathKey,
    retainExisting,
    withAdmins,
    withMove,
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
  let appliedSection: string | null | undefined;

  // Apply a navigation hint once, after the section list arrives. Reloads and
  // filter edits must not restore a hint the admin has cleared or changed.
  $effect(() => {
    const section = page.url.searchParams.get('section');
    if (!view || section === appliedSection) return;
    appliedSection = section;
    filter = view.sections.find((item) => item.key === section)?.key ?? '';
  });

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

  /** `[servarr.apps]` has a block per app rather than a TOML table. Its form
   *  starts from the servarr connection, which knows each app's URL and
   *  whether its key is set; the view's table value redacts both. */
  let servarr = $state<AdminServarrConnection | null>(null);
  let appsInitial = $state<ServarrAppsForm | null>(null);
  let appsForm = $state<ServarrAppsForm | null>(null);
  let appsProblems = $state<Record<string, string>>({});

  let editable = $derived(!!view?.editable);
  let configManaged = $derived(!!view?.managed.config.managed);
  /** The form's fields, locked while the raw editor holds the whole file. */
  let formEditable = $derived(editable && draft.raw === null);

  let servarrFields = $derived(view?.sections.find((s) => s.key === 'servarr')?.fields ?? []);
  let appsField = $derived(servarrFields.find((f) => f.key === 'servarr.apps') ?? null);
  let appsEditable = $derived(formEditable && !!appsField && appsField.editor !== 'readonly');
  let appsDraft = $derived(
    appsForm && appsInitial && appsEditable
      ? servarrAppsDraft(appsForm, appsInitial)
      : emptyDraft(),
  );
  /** `[servarr] private_hosts` as it will be once the pending change lands. */
  let servarrPrivateHosts = $derived.by(() => {
    const field = servarrFields.find((f) => f.key === 'servarr.private_hosts');
    if (!field) return [];
    const op = draft.ops[pathKey(field.path)];
    const value = op?.op === 'set' ? op.value : op?.op === 'unset' ? [] : field.value;
    return Array.isArray(value) ? value.map(String) : [];
  });

  /** Everything the review sends: the page's draft and the apps editor's part. */
  let combined = $derived(mergeDrafts(draft, appsDraft));
  let count = $derived(changeCount(combined));
  /** Form changes the raw editor cannot be combined with: ops, and moves. */
  let opCount = $derived(Object.keys(combined.ops).length + combined.move_to_file.length);

  useSettingsSave(() =>
    editable
      ? {
          dirty: count > 0,
          saving: false,
          disabled: requestId !== null || !view?.document_digest,
          label: 'Review changes',
          dirtyLabel: count > 0 ? (count === 1 ? '1 change' : `${count} changes`) : undefined,
          save: openReview,
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
  /** A credential held only inline sits with the plain-text count, not the warnings. */
  let moveWarnings = $derived(fileWarnings.filter((p) => p.action === 'move'));
  let otherWarnings = $derived(fileWarnings.filter((p) => p.action !== 'move'));

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

  /** Those with no section in the view keep a card of their own. */
  let orphanSecrets = $derived(
    standaloneSecrets.filter((s) => !view?.sections.some((section) => section.key === s.section)),
  );

  function secretMatches(secret: ConfigSecretFile, needle: string): boolean {
    return (
      secret.name.toLowerCase().includes(needle) || secret.label.toLowerCase().includes(needle)
    );
  }

  let secretByKey = $derived(secretsByConfigKey(view));

  let inlineSecrets = $derived(view ? view.secrets.filter((s) => s.source === 'inline') : []);
  /** The inline credentials this page can move; a managed plan keeps the rest. */
  let movableLabels = $derived(
    editable ? inlineSecrets.flatMap((s) => (s.movable && s.config_key ? [s.config_key] : [])) : [],
  );
  let unmovableCount = $derived(editable ? inlineSecrets.length - movableLabels.length : 0);
  let allMovesPending = $derived(
    movableLabels.length > 0 && movableLabels.every((l) => draft.move_to_file.includes(l)),
  );
  let unmovableReason = $derived(
    view?.managed.config.managed
      ? `a plan (${view.managed.config.source ?? 'unknown'}) manages config.toml`
      : view?.managed.secrets.managed
        ? `a plan (${view.managed.secrets.source ?? 'unknown'}) manages the deployment credentials`
        : null,
  );

  let managedConfig = $derived(managedResources.find((e) => e.resource === 'config') ?? null);
  let plainTextCount = $derived(Math.max(inlineSecrets.length, moveWarnings.length));

  function plural(n: number, one: string, many: string): string {
    return n === 1 ? `1 ${one}` : `${n} ${many}`;
  }

  /** Everything this page has to say about config.toml, as one banner: its
   *  title lists each part, its colour is the most serious one. */
  let fileNotice = $derived.by(() => {
    if (!view) return null;
    const blocking = editable ? blockingProblems.length : 0;
    const comments = editable && view.has_comments;
    const parts: string[] = [];
    if (blocking) parts.push(plural(blocking, 'problem to fix first', 'problems to fix first'));
    if (plainTextCount) {
      parts.push(plural(plainTextCount, 'credential in plain text', 'credentials in plain text'));
    }
    if (otherWarnings.length) parts.push(plural(otherWarnings.length, 'warning', 'warnings'));
    if (managedConfig) parts.push(`managed by ${managedConfig.source ?? 'a plan'}`);
    if (comments) parts.push('comments are not kept');
    if (!parts.length) return null;
    const variant: 'danger' | 'warn' | 'info' = blocking
      ? 'danger'
      : plainTextCount || otherWarnings.length || managedConfig
        ? 'warn'
        : 'info';
    return { title: `config.toml: ${parts.join(', ')}`, variant };
  });

  function matches(field: AdminConfigField, needle: string): boolean {
    return (
      field.key.toLowerCase().includes(needle) ||
      formatValue(field.value).toLowerCase().includes(needle)
    );
  }

  let sections = $derived.by(() => {
    if (!view) return [];
    const needle = filter.trim().toLowerCase();
    const all = view.sections.map((s) => ({
      ...s,
      secrets: standaloneSecrets.filter((secret) => secret.section === s.key),
    }));
    if (!needle) return all;
    return all
      .map((s) => ({
        ...s,
        fields: s.fields.filter((f) => matches(f, needle)),
        secrets: s.secrets.filter((secret) => secretMatches(secret, needle)),
      }))
      .filter((s) => s.fields.length + s.secrets.length > 0);
  });

  let matchCount = $derived(sections.reduce((n, s) => n + s.fields.length + s.secrets.length, 0));

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
    await Promise.all([loadServarr(), loadHistory()]);
  }

  /** The apps editor's starting point, rebased so pending edits survive a reload.
   *  Without the connection the page falls back to the plain table editor. */
  async function loadServarr() {
    if (!view || !view.sections.some((s) => s.fields.some((f) => f.key === 'servarr.apps'))) {
      return;
    }
    try {
      const { connections } = await getAdminConnections();
      servarr = connections.find((c): c is AdminServarrConnection => c.id === 'servarr') ?? servarr;
    } catch {
      // Keep the last connection; with none, the table editor stands in.
    }
    if (!servarr) return;
    const after = servarrAppsForm(servarr, view.secrets);
    if (appsForm && appsInitial) {
      appsForm = rebaseApps($state.snapshot(appsForm), $state.snapshot(appsInitial), after);
    } else {
      appsForm = structuredClone(after);
    }
    appsInitial = after;
  }

  function resetApps() {
    appsProblems = {};
    if (appsInitial) appsForm = structuredClone($state.snapshot(appsInitial));
  }

  function openReview() {
    appsProblems =
      appsForm && appsInitial && appsEditable
        ? appProblems(appsForm, appsInitial, servarrPrivateHosts)
        : {};
    if (Object.keys(appsProblems).length) {
      notifyWarning('An app under [servarr] needs fixing before the change can be reviewed.');
      return;
    }
    reviewing = true;
  }

  function allowHost(app: ServarrApp, host: string) {
    setOp(
      {
        op: 'set',
        path: ['servarr', 'private_hosts'],
        value: withPrivateHost(servarrPrivateHosts, host),
      },
      ['servarr', 'private_hosts'],
    );
    const { [`${app}.url`]: _answered, ...rest } = appsProblems;
    appsProblems = rest;
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

  function movePending(problem: ConfigProblem): boolean {
    return draft.move_to_file.includes(problem.path.join('.'));
  }

  function toggleMove(problem: ConfigProblem) {
    draft = withMove(draft, problem.path.join('.'), !movePending(problem));
  }

  /** Every movable inline credential into its file, or all of them back. */
  function toggleMoveAll() {
    const move = !allMovesPending;
    let next = draft;
    for (const label of movableLabels) next = withMove(next, label, move);
    draft = next;
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
    resetApps();
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
    const appChanges = changeCount(appsDraft);
    await load();
    if (!view) return;
    const kept = retainExisting(draft, view);
    draft = kept.draft;
    let dropped = kept.dropped;
    if (!appsEditable && appChanges) {
      dropped += appChanges;
      resetApps();
    }
    pageNotice =
      'config.toml changed on the host since this page loaded. The page now shows the new values' +
      (dropped
        ? `, and ${dropped} of your changes no longer apply and were dropped.`
        : '; your changes are kept.') +
      ' Review them again.';
  }

  function onSubmitted(id: number) {
    closeReview();
    draft = emptyDraft();
    resetApps();
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

<div class="settings config-page card-pane">
  <LoadState {loading} {error} hasData={!!view}>
    {#if view}
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

      {#each managedResources.filter((e) => e.resource !== 'config') as entry (entry.resource)}
        <NoticeBanner
          title="{RESOURCE_LABELS[entry.resource]} is managed by {entry.source ?? 'a plan'}"
          variant="warn"
          collapsed={false}
        >
          {@render managedDetail(entry.source)}
        </NoticeBanner>
      {/each}

      {#if fileNotice}
        <NoticeBanner
          title={fileNotice.title}
          variant={fileNotice.variant}
          collapsed={fileNotice.variant === 'info'}
        >
          {#if editable && blockingProblems.length}
            <div class="notice-part">
              <p class="notice-part-title">Problems to fix first</p>
              <p>The host refuses a file with these, so the next change has to fix them too.</p>
              <ul class="problem-list">
                {#each blockingProblems as problem, i (i)}
                  <li>
                    {#if problem.path.length}<code>{problem.path.join('.')}</code>{/if}
                    {problem.message}
                  </li>
                {/each}
              </ul>
            </div>
          {/if}

          {#if plainTextCount}
            <div class="notice-part">
              <p class="notice-part-title">
                {plainTextCount === 1
                  ? '1 credential is stored in config.toml in plain text'
                  : `${plainTextCount} credentials are stored in config.toml in plain text`}
              </p>
              <p>
                They work, but anyone who can read config.toml can read them. Moving one to its
                secret file is done by the host, so the value never reaches this page.
              </p>
              {#if unmovableCount}
                <p>
                  {unmovableCount === inlineSecrets.length
                    ? 'None of them can'
                    : `${unmovableCount} of them cannot`} be moved from here{unmovableReason
                    ? `, because ${unmovableReason}`
                    : ''}.
                </p>
              {/if}
              {@render warningRows(moveWarnings)}
              {#if movableLabels.length > 1 || (movableLabels.length === 1 && !moveWarnings.length)}
                <div class="banner-actions">
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={!formEditable}
                    onclick={toggleMoveAll}
                    >{allMovesPending
                      ? 'Keep all in config.toml'
                      : 'Move all to secret files'}</Button
                  >
                </div>
              {/if}
            </div>
          {/if}

          {#if otherWarnings.length}
            <div class="notice-part">
              <p class="notice-part-title">
                {otherWarnings.length === 1 ? '1 warning' : `${otherWarnings.length} warnings`}
              </p>
              <p>Istota loads the file with these. A key it ignores is kept until it is removed.</p>
              {@render warningRows(otherWarnings)}
            </div>
          {/if}

          {#if managedConfig}
            <div class="notice-part">
              <p class="notice-part-title">Managed by {managedConfig.source ?? 'a plan'}</p>
              {@render managedDetail(managedConfig.source)}
            </div>
          {/if}

          {#if editable && view.has_comments}
            <div class="notice-part">
              <p class="notice-part-title">Comments are not kept</p>
              <p>
                Applying a change rewrites the file in Istota's own layout, so its comments and
                formatting are not kept. The host keeps the previous file as
                <code>config/config.toml.bak.&lt;timestamp&gt;</code>.
              </p>
            </div>
          {/if}
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
              void loadServarr();
            }}
            onDismiss={() => {
              requestId = null;
              void load();
            }}
          />
        {/key}
      {/if}

      <div class="config-toolbar toolbar control-row">
        <Input
          bind:value={filter}
          size="toolbar"
          class="filter-input"
          placeholder="Filter settings…"
          aria-label="Filter settings"
        />
        {#if filter.trim()}
          <span class="match-count caption num">{matchCount} matching</span>
        {/if}
      </div>

      {#if !filter.trim()}
        <Card title="Admins">
          <AdminsEditor
            users={view.admins.users}
            candidates={view.admins.candidates ?? []}
            pending={draft.admins}
            {self}
            {editable}
            readOnlyReason={view.admins.read_only_reason}
            onChange={(admins) => (draft = withAdmins(draft, admins))}
          />
        </Card>

        {#if orphanSecrets.length}
          <Card title="Credentials">
            {#each orphanSecrets as secret (secret.name)}
              {@render standaloneSecret(secret)}
            {/each}
          </Card>
        {/if}
      {/if}

      {#if sections.length === 0}
        <EmptyState>No settings match “{filter}”.</EmptyState>
      {/if}

      {#each sections as section (section.key)}
        <Card title={section.label} class="config-section">
          {#each section.fields as field (field.key)}
            {#if field.key === 'servarr.apps' && appsForm}
              <ServarrAppsEditor
                bind:form={appsForm}
                problems={appsProblems}
                privateHosts={servarrPrivateHosts}
                editable={appsEditable}
                readOnlyReason={field.read_only_reason}
                onAllowHost={allowHost}
              />
            {:else}
              <FieldControl
                {field}
                op={draft.ops[pathKey(field.path)]}
                secretPending={field.credential ? draft.secrets[field.credential.name] : undefined}
                secretSource={secretByKey.get(field.key)?.source}
                editable={formEditable}
                problem={problemByKey.get(field.path.join('.'))}
                onOp={setOp}
                onSecret={setSecret}
              />
            {/if}
          {/each}
          {#each section.secrets as secret (secret.name)}
            {@render standaloneSecret(secret)}
          {/each}
        </Card>
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
          changeSet={buildChangeSet(combined, view.document_digest)}
          initialPreview={restored?.preview ?? null}
          note={restored?.note ?? ''}
          onClose={closeReview}
          {onSubmitted}
          {onConflict}
          {onRequestOpen}
        />
      {/if}
    {/if}
  </LoadState>
</div>

{#snippet managedDetail(source: string | null)}
  {#if source === 'unreadable'}
    <p>
      config/managed.json cannot be read, so everything reads as managed. Re-run
      <code>istota-stack apply</code> on the host with the intended <code>[meta]</code> to repair it.
    </p>
  {:else}
    <p>
      It is read-only here: the next run of {source ?? 'that plan'} would revert a change made from this
      page. Change it there, or remove it from that source to hand it to the web.
    </p>
  {/if}
{/snippet}

{#snippet warningRows(problems: ConfigProblem[])}
  {#if problems.length}
    <ul class="warning-list">
      {#each problems as problem, i (i)}
        <li class="warning-row">
          <span>
            {#if problem.path.length}<code>{problem.path.join('.')}</code>{/if}
            {problem.message}
          </span>
          {#if editable && problem.action === 'unset'}
            <Button
              variant="secondary"
              size="sm"
              disabled={!formEditable}
              onclick={() => toggleRemoval(problem)}
              >{removalPending(problem) ? 'Keep' : 'Remove'}</Button
            >
          {:else if editable && problem.action === 'move'}
            <Button
              variant="secondary"
              size="sm"
              disabled={!formEditable}
              onclick={() => toggleMove(problem)}
              >{movePending(problem) ? 'Keep in config.toml' : 'Move to secret file'}</Button
            >
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
{/snippet}

<!-- A secret file no config key reads, laid out as a section's own credential
     field: the name in the label column, the status at the row's far end. -->
{#snippet standaloneSecret(secret: ConfigSecretFile)}
  {@const status = credentialStatus(secret.set, secret.source)}
  <div class="standalone-field">
    {#if editable && !secret.read_only_reason}
      <FieldRow>
        <ConfigSecretField
          label={secret.name}
          hint={secret.label}
          inline
          configured={secret.set}
          {status}
          pending={draft.secrets[secret.name]}
          onChange={(value) => setSecret(secret.name, value)}
        />
      </FieldRow>
    {:else}
      <ConfigSecretField
        label={secret.name}
        hint={secret.label}
        inline
        configured={secret.set}
        {status}
        pending={undefined}
        readOnlyReason={secret.read_only_reason ?? 'Read-only'}
        onChange={() => {}}
      />
    {/if}
  </div>
{/snippet}

<style>
  .config-toolbar {
    flex-wrap: nowrap;
    margin-bottom: var(--space-3);
  }

  /* See the same rule in admin/logs: Input is full-width by default, which is
     right in a settings form and wrong for a filter box. */
  .config-toolbar :global(.filter-input) {
    max-width: 22rem;
  }

  /* About 470 fields in one column made phone WebKit stutter on scroll (ISSUE-788).
     Off-screen sections skip layout and paint; `auto` keeps each one's real height. */
  .config-page :global(.config-section) {
    content-visibility: auto;
    contain-intrinsic-size: auto 40rem;
  }

  .match-count {
    white-space: nowrap;
  }

  .banner-actions {
    margin-top: var(--space-2);
  }

  /* The status sits after the input, as it does on a section's credential. */
  .standalone-field {
    padding: var(--space-2) 0;
    border-bottom: 1px solid var(--border-subtle);
  }

  .standalone-field:last-child {
    border-bottom: none;
  }

  /* One banner holds every part; each part opens with its own title line. */
  .notice-part + .notice-part {
    margin-top: var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-subtle);
  }

  .notice-part-title {
    font-weight: var(--weight-semibold);
    color: var(--text-primary);
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
