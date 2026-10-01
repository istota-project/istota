/** A list field rendered as one comma-separated text input, and read back. */
export function profileListString(values: string[]): string {
  return values.join(', ');
}

export function parseListInput(value: string): string[] {
  return value
    .split(',')
    .map((v) => v.trim())
    .filter((v) => v.length > 0);
}
