<script lang="ts">
  import { base } from '$app/paths';
  import type { AdminGoogleWorkspaceConnection } from '$lib/api';
  import { Badge, Button } from '$lib/components/ui';

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

<section class="card" data-testid="google-workspace-card">
  <div class="card-head">
    <h2>{connection.label}</h2>
    <Badge variant={badge.variant}>{badge.label}</Badge>
  </div>

  <dl class="kv">
    <dt>Client ID</dt>
    <dd>{connection.client_id.trim() ? connection.client_id : 'Not set'}</dd>
    <dt>Client secret</dt>
    <dd>{connection.client_secret_set ? 'Set' : 'Not set'}</dd>
    <dt>Callback URL</dt>
    <dd>{connection.redirect_uri ?? 'Not set'}</dd>
  </dl>

  <h3 class="micro-label">Maximum service access</h3>
  <dl class="kv">
    {#each connection.services as service (service.service)}
      <dt>{service.label}</dt>
      <dd>{LEVEL_LABELS[service.max_level]}</dd>
    {/each}
  </dl>
  {#if noAccess}
    <p class="summary">No services are allowed</p>
  {/if}
  {#if connection.additional_scopes.length > 0}
    <h3 class="micro-label">Additional scopes requested on connection</h3>
    <ul>
      {#each connection.additional_scopes as scope (scope)}
        <li><code>{scope}</code></li>
      {/each}
    </ul>
    <p class="summary">These scopes have no per-user service picker control.</p>
  {/if}

  {#if connection.missing.length > 0}
    <ul>
      {#each connection.missing as field (field)}
        <li>{MISSING[field]}</li>
      {/each}
    </ul>
  {/if}

  <p class="summary">Each user connects their own Google account in Settings → Connections.</p>
  <p class="summary">
    Configuration presence does not verify credentials, enabled APIs or Google consent settings.
    This card reflects the loaded configuration after the normal configuration restart.
  </p>
  <div class="form-actions">
    <Button variant="secondary" href={`${base}/admin/config?section=google_workspace`}
      >Configure</Button
    >
    <Button variant="secondary" href="https://istota.cynium.com/docs/features/google-workspace/"
      >Setup guide</Button
    >
  </div>
</section>

<style>
  .summary {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }

  dd,
  code {
    overflow-wrap: anywhere;
    user-select: text;
  }
</style>
