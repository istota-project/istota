<script lang="ts">
  /**
   * The one glyph for a phone surface, shared by the room list, the chat
   * header and a texted turn's provenance line (ISSUE-584). WhatsApp is green;
   * the private 1:1 chat gets a filled bubble and a group the outline, so the
   * two can be told apart from the icon alone.
   */
  import { Smartphone, MessageCircle } from '@lucide/svelte';

  let {
    surface,
    group = false,
    size = 13,
  }: { surface: 'sms' | 'whatsapp'; group?: boolean; size?: number } = $props();
</script>

{#if surface === 'sms'}
  <Smartphone {size} />
{:else}
  <span class="whatsapp" class:group data-whatsapp={group ? 'group' : 'private'}>
    <MessageCircle {size} fill={group ? 'none' : 'currentColor'} />
  </span>
{/if}

<style>
  .whatsapp {
    display: inline-flex;
    color: var(--accent-whatsapp);
  }
</style>
