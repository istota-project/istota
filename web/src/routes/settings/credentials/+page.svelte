<script lang="ts">
  import { getCurrentUser } from '$lib/userContext';
  import { SettingsLayout, VaultCard, CredentialGrantsCard } from '$lib/components/settings';

  const identity = getCurrentUser();
</script>

<SettingsLayout
  description="Credentials your tasks can use, where they come from, and which rooms may use them."
>
  <VaultCard />
  <CredentialGrantsCard onSignedOut={identity.expireSession} />
</SettingsLayout>
