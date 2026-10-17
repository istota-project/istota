//! The VM's state, kept by the app: `absent → creating → loading → setup →
//! running ⇄ stopped`, plus `starting`, `stopping`, `removing` and
//! `error(step, message)`.
//!
//! `next` is the whole state machine, a pure function of the state and one
//! event. `Supervisor` turns Mac CLI calls into events: it runs one call at a
//! time, never retries a failed one, and never reports a step done that
//! exited non-zero. `create`, `restore` and `import-standalone` all make a
//! VM, so all three walk `creating → … → running`, and a failed one is resumed
//! by running it again (the CLI's verbs resume where they stopped).

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use serde::Serialize;

use crate::paths::Paths;
use crate::stack::{self, Call, Outcome};

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Failure {
    pub step: String,
    /// The command as `Call::display` shows it; empty for a check the app made itself.
    pub command: String,
    pub exit_code: Option<i32>,
    pub message: String,
    pub tail: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum VmState {
    /// Before the first `limactl list`.
    Unknown,
    Absent,
    Creating,
    /// `create` reached the images: the release is being installed.
    Loading,
    /// `create` printed the setup page's address; the stack is set up there.
    Setup { url: String },
    Starting,
    Running,
    Stopping,
    Stopped,
    /// `destroy` is deleting the VM.
    Removing,
    Error(Failure),
}

#[derive(Clone, Debug, PartialEq)]
pub enum Listed {
    None,
    Running,
    Stopped,
    Other(String),
}

#[derive(Clone, Debug)]
pub enum Event {
    Listed(Listed),
    ListFailed(Failure),
    CreateStarted,
    CreateLine(String),
    CreateExited(Failure, bool),
    StartStarted,
    StartExited(Failure, bool),
    StopStarted,
    StopExited(Failure, bool),
    RemoveStarted,
    RemoveExited(Failure, bool),
    /// The user cancelled a call that makes the VM; it was stopped.
    Cancelled(String),
    /// A VM is there, but the app never saw its first run finish.
    Unfinished,
    FolderGone(String),
}

/// The step a VM-making call that did not finish is resumed from.
pub const MAKE_STEPS: &[&str] = &["create", "restore", "import"];

/// Whether a failure is one the app offers to resume: a call that makes the VM.
pub fn resumable(failure: &Failure) -> bool {
    MAKE_STEPS.contains(&failure.step.as_str())
}

/// The line `create --web-setup-in-app` prints once the setup page answers.
pub const SETUP_URL_PREFIX: &str = "ISTOTA_SETUP_URL=";
/// What the VM's `update --images` logs as it starts loading the archive.
const LOADING_MARKER: &str = "loading the images of ";

/// The setup page's address, if `line` is the CLI's line naming one on this Mac.
pub fn setup_url(line: &str) -> Option<String> {
    let url = line.trim().strip_prefix(SETUP_URL_PREFIX)?;
    let local = url.starts_with("http://localhost:") || url.starts_with("http://127.0.0.1:");
    if local && !url.contains(char::is_whitespace) {
        Some(url.to_string())
    } else {
        None
    }
}

fn in_create(state: &VmState) -> bool {
    matches!(state, VmState::Creating | VmState::Loading | VmState::Setup { .. })
}

fn busy(state: &VmState) -> bool {
    in_create(state) || matches!(state, VmState::Starting | VmState::Stopping | VmState::Removing)
}

pub fn next(state: &VmState, event: Event) -> VmState {
    match event {
        // A listing never interrupts a call in flight; that call's exit decides.
        Event::Listed(_) | Event::ListFailed(_) if busy(state) => state.clone(),
        // An error stays until a call moves on from it.
        Event::Listed(_) if matches!(state, VmState::Error(_)) => state.clone(),
        Event::Listed(Listed::None) => VmState::Absent,
        Event::Listed(Listed::Running) => VmState::Running,
        Event::Listed(Listed::Stopped) => VmState::Stopped,
        Event::Listed(Listed::Other(status)) => VmState::Error(Failure {
            step: "status".into(),
            command: "limactl list --json".into(),
            exit_code: None,
            message: format!("Lima reports the VM as {status}."),
            tail: Vec::new(),
        }),
        Event::ListFailed(failure) => VmState::Error(failure),
        Event::CreateStarted => VmState::Creating,
        Event::CreateLine(line) => {
            if !in_create(state) {
                return state.clone();
            }
            if let Some(url) = setup_url(&line) {
                return VmState::Setup { url };
            }
            if matches!(state, VmState::Creating) && line.contains(LOADING_MARKER) {
                return VmState::Loading;
            }
            state.clone()
        }
        Event::CreateExited(_, true) | Event::StartExited(_, true) => VmState::Running,
        Event::StopExited(_, true) => VmState::Stopped,
        Event::CreateExited(failure, false)
        | Event::StartExited(failure, false)
        | Event::StopExited(failure, false) => VmState::Error(failure),
        Event::StartStarted => VmState::Starting,
        Event::StopStarted => VmState::Stopping,
        Event::RemoveStarted => VmState::Removing,
        Event::RemoveExited(_, true) => VmState::Absent,
        Event::RemoveExited(failure, false) => VmState::Error(failure),
        Event::Cancelled(step) => VmState::Error(Failure {
            step,
            command: String::new(),
            exit_code: None,
            message: "Cancelled. Istota on this Mac is stopped; Resume continues where it stopped.".into(),
            tail: Vec::new(),
        }),
        Event::Unfinished => VmState::Error(Failure {
            step: "create".into(),
            command: String::new(),
            exit_code: None,
            message: "Setting up Istota on this Mac did not finish. Resume continues where it stopped.".into(),
            tail: Vec::new(),
        }),
        Event::FolderGone(path) => VmState::Error(Failure {
            step: "folder".into(),
            command: String::new(),
            exit_code: None,
            message: format!(
                "The workspace folder {path} is gone, or its disk is not connected. Istota stopped so nothing is written elsewhere."
            ),
            tail: Vec::new(),
        }),
    }
}

/// The instance `limactl list --json` reports for the app's VM. It prints one
/// JSON object per instance and nothing when there are none.
pub fn parse_list(stdout: &str, instance: &str) -> Listed {
    for line in stdout.lines() {
        let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        if value.get("name").and_then(|n| n.as_str()) != Some(instance) {
            continue;
        }
        return match value.get("status").and_then(|s| s.as_str()).unwrap_or("") {
            "Running" => Listed::Running,
            "Stopped" => Listed::Stopped,
            other => Listed::Other(other.to_string()),
        };
    }
    Listed::None
}

pub fn failure(call: &Call, outcome: &Outcome) -> Failure {
    Failure {
        step: call.step().to_string(),
        command: call.display(),
        exit_code: outcome.code,
        message: stack::exit_message(outcome.code),
        tail: outcome.tail.clone(),
    }
}

type Notify = Box<dyn Fn(&VmState) + Send + Sync>;
type LineSink = Box<dyn Fn(&str) + Send + Sync>;

pub struct Supervisor {
    paths: Paths,
    state: Mutex<VmState>,
    /// Held for the length of each call: one Mac CLI call at a time.
    calls: Mutex<()>,
    /// The process id of a VM-making call in flight, for `cancel`.
    making: Mutex<Option<u32>>,
    cancelled: AtomicBool,
    notify: Notify,
    /// Every line any call prints, for the app's log.
    lines: LineSink,
}

impl Supervisor {
    pub fn new(paths: Paths, notify: Notify, lines: LineSink) -> Arc<Supervisor> {
        Arc::new(Supervisor {
            paths,
            state: Mutex::new(VmState::Unknown),
            calls: Mutex::new(()),
            making: Mutex::new(None),
            cancelled: AtomicBool::new(false),
            notify,
            lines,
        })
    }

    pub fn state(&self) -> VmState {
        self.state.lock().unwrap().clone()
    }

    fn apply(&self, event: Event) -> VmState {
        let (changed, new) = {
            let mut state = self.state.lock().unwrap();
            let new = next(&state, event);
            let changed = *state != new;
            *state = new.clone();
            (changed, new)
        };
        if changed {
            (self.notify)(&new);
        }
        new
    }

    /// Runs one call. `log_output` is false for a call whose output is a
    /// value (a login code) or goes to a file of its own: the app's log keeps
    /// only the command and its exit for those.
    fn call(&self, call: &Call, log_output: bool, on_line: impl FnMut(&str)) -> Outcome {
        self.call_with(call, log_output, |_| {}, on_line)
    }

    fn call_with(
        &self,
        call: &Call,
        log_output: bool,
        on_spawn: impl FnOnce(u32),
        mut on_line: impl FnMut(&str),
    ) -> Outcome {
        let command = stack::command(&self.paths, call);
        (self.lines)(&format!("$ {}", call.display()));
        let outcome = stack::run_with(command, call.stdin(&self.paths.instance), on_spawn, |line| {
            if log_output {
                (self.lines)(line);
            }
            on_line(line);
        });
        (self.lines)(&format!("exit {:?}", outcome.code));
        outcome
    }

    /// Asks Lima where the VM is, and adopts whatever it finds: a VM a
    /// crashed app left running is simply running.
    pub fn refresh(&self) -> VmState {
        let _guard = self.calls.lock().unwrap();
        self.refresh_locked()
    }

    fn refresh_locked(&self) -> VmState {
        let outcome = stack::run(stack::limactl_list(&self.paths), |_| {});
        if !outcome.ok() {
            return self.apply(Event::ListFailed(Failure {
                step: "status".into(),
                command: "limactl list --json".into(),
                exit_code: outcome.code,
                message: stack::exit_message(outcome.code),
                tail: outcome.tail,
            }));
        }
        self.apply(Event::Listed(parse_list(&outcome.stdout, &self.paths.instance)))
    }

    /// What the app does at launch: adopt a running VM, start a stopped one.
    /// `finished` is whether the app saw this VM's first run through; a VM
    /// it did not is left as it is, with Resume offered.
    pub fn adopt_or_start(&self, finished: bool) -> VmState {
        match self.refresh() {
            VmState::Running | VmState::Stopped if !finished => self.apply(Event::Unfinished),
            VmState::Stopped => self.start(),
            state => state,
        }
    }

    #[cfg(test)]
    fn create(&self, appliance: &Path, workspace: &Path, on_line: impl FnMut(&str)) -> VmState {
        self.make(&Call::Create { appliance, workspace }, on_line)
    }

    /// Runs a call that makes the VM (`create`, `restore`, `import-standalone`),
    /// following its output through `creating`, `loading` and `setup`. A
    /// cancelled one stops whatever VM it got as far as making.
    pub fn make(&self, call: &Call, mut on_line: impl FnMut(&str)) -> VmState {
        let _guard = self.calls.lock().unwrap();
        self.cancelled.store(false, Ordering::SeqCst);
        self.apply(Event::CreateStarted);
        let outcome = self.call_with(
            call,
            true,
            |pid| *self.making.lock().unwrap() = Some(pid),
            |line| {
                on_line(line);
                self.apply(Event::CreateLine(line.to_string()));
            },
        );
        *self.making.lock().unwrap() = None;
        if self.cancelled.swap(false, Ordering::SeqCst) && !outcome.ok() {
            let listed = stack::run(stack::limactl_list(&self.paths), |_| {});
            if listed.ok() && parse_list(&listed.stdout, &self.paths.instance) == Listed::Running {
                self.call(&Call::Stop, true, |_| {});
            }
            return self.apply(Event::Cancelled(call.step().to_string()));
        }
        self.apply(Event::CreateExited(failure(call, &outcome), outcome.ok()))
    }

    /// Ends the VM-making call in flight, if there is one.
    pub fn cancel(&self) -> bool {
        let Some(pid) = *self.making.lock().unwrap() else {
            return false;
        };
        self.cancelled.store(true, Ordering::SeqCst);
        stack::cancel(pid);
        true
    }

    /// `destroy`: the VM goes; the folder and the Keychain key stay.
    pub fn remove(&self) -> VmState {
        let _guard = self.calls.lock().unwrap();
        self.apply(Event::RemoveStarted);
        let outcome = self.call(&Call::Destroy, true, |_| {});
        self.apply(Event::RemoveExited(failure(&Call::Destroy, &outcome), outcome.ok()))
    }

    /// A long call that runs beside the others rather than after them: the
    /// background installs, which take minutes and change no VM state.
    pub fn alongside(&self, call: &Call) -> Outcome {
        self.call(call, true, |_| {})
    }

    /// A short call whose output is a value, beside the others: polling the
    /// background installs' status must not wait for a long call to end.
    pub fn output_alongside(&self, call: &Call) -> Result<String, Failure> {
        let outcome = self.call(call, false, |_| {});
        if outcome.ok() {
            Ok(outcome.stdout)
        } else {
            Err(failure(call, &outcome))
        }
    }

    pub fn start(&self) -> VmState {
        let _guard = self.calls.lock().unwrap();
        self.apply(Event::StartStarted);
        let outcome = self.call(&Call::Start, true, |_| {});
        self.apply(Event::StartExited(failure(&Call::Start, &outcome), outcome.ok()))
    }

    pub fn stop(&self) -> VmState {
        let _guard = self.calls.lock().unwrap();
        self.apply(Event::StopStarted);
        let outcome = self.call(&Call::Stop, true, |_| {});
        self.apply(Event::StopExited(failure(&Call::Stop, &outcome), outcome.ok()))
    }

    /// The workspace went away: stop the stack, then say why it stopped.
    pub fn folder_gone(&self, path: &Path) -> VmState {
        if matches!(self.state(), VmState::Running) {
            self.stop();
        }
        self.apply(Event::FolderGone(path.display().to_string()))
    }

    /// A call whose output is the value the app wants (a login code, a list).
    pub fn output(&self, call: &Call) -> Result<String, Failure> {
        let _guard = self.calls.lock().unwrap();
        let outcome = self.call(call, false, |_| {});
        if outcome.ok() {
            Ok(outcome.stdout)
        } else {
            Err(failure(call, &outcome))
        }
    }

    /// A call whose output goes into a file, such as the logs bundle.
    pub fn capture(&self, call: &Call) -> Outcome {
        let _guard = self.calls.lock().unwrap();
        let mut lines = Vec::new();
        let mut outcome = self.call(call, false, |line| lines.push(line.to_string()));
        outcome.tail = lines;
        outcome
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;

    fn fail(step: &str) -> Failure {
        Failure {
            step: step.into(),
            command: format!("istota-stack {step}"),
            exit_code: Some(78),
            message: "refused".into(),
            tail: vec!["last line".into()],
        }
    }

    #[test]
    fn a_create_walks_through_loading_and_setup_to_running() {
        let mut state = next(&VmState::Absent, Event::CreateStarted);
        assert_eq!(state, VmState::Creating);
        state = next(&state, Event::CreateLine("[istota-stack] unpacking the appliance disk of v1".into()));
        assert_eq!(state, VmState::Creating);
        state = next(&state, Event::CreateLine("[istota-stack] loading the images of v1 from /mnt/x".into()));
        assert_eq!(state, VmState::Loading);
        state = next(&state, Event::CreateLine("ISTOTA_SETUP_URL=http://localhost:8080/istota/setup#code=AB".into()));
        assert_eq!(state, VmState::Setup { url: "http://localhost:8080/istota/setup#code=AB".into() });
        state = next(&state, Event::CreateExited(fail("create"), true));
        assert_eq!(state, VmState::Running);
    }

    #[test]
    fn a_setup_address_off_this_mac_is_not_followed() {
        assert_eq!(setup_url("ISTOTA_SETUP_URL=https://evil.example/istota/setup"), None);
        assert_eq!(setup_url("ISTOTA_SETUP_URL=http://localhost:8080/a b"), None);
        assert_eq!(setup_url("echo ISTOTA_SETUP_URL=http://localhost:1/"), None);
        assert_eq!(
            setup_url("ISTOTA_SETUP_URL=http://127.0.0.1:8080/istota/setup#code=X").as_deref(),
            Some("http://127.0.0.1:8080/istota/setup#code=X")
        );
    }

    #[test]
    fn a_failed_step_is_an_error_naming_it_and_never_done() {
        let state = next(&VmState::Creating, Event::CreateExited(fail("create"), false));
        assert_eq!(state, VmState::Error(fail("create")));
        let state = next(&VmState::Starting, Event::StartExited(fail("start"), false));
        assert_eq!(state, VmState::Error(fail("start")));
    }

    #[test]
    fn a_listing_does_not_interrupt_a_call_or_hide_an_error() {
        assert_eq!(next(&VmState::Creating, Event::Listed(Listed::Running)), VmState::Creating);
        assert_eq!(next(&VmState::Stopping, Event::Listed(Listed::Running)), VmState::Stopping);
        let error = VmState::Error(fail("start"));
        assert_eq!(next(&error, Event::Listed(Listed::Stopped)), error);
        assert_eq!(next(&VmState::Unknown, Event::Listed(Listed::None)), VmState::Absent);
        assert_eq!(next(&VmState::Stopped, Event::Listed(Listed::Running)), VmState::Running);
    }

    #[test]
    fn a_removal_ends_absent_or_in_an_error() {
        let state = next(&VmState::Stopped, Event::RemoveStarted);
        assert_eq!(state, VmState::Removing);
        assert_eq!(next(&state, Event::Listed(Listed::Running)), VmState::Removing);
        assert_eq!(next(&state, Event::RemoveExited(fail("remove"), true)), VmState::Absent);
        assert_eq!(next(&state, Event::RemoveExited(fail("remove"), false)), VmState::Error(fail("remove")));
    }

    #[test]
    fn a_cancelled_or_unfinished_make_is_resumable() {
        let VmState::Error(cancelled) = next(&VmState::Loading, Event::Cancelled("restore".into())) else {
            panic!()
        };
        assert!(resumable(&cancelled));
        let VmState::Error(unfinished) = next(&VmState::Unknown, Event::Unfinished) else { panic!() };
        assert!(resumable(&unfinished));
        assert!(!resumable(&fail("start")));
    }

    #[test]
    fn running_and_stopped_go_back_and_forth() {
        let state = next(&VmState::Running, Event::StopStarted);
        let state = next(&state, Event::StopExited(fail("stop"), true));
        assert_eq!(state, VmState::Stopped);
        let state = next(&state, Event::StartStarted);
        assert_eq!(next(&state, Event::StartExited(fail("start"), true)), VmState::Running);
    }

    #[test]
    fn the_listing_finds_the_apps_instance_only() {
        let out = concat!(
            r#"{"name":"other","status":"Running"}"#, "\n",
            r#"{"name":"istota","status":"Stopped","dir":"/x"}"#, "\n",
        );
        assert_eq!(parse_list(out, "istota"), Listed::Stopped);
        assert_eq!(parse_list(out, "istota-d9"), Listed::None);
        assert_eq!(parse_list(r#"{"name":"istota-d9","status":"Running"}"#, "istota-d9"), Listed::Running);
        assert_eq!(parse_list("", "istota"), Listed::None);
        assert_eq!(parse_list(r#"{"name":"istota","status":"Broken"}"#, "istota"), Listed::Other("Broken".into()));
    }

    /// A fake Mac CLI and limactl: each records its argv and the environment
    /// the app gave it, prints what the test scripts, and exits with its code.
    struct Fake {
        dir: PathBuf,
        paths: Paths,
    }

    impl Fake {
        fn new(name: &str) -> Fake {
            let dir = std::env::temp_dir().join(format!("istota-desktop-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            let bin = dir.join("Resources/bin");
            std::fs::create_dir_all(&bin).unwrap();
            let paths = Paths::new(dir.join("state"), dir.join("Resources"));
            let fake = Fake { dir, paths };
            for tool in ["istota-stack", "limactl"] {
                fake.script(tool, "exit 0");
            }
            fake
        }

        /// The tool's script body runs after the recording lines.
        fn script(&self, tool: &str, body: &str) {
            let path = self.paths.bin_dir().join(tool);
            let calls = self.dir.join("calls");
            let text = format!(
                "#!/bin/bash\nprintf '%s %s|LIMA_HOME=%s|XDG_CONFIG_HOME=%s|PATH=%s|XDG_DATA_HOME=%s\\n' {tool} \"$*\" \"$LIMA_HOME\" \"$XDG_CONFIG_HOME\" \"$PATH\" \"${{XDG_DATA_HOME:-unset}}\" >> '{}'\n{body}\n",
                calls.display()
            );
            std::fs::write(&path, text).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }

        fn calls(&self) -> Vec<String> {
            std::fs::read_to_string(self.dir.join("calls"))
                .unwrap_or_default()
                .lines()
                .map(str::to_string)
                .collect()
        }

        fn supervisor(&self) -> (Arc<Supervisor>, Arc<Mutex<Vec<VmState>>>) {
            let seen = Arc::new(Mutex::new(Vec::new()));
            let sink = seen.clone();
            let supervisor = Supervisor::new(
                self.paths.clone(),
                Box::new(move |state| sink.lock().unwrap().push(state.clone())),
                Box::new(|_| {}),
            );
            (supervisor, seen)
        }
    }

    impl Drop for Fake {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    #[test]
    fn create_runs_the_cli_with_the_apps_environment_and_follows_its_output() {
        let fake = Fake::new("create");
        fake.script(
            "istota-stack",
            "echo '[istota-stack] loading the images of v1 from /mnt/istota-release/v1'\necho 'ISTOTA_SETUP_URL=http://localhost:8080/istota/setup#code=K'\nexit 0",
        );
        let (supervisor, seen) = fake.supervisor();
        let mut lines = Vec::new();
        let state = supervisor.create(Path::new("/rel/v1"), Path::new("/Users/a/Istota"), |l| lines.push(l.to_string()));
        assert_eq!(state, VmState::Running);
        assert_eq!(lines.len(), 2);
        assert_eq!(
            *seen.lock().unwrap(),
            vec![
                VmState::Creating,
                VmState::Loading,
                VmState::Setup { url: "http://localhost:8080/istota/setup#code=K".into() },
                VmState::Running,
            ]
        );
        let calls = fake.calls();
        assert_eq!(calls.len(), 1);
        let fields: Vec<&str> = calls[0].split('|').collect();
        assert_eq!(
            fields[0],
            "istota-stack create --appliance /rel/v1 --workspace /Users/a/Istota --web-setup-in-app"
        );
        assert_eq!(fields[1], format!("LIMA_HOME={}", fake.paths.lima_home().display()));
        assert_eq!(fields[2], format!("XDG_CONFIG_HOME={}", fake.paths.cli_config_home().display()));
        assert_eq!(fields[3], format!("PATH={}", fake.paths.search_path()));
        assert_eq!(fields[4], "XDG_DATA_HOME=unset");
    }

    #[test]
    fn a_refused_create_is_an_error_with_the_message_and_last_lines() {
        let fake = Fake::new("refused");
        fake.script("istota-stack", "echo 'the folder is in iCloud' >&2\nexit 78");
        let (supervisor, _) = fake.supervisor();
        let state = supervisor.create(Path::new("/rel/v1"), Path::new("/Users/a/Library/Mobile Documents/x"), |_| {});
        let VmState::Error(failure) = state else { panic!("{state:?}") };
        assert_eq!(failure.step, "create");
        assert_eq!(failure.exit_code, Some(78));
        assert_eq!(failure.message, stack::exit_message(Some(78)));
        assert_eq!(failure.tail, vec!["the folder is in iCloud".to_string()]);
        assert_eq!(failure.command, "istota-stack create --appliance … --workspace … --web-setup-in-app");
    }

    #[test]
    fn launch_adopts_a_running_vm_without_starting_it() {
        let fake = Fake::new("adopt");
        fake.script("limactl", r#"echo '{"name":"istota","status":"Running"}'"#);
        let (supervisor, _) = fake.supervisor();
        assert_eq!(supervisor.adopt_or_start(true), VmState::Running);
        let calls = fake.calls();
        assert_eq!(calls.len(), 1, "{calls:?}");
        assert!(calls[0].starts_with("limactl list --json|"));
    }

    #[test]
    fn launch_starts_a_stopped_vm_and_reports_a_failed_start() {
        let fake = Fake::new("start");
        fake.script("limactl", r#"echo '{"name":"istota","status":"Stopped"}'"#);
        fake.script("istota-stack", "echo 'port 8080 is held by something else'\nexit 78");
        let (supervisor, seen) = fake.supervisor();
        let state = supervisor.adopt_or_start(true);
        assert!(matches!(state, VmState::Error(ref f) if f.step == "start"), "{state:?}");
        assert_eq!(seen.lock().unwrap()[..2], [VmState::Stopped, VmState::Starting]);
        assert!(fake.calls()[1].starts_with("istota-stack start|"));
    }

    #[test]
    fn launch_with_no_vm_is_absent_and_calls_nothing_else() {
        let fake = Fake::new("absent");
        let (supervisor, _) = fake.supervisor();
        assert_eq!(supervisor.adopt_or_start(true), VmState::Absent);
        assert_eq!(fake.calls().len(), 1);
    }

    #[test]
    fn launch_leaves_a_vm_whose_first_run_never_finished_for_resume() {
        let fake = Fake::new("unfinished");
        fake.script("limactl", r#"echo '{"name":"istota","status":"Stopped"}'"#);
        let (supervisor, _) = fake.supervisor();
        let state = supervisor.adopt_or_start(false);
        assert!(matches!(state, VmState::Error(ref f) if resumable(f)), "{state:?}");
        assert_eq!(fake.calls().len(), 1, "nothing but the listing: {:?}", fake.calls());
    }

    #[test]
    fn a_restore_answers_the_name_prompt_and_walks_to_running() {
        let fake = Fake::new("restore");
        fake.script(
            "istota-stack",
            "read -r name\necho \"[istota-stack] deleting $name\"\necho '[istota-stack] loading the images of v1 from /mnt/x'\nexit 0",
        );
        let (supervisor, seen) = fake.supervisor();
        let call = Call::Restore { appliance: Path::new("/rel/v1"), workspace: Path::new("/Users/a/Istota"), date: None };
        let mut lines = Vec::new();
        assert_eq!(supervisor.make(&call, |l| lines.push(l.to_string())), VmState::Running);
        assert_eq!(lines[0], "[istota-stack] deleting istota");
        assert_eq!(*seen.lock().unwrap(), vec![VmState::Creating, VmState::Loading, VmState::Running]);
        assert!(fake.calls()[0].starts_with("istota-stack restore --appliance /rel/v1 --workspace /Users/a/Istota|"));
    }

    #[test]
    fn a_cancel_ends_the_make_and_stops_the_vm_it_made() {
        let fake = Fake::new("cancel");
        fake.script(
            "istota-stack",
            "case \"$1\" in create) echo '[istota-stack] unpacking the appliance disk of v1'; sleep 30 ;; esac\nexit 0",
        );
        fake.script("limactl", r#"echo '{"name":"istota","status":"Running"}'"#);
        let (supervisor, _) = fake.supervisor();
        let worker = supervisor.clone();
        let started = std::time::Instant::now();
        let handle = std::thread::spawn(move || worker.create(Path::new("/rel/v1"), Path::new("/Users/a/Istota"), |_| {}));
        // Once the fake has recorded its call, so the record is not raced.
        while fake.calls().is_empty() || !supervisor.cancel() {
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        let state = handle.join().unwrap();
        assert!(started.elapsed() < std::time::Duration::from_secs(10));
        assert!(matches!(state, VmState::Error(ref f) if resumable(f) && f.message.starts_with("Cancelled")), "{state:?}");
        let calls = fake.calls();
        assert!(calls[0].starts_with("istota-stack create "), "{calls:?}");
        assert!(calls[1].starts_with("limactl list --json|"), "{calls:?}");
        assert!(calls[2].starts_with("istota-stack stop|"), "{calls:?}");
        assert!(!supervisor.cancel(), "nothing left to cancel");
    }

    #[test]
    fn remove_answers_the_name_prompt_and_leaves_no_vm() {
        let fake = Fake::new("remove");
        fake.script("istota-stack", "read -r name\n[ \"$name\" = istota ] || exit 1\nexit 0");
        let (supervisor, _) = fake.supervisor();
        assert_eq!(supervisor.remove(), VmState::Absent);
        assert!(fake.calls()[0].starts_with("istota-stack destroy|"));
    }

    #[test]
    fn a_missing_limactl_is_an_error_not_absent() {
        let fake = Fake::new("nolimactl");
        std::fs::remove_file(fake.paths.limactl()).unwrap();
        let (supervisor, _) = fake.supervisor();
        let state = supervisor.refresh();
        assert!(matches!(state, VmState::Error(ref f) if f.exit_code.is_none()), "{state:?}");
    }

    #[test]
    fn a_gone_folder_stops_the_stack_first() {
        let fake = Fake::new("gone");
        fake.script("limactl", r#"echo '{"name":"istota","status":"Running"}'"#);
        let (supervisor, _) = fake.supervisor();
        supervisor.refresh();
        let state = supervisor.folder_gone(Path::new("/Volumes/x/Istota"));
        assert!(matches!(state, VmState::Error(ref f) if f.step == "folder"), "{state:?}");
        assert!(fake.calls()[1].starts_with("istota-stack stop|"));
    }
}
