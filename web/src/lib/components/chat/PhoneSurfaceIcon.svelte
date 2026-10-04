<script lang="ts">
  /**
   * The one glyph for a phone surface, shared by the room list, the chat
   * header and a texted turn's provenance line (ISSUE-584). WhatsApp is green:
   * one bubble for the private 1:1 chat, two for a group, both outlined like
   * every other glyph in the app. The user's private email room takes an
   * envelope.
   */
  import { Mail, Smartphone, MessageCircle, MessagesCircle } from '@lucide/svelte';

  let {
    surface,
    group = false,
    size = 13,
  }: { surface: 'sms' | 'whatsapp' | 'email'; group?: boolean; size?: number } = $props();
</script>

{#if surface === 'sms'}
  <Smartphone {size} />
{:else if surface === 'email'}
  <Mail {size} />
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
