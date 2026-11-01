<script lang="ts" module>
  /** What the page says after a save: the approval asked for did not land. */
  export interface JobSaved {
    accessLost: boolean;
  }
</script>

<script lang="ts">
  import { base } from '$app/paths';
  import {
    AuthError,
    createScheduledJob,
    getJobModelChoices,
    JobSaveError,
    updateScheduledJob,
    type ScheduledJob,
    type ScheduledJobFields,
    type ScheduledJobs,
  } from '$lib/api';
  import {
    effectiveProvider,
    effortOptions,
    effortsFor,
    filterOptions,
    listsModel,
    modelChoicesFrom,
    modelSurvivesProviderMove,
    pickerOptions,
    providerOptions,
    valueOf,
    type ModelChoices,
  } from '$lib/components/chat/modelPicker';
  import { deliveryFromJob, jobTarget } from '$lib/settings/jobDelivery';
  import { getCurrentUser } from '$lib/userContext';
  import {
    Alert,
    AutocompleteInput,
    Button,
    Card,
    Checkbox,
    ConfirmDialog,
    Disclosure,
    Field,
    FormGrid,
    FormStack,
    IconButton,
    Input,
    Select,
    TextArea,
    ToggleGroup,
  } from '@minot/ui';
  import { notifyError, notifyWarning } from '@minot/ui/stores/notices';
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import { createDraft } from '@minot/ui/ui/draft.svelte';
  import { latest } from '@minot/ui/ui/latest';
  import { X } from '@lucide/svelte';
  import { onDestroy } from 'svelte';
  import DeliveryFields from './DeliveryFields.svelte';

  type Reach = 'limited' | 'public' | 'full';
  type Kept = 'open' | 'public';

  let {
    job,
    options,
    timezone,
    onSaved,
    onReload,
  }: {
    /** Null on create. */
    job: ScheduledJob | null;
    options: ScheduledJobs['options'];
    timezone: string;
    onSaved: (saved: JobSaved) => void | Promise<void>;
    onReload: () => void | Promise<void>;
  } = $props();

  const identity = getCurrentUser();

  const KIND_OPTIONS = [
    { value: 'prompt', label: 'Prompt' },
    { value: 'command', label: 'Command' },
  ];
  const REACH_OPTIONS = [
    { value: 'limited', label: 'Limited' },
    { value: 'public', label: 'Public' },
    { value: 'full', label: 'Full access' },
  ];
  const ACCESS_LABEL: Record<Kept, string> = { open: 'Full access', public: 'Public' };
  const CONFIG_EDITOR_HREF = `${base}/files?view=config`;
  // Where a refusal's `field` is drawn. Anything else has no place in this form.
  const RUN_FIELDS = new Set(['model', 'effort', 'brain', 'provider']);
  const FIELDS = new Set([
    'name',
    'cron',
    'kind',
    'prompt',
    'command',
    'target',
    'room',
    'reach',
    'publish_shared_kv',
    'publish_shared_kv_trusted',
    'egress',
    ...RUN_FIELDS,
  ]);
  const NO_CHOICES: ModelChoices = {
    aliases: [],
    models: [],
    defaultModel: null,
    roomModel: null,
    providers: [],
  };

  /** The output a new job starts on: a room if there is one to deliver into. */
  function firstOutput(outputs: string[]): string {
    return ['web', 'talk'].find((output) => outputs.includes(output)) ?? outputs[0] ?? '';
  }

  function fromJob(source: ScheduledJob | null) {
    const definition = source?.definition;
    const delivery = definition
      ? deliveryFromJob(definition.target, definition.room)
      : { output: firstOutput(options.outputs), room: '' };
    return {
      name: source?.name ?? '',
      cron: source?.cron ?? '',
      kind: definition?.kind ?? ('prompt' as 'prompt' | 'command'),
      prompt: definition?.prompt ?? '',
      command: definition?.command ?? '',
      output: delivery.output,
      room: delivery.room,
      enabled: source?.enabled ?? true,
      once: source?.once ?? false,
      silent_unless_action: definition?.silent_unless_action ?? false,
      skip_log_channel: definition?.skip_log_channel ?? false,
      reach: 'limited' as Reach,
      model: definition?.model ?? '',
      effort: definition?.effort ?? '',
      brain: definition?.brain ?? '',
      provider: definition?.provider ?? '',
      publish_shared_kv: definition?.publish_shared_kv ?? '',
      publish_shared_kv_trusted: definition?.publish_shared_kv_trusted ?? false,
      egress: definition?.egress ?? [],
    };
  }

  // svelte-ignore state_referenced_locally
  const draft = createDraft(fromJob(job));
  const form = $derived(draft.value);
  // What the page read. A save compares it with the file, so a reload that brings in
  // somebody else's edit must not move it under an open edit.
  // svelte-ignore state_referenced_locally
  let revision = $state(job?.revision ?? null);
  let saving = $state(false);
  let fieldErrors = $state<Record<string, string>>({});
  let formErrors = $state<string[]>([]);
  const keeping = createConfirm<Kept>();
  const renaming = createConfirm<true>();

  const creating = $derived(job === null);
  const readOnly = $derived(job !== null && !job.editable);
  const network = $derived(job?.network ?? null);
  const definition = $derived(job?.definition ?? null);
  const keepLabel = $derived(keeping.target ? `Keep ${ACCESS_LABEL[keeping.target]}` : '');
  const isAdmin = $derived(identity.user.is_admin);

  // svelte-ignore state_referenced_locally
  const startedWithRunFields = !!(
    job?.definition.model ||
    job?.definition.effort ||
    job?.definition.brain ||
    job?.definition.provider
  );
  let runOpen = $state(startedWithRunFields);

  let choices = $state<ModelChoices>(NO_CHOICES);
  let choicesLoaded = $state(false);
  let choicesFailed = $state(false);
  // Set when the person moved the brain, not when a reload did: only their change may
  // clear a model, and only once the new list has come back to say it lacks it.
  let brainMoved = false;
  const loads = latest();
  onDestroy(() => loads.dispose());

  const offersProvider = $derived((choices.providers ?? []).length > 0);
  const shownProvider = $derived(offersProvider ? effectiveProvider(choices, form.provider) : null);
  const modelOptions = $derived(pickerOptions(choices, shownProvider));
  const modelLabels = $derived(modelOptions.map((option) => option.label));
  const modelFilter = $derived(filterOptions(modelOptions, draft.saved.model || null));
  const modelValue = $derived(valueOf(modelOptions, form.model));
  const effortSelectOptions = $derived(
    effortOptions(effortsFor(modelOptions, null, modelValue), form.effort),
  );
  const modelUnlisted = $derived(
    choicesLoaded &&
      !!modelValue &&
      modelValue === draft.saved.model &&
      !listsModel(modelOptions, modelValue),
  );

  const canSetBrain = $derived(options.selectable_brains.length > 0 || isAdmin);
  const brainOffered = $derived(options.selectable_brains.some((b) => b.kind === form.brain));
  const showBrain = $derived(
    canSetBrain && (options.selectable_brains.length > 0 || !!draft.saved.brain),
  );
  const brainOptions = $derived([
    { value: '', label: 'Default brain' },
    ...options.selectable_brains.map((b) => ({ value: b.kind, label: b.label })),
    ...(form.brain && !brainOffered
      ? [{ value: form.brain, label: `${form.brain} (not offered)` }]
      : []),
  ]);
  const providerSelectOptions = $derived.by(() => {
    const listed = providerOptions(choices);
    if (!form.provider || listed.some((option) => option.value === form.provider)) return listed;
    const known = (choices.providers ?? []).some((p) => p.id === form.provider);
    return [
      ...listed,
      { value: form.provider, label: known ? form.provider : `${form.provider} (not offered)` },
    ];
  });
  // What the sync drops for this owner, or what cannot apply on the job's brain.
  const ignoredPin = $derived.by(() => {
    if (!canSetBrain) {
      if (form.brain && form.provider)
        return "This job's brain and provider settings are ignored because only an admin can set them.";
      if (form.brain)
        return "This job's brain setting is ignored because only an admin can set it.";
      if (form.provider)
        return "This job's provider setting is ignored because only an admin can set it.";
      return null;
    }
    if (form.provider && choicesLoaded && !offersProvider)
      return "This job's provider setting is ignored, since only the native brain has a provider.";
    return null;
  });
  const ignoredPinAction = $derived(
    !canSetBrain && form.brain && form.provider
      ? 'Remove the brain and provider'
      : !canSetBrain && form.brain
        ? 'Remove the brain'
        : 'Remove the provider',
  );

  const showPublish = $derived(options.can_publish || !!form.publish_shared_kv);
  const publishTarget = $derived(form.publish_shared_kv.trim());
  const publishTrusted = $derived(!!publishTarget && form.publish_shared_kv_trusted);
  const keptAsks = $derived(definition?.egress_kept ?? []);
  let askText = $state('');

  $effect(() => {
    void loadChoices(form.brain, form.provider);
  });

  async function loadChoices(brain: string, provider: string) {
    const current = loads.next();
    try {
      const answer = await getJobModelChoices(brain, provider);
      if (!current()) return;
      choices = modelChoicesFrom(answer);
      choicesLoaded = true;
      choicesFailed = false;
      if (brainMoved && !listsModel(modelOptions, modelValue)) {
        form.model = '';
        form.effort = '';
      }
    } catch (e) {
      if (!current()) return;
      if (e instanceof AuthError) {
        identity.expireSession();
        return;
      }
      choicesFailed = true;
    }
    brainMoved = false;
  }

  function chooseBrain(next: string) {
    if (next === form.brain) return;
    brainMoved = true;
    form.brain = next;
  }

  function chooseProvider(next: string) {
    if (next === form.provider) return;
    const stored = { provider: draft.saved.provider, model: draft.saved.model };
    if (!modelSurvivesProviderMove(choices, modelOptions, modelValue, next, stored)) {
      form.model = '';
      form.effort = '';
    }
    form.provider = next;
  }

  function removeIgnoredPin() {
    if (!canSetBrain) form.brain = '';
    form.provider = '';
  }

  function removePublish() {
    form.publish_shared_kv = '';
    form.publish_shared_kv_trusted = false;
  }

  function addAsk() {
    const entry = askText.trim();
    askText = '';
    if (entry && !form.egress.includes(entry)) form.egress = [...form.egress, entry];
  }

  function removeAsk(entry: string) {
    form.egress = form.egress.filter((other) => other !== entry);
  }

  $effect(() => {
    if (draft.dirty) return;
    const loaded = fromJob(job);
    if (JSON.stringify(loaded) !== JSON.stringify(draft.saved)) draft.reset(loaded);
    revision = job?.revision ?? null;
  });

  /** What the digest covers: the text and where its answer is published. The server strips
   *  text before it digests it, so spaces alone change nothing. */
  function textChanged(): boolean {
    const saved = draft.saved;
    if (publishTarget !== saved.publish_shared_kv.trim()) return true;
    if (publishTrusted !== saved.publish_shared_kv_trusted) return true;
    if (form.kind !== saved.kind) return true;
    return form.kind === 'prompt'
      ? form.prompt.trim() !== saved.prompt.trim()
      : form.command.trim() !== saved.command.trim();
  }

  function renamed(): boolean {
    return form.name.trim() !== draft.saved.name;
  }

  function heldApproval(): Kept | null {
    if (!network || network.state === 'limited') return null;
    return network.state;
  }

  function hasNetworkAccess(): boolean {
    if (!network) return false;
    return (
      network.hosts.length > 0 ||
      network.skills.length > 0 ||
      network.approvals.length > 0 ||
      network.state !== 'limited'
    );
  }

  function textFields(): ScheduledJobFields {
    return form.kind === 'prompt'
      ? { kind: 'prompt', prompt: form.prompt }
      : { kind: 'command', command: form.command };
  }

  function newJobFields(): ScheduledJobFields {
    return {
      name: form.name,
      cron: form.cron,
      ...textFields(),
      ...jobTarget(form.output, form.room),
      enabled: form.enabled,
      once: form.once,
      silent_unless_action: form.silent_unless_action,
      skip_log_channel: form.skip_log_channel,
      reach: form.reach,
      ...(modelValue ? { model: modelValue } : {}),
      ...(form.effort ? { effort: form.effort } : {}),
      ...(form.brain ? { brain: form.brain } : {}),
      ...(form.provider ? { provider: form.provider } : {}),
      ...(publishTarget ? { publish_shared_kv: publishTarget } : {}),
      ...(publishTrusted ? { publish_shared_kv_trusted: true } : {}),
      ...(form.egress.length > 0 ? { egress: form.egress } : {}),
    };
  }

  /** Only what changed: what is absent stays as the file has it, so an edit made
   *  elsewhere while this form was open survives the save. */
  function changedFields(): ScheduledJobFields {
    const changed = draft.changed();
    const fields: ScheduledJobFields = {};
    if ('name' in changed) fields.name = form.name;
    if ('cron' in changed) fields.cron = form.cron;
    const text = form.kind === 'prompt' ? 'prompt' : 'command';
    if ('kind' in changed || text in changed) Object.assign(fields, textFields());
    if ('output' in changed || 'room' in changed)
      Object.assign(fields, jobTarget(form.output, form.room));
    if ('enabled' in changed) fields.enabled = form.enabled;
    if ('once' in changed) fields.once = form.once;
    if ('silent_unless_action' in changed) fields.silent_unless_action = form.silent_unless_action;
    if ('skip_log_channel' in changed) fields.skip_log_channel = form.skip_log_channel;
    const saved = draft.saved;
    if (modelValue !== saved.model) fields.model = modelValue;
    if ('effort' in changed) fields.effort = form.effort;
    if ('brain' in changed) fields.brain = form.brain;
    if ('provider' in changed) fields.provider = form.provider;
    if (publishTarget !== saved.publish_shared_kv.trim()) fields.publish_shared_kv = publishTarget;
    if (publishTrusted !== saved.publish_shared_kv_trusted)
      fields.publish_shared_kv_trusted = publishTrusted;
    if ('egress' in changed) fields.egress = form.egress;
    return fields;
  }

  function checkLocally(): boolean {
    const deliveryChanged = 'output' in draft.changed() || 'room' in draft.changed();
    if (form.output === 'talk' && !form.room && (creating || deliveryChanged)) {
      fieldErrors = { room: 'Choose a Talk room.' };
      return false;
    }
    return true;
  }

  function submit() {
    if (saving || readOnly) return;
    fieldErrors = {};
    formErrors = [];
    addAsk();
    if (!checkLocally()) return;
    if (job) {
      if (renamed() && hasNetworkAccess()) {
        renaming.ask(true);
        return;
      }
      const held = !renamed() && textChanged() ? heldApproval() : null;
      if (held) {
        keeping.ask(held);
        return;
      }
    }
    void save(null);
  }

  async function save(keep: Kept | null) {
    saving = true;
    try {
      let accessLost = false;
      if (!job) {
        await createScheduledJob(newJobFields());
      } else if (job.id !== null && revision !== null) {
        const answer = await updateScheduledJob(job.id, {
          ...changedFields(),
          ...(keep ? { keep_access: keep } : {}),
          revision,
        });
        accessLost = keep !== null && answer.access_kept === false;
      }
      draft.reset();
      await onSaved({ accessLost });
    } catch (e) {
      await refused(e);
    } finally {
      saving = false;
    }
  }

  async function refused(e: unknown) {
    if (e instanceof AuthError) {
      identity.expireSession();
      return;
    }
    if (!(e instanceof JobSaveError)) {
      notifyError((e as Error).message || 'That could not be saved.');
      return;
    }
    if (e.field && FIELDS.has(e.field)) {
      fieldErrors = { [e.field]: e.message };
      if (RUN_FIELDS.has(e.field)) runOpen = true;
    } else if (e.errors.length > 0) {
      formErrors = e.errors;
    } else if (e.code === 'job_changed') {
      notifyWarning(
        'This job changed since you opened it. Your edits are still here: check them and save again.',
      );
      await onReload();
      revision = job?.revision ?? revision;
    } else if (e.status === 404) {
      await onReload();
    } else {
      notifyError(e.message);
    }
  }

  function setOutput(output: string) {
    form.output = output;
    form.room = '';
  }
</script>

<Card
  title={creating ? 'Job' : 'Definition'}
  description={readOnly
    ? 'Read only here, since CRON.md cannot be changed for this job.'
    : undefined}
>
  <FormStack as="form" onsubmit={submit} disabled={saving || readOnly} novalidate>
    <FormGrid min="14rem">
      <Field label="Name" error={fieldErrors.name}>
        <Input bind:value={form.name} invalid={!!fieldErrors.name} />
      </Field>
      <Field
        label="Schedule"
        description="A cron expression, in your timezone ({timezone})."
        error={fieldErrors.cron}
      >
        <Input
          bind:value={form.cron}
          monospace
          placeholder="0 7 * * *"
          invalid={!!fieldErrors.cron}
        />
      </Field>
    </FormGrid>

    <Field label="Kind" error={fieldErrors.kind}>
      <ToggleGroup bind:value={form.kind} options={KIND_OPTIONS} size="md" label="Kind" />
    </Field>

    {#if form.kind === 'prompt'}
      <Field
        label="Prompt"
        wide
        description={definition?.prompt_file
          ? `Read from ${definition.prompt_file}. Changing it here moves the text out of that file, which stays where it is.`
          : undefined}
        error={fieldErrors.prompt}
      >
        <TextArea bind:value={form.prompt} rows={6} invalid={!!fieldErrors.prompt} />
      </Field>
    {:else}
      <Field label="Command" wide error={fieldErrors.command}>
        <TextArea bind:value={form.command} rows={3} monospace invalid={!!fieldErrors.command} />
      </Field>
    {/if}

    <DeliveryFields
      outputs={options.outputs}
      talkRooms={options.rooms.talk}
      webRooms={options.rooms.web}
      output={form.output}
      room={form.room}
      onOutputChange={setOutput}
      onRoomChange={(room) => (form.room = room)}
      outputError={fieldErrors.target}
      roomError={fieldErrors.room}
    />

    <div class="vstack job-flags">
      <Checkbox bind:checked={form.enabled} label="Enabled" />
      <Checkbox bind:checked={form.once} label="One time" />
      <Checkbox
        bind:checked={form.silent_unless_action}
        label="Stay quiet unless there is something to act on"
      />
      <Checkbox bind:checked={form.skip_log_channel} label="Leave out of the log channel" />
    </div>

    {#if creating}
      <Field
        label="Access"
        description="What the job may reach. Full access asks for approval on the job's page when your operator's setting does not cover it."
        error={fieldErrors.reach}
      >
        <ToggleGroup bind:value={form.reach} options={REACH_OPTIONS} size="md" label="Access" />
      </Field>
    {/if}

    <Disclosure summary="Model and run" bind:open={runOpen}>
      <div class="vstack">
        {#if showBrain}
          <Field
            label="Brain"
            description="A job that names a brain runs on it, and does not fall back to another if it is unavailable."
            warning={form.brain && !brainOffered
              ? 'This deployment no longer offers this brain, so the job falls through to the default at dispatch.'
              : undefined}
            error={fieldErrors.brain}
          >
            <Select
              value={form.brain}
              options={brainOptions}
              onValueChange={chooseBrain}
              ariaLabel="Brain"
              fullWidth
            />
          </Field>
        {/if}
        {#if offersProvider}
          <Field
            label="Provider"
            description="Which of the native brain's providers this job runs on, and so whose key it is billed to."
            error={fieldErrors.provider}
          >
            <Select
              value={form.provider}
              options={providerSelectOptions}
              onValueChange={chooseProvider}
              ariaLabel="Provider"
              fullWidth
            />
          </Field>
        {/if}
        {#if ignoredPin}
          <div class="hstack wrap job-row">
            <p class="caption">{ignoredPin}</p>
            <Button variant="secondary" size="sm" onclick={removeIgnoredPin}>
              {ignoredPinAction}
            </Button>
          </div>
        {/if}
        <FormGrid min="14rem">
          <Field
            label="Model"
            description={choicesFailed
              ? 'The list of models could not be loaded. You can still type one.'
              : undefined}
            warning={modelUnlisted
              ? 'Not in your list. It stays as it is until you change it, and the job may run on the default model.'
              : undefined}
            error={fieldErrors.model}
          >
            <AutocompleteInput
              bind:value={form.model}
              options={modelLabels}
              filter={modelFilter}
              limit={500}
              onSelect={(label) => (form.model = valueOf(modelOptions, label))}
              placeholder={choices.defaultModel
                ? `Default model (${choices.defaultModel})`
                : 'Default model'}
              invalid={!!fieldErrors.model}
            />
          </Field>
          <Field label="Effort" error={fieldErrors.effort}>
            <Select
              value={form.effort}
              options={effortSelectOptions}
              onValueChange={(effort) => (form.effort = effort)}
              ariaLabel="Effort"
              fullWidth
            />
          </Field>
        </FormGrid>
      </div>
    </Disclosure>

    {#if showPublish}
      <div class="vstack job-section">
        <h3>Publish</h3>
        <p class="caption">
          This answer goes into other users' briefings. A trusted one is not fenced as untrusted
          there.
        </p>
        {#if options.can_publish}
          <Field
            label="Publish to"
            description="A namespace and key, as namespace/key, or a key alone for a shared briefing block."
            error={fieldErrors.publish_shared_kv}
          >
            <Input
              bind:value={form.publish_shared_kv}
              monospace
              invalid={!!fieldErrors.publish_shared_kv}
            />
          </Field>
          <Field label="Trusted" checkbox error={fieldErrors.publish_shared_kv_trusted}>
            <Checkbox bind:checked={form.publish_shared_kv_trusted} disabled={!publishTarget} />
          </Field>
        {:else}
          <div class="hstack wrap job-row">
            <code>{form.publish_shared_kv}</code>
            {#if form.publish_shared_kv_trusted}<span class="caption">Marked trusted</span>{/if}
            <Button variant="secondary" size="sm" onclick={removePublish}>
              Remove the publish target
            </Button>
          </div>
          {#if fieldErrors.publish_shared_kv}
            <p class="form-error">{fieldErrors.publish_shared_kv}</p>
          {/if}
        {/if}
      </div>
    {/if}

    <div class="vstack job-section">
      <h3>Asks ahead of its first run</h3>
      <p class="caption">
        This asks and grants nothing. Hosts it should have are added under Network access{creating
          ? " on the job's page"
          : ' below'}. A one-time job with an unmet entry does not fire until it is answered.
      </p>
      {#if form.egress.length > 0 || keptAsks.length > 0}
        <ul class="list-reset divided-list">
          {#each form.egress as entry (entry)}
            <li class="hstack job-row">
              <code>{entry}</code>
              <IconButton
                label={`Remove ${entry}`}
                icon={X}
                size="sm"
                onclick={() => removeAsk(entry)}
              />
            </li>
          {/each}
          {#each keptAsks as entry (entry)}
            <li class="hstack wrap job-row">
              <code>{entry}</code>
              <span class="caption">
                This asks for nothing: it is not a skill that needs approval and that you can use.
              </span>
            </li>
          {/each}
        </ul>
      {/if}
      <Field label="Ask for" description="A host, or skill:name." error={fieldErrors.egress}>
        <div class="hstack">
          <Input
            bind:value={askText}
            monospace
            invalid={!!fieldErrors.egress}
            onkeydown={(event: KeyboardEvent) => {
              if (event.key !== 'Enter') return;
              event.preventDefault();
              addAsk();
            }}
          />
          <Button variant="secondary" onclick={addAsk}>Add</Button>
        </div>
      </Field>
    </div>

    {#if formErrors.length > 0}
      <Alert appearance="inline" variant="danger" role="alert">
        {#each formErrors as problem, index (index)}<p>{problem}</p>{/each}
      </Alert>
    {/if}

    {#if !readOnly}
      <div class="form-actions">
        <Button
          type="submit"
          variant="primary"
          disabled={!creating && !draft.dirty}
          loading={saving}
        >
          {creating ? 'Create job' : 'Save'}
        </Button>
      </div>
    {/if}
  </FormStack>
  {#if readOnly}
    <p class="caption">
      <a href={CONFIG_EDITOR_HREF}>Open CRON.md under Files, Configuration.</a>
    </p>
  {/if}
</Card>

<ConfirmDialog
  bind:open={keeping.open}
  title={keepLabel}
  message={keeping.target
    ? `This job has ${ACCESS_LABEL[keeping.target]} for its current text. Are you sure you want to keep it for the new text? You can also save and run limited.`
    : ''}
  confirmLabel={keepLabel}
  confirmVariant="primary"
  secondaryLabel="Save and run limited"
  onSecondary={() => save(null)}
  onConfirm={() => save(keeping.target)}
/>

<ConfirmDialog
  bind:open={renaming.open}
  title="Rename job"
  message="Its network access starts over under the new name: its hosts, skills and approvals do not move with it. Are you sure you want to rename it?"
  confirmLabel="Rename and save"
  confirmVariant="primary"
  onConfirm={() => save(null)}
/>

<style>
  .job-flags,
  .job-section {
    --vstack-gap: var(--space-2);
  }
  .job-row {
    justify-content: space-between;
  }
</style>
