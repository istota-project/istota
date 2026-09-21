<script lang="ts">
  import { onMount } from 'svelte';
  import { getAdminBrowsers, type AdminBrowsers } from '$lib/api';
  import { Button, NoticeBanner } from '$lib/components/ui';

  let report = $state<AdminBrowsers | null>(null);
  let loading = $state(true);
  let failed = $state(false);
  let aboutCollapsed = $state(true);

  function idle(seconds: number): string {
    const total = Math.floor(seconds);
    if (total < 60) return `${total}s`;
    if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
    return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
  }

  onMount(() => {
    let closed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const next = await getAdminBrowsers();
        if (!closed) {
          report = next;
          failed = false;
        }
      } catch {
        if (!closed) {
          report = null;
          failed = true;
        }
      } finally {
        if (!closed) {
          loading = false;
          timer = setTimeout(load, 10_000);
        }
      }
    }
    void load();
    return () => {
      closed = true;
      clearTimeout(timer);
    };
  });
</script>

<div class="settings admin-page">
  {#if loading}
    <div class="center-msg">Loading browsers…</div>
  {:else if failed || report?.status === 'unavailable'}
    <div class="center-msg error" role="status">Browser service is unavailable.</div>
  {:else if report?.status === 'disabled'}
    <div class="center-msg">Browser service is disabled.</div>
  {:else if report}
    <NoticeBanner title="Browsers start on demand" bind:collapsed={aboutCollapsed}>
      <p>
        Browse with a retained session to make a browser available here. This page refreshes every
        ten seconds without starting browsers or keeping idle browsers alive.
      </p>
      <p>VNC opens in a new tab and uses the deployment’s VNC authentication and network access.</p>
    </NoticeBanner>
    {#if !report.console_configured}
      <div class="banner info">
        No external VNC URL is configured. Set a reachable viewer URL without an embedded password
        to enable Open VNC links.
      </div>
    {/if}
    {#if report.instances.length === 0}
      <section class="card"><p class="caption">No live browsers.</p></section>
    {:else}
      <section class="card">
        <div class="table-scroll">
          <table class="grid browser-grid">
            <thead><tr><th>User</th><th>Slot</th><th>Idle</th><th>Console</th></tr></thead>
            <tbody>
              {#each report.instances as instance (instance.user)}
                <tr>
                  <td class="browser-user">{instance.user}</td><td>{instance.slot}</td><td
                    >{idle(instance.idle_seconds)}</td
                  >
                  <td
                    >{#if instance.url}<Button
                        variant="secondary"
                        size="sm"
                        href={instance.url}
                        target="_blank"
                        rel="noopener noreferrer">Open VNC</Button
                      >{:else}<span class="caption">Unavailable</span>{/if}</td
                  >
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    {/if}
  {/if}
</div>

<style>
  .browser-grid {
    min-width: 32rem;
  }
  .browser-user {
    overflow-wrap: anywhere;
  }
</style>
