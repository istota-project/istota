//! Istota for the Mac: a menu-bar app that supervises the local VM through
//! the bundled Mac CLI and shows the web UI in a window of its own.
//!
//! Two kinds of window. `app` is a Tauri webview holding the bundled pages
//! (first run, progress, status, errors, settings) and the only place Tauri's
//! IPC exists; `capabilities/app.json` grants it the commands below. Each web
//! UI window (`web-<profile>`) is a plain wry webview with no IPC at all
//! (`web.rs`).

mod firstrun;
mod health;
mod lifecycle;
mod logs;
mod login;
mod paths;
mod profiles;
mod settings;
mod stack;
mod testhooks;
mod vm;
mod watch;
mod web;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{TrayIcon, TrayIconBuilder};
use tauri::{AppHandle, Emitter, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder, Wry};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use firstrun::{Job, Progress};
use paths::Paths;
use profiles::{AppState, Kind};
use settings::Layer;
use stack::Call;
use vm::{Supervisor, VmState};

type Log = Arc<dyn Fn(&str) + Send + Sync>;

struct Menus {
    status: MenuItem<Wry>,
    layers: MenuItem<Wry>,
    login: CheckMenuItem<Wry>,
}

struct Shared {
    paths: Paths,
    home: PathBuf,
    supervisor: Arc<Supervisor>,
    profiles: Mutex<AppState>,
    healthy: Mutex<Option<bool>>,
    notice: Mutex<Option<String>>,
    progress: Mutex<Option<Progress>>,
    layers: Mutex<Vec<Layer>>,
    layers_running: AtomicBool,
    watcher: Mutex<Option<notify::RecommendedWatcher>>,
    tray: Mutex<Option<TrayIcon<Wry>>>,
    menus: Mutex<Option<Menus>>,
    quitting: AtomicBool,
    log: Log,
}

/// What the app window shows.
#[derive(Clone, Serialize)]
struct Status {
    vm: VmState,
    healthy: Option<bool>,
    profile: Option<String>,
    remote: bool,
    launch_at_login: bool,
    notice: Option<String>,
    /// The local VM's first run finished.
    local_ready: bool,
    progress: Option<Progress>,
    /// A failed step the app offers to resume (a VM-making call).
    resumable: bool,
    layers: Vec<Layer>,
    layers_running: bool,
    layers_hint: Option<&'static str>,
}

const LOCAL_WINDOW: &str = "web-local";

fn shared(app: &AppHandle) -> Arc<Shared> {
    app.state::<Arc<Shared>>().inner().clone()
}

fn status(shared: &Shared) -> Status {
    let profiles = shared.profiles.lock().unwrap();
    let current = profiles.current();
    let vm = shared.supervisor.state();
    let layers = shared.layers.lock().unwrap().clone();
    Status {
        resumable: matches!(&vm, VmState::Error(f) if vm::resumable(f)),
        vm,
        healthy: *shared.healthy.lock().unwrap(),
        profile: current.map(|p| p.id.clone()),
        remote: matches!(current.map(|p| &p.kind), Some(Kind::Remote { .. })),
        launch_at_login: lifecycle::launch_at_login_enabled(),
        notice: shared.notice.lock().unwrap().clone(),
        local_ready: profiles.local_ready,
        progress: shared.progress.lock().unwrap().clone(),
        layers_hint: settings::filter_hint(&layers),
        layers,
        layers_running: shared.layers_running.load(Ordering::SeqCst),
    }
}

fn status_line(state: &VmState, healthy: Option<bool>) -> String {
    let text = match state {
        VmState::Unknown => "checking",
        VmState::Absent => "not set up on this Mac",
        VmState::Creating => "setting up",
        VmState::Loading => "installing the release",
        VmState::Setup { .. } => "waiting for setup",
        VmState::Starting => "starting",
        VmState::Running if healthy == Some(false) => "not answering",
        VmState::Running => "running",
        VmState::Stopping => "stopping",
        VmState::Stopped => "stopped",
        VmState::Removing => "removing",
        VmState::Error(_) => "stopped with an error",
    };
    format!("Istota: {text}")
}

/// The menu's line for the background installs.
fn layers_line(layers: &[Layer], running: bool) -> String {
    if layers.is_empty() {
        return if running { "Background installs: running".into() } else { "Background installs: not checked".into() };
    }
    let parts: Vec<String> = layers
        .iter()
        .map(|l| {
            let name = if l.layer == "claude-code" { "Claude Code" } else { "Chrome" };
            format!("{name} {}", l.state)
        })
        .collect();
    format!("Background installs: {}", parts.join(", "))
}

/// The menu-bar icon: the octopus as a template, or tinted red when unhealthy.
/// Its own asset, not the app icon: a template draws only alpha, so the app
/// icon's opaque square would show as a filled block.
fn tray_icon(alert: bool) -> Image<'static> {
    let base = Image::from_bytes(include_bytes!("../icons/tray@2x.png")).expect("the bundled icon decodes");
    if !alert {
        return base.to_owned();
    }
    let mut rgba = base.rgba().to_vec();
    for pixel in rgba.chunks_mut(4) {
        pixel[0] = 0xd9;
        pixel[1] = 0x3b;
        pixel[2] = 0x30;
    }
    Image::new_owned(rgba, base.width(), base.height())
}

fn publish(app: &AppHandle) {
    let shared = shared(app);
    let current = status(&shared);
    let alert = current.healthy == Some(false) || matches!(current.vm, VmState::Error(_));
    if let Some(tray) = shared.tray.lock().unwrap().as_ref() {
        let _ = tray.set_icon(Some(tray_icon(alert)));
        let _ = tray.set_icon_as_template(!alert);
    }
    if let Some(menus) = shared.menus.lock().unwrap().as_ref() {
        let _ = menus.status.set_text(status_line(&current.vm, current.healthy));
        let _ = menus.layers.set_text(layers_line(&current.layers, current.layers_running));
        let _ = menus.login.set_checked(current.launch_at_login);
    }
    let _ = app.emit("status", current);
}

fn set_notice(app: &AppHandle, notice: Option<String>) {
    if let Some(text) = &notice {
        (shared(app).log)(&format!("notice: {text}"));
    }
    *shared(app).notice.lock().unwrap() = notice;
    publish(app);
}

fn save_profiles(shared: &Shared, change: impl FnOnce(&mut AppState)) {
    let mut profiles = shared.profiles.lock().unwrap();
    change(&mut profiles);
    if let Err(err) = profiles.save(&shared.paths.state_file()) {
        (shared.log)(&err);
    }
}

fn show_app_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("app") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn hide_app_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("app") {
        let _ = window.hide();
    }
}

fn open_web_window(app: &AppHandle, label: &str, title: &str, url: Url) {
    let log = shared(app).log.clone();
    let handle = app.clone();
    let label = label.to_string();
    let title = title.to_string();
    let _ = app.run_on_main_thread(move || match web::open(&handle, &label, &title, &url, log) {
        Ok(()) => hide_app_window(&handle),
        Err(err) => {
            set_notice(&handle, Some(format!("Could not open the window: {err}")));
            show_app_window(&handle);
        }
    });
}

/// The login handoff: a one-use code minted in the VM, loaded at once. An
/// install whose config takes no codes (one moved in from the command line
/// keeps its own sign-in) opens at its login page instead.
fn open_local(app: &AppHandle) -> Result<(), String> {
    let shared = shared(app);
    if !matches!(shared.supervisor.state(), VmState::Running) {
        return Err("Istota is not running on this Mac.".into());
    }
    let port = login::local_port(&shared.paths);
    let url = match shared.supervisor.output(&Call::Istota(login::LOGIN_CODE)) {
        Ok(stdout) => login::login_url(port, &login::parse_code(&stdout)?),
        Err(f) if login::codes_off(&f.tail) => {
            set_notice(app, Some("This install signs in with its own password; login codes are off in its settings.".into()));
            login::plain_url(port)
        }
        Err(f) => return Err(format!("{} {}", f.message, f.tail.join(" ")).trim().to_string()),
    };
    let url = Url::parse(&url).map_err(|err| err.to_string())?;
    open_web_window(app, LOCAL_WINDOW, "Istota", url);
    Ok(())
}

fn open_current(app: &AppHandle) -> Result<(), String> {
    let current = shared(app).profiles.lock().unwrap().current().cloned();
    match current.map(|p| (p.id, p.kind)) {
        Some((id, Kind::Remote { url })) => {
            let url = profiles::remote_url(&url)?;
            open_web_window(app, &format!("web-{id}"), &format!("Istota — {id}"), url);
            Ok(())
        }
        _ => open_local(app),
    }
}

/// Runs `job` off the main thread, showing a failure in the app window.
fn background(app: &AppHandle, job: impl FnOnce(&AppHandle) -> Result<(), String> + Send + 'static) {
    let app = app.clone();
    std::thread::spawn(move || {
        if let Err(err) = job(&app) {
            set_notice(&app, Some(err));
            show_app_window(&app);
        }
    });
}

fn on_vm_state(app: &AppHandle, state: &VmState) {
    let shared = shared(app);
    if !matches!(state, VmState::Running) {
        *shared.healthy.lock().unwrap() = None;
    }
    if matches!(state, VmState::Running) {
        ensure_watch(app);
    }
    if matches!(state, VmState::Error(_)) {
        show_app_window(app);
    }
    // The web setup is the first-run form: it opens in the app's own window.
    if let VmState::Setup { url } = state {
        // The tier answers the setup itself: the page would spend its one-use code.
        if testhooks::first_run_folder().is_some() {
            web::open_externally(url);
            publish(app);
            return;
        }
        match Url::parse(url) {
            Ok(url) => open_web_window(app, LOCAL_WINDOW, "Istota setup", url),
            Err(err) => (shared.log)(&format!("the setup address did not parse: {err}")),
        }
    }
    publish(app);
}

fn ensure_watch(app: &AppHandle) {
    let shared = shared(app);
    let mut watcher = shared.watcher.lock().unwrap();
    if watcher.is_some() {
        return;
    }
    let Some(folder) = login::local_workspace(&shared.paths) else {
        return;
    };
    let handle = app.clone();
    match watch::watch(folder, move |path| {
        let path = path.to_path_buf();
        let handle = handle.clone();
        std::thread::spawn(move || {
            shared_folder_gone(&handle, &path);
        });
    }) {
        Ok(w) => *watcher = Some(w),
        Err(err) => (shared.log)(&format!("could not watch the workspace folder: {err}")),
    }
}

fn shared_folder_gone(app: &AppHandle, path: &Path) {
    let shared = shared(app);
    *shared.watcher.lock().unwrap() = None;
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || web::close_all(&handle));
    shared.supervisor.folder_gone(path);
}

/// Polls the local web UI while the VM runs, and checks the folder's marker.
fn health_loop(app: AppHandle) {
    let mut tracker = health::Tracker::default();
    loop {
        let window_open = app.get_window(LOCAL_WINDOW).is_some()
            || app
                .get_webview_window("app")
                .and_then(|w| w.is_visible().ok())
                .unwrap_or(false);
        std::thread::sleep(health::interval(window_open));
        let shared = shared(&app);
        let local = !matches!(
            shared.profiles.lock().unwrap().current().map(|p| &p.kind),
            Some(Kind::Remote { .. })
        );
        if !local || !matches!(shared.supervisor.state(), VmState::Running) {
            tracker.reset();
            continue;
        }
        if let Some(folder) = login::local_workspace(&shared.paths) {
            if !watch::folder_present(&folder) {
                shared_folder_gone(&app, &folder);
                continue;
            }
        }
        let ok = health::probe(login::local_port(&shared.paths));
        if let Some(verdict) = tracker.record(ok) {
            *shared.healthy.lock().unwrap() = Some(verdict == health::Verdict::Healthy);
            (shared.log)(&format!("health: {verdict:?}"));
            publish(&app);
        } else if ok && shared.healthy.lock().unwrap().is_none() {
            *shared.healthy.lock().unwrap() = Some(true);
            publish(&app);
        }
    }
}

fn show_logs(app: &AppHandle) -> Result<(), String> {
    let shared = shared(app);
    let outcome = shared.supervisor.capture(&Call::Status);
    let mut status = outcome.tail.join("\n");
    status.push_str(&format!("\n(exit {:?})\n", outcome.code));
    let dir = logs::write_bundle(&shared.paths.logs_dir(), &status, &shared.paths.app_log())
        .map_err(|err| format!("Could not write the logs: {err}"))?;
    logs::reveal(&dir);
    Ok(())
}

fn sign_out_everywhere(app: &AppHandle) -> Result<(), String> {
    let shared = shared(app);
    let failed = |f: vm::Failure| format!("{} {}", f.message, f.tail.join(" ")).trim().to_string();
    let listing = shared.supervisor.output(&Call::Istota(&["auth", "list"])).map_err(failed)?;
    let user = login::only_live_identity(&listing)?;
    shared
        .supervisor
        .output(&Call::Istota(&["auth", "logout-all", &user]))
        .map_err(failed)?;
    set_notice(app, Some("Signed out everywhere.".into()));
    Ok(())
}

fn restart(app: &AppHandle) -> Result<(), String> {
    let supervisor = shared(app).supervisor.clone();
    if !matches!(supervisor.stop(), VmState::Stopped) {
        return Ok(());
    }
    supervisor.start();
    Ok(())
}

/// Whether the command-line install's VM is running on `folder`: the app
/// must not start a second VM on it. Lists `~/.lima` read-only.
fn shared_workspace_refusal(shared: &Shared, folder: &Path) -> Result<(), String> {
    if !firstrun::offers(&shared.home).cli_install {
        return Ok(());
    }
    let cli_folder = firstrun::cli_install_workspace(&shared.home);
    if cli_folder != folder {
        return Ok(());
    }
    let listed = stack::run(stack::cli_install_list(&shared.paths, &shared.home), |_| {});
    let running = listed.ok() && vm::parse_list(&listed.stdout, paths::CLI_INSTALL_INSTANCE) == vm::Listed::Running;
    if firstrun::shares_a_running_workspace(&cli_folder, folder, running) {
        return Err(firstrun::SHARED_WORKSPACE.into());
    }
    Ok(())
}

/// The bundled release, copied to where the VM mounts it.
fn staged_release(shared: &Shared) -> Result<PathBuf, String> {
    let (tag, source) = firstrun::bundled_release(&shared.paths.resources)
        .ok_or("This copy of Istota carries no release to install. Download Istota again.")?;
    firstrun::stage_release(&tag, &source, &shared.paths.release_root())
        .map_err(|err| format!("Could not copy the release out of the app: {err}"))
}

fn requirement_refusals(shared: &Shared) -> Vec<String> {
    let version = objc2_foundation::NSProcessInfo::processInfo().operatingSystemVersion();
    firstrun::refusals(
        std::env::consts::ARCH,
        version.majorVersion as u64,
        firstrun::free_bytes(&shared.paths.state_dir),
    )
}

/// One run of a job that makes the local VM, with the progress screen.
fn make_local(app: &AppHandle, job: Job, folder: &Path, date: Option<String>) -> Result<(), String> {
    let shared = shared(app);
    let refusals = requirement_refusals(&shared);
    if !refusals.is_empty() {
        return Err(refusals.join(" "));
    }
    // A move stops the command-line install's VM on that folder itself.
    if job != Job::Move {
        shared_workspace_refusal(&shared, folder)?;
    }
    let release = staged_release(&shared)?;
    if job == Job::Move {
        stop_cli_install(&shared)?;
    }
    // From here the job has a progress screen; a refusal above leaves the
    // first-run screen up with the reason.
    save_profiles(&shared, |p| {
        p.pending_job = Some(job);
        if job != Job::Restore {
            p.local_ready = false;
        }
    });
    *shared.progress.lock().unwrap() = Some(Progress::new(job, firstrun::now_ms()));
    set_notice(app, None);
    let call = match job {
        Job::Create => Call::Create { appliance: &release, workspace: folder },
        Job::Import => Call::ImportStandalone { appliance: &release, workspace: folder },
        Job::Move | Job::Restore => Call::Restore { appliance: &release, workspace: folder, date: date.as_deref() },
    };
    let handle = app.clone();
    let state = shared.supervisor.make(&call, |line| {
        if let Some(progress) = shared.progress.lock().unwrap().as_mut() {
            progress.advance(line, firstrun::now_ms());
        }
        publish(&handle);
    });
    let ok = matches!(state, VmState::Running);
    if let Some(progress) = shared.progress.lock().unwrap().as_mut() {
        progress.end(ok, firstrun::now_ms());
    }
    if !ok {
        publish(app);
        return Ok(());
    }
    *shared.progress.lock().unwrap() = None;
    let first = !shared.profiles.lock().unwrap().local_ready;
    save_profiles(&shared, |p| {
        p.local_ready = true;
        p.pending_job = None;
    });
    if job == Job::Move {
        set_notice(
            app,
            Some("Your command-line Istota is stopped and left as it was. Once Istota works here, delete it with `istota-stack destroy` in Terminal.".into()),
        );
    }
    #[cfg(feature = "m6-negative-control")]
    testhooks::put_the_master_key_on_an_argv(&shared.paths.instance, &*shared.log);
    start_layers(app);
    open_local(app)?;
    if first && !lifecycle::launch_at_login_enabled() && testhooks::first_run_folder().is_none() {
        offer_launch_at_login(app);
    }
    Ok(())
}

fn stop_cli_install(shared: &Shared) -> Result<(), String> {
    firstrun::stop_cli_install(&shared.paths, &shared.home, &*shared.log)
}

/// Offered at the end of the first run, on by default.
fn offer_launch_at_login(app: &AppHandle) {
    let yes = app
        .dialog()
        .message("Open Istota when you log in? It starts Istota on this Mac and opens no window.")
        .title("Open at login")
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::OkCancelCustom("Open at login".into(), "Not now".into()))
        .blocking_show();
    if yes {
        if let Err(err) = lifecycle::set_launch_at_login(true) {
            set_notice(app, Some(err));
        }
    }
    publish(app);
}

/// The background installs (Claude Code, Chrome): `local-layers` in the VM,
/// with its status polled into the menu and Settings while it runs.
fn start_layers(app: &AppHandle) {
    if shared(app).layers_running.swap(true, Ordering::SeqCst) {
        return;
    }
    publish(app);
    let poller = app.clone();
    std::thread::spawn(move || {
        while shared(&poller).layers_running.load(Ordering::SeqCst) {
            refresh_layers(&poller);
            std::thread::sleep(Duration::from_secs(15));
        }
    });
    let handle = app.clone();
    std::thread::spawn(move || {
        let shared = shared(&handle);
        shared.supervisor.alongside(&Call::LocalLayers);
        shared.layers_running.store(false, Ordering::SeqCst);
        refresh_layers(&handle);
    });
}

fn refresh_layers(app: &AppHandle) {
    let shared = shared(app);
    if !matches!(shared.supervisor.state(), VmState::Running) {
        return;
    }
    if let Ok(stdout) = shared.supervisor.output_alongside(&Call::LocalLayersStatus) {
        *shared.layers.lock().unwrap() = settings::parse_layers(&stdout);
    }
    publish(app);
}

fn confirm(app: &AppHandle, title: &str, message: &str, yes: &str) -> bool {
    app.dialog()
        .message(message)
        .title(title)
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(yes.into(), "Cancel".into()))
        .blocking_show()
}

/// "Quit Istota": stops the stack and the VM, after asking when tasks run.
fn quit(app: &AppHandle) -> Result<(), String> {
    let shared = shared(app);
    if shared.quitting.swap(true, Ordering::SeqCst) {
        return Ok(());
    }
    // A first run in flight is cancelled; Resume picks it up at the next launch.
    shared.supervisor.cancel();
    let local_running = matches!(shared.supervisor.state(), VmState::Running);
    if local_running {
        let running = shared
            .supervisor
            .output(&Call::Istota(&["list", "--status", "running"]))
            .map(|out| login::running_tasks(&out))
            .unwrap_or(0);
        if running > 0 {
            let message = format!(
                "{running} task{} still running. Quitting stops Istota and ends {}.",
                if running == 1 { " is" } else { "s are" },
                if running == 1 { "it" } else { "them" }
            );
            if !confirm(app, "Quit Istota?", &message, "Quit") {
                shared.quitting.store(false, Ordering::SeqCst);
                return Ok(());
            }
        }
        if !matches!(shared.supervisor.stop(), VmState::Stopped) {
            shared.quitting.store(false, Ordering::SeqCst);
            return Err("Istota could not stop the VM; it is still running. See Show logs.".into());
        }
    }
    app.exit(0);
    Ok(())
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let status = MenuItem::with_id(app, "status", "Istota: checking", false, None::<&str>)?;
    let layers = MenuItem::with_id(app, "layers", "Background installs: not checked", false, None::<&str>)?;
    let open = MenuItem::with_id(app, "open", "Open Istota", true, None::<&str>)?;
    let settings_item = MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?;
    let restart_item = MenuItem::with_id(app, "restart", "Restart Istota", true, None::<&str>)?;
    let logs = MenuItem::with_id(app, "logs", "Show logs", true, None::<&str>)?;
    let sign_out = MenuItem::with_id(app, "sign-out", "Sign out everywhere", true, None::<&str>)?;
    let login = CheckMenuItem::with_id(app, "login", "Open at login", true, false, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "Quit Istota", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &status,
            &layers,
            &PredefinedMenuItem::separator(app)?,
            &open,
            &settings_item,
            &restart_item,
            &logs,
            &sign_out,
            &PredefinedMenuItem::separator(app)?,
            &login,
            &quit_item,
        ],
    )?;
    let tray = TrayIconBuilder::with_id("istota")
        .icon(tray_icon(false))
        .icon_as_template(true)
        .tooltip("Istota")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => background(app, open_current),
            "settings" => {
                show_app_window(app);
                let _ = app.emit("show-settings", ());
            }
            "restart" => background(app, restart),
            "logs" => background(app, show_logs),
            "sign-out" => background(app, sign_out_everywhere),
            "login" => background(app, |app| {
                let result = lifecycle::set_launch_at_login(!lifecycle::launch_at_login_enabled());
                publish(app);
                result
            }),
            "quit" => background(app, quit),
            _ => {}
        })
        .build(app)?;
    let shared = shared(app);
    *shared.tray.lock().unwrap() = Some(tray);
    *shared.menus.lock().unwrap() = Some(Menus { status, layers, login });
    Ok(())
}

fn build_app_window(app: &AppHandle) -> tauri::Result<()> {
    let window = WebviewWindowBuilder::new(app, "app", WebviewUrl::App("index.html".into()))
        .title("Istota")
        .inner_size(620.0, 560.0)
        .resizable(true)
        .visible(false)
        // The bundled pages, and the dev server in a debug build; nothing else.
        .on_navigation(|url| {
            url.scheme() == "tauri" || (cfg!(debug_assertions) && url.host_str() == Some("localhost"))
        })
        .build()?;
    let handle = window.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            let _ = handle.hide();
        }
    });
    Ok(())
}

// --- commands, for the app window alone (capabilities/app.json) ---

#[tauri::command]
fn app_status(app: AppHandle) -> Status {
    status(&shared(&app))
}

#[tauri::command]
fn open_web(app: AppHandle) {
    set_notice(&app, None);
    background(&app, open_current);
}

#[tauri::command]
fn open_setup(app: AppHandle) -> Result<(), String> {
    let VmState::Setup { url } = shared(&app).supervisor.state() else {
        return Err("No setup is waiting.".into());
    };
    let url = Url::parse(&url).map_err(|err| err.to_string())?;
    open_web_window(&app, LOCAL_WINDOW, "Istota setup", url);
    Ok(())
}

#[tauri::command]
fn vm_start(app: AppHandle) {
    set_notice(&app, None);
    background(&app, |app| {
        let shared = shared(app);
        if let Some(folder) = login::local_workspace(&shared.paths) {
            shared_workspace_refusal(&shared, &folder)?;
        }
        if matches!(shared.supervisor.start(), VmState::Running) {
            open_local(app)?;
        }
        Ok(())
    });
}

#[tauri::command]
fn show_log_files(app: AppHandle) {
    background(&app, show_logs);
}

#[tauri::command]
fn quit_app(app: AppHandle) {
    background(&app, quit);
}

/// What the first-run screen needs to know before the user picks anything.
#[derive(Serialize)]
struct FirstRun {
    refusals: Vec<String>,
    offers: firstrun::Offers,
    default_folder: String,
    release: Option<String>,
}

#[tauri::command]
fn first_run_info(app: AppHandle) -> FirstRun {
    let shared = shared(&app);
    FirstRun {
        refusals: requirement_refusals(&shared),
        offers: firstrun::offers(&shared.home),
        default_folder: shared.home.join("Istota").display().to_string(),
        release: firstrun::bundled_release(&shared.paths.resources).map(|(tag, _)| tag),
    }
}

/// The folder picker. The Mac CLI's refusals of a folder apply when the
/// setup starts, and are shown as it words them.
#[tauri::command]
async fn pick_folder(app: AppHandle, current: String) -> Option<String> {
    let start = Path::new(&current).parent().map(Path::to_path_buf).unwrap_or_else(|| shared(&app).home.clone());
    app.dialog()
        .file()
        .set_title("Choose the folder Istota keeps its files in")
        .set_directory(start)
        .blocking_pick_folder()
        .and_then(|path| path.into_path().ok())
        .map(|path| path.display().to_string())
}

#[tauri::command]
fn begin_setup(app: AppHandle, job: Job, folder: String) -> Result<(), String> {
    if job == Job::Restore {
        return Err("A restore starts from Settings.".into());
    }
    let folder = PathBuf::from(folder);
    if !folder.is_absolute() {
        return Err("Choose a folder.".into());
    }
    background(&app, move |app| make_local(app, job, &folder, None));
    Ok(())
}

/// Resume: the job that did not finish, run again; the CLI's verbs resume
/// where they stopped.
#[tauri::command]
fn resume_setup(app: AppHandle) {
    background(&app, |app| {
        let shared = shared(app);
        let job = shared.profiles.lock().unwrap().pending_job.unwrap_or(Job::Create);
        // A move whose old VM is already stopped resumes as the restore it is.
        let job = if job == Job::Move { Job::Restore } else { job };
        let folder = login::local_workspace(&shared.paths).unwrap_or_else(|| shared.home.join("Istota"));
        make_local(app, job, &folder, None)
    });
}

#[tauri::command]
fn cancel_setup(app: AppHandle) {
    shared(&app).supervisor.cancel();
}

/// Settings' data: the background installs and the local model servers.
#[derive(Serialize)]
struct SettingsInfo {
    running: bool,
    claude_code_wanted: bool,
    models: Vec<settings::LocalModel>,
    workspace: Option<String>,
}

#[tauri::command]
async fn settings_info(app: AppHandle) -> SettingsInfo {
    let shared = shared(&app);
    let running = matches!(shared.supervisor.state(), VmState::Running);
    let workspace = login::local_workspace(&shared.paths).map(|p| p.display().to_string());
    if !running {
        return SettingsInfo { running, claude_code_wanted: true, models: settings::local_models(&[], settings::listening), workspace };
    }
    refresh_layers(&app);
    let allowed = shared
        .supervisor
        .output_alongside(&Call::HostEnvGet(stack::HOST_LOOPBACK_ALLOW))
        .map(|out| settings::allowed_ports(&out))
        .unwrap_or_default();
    let layers = shared.layers.lock().unwrap().clone();
    SettingsInfo {
        running,
        claude_code_wanted: settings::claude_code_wanted(&layers),
        models: settings::local_models(&allowed, settings::listening),
        workspace,
    }
}

#[tauri::command]
fn retry_layers(app: AppHandle) {
    start_layers(&app);
}

#[tauri::command]
fn set_claude_code(app: AppHandle, wanted: bool) {
    background(&app, move |app| {
        let shared = shared(app);
        shared
            .supervisor
            .output(&Call::HostEnvSet(stack::LOCAL_LAYERS, settings::local_layers_value(wanted)))
            .map_err(|f| format!("{} {}", f.message, f.tail.join(" ")))?;
        start_layers(app);
        Ok(())
    });
}

#[tauri::command]
fn set_host_port(app: AppHandle, port: u16, allow: bool) -> Result<(), String> {
    if !settings::known_port(port) {
        return Err(format!("{port} is not a local model server's port the app knows."));
    }
    background(&app, move |app| {
        let call = if allow { Call::AllowHostPort(port) } else { Call::DenyHostPort(port) };
        shared(app).supervisor.output(&call).map_err(|f| format!("{} {}", f.message, f.tail.join(" ")))?;
        set_notice(
            app,
            Some(if allow {
                format!("Istota can now reach port {port} on this Mac.")
            } else {
                format!("Istota can no longer reach port {port} on this Mac.")
            }),
        );
        Ok(())
    });
    Ok(())
}

#[tauri::command]
fn restore_backup(app: AppHandle, date: Option<String>) -> Result<(), String> {
    let date = date.filter(|d| !d.trim().is_empty());
    if let Some(d) = &date {
        if !firstrun::valid_date(d) {
            return Err(format!("{d} is not a date in the form 2026-10-01."));
        }
    }
    background(&app, move |app| {
        let shared = shared(app);
        let folder = login::local_workspace(&shared.paths).ok_or("Istota has no workspace folder on record to restore from.")?;
        let when = date.as_deref().map(|d| format!("the backup of {d}")).unwrap_or_else(|| "the newest backup".into());
        let message = format!(
            "This makes Istota on this Mac again from {when} in {}. Anything newer than that backup is lost, and the model's key and other credentials must be entered again.",
            folder.display()
        );
        if !confirm(app, "Restore from backup?", &message, "Restore") {
            return Ok(());
        }
        make_local(app, Job::Restore, &folder, date.clone())
    });
    Ok(())
}

#[tauri::command]
fn remove_istota(app: AppHandle) {
    background(&app, |app| {
        let shared = shared(app);
        let folder = login::local_workspace(&shared.paths)
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| "your workspace folder".into());
        let message = format!(
            "This deletes Istota's VM on this Mac: its database, its settings and the credentials stored in it. Your files and backups in {folder} and the master key in your login Keychain stay, so a restore can bring it back."
        );
        if !confirm(app, "Remove Istota?", &message, "Remove") {
            return Ok(());
        }
        let _ = app.run_on_main_thread({
            let handle = app.clone();
            move || web::close_all(&handle)
        });
        if matches!(shared.supervisor.remove(), VmState::Absent) {
            save_profiles(&shared, |p| {
                p.local_ready = false;
                p.pending_job = None;
            });
            *shared.layers.lock().unwrap() = Vec::new();
            set_notice(app, Some(format!("Istota is removed. {folder} and the Keychain's key are still there.")));
        }
        Ok(())
    });
}

/// "Locate folder", after the workspace went away: the VM mounts the folder
/// at the place it was made with, so it must come back there (a reconnected
/// disk); the app checks it and starts Istota again.
#[tauri::command]
fn locate_folder(app: AppHandle) {
    background(&app, |app| {
        let shared = shared(app);
        let folder = login::local_workspace(&shared.paths).ok_or("Istota has no workspace folder on record.")?;
        if !watch::folder_present(&folder) {
            return Err(format!(
                "Istota keeps its files in {}, and it is still not there. Connect its disk, or put the folder back at that place, then try again.",
                folder.display()
            ));
        }
        if matches!(shared.supervisor.start(), VmState::Running) {
            open_local(app)?;
        }
        Ok(())
    });
}

fn setup(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    app.set_activation_policy(tauri::ActivationPolicy::Accessory);
    let handle = app.handle().clone();
    let paths = Paths::new(Paths::default_state_dir(), app.path().resource_dir()?).with_instance(Paths::default_instance()?);
    paths.ensure()?;
    let log_path = paths.app_log();
    let log: Log = Arc::new(move |line: &str| logs::append(&log_path, line));
    log(&format!("launched; resources at {}", paths.resources.display()));

    let (profiles, notice) = match AppState::load(&paths.state_file()) {
        Ok(state) => (state, None),
        Err(err) => (AppState::default(), Some(err)),
    };
    let ready = profiles.local_ready;
    let notify_handle = handle.clone();
    let line_log = log.clone();
    let supervisor = Supervisor::new(
        paths.clone(),
        Box::new(move |state| on_vm_state(&notify_handle, state)),
        Box::new(move |line| line_log(line)),
    );
    app.manage(Arc::new(Shared {
        paths,
        home: paths::home(),
        supervisor,
        profiles: Mutex::new(profiles),
        healthy: Mutex::new(None),
        notice: Mutex::new(notice.clone()),
        progress: Mutex::new(None),
        layers: Mutex::new(Vec::new()),
        layers_running: AtomicBool::new(false),
        watcher: Mutex::new(None),
        tray: Mutex::new(None),
        menus: Mutex::new(None),
        quitting: AtomicBool::new(false),
        log,
    }));
    build_tray(&handle)?;
    build_app_window(&handle)?;

    let at_login = lifecycle::launched_at_login();
    let remote = matches!(
        shared(&handle).profiles.lock().unwrap().current().map(|p| &p.kind),
        Some(Kind::Remote { .. })
    );
    if notice.is_some() && !at_login {
        show_app_window(&handle);
    }
    if remote {
        // A remote profile needs no VM: none is created, started or listed.
        if !at_login {
            background(&handle, open_current);
        }
    } else {
        if !at_login {
            show_app_window(&handle);
        }
        background(&handle, move |app| {
            let shared = shared(app);
            if ready {
                if let Some(folder) = login::local_workspace(&shared.paths) {
                    shared_workspace_refusal(&shared, &folder)?;
                }
            }
            let state = shared.supervisor.adopt_or_start(ready);
            if let (VmState::Absent, Some(folder)) = (&state, testhooks::first_run_folder()) {
                (shared.log)("first run started by ISTOTA_DESKTOP_FIRST_RUN_FOLDER (desktop-test-hooks)");
                return make_local(app, Job::Create, &folder, None);
            }
            if matches!(state, VmState::Running) {
                // A layer that failed for want of a network is tried again now.
                start_layers(app);
                if !at_login {
                    open_local(app)?;
                }
            }
            Ok(())
        });
    }
    let health_handle = handle.clone();
    std::thread::spawn(move || health_loop(health_handle));
    publish(&handle);
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            app_status,
            open_web,
            open_setup,
            vm_start,
            show_log_files,
            quit_app,
            first_run_info,
            pick_folder,
            begin_setup,
            resume_setup,
            cancel_setup,
            settings_info,
            retry_layers,
            set_claude_code,
            set_host_port,
            restore_backup,
            remove_istota,
            locate_folder
        ])
        .setup(setup)
        .build(tauri::generate_context!())
        .expect("the app builds")
        .run(|_app, event| {
            // Closing the last window leaves the app, and the VM, running.
            if let RunEvent::ExitRequested { api, code: None, .. } = event {
                api.prevent_exit();
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_tray_icon_is_a_shape_on_transparency() {
        for alert in [false, true] {
            let icon = tray_icon(alert);
            let (w, rgba) = (icon.width() as usize, icon.rgba());
            let alpha = |x: usize, y: usize| rgba[(y * w + x) * 4 + 3];
            assert_eq!(alpha(0, 0), 0, "a corner is transparent");
            assert_eq!(alpha(w / 2, w / 4), 255, "the head is opaque");
        }
    }

    #[test]
    fn the_menu_names_each_background_install() {
        assert_eq!(layers_line(&[], false), "Background installs: not checked");
        let layers = settings::parse_layers(
            "{\"layer\":\"claude-code\",\"state\":\"ready\",\"detail\":\"\"}\n{\"layer\":\"chrome\",\"state\":\"building\",\"detail\":\"\"}\n",
        );
        assert_eq!(layers_line(&layers, true), "Background installs: Claude Code ready, Chrome building");
    }
}
