import { describe, expect, it } from 'vitest';
import { loadStarterScript } from '../../testing/classicScript';

type Review = {
  due: string | null;
  interval: number;
  ease: number;
  reps: number;
  lapses: number;
  history: Array<{ on: string; grade: number; interval: number }>;
  [extra: string]: unknown;
};

type SM2 = {
  INITIAL_EASE: number;
  MIN_EASE: number;
  HISTORY_MAX: number;
  MAX_INTERVAL: number;
  newReview(): Review;
  parseReview(value: unknown): Review;
  grade(review: Review, quality: number, today: string): Review;
  nextInterval(review: Review, quality: number): number;
  isMiss(quality: number): boolean;
  addDays(day: string, days: number): string;
};

function sm2(): SM2 {
  return loadStarterScript('recall', 'sm2.js').SM2 as SM2;
}

describe('SM-2 scheduling', () => {
  it('follows the intervals and ease of a known sequence', () => {
    const S = sm2();
    let review = S.newReview();
    const steps: Array<[string, number]> = [
      ['2026-10-01', 4],
      ['2026-10-02', 4],
      ['2026-10-08', 4],
      ['2026-10-23', 1],
      ['2026-10-24', 3],
      ['2026-10-25', 4],
      ['2026-10-31', 4],
    ];
    const seen = [];
    for (const [today, quality] of steps) {
      review = S.grade(review, quality, today);
      seen.push([review.interval, review.ease, review.reps, review.lapses, review.due]);
    }
    expect(seen).toEqual([
      [1, 2.5, 1, 0, '2026-10-02'],
      [6, 2.5, 2, 0, '2026-10-08'],
      [15, 2.5, 3, 0, '2026-10-23'],
      // A miss starts the repetitions again and costs ease.
      [1, 1.96, 0, 1, '2026-10-24'],
      [1, 1.82, 1, 1, '2026-10-25'],
      [6, 1.82, 2, 1, '2026-10-31'],
      [11, 1.82, 3, 1, '2026-11-11'],
    ]);
    expect(review.history.map((h) => h.grade)).toEqual([4, 4, 4, 1, 3, 4, 4]);
    expect(review.history[3]).toEqual({ on: '2026-10-23', grade: 1, interval: 1 });
  });

  it('raises ease on easy answers, using the ease before the update', () => {
    const S = sm2();
    let review = S.newReview();
    for (const today of ['2026-01-01', '2026-01-02', '2026-01-08']) {
      review = S.grade(review, 5, today);
    }
    // 1, 6, then round(6 * 2.7): the third interval uses the ease the second answer left.
    expect([review.interval, review.ease, review.due]).toEqual([16, 2.8, '2026-01-24']);
  });

  it('never lets ease fall below 1.3', () => {
    const S = sm2();
    let review = S.newReview();
    for (let i = 0; i < 6; i += 1) review = S.grade(review, 0, '2026-03-01');
    expect(review.ease).toBe(S.MIN_EASE);
    expect(review.lapses).toBe(6);
    expect(review.interval).toBe(1);
  });

  it('counts a grade under 3 as a miss', () => {
    const S = sm2();
    expect([0, 1, 2, 3, 4, 5].map((q) => S.isMiss(q))).toEqual([
      true,
      true,
      true,
      false,
      false,
      false,
    ]);
  });

  it('previews the next interval without changing the review', () => {
    const S = sm2();
    const review = S.grade(S.grade(S.newReview(), 4, '2026-10-01'), 4, '2026-10-02');
    expect([1, 3, 4, 5].map((q) => S.nextInterval(review, q))).toEqual([1, 15, 15, 15]);
    expect(review.interval).toBe(6);
  });

  it('refuses a grade outside 0 to 5', () => {
    const S = sm2();
    expect(() => S.grade(S.newReview(), 6, '2026-10-01')).toThrow();
    expect(() => S.grade(S.newReview(), 2.5, '2026-10-01')).toThrow();
  });

  it('adds days across months and years', () => {
    const S = sm2();
    expect(S.addDays('2026-12-30', 3)).toBe('2027-01-02');
    expect(S.addDays('2028-02-28', 1)).toBe('2028-02-29');
  });

  it('reads a stored review leniently, keeping unknown fields', () => {
    const S = sm2();
    expect(
      S.parseReview({ due: '2026-10-30', interval: '6', ease: 0.5, reps: 2, note: 'x' }),
    ).toEqual({
      due: '2026-10-30',
      interval: 6,
      ease: 1.3,
      reps: 2,
      lapses: 0,
      history: [],
      note: 'x',
    });
    expect(S.parseReview(null)).toEqual(S.newReview());
    expect(S.parseReview({ due: 'soon', ease: 'x', history: 'no' })).toEqual(S.newReview());
  });

  it('keeps only the newest history entries', () => {
    const S = sm2();
    let review = S.newReview();
    for (let i = 0; i < S.HISTORY_MAX + 5; i += 1) review = S.grade(review, 4, '2026-10-01');
    expect(review.history).toHaveLength(S.HISTORY_MAX);
    // A long run of passes stops at the ceiling rather than overflowing the date.
    expect(review.interval).toBe(S.MAX_INTERVAL);
    expect(review.due).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it('leaves the review it was given alone', () => {
    const S = sm2();
    const review = S.newReview();
    S.grade(review, 4, '2026-10-01');
    expect(review).toEqual(S.newReview());
  });
});
