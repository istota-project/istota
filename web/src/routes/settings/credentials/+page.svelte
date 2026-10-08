<script lang="ts">
  import KeepassExportCard from '$lib/components/settings/KeepassExportCard.svelte';
  import KeepassImportCard from '$lib/components/settings/KeepassImportCard.svelte';
  import CredentialActivity from '$lib/components/settings/CredentialActivity.svelte';
  import { getCurrentUser } from '$lib/userContext';
  import { SettingsLayout, VaultCard, CredentialsCard } from '$lib/components/settings';

  let refresh = $state(0);
  const identity = getCurrentUser();
</script>

<SettingsLayout description="Credentials your tasks can use, and who may use them.">
  {#key refresh}<CredentialsCard onSignedOut={identity.expireSession} />{/key}
  <KeepassImportCard onImported={() => (refresh += 1)} />
  <KeepassExportCard onExported={() => (refresh += 1)} />
  <VaultCard />
  {#key refresh}<CredentialActivity />{/key}
</SettingsLayout>
