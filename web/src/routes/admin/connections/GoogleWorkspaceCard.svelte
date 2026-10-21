<script lang="ts">
  import { base } from '$app/paths';
  import type { AdminGoogleWorkspaceConnection } from '$lib/api';
  import { Alert, Button } from '@minot/ui';
  import { KeyRound } from '@lucide/svelte';
  import ConnectionCard from './ConnectionCard.svelte';

  let { connection }: { connection: AdminGoogleWorkspaceConnection } = $props();

  const BADGE = {
    disabled: { variant: 'neutral', label: 'Disabled' },
    incomplete: { variant: 'warn', label: 'Setup incomplete' },
    configured: { variant: 'success', label: 'Configured' },
  } as const;
  const LEVEL_LABELS = {
    off: 'No access',
    readonly: 'Read-only',
    full: 'Read and write',
  } as const;
  const MISSING = {
    client_id: 'Set the OAuth client ID in Configuration.',
    client_secret: 'Set the OAuth client secret in Configuration.',
    site_hostname:
      'Set the site hostname through the deployment setup procedure. It is read-only in Configuration; see the Setup guide.',
    scopes: 'Allow at least one Google scope in Configuration.',
  } as const;

  let badge = $derived(BADGE[connection.status]);
  let noAccess = $derived(
    connection.services.every((service) => service.max_level === 'off') &&
      connection.additional_scopes.length === 0,
  );
</script>

<ConnectionCard
  testid="google-workspace-card"
  title={connection.label}
  subtitle="The OAuth app behind each user's Google connection"
  icon={KeyRound}
  {badge}
>
  {#if connection.missing.length > 0}
    <Alert appearance="inline" variant="warn" class="wide">
      <ul class="missing">
        {#each connection.missing as field (field)}
          <li>{MISSING[field]}</li>
        {/each}
      </ul>
    </Alert>
  {/if}

  <div class="panel">
    <h3 class="micro-label">OAuth app</h3>
    <dl class="kv">
      <dt>Client ID</dt>
      <dd class:unset={!connection.client_id.trim()}>
        {connection.client_id.trim() ? connection.client_id : 'Not set'}
      </dd>
      <dt>Client secret</dt>
      <dd class:unset={!connection.client_secret_set}>
        {connection.client_secret_set ? 'Set' : 'Not set'}
      </dd>
      <dt>Callback URL</dt>
      <dd class:unset={!connection.redirect_uri}>{connection.redirect_uri ?? 'Not set'}</dd>
    </dl>
  </div>

  <div class="panel">
    <h3 class="micro-label">Maximum service access</h3>
    <dl class="kv">
      {#each connection.services as service (service.service)}
        <dt>{service.label}</dt>
        <dd class:unset={service.max_level === 'off'}>{LEVEL_LABELS[service.max_level]}</dd>
      {/each}
    </dl>
    {#if noAccess}
      <p class="summary">No services are allowed</p>
    {/if}
  </div>

  {#if connection.additional_scopes.length > 0}
    <div class="panel">
      <h3 class="micro-label">Additional scopes</h3>
      <ul class="scopes">
        {#each connection.additional_scopes as scope (scope)}
          <li><code>{scope}</code></li>
        {/each}
      </ul>
      <p class="caption">
        Requested on every connection. These scopes have no per-user service picker control.
      </p>
    </div>
  {/if}

  {#snippet meta()}
    <p class="caption">
      Each user connects their own Google account in Settings → Connections. Configuration presence
      does not verify credentials, enabled APIs or Google consent settings. This card reflects the
      loaded configuration after the normal configuration restart.
    </p>
  {/snippet}

  {#snippet actions()}
    <Button
      variant="secondary"
      href="https://istota.cynium.com/docs/features/google-workspace/"
      target="_blank"
      rel="noopener noreferrer">Setup guide</Button
    >
    <Button variant="secondary" href={`${base}/admin/config?section=google_workspace`}
      >Configure</Button
    >
  {/snippet}
</ConnectionCard>

<style>
  .summary {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }

  .unset {
    color: var(--text-muted);
  }

  .missing {
    margin: 0;
    padding-left: var(--space-6);
  }

  .scopes {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  code {
    overflow-wrap: anywhere;
    user-select: text;
  }
</style>
