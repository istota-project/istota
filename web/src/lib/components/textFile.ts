/** What `TextFileEditor` needs from a loaded file. */
export interface TextFile {
  content: string;
  revision: string;
  /** False when the file is absent or whitespace-only; both are the empty state. */
  exists: boolean;
  /** Starting text offered for the empty state. */
  template?: string;
}
