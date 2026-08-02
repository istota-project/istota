<script lang="ts">
  import { onMount } from 'svelte';
  import { getAdminConfig, type AdminConfigField, type AdminConfigView } from '$lib/api';
  import { Badge, Input, NoticeBanner } from '$lib/components/ui';

  let view = $state<AdminConfigView | null>(null);
  let loading = $state(true);
  let error = $state('');
  let filter = $state('');
  let readOnlyCollapsed = $state(true);

  let sections = $derived.by(() => {
    if (!view) return [];
    const needle = filter.trim().toLowerCase();
    if (!needle) return view.sections;
    return view.sections
      .map((s) => ({
        ...s,
        fields: s.fields.filter(
          (f) =>
            f.key.toLowerCase().includes(needle) ||
            String(f.value ?? '')
              .toLowerCase()
              .includes(needle),
        ),
      }))
      .filter((s) => s.fields.length > 0);
  });

  let matchCount = $derived(sections.reduce((n, s) => n + s.fields.length, 0));

  function renderValue(field: AdminConfigField): string {
    if (field.secret) return field.set ? '•••••••• (set)' : 'not set';
    const v = field.value;
    if (v === null || v === undefined) return '—';
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    if (Array.isArray(v)) return v.length ? v.map((x) => String(x)).join(', ') : '—';
    if (typeof v === 'object') {
      const entries = Object.entries(v as Record<string, unknown>);
      if (!entries.length) return '—';
      return entries.map(([k, val]) => `${k} = ${JSON.stringify(val)}`).join('\n');
    }
    if (v === '') return '—';
    return String(v);
  }

  function isEmpty(field: AdminConfigField): boolean {
    return !field.secret && renderValue(field) === '—';
  }

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
  }

  onMount(load);
</script>

<div class="settings config-page">
  {#if loading && !view}
    <div class="center-msg">Loading…</div>
  {:else if error}
    <div class="banner error">{error}</div>
  {:else if view}
    <NoticeBanner
      title="Read-only view of the running configuration"
      bind:collapsed={readOnlyCollapsed}
    >
      <p>
        These are the values the running processes loaded, not the file on disk — a change needs an
        edit to
        {#if view.config_path}<code>{view.config_path}</code>{:else}the config file{/if}
        followed by a reload. Editing from here is not wired up yet.
      </p>
      <p>Credentials are never sent to the browser. A secret shows only whether it is set.</p>
    </NoticeBanner>

    <!-- One input and no sibling control, so nothing here needs levelling —
         the scope is for the field appearance, which has to match the logs
         toolbar two clicks away rather than sit a size below it. -->
    <div class="config-toolbar control-row">
      <Input bind:value={filter} placeholder="Filter settings…" aria-label="Filter settings" />
      {#if filter.trim()}
        <span class="match-count">{matchCount} matching</span>
      {/if}
    </div>

    {#if sections.length === 0}
      <div class="center-msg">No settings match “{filter}”.</div>
    {/if}

    {#each sections as section (section.key)}
      <section class="card">
        <h2 class="section-title">{section.label}</h2>
        <dl class="config-grid">
          {#each section.fields as field (field.key)}
            <dt class:dim={isEmpty(field)}>{field.name}</dt>
            <dd class:dim={isEmpty(field)}>
              {#if field.secret}
                <Badge variant={field.set ? 'success' : 'partial'}>
                  {field.set ? 'set' : 'not set'}
                </Badge>
              {:else}
                <span class="config-value">{renderValue(field)}</span>
              {/if}
            </dd>
          {/each}
        </dl>
      </section>
    {/each}
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

  .section-title {
    margin: 0 0 var(--space-3);
    font-family: var(--font-mono);
    font-size: var(--text-sm);
    font-weight: 600;
    color: var(--text-secondary);
  }

  .config-grid {
    display: grid;
    grid-template-columns: minmax(0, 18rem) minmax(0, 1fr);
    gap: var(--space-1) var(--space-4);
    margin: 0;
  }

  .config-grid dt {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  .config-grid dd {
    margin: 0;
    font-size: var(--text-xs);
    color: var(--text-primary);
  }

  /* An unset value is the common case in a config with many defaults; dimming
     it lets the eye find what is actually configured. */
  .config-grid dt.dim,
  .config-grid dd.dim {
    color: var(--text-dim);
  }

  .config-value {
    font-family: var(--font-mono);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  @media (max-width: 640px) {
    .config-grid {
      grid-template-columns: minmax(0, 1fr);
      gap: 0;
    }

    .config-grid dd {
      margin-bottom: var(--space-2);
    }
  }
</style>
