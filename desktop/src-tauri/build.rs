fn main() {
    // Listing the app's commands makes each one a permission, so a command
    // is callable only where capabilities/app.json grants it.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "app_status",
            "open_web",
            "open_setup",
            "vm_start",
            "show_log_files",
            "quit_app",
        ]),
    ))
    .expect("tauri-build");
}
