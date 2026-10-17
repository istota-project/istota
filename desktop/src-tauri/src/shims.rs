//! "Install command-line tools": `istota-stack` and `istota` in a directory on
//! `PATH`, two small scripts that run the Mac CLI bundled in the app against
//! the app's own VM. Each sets what the app sets for its own calls
//! (`LIMA_HOME`, `XDG_CONFIG_HOME`, the instance, the CLI's script directory)
//! and puts the bundled tools first on `PATH`.
//!
//! The scripts name the app through `<state>/app`, a link the app points at
//! its own bundle each time it starts (`link_app`), so moving the app needs
//! no new scripts and no administrator prompt.
//!
//! Two places: `/usr/local/bin`, written through one administrator prompt
//! (`osascript`'s `with administrator privileges`), or `~/.local/bin`, which
//! needs none. A file there that the app did not write is never replaced or
//! removed: `~/.local/bin/istota-stack` is the command-line install's own
//! script, and replacing it would point the old install's commands, `destroy`
//! included, at the app's VM.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde::{Deserialize, Serialize};

use crate::paths::Paths;

pub const NAMES: [&str; 2] = ["istota-stack", "istota"];
/// The second line of every script the app writes; how it knows its own.
pub const MARKER: &str = "# istota-desktop-shim:";
pub const SYSTEM_DIR: &str = "/usr/local/bin";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Place {
    /// `/usr/local/bin`, through an administrator prompt.
    System,
    /// `~/.local/bin`.
    User,
}

pub fn dir_of(place: Place, home: &Path) -> PathBuf {
    match place {
        Place::System => PathBuf::from(SYSTEM_DIR),
        Place::User => home.join(".local/bin"),
    }
}

/// A word for `sh`, quoted whatever it holds.
pub fn quote(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}

/// Where the scripts find the app: a link in the state directory.
pub fn app_link(paths: &Paths) -> PathBuf {
    paths.state_dir.join("app")
}

/// One script: `name` is `istota-stack` or `istota`.
pub fn render(name: &str, paths: &Paths) -> String {
    let state = |p: PathBuf| quote(&p.display().to_string());
    format!(
        "#!/bin/sh\n\
         {MARKER} written by Istota.app, which replaces or removes it.\n\
         # The Mac CLI bundled in the app, for the app's own VM. The link below is\n\
         # the app wherever it is; the app points it there each time it starts.\n\
         app={app}\n\
         if [ ! -x \"$app/Contents/Resources/bin/{name}\" ]; then\n\
         \x20   echo \"{name}: Istota.app is not where it was. Open it once, and this command finds it again.\" >&2\n\
         \x20   exit 69\n\
         fi\n\
         LIMA_HOME={lima}\n\
         XDG_CONFIG_HOME={config}\n\
         ISTOTA_LIMA_INSTANCE={instance}\n\
         ISTOTA_MAC_BIN_DIR={bin}\n\
         PATH=\"$app/Contents/Resources/bin:$PATH\"\n\
         export LIMA_HOME XDG_CONFIG_HOME ISTOTA_LIMA_INSTANCE ISTOTA_MAC_BIN_DIR PATH\n\
         unset ISTOTA_LIMA_TEMPLATE ISTOTA_STACK_BIN\n\
         exec \"$app/Contents/Resources/bin/{name}\" \"$@\"\n",
        app = state(app_link(paths)),
        lima = state(paths.lima_home()),
        config = state(paths.cli_config_home()),
        instance = quote(&paths.instance),
        bin = state(paths.cli_bin_dir()),
    )
}

/// Whether `path` is a script the app wrote: a regular file, not a link,
/// carrying the marker on its second line.
pub fn is_ours(path: &Path) -> bool {
    let Ok(meta) = std::fs::symlink_metadata(path) else {
        return false;
    };
    if !meta.file_type().is_file() || meta.len() > 64 * 1024 {
        return false;
    }
    std::fs::read_to_string(path)
        .map(|text| text.lines().nth(1).is_some_and(|line| line.starts_with(MARKER)))
        .unwrap_or(false)
}

/// Whether the app's scripts are in `dir`.
pub fn installed_in(dir: &Path) -> bool {
    NAMES.iter().all(|name| is_ours(&dir.join(name)))
}

/// A refusal when `dir` holds a file of the same name the app did not write.
pub fn check_dir(dir: &Path) -> Result<(), String> {
    for name in NAMES {
        let path = dir.join(name);
        if std::fs::symlink_metadata(&path).is_ok() && !is_ours(&path) {
            return Err(format!(
                "{} is there already and Istota did not write it (on a Mac with the command-line install, it is that install's own). Istota leaves it alone: choose the other place, or remove that file first.",
                path.display()
            ));
        }
    }
    Ok(())
}

fn write_script(path: &Path, text: &str) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let dir = path.parent().unwrap_or(Path::new("."));
    let partial = dir.join(format!(
        ".{}.{}",
        path.file_name().and_then(|n| n.to_str()).unwrap_or("istota"),
        std::process::id()
    ));
    let _ = std::fs::remove_file(&partial);
    std::fs::write(&partial, text)?;
    std::fs::set_permissions(&partial, std::fs::Permissions::from_mode(0o755))?;
    std::fs::rename(&partial, path)
}

/// Writes both scripts into `dir`, which needs no administrator: `~/.local/bin`,
/// or the tier's scratch directory.
pub fn install_into(dir: &Path, paths: &Paths) -> Result<(), String> {
    std::fs::create_dir_all(dir).map_err(|err| format!("Could not make {}: {err}", dir.display()))?;
    check_dir(dir)?;
    for name in NAMES {
        write_script(&dir.join(name), &render(name, paths))
            .map_err(|err| format!("Could not write {}: {err}", dir.join(name).display()))?;
    }
    Ok(())
}

/// Removes the app's scripts from `dir`, and nothing else.
pub fn remove_from(dir: &Path) -> Result<(), String> {
    for name in NAMES {
        let path = dir.join(name);
        if is_ours(&path) {
            std::fs::remove_file(&path).map_err(|err| format!("Could not remove {}: {err}", path.display()))?;
        }
    }
    Ok(())
}

/// The scripts written into `<state>/shims/` (0700), for the administrator's
/// copy into `/usr/local/bin`.
pub fn stage(paths: &Paths) -> Result<PathBuf, String> {
    use std::os::unix::fs::PermissionsExt;
    let dir = paths.state_dir.join("shims");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).map_err(|err| format!("Could not make {}: {err}", dir.display()))?;
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).map_err(|err| err.to_string())?;
    for name in NAMES {
        write_script(&dir.join(name), &render(name, paths)).map_err(|err| err.to_string())?;
    }
    Ok(dir)
}

/// The shell command the administrator prompt runs to put staged scripts in
/// `target`. `install` unlinks a target before it writes, so it follows no link.
pub fn install_command(staged: &Path, target: &Path) -> String {
    let mut parts = vec![format!("umask 022; /bin/mkdir -p {}", quote(&target.display().to_string()))];
    for name in NAMES {
        parts.push(format!(
            "/usr/bin/install -m 0755 {} {}",
            quote(&staged.join(name).display().to_string()),
            quote(&target.join(name).display().to_string())
        ));
    }
    parts.join(" && ")
}

pub fn remove_command(target: &Path) -> String {
    let files: Vec<String> = NAMES.iter().map(|n| quote(&target.join(n).display().to_string())).collect();
    format!("/bin/rm -f {}", files.join(" "))
}

/// Runs `command` as root through macOS's administrator prompt. The command
/// is `osascript`'s argument, never part of the AppleScript, so no quoting
/// meets AppleScript's.
pub fn run_as_administrator(command: &str) -> Result<(), String> {
    let out = Command::new("/usr/bin/osascript")
        .args([
            "-e",
            "on run argv",
            "-e",
            "do shell script (item 1 of argv) with administrator privileges",
            "-e",
            "end run",
            command,
        ])
        .stdin(Stdio::null())
        .output()
        .map_err(|err| format!("Could not ask for an administrator: {err}"))?;
    if out.status.success() {
        return Ok(());
    }
    let said = String::from_utf8_lossy(&out.stderr);
    if said.contains("-128") {
        return Err("Cancelled; nothing was installed.".into());
    }
    Err(format!("The administrator step failed: {}", said.trim()))
}

pub fn install(place: Place, paths: &Paths, home: &Path) -> Result<PathBuf, String> {
    let dir = dir_of(place, home);
    match place {
        Place::User => install_into(&dir, paths)?,
        Place::System => {
            check_dir(&dir)?;
            let staged = stage(paths)?;
            let result = run_as_administrator(&install_command(&staged, &dir));
            let _ = std::fs::remove_dir_all(&staged);
            result?;
        }
    }
    Ok(dir)
}

pub fn remove(place: Place, home: &Path) -> Result<(), String> {
    let dir = dir_of(place, home);
    match place {
        Place::User => remove_from(&dir),
        Place::System => {
            if !NAMES.iter().any(|name| is_ours(&dir.join(name))) {
                return Ok(());
            }
            check_dir(&dir)?;
            run_as_administrator(&remove_command(&dir))
        }
    }
}

/// Points `<state>/app` at the bundle holding `paths.resources`, through a
/// new link and a rename. A layout that is not `<bundle>/Contents/Resources`
/// (a dev build) is left alone.
pub fn link_app(paths: &Paths) -> Result<(), String> {
    let Some(bundle) = paths
        .resources
        .parent()
        .filter(|contents| contents.file_name().is_some_and(|n| n == "Contents"))
        .and_then(Path::parent)
    else {
        return Ok(());
    };
    let link = app_link(paths);
    if std::fs::read_link(&link).is_ok_and(|target| target == bundle) {
        return Ok(());
    }
    let partial = paths.state_dir.join(format!(".app.{}", std::process::id()));
    let _ = std::fs::remove_file(&partial);
    std::os::unix::fs::symlink(bundle, &partial)
        .and_then(|()| std::fs::rename(&partial, &link))
        .map_err(|err| format!("Could not record where the app is: {err}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("istota-shims-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// An app bundle whose bundled CLI prints what it was started with, and a
    /// state directory with a space and a quote in its path.
    fn fake_app(dir: &Path) -> Paths {
        let bin = dir.join("Apps/Istota.app/Contents/Resources/bin");
        std::fs::create_dir_all(&bin).unwrap();
        for name in NAMES {
            let path = bin.join(name);
            std::fs::write(
                &path,
                format!(
                    "#!/bin/sh\nprintf '%s\\n' \"{name}\" \"$LIMA_HOME\" \"$XDG_CONFIG_HOME\" \"$ISTOTA_LIMA_INSTANCE\" \"$ISTOTA_MAC_BIN_DIR\" \"${{PATH%%:*}}\" \"${{ISTOTA_STACK_BIN:-unset}}\" \"$@\"\n"
                ),
            )
            .unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let state = dir.join("Application Support/It's Istota");
        std::fs::create_dir_all(&state).unwrap();
        Paths::new(state, dir.join("Apps/Istota.app/Contents/Resources")).with_instance("istota-d9".into())
    }

    fn run(script: &Path, args: &[&str]) -> std::process::Output {
        Command::new(script)
            .args(args)
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("ISTOTA_STACK_BIN", "/elsewhere/istota-stack")
            .output()
            .unwrap()
    }

    #[test]
    fn a_script_runs_the_bundled_cli_in_the_apps_environment() {
        let dir = scratch("run");
        let paths = fake_app(&dir);
        link_app(&paths).unwrap();
        let bin = dir.join("bin");
        install_into(&bin, &paths).unwrap();
        let out = run(&bin.join("istota-stack"), &["exec", "a b", "it's"]);
        assert!(out.status.success(), "{out:?}");
        let state = paths.state_dir.display().to_string();
        let lines: Vec<String> = String::from_utf8(out.stdout).unwrap().lines().map(str::to_string).collect();
        assert_eq!(
            lines,
            [
                "istota-stack".to_string(),
                format!("{state}/lima"),
                format!("{state}/config"),
                "istota-d9".to_string(),
                format!("{state}/bin"),
                format!("{state}/app/Contents/Resources/bin"),
                "unset".to_string(),
                "exec".to_string(),
                "a b".to_string(),
                "it's".to_string(),
            ]
        );
        let out = run(&bin.join("istota"), &["user", "list"]);
        assert_eq!(String::from_utf8(out.stdout).unwrap().lines().next(), Some("istota"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_moved_app_is_found_through_the_link_and_a_missing_one_is_named() {
        let dir = scratch("moved");
        let paths = fake_app(&dir);
        link_app(&paths).unwrap();
        let bin = dir.join("bin");
        install_into(&bin, &paths).unwrap();
        let written = std::fs::read(bin.join("istota-stack")).unwrap();

        std::fs::rename(dir.join("Apps"), dir.join("Moved")).unwrap();
        let out = run(&bin.join("istota-stack"), &["status"]);
        assert_eq!(out.status.code(), Some(69));
        assert!(String::from_utf8_lossy(&out.stderr).contains("Open it once"));

        let moved = Paths::new(paths.state_dir.clone(), dir.join("Moved/Istota.app/Contents/Resources"))
            .with_instance("istota-d9".into());
        link_app(&moved).unwrap();
        assert!(run(&bin.join("istota-stack"), &["status"]).status.success());
        assert_eq!(std::fs::read(bin.join("istota-stack")).unwrap(), written, "the script itself did not change");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_file_the_app_did_not_write_is_left_alone() {
        let dir = scratch("foreign");
        let paths = fake_app(&dir);
        let bin = dir.join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        // The command-line install's own script.
        std::fs::write(bin.join("istota-stack"), "#!/bin/bash\n# istota-stack on the Mac\n").unwrap();
        let err = install_into(&bin, &paths).unwrap_err();
        assert!(err.contains("Istota did not write it"), "{err}");
        assert_eq!(std::fs::read_to_string(bin.join("istota-stack")).unwrap(), "#!/bin/bash\n# istota-stack on the Mac\n");
        assert!(!bin.join("istota").exists(), "nothing was written beside it");
        remove_from(&bin).unwrap();
        assert!(bin.join("istota-stack").exists());

        // A link to one of the app's own scripts is not the app's either.
        let other = dir.join("other");
        install_into(&other, &paths).unwrap();
        std::fs::remove_file(bin.join("istota-stack")).unwrap();
        std::os::unix::fs::symlink(other.join("istota-stack"), bin.join("istota-stack")).unwrap();
        assert!(!is_ours(&bin.join("istota-stack")));
        assert!(install_into(&bin, &paths).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_apps_own_scripts_are_replaced_and_removed() {
        let dir = scratch("own");
        let paths = fake_app(&dir);
        let bin = dir.join("bin");
        install_into(&bin, &paths).unwrap();
        assert!(installed_in(&bin));
        install_into(&bin, &paths).unwrap();
        let mode = std::fs::metadata(bin.join("istota")).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o755);
        remove_from(&bin).unwrap();
        assert!(!bin.join("istota-stack").exists() && !bin.join("istota").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The administrator's command, run here without the prompt against a
    /// scratch prefix: what it installs is what `install_into` writes.
    #[test]
    fn the_administrators_command_installs_and_removes_the_staged_scripts() {
        let dir = scratch("admin");
        let paths = fake_app(&dir);
        let staged = stage(&paths).unwrap();
        assert_eq!(std::fs::metadata(&staged).unwrap().permissions().mode() & 0o777, 0o700);
        let target = dir.join("usr local/bin");
        let status = Command::new("/bin/sh").args(["-c", &install_command(&staged, &target)]).status().unwrap();
        assert!(status.success());
        assert!(installed_in(&target));
        assert_eq!(std::fs::read_to_string(target.join("istota")).unwrap(), render("istota", &paths));
        let status = Command::new("/bin/sh").args(["-c", &remove_command(&target)]).status().unwrap();
        assert!(status.success());
        assert!(!target.join("istota-stack").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_two_places() {
        let home = Path::new("/Users/a");
        assert_eq!(dir_of(Place::System, home), PathBuf::from("/usr/local/bin"));
        assert_eq!(dir_of(Place::User, home), PathBuf::from("/Users/a/.local/bin"));
        assert_eq!(quote("it's"), "'it'\\''s'");
    }

    #[test]
    fn a_dev_build_records_no_app() {
        let dir = scratch("dev");
        let paths = Paths::new(dir.join("state"), dir.join("target/debug"));
        std::fs::create_dir_all(&paths.state_dir).unwrap();
        link_app(&paths).unwrap();
        assert!(std::fs::symlink_metadata(app_link(&paths)).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
