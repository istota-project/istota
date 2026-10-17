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
  | { state: 'stopping' | 'stopped' }
  | { state: 'setup'; url: string }
  | ({ state: 'error' } & Failure);

export type Status = {
  vm: VmState;
  healthy: boolean | null;
  profile: string | null;
  remote: boolean;
  launch_at_login: boolean;
  notice: string | null;
};

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
    case 'error':
      return 'Istota stopped with an error';
  }
}
