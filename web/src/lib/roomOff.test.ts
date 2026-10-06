import { describe, expect, it } from 'vitest';
import { codeSpans, describeRoomOff } from './roomOff';

const off = (by: { name: string; guest: boolean; agreed: boolean }[]) => ({
  at: '2026-09-30T10:00:00Z',
  by,
  way_back: '',
});

describe('describeRoomOff', () => {
  it('names one member', () => {
    expect(describeRoomOff(off([{ name: 'Bob', guest: false, agreed: false }]))).toBe(
      'Switched off by Bob.',
    );
  });

  it('marks a guest and a vetoer who has agreed', () => {
    expect(
      describeRoomOff(
        off([
          { name: 'Bob', guest: false, agreed: false },
          { name: 'Max', guest: true, agreed: true },
        ]),
      ),
    ).toBe('Switched off by Bob and Max (a guest, has agreed to switch it back on).');
  });

  it('says a removal from the group names nobody', () => {
    expect(describeRoomOff(off([]))).toBe(
      'It was switched off when it was removed from the group.',
    );
  });
});

describe('codeSpans', () => {
  it('marks backtick spans as code and drops the backticks', () => {
    expect(codeSpans('A member sends `!istota on`, then `!istota on` too.')).toEqual([
      { text: 'A member sends ', code: false },
      { text: '!istota on', code: true },
      { text: ', then ', code: false },
      { text: '!istota on', code: true },
      { text: ' too.', code: false },
    ]);
  });

  it('leaves text with no spans whole', () => {
    expect(codeSpans('Nothing to mark.')).toEqual([{ text: 'Nothing to mark.', code: false }]);
  });
});
