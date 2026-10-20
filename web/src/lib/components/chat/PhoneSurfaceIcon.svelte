<script lang="ts">
  /**
   * The one glyph for a phone surface, shared by the room list, the chat
   * header and a texted turn's provenance line (ISSUE-584). WhatsApp is green:
   * one bubble for the private 1:1 chat, two for a group, both outlined like
   * every other glyph in the app. The user's private email room takes an
   * envelope.
   */
  import { Mail, Smartphone, MessageCircle, MessagesCircle } from '@lucide/svelte';
  import { Icon, type IconSize } from '$lib/components/ui';

  let {
    surface,
    group = false,
    size = 'sm',
  }: { surface: 'sms' | 'whatsapp' | 'email'; group?: boolean; size?: IconSize } = $props();
</script>

{#if surface === 'sms'}
  <Icon icon={Smartphone} {size} />
{:else if surface === 'email'}
  <Icon icon={Mail} {size} />
{:else}
  <span class="whatsapp" data-whatsapp={group ? 'group' : 'private'}>
    <Icon icon={group ? MessagesCircle : MessageCircle} {size} />
  </span>
{/if}

<style>
  .whatsapp {
    display: inline-flex;
    color: var(--accent-whatsapp);
  }
</style>
