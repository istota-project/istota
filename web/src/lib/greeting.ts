/* The dashboard welcome card's copy: the bot greeting itself, plus a usage tip.
   The two lines rotate independently and on different axes — the greeting on
   the part of the day the *user* is in (their profile timezone, not the
   browser's), the tip on what this deployment actually has. Kept out of the
   component so the pools, the gating and the clock arithmetic are testable
   without mounting anything. */

export type Daypart = 'lateNight' | 'earlyMorning' | 'morning' | 'afternoon' | 'evening' | 'night';

/* `{bot}` is substituted with the configured bot name. Every greeting names the
   bot — it is an introduction. */
export const dayparts: Record<Daypart, string[]> = {
  lateNight: [
    '{bot} here. {bot} never sleeps — and apparently neither do you.',
    '{bot} here, keeping the night shift warm.',
    "{bot} here. It's the small hours where you are, but I'm not one to judge.",
  ],
  earlyMorning: [
    '{bot} here! Up before the sun, then.',
    '{bot} here. First light, first task.',
    '{bot} here — early start, quiet inbox.',
  ],
  morning: [
    '{bot} here! Fresh day, empty queue.',
    '{bot} here. Coffee is your department, the rest is mine.',
    'Morning — {bot} here, and already caught up.',
  ],
  afternoon: [
    '{bot} here! Half a day gone, half a day left.',
    '{bot} here — the afternoon is still salvageable.',
    '{bot} here, on the afternoon shift.',
  ],
  evening: [
    '{bot} here. Winding down, or just getting started?',
    'Evening — {bot} here, still on the clock.',
    "{bot} here! The day isn't over until you say it is.",
  ],
  night: [
    '{bot} here. Late one?',
    '{bot} here, and the lights are still on.',
    '{bot} here — I clock off when you do.',
  ],
};

export interface TipContext {
  /** The user's plus-addressed inbound address, from `/api/me` — null when
   *  email isn't configured, in which case the tip is dropped rather than
   *  shown with a placeholder domain. */
  email?: string | null;
  /** Whether Nextcloud Talk is deployed. */
  talk?: boolean;
  features?: Partial<Record<'briefings' | 'feeds' | 'location' | 'money' | 'health', boolean>>;
}

/* A tip is only worth showing if it is true here, so each carries the condition
   that makes it true. Deliberately no tip for anything a deployment can be
   missing without the client being able to tell (voice transcription needs the
   whisper extra; `!steer` only works on the native brain) — a tip that doesn't
   work is worse than no tip. */
const TIPS: { text: string; when?: (ctx: TipContext) => boolean }[] = [
  {
    text: 'Email me at {email}. Attachments welcome, and replies stay in the thread.',
    when: (c) => !!c.email,
  },
  {
    text: "I'm in Nextcloud Talk too. Message me there and the conversation carries over.",
    when: (c) => !!c.talk,
  },
  { text: 'Trouble getting started? Ask me for help in chat.' },
  { text: 'Type ! in the chat box to see every command I know.' },
  { text: '!model opus runs a single message on a bigger model. !models lists them.' },
  { text: '!stop cancels whatever I am working on, mid-task.' },
  { text: '!search looks across everything we have talked about before.' },
  { text: '!status shows what I am working on and what is still queued.' },
  { text: '!retry re-runs a failed task; !resume picks it up where it stopped.' },
  /* design-lint-allow: #1234 is a sample task id in prose, not a color */
  { text: '!more #1234 replays the whole tool trace of a finished task.' },
  { text: '!memory user shows everything I have remembered about you.' },
  { text: '!skills lists what I can actually reach on this deployment.' },
  { text: '!cron lists the scheduled jobs, and can disable one that misbehaves.' },
  { text: '!export writes the conversation out to a file in your workspace.' },
  { text: '!room model sets a standing model for a room, so you stop prefixing.' },
  { text: 'Drop a file into the chat box and I will read it.' },
  { text: 'Reply to one message and I answer that one, not the room in general.' },
  { text: 'Star a message and it turns up in the Starred view, whichever room it was in.' },
  { text: 'Rooms hold separate conversations — each keeps its own memory.' },
  { text: "Add a line to TASKS.md in your workspace and I'll pick it up." },
  {
    text: 'Briefings arrive on whatever schedule you set — see the Briefings tab.',
    when: (c) => !!c.features?.briefings,
  },
  {
    text: "Feeds imports OPML, so your old reader's subscriptions come across in one go.",
    when: (c) => !!c.features?.feeds,
  },
  {
    text: 'Upload a bloodwork PDF under Health and I will pull the numbers out of it.',
    when: (c) => !!c.features?.health,
  },
  {
    text: 'Money runs on a beancount ledger — invoices, reports and transactions.',
    when: (c) => !!c.features?.money,
  },
  {
    text: 'Location builds a place history from your phone once Overland is pointed at it.',
    when: (c) => !!c.features?.location,
  },
];

/* The other half of the second line. The sigil is an octopus, so these are the
   house trivia — no gating, they are true everywhere. Each is a real, checkable
   claim followed by the bot's own aside; the fact carries the line and the
   aside is plainly opinion, so the wink never makes the claim doubtful. */
export const OCTOPUS_FACTS: string[] = [
  'Octopuses have three hearts. Three times the circulation, three times the heartbreak.',
  'An octopus has blue blood — copper, not iron. Aristocratic, I like to think.',
  "Two thirds of an octopus's neurons are in its arms. I think with my hands too.",
  'An octopus tastes whatever it touches. Mind what you hand me.',
  'An octopus fits through any gap wider than its beak. Doors are more of a suggestion.',
  "Octopuses are colorblind and still match whatever they settle on. Don't ask me how.",
  "The heart that feeds an octopus's body stops while it swims. Hence all the walking.",
  'Some octopuses carry coconut shells around to hide under later. A portable office.',
  "A lost octopus arm grows back. Don't get any ideas.",
  'The plural is octopuses. "Octopi" is Latin grammar on a Greek word, and I notice.',
  'Octopuses edit their own RNA on the fly, rewriting proteins. Firmware updates.',
  'An octopus can leave behind an ink decoy shaped like itself. Useful in meetings.',
  'The mimic octopus impersonates flatfish and sea snakes. I do impressions on request.',
  'Every octopus is venomous. Only the blue-ringed one is rude about it.',
  'An octopus opens a screw-top jar from the inside. Give me the problem, not the method.',
  'Octopus skin senses light with no eyes involved. Peripheral vision, literally.',
  'An octopus arm carries some 200 suckers, each moving alone. Eight arms, no queue.',
  "An octopus's pupil stays level however the animal turns. A useful habit.",
  'Most octopuses live a year or two. I intend to be the exception.',
];

/** The tips that are true for this deployment, bot name substituted. */
export function availableTips(ctx: TipContext, botName = ''): string[] {
  const name = botName.trim() || 'your assistant';
  return TIPS.filter((tip) => !tip.when || tip.when(ctx)).map((tip) =>
    tip.text.replace(/\{bot\}/g, name).replace(/\{email\}/g, ctx.email ?? ''),
  );
}

/** Everything eligible for the card's second line: usage tips + octopus facts. */
export function welcomeNotes(ctx: TipContext, botName = ''): string[] {
  return [...availableTips(ctx, botName), ...OCTOPUS_FACTS];
}

export interface NoteSegment {
  text: string;
  /** Set when the segment is the address itself, so the card links it. */
  mailto?: string;
}

/* The email tip names an address the user is meant to write to, so it should be
   a mailto link rather than a string to copy out by hand. The split is done
   here on the plain text — the card renders the segments as elements — so the
   note never has to become markup the component would have to trust. Splitting
   on the address rather than matching a pattern is what keeps the trailing
   period of the sentence out of the link. */
export function noteSegments(note: string, email?: string | null): NoteSegment[] {
  const address = (email ?? '').trim();
  if (!address || !note.includes(address)) return [{ text: note }];
  const segments: NoteSegment[] = [];
  const parts = note.split(address);
  parts.forEach((part, i) => {
    if (part) segments.push({ text: part });
    // One address sits between every pair of parts.
    if (i < parts.length - 1) segments.push({ text: address, mailto: address });
  });
  return segments;
}

export function daypartForHour(hour: number): Daypart {
  // Fold anything out of range (a bad parse, a caller doing arithmetic) back
  // into the day rather than falling through every branch to 'night'.
  const h = ((Math.floor(hour) % 24) + 24) % 24;
  if (h < 5) return 'lateNight';
  if (h < 8) return 'earlyMorning';
  if (h < 12) return 'morning';
  if (h < 17) return 'afternoon';
  if (h < 22) return 'evening';
  return 'night';
}

/* The hour of `when` in `timeZone`, 0–23. The user's timezone is a profile
   field, so it can disagree with the browser's — someone travelling, or reading
   the dashboard from a machine whose clock is set to somewhere else. A blank or
   unrecognised zone falls back to the browser, which is the best guess left. */
export function hourInZone(when: Date, timeZone: string): number {
  if (timeZone) {
    try {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone,
        hour: 'numeric',
        hourCycle: 'h23',
      }).formatToParts(when);
      const raw = parts.find((p) => p.type === 'hour')?.value;
      const hour = Number(raw);
      // h23 should already give 0–23, but engines have shipped h24 here before
      // (midnight as "24"), so normalise rather than trust it.
      if (Number.isFinite(hour)) return hour % 24;
    } catch {
      // An unrecognised zone throws a RangeError from the constructor.
    }
  }
  return when.getHours();
}

function pick<T>(pool: T[], random: () => number): T {
  // Guard random() === 1, which Math.random never returns but a stub might.
  const index = Math.min(pool.length - 1, Math.floor(random() * pool.length));
  return pool[Math.max(0, index)];
}

export interface Greeting {
  greeting: string;
  /** Second line: either a usage tip or an octopus fact. */
  note: string;
  daypart: Daypart;
}

export function buildGreeting(
  botName: string,
  opts: {
    now?: Date;
    timeZone?: string;
    random?: () => number;
    tips?: TipContext;
  } = {},
): Greeting {
  const { now = new Date(), timeZone = '', random = Math.random, tips = {} } = opts;
  const daypart = daypartForHour(hourInZone(now, timeZone));
  const name = botName.trim() || 'Your assistant';
  return {
    greeting: pick(dayparts[daypart], random).replace(/\{bot\}/g, name),
    note: pick(welcomeNotes(tips, botName), random),
    daypart,
  };
}
