<script lang="ts">
  // Per-kind config fields for a briefing source. Shared by the per-user block
  // editor and the admin shared-block editor so both render identically. Operates
  // on a plain `config` object via an `onChange(patch)` callback — the caller owns
  // where the config lives. Path kinds (todos/reminders/notes) keep their
  // debounced file-picker in the page and are not handled here.
  import { Button, Select, type SelectOption } from '$lib/components/ui';
  import { SettingsField } from '$lib/components/settings';

  interface Props {
    kind: string;
    config: Record<string, unknown>;
    onChange: (patch: Record<string, unknown>) => void;
    browseOptions?: SelectOption[];
    rssOptions?: SelectOption[];
    sharedBlockOptions?: SelectOption[];
  }

  let {
    kind,
    config,
    onChange,
    browseOptions = [],
    rssOptions = [],
    sharedBlockOptions = [],
  }: Props = $props();

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

{#if kind === 'email'}
  <SettingsField label="Mode">
    <Select
      value={(config.mode as string) ?? 'shared'}
      options={EMAIL_MODE_OPTIONS}
      onValueChange={(v) => onChange({ mode: v })}
      ariaLabel="Email mode"
      fullWidth
    />
  </SettingsField>
  {#if config.mode === 'senders'}
    <SettingsField
      label="Sender filters"
      hint="fnmatch patterns, e.g. *@semafor.com or news@axios.com"
    >
      <div class="chip-list">
        {#each senders as s (s)}
          <span class="chip">
            {s}
            <button type="button" title="Remove" onclick={() => removeSender(s)}>×</button>
          </span>
        {/each}
        {#if senders.length === 0}
          <span class="muted small">No senders yet.</span>
        {/if}
      </div>
      <div class="chip-add">
        <input
          type="text"
          placeholder="*@example.com"
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
    </SettingsField>
  {/if}
  <SettingsField label="Lookback (hours)" hint="Leave blank to use the briefing default.">
    <input
      type="number"
      min="1"
      value={(config.lookback_hours as number) ?? ''}
      placeholder="default"
      oninput={(e) => patchNumber('lookback_hours', (e.target as HTMLInputElement).value)}
    />
  </SettingsField>
{:else if kind === 'rss'}
  <SettingsField label="Feed or category">
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
  </SettingsField>
  {#if rssOptions.length === 0}
    <p class="muted small">No feeds available — subscribe in the Feeds module first.</p>
  {/if}
  <div class="inline-fields">
    <SettingsField label="Max entries">
      <input
        type="number"
        min="1"
        value={(config.limit as number) ?? ''}
        placeholder="10"
        oninput={(e) => patchNumber('limit', (e.target as HTMLInputElement).value)}
      />
    </SettingsField>
    <SettingsField label="Unread only" checkbox>
      <input
        type="checkbox"
        checked={!!config.unread_only}
        onchange={(e) => onChange({ unread_only: (e.target as HTMLInputElement).checked })}
      />
    </SettingsField>
  </div>
{:else if kind === 'browse'}
  <SettingsField label="Source">
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
  </SettingsField>
  {#if !config.preset}
    <SettingsField label="URL">
      <input
        type="text"
        placeholder="https://…"
        value={(config.url as string) ?? ''}
        oninput={(e) => onChange({ url: (e.target as HTMLInputElement).value })}
      />
    </SettingsField>
  {/if}
{:else if kind === 'markets'}
  <p class="muted small">Leave blank for the default index &amp; futures set.</p>
  <SettingsField label="Indices" hint="Comma-separated symbols, e.g. ^GSPC, ^IXIC">
    <input
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
  </SettingsField>
  <SettingsField label="Futures" hint="Comma-separated symbols.">
    <input
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
  </SettingsField>
{:else if kind === 'calendar'}
  <p class="muted small">Pulls from your connected calendars. No configuration needed.</p>
{:else if kind === 'shared_block'}
  <SettingsField
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
  </SettingsField>
  {#if sharedBlockOptions.length === 0}
    <p class="muted small">No shared blocks available yet.</p>
  {/if}
  <SettingsField label="Max age (hours)" hint="Omit stale content older than this. Blank = 24h.">
    <input
      type="number"
      min="1"
      value={(config.max_age_hours as number) ?? ''}
      placeholder="24"
      oninput={(e) => patchNumber('max_age_hours', (e.target as HTMLInputElement).value)}
    />
  </SettingsField>
{/if}

<style>
  .inline-fields {
    display: flex;
    gap: 1.25rem;
    align-items: flex-end;
    flex-wrap: wrap;
  }
  .muted {
    color: var(--text-muted);
  }
  .small {
    font-size: var(--text-xs);
  }
  p.muted.small {
    margin: 0.1rem 0;
  }
  .chip-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    margin-bottom: 0.4rem;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: var(--text-xs);
    padding: 0.1rem 0.2rem 0.1rem 0.5rem;
    border-radius: var(--radius-pill);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    color: var(--text-secondary);
  }
  .chip button {
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-muted);
    font-size: 1rem;
    line-height: 1;
    padding: 0;
  }
  .chip-add {
    display: flex;
    gap: 0.4rem;
    align-items: center;
  }
  .chip-add input {
    max-width: 16rem;
  }
</style>
