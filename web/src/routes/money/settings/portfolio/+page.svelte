<script lang="ts">
  import { SettingsLayout } from '$lib/components/settings';
  import PortfolioAccountsCard from '$lib/components/money/PortfolioAccountsCard.svelte';
  import PortfolioClassificationsCard from '$lib/components/money/PortfolioClassificationsCard.svelte';

  // Both cards load their own data and register their own save, so this page is
  // the frame and nothing else.
</script>

<SettingsLayout
  title="Portfolio"
  description="How imported positions are grouped and classified. Both resolve at read time, so an edit here retroactively reclassifies every snapshot."
>
  <PortfolioAccountsCard />
  <PortfolioClassificationsCard />
</SettingsLayout>
