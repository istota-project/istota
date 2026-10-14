import type { WorkspaceScript, WorkspaceScriptRef, WorkspaceScriptsIndex } from '$lib/api';

export type ScriptKind = WorkspaceScript['kind'];
export type BrokenScript = WorkspaceScriptsIndex['broken'][number];

const SOURCE_FILES: Record<WorkspaceScriptRef['source'], string> = {
  cron: 'CRON.md',
  heartbeat: 'HEARTBEAT.md',
};

const STATES: Record<WorkspaceScriptRef['state'], string> = {
  enabled: '',
  disabled: 'disabled',
  suspended: 'suspended',
  not_synced: 'not synced yet',
  admin_only: 'admin only',
};

export function scriptRefLabel(ref: WorkspaceScriptRef): string {
  const state = STATES[ref.state];
  return `${SOURCE_FILES[ref.source]}: ${ref.name}${state ? ` (${state})` : ''}`;
}

export function scriptUsage(file: WorkspaceScript): string {
  const parts = [
    file.referenced_by.length
      ? `Used by ${file.referenced_by.map(scriptRefLabel).join(', ')}`
      : 'Not used by any job',
  ];
  const mentions = file.mentioned_in.length;
  if (mentions) parts.push(`mentioned in ${mentions} ${mentions === 1 ? 'prompt' : 'prompts'}`);
  return parts.join(' · ');
}

/** A missing path belongs to the prompt files when a job names it as its prompt file. */
export function brokenKind(broken: BrokenScript): ScriptKind {
  return broken.referenced_by.some((ref) => ref.kind === 'prompt_file') ? 'prompt' : 'script';
}
