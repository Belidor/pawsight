//! pawsight desktop app.
//!
//! The app is a thin shell. The real program is the same Python code as the
//! CLI install, and the shell only does what install.sh + start.sh do:
//!
//! 1. copies the bundled code (`payload/`) into the per-user data folder,
//!    which then looks exactly like a git checkout of pawsight, so every path
//!    in the Python code (ROOT/.env, ROOT/storage, ...) works unchanged;
//! 2. creates `.venv` with the bundled `uv` (Python 3.12 and torch are
//!    downloaded on the first start, the right torch build for the machine);
//! 3. runs `python -m pawsight appinit` (templates, passwords, YOLO weights);
//! 4. runs `python -m pawsight run --watch-stdin`, the supervisor that starts
//!    go2rtc, the broker, the brain and the detector, and shows the dashboard.
//!
//! The supervisor holds our pipe on its stdin: if the app dies, even by
//! force, the pipe closes and the supervisor stops everything it started.
//! Nothing listens anywhere but 127.0.0.1 (AGENTS.md, rule 3).

use std::fs;
use std::io::{BufRead, BufReader};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use sha2::{Digest, Sha256};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};

const DASHBOARD: &str = "http://127.0.0.1:8099/";
const WEB_PORT: u16 = 8099;
const OLLAMA_DOWNLOAD: &str = "https://ollama.com/download";
const VLM_SKIPPED: &str = ".vlm-skipped";

#[derive(Clone, Serialize)]
struct Line {
    step: String,
    text: String,
    level: String, // info | warn | error
}

#[derive(Clone, Serialize, Default)]
struct Snapshot {
    /// installing | starting | ready | running | error | stopped
    phase: String,
    lines: Vec<Line>,
    /// missing | no-model | ready | "" (not checked yet)
    ollama: String,
    vlm_model: String,
    data_dir: String,
}

#[derive(Default)]
struct Inner {
    snap: Snapshot,
    child: Option<Child>,
    busy: bool,
    local_url: Option<tauri::Url>,
}

struct AppState(Mutex<Inner>);

// ---------- small helpers ----------

fn exe(name: &str) -> String {
    if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_string()
    }
}

fn sidecar(name: &str) -> PathBuf {
    // Tauri puts externalBin next to the main executable, without the triple.
    let me = std::env::current_exe().unwrap_or_default();
    me.parent().unwrap_or(Path::new(".")).join(exe(name))
}

fn venv_python(data: &Path) -> PathBuf {
    if cfg!(windows) {
        data.join(".venv").join("Scripts").join("python.exe")
    } else {
        data.join(".venv").join("bin").join("python")
    }
}

fn port_open(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok()
}

fn no_window(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    let _ = cmd;
}

fn data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let d = app.path().app_data_dir().map_err(|e| e.to_string())?;
    fs::create_dir_all(&d).map_err(|e| format!("{}: {e}", d.display()))?;
    Ok(d)
}

fn say(app: &AppHandle, step: &str, text: &str, level: &str) {
    let line = Line { step: step.into(), text: text.into(), level: level.into() };
    {
        let st = app.state::<AppState>();
        let mut g = st.0.lock().unwrap();
        // The same lines go to storage/app.log: the window may be closed, and
        // "what did the app do" must be answerable from the data folder.
        if !g.snap.data_dir.is_empty() {
            let p = Path::new(&g.snap.data_dir).join("storage").join("app.log");
            if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(p) {
                use std::io::Write;
                let _ = writeln!(f, "{step}: {text}");
            }
        }
        g.snap.lines.push(line.clone());
        let n = g.snap.lines.len();
        if n > 400 {
            g.snap.lines.drain(0..n - 400);
        }
    }
    let _ = app.emit("pawsight://line", line);
}

fn set_phase(app: &AppHandle, phase: &str) {
    let snap = {
        let st = app.state::<AppState>();
        let mut g = st.0.lock().unwrap();
        g.snap.phase = phase.into();
        g.snap.clone()
    };
    let _ = app.emit("pawsight://state", snap);
}

/// Run a command, streaming stdout and stderr lines into the progress list.
fn run_streamed(app: &AppHandle, step: &str, cmd: &mut Command) -> Result<(), String> {
    no_window(cmd);
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("could not start {:?}: {e}", cmd.get_program()))?;
    let err = child.stderr.take().unwrap();
    let app2 = app.clone();
    let step2 = step.to_string();
    let t = thread::spawn(move || {
        for l in BufReader::new(err).lines().map_while(Result::ok) {
            let l = l.trim().to_string();
            if !l.is_empty() {
                say(&app2, &step2, &l, "info");
            }
        }
    });
    let out = child.stdout.take().unwrap();
    for l in BufReader::new(out).lines().map_while(Result::ok) {
        let l = l.trim().to_string();
        if l.is_empty() {
            continue;
        }
        // appinit prints "step: text"; keep its own step name
        if let Some((s, rest)) = l.split_once(": ") {
            if !s.contains(' ') && s.len() < 16 {
                if s == "ollama" {
                    remember_ollama(app, rest);
                }
                say(app, s, rest, "info");
                continue;
            }
        }
        say(app, step, &l, "info");
    }
    let _ = t.join();
    let status = child.wait().map_err(|e| e.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("{step} failed ({status})"))
    }
}

fn remember_ollama(app: &AppHandle, rest: &str) {
    // "missing qwen3-vl:8b-instruct" | "no-model …" | "ready …" | "pulling 42%"
    let mut it = rest.splitn(2, ' ');
    let state = it.next().unwrap_or("").to_string();
    if !["missing", "no-model", "ready"].contains(&state.as_str()) {
        return;
    }
    let st = app.state::<AppState>();
    let mut g = st.0.lock().unwrap();
    g.snap.ollama = state;
    g.snap.vlm_model = it.next().unwrap_or("").to_string();
}

// ---------- install ----------

fn copy_dir(src: &Path, dst: &Path) -> std::io::Result<()> {
    fs::create_dir_all(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let name = entry.file_name();
        if name == "__pycache__" || name == ".DS_Store" {
            continue;
        }
        let from = entry.path();
        let to = dst.join(&name);
        if entry.file_type()?.is_dir() {
            copy_dir(&from, &to)?;
        } else {
            fs::copy(&from, &to)?;
        }
    }
    Ok(())
}

/// Bundled code -> data folder. Only code and templates are in the payload;
/// the owner's files (.env, zones.yaml, go2rtc.yaml, storage/, dataset/,
/// weights) are never there, so overwriting is safe. pawsight/ is replaced
/// as a whole, so a module removed in a new version does not linger.
fn sync_payload(app: &AppHandle, data: &Path) -> Result<(), String> {
    let payload = app
        .path()
        .resource_dir()
        .map_err(|e| e.to_string())?
        .join("payload");
    if !payload.join("pawsight").is_dir() {
        return Err(format!("the app is incomplete: no {}", payload.display()));
    }
    let code = data.join("pawsight");
    if code.exists() {
        fs::remove_dir_all(&code).map_err(|e| format!("{}: {e}", code.display()))?;
    }
    copy_dir(&payload, data).map_err(|e| format!("copying the program: {e}"))?;
    Ok(())
}

fn requirements_hash(data: &Path) -> String {
    let body = fs::read(data.join("requirements.txt")).unwrap_or_default();
    format!("{:x}", Sha256::digest(&body))
}

fn uv(data: &Path) -> Command {
    let mut c = Command::new(sidecar("uv"));
    c.current_dir(data)
        // Python lives inside the app's folder, not in the user's ~/.local,
        // and only uv's own build is used: a system python may be x86_64
        // under Rosetta, which has no torch (install.sh learned that).
        .env("UV_PYTHON_INSTALL_DIR", data.join("python"))
        .env("UV_CACHE_DIR", data.join("cache").join("uv"))
        .env("UV_PYTHON_PREFERENCE", "only-managed")
        .env("UV_NO_PROGRESS", "1")
        .env_remove("VIRTUAL_ENV")
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH");
    c
}

fn install(app: &AppHandle, data: &Path) -> Result<(), String> {
    say(app, "app", &format!("data folder: {}", data.display()), "info");
    sync_payload(app, data)?;
    say(app, "app", "program files updated", "info");

    let py = venv_python(data);
    if !py.exists() {
        say(app, "python", "creating Python 3.12 (first start only)...", "info");
        run_streamed(app, "python", uv(data).args(["venv", "--python", "3.12", ".venv"]))?;
    }
    let stamp = data.join(".venv").join("pawsight-requirements.sha256");
    let want = requirements_hash(data);
    if fs::read_to_string(&stamp).unwrap_or_default().trim() != want {
        say(
            app,
            "packages",
            "installing packages: torch is large, the first time takes several minutes...",
            "info",
        );
        run_streamed(
            app,
            "packages",
            uv(data)
                .args(["pip", "install", "-r", "requirements.txt", "--torch-backend", "auto", "--python"])
                .arg(&py),
        )?;
        let _ = fs::write(&stamp, &want);
        say(app, "packages", "packages are in place", "info");
    }

    run_streamed(
        app,
        "config",
        Command::new(&py)
            .current_dir(data)
            .args(["-m", "pawsight", "appinit", "--models"])
            .env("PYTHONUNBUFFERED", "1"),
    )?;
    Ok(())
}

// ---------- run ----------

fn rotate(p: &Path) {
    if p.exists() {
        let mut old = p.as_os_str().to_owned();
        old.push(".1");
        let _ = fs::rename(p, PathBuf::from(old));
    }
}

fn start_runner(app: &AppHandle, data: &Path) -> Result<(), String> {
    if port_open(WEB_PORT) {
        return Err(format!(
            "port {WEB_PORT} is already taken: is pawsight already running from a terminal (./start.sh)? \
             Stop it (./stop.sh) and press Retry."
        ));
    }
    let storage = data.join("storage");
    let _ = fs::create_dir_all(&storage);
    let log = storage.join("runner.log");
    rotate(&log);
    let fh = fs::File::create(&log).map_err(|e| e.to_string())?;
    let fh2 = fh.try_clone().map_err(|e| e.to_string())?;
    let mut cmd = Command::new(venv_python(data));
    cmd.current_dir(data)
        .args(["-m", "pawsight", "run", "--watch-stdin"])
        .env("PYTHONUNBUFFERED", "1")
        .env("PAWSIGHT_GO2RTC_BIN", sidecar("go2rtc"))
        .stdin(Stdio::piped())
        .stdout(fh)
        .stderr(fh2);
    no_window(&mut cmd);
    let child = cmd.spawn().map_err(|e| format!("could not start pawsight: {e}"))?;
    say(app, "run", "starting go2rtc, the broker, the brain and the detector...", "info");
    app.state::<AppState>().0.lock().unwrap().child = Some(child);
    Ok(())
}

fn stop_runner(app: &AppHandle) {
    let child = app.state::<AppState>().0.lock().unwrap().child.take();
    if let Some(mut c) = child {
        drop(c.stdin.take()); // the supervisor stops everything when its stdin closes
        let end = Instant::now() + Duration::from_secs(20);
        while Instant::now() < end {
            if let Ok(Some(_)) = c.try_wait() {
                return;
            }
            thread::sleep(Duration::from_millis(200));
        }
        let _ = c.kill();
        let _ = c.wait();
    }
}

fn runner_alive(app: &AppHandle) -> bool {
    let st = app.state::<AppState>();
    let mut g = st.0.lock().unwrap();
    match g.child.as_mut() {
        Some(c) => matches!(c.try_wait(), Ok(None)),
        None => false,
    }
}

fn tail(path: &Path, n: usize) -> String {
    let s = fs::read_to_string(path).unwrap_or_default();
    let lines: Vec<&str> = s.lines().collect();
    lines[lines.len().saturating_sub(n)..].join("\n")
}

fn wait_ready(app: &AppHandle, data: &Path) -> Result<(), String> {
    let end = Instant::now() + Duration::from_secs(180);
    while Instant::now() < end {
        if port_open(WEB_PORT) {
            return Ok(());
        }
        if !runner_alive(app) {
            let t = tail(&data.join("storage").join("runner.log"), 15);
            return Err(format!("pawsight stopped right after the start:\n{t}"));
        }
        thread::sleep(Duration::from_millis(500));
    }
    Err("the dashboard did not come up in 3 minutes; see storage/brain.log in the data folder".into())
}

fn show_dashboard(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        if let Ok(u) = tauri::Url::parse(DASHBOARD) {
            let _ = w.navigate(u);
        }
        let _ = w.show();
        let _ = w.set_focus();
    }
    set_phase(app, "running");
}

fn show_status(app: &AppHandle) {
    let url = app.state::<AppState>().0.lock().unwrap().local_url.clone();
    if let (Some(w), Some(u)) = (app.get_webview_window("main"), url) {
        let _ = w.navigate(u);
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn boot(app: AppHandle) {
    {
        let st = app.state::<AppState>();
        let mut g = st.0.lock().unwrap();
        if g.busy {
            return;
        }
        g.busy = true;
    }
    thread::spawn(move || {
        let result = (|| -> Result<(), String> {
            let data = data_dir(&app)?;
            let _ = fs::create_dir_all(data.join("storage"));
            rotate(&data.join("storage").join("app.log"));
            app.state::<AppState>().0.lock().unwrap().snap.data_dir = data.display().to_string();
            set_phase(&app, "installing");
            install(&app, &data)?;
            set_phase(&app, "starting");
            start_runner(&app, &data)?;
            wait_ready(&app, &data)?;
            say(&app, "run", "pawsight is running", "info");
            let (ollama, skipped) = {
                let g = app.state::<AppState>();
                let g = g.0.lock().unwrap();
                (g.snap.ollama.clone(), data.join(VLM_SKIPPED).exists())
            };
            if ollama == "ready" || skipped {
                show_dashboard(&app);
            } else {
                // Descriptions need Ollama: say so once, on the status page,
                // instead of silently running without them.
                set_phase(&app, "ready");
            }
            Ok(())
        })();
        if let Err(e) = result {
            say(&app, "error", &e, "error");
            stop_runner(&app);
            set_phase(&app, "error");
            show_status(&app);
        }
        app.state::<AppState>().0.lock().unwrap().busy = false;
    });
}

fn restart(app: AppHandle) {
    thread::spawn(move || {
        show_status(&app);
        say(&app, "run", "restarting...", "info");
        stop_runner(&app);
        boot(app);
    });
}

// ---------- commands for the local status page ----------

#[tauri::command]
fn get_state(state: tauri::State<'_, AppState>) -> Snapshot {
    state.0.lock().unwrap().snap.clone()
}

#[tauri::command]
fn retry(app: AppHandle) {
    restart(app);
}

#[tauri::command]
fn open_dashboard(app: AppHandle) {
    show_dashboard(&app);
}

#[tauri::command]
fn skip_vlm(app: AppHandle) {
    if let Ok(d) = data_dir(&app) {
        let _ = fs::write(d.join(VLM_SKIPPED), "descriptions turned off on the status page\n");
    }
    show_dashboard(&app);
}

#[tauri::command]
fn get_ollama(app: AppHandle) {
    use tauri_plugin_opener::OpenerExt;
    let _ = app.opener().open_url(OLLAMA_DOWNLOAD, None::<&str>);
}

#[tauri::command]
fn pull_vlm(app: AppHandle) {
    thread::spawn(move || {
        let Ok(data) = data_dir(&app) else { return };
        let r = run_streamed(
            &app,
            "ollama",
            Command::new(venv_python(&data))
                .current_dir(&data)
                .args(["-m", "pawsight", "appinit", "--pull-vlm"])
                .env("PYTHONUNBUFFERED", "1"),
        );
        if let Err(e) = r {
            say(&app, "ollama", &e, "error");
        }
        let snap = app.state::<AppState>().0.lock().unwrap().snap.clone();
        let _ = app.emit("pawsight://state", snap);
    });
}

#[tauri::command]
fn open_data_dir(app: AppHandle) {
    use tauri_plugin_opener::OpenerExt;
    if let Ok(d) = data_dir(&app) {
        let _ = app.opener().open_path(d.display().to_string(), None::<&str>);
    }
}

// ---------- app ----------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .manage(AppState(Mutex::new(Inner::default())))
        .invoke_handler(tauri::generate_handler![
            get_state,
            retry,
            open_dashboard,
            skip_vlm,
            get_ollama,
            pull_vlm,
            open_data_dir
        ])
        .setup(|app| {
            let h = app.handle().clone();
            if let Some(w) = app.get_webview_window("main") {
                if let Ok(u) = w.url() {
                    h.state::<AppState>().0.lock().unwrap().local_url = Some(u);
                }
            }
            let open = MenuItem::with_id(app, "open", "Open pawsight", true, None::<&str>)?;
            let status = MenuItem::with_id(app, "status", "Status and log", true, None::<&str>)?;
            let restart_i = MenuItem::with_id(app, "restart", "Restart", true, None::<&str>)?;
            let folder = MenuItem::with_id(app, "folder", "Open data folder", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let quit = MenuItem::with_id(app, "quit", "Quit pawsight", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &status, &restart_i, &folder, &sep, &quit])?;
            TrayIconBuilder::with_id("tray")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("pawsight")
                .menu(&menu)
                .on_menu_event(|app, ev| match ev.id().as_ref() {
                    "open" => {
                        if runner_alive(app) {
                            show_dashboard(app)
                        } else {
                            show_status(app)
                        }
                    }
                    "status" => show_status(app),
                    "restart" => restart(app.clone()),
                    "folder" => open_data_dir(app.clone()),
                    "quit" => {
                        let app = app.clone();
                        thread::spawn(move || {
                            stop_runner(&app);
                            app.exit(0);
                        });
                    }
                    _ => {}
                })
                .build(app)?;
            boot(h);
            Ok(())
        })
        .on_window_event(|w, ev| {
            // Closing the window keeps the camera watching: the app lives in
            // the tray/menu bar. "Quit pawsight" there stops everything.
            if let WindowEvent::CloseRequested { api, .. } = ev {
                api.prevent_close();
                let _ = w.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building the pawsight app");

    app.run(|app, ev| match ev {
        RunEvent::ExitRequested { .. } | RunEvent::Exit => stop_runner(app),
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }
        _ => {}
    });
}
