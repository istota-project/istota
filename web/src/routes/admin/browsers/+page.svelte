<script lang="ts">
  import { base } from '$app/paths';
  import { getAdminBrowsers, type AdminBrowsers } from '$lib/api';
  import BrowserConsole from '$lib/admin/browsers/BrowserConsole.svelte';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import { Button, Card, Disclosure, LoadState, Timestamp } from '@minot/ui';

  let report = $state<AdminBrowsers | null>(null);
  let loading = $state(true);
  let failed = $state(false);
  let consoleUser = $state<string | null>(null);

  const listHref = `${base}/admin/browsers`;

  const selection = createUrlSelection<{ user: string | null }>({
    key: 'adminBrowser',
    params: ['user'],
    encode: ({ user }): Record<string, string> => (user ? { user } : {}),
    decode: (params) => ({ user: params.user || null }),
    read: () => ({ user: consoleUser }),
    apply: ({ user }) => {
      consoleUser = user;
    },
  });
  selection.start();
  // A deep link opens the console whatever the list says: the relay, not the
  // daemon's record, decides whether the browser is running.
  consoleUser = selection.current()?.user ?? null;

  function idle(seconds: number): string {
    const total = Math.floor(seconds);
    if (total < 60) return `${total}s`;
    if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
    return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
  }

  let listing = $derived(consoleUser === null);

  // The list polls only while it is on screen: an open console stops it, and
  // going back starts it again with a fresh read.
  $effect(() => {
    if (!listing) return;
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

{#if consoleUser !== null}
  {#key consoleUser}
    <BrowserConsole
      user={consoleUser}
      backHref={listHref}
      onExit={() => selection.push({ user: null })}
    />
  {/key}
{:else}
  <div class="settings admin-page card-pane">
    <LoadState
      {loading}
      error={failed || report?.status === 'unavailable' ? 'Browser service is unavailable.' : null}
      loadingLabel="Loading browsers…"
    >
      {#if report?.status === 'disabled'}
        <div class="center-msg">Browser service is disabled.</div>
      {:else if report}
        <Disclosure variant="card" summary="Browsers start on demand">
          <p>
            Browse with a retained session to make a browser available here. The scheduler reads the
            browser pool every 15 seconds and this page refreshes every ten, without starting
            browsers or keeping idle browsers alive.
          </p>
          <p>
            View opens the user's browser in this page. Each console asks you to confirm with your
            password or a mailed code first. Working in the console does not keep a browser alive:
            without browse activity it still closes when its idle time runs out.
          </p>
        </Disclosure>
        {#if report.instances.length === 0}
          <Card><p class="caption">No live browsers.</p></Card>
        {:else}
          <Card>
            <div class="table-scroll">
              <table class="table table--fixed browser-grid">
                <thead><tr><th>User</th><th>Slot</th><th>Idle</th><th>Console</th></tr></thead>
                <tbody>
                  {#each report.instances as instance (instance.user)}
                    <tr>
                      <td class="browser-user">{instance.user}</td><td>{instance.slot}</td><td
                        >{idle(instance.idle_seconds)}</td
                      >
                      <td
                        ><Button
                          variant="secondary"
                          size="sm"
                          ariaLabel={`View ${instance.user}'s browser`}
                          onclick={() => selection.push({ user: instance.user })}>View</Button
                        ></td
                      >
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </Card>
        {/if}
        {#if report.as_of}
          <p class="caption">
            As of <Timestamp
              value={report.as_of}
              format="time"
              options={{ hour: 'numeric', minute: '2-digit', second: '2-digit' }}
            />
          </p>
        {/if}
      {/if}
    </LoadState>
  </div>
{/if}

<style>
  .browser-grid {
    min-width: 32rem;
  }
  .browser-user {
    overflow-wrap: anywhere;
  }
</style>
