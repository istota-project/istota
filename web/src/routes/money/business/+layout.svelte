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
  <!-- Field tier at both widths: the inline links on the wide layout and the
       Select they collapse into below 768px. A body-header nav is then the
       same height as the filters in a sibling section, so the bar does not
       change height as you move between tabs of the same module — this section
       has no filter at all, and used to sit shorter than the rest. -->
  <div class="money-section-nav control-row">
    <HeaderNav items={navItems} ariaLabel="Business section" />
  </div>
</div>

<div class="money-section-body">
  {@render children()}
</div>
