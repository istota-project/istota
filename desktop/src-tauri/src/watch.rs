//! Watching the workspace folder (FSEvents, through `notify`).
//!
//! A folder that went away still reads as a mount inside the VM, so the
//! `.istota-workspace` marker at its root is the signal, as it is for the
//! entrypoint and doctor (row L1). The watch covers the folder and its parent,
//! so a folder renamed, deleted or on a disk that was ejected is seen; the
//! health loop checks the marker too, in case an event is missed.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use notify::{RecursiveMode, Watcher};

pub const MARKER: &str = ".istota-workspace";

pub fn folder_present(folder: &Path) -> bool {
    folder.join(MARKER).is_file()
}

/// Calls `gone` once, the first time the folder's marker is missing after an
/// event. The returned watcher must be kept for the watch to last.
pub fn watch(
    folder: PathBuf,
    gone: impl Fn(&Path) + Send + 'static,
) -> notify::Result<notify::RecommendedWatcher> {
    let fired = Arc::new(AtomicBool::new(false));
    let checked = folder.clone();
    let mut watcher = notify::recommended_watcher(move |_event: notify::Result<notify::Event>| {
        if !folder_present(&checked) && !fired.swap(true, Ordering::SeqCst) {
            gone(&checked);
        }
    })?;
    if let Some(parent) = folder.parent() {
        watcher.watch(parent, RecursiveMode::NonRecursive)?;
    }
    watcher.watch(&folder, RecursiveMode::NonRecursive)?;
    Ok(watcher)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;
    use std::time::Duration;

    #[test]
    fn the_marker_is_what_makes_the_folder_present() {
        let dir = std::env::temp_dir().join(format!("istota-watch-a-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert!(!folder_present(&dir));
        std::fs::write(dir.join(MARKER), "").unwrap();
        assert!(folder_present(&dir));
        let _ = std::fs::remove_dir_all(&dir);
        assert!(!folder_present(&dir));
    }

    #[test]
    fn deleting_the_folder_fires_once() {
        let root = std::env::temp_dir().join(format!("istota-watch-b-{}", std::process::id()));
        let folder = root.join("Istota");
        std::fs::create_dir_all(&folder).unwrap();
        std::fs::write(folder.join(MARKER), "").unwrap();
        let (sender, receiver) = mpsc::channel();
        let _watcher = watch(folder.clone(), move |path| sender.send(path.to_path_buf()).unwrap()).unwrap();
        std::thread::sleep(Duration::from_millis(300));
        std::fs::remove_dir_all(&folder).unwrap();
        let fired = receiver.recv_timeout(Duration::from_secs(10)).expect("no event");
        assert_eq!(fired, folder);
        assert!(receiver.recv_timeout(Duration::from_millis(500)).is_err(), "fired twice");
        let _ = std::fs::remove_dir_all(&root);
    }
}
