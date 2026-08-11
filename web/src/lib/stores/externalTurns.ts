/**
 * How much of a turn that arrived from outside the room the transcript shows.
 *
 * A pure module beside the store, like `roomOrder.ts`, rather than a helper on
 * `$lib/api`: the store tests mock that module wholesale, so a *function*
 * living there is undefined for every one of them, while the `ExternalTurnDisplay`
 * type is erased at build time and can stay with the payload shapes it describes.
 */
import type { ExternalTurnDisplay } from '$lib/api';

export const EXTERNAL_TURN_DISPLAYS: ExternalTurnDisplay[] = ['full', 'collapsed', 'hidden'];

/**
 * Fold anything the server or an older payload hands us onto a known value.
 *
 * `user_profiles.external_turn_display` is a TEXT column that takes whatever a
 * hand edit puts in it, and the transcript needs a branch it can take. The
 * fallback is `collapsed` rather than `full` because the safe direction is less
 * of a stranger's text on screen, not more.
 */
export function normalizeExternalTurnDisplay(value: unknown): ExternalTurnDisplay {
  return EXTERNAL_TURN_DISPLAYS.includes(value as ExternalTurnDisplay)
    ? (value as ExternalTurnDisplay)
    : 'collapsed';
}
