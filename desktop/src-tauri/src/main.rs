//! Istota for the Mac: a menu-bar app that supervises the local VM through
//! the bundled Mac CLI and shows the web UI in a window of its own.
//!
//! Two kinds of window. `app` is a Tauri webview holding the bundled pages
//! (status, errors, the login handoff) and the only place Tauri's IPC exists;
//! `capabilities/app.json` grants it the commands below. Each web UI window
//! (`web-<profile>`) is a plain wry webview with no IPC at all (`web.rs`).

mod health;
mod lifecycle;
mod logs;
mod login;
mod paths;
mod profiles;
mod stack;
mod vm;
mod watch;
mod web;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use serde::Serialize;
use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{TrayIcon, TrayIconBuilder};
use tauri::{AppHandle, Emitter, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder, Wry};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use paths::Paths;
use profiles::{AppState, Kind};
use stack::Call;
use vm::{Supervisor, VmState};

type Log = Arc<dyn Fn(&str) + Send + Sync>;

struct Menus {
    status: MenuItem<Wry>,
    login: CheckMenuItem<Wry>,
}

struct Shared {
    paths: Paths,
    supervisor: Arc<Supervisor>,
    profiles: Mutex<AppState>,
    healthy: Mutex<Option<bool>>,
    notice: Mutex<Option<String>>,
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
}

const LOCAL_WINDOW: &str = "web-local";

fn shared(app: &AppHandle) -> Arc<Shared> {
    app.state::<Arc<Shared>>().inner().clone()
}

fn status(shared: &Shared) -> Status {
    let profiles = shared.profiles.lock().unwrap();
    let current = profiles.current();
    Status {
        vm: shared.supervisor.state(),
        healthy: *shared.healthy.lock().unwrap(),
        profile: current.map(|p| p.id.clone()),
        remote: matches!(current.map(|p| &p.kind), Some(Kind::Remote { .. })),
        launch_at_login: lifecycle::launch_at_login_enabled(),
        notice: shared.notice.lock().unwrap().clone(),
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
        VmState::Error(_) => "stopped with an error",
    };
    format!("Istota: {text}")
}

/// The menu-bar icon: the logo as a template, or tinted red when unhealthy.
fn tray_icon(alert: bool) -> Image<'static> {
    let base = Image::from_bytes(include_bytes!("../icons/32x32.png")).expect("the bundled icon decodes");
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

/// The login handoff: a one-use code minted in the VM, loaded at once.
fn open_local(app: &AppHandle) -> Result<(), String> {
    let shared = shared(app);
    if !matches!(shared.supervisor.state(), VmState::Running) {
        return Err("Istota is not running on this Mac.".into());
    }
    let stdout = shared
        .supervisor
        .output(&Call::Istota(login::LOGIN_CODE))
        .map_err(|f| format!("{} {}", f.message, f.tail.join(" ")).trim().to_string())?;
    let code = login::parse_code(&stdout)?;
    let port = login::local_port(&shared.paths);
    let url = Url::parse(&login::login_url(port, &code)).map_err(|err| err.to_string())?;
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

fn shared_folder_gone(app: &AppHandle, path: &std::path::Path) {
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

/// "Quit Istota": stops the stack and the VM, after asking when tasks run.
fn quit(app: &AppHandle) -> Result<(), String> {
    let shared = shared(app);
    if shared.quitting.swap(true, Ordering::SeqCst) {
        return Ok(());
    }
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
            let stop = app
                .dialog()
                .message(message)
                .title("Quit Istota?")
                .kind(MessageDialogKind::Warning)
                .buttons(MessageDialogButtons::OkCancelCustom("Quit".into(), "Cancel".into()))
                .blocking_show();
            if !stop {
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
    let open = MenuItem::with_id(app, "open", "Open Istota", true, None::<&str>)?;
    let restart_item = MenuItem::with_id(app, "restart", "Restart Istota", true, None::<&str>)?;
    let logs = MenuItem::with_id(app, "logs", "Show logs", true, None::<&str>)?;
    let sign_out = MenuItem::with_id(app, "sign-out", "Sign out everywhere", true, None::<&str>)?;
    let login = CheckMenuItem::with_id(app, "login", "Open at login", true, false, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "Quit Istota", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &status,
            &PredefinedMenuItem::separator(app)?,
            &open,
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
    *shared.menus.lock().unwrap() = Some(Menus { status, login });
    Ok(())
}

fn build_app_window(app: &AppHandle) -> tauri::Result<()> {
    let window = WebviewWindowBuilder::new(app, "app", WebviewUrl::App("index.html".into()))
        .title("Istota")
        .inner_size(560.0, 460.0)
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
        if matches!(shared(app).supervisor.start(), VmState::Running) {
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

fn setup(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    app.set_activation_policy(tauri::ActivationPolicy::Accessory);
    let handle = app.handle().clone();
    let paths = Paths::new(Paths::default_state_dir(), app.path().resource_dir()?);
    paths.ensure()?;
    let log_path = paths.app_log();
    let log: Log = Arc::new(move |line: &str| logs::append(&log_path, line));
    log(&format!("launched; resources at {}", paths.resources.display()));

    let (profiles, notice) = match AppState::load(&paths.state_file()) {
        Ok(state) => (state, None),
        Err(err) => (AppState::default(), Some(err)),
    };
    let notify_handle = handle.clone();
    let line_log = log.clone();
    let supervisor = Supervisor::new(
        paths.clone(),
        Box::new(move |state| on_vm_state(&notify_handle, state)),
        Box::new(move |line| line_log(line)),
    );
    app.manage(Arc::new(Shared {
        paths,
        supervisor,
        profiles: Mutex::new(profiles),
        healthy: Mutex::new(None),
        notice: Mutex::new(notice.clone()),
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
            let state = shared(app).supervisor.adopt_or_start();
            if matches!(state, VmState::Running) && !at_login {
                open_local(app)?;
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
            quit_app
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
