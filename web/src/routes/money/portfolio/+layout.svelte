<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { HeaderNav } from '$lib/components/ui';

  let { children } = $props();

  function isActive(path: string): boolean {
    return page.url.pathname.startsWith(`${base}${path}`);
  }

  const navItems = $derived([
    {
      href: `${base}/money/portfolio/overview`,
      label: 'Overview',
      active: isActive('/money/portfolio/overview'),
    },
    {
      href: `${base}/money/portfolio/history`,
      label: 'History',
      active: isActive('/money/portfolio/history'),
    },
    {
      href: `${base}/money/portfolio/import`,
      label: 'Import',
      active: isActive('/money/portfolio/import'),
    },
  ]);
</script>

<div class="money-section-header">
  <!-- Field tier for the collapsed Select below 768px — same reasoning as the
       business section header. Inert on the wide layout. -->
  <div class="money-section-nav control-row">
    <HeaderNav items={navItems} ariaLabel="Portfolio section" />
  </div>
</div>

<div class="money-section-body">
  {@render children()}
</div>
