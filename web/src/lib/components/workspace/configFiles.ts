import type { WorkspaceConfigFile } from '$lib/api';
import { formatBytes } from '$lib/format';

const DESCRIPTIONS: Record<string, string> = {
  'USER.md': 'What the assistant remembers about you, read at the start of every task.',
  'CRON.md': 'Your scheduled jobs.',
  'TASKS.md': 'Tasks the assistant picks up from a checklist.',
  'HEARTBEAT.md': 'Checks the assistant runs on a schedule and alerts you about.',
};

export function configFileLabel(file: WorkspaceConfigFile): string {
  return file.kind === 'skill' ? file.name.slice('skills/'.length) : file.name;
}

export function configFileDescription(file: WorkspaceConfigFile): string {
  if (file.kind === 'skill')
    return `Your instructions for the ${configFileLabel(file)} skill, added to its own whenever it loads.`;
  return DESCRIPTIONS[file.name] ?? '';
}

export function configFileDetail(file: WorkspaceConfigFile): string {
  if (!file.editable) return 'Not a plain file';
  if (!file.exists) return 'Not created yet';
  return file.size === null ? '' : formatBytes(file.size);
}
