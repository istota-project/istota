import { describe, it, expect } from 'vitest';
import {
  availableTips,
  buildGreeting,
  dayparts,
  daypartForHour,
  hourInZone,
  noteSegments,
  welcomeNotes,
  OCTOPUS_FACTS,
  type Daypart,
  type TipContext,
} from './greeting';

describe('daypartForHour', () => {
  const cases: [number, Daypart][] = [
    [0, 'lateNight'],
    [4, 'lateNight'],
    [5, 'earlyMorning'],
    [7, 'earlyMorning'],
    [8, 'morning'],
    [11, 'morning'],
    [12, 'afternoon'],
    [16, 'afternoon'],
    [17, 'evening'],
    [21, 'evening'],
    [22, 'night'],
    [23, 'night'],
  ];
  for (const [hour, expected] of cases) {
    it(`maps ${hour}:00 to ${expected}`, () => {
      expect(daypartForHour(hour)).toBe(expected);
    });
  }

  it('folds an out-of-range hour back into the day', () => {
    expect(daypartForHour(24)).toBe('lateNight');
    expect(daypartForHour(-1)).toBe('night');
  });
});

describe('hourInZone', () => {
  // 2026-07-27T12:00:00Z — a summer instant, so the European zones are on DST.
  const noonUtc = new Date('2026-07-27T12:00:00Z');

  it('reads the hour in the named zone, not the browser zone', () => {
    expect(hourInZone(noonUtc, 'UTC')).toBe(12);
    expect(hourInZone(noonUtc, 'Europe/Lisbon')).toBe(14);
    expect(hourInZone(noonUtc, 'America/Los_Angeles')).toBe(5);
  });

  it('reads midnight as 0 rather than 24', () => {
    expect(hourInZone(new Date('2026-07-27T00:00:00Z'), 'UTC')).toBe(0);
  });

  it('falls back to the browser hour for a blank or unusable zone', () => {
    const local = noonUtc.getHours();
    expect(hourInZone(noonUtc, '')).toBe(local);
    expect(hourInZone(noonUtc, 'Not/AZone')).toBe(local);
  });
});

describe('availableTips', () => {
  const everything: TipContext = {
    email: 'zorg+alice@bot.example.com',
    talk: true,
    features: { briefings: true, feeds: true, location: true, money: true, health: true },
  };

  it('names the real plus-address rather than a placeholder', () => {
    const tips = availableTips(everything);
    expect(tips.some((t) => t.includes('zorg+alice@bot.example.com'))).toBe(true);
  });

  it('drops the email tip when no address is configured', () => {
    for (const email of [null, undefined, '']) {
      const tips = availableTips({ ...everything, email });
      expect(tips.some((t) => t.toLowerCase().includes('email'))).toBe(false);
    }
  });

  it('drops the Talk tip when Talk is not deployed', () => {
    const tips = availableTips({ ...everything, talk: false });
    expect(tips.some((t) => t.includes('Talk'))).toBe(false);
  });

  it('drops a module tip when the user does not have that module', () => {
    const tips = availableTips({ ...everything, features: {} });
    for (const word of ['Briefings', 'Feeds', 'Location', 'Money', 'Health']) {
      expect(
        tips.some((t) => t.includes(word)),
        `${word} tip leaked`,
      ).toBe(false);
    }
  });

  it('always leaves something to say, even with nothing configured', () => {
    // A bare install still has chat, so the pool can never be empty — the card
    // would otherwise render a blank second line.
    expect(availableTips({}).length).toBeGreaterThan(0);
  });

  it('substitutes the bot name', () => {
    for (const tip of availableTips(everything, 'Zorg')) {
      expect(tip).not.toContain('{bot}');
    }
  });
});

describe('welcomeNotes', () => {
  const everything: TipContext = {
    email: 'zorg+alice@bot.example.com',
    talk: true,
    features: { briefings: true, feeds: true, location: true, money: true, health: true },
  };

  it('mixes the deployment tips with the octopus facts', () => {
    const notes = welcomeNotes(everything, 'Zorg');
    for (const tip of availableTips(everything, 'Zorg')) expect(notes).toContain(tip);
    for (const fact of OCTOPUS_FACTS) expect(notes).toContain(fact);
  });

  it('keeps the facts when nothing at all is configured', () => {
    // The facts are ungated, so a bare install still gets a varied second line.
    const notes = welcomeNotes({});
    for (const fact of OCTOPUS_FACTS) expect(notes).toContain(fact);
  });

  it('has facts short enough not to grow the card', () => {
    // The line sits beside the tiles and the row is sized to the tallest item;
    // past ~90 characters it wraps to a third line and pushes the row taller.
    for (const fact of OCTOPUS_FACTS) {
      expect(fact.length, fact).toBeLessThanOrEqual(90);
    }
  });
});

describe('noteSegments', () => {
  const email = 'zorg+alice@bot.example.com';

  it('splits the email tip so the address can be linked', () => {
    const note = availableTips({ email })[0];
    const segments = noteSegments(note, email);
    expect(segments.map((s) => s.text).join('')).toBe(note);
    const linked = segments.filter((s) => s.mailto);
    expect(linked).toHaveLength(1);
    expect(linked[0].text).toBe(email);
    expect(linked[0].mailto).toBe(email);
  });

  it('leaves the trailing punctuation outside the link', () => {
    const segments = noteSegments(`Email me at ${email}. Attachments welcome.`, email);
    expect(segments[segments.length - 1].text).toBe('. Attachments welcome.');
    expect(segments[segments.length - 1].mailto).toBeUndefined();
  });

  it('is a single plain segment for a note that names no address', () => {
    const fact = OCTOPUS_FACTS[0];
    expect(noteSegments(fact, email)).toEqual([{ text: fact }]);
  });

  it('is a single plain segment when no address is configured', () => {
    const note = 'Email me at nowhere.';
    for (const address of [null, undefined, '', '   ']) {
      expect(noteSegments(note, address)).toEqual([{ text: note }]);
    }
  });

  it('links every occurrence of the address', () => {
    const segments = noteSegments(`${email} and ${email}`, email);
    expect(segments.filter((s) => s.mailto)).toHaveLength(2);
    expect(segments.map((s) => s.text).join('')).toBe(`${email} and ${email}`);
  });
});

describe('buildGreeting', () => {
  const at = (iso: string) => new Date(iso);
  const base = { now: at('2026-07-27T09:00:00Z'), timeZone: 'UTC' };

  it('substitutes the bot name into the greeting', () => {
    const g = buildGreeting('Zorg', base);
    expect(g.greeting).toContain('Zorg');
    expect(g.greeting).not.toContain('{bot}');
    expect(g.note).not.toContain('{bot}');
  });

  it('falls back to a neutral name when the bot has none', () => {
    const g = buildGreeting('', base);
    expect(g.greeting).not.toContain('{bot}');
    expect(g.greeting.trim()).not.toBe('');
  });

  it('picks the greeting from the daypart the user is actually in', () => {
    // 03:00 in Lisbon is 01:00 UTC — the late-night pool, not the morning one.
    const g = buildGreeting('Zorg', {
      now: at('2026-07-27T01:00:00Z'),
      timeZone: 'Europe/Lisbon',
      random: () => 0,
    });
    expect(g.daypart).toBe('lateNight');
    expect(g.greeting).toBe(dayparts.lateNight[0].replace(/\{bot\}/g, 'Zorg'));
  });

  it('draws the note from the context, not from the daypart', () => {
    const g = buildGreeting('Zorg', {
      ...base,
      random: () => 0,
      tips: { email: 'zorg+alice@bot.example.com' },
    });
    expect(g.note).toBe(welcomeNotes({ email: 'zorg+alice@bot.example.com' }, 'Zorg')[0]);
  });

  it('rotates: a different random draw yields a different line', () => {
    const first = buildGreeting('Zorg', { ...base, random: () => 0 });
    const last = buildGreeting('Zorg', { ...base, random: () => 0.999 });
    expect(first.greeting).not.toBe(last.greeting);
    expect(first.note).not.toBe(last.note);
  });

  it('stays in range for the extreme random values', () => {
    for (const random of [() => 0, () => 0.999999, () => 1]) {
      const g = buildGreeting('Zorg', { ...base, random });
      expect(g.greeting).toBeTruthy();
      expect(g.note).toBeTruthy();
    }
  });

  it('offers a non-empty greeting pool for every daypart', () => {
    for (const [name, pool] of Object.entries(dayparts)) {
      expect(pool.length, `${name} greetings`).toBeGreaterThan(0);
      for (const line of pool) {
        expect(line, `${name} greeting names the bot`).toContain('{bot}');
      }
    }
  });
});
