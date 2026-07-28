//! Spawns the Python engine (veloci_engine.cli) as a child process and
//! bridges its newline-delimited JSON stdio protocol to the frontend:
//! commands go in via `send_engine_command`, events come out as
//! "engine-event" Tauri events.

use std::io::{BufRead, BufReader, Write};
#[cfg(debug_assertions)]
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;

use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager};

pub struct EngineHandle {
    stdin: Mutex<Option<ChildStdin>>,
}

// Dev-only: CARGO_MANIFEST_DIR is baked in at compile time, which is exactly
// why this whole path only works on this machine, in this exact folder --
// see spawn_bundled_engine_process() below for the release-mode equivalent
// that doesn't have that problem.
#[cfg(debug_assertions)]
fn engine_project_dir() -> PathBuf {
    // In dev, the engine lives at ../../engine relative to src-tauri.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("engine")
}

// Windows only allocates a console for a newly-created process that doesn't
// already have one attached (CREATE_NO_WINDOW below). Without it, spawning
// python.exe -- a console-subsystem executable -- from our GUI-subsystem app
// makes Windows pop a brand new, empty conhost window for it: confirmed live
// by tracing process trees (conhost.exe's parent was the python.exe engine
// child, itself a child of veloci.exe), which is exactly the flash-of-a-
// terminal users see on every launch even though the app window itself has
// no console. This flag only affects *new* console allocation for the child;
// it has no effect on the stdio pipes below, which are unrelated.
#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

// .venv/Scripts/python.exe (and pythonw.exe) are *both* tiny launcher stubs
// -- confirmed by tracing process trees, each one spawns a second, distinct
// python.exe at the base install path recorded in pyvenv.cfg's "home ="
// line. That second hop is entirely internal to the stub and doesn't
// propagate CREATE_NO_WINDOW, so Windows auto-allocates a console for it
// regardless of what flag we pass to the stub itself -- the console the
// user sees survives even with CREATE_NO_WINDOW set on the first hop.
// pythonw.exe doesn't help either: this uv-managed toolchain only ships a
// console-subsystem base interpreter, so pythonw.exe's stub redirects to
// the exact same base python.exe as the plain python.exe stub does.
//
// The fix is to skip the stub entirely and invoke that base interpreter
// directly (a single real hop, so CREATE_NO_WINDOW actually takes effect),
// while setting __PYVENV_LAUNCHER__ to the venv's own python.exe path --
// this is the same env var CPython's own launcher stub sets internally, and
// it's what makes the base interpreter still resolve sys.prefix/site-packages
// to *our* venv instead of the base install. Verified directly: with this
// set, `sys.prefix` reports the venv path and `import yt_dlp` succeeds, with
// zero new conhost.exe appearing.
#[cfg(debug_assertions)]
fn base_interpreter(venv_dir: &PathBuf) -> Option<PathBuf> {
    let cfg = std::fs::read_to_string(venv_dir.join("pyvenv.cfg")).ok()?;
    let home = cfg
        .lines()
        .find_map(|line| line.split_once('='))
        .filter(|(key, _)| key.trim().eq_ignore_ascii_case("home"))
        .map(|(_, value)| value.trim().to_string())?;
    let candidate = PathBuf::from(home).join("python.exe");
    candidate.exists().then_some(candidate)
}

#[cfg(debug_assertions)]
fn spawn_dev_engine_process() -> std::io::Result<Child> {
    let venv_dir = engine_project_dir().join(".venv");
    let venv_python = venv_dir.join("Scripts").join("python.exe");

    #[allow(unused_mut)]
    let mut command = match base_interpreter(&venv_dir) {
        Some(base_python) => {
            let mut cmd = Command::new(base_python);
            cmd.env("__PYVENV_LAUNCHER__", &venv_python);
            cmd
        }
        // pyvenv.cfg missing/unparseable: fall back to the stub directly
        // rather than failing to start at all. Shows a console (the bug
        // this whole function works around), but the engine still runs.
        None => Command::new(venv_python),
    };

    // Invoke as a module directly rather than "uv run python -m
    // veloci_engine.cli": uv run's own process (uv -> python -> python, at
    // least two layers deep) means killing *our* Child handle only kills the
    // outermost wrapper, leaking the real interpreter as an orphaned process
    // -- confirmed live while testing (a `.terminate()` on such a chain left
    // the actual engine running indefinitely, still holding the stdio pipes
    // open). The venv already exists by the time this runs, so uv isn't
    // needed at runtime, only at dev-setup time.
    command
        .args(["-m", "veloci_engine.cli"])
        .current_dir(engine_project_dir())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    command.spawn()
}

// Release builds bundle a PyInstaller-frozen, fully standalone build of the
// same engine as a Tauri "sidecar" (see tauri.conf.json's bundle.externalBin
// and src-tauri/binaries/) -- no Python, uv, or venv needed on the target
// machine, unlike spawn_dev_engine_process() above which only works on this
// exact machine (CARGO_MANIFEST_DIR is a compile-time absolute path). Tauri
// installs sidecar binaries in the same directory as the main executable,
// stripped of their build-time target-triple suffix.
#[cfg(not(debug_assertions))]
fn spawn_bundled_engine_process() -> std::io::Result<Child> {
    let exe_dir = std::env::current_exe()?
        .parent()
        .expect("the running executable has no parent directory")
        .to_path_buf();
    let sidecar = exe_dir.join(if cfg!(windows) {
        "veloci-engine.exe"
    } else {
        "veloci-engine"
    });

    let mut command = Command::new(sidecar);
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    command.spawn()
}

fn spawn_engine_process() -> std::io::Result<Child> {
    #[cfg(debug_assertions)]
    {
        spawn_dev_engine_process()
    }
    #[cfg(not(debug_assertions))]
    {
        spawn_bundled_engine_process()
    }
}

pub fn start(app: &AppHandle) {
    let mut child = spawn_engine_process().expect("failed to start veloci_engine sidecar");

    let stdin = child.stdin.take().expect("child stdin was not piped");
    let stdout = child.stdout.take().expect("child stdout was not piped");
    let stderr = child.stderr.take().expect("child stderr was not piped");

    app.manage(EngineHandle {
        stdin: Mutex::new(Some(stdin)),
    });

    let stdout_app = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if line.trim().is_empty() {
                continue;
            }
            match serde_json::from_str::<Value>(&line) {
                Ok(payload) => {
                    let _ = stdout_app.emit("engine-event", payload);
                }
                Err(err) => {
                    let _ = stdout_app.emit(
                        "engine-event",
                        serde_json::json!({
                            "event": "error",
                            "message": format!("malformed engine output: {err}: {line}"),
                        }),
                    );
                }
            }
        }
    });

    // Surface stderr (tracebacks, yt-dlp warnings that slip past stdout) as
    // engine-log events instead of silently discarding them.
    let stderr_app = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            let _ = stderr_app.emit("engine-log", line);
        }
    });

    // Reap the child in the background so it doesn't become a zombie if it
    // exits on its own (crash, or stdin closed).
    std::thread::spawn(move || {
        let _ = child.wait();
    });
}

#[tauri::command]
pub fn send_engine_command(
    handle: tauri::State<EngineHandle>,
    payload: Value,
) -> Result<(), String> {
    let mut guard = handle.stdin.lock().map_err(|e| e.to_string())?;
    let stdin = guard.as_mut().ok_or("engine process is not running")?;
    let mut line = serde_json::to_string(&payload).map_err(|e| e.to_string())?;
    line.push('\n');
    stdin.write_all(line.as_bytes()).map_err(|e| e.to_string())?;
    stdin.flush().map_err(|e| e.to_string())
}
