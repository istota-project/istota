<script lang="ts">
  // Per-kind config fields for a briefing source. Shared by the per-user block
  // editor and the admin shared-block editor so both render identically. Operates
  // on a plain `config` object via an `onChange(patch)` callback. File checks
  // are advisory and never write the draft.
  import { onDestroy } from 'svelte';
  import { checkBriefingPath, getBriefingPathSuggestions, type EmailAccount } from '$lib/api';
  import {
    AutocompleteInput,
    Button,
    Input,
    Select,
    type SelectOption,
    Field,
    Checkbox,
    Chip,
  } from '$lib/components/ui';

  interface Props {
    kind: string;
    config: Record<string, unknown>;
    onChange: (patch: Record<string, unknown>) => void;
    browseOptions?: SelectOption[];
    rssOptions?: SelectOption[];
    sharedBlockOptions?: SelectOption[];
    userId?: string;
    fileOptions?: string[];
    calendarOptions?: SelectOption[];
    emailAccounts?: EmailAccount[];
    ownerFixed?: boolean;
  }

  let {
    kind,
    config,
    onChange,
    browseOptions = [],
    rssOptions = [],
    sharedBlockOptions = [],
    userId = '',
    fileOptions = [],
    calendarOptions = [],
    emailAccounts = [],
    ownerFixed = false,
  }: Props = $props();

  // Until the account list loads, the bot address stands in: it is the default
  // the server reads when the config names no account.
  const accountOptions = $derived<SelectOption[]>(
    emailAccounts.length
      ? emailAccounts.map((account) => ({
          value: account.id,
          label: account.address ? `${account.label} (${account.address})` : account.label,
          disabled: !account.available,
        }))
      : [{ value: 'bot', label: 'Your bot address' }],
  );
  const selectedAccount = $derived(
    emailAccounts.find((account) => account.id === ((config.account as string) || 'bot')),
  );

  let pathSuggestions = $state<string[]>([]);
  let pathStatus = $state<'' | 'checking' | 'ok' | 'missing'>('');
  let pathResolved = $state('');
  let pathSeq = 0;
  let pathTimer: ReturnType<typeof setTimeout> | undefined;
  const fileKinds = ['todos', 'reminders', 'notes'];
  const filePlaceholder: Record<string, string> = {
    todos: 'shared/team-todo.md',
    reminders: 'istota/config/reminders.md',
    notes: 'istota/notes/agenda.md',
  };

  $effect(() => {
    const fileKind = fileKinds.includes(kind);
    const path = String(config.path ?? '').trim();
    const seq = ++pathSeq;
    clearTimeout(pathTimer);
    pathStatus = '';
    pathResolved = '';
    if (!fileKind) return;
    pathTimer = setTimeout(() => {
      getBriefingPathSuggestions(path)
        .then((response) => {
          if (seq === pathSeq) pathSuggestions = response.paths;
        })
        .catch(() => {});
      if (!path) return;
      pathStatus = 'checking';
      checkBriefingPath(path)
        .then((response) => {
          if (seq !== pathSeq) return;
          pathStatus = response.ok ? 'ok' : 'missing';
          pathResolved = response.resolved ?? '';
        })
        .catch(() => {
          if (seq === pathSeq) pathStatus = '';
        });
    }, 150);
  });
  onDestroy(() => {
    clearTimeout(pathTimer);
    pathSeq += 1;
  });

  const EMAIL_MODE_OPTIONS: SelectOption[] = [
    { value: 'shared', label: 'Shared newsletter pool' },
    { value: 'senders', label: 'Selected senders' },
  ];

  let newSender = $state('');

  function patchNumber(field: string, raw: string) {
    const t = raw.trim();
    if (t === '') {
      onChange({ [field]: undefined });
      return;
    }
    const n = Number(t);
    if (Number.isFinite(n)) onChange({ [field]: n });
  }

  const senders = $derived((config.senders as string[]) ?? []);
  function addSender() {
    const v = newSender.trim();
    if (!v) return;
    if (!senders.includes(v)) onChange({ senders: [...senders, v] });
    newSender = '';
  }
  function removeSender(v: string) {
    onChange({ senders: senders.filter((s) => s !== v) });
  }

  const rssValue = $derived.by(() => {
    const ref = config.feed_ref as { kind: string; value: number } | undefined;
    return ref ? `${ref.kind}:${ref.value}` : '';
  });
  const browseValue = $derived(config.preset ? `preset:${config.preset}` : '__custom__');
</script>

{#if kind === 'group_memory'}
  <p class="caption">This group's notebook, selected explicitly for the common edition.</p>
{:else if kind === 'group_file'}
  <Field label="Group file path" hint="One text or Markdown file relative to this group's folder."
    ><Input
      value={String(config.path ?? '')}
      oninput={(e) => onChange({ path: e.currentTarget.value })}
    /></Field
  >
{:else if kind === 'personal_file' || kind === 'personal_calendar'}
  {#if !ownerFixed}
    <Field
      label="Source owner"
      hint="A current member's user id. Only that member can select and approve their own resource."
      ><Input
        value={String(config.owner_id ?? userId)}
        oninput={(e) =>
          onChange({
            owner_id: e.currentTarget.value,
            path: undefined,
            calendar_url: undefined,
            fields: undefined,
            lookback_days: undefined,
            lookahead_days: undefined,
            include_private: undefined,
          })}
      /></Field
    >
  {/if}
  {#if config.owner_id === userId}
    {#if kind === 'personal_file'}
      <Field
        label="Personal file"
        hint="One exact text or Markdown file relative to your folder. Its full contents may be summarized or quoted."
        ><AutocompleteInput
          value={String(config.path ?? '')}
          options={fileOptions}
          onValueChange={(v) => onChange({ path: v })}
          ariaLabel="Personal file"
        /></Field
      >
    {:else}
      <Field label="Personal calendar"
        ><Select
          value={String(config.calendar_url ?? '')}
          options={calendarOptions}
          placeholder="Select your calendar"
          onValueChange={(v) =>
            onChange({
              calendar_url: v,
              fields: config.fields ?? ['time', 'title'],
              include_private: config.include_private ?? false,
              lookback_days: config.lookback_days ?? 0,
              lookahead_days: config.lookahead_days ?? 1,
            })}
          ariaLabel="Personal calendar"
          fullWidth
        /></Field
      >
      <p class="caption">Disclosed fields (time and title by default):</p>
      {#each ['time', 'title', 'description', 'attendees', 'links', 'location'] as field}
        <Field label={field} checkbox
          ><Checkbox
            checked={((config.fields as string[]) ?? ['time', 'title']).includes(field)}
            onchange={(on) => {
              const fields = (config.fields as string[]) ?? ['time', 'title'];
              onChange({
                fields: on ? [...fields, field] : fields.filter((f) => f !== field),
              });
            }}
          /></Field
        >
      {/each}
      <Field label="Days back"
        ><Select
          value={String(config.lookback_days ?? 0)}
          options={[0, 1, 7, 30].map((n) => ({ value: String(n), label: String(n) }))}
          onValueChange={(v) => onChange({ lookback_days: Number(v) })}
          ariaLabel="Days back"
        /></Field
      >
      <Field label="Days ahead"
        ><Select
          value={String(config.lookahead_days ?? 1)}
          options={[1, 7, 14, 30].map((n) => ({ value: String(n), label: String(n) }))}
          onValueChange={(v) => onChange({ lookahead_days: Number(v) })}
          ariaLabel="Days ahead"
        /></Field
      >
      <Field
        label="Include private-marked events"
        checkbox
        hint="Excluded by default. Including them requires explicit approval."
        ><Checkbox
          checked={config.include_private === true}
          onchange={(on) => onChange({ include_private: on })}
        /></Field
      >
    {/if}
  {:else}<p class="caption">
      Save the draft to request this member's contribution. Their private resources are only visible
      to them.
    </p>{/if}
{:else if fileKinds.includes(kind)}
  <Field
    label="File path"
    hint="Relative to your user folder. Use shared/… for a file shared with the bot, or istota/… for the bot's folder. Required, with no default."
  >
    <AutocompleteInput
      value={String(config.path ?? '')}
      options={pathSuggestions}
      placeholder={filePlaceholder[kind]}
      invalid={pathStatus === 'missing'}
      monospace
      ariaLabel="File path"
      filter={(options) => options}
      onValueChange={(value) => onChange({ path: value || undefined })}
    />
    {#if pathStatus === 'checking'}<p class="caption">Checking…</p>
    {:else if pathStatus === 'ok'}<p class="caption">Resolves to {pathResolved}</p>
    {:else if pathStatus === 'missing'}<p class="caption">
        No file there yet. This source is skipped until the file exists.
      </p>{/if}
  </Field>
{:else if kind === 'email'}
  <Field label="Mode">
    <Select
      value={(config.mode as string) ?? 'shared'}
      options={EMAIL_MODE_OPTIONS}
      onValueChange={(v) => onChange({ mode: v })}
      ariaLabel="Email mode"
      fullWidth
    />
  </Field>
  {#if config.mode === 'senders'}
    <Field label="Sender filters" hint="fnmatch patterns, e.g. *@semafor.com or news@axios.com">
      <div class="chip-list">
        {#each senders as s (s)}
          <Chip size="xs" removable removeLabel="Remove {s}" onremove={() => removeSender(s)}
            >{s}</Chip
          >
        {/each}
        {#if senders.length === 0}
          <span class="caption">No senders yet.</span>
        {/if}
      </div>
      <div class="hstack">
        <Input
          type="text"
          placeholder="*@example.com"
          aria-label="Add a sender pattern"
          bind:value={newSender}
          onkeydown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              addSender();
            }
          }}
        />
        <Button variant="secondary" size="sm" onclick={addSender} disabled={!newSender.trim()}
          >Add</Button
        >
      </div>
    </Field>
  {/if}
  <Field label="Lookback (hours)" hint="Leave blank to use the briefing default.">
    <Input
      type="number"
      min="1"
      value={(config.lookback_hours as number) ?? ''}
      placeholder="default"
      oninput={(e) => patchNumber('lookback_hours', (e.target as HTMLInputElement).value)}
    />
  </Field>
{:else if kind === 'my_email'}
  <Field
    label="Account"
    warning={selectedAccount && !selectedAccount.available ? selectedAccount.reason : undefined}
  >
    <Select
      value={(config.account as string) || 'bot'}
      options={accountOptions}
      onValueChange={(v) => onChange({ account: v })}
      ariaLabel="Email account"
      fullWidth
    />
    {#each emailAccounts.filter((account) => !account.available && account !== selectedAccount) as account (account.id)}
      <p class="caption">{account.label}: {account.reason}</p>
    {/each}
  </Field>
  <Field label="Lookback (hours)" hint="Leave blank to read the past 24 hours.">
    <Input
      type="number"
      min="1"
      value={(config.lookback_hours as number) ?? ''}
      placeholder="24"
      oninput={(e) => patchNumber('lookback_hours', (e.target as HTMLInputElement).value)}
    />
  </Field>
{:else if kind === 'rss'}
  <Field label="Feed or category">
    <Select
      value={rssValue}
      options={rssOptions}
      placeholder="Pick a feed / category…"
      onValueChange={(v) => {
        const [k, value] = v.split(':');
        onChange({ feed_ref: { kind: k, value: Number(value) } });
      }}
      ariaLabel="Feed or category"
      fullWidth
    />
  </Field>
  {#if rssOptions.length === 0}
    <p class="caption">No feeds available — subscribe in the Feeds module first.</p>
  {/if}
  <div class="inline-fields">
    <Field label="Max entries">
      <Input
        type="number"
        min="1"
        value={(config.limit as number) ?? ''}
        placeholder="10"
        oninput={(e) => patchNumber('limit', (e.target as HTMLInputElement).value)}
      />
    </Field>
    <Field label="Unread only" checkbox>
      <Checkbox checked={!!config.unread_only} onchange={(on) => onChange({ unread_only: on })} />
    </Field>
  </div>
{:else if kind === 'browse'}
  <Field label="Source">
    <Select
      value={browseValue}
      options={browseOptions}
      onValueChange={(v) => {
        if (v === '__custom__') {
          onChange({ preset: undefined, url: (config.url as string) ?? '' });
        } else {
          onChange({ preset: v.split(':')[1], url: undefined });
        }
      }}
      ariaLabel="Browse source"
      fullWidth
    />
  </Field>
  {#if !config.preset}
    <Field label="URL">
      <Input
        type="text"
        placeholder="https://…"
        value={(config.url as string) ?? ''}
        oninput={(e) => onChange({ url: (e.target as HTMLInputElement).value })}
      />
    </Field>
  {/if}
{:else if kind === 'markets'}
  <p class="caption">Leave blank for the default index &amp; futures set.</p>
  <Field label="Indices" hint="Comma-separated symbols, e.g. ^GSPC, ^IXIC">
    <Input
      type="text"
      value={((config.indices as string[]) ?? []).join(', ')}
      placeholder="default"
      oninput={(e) =>
        onChange({
          indices: (e.target as HTMLInputElement).value
            .split(',')
            .map((x) => x.trim())
            .filter(Boolean),
        })}
    />
  </Field>
  <Field label="Futures" hint="Comma-separated symbols.">
    <Input
      type="text"
      value={((config.futures as string[]) ?? []).join(', ')}
      placeholder="default"
      oninput={(e) =>
        onChange({
          futures: (e.target as HTMLInputElement).value
            .split(',')
            .map((x) => x.trim())
            .filter(Boolean),
        })}
    />
  </Field>
{:else if kind === 'calendar'}
  <p class="caption">Pulls from your connected calendars. No configuration needed.</p>
{:else if kind === 'shared_block'}
  <Field
    label="Shared block"
    hint="Pre-made content generated once for everyone (world headlines, markets, curated digests). Spliced in verbatim."
  >
    <Select
      value={(config.name as string) ?? ''}
      options={sharedBlockOptions}
      placeholder="Pick a shared block…"
      onValueChange={(v) => onChange({ name: v })}
      ariaLabel="Shared block"
      fullWidth
    />
  </Field>
  {#if sharedBlockOptions.length === 0}
    <p class="caption">No shared blocks available yet.</p>
  {/if}
  <Field label="Max age (hours)" hint="Omit stale content older than this. Blank = 24h.">
    <Input
      type="number"
      min="1"
      value={(config.max_age_hours as number) ?? ''}
      placeholder="24"
      oninput={(e) => patchNumber('max_age_hours', (e.target as HTMLInputElement).value)}
    />
  </Field>
{/if}

<style>
  .inline-fields {
    display: flex;
    gap: 1.25rem;
    align-items: flex-end;
    flex-wrap: wrap;
  }
  /* Typography is the global .caption; only the <p> reset stays here. */
  p.caption {
    margin: 0.1rem 0;
  }
  .chip-list {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    margin-bottom: var(--space-2);
  }
</style>
