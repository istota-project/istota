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
    JobSaveError,
    updateScheduledJob,
    type ScheduledJob,
    type ScheduledJobFields,
    type ScheduledJobs,
  } from '$lib/api';
  import { deliveryFromJob, jobTarget } from '$lib/settings/jobDelivery';
  import { getCurrentUser } from '$lib/userContext';
  import {
    Alert,
    Button,
    Card,
    Checkbox,
    ConfirmDialog,
    Field,
    FormGrid,
    FormStack,
    Input,
    TextArea,
    ToggleGroup,
  } from '@minot/ui';
  import { notifyError, notifyWarning } from '@minot/ui/stores/notices';
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import { createDraft } from '@minot/ui/ui/draft.svelte';
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
  const FIELDS = new Set(['name', 'cron', 'kind', 'prompt', 'command', 'target', 'room', 'reach']);

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
  const fromFile = $derived(
    definition
      ? [
          { label: 'Model', value: definition.model },
          { label: 'Effort', value: definition.effort },
          { label: 'Brain', value: definition.brain },
          { label: 'Provider', value: definition.provider },
          {
            label: 'Publishes to',
            value:
              definition.publish_shared_kv +
              (definition.publish_shared_kv && definition.publish_shared_kv_trusted
                ? ', marked trusted'
                : ''),
          },
          { label: 'Asks for', value: definition.egress.join(', ') },
        ].filter((row) => row.value)
      : [],
  );

  $effect(() => {
    if (draft.dirty) return;
    const loaded = fromJob(job);
    if (JSON.stringify(loaded) !== JSON.stringify(draft.saved)) draft.reset(loaded);
    revision = job?.revision ?? null;
  });

  /** The server strips text before it digests it, so spaces alone change nothing. */
  function textChanged(): boolean {
    const saved = draft.saved;
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

    {#if fromFile.length > 0}
      <dl class="kv">
        {#each fromFile as row (row.label)}
          <dt>{row.label}</dt>
          <dd class="mono">{row.value}</dd>
        {/each}
      </dl>
    {/if}

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
  .job-flags {
    --vstack-gap: var(--space-2);
  }
</style>
