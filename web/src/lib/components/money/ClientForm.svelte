<script lang="ts">
  import { untrack } from 'svelte';
  import {
    KEY_RE,
    KEY_HINT,
    normalizeClientKey,
    type ClientConfigRow,
    type ClientInput,
    type EntityRow,
  } from '$lib/money/api';
  import { Modal, Button, Select, type SelectOption } from '$lib/components/ui';
  import { SettingsField } from '$lib/components/settings';

  /**
   * Create/edit form for an invoicing client.
   *
   * Two payload rules, shared with the sibling entity and service forms:
   *
   * - **Optional fields clear with `""`, never `null`.** The store skips null
   *   values when merging, so a null would leave the old value in place while
   *   the form showed the field as cleared.
   * - **`bundles` and `separate` are omitted entirely.** The merge preserves
   *   what's stored, which is what lets this form skip a nested-list editor
   *   without dropping either on every save.
   *
   * The `key` is the identity — work entries reference it, and none of those
   * references are foreign keys — so it is set on create and read-only after.
   */
  interface Props {
    /** The client being edited (raw config shape), or null when adding. */
    client?: ClientConfigRow | null;
    entities?: EntityRow[];
    /** Named in the "use the default" entity option. */
    defaultEntity?: string;
    onSave: (key: string, data: ClientInput) => void;
    onCancel: () => void;
    error?: string;
    saving?: boolean;
  }

  let {
    client = null,
    entities = [],
    defaultEntity = '',
    onSave,
    onCancel,
    error = '',
    saving = false,
  }: Props = $props();

  // Mounted fresh each time it opens, so the initial props are the whole
  // story — the local fields deliberately don't track them.
  const isEdit = untrack(() => !!client);

  let key = $state(untrack(() => client?.key ?? ''));
  let name = $state(untrack(() => client?.name ?? ''));
  let email = $state(untrack(() => client?.email ?? ''));
  let address = $state(untrack(() => client?.address ?? ''));
  let terms = $state(untrack(() => (client?.terms != null ? String(client.terms) : '')));
  let entity = $state(untrack(() => client?.entity ?? ''));
  let arAccount = $state(untrack(() => client?.ar_account ?? ''));
  let schedule = $state(untrack(() => client?.schedule || 'on-demand'));
  let scheduleDay = $state(untrack(() => String(client?.schedule_day ?? 1)));
  let ledgerPosting = $state(untrack(() => client?.ledger_posting ?? true));
  let reminderDays = $state(untrack(() => String(client?.reminder_days ?? 3)));
  let notifications = $state(untrack(() => client?.notifications ?? ''));
  let daysUntilOverdue = $state(untrack(() => String(client?.days_until_overdue ?? 0)));
  let advancedOpen = $state(false);
  let open = $state(true);

  const KNOWN_SCHEDULES = ['on-demand', 'monthly'];
  const baseScheduleOptions: SelectOption[] = [
    { value: 'on-demand', label: 'On demand' },
    { value: 'monthly', label: 'Monthly' },
  ];

  // A client migrated from legacy TOML can carry a schedule outside the set
  // (it was simply never picked up by the scheduler). Surfacing it as its own
  // option keeps the record editable and shows the user what it actually says,
  // instead of the dropdown reading "On demand" for a record that doesn't.
  const legacySchedule = untrack(() =>
    client?.schedule && !KNOWN_SCHEDULES.includes(client.schedule) ? client.schedule : '',
  );
  const scheduleOptions: SelectOption[] = legacySchedule
    ? [...baseScheduleOptions, { value: legacySchedule, label: `${legacySchedule} (unrecognised)` }]
    : baseScheduleOptions;
  const scheduleWarning = $derived(
    legacySchedule && schedule === legacySchedule
      ? `"${legacySchedule}" is not a schedule — this client is never invoiced automatically.`
      : '',
  );

  const entityOptions = $derived.by<SelectOption[]>(() => {
    const opts: SelectOption[] = [
      { value: '', label: defaultEntity ? `Use default (${defaultEntity})` : 'Use default' },
      ...entities.map((e) => ({ value: e.key, label: e.name || e.key })),
    ];
    // Keep a client pointing at a since-removed entity selectable rather than
    // silently reassigning it on the next save.
    if (entity && !entities.some((e) => e.key === entity)) {
      opts.push({ value: entity, label: `${entity} (unknown)` });
    }
    return opts;
  });

  const keyError = $derived(!isEdit && key && !KEY_RE.test(key) ? KEY_HINT : '');
  // A numeric terms value is a day count either way: the column is TEXT and
  // the loader coerces "-5" back to -5, which renders a due date before the
  // invoice date. Caught here as well as server-side so the message lands on
  // the field.
  const termsError = $derived.by(() => {
    const trimmed = terms.trim();
    if (!trimmed) return '';
    const asNumber = Number(trimmed);
    return Number.isInteger(asNumber) && asNumber < 0 ? 'Expected 0 days or more' : '';
  });
  const canSave = $derived(
    !!name.trim() && (isEdit || (!!key && !keyError)) && !termsError && !saving,
  );

  /** A blank numeric field means "leave it alone", not zero. */
  function intOrOmit(raw: string): number | undefined {
    const trimmed = raw.trim();
    if (!trimmed) return undefined;
    const parsed = Number(trimmed);
    return Number.isInteger(parsed) ? parsed : undefined;
  }

  function handleSave() {
    if (!canSave) return;

    const data: ClientInput = {
      name: name.trim(),
      // Empty string, not undefined: these are the fields a user clears.
      email: email.trim(),
      address,
      entity,
      ar_account: arAccount.trim(),
      schedule,
      notifications: notifications.trim(),
      ledger_posting: ledgerPosting,
    };

    // Terms is `int | str` server-side: "30" is a day count, "NET 15" a label.
    // Blank preserves whatever is stored (the store refuses an empty value).
    const trimmedTerms = terms.trim();
    if (trimmedTerms) {
      const asNumber = Number(trimmedTerms);
      data.terms = Number.isInteger(asNumber) && asNumber >= 0 ? asNumber : trimmedTerms;
    }

    // Only meaningful on a monthly schedule; an on-demand client keeps
    // whatever day is stored rather than having one invented for it.
    if (schedule === 'monthly') {
      const day = intOrOmit(scheduleDay);
      if (day !== undefined) data.schedule_day = day;
    }

    const reminder = intOrOmit(reminderDays);
    if (reminder !== undefined) data.reminder_days = reminder;
    const overdue = intOrOmit(daysUntilOverdue);
    if (overdue !== undefined) data.days_until_overdue = overdue;

    onSave(isEdit ? (client as ClientConfigRow).key : key.trim(), data);
  }

  function handleOpenChange(next: boolean) {
    if (!next) onCancel();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key !== 'Enter') return;
    const target = e.target as HTMLElement | null;
    // Only a single-line text input commits. Enter anywhere else is that
    // control's own business — notably confirming a dropdown option, which
    // would otherwise select *and* save in one keystroke.
    if (!(target instanceof HTMLInputElement)) return;
    if (target.type === 'checkbox') return;
    handleSave();
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<Modal
  bind:open
  title={isEdit ? `Edit ${client?.name || client?.key}` : 'Add client'}
  onOpenChange={handleOpenChange}
  width="420px"
>
  <div class="form-grid">
    {#if isEdit}
      <div class="static-key">
        <span>Key</span>
        <code>{client?.key}</code>
        <small>The key is the identity — work entries reference it by name.</small>
      </div>
    {:else}
      <SettingsField
        label="Key"
        hint="Short identifier used by work entries. Lowercase."
        error={keyError}
      >
        <input
          type="text"
          value={key}
          oninput={(e) => (key = normalizeClientKey(e.currentTarget.value))}
          placeholder="acme"
          autocomplete="off"
        />
      </SettingsField>
    {/if}

    <SettingsField label="Name">
      <input type="text" bind:value={name} placeholder="Acme Corp" />
    </SettingsField>

    <SettingsField label="Email">
      <input type="text" bind:value={email} placeholder="ap@acme.example" />
    </SettingsField>

    <SettingsField label="Address">
      <textarea rows="3" bind:value={address}></textarea>
    </SettingsField>

    <SettingsField
      label="Terms"
      hint="A number of days, or a label like NET 15."
      error={termsError}
    >
      <input type="text" bind:value={terms} placeholder="30" />
    </SettingsField>

    <SettingsField label="Entity" hint="Which of your entities bills this client.">
      <Select bind:value={entity} options={entityOptions} fullWidth ariaLabel="Entity" />
    </SettingsField>

    <SettingsField label="A/R account" hint="Blank falls back to the business default.">
      <input type="text" bind:value={arAccount} placeholder="Assets:Accounts-Receivable" />
    </SettingsField>

    <SettingsField label="Schedule" warning={scheduleWarning}>
      <Select bind:value={schedule} options={scheduleOptions} fullWidth ariaLabel="Schedule" />
    </SettingsField>

    {#if schedule === 'monthly'}
      <SettingsField label="Schedule day" hint="Clamped to the last day of shorter months.">
        <input type="text" inputmode="numeric" bind:value={scheduleDay} placeholder="1" />
      </SettingsField>
    {/if}

    <SettingsField label="Post income to ledger on payment" checkbox>
      <input type="checkbox" bind:checked={ledgerPosting} />
    </SettingsField>

    <details bind:open={advancedOpen}>
      <summary>Advanced</summary>
      <div class="form-grid">
        <SettingsField label="Reminder days">
          <input type="text" inputmode="numeric" bind:value={reminderDays} placeholder="3" />
        </SettingsField>
        <SettingsField label="Notifications" hint="Where overdue notices go.">
          <input type="text" bind:value={notifications} />
        </SettingsField>
        <SettingsField label="Days until overdue">
          <input type="text" inputmode="numeric" bind:value={daysUntilOverdue} placeholder="0" />
        </SettingsField>
      </div>
    </details>
  </div>

  {#if error}
    <div class="form-error">{error}</div>
  {/if}

  {#snippet footer()}
    <Button variant="ghost" onclick={onCancel}>Cancel</Button>
    <Button variant="primary" onclick={handleSave} disabled={!canSave}>
      {saving ? 'Saving…' : 'Save'}
    </Button>
  {/snippet}
</Modal>

<style>
  .form-grid {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  .static-key {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: var(--text-sm);
  }

  .static-key > span {
    color: var(--text-muted);
  }

  .static-key code {
    font-size: var(--text-xs);
    color: var(--text-secondary);
  }

  .static-key small {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  details {
    border-top: 1px solid var(--border-subtle);
    padding-top: 0.5rem;
  }

  summary {
    cursor: pointer;
    font-size: var(--text-xs);
    color: var(--text-muted);
    margin-bottom: 0.5rem;
  }

  .form-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    margin-top: 0.5rem;
  }
</style>
