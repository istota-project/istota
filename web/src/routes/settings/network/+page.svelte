<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { AuthError, getNetworkAccess, type NetworkAccess } from '$lib/api';
  import { NetworkSubjectCard, SettingsLayout } from '$lib/components/settings';
  import { getCurrentUser } from '$lib/userContext';
  import { EmptyState } from '@minot/ui';
  import { latest } from '@minot/ui/ui/latest';

  const identity = getCurrentUser();

  let data: NetworkAccess | null = $state(null);
  let error = $state('');
  let loading = $state(true);
  const loads = latest();
  onDestroy(() => loads.dispose());

  async function refresh() {
    const current = loads.next();
    try {
      const result = await getNetworkAccess();
      if (!current()) return;
      data = result;
      error = '';
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
    {#each data.subjects as subject (subject.subject)}
      <NetworkSubjectCard {subject} externalSkills={data.external_skills} onChanged={refresh} />
    {/each}
  {/if}
</SettingsLayout>
