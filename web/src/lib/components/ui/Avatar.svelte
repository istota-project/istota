<script lang="ts">
  import { avatarUrl } from '$lib/api';

  interface Props {
    /** Which identity this is. Decides the fallback chip's fill and the URL. */
    kind: 'user' | 'bot';
    /** The istota user id. Required when `kind === 'user'`. */
    userId?: string;
    /**
     * Content hash, when the caller knows one.
     *
     * The three values mean three different things and the difference is a
     * request each. A **string** is a hash the caller read off `/me`, so the
     * URL carries `?v=` and the server answers `immutable` for the caller's own
     * picture and for the bot icon — every later render in a transcript is a
     * cache hit. **`null`** is `/me` saying there is no picture, so nothing is
     * requested at all; without that distinction a deployment with no avatars
     * would pay a 404 per identity per page load. **Absent** is "nobody told
     * me", which is a third party: the request goes out bare and pays one
     * conditional round trip per author per session (D13).
     */
    version?: string | null;
    /** Display name. Supplies the fallback initial. */
    label: string;
    /**
     * Alt text. Empty (decorative) by default: most call sites render the name
     * beside the image, and repeating it is noise for a screen reader. When it
     * is empty the fallback chip carries `aria-hidden`, so the two states have
     * the same accessible name — namely none.
     */
    alt?: string;
  }

  let { kind, userId, version, label, alt = '' }: Props = $props();

  const initial = $derived((label.trim()[0] ?? '?').toUpperCase());

  const src = $derived.by(() => {
    // Known to have no picture, so do not ask for one.
    if (version === null) return '';
    // `avatarUrl` throws on this pairing rather than building a URL with an
    // empty last segment. A user whose id the client has not been given yet is
    // an ordinary case here (a co-member's turn until Stage 6 names them), so
    // it is answered with the chip rather than an exception in a render.
    if (kind === 'user' && !userId) return '';
    return avatarUrl(kind, userId, version);
  });

  // One error path, not distinguished by cause: a 404, a stored image that
  // fails to decode and an offline boot from the cached identity all mean "no
  // picture", and the answer to all three is the initial chip.
  let failed = $state(false);
  $effect(() => {
    // Keyed on the resolved URL. Without the reset the chip is one-way for the
    // life of the component, and two sequences reach that: a request aborted by
    // navigation fires `error` in Chrome and WebKit, and an upload made while
    // offline fails and is then re-tried — which is the moment the Settings
    // preview swaps to a new hash.
    src;
    failed = false;
  });
</script>

<!--
  One identity, drawn. An uploaded picture, an imported one behind it, and the
  initial chip that shipped before either as the terminal fallback (D8).

  Sizing is `--avatar-size`, set by whatever the call site renders this into —
  the chat gutter hands it the gutter's own token, the Settings preview hands it
  4rem. Custom properties inherit, so it goes on the avatar's own wrapper and
  never on a shared container, or every nested avatar resizes with it.

  It applies no filter, deliberately. `--sigil-filter` inverts a flat near-white
  silhouette for the light theme; a photograph through it is a negative.
-->
{#if src && !failed}
  <img class="avatar" {src} {alt} onerror={() => (failed = true)} />
{:else if alt}
  <span class="avatar fallback" class:bot={kind === 'bot'} role="img" aria-label={alt}>
    {initial}
  </span>
{:else}
  <span class="avatar fallback" class:bot={kind === 'bot'} aria-hidden="true">{initial}</span>
{/if}

<style>
  .avatar {
    /* design-lint-allow-begin: --avatar-size is the documented hook a call site
       sets on the wrapper it renders an Avatar into, the way health's cards set
       --card-padding. The fallback is this primitive's own default and is
       deliberately not the chat gutter's token: a primitive that reads
       --chat-avatar has a transcript's sizing baked into every later render
       site, which is the coupling the indirection exists to avoid. */
    width: var(--avatar-size, 2rem);
    height: var(--avatar-size, 2rem);
    /* design-lint-allow-end */
    border-radius: var(--radius-md);
    user-select: none;
  }

  /* The image half of the box. `cover` rather than `contain`: the server
     centre-crops to a square already, so this only has to survive a stored
     image that predates that or arrives from somewhere else. */
  img.avatar {
    object-fit: cover;
    display: block;
  }

  /* The chip. Its fill sits here rather than on `.avatar` so it cannot show
     through behind a picture that is still loading, or around one whose aspect
     ratio does not fill the box. */
  .fallback {
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.85rem;
    font-weight: 600;
    /* design-lint-allow-begin: fixed surface — an avatar fill is an identity
       chip, not a themed surface, so it holds one value in both themes. */
    color: #fff;
    background: #4a4a52;
    /* design-lint-allow-end */
  }
  .fallback.bot {
    background: var(--accent-amber-fill);
    color: var(--accent-amber-fill-fg);
  }

  /* The mobile avatar is a good deal smaller (in the chat gutter it is what
	   buys the shared text inset — see app.css), so the initial and the corner
	   both have to come down with it: the desktop values leave a 600-weight glyph
	   crowding the box, and a 0.5rem radius on a 1.25rem square is a pill rather
	   than a rounded square. */
  @media (max-width: 768px) {
    .avatar {
      border-radius: var(--radius-sm);
    }
    .fallback {
      font-size: var(--text-xs);
    }
  }
</style>
