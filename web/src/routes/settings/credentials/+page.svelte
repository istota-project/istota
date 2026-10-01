<script lang="ts">
  import { getCurrentUser } from '$lib/userContext';
  import { SettingsLayout, VaultCard, CredentialsCard } from '$lib/components/settings';

  const identity = getCurrentUser();
</script>

<SettingsLayout description="Credentials your tasks can use, and who may use them.">
  <CredentialsCard onSignedOut={identity.expireSession} />
  <VaultCard />
</SettingsLayout>
