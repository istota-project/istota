<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { HeaderNav } from '$lib/components/ui';

  let { children } = $props();

  function isActive(path: string): boolean {
    return page.url.pathname.startsWith(`${base}${path}`);
  }

  // Work is upstream of invoices in the actual workflow, so it leads.
  const navItems = $derived([
    {
      href: `${base}/money/business/work`,
      label: 'Work',
      active: isActive('/money/business/work'),
    },
    {
      href: `${base}/money/business/invoices`,
      label: 'Invoices',
      active: isActive('/money/business/invoices'),
    },
    {
      href: `${base}/money/business/clients`,
      label: 'Clients',
      active: isActive('/money/business/clients'),
    },
  ]);
</script>

<div class="money-section-header">
  <!-- Field tier for the collapsed Select below 768px, so a body-header nav is
       the same size and shape in every money section whether or not it has a
       filter beside it. Inert on the wide layout — `NavLink` reads neither
       token, so the inline links keep their chip shape. -->
  <div class="money-section-nav control-row">
    <HeaderNav items={navItems} ariaLabel="Business section" />
  </div>
</div>

<div class="money-section-body">
  {@render children()}
</div>
