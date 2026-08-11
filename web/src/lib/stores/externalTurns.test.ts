/**
 * The external-turn display normalizer.
 *
 * `user_profiles.external_turn_display` is a TEXT column that takes whatever a
 * hand edit puts in it, and the transcript needs a branch it can take. These
 * assertions are the whole specification of which way each input folds — the
 * store tests cannot stand in for them, because the store is seeded `collapsed`
 * at construction and so passes the interesting cases whatever this returns.
 */
import { describe, it, expect } from 'vitest';
import { EXTERNAL_TURN_DISPLAYS, normalizeExternalTurnDisplay } from './externalTurns';

describe('normalizeExternalTurnDisplay', () => {
  it('passes each recognized value through unchanged', () => {
    for (const v of EXTERNAL_TURN_DISPLAYS) {
      expect(normalizeExternalTurnDisplay(v)).toBe(v);
    }
  });

  it('lists exactly the three values the server accepts', () => {
    // The server's own enum is `("full", "collapsed", "hidden")`
    // (`web_app._PROFILE_EDITABLE_FIELDS`). A client that recognized fewer would
    // silently rewrite a setting the user is allowed to store.
    expect(EXTERNAL_TURN_DISPLAYS).toEqual(['full', 'collapsed', 'hidden']);
  });

  it.each([
    ['an unknown word', 'sideways'],
    ['the empty string', ''],
    ['undefined', undefined],
    ['null', null],
    ['a number', 3],
    ['an object', {}],
  ])('folds %s onto collapsed', (_label, value) => {
    expect(normalizeExternalTurnDisplay(value)).toBe('collapsed');
  });

  it('does not case-fold', () => {
    // Deliberate: the column is written by a validated PUT, so a differently
    // cased value is a hand edit rather than a variant spelling, and guessing at
    // it would mean accepting values the server would refuse.
    expect(normalizeExternalTurnDisplay('FULL')).toBe('collapsed');
  });

  it('never returns full for anything but an exact match', () => {
    // The one direction that matters: the fallback must not open a stranger's
    // mail. Anything unrecognized shows less, never more.
    for (const v of ['ful', 'full ', ' full', 'Full', null, undefined]) {
      expect(normalizeExternalTurnDisplay(v)).not.toBe('full');
    }
  });
});
