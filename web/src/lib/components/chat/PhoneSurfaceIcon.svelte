<script lang="ts">
  /**
   * The one glyph for a phone surface, shared by the room list, the chat
   * header and a texted turn's provenance line (ISSUE-584). WhatsApp is green:
   * one bubble for the private 1:1 chat, two for a group, both outlined like
   * every other glyph in the app.
   */
  import { Smartphone, MessageCircle, MessagesCircle } from '@lucide/svelte';

  let {
    surface,
    group = false,
    size = 13,
  }: { surface: 'sms' | 'whatsapp'; group?: boolean; size?: number } = $props();
</script>

{#if surface === 'sms'}
  <Smartphone {size} />
{:else}
  <span class="whatsapp" data-whatsapp={group ? 'group' : 'private'}>
    {#if group}<MessagesCircle {size} />{:else}<MessageCircle {size} />{/if}
  </span>
{/if}

<style>
  .whatsapp {
    display: inline-flex;
    color: var(--accent-whatsapp);
  }
</style>
