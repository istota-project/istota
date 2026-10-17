//! The app's contract with the stack: which Mac CLI verbs it runs, with which
//! options, in which environment, and what their exit codes mean.
//!
//! The app is a front end to `host/mac/istota-stack`; it re-implements none of
//! its jobs. Every call goes through `command`, which builds the argv from a
//! `Call` and the environment from `Paths`, and the verbs and options a `Call`
//! can produce are the ones in `VERBS`. `tests/test_desktop_contract.py` reads
//! `VERBS`, `ISTOTA_COMMANDS` and `EXIT_MESSAGES` out of this file and holds
//! them to the usage text of `host/mac/istota-stack` and `host/istota-stack`
//! and to `istota`'s own parser, so a renamed verb fails the default suite
//! rather than the app on a user's Mac.

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::mpsc;

use crate::paths::{Paths, INSTANCE};

/// Which script owns a verb: the Mac CLI itself, or the VM's `istota-stack`,
/// which the Mac CLI forwards every verb it does not handle to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Script {
    Mac,
    Vm,
}

pub struct Verb {
    pub script: Script,
    pub name: &'static str,
    pub options: &'static [&'static str],
}

impl Verb {
    const fn mac(name: &'static str, options: &'static [&'static str]) -> Verb {
        Verb { script: Script::Mac, name, options }
    }

    const fn vm(name: &'static str, options: &'static [&'static str]) -> Verb {
        Verb { script: Script::Vm, name, options }
    }
}

// --- contract ---
pub const VERBS: &[Verb] = &[
    Verb::mac("create", &["--appliance", "--workspace", "--web-setup-in-app"]),
    Verb::mac("start", &[]),
    Verb::mac("stop", &[]),
    Verb::mac("status", &[]),
    Verb::mac("restore", &["--date", "--appliance", "--workspace"]),
    Verb::mac("import-standalone", &["--appliance", "--workspace"]),
    Verb::mac("destroy", &[]),
    Verb::mac("allow-host-port", &[]),
    Verb::mac("deny-host-port", &[]),
    Verb::vm("exec", &[]),
    Verb::vm("local-layers", &["--status"]),
    Verb::vm("host-env", &[]),
];

/// The `istota` commands the app runs through `exec`, with their options.
pub const ISTOTA_COMMANDS: &[&[&str]] = &[
    &["web", "login-code"],
    &["auth", "list"],
    &["auth", "logout-all"],
    &["list", "--status"],
];

/// The Mac CLI's documented exit codes, and the VM's refused-tag code (65 too).
pub const EXIT_MESSAGES: &[(i32, &str)] = &[
    (64, "The app called the command line with arguments it does not take."),
    (65, "The release did not verify against the release key."),
    (69, "A tool the VM needs is missing from the app (limactl or zstd)."),
    (78, "The command line refused this Mac's folder, port, machine or Keychain."),
];
// --- end contract ---

/// The `host.env` keys the app reads or sets through `host-env`.
pub const HOST_LOOPBACK_ALLOW: &str = "HOST_LOOPBACK_ALLOW";
pub const LOCAL_LAYERS: &str = "ISTOTA_LOCAL_LAYERS";

/// One call the app makes.
#[derive(Clone, Debug)]
pub enum Call<'a> {
    Create { appliance: &'a Path, workspace: &'a Path },
    Start,
    Stop,
    Status,
    /// A new VM from the workspace's backups and the Keychain's key; the
    /// app's own VM, if there is one, is deleted first.
    Restore { appliance: &'a Path, workspace: &'a Path, date: Option<&'a str> },
    /// A standalone install (`~/.istota`) moved into a new VM.
    ImportStandalone { appliance: &'a Path, workspace: &'a Path },
    Destroy,
    AllowHostPort(u16),
    DenyHostPort(u16),
    /// Build what the release leaves out (Claude Code, Chrome); long.
    LocalLayers,
    LocalLayersStatus,
    HostEnvGet(&'a str),
    HostEnvSet(&'a str, &'a str),
    /// An `istota` command in the daemon container, through the VM's `exec`.
    Istota(&'a [&'a str]),
}

impl Call<'_> {
    pub fn argv(&self) -> Vec<String> {
        let argv = self.build();
        debug_assert!(within_table(&argv), "{argv:?} is not in VERBS");
        argv
    }

    fn build(&self) -> Vec<String> {
        match self {
            Call::Create { appliance, workspace } => vec![
                "create".into(),
                "--appliance".into(),
                appliance.display().to_string(),
                "--workspace".into(),
                workspace.display().to_string(),
                "--web-setup-in-app".into(),
            ],
            Call::Start => vec!["start".into()],
            Call::Stop => vec!["stop".into()],
            Call::Status => vec!["status".into()],
            Call::Restore { appliance, workspace, date } => {
                let mut argv = vec!["restore".to_string()];
                if let Some(date) = date {
                    argv.extend(["--date".to_string(), date.to_string()]);
                }
                argv.extend([
                    "--appliance".to_string(),
                    appliance.display().to_string(),
                    "--workspace".to_string(),
                    workspace.display().to_string(),
                ]);
                argv
            }
            Call::ImportStandalone { appliance, workspace } => vec![
                "import-standalone".into(),
                "--appliance".into(),
                appliance.display().to_string(),
                "--workspace".into(),
                workspace.display().to_string(),
            ],
            Call::Destroy => vec!["destroy".into()],
            Call::AllowHostPort(port) => vec!["allow-host-port".into(), port.to_string()],
            Call::DenyHostPort(port) => vec!["deny-host-port".into(), port.to_string()],
            Call::LocalLayers => vec!["local-layers".into()],
            Call::LocalLayersStatus => vec!["local-layers".into(), "--status".into()],
            Call::HostEnvGet(key) => vec!["host-env".into(), key.to_string()],
            Call::HostEnvSet(key, value) => vec!["host-env".into(), format!("{key}={value}")],
            Call::Istota(args) => {
                debug_assert!(istota_listed(args), "{args:?} is not in ISTOTA_COMMANDS");
                let mut argv = vec!["exec".to_string(), "istota".to_string()];
                argv.extend(args.iter().map(|s| s.to_string()));
                argv
            }
        }
    }

    /// The step a failure names.
    pub fn step(&self) -> &'static str {
        match self {
            Call::Create { .. } => "create",
            Call::Start => "start",
            Call::Stop => "stop",
            Call::Status => "status",
            Call::Restore { .. } => "restore",
            Call::ImportStandalone { .. } => "import",
            Call::Destroy => "remove",
            Call::AllowHostPort(_) | Call::DenyHostPort(_) => "host port",
            Call::LocalLayers | Call::LocalLayersStatus => "background installs",
            Call::HostEnvGet(_) | Call::HostEnvSet(..) => "host.env",
            Call::Istota(_) => "istota",
        }
    }

    /// What the call reads on stdin. `destroy`, and `restore` over an
    /// existing VM, ask for the instance's name before deleting it; the app
    /// asked its own confirmation first and answers for the user.
    pub fn stdin(&self) -> Option<String> {
        match self {
            Call::Destroy | Call::Restore { .. } => Some(format!("{INSTANCE}\n")),
            _ => None,
        }
    }

    /// How a failure shows the command: the verb and its options, never its values.
    pub fn display(&self) -> String {
        let argv = self.argv();
        let mut shown = vec!["istota-stack".to_string()];
        let mut skip_value = false;
        for arg in argv {
            if skip_value {
                shown.push("…".into());
                skip_value = false;
                continue;
            }
            skip_value = arg == "--appliance" || arg == "--workspace" || arg == "--date";
            shown.push(arg);
        }
        shown.join(" ")
    }
}

/// Whether an argv's verb is in `VERBS` and, for a verb the Mac CLI handles,
/// each of its options is too. A forwarded verb's arguments are the VM's.
pub fn within_table(argv: &[String]) -> bool {
    let Some(verb) = argv.first().and_then(|name| VERBS.iter().find(|v| v.name == name)) else {
        return false;
    };
    verb.script == Script::Vm
        || argv
            .iter()
            .filter(|arg| arg.starts_with("--"))
            .all(|arg| verb.options.contains(&arg.as_str()))
}

/// Whether an `istota` argv starts with a command `ISTOTA_COMMANDS` lists;
/// what follows it are values (a user id, a status).
pub fn istota_listed(args: &[&str]) -> bool {
    ISTOTA_COMMANDS
        .iter()
        .any(|listed| args.len() >= listed.len() && &args[..listed.len()] == *listed)
}

/// The environment every Mac CLI call runs in, and nothing else of the app's:
/// a user's own `LIMA_HOME` or `XDG_DATA_HOME` must not reach it.
pub fn environment(paths: &Paths) -> Vec<(String, String)> {
    let mut env = vec![
        ("PATH".to_string(), paths.search_path()),
        ("LIMA_HOME".to_string(), paths.lima_home().display().to_string()),
        ("XDG_CONFIG_HOME".to_string(), paths.cli_config_home().display().to_string()),
        ("ISTOTA_LIMA_INSTANCE".to_string(), INSTANCE.to_string()),
        ("ISTOTA_MAC_BIN_DIR".to_string(), paths.cli_bin_dir().display().to_string()),
    ];
    for key in ["HOME", "USER", "LOGNAME", "TMPDIR", "LANG"] {
        if let Some(value) = std::env::var_os(key) {
            env.push((key.to_string(), value.to_string_lossy().into_owned()));
        }
    }
    env
}

fn base_command(program: &Path, paths: &Paths) -> Command {
    let mut command = Command::new(program);
    command.env_clear();
    command.envs(environment(paths));
    command.stdin(Stdio::null());
    command
}

pub fn command(paths: &Paths, call: &Call) -> Command {
    let mut command = base_command(&paths.mac_cli(), paths);
    command.args(call.argv());
    command
}

/// `PATH` for a command-line install's own `istota-stack`, which finds the
/// `limactl` it was installed with (Homebrew's).
const CLI_INSTALL_PATH: &str = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";

/// `istota-stack stop` for the command-line install, in the default
/// `LIMA_HOME` and config directory: the user's own `~/.local/bin/istota-stack`
/// when it is there, else the bundled one with the bundled `limactl`.
pub fn cli_install_stop(paths: &Paths, home: &Path) -> Command {
    let own = home.join(".local/bin/istota-stack");
    let (program, search_path) = if own.is_file() {
        (own, CLI_INSTALL_PATH.to_string())
    } else {
        (paths.mac_cli(), paths.search_path())
    };
    let mut command = Command::new(program);
    command.env_clear();
    command.envs(cli_install_environment(home, &search_path));
    command.stdin(Stdio::null());
    command.arg("stop");
    command
}

/// `limactl list --json` over the command-line install's `~/.lima`, read only.
pub fn cli_install_list(paths: &Paths, home: &Path) -> Command {
    let mut command = Command::new(paths.limactl());
    command.env_clear();
    command.envs(cli_install_environment(home, &paths.search_path()));
    command.stdin(Stdio::null());
    command.args(["list", "--json"]);
    command
}

fn cli_install_environment(home: &Path, search_path: &str) -> Vec<(String, String)> {
    let mut env = vec![
        ("PATH".to_string(), search_path.to_string()),
        ("HOME".to_string(), home.display().to_string()),
        ("LIMA_HOME".to_string(), home.join(".lima").display().to_string()),
        ("ISTOTA_LIMA_INSTANCE".to_string(), INSTANCE.to_string()),
    ];
    for key in ["USER", "LOGNAME", "TMPDIR", "LANG"] {
        if let Some(value) = std::env::var_os(key) {
            env.push((key.to_string(), value.to_string_lossy().into_owned()));
        }
    }
    env
}

/// `limactl list --json`, the one `limactl` call the app makes itself.
pub fn limactl_list(paths: &Paths) -> Command {
    let mut command = base_command(&paths.limactl(), paths);
    command.args(["list", "--json"]);
    command
}

/// What a finished call left: its exit status, stdout alone (for calls whose
/// output is a value), and the last lines of everything it printed.
#[derive(Clone, Debug, Default)]
pub struct Outcome {
    pub code: Option<i32>,
    pub stdout: String,
    pub tail: Vec<String>,
}

impl Outcome {
    pub fn ok(&self) -> bool {
        self.code == Some(0)
    }
}

pub const TAIL_LINES: usize = 20;

/// Runs a command, handing each line of stdout and stderr to `on_line` as it
/// arrives. A command that cannot start is an outcome with no code.
pub fn run(command: Command, on_line: impl FnMut(&str)) -> Outcome {
    run_with(command, None, |_| {}, on_line)
}

/// `run`, with `stdin` written to the command and closed, and the child's
/// process id handed to `on_spawn`. The child leads a process group of its
/// own, so a cancel reaches everything it started (`cancel`).
pub fn run_with(
    mut command: Command,
    stdin: Option<String>,
    on_spawn: impl FnOnce(u32),
    mut on_line: impl FnMut(&str),
) -> Outcome {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    command.process_group(0);
    if stdin.is_some() {
        command.stdin(Stdio::piped());
    }
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(err) => {
            let line = format!("could not run {}: {err}", command.get_program().to_string_lossy());
            on_line(&line);
            return Outcome { code: None, stdout: String::new(), tail: vec![line] };
        }
    };
    on_spawn(child.id());
    if let (Some(text), Some(mut pipe)) = (stdin, child.stdin.take()) {
        // A short answer fits the pipe's buffer; the pipe closes when dropped.
        let _ = pipe.write_all(text.as_bytes());
    }
    let (sender, receiver) = mpsc::channel::<(bool, String)>();
    let readers: Vec<_> = [
        (true, child.stdout.take().map(|s| Box::new(s) as Box<dyn Read + Send>)),
        (false, child.stderr.take().map(|s| Box::new(s) as Box<dyn Read + Send>)),
    ]
    .into_iter()
    .filter_map(|(is_stdout, stream)| stream.map(|s| (is_stdout, s)))
    .map(|(is_stdout, stream)| {
        let sender = sender.clone();
        std::thread::spawn(move || {
            for line in BufReader::new(stream).lines().map_while(Result::ok) {
                if sender.send((is_stdout, line)).is_err() {
                    break;
                }
            }
        })
    })
    .collect();
    drop(sender);

    let mut stdout = String::new();
    let mut tail: Vec<String> = Vec::new();
    for (is_stdout, line) in receiver {
        on_line(&line);
        if is_stdout {
            stdout.push_str(&line);
            stdout.push('\n');
        }
        tail.push(line);
        if tail.len() > TAIL_LINES {
            tail.remove(0);
        }
    }
    for reader in readers {
        let _ = reader.join();
    }
    let code = child.wait().ok().and_then(|status| status.code());
    Outcome { code, stdout, tail }
}

/// Ends a call started by `run_with`: SIGTERM to its process group, which
/// the Mac CLI's own traps turn into a clean stop of what it was doing.
pub fn cancel(pid: u32) {
    let _ = Command::new("/bin/kill")
        .args(["-TERM", "--", &format!("-{pid}")])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

/// The fixed message for a documented exit code; any other code shows the output.
pub fn exit_message(code: Option<i32>) -> String {
    match code {
        None => "The command could not be run.".to_string(),
        Some(code) => EXIT_MESSAGES
            .iter()
            .find(|(known, _)| *known == code)
            .map(|(_, message)| message.to_string())
            .unwrap_or_else(|| format!("The command exited with status {code}.")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every argv a `Call` makes uses a verb in `VERBS`, and each of its
    /// options is listed there, so the contract test sees all of them.
    #[test]
    fn every_call_stays_inside_the_table() {
        let appliance = Path::new("/r/v1.0.0");
        let workspace = Path::new("/Users/a/Istota");
        let calls = [
            Call::Create { appliance, workspace },
            Call::Start,
            Call::Stop,
            Call::Status,
            Call::Restore { appliance, workspace, date: Some("2026-10-01") },
            Call::Restore { appliance, workspace, date: None },
            Call::ImportStandalone { appliance, workspace },
            Call::Destroy,
            Call::AllowHostPort(11434),
            Call::DenyHostPort(1234),
            Call::LocalLayers,
            Call::LocalLayersStatus,
            Call::HostEnvGet(HOST_LOOPBACK_ALLOW),
            Call::HostEnvSet(LOCAL_LAYERS, "chrome"),
            Call::Istota(&["web", "login-code"]),
        ];
        for call in calls {
            assert!(within_table(&call.build()), "{call:?}");
        }
        let unlisted = |argv: &[&str]| within_table(&argv.iter().map(|s| s.to_string()).collect::<Vec<_>>());
        assert!(!unlisted(&["create", "--repo", "/x"]));
        assert!(!unlisted(&["destroy", "--purge"]));
        assert!(unlisted(&["exec", "istota", "list", "--status", "running"]));
    }

    #[test]
    fn an_istota_command_is_listed_by_its_words_not_its_values() {
        assert!(istota_listed(&["auth", "logout-all", "alice"]));
        assert!(istota_listed(&["list", "--status", "running"]));
        assert!(!istota_listed(&["auth", "remove", "alice"]));
        assert!(!istota_listed(&["web"]));
    }

    #[test]
    fn a_failure_shows_the_verb_and_options_but_not_the_paths() {
        let call = Call::Create {
            appliance: Path::new("/Users/a/Library/Application Support/Istota/release/v1.0.0"),
            workspace: Path::new("/Users/a/Istota"),
        };
        assert_eq!(
            call.display(),
            "istota-stack create --appliance … --workspace … --web-setup-in-app"
        );
    }

    #[test]
    fn a_restore_hides_its_date_and_paths_and_answers_the_name_prompt() {
        let call = Call::Restore {
            appliance: Path::new("/s/release/v1.0.0"),
            workspace: Path::new("/Users/a/Istota"),
            date: Some("2026-10-01"),
        };
        assert_eq!(
            call.argv(),
            ["restore", "--date", "2026-10-01", "--appliance", "/s/release/v1.0.0", "--workspace", "/Users/a/Istota"]
        );
        assert_eq!(call.display(), "istota-stack restore --date … --appliance … --workspace …");
        assert_eq!(call.stdin().as_deref(), Some("istota\n"));
        assert_eq!(Call::Destroy.stdin().as_deref(), Some("istota\n"));
        assert_eq!(Call::Start.stdin(), None);
        assert_eq!(Call::HostEnvSet(LOCAL_LAYERS, "").argv(), ["host-env", "ISTOTA_LOCAL_LAYERS="]);
    }

    #[test]
    fn stdin_reaches_the_command_and_is_closed() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "read -r a; read -r b || echo \"got $a, then end\""]);
        let mut pid = 0;
        let outcome = run_with(command, Some("istota\n".into()), |p| pid = p, |_| {});
        assert!(outcome.ok(), "{outcome:?}");
        assert_eq!(outcome.stdout, "got istota, then end\n");
        assert!(pid > 0);
    }

    #[test]
    fn a_cancel_ends_the_whole_process_group() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "sleep 30 & wait"]);
        let started = std::time::Instant::now();
        let outcome = run_with(
            command,
            None,
            |pid| {
                std::thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_millis(300));
                    cancel(pid);
                });
            },
            |_| {},
        );
        assert!(!outcome.ok());
        assert!(started.elapsed() < std::time::Duration::from_secs(10));
    }

    #[test]
    fn the_command_line_install_is_stopped_with_its_own_cli_when_there_is_one() {
        let home = std::env::temp_dir().join(format!("istota-cli-install-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&home);
        std::fs::create_dir_all(home.join(".local/bin")).unwrap();
        let paths = Paths::new("/s".into(), "/A/Contents/Resources".into());
        let env_of = |command: &Command, key: &str| {
            command
                .get_envs()
                .find(|(k, _)| *k == key)
                .and_then(|(_, v)| v.map(|v| v.to_string_lossy().into_owned()))
        };
        let bundled = cli_install_stop(&paths, &home);
        assert_eq!(bundled.get_program(), "/A/Contents/Resources/bin/istota-stack");
        assert_eq!(env_of(&bundled, "LIMA_HOME"), Some(home.join(".lima").display().to_string()));
        assert_eq!(env_of(&bundled, "XDG_CONFIG_HOME"), None);
        std::fs::write(home.join(".local/bin/istota-stack"), "#!/bin/sh\n").unwrap();
        let own = cli_install_stop(&paths, &home);
        assert_eq!(own.get_program(), home.join(".local/bin/istota-stack").as_os_str());
        assert!(env_of(&own, "PATH").unwrap().starts_with("/opt/homebrew/bin:"));
        assert_eq!(own.get_args().collect::<Vec<_>>(), ["stop"]);
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn documented_exit_codes_have_fixed_messages() {
        assert!(exit_message(Some(78)).contains("refused"));
        assert!(exit_message(Some(69)).contains("limactl"));
        assert_eq!(exit_message(Some(3)), "The command exited with status 3.");
        assert_eq!(exit_message(None), "The command could not be run.");
    }

    #[test]
    fn the_environment_is_the_apps_and_nothing_inherited() {
        let paths = Paths::new("/s".into(), "/A/Contents/Resources".into());
        let env = environment(&paths);
        let get = |k: &str| env.iter().find(|(key, _)| key == k).map(|(_, v)| v.clone());
        assert_eq!(get("LIMA_HOME").as_deref(), Some("/s/lima"));
        assert_eq!(get("XDG_CONFIG_HOME").as_deref(), Some("/s/config"));
        assert_eq!(get("ISTOTA_LIMA_INSTANCE").as_deref(), Some("istota"));
        assert_eq!(get("ISTOTA_MAC_BIN_DIR").as_deref(), Some("/s/bin"));
        assert!(get("PATH").unwrap().starts_with("/A/Contents/Resources/bin:"));
        assert_eq!(get("XDG_DATA_HOME"), None);
    }
}
