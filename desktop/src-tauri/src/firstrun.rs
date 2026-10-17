//! The first run on this Mac (the Mac app spec, "First run"): what the Mac
//! must have, what the app offers to set up, the release it makes the VM
//! from, and the progress screen's steps.
//!
//! The setup itself is the Mac CLI's: `create`, or `restore` to move a
//! command-line install in, or `import-standalone` for a standalone install
//! left behind. Its refusals (a folder in iCloud, a taken port) are shown as
//! the CLI words them.

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::logs::redact_line;
use crate::paths::{env_file_value, Paths, CLI_INSTALL_INSTANCE};
use crate::stack::{self, TAIL_LINES};
use crate::vm;

pub const MIN_MACOS_MAJOR: u64 = 14;
pub const MIN_FREE_BYTES: u64 = 25 * 1024 * 1024 * 1024;

/// What the Mac must have before anything is unpacked; each refusal names
/// the requirement it misses.
pub fn refusals(arch: &str, macos_major: u64, free_bytes: Option<u64>) -> Vec<String> {
    let mut out = Vec::new();
    if arch != "aarch64" {
        out.push("Istota needs a Mac with Apple silicon (M1 or later).".to_string());
    }
    if macos_major < MIN_MACOS_MAJOR {
        out.push(format!("Istota needs macOS {MIN_MACOS_MAJOR} or later; this Mac runs macOS {macos_major}."));
    }
    match free_bytes {
        Some(free) if free < MIN_FREE_BYTES => out.push(format!(
            "Istota needs 25 GB free on the startup disk; {} GB is free.",
            free / (1024 * 1024 * 1024)
        )),
        Some(_) => {}
        None => out.push("Could not read how much space is free on the startup disk.".to_string()),
    }
    out
}

/// Free bytes for an unprivileged user on the volume holding `path`, or its
/// nearest existing parent.
pub fn free_bytes(path: &Path) -> Option<u64> {
    let mut at = path;
    while !at.exists() {
        at = at.parent()?;
    }
    let c_path = std::ffi::CString::new(at.as_os_str().as_encoded_bytes()).ok()?;
    let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: a NUL-terminated path and a zeroed struct statvfs fills.
    if unsafe { libc::statvfs(c_path.as_ptr(), &mut stat) } != 0 {
        return None;
    }
    Some(stat.f_bavail as u64 * stat.f_frsize)
}

/// What the first screen offers besides a new install.
#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct Offers {
    /// `~/.lima/istota`: the command-line install, offered as a move.
    pub cli_install: bool,
    /// `~/.istota`: a standalone install left behind, offered as an import.
    pub standalone: bool,
    /// The command-line install's workspace, which a move restores from.
    pub cli_workspace: Option<String>,
}

pub fn offers(home: &Path) -> Offers {
    let cli_install = home.join(".lima").join(CLI_INSTALL_INSTANCE).is_dir();
    Offers {
        cli_install,
        standalone: !cli_install && home.join(".istota").is_dir(),
        cli_workspace: cli_install.then(|| cli_install_workspace(home).display().to_string()),
    }
}

/// The folder the command-line install's state file names, as its
/// `workspace_or_default` reads it.
pub fn cli_install_workspace(home: &Path) -> PathBuf {
    std::fs::read_to_string(home.join(".config/istota-stack").join(format!("{CLI_INSTALL_INSTANCE}.env")))
        .ok()
        .and_then(|text| env_file_value(&text, "WORKSPACE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join("Istota"))
}

/// Whether the app must not start its VM: the command-line install's VM is
/// running on the same folder, and two daemons on one workspace would
/// corrupt each other's view of it.
pub fn shares_a_running_workspace(cli_workspace: &Path, ours: &Path, cli_running: bool) -> bool {
    cli_running && cli_workspace == ours
}

/// A move's first step: the command-line install's VM stopped, if it runs.
/// It is listed and stopped in `~/.lima`, and nothing else is done to it.
pub fn stop_cli_install(paths: &Paths, home: &Path, log: &dyn Fn(&str)) -> Result<(), String> {
    let listed = stack::run(stack::cli_install_list(paths, home), |_| {});
    if !(listed.ok() && vm::parse_list(&listed.stdout, CLI_INSTALL_INSTANCE) == vm::Listed::Running) {
        return Ok(());
    }
    log("$ istota-stack stop (the command-line install)");
    let outcome = stack::run(stack::cli_install_stop(paths, home), log);
    log(&format!("exit {:?}", outcome.code));
    if outcome.ok() {
        Ok(())
    } else {
        Err(format!(
            "Could not stop your command-line Istota (exit {:?}): {}",
            outcome.code,
            outcome.tail.join(" ")
        ))
    }
}

pub const SHARED_WORKSPACE: &str = "Your command-line Istota is running on the same folder. Stop it first (`istota-stack stop` in Terminal): two copies of Istota on one folder corrupt each other's view of it.";

/// The release bundled in the app, `Contents/Resources/release/<tag>/`: its
/// tag and directory, if there is one with an appliance disk.
pub fn bundled_release(resources: &Path) -> Option<(String, PathBuf)> {
    let mut found: Vec<(String, PathBuf)> = std::fs::read_dir(resources.join("release"))
        .ok()?
        .filter_map(Result::ok)
        .map(|entry| (entry.file_name().to_string_lossy().into_owned(), entry.path()))
        .filter(|(tag, dir)| dir.join(format!("istota-appliance-{tag}.raw.zst")).is_file())
        .collect();
    found.sort();
    found.pop()
}

/// The release copied out of the app into `<state>/release/<tag>/`, which
/// the VM mounts read-only, so the VM does not depend on where the app is.
/// APFS clones the files, so the copy costs no space. A copy cut short is
/// in `.<tag>.partial` and never named as the release.
pub fn stage_release(tag: &str, source: &Path, release_root: &Path) -> std::io::Result<PathBuf> {
    let target = release_root.join(tag);
    if target.is_dir() && same_files(source, &target)? {
        return Ok(target);
    }
    std::fs::create_dir_all(release_root)?;
    let partial = release_root.join(format!(".{tag}.partial"));
    let _ = std::fs::remove_dir_all(&partial);
    std::fs::create_dir(&partial)?;
    for entry in std::fs::read_dir(source)? {
        let entry = entry?;
        if entry.file_type()?.is_file() {
            let copy = partial.join(entry.file_name());
            std::fs::copy(entry.path(), &copy)?;
            // same_files compares a large file's mtime, so the copy carries the source's.
            let modified = entry.metadata()?.modified()?;
            std::fs::File::options().write(true).open(&copy)?.set_modified(modified)?;
        }
    }
    if target.exists() {
        std::fs::remove_dir_all(&target)?;
    }
    std::fs::rename(&partial, &target)?;
    Ok(target)
}

/// Files up to this size (the manifest, its signature, the bundle's
/// metadata) are compared byte for byte: a rebuilt release with the same tag
/// can keep their sizes.
const COMPARED_BY_CONTENT: u64 = 1 << 20;

/// Whether every file in `a` is in `b` unchanged: the small ones by content,
/// the disk and the image archives by size and modification time.
fn same_files(a: &Path, b: &Path) -> std::io::Result<bool> {
    for entry in std::fs::read_dir(a)? {
        let entry = entry?;
        if !entry.file_type()?.is_file() {
            continue;
        }
        let copy_path = b.join(entry.file_name());
        let Ok(copy) = std::fs::metadata(&copy_path) else {
            return Ok(false);
        };
        let source = entry.metadata()?;
        if copy.len() != source.len() {
            return Ok(false);
        }
        if source.len() <= COMPARED_BY_CONTENT {
            if std::fs::read(entry.path())? != std::fs::read(&copy_path)? {
                return Ok(false);
            }
        } else if copy.modified()? != source.modified()? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Which job the progress screen follows: one of the first run's three, or
/// a restore from Settings.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Job {
    Create,
    Move,
    Import,
    Restore,
}

/// A restore's `--date`, as the Mac CLI takes it: `YYYY-MM-DD`.
pub fn valid_date(date: &str) -> bool {
    let bytes = date.as_bytes();
    bytes.len() == 10
        && bytes.iter().enumerate().all(|(i, b)| if i == 4 || i == 7 { *b == b'-' } else { b.is_ascii_digit() })
}

/// The progress screen's steps, and the line of the CLI's output (or
/// Lima's, which it passes through) that starts each.
pub const STEPS: &[(&str, &str)] = &[
    ("Unpacking the disk", "unpacking the appliance disk"),
    ("Booting", "Starting the instance"),
    ("Provisioning, offline", "Waiting for the final requirement"),
    ("Loading the images", "loading the images of "),
    ("Finishing", "loaded, and match the signed manifest"),
];

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Step {
    pub name: &'static str,
    /// Milliseconds since the epoch.
    pub started_ms: Option<u64>,
    pub finished_ms: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Progress {
    pub job: Job,
    pub started_ms: u64,
    pub steps: Vec<Step>,
    /// The last lines printed, one-use addresses cut (`logs::redact_line`).
    pub lines: Vec<String>,
}

pub fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

impl Progress {
    pub fn new(job: Job, now: u64) -> Progress {
        Progress {
            job,
            started_ms: now,
            steps: STEPS.iter().map(|(name, _)| Step { name, started_ms: None, finished_ms: None }).collect(),
            lines: Vec::new(),
        }
    }

    /// Takes one line of output. A step's line starts it and finishes every
    /// step before it; a step never goes back, and one the run skipped (a
    /// resumed create that finds the disk unpacked) is left without times.
    pub fn advance(&mut self, line: &str, now: u64) {
        self.lines.push(redact_line(line));
        if self.lines.len() > TAIL_LINES {
            self.lines.remove(0);
        }
        let Some(index) = STEPS.iter().position(|(_, marker)| line.contains(marker)) else {
            return;
        };
        let current = self.steps.iter().rposition(|s| s.started_ms.is_some());
        if current.is_some_and(|c| index <= c) {
            return;
        }
        for step in &mut self.steps[..index] {
            if step.started_ms.is_some() && step.finished_ms.is_none() {
                step.finished_ms = Some(now);
            }
        }
        self.steps[index].started_ms = Some(now);
    }

    /// The run ended: the step in flight is finished when it succeeded.
    pub fn end(&mut self, ok: bool, now: u64) {
        if !ok {
            return;
        }
        for step in &mut self.steps {
            if step.started_ms.is_some() && step.finished_ms.is_none() {
                step.finished_ms = Some(now);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const GIB: u64 = 1024 * 1024 * 1024;

    #[test]
    fn each_missing_requirement_is_named() {
        assert!(refusals("aarch64", 26, Some(100 * GIB)).is_empty());
        assert!(refusals("aarch64", 14, Some(25 * GIB)).is_empty());
        let all = refusals("x86_64", 13, Some(10 * GIB));
        assert_eq!(all.len(), 3, "{all:?}");
        assert!(all[0].contains("Apple silicon"));
        assert!(all[1].contains("macOS 14") && all[1].contains("macOS 13"));
        assert!(all[2].contains("25 GB") && all[2].contains("10 GB is free"));
        assert!(refusals("aarch64", 15, None)[0].contains("free"));
    }

    #[test]
    fn free_space_is_read_from_the_nearest_existing_folder() {
        let free = free_bytes(Path::new("/nonexistent/istota/state")).unwrap();
        assert!(free > 0);
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("istota-firstrun-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn the_command_line_install_is_offered_as_a_move_from_its_own_folder() {
        let home = scratch("offers");
        assert_eq!(offers(&home), Offers::default());
        std::fs::create_dir_all(home.join(".istota")).unwrap();
        assert!(offers(&home).standalone);
        std::fs::create_dir_all(home.join(".lima/istota")).unwrap();
        let found = offers(&home);
        assert!(found.cli_install && !found.standalone, "a move is offered over an import");
        assert_eq!(found.cli_workspace, Some(home.join("Istota").display().to_string()));
        std::fs::create_dir_all(home.join(".config/istota-stack")).unwrap();
        std::fs::write(home.join(".config/istota-stack/istota.env"), "PORT=8080\nWORKSPACE=/Users/a/Notes/Istota\n")
            .unwrap();
        assert_eq!(cli_install_workspace(&home), PathBuf::from("/Users/a/Notes/Istota"));
        let _ = std::fs::remove_dir_all(&home);
    }

    /// A fake command-line install: `limactl` in the app's bin directory and
    /// the user's own `~/.local/bin/istota-stack`, each recording its argv and
    /// `LIMA_HOME`. Nothing here reaches a real `~/.lima`.
    fn fake_cli_install(name: &str, status: &str, stop_exit: i32) -> (PathBuf, Paths) {
        use std::os::unix::fs::PermissionsExt;
        let dir = scratch(name);
        let home = dir.join("home");
        let bin = dir.join("Resources/bin");
        std::fs::create_dir_all(home.join(".local/bin")).unwrap();
        std::fs::create_dir_all(home.join(".lima/istota")).unwrap();
        std::fs::create_dir_all(&bin).unwrap();
        let calls = dir.join("calls");
        let write = |path: PathBuf, body: String| {
            std::fs::write(&path, format!("#!/bin/bash\necho \"$(basename \"$0\") $*|$LIMA_HOME\" >> '{}'\n{body}\n", calls.display()))
                .unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        };
        write(bin.join("limactl"), format!("echo '{{\"name\":\"istota\",\"status\":\"{status}\"}}'"));
        write(home.join(".local/bin/istota-stack"), format!("echo stopping\nexit {stop_exit}"));
        (dir.clone(), Paths::new(dir.join("state"), dir.join("Resources")))
    }

    #[test]
    fn a_move_stops_the_running_command_line_install_with_its_own_cli() {
        let (dir, paths) = fake_cli_install("move-stop", "Running", 0);
        let home = dir.join("home");
        let logged = std::sync::Mutex::new(Vec::new());
        stop_cli_install(&paths, &home, &|line: &str| logged.lock().unwrap().push(line.to_string())).unwrap();
        let calls = std::fs::read_to_string(dir.join("calls")).unwrap();
        let lima = home.join(".lima").display().to_string();
        assert_eq!(calls, format!("limactl list --json|{lima}\nistota-stack stop|{lima}\n"));
        assert!(logged.lock().unwrap().contains(&"stopping".to_string()));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_stopped_command_line_install_is_left_alone_and_a_failed_stop_is_an_error() {
        let (dir, paths) = fake_cli_install("move-stopped", "Stopped", 0);
        stop_cli_install(&paths, &dir.join("home"), &|_| {}).unwrap();
        let calls = std::fs::read_to_string(dir.join("calls")).unwrap();
        assert_eq!(calls.lines().count(), 1, "{calls}");
        let _ = std::fs::remove_dir_all(&dir);

        let (dir, paths) = fake_cli_install("move-fail", "Running", 1);
        let err = stop_cli_install(&paths, &dir.join("home"), &|_| {}).unwrap_err();
        assert!(err.contains("Could not stop") && err.contains("stopping"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn two_vms_on_one_running_folder_are_refused() {
        let a = Path::new("/Users/a/Istota");
        assert!(shares_a_running_workspace(a, a, true));
        assert!(!shares_a_running_workspace(a, a, false));
        assert!(!shares_a_running_workspace(a, Path::new("/Users/a/Other"), true));
    }

    #[test]
    fn the_bundled_release_is_staged_once_and_never_half_named() {
        let dir = scratch("release");
        let resources = dir.join("Resources");
        assert_eq!(bundled_release(&resources), None);
        let source = resources.join("release/v1.2.3");
        std::fs::create_dir_all(&source).unwrap();
        assert_eq!(bundled_release(&resources), None, "no disk, no release");
        std::fs::write(source.join("istota-appliance-v1.2.3.raw.zst"), "disk").unwrap();
        std::fs::write(source.join("images-v1.2.3.json"), "{}").unwrap();
        assert_eq!(bundled_release(&resources), Some(("v1.2.3".to_string(), source.clone())));

        let root = dir.join("state/release");
        let staged = stage_release("v1.2.3", &source, &root).unwrap();
        assert_eq!(staged, root.join("v1.2.3"));
        assert_eq!(std::fs::read_to_string(staged.join("istota-appliance-v1.2.3.raw.zst")).unwrap(), "disk");
        assert!(!root.join(".v1.2.3.partial").exists());
        // A copy that differs from the app's is replaced.
        std::fs::write(staged.join("images-v1.2.3.json"), "{\"x\":1}").unwrap();
        stage_release("v1.2.3", &source, &root).unwrap();
        assert_eq!(std::fs::read_to_string(staged.join("images-v1.2.3.json")).unwrap(), "{}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_rebuilt_release_with_the_same_tag_and_sizes_is_staged_again() {
        let dir = scratch("rebuilt");
        let source = dir.join("Resources/release/v1.2.3");
        std::fs::create_dir_all(&source).unwrap();
        let disk = source.join("istota-appliance-v1.2.3.raw.zst");
        let big = (COMPARED_BY_CONTENT + 1) as usize;
        std::fs::write(&disk, vec![b'a'; big]).unwrap();
        std::fs::write(source.join("images-v1.2.3.json"), "{\"a\":1}").unwrap();
        std::fs::write(source.join("images-v1.2.3.json.sig"), "sig-one").unwrap();
        let root = dir.join("state/release");
        let staged = stage_release("v1.2.3", &source, &root).unwrap();

        // The rebuild: a manifest and signature of the same sizes.
        std::fs::write(source.join("images-v1.2.3.json"), "{\"a\":2}").unwrap();
        std::fs::write(source.join("images-v1.2.3.json.sig"), "sig-two").unwrap();
        stage_release("v1.2.3", &source, &root).unwrap();
        assert_eq!(std::fs::read_to_string(staged.join("images-v1.2.3.json")).unwrap(), "{\"a\":2}");
        assert_eq!(std::fs::read_to_string(staged.join("images-v1.2.3.json.sig")).unwrap(), "sig-two");

        // A disk of the same size, written later.
        std::fs::write(&disk, vec![b'b'; big]).unwrap();
        let later = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
        std::fs::File::options().write(true).open(&disk).unwrap().set_modified(later).unwrap();
        stage_release("v1.2.3", &source, &root).unwrap();
        assert_eq!(std::fs::read(staged.join("istota-appliance-v1.2.3.raw.zst")).unwrap()[0], b'b');
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_restore_date_is_a_calendar_day_in_the_clis_form() {
        assert!(valid_date("2026-10-01"));
        assert!(!valid_date("2026-10-1"));
        assert!(!valid_date("2026/10/01"));
        assert!(!valid_date("--force -x"));
    }

    #[test]
    fn progress_follows_the_steps_forward_only() {
        let mut p = Progress::new(Job::Create, 0);
        p.advance("[istota-stack] the release v1 is signed by the release key", 1);
        assert!(p.steps.iter().all(|s| s.started_ms.is_none()));
        p.advance("[istota-stack] unpacking the appliance disk of v1", 2);
        p.advance("time=\"…\" level=info msg=\"Starting the instance \\\"istota\\\" with internal VM driver \\\"vz\\\"\"", 5);
        assert_eq!(p.steps[0], Step { name: "Unpacking the disk", started_ms: Some(2), finished_ms: Some(5) });
        p.advance("level=info msg=\"Waiting for the final requirement 1 of 1: \\\"boot scripts must have finished\\\"\"", 9);
        p.advance("[istota-stack] loading the images of v1 from /mnt/istota-release/v1", 20);
        // A marker for an earlier step later on does not move it back.
        p.advance("[istota-stack] unpacking the appliance disk of v1", 21);
        assert_eq!(p.steps[3].started_ms, Some(20));
        assert_eq!(p.steps[0].finished_ms, Some(5));
        p.end(true, 30);
        assert!(p.steps[..4].iter().all(|s| s.finished_ms.is_some()));
        assert_eq!(p.steps[4].started_ms, None);
    }

    #[test]
    fn progress_keeps_the_last_lines_with_one_use_addresses_cut() {
        let mut p = Progress::new(Job::Move, 0);
        for i in 0..30 {
            p.advance(&format!("line {i}"), i);
        }
        p.advance("ISTOTA_SETUP_URL=http://localhost:8080/istota/setup#code=SECRET", 31);
        assert_eq!(p.lines.len(), TAIL_LINES);
        assert_eq!(p.lines.last().unwrap(), "ISTOTA_SETUP_URL=http://localhost:8080/istota/setup…");
        assert!(p.lines.iter().all(|l| !l.contains("SECRET")));
    }
}
