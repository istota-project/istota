<script lang="ts" module>
  export type FilterView = 'all' | 'unread' | 'starred';
</script>

<script lang="ts">
  import { Circle, Star } from '@lucide/svelte';
  import { SidebarNav } from '@minot/ui';

  interface Props {
    /** The view on screen, or null while a single item (a room, a feed) is. */
    current: FilterView | null;
    /** The URL each view lives at, so a view opens in a new tab too. */
    href: (view: FilterView) => string;
    /** A plain click: apply the view in place rather than navigating. */
    onselect: (view: FilterView) => void;
    unread?: number;
    starred?: number;
  }

  let { current, href, onselect, unread = 0, starred = 0 }: Props = $props();

  const items = $derived([
    { view: 'all' as const, href: href('all'), label: 'All', active: current === 'all' },
    {
      view: 'unread' as const,
      href: href('unread'),
      label: 'Unread',
      icon: Circle,
      count: unread,
      countLabel: 'unread',
      active: current === 'unread',
    },
    {
      view: 'starred' as const,
      href: href('starred'),
      label: 'Starred',
      icon: Star,
      count: starred,
      countLabel: 'starred',
      countTone: 'muted' as const,
      active: current === 'starred',
    },
  ]);
</script>

<!-- The All / Unread / Starred block over chat's rooms and feeds' sources, so
     the two sidebars read the same. -->
<SidebarNav
  {items}
  label="Views"
  divider
  onnavigate={(item, event) => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)
      return;
    event.preventDefault();
    onselect(item.view);
  }}
/>
