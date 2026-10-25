<script lang="ts">
  /* Activity: what one app asked the bridge for, and what its current
     version reported going wrong. Calls cover every version, newest first,
     at most 200; a call's arguments are never stored, so the page says what
     was asked for and how it was answered, never which keys or files. An
     error report is the app's own text and is rendered as text. */
  import { onDestroy, untrack } from 'svelte';
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { latest } from '@minot/ui/ui/latest';
  import {
    Badge,
    Card,
    Disclosure,
    EmptyState,
    LoadState,
    PageHeader,
    Timestamp,
    type BadgeVariant,
  } from '@minot/ui';
  import { AuthError } from '$lib/api';
  import { AppsApiError, appPath, getActivity, type AppActivity } from '$lib/apps/api';
  import { getAppsList } from '$lib/apps/context';
  import { getCurrentUser } from '$lib/userContext';

  const apps = getAppsList();
  const identity = getCurrentUser();

  const appId = $derived(page.params.id ?? '');
  const name = $derived(apps.list?.apps.find((a) => a.app_id === appId)?.name ?? appId);

  let data = $state<AppActivity | null>(null);
  let loading = $state(true);
  let error = $state('');

  const loads = latest();
  onDestroy(() => loads.dispose());

  async function load(id: string) {
    const current = loads.next();
    if (!data) loading = true;
    try {
      const next = await getActivity(id);
      if (!current()) return;
      data = next;
      error = '';
    } catch (e) {
      if (!current()) return;
      if (e instanceof AuthError) {
        identity.expireSession();
        return;
      }
      data = null;
      error =
        e instanceof AppsApiError && e.status === 404
          ? 'This app is not installed.'
          : e instanceof Error && e.message
            ? e.message
            : 'Could not load the activity.';
    } finally {
      if (current()) loading = false;
    }
  }

  $effect(() => {
    const id = appId;
    untrack(() => {
      data = null;
      void load(id);
    });
  });

  $effect(() => {
    apps.setHeader({ title: name });
  });
  onDestroy(() => apps.setHeader(null));

  const STATUS: Record<string, [string, BadgeVariant]> = {
    ok: ['Answered', 'success'],
    not_granted: ['Not granted', 'warn'],
    bad_args: ['Bad arguments', 'warn'],
    quota: ['Over a limit', 'warn'],
    rate_limited: ['Too many calls', 'warn'],
    stale_version: ['Old version', 'neutral'],
    not_found: ['Not found', 'neutral'],
    internal: ['Failed', 'danger'],
  };

  function status(code: string): [string, BadgeVariant] {
    return STATUS[code] ?? [code, 'neutral'];
  }

  const KINDS: Record<string, string> = {
    error: 'Error',
    rejection: 'Unhandled rejection',
    navigated: 'Navigated away',
    shell: 'Shell error',
  };
</script>

<div class="activity content-frame vstack">
  <PageHeader
    title="Activity"
    back={{ href: `${base}${appPath(appId)}`, label: `Back to ${name}` }}
    align="start"
  />
  <LoadState {loading} {error} onRetry={() => void load(appId)}>
    {#if data}
      <Card
        title="Calls"
        level={2}
        description="Newest first, from every version, at most 200. What a call read is not recorded."
      >
        {#if data.calls.length}
          <div class="table-scroll table--cards">
            <table class="table" aria-label="Calls">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Capability</th>
                  <th>Operation</th>
                  <th class="num">Version</th>
                  <th>Answer</th>
                </tr>
              </thead>
              <tbody>
                {#each data.calls as call (call.id)}
                  {@const [label, variant] = status(call.status)}
                  <tr class:is-warn={variant === 'warn'} class:is-danger={variant === 'danger'}>
                    <td data-label="Time"><Timestamp value={call.at} format="datetime" /></td>
                    <td class="cell-primary"><code class="code-chip">{call.capability}</code></td>
                    <td data-label="Operation">{call.op}</td>
                    <td class="num" data-label="Version">{call.version}</td>
                    <td data-label="Answer">
                      <Badge size="sm" {variant} uppercase={false}>{label}</Badge>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {:else}
          <EmptyState size="sm">This app has made no calls.</EmptyState>
        {/if}
      </Card>

      <Card title="Errors in version {data.current_version}" level={2}>
        {#if data.errors.length}
          <ul class="list-reset divided-list">
            {#each data.errors as report (report.id)}
              <li class="vstack" style:--vstack-gap="var(--space-2)">
                <div class="hstack wrap">
                  <Badge size="sm" variant="danger" uppercase={false}>
                    {KINDS[report.kind] ?? report.kind}
                  </Badge>
                  <Timestamp class="caption" value={report.at} format="datetime" />
                </div>
                <p class="message">{report.message}</p>
                {#if report.stack}
                  <Disclosure summary="Stack">
                    <pre class="stack">{report.stack}</pre>
                  </Disclosure>
                {/if}
              </li>
            {/each}
          </ul>
        {:else}
          <EmptyState size="sm">Version {data.current_version} has reported no errors.</EmptyState>
        {/if}
      </Card>
    {/if}
  </LoadState>
</div>

<style>
  .activity {
    padding: var(--space-4);
  }

  .message {
    overflow-wrap: anywhere;
  }

  .stack {
    margin: 0;
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    white-space: pre;
    overflow-x: auto;
  }
</style>
