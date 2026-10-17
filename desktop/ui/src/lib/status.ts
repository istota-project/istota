// The app's status as the Rust side sends it (`Status` in src-tauri/src/main.rs).

export type Failure = {
  step: string;
  command: string;
  exit_code: number | null;
  message: string;
  tail: string[];
};

export type VmState =
  | { state: 'unknown' | 'absent' | 'creating' | 'loading' | 'starting' | 'running' }
  | { state: 'stopping' | 'stopped' | 'removing' }
  | { state: 'setup'; url: string }
  | ({ state: 'error' } & Failure);

export type Job = 'create' | 'move' | 'import' | 'restore';

export type Step = {
  name: string;
  started_ms: number | null;
  finished_ms: number | null;
};

export type Progress = {
  job: Job;
  started_ms: number;
  steps: Step[];
  lines: string[];
};

export type Layer = {
  layer: string;
  state: 'absent' | 'building' | 'ready' | 'failed';
  detail: string;
};

export type Status = {
  vm: VmState;
  healthy: boolean | null;
  profile: string | null;
  remote: boolean;
  launch_at_login: boolean;
  notice: string | null;
  local_ready: boolean;
  progress: Progress | null;
  resumable: boolean;
  layers: Layer[];
  layers_running: boolean;
  layers_hint: string | null;
};

// `FirstRun` in main.rs.
export type FirstRun = {
  refusals: string[];
  offers: { cli_install: boolean; standalone: boolean; cli_workspace: string | null };
  default_folder: string;
  release: string | null;
};

export type LocalModel = {
  name: string;
  port: number;
  listening: boolean;
  allowed: boolean;
};

// `SettingsInfo` in main.rs.
export type SettingsInfo = {
  running: boolean;
  claude_code_wanted: boolean;
  models: LocalModel[];
  workspace: string | null;
};

export const JOB_TITLES: Record<Job, string> = {
  create: 'Setting up Istota on this Mac',
  move: 'Moving your command-line Istota into the app',
  import: 'Importing your standalone Istota',
  restore: 'Restoring Istota from its backup',
};

export const LAYER_NAMES: Record<string, string> = {
  'claude-code': 'Claude Code',
  chrome: 'Google Chrome',
};

/// Whether the window is following a job that makes the VM.
export function making(status: Status): boolean {
  return (
    status.progress !== null &&
    (['creating', 'loading', 'setup'].includes(status.vm.state) || status.resumable)
  );
}

/// The first-run choice: nothing on this Mac yet, and nothing in flight.
export function firstRun(status: Status): boolean {
  return !status.remote && status.vm.state === 'absent' && status.progress === null;
}

export function elapsed(fromMs: number, toMs: number): string {
  const seconds = Math.max(0, Math.round((toMs - fromMs) / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes} min ${seconds % 60} s` : `${seconds} s`;
}

export function headline(status: Status): string {
  if (status.remote) return `Connected to ${status.profile ?? 'a remote Istota'}`;
  switch (status.vm.state) {
    case 'unknown':
      return 'Checking Istota on this Mac';
    case 'absent':
      return 'Istota is not set up on this Mac';
    case 'creating':
      return 'Setting up Istota';
    case 'loading':
      return 'Installing the release';
    case 'setup':
      return 'Setup is waiting for you';
    case 'starting':
      return 'Starting Istota';
    case 'running':
      return status.healthy === false ? 'Istota is not answering' : 'Istota is running';
    case 'stopping':
      return 'Stopping Istota';
    case 'stopped':
      return 'Istota is stopped';
    case 'removing':
      return 'Removing Istota';
    case 'error':
      return 'Istota stopped with an error';
  }
}
