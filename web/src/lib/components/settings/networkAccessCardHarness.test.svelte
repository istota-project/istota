<script lang="ts">
  import { setCurrentUser } from '$lib/userContext';
  import type { NetworkSubject, User } from '$lib/api';
  import NetworkAccessCard from './NetworkAccessCard.svelte';
  let {
    subjects,
    onChanged = () => {},
    expireSession = () => {},
    showJobText = true,
    nextRunAt = null,
  }: {
    subjects: NetworkSubject[];
    onChanged?: () => void;
    expireSession?: () => void;
    showJobText?: boolean;
    nextRunAt?: string | null;
  } = $props();
  setCurrentUser({
    user: { username: 'alice', features: {} } as unknown as User,
    live: true,
    reload: async () => true,
    expireSession: () => expireSession(),
  });
</script>

{#each subjects as subject (subject.subject)}
  <NetworkAccessCard
    title={subject.label}
    {subject}
    {showJobText}
    {nextRunAt}
    externalSkills={['browse', 'devbox', 'wordpress']}
    {onChanged}
  />
{/each}
