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

use std::io::{BufRead, BufReader, Read};
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
    Verb::vm("exec", &[]),
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

/// One call the app makes.
#[derive(Clone, Debug)]
pub enum Call<'a> {
    Create { appliance: &'a Path, workspace: &'a Path },
    Start,
    Stop,
    Status,
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
            Call::Istota(_) => "istota",
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
            skip_value = arg == "--appliance" || arg == "--workspace";
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
pub fn run(mut command: Command, mut on_line: impl FnMut(&str)) -> Outcome {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(err) => {
            let line = format!("could not run {}: {err}", command.get_program().to_string_lossy());
            on_line(&line);
            return Outcome { code: None, stdout: String::new(), tail: vec![line] };
        }
    };
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
