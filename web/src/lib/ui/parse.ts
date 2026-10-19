/**
 * Text a form field holds, read back as the value it stands for. Every number
 * or list field parses through here, so a blank, a fraction or a stray comma
 * means the same thing on every page.
 */

/** A number typed into a field, or null when the text is blank or is not one
 *  (or, for `int`, is not a whole number). */
export function parseNumber(text: string, kind: 'int' | 'float'): number | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  if (!Number.isFinite(n)) return null;
  if (kind === 'int' && !Number.isInteger(n)) return null;
  return n;
}

/**
 * An optional whole-number setting. `null` when the field is blank, which
 * unsets the setting; `undefined` when the text is not a whole number at or
 * above `min` (default 0), which leaves the setting as it was; else the number.
 */
export function parseOptionalInt(
  text: string,
  { min = 0 }: { min?: number } = {},
): number | null | undefined {
  if (!text.trim()) return null;
  const n = parseNumber(text, 'int');
  if (n === null || n < min) return undefined;
  return n;
}

export interface ListOptions {
  /** `,` for a list typed on one line (the default), `\n` for one per line. */
  separator?: ',' | '\n';
  /** Read every item as a number; the result is null if one is not. */
  numeric?: boolean;
}

/** A list typed into one field. Items are trimmed and blank ones dropped. */
export function parseList(text: string, options?: ListOptions & { numeric?: false }): string[];
export function parseList(text: string, options: ListOptions & { numeric: true }): number[] | null;
export function parseList(text: string, options?: ListOptions): string[] | number[] | null;
export function parseList(
  text: string,
  { separator = ',', numeric = false }: ListOptions = {},
): string[] | number[] | null {
  const items = text
    .split(separator)
    .map((item) => item.trim())
    .filter((item) => item !== '');
  if (!numeric) return items;
  const numbers = items.map(Number);
  return numbers.every(Number.isFinite) ? numbers : null;
}

/** A list as the one-line text `parseList` reads back. */
export function formatList(values: string[]): string {
  return values.join(', ');
}
