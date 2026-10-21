<script lang="ts">
  import { avatarUrl } from '$lib/api';
  import { Avatar } from '@minot/ui';

  interface Props {
    /** Which identity this is. Decides the fallback chip's fill and the URL. */
    kind: 'user' | 'bot';
    /**
     * The istota user id, which is what the endpoint keys on. Without one a
     * `user` avatar renders the chip rather than requesting a URL it cannot
     * build — a supported state, not an omission: an external sender mirrored
     * into a room is named by an email address with no account behind it.
     */
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
    /** Alt text, passed to `Avatar`. Empty (decorative) by default. */
    alt?: string;
  }

  let { kind, userId, version, label, alt = '' }: Props = $props();

  const src = $derived.by(() => {
    // Known to have no picture, so do not ask for one.
    if (version === null) return null;
    // `avatarUrl` throws on this pairing rather than building a URL with an
    // empty last segment. A user whose id the client has not been given is an
    // ordinary case here (an external sender's turn, and the viewer's own on
    // an offline boot, where the id under a guess is the wrong person's), so
    // it is answered with the chip rather than an exception in a render.
    if (kind === 'user' && !userId) return null;
    return avatarUrl(kind, userId, version);
  });
</script>

<!-- An istota identity drawn by the `Avatar` primitive, with the URL built from
     `/api/avatars`. Sizing, the fallback chip and the failed-image reset are
     `Avatar`'s. -->
<Avatar {kind} {src} {label} {alt} />
