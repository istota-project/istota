export function segments(
  snippet: string,
  highlights: number[][],
): { text: string; mark: boolean }[] {
  // Server offsets count Unicode code points, not UTF-16 code units.
  const chars = Array.from(snippet);
  const ranges = highlights
    .filter(([start, end]) => Number.isInteger(start) && Number.isInteger(end))
    .map(([start, end]) => [Math.max(0, start), Math.min(chars.length, end)])
    .filter(([start, end]) => end > start)
    .sort((a, b) => a[0] - b[0]);
  const merged: number[][] = [];
  for (const [start, end] of ranges) {
    const previous = merged[merged.length - 1];
    if (previous && start <= previous[1]) previous[1] = Math.max(previous[1], end);
    else merged.push([start, end]);
  }
  const result: { text: string; mark: boolean }[] = [];
  let cursor = 0;
  for (const [start, end] of merged) {
    if (start > cursor) result.push({ text: chars.slice(cursor, start).join(''), mark: false });
    result.push({ text: chars.slice(start, end).join(''), mark: true });
    cursor = end;
  }
  if (cursor < chars.length) result.push({ text: chars.slice(cursor).join(''), mark: false });
  return result;
}
