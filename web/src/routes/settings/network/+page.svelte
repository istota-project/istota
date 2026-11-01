<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { AuthError, getNetworkAccess, type NetworkAccess, type NetworkSubject } from '$lib/api';
  import { NetworkSubjectCard, SettingsLayout } from '$lib/components/settings';
  import { getCurrentUser } from '$lib/userContext';
  import { Badge, Disclosure, EmptyState } from '@minot/ui';
  import { latest } from '@minot/ui/ui/latest';

  const identity = getCurrentUser();

  let data: NetworkAccess | null = $state(null);
  let error = $state('');
  let loading = $state(true);
  let disabledOpen = $state(false);
  const subjects = $derived.by(() => data?.subjects ?? []);
  const active = $derived(subjects.filter((item) => !isDisabled(item)));
  const disabled = $derived(subjects.filter(isDisabled));
  const loads = latest();
  onDestroy(() => loads.dispose());

  function isDisabled(item: NetworkSubject): boolean {
    return item.job?.run_state === 'disabled';
  }

  async function refresh() {
    const current = loads.next();
    try {
      const result = await getNetworkAccess();
      if (!current()) return;
      data = result;
      error = '';
      // The bell opens this page, so a request it raised must not sit behind a fold.
      if (result.subjects.some((item) => isDisabled(item) && item.refused.length > 0)) {
        disabledOpen = true;
      }
    } catch (e) {
      if (!current()) return;
      report(e);
    } finally {
      if (current()) loading = false;
    }
  }

  function report(e: unknown) {
    if (e instanceof AuthError) identity.expireSession();
    else error = (e as Error).message || 'Network access could not be loaded.';
  }

  function retry() {
    loading = true;
    error = '';
    void refresh();
  }

  onMount(refresh);
</script>

<SettingsLayout
  description="Where your scheduled jobs, briefings and other unattended work may send data. Work you start yourself is not limited here."
  {loading}
  error={data ? error : ''}
  loadError={data ? '' : error}
  onRetry={retry}
>
  {#if data}
    {#if data.subjects.length === 0}
      <EmptyState>Nothing unattended to decide about yet.</EmptyState>
    {/if}
    {#each active as subject (subject.subject)}
      <NetworkSubjectCard {subject} externalSkills={data.external_skills} onChanged={refresh} />
    {/each}
    {#if disabled.length > 0}
      <Disclosure variant="section" summary="Disabled jobs" bind:open={disabledOpen}>
        {#snippet trailing()}<Badge size="sm" variant="neutral">{disabled.length}</Badge>{/snippet}
        <div class="vstack">
          {#each disabled as subject (subject.subject)}
            <NetworkSubjectCard
              {subject}
              externalSkills={data.external_skills}
              onChanged={refresh}
            />
          {/each}
        </div>
      </Disclosure>
    {/if}
  {/if}
</SettingsLayout>
