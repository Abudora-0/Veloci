//! Spawns the Python engine (veloci_engine.cli) as a child process and
//! bridges its newline-delimited JSON stdio protocol to the frontend:
//! commands go in via `send_engine_command`, events come out as
//! "engine-event" Tauri events.

use std::io::{BufRead, BufReader, Write};
#[cfg(debug_assertions)]
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Sender};
use std::sync::Mutex;

use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager};

pub struct EngineHandle {
    // A dedicated writer thread (spawned in start()) owns the actual
    // ChildStdin and drains this channel -- send_engine_command only ever
    // enqueues here, it never touches the pipe itself. Sender::send on an
    // unbounded channel doesn't block, so a stalled/slow engine can no
    // longer serialize every queued command behind one blocking pipe
    // write. None means no engine process is running at all (spawn
    // failed), which send_engine_command reports the same way a closed
    // channel does: "engine process is not running".
    writer: Mutex<Option<Sender<String>>>,
    // Kept alive for the app's whole lifetime -- see ProcessTreeJob's docs
    // below. Never read again after construction; the underscore reflects
    // that its only job is to not get dropped early.
    _job: Option<ProcessTreeJob>,
}

// std::process::Command alone never kills a child's own descendants on
// Windows -- confirmed live: closing the app window left the engine (and,
// one level deeper, whatever yt-dlp subprocess it had spawned for an active
// download) running indefinitely as orphans. A Job Object with
// KILL_ON_JOB_CLOSE fixes this at the OS level: every process assigned to
// the job dies the instant its last handle closes, which happens
// automatically when our own process exits for *any* reason (clean close,
// crash, or the user force-killing veloci.exe via Task Manager) -- Windows
// tears down a process's handle table on exit regardless of whether any
// Rust Drop code gets a chance to run, so this doesn't depend on a
// graceful-shutdown hook existing at all.
#[cfg(target_os = "windows")]
mod process_tree_job {
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    pub struct ProcessTreeJob(HANDLE);

    // A job object handle has no thread affinity; every Win32 call it's
    // used with here is documented as safe from any thread.
    unsafe impl Send for ProcessTreeJob {}
    unsafe impl Sync for ProcessTreeJob {}

    impl ProcessTreeJob {
        pub fn new() -> Option<Self> {
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if handle.is_null() {
                    return None;
                }
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const _,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if ok == 0 {
                    CloseHandle(handle);
                    return None;
                }
                Some(Self(handle))
            }
        }

        // A new process spawned by a job member is, by default, automatically
        // added to the same job -- this single assignment on the immediate
        // child is enough to also cover the PyInstaller sidecar's own
        // unpacked child process (see engine.rs module docs: onefile builds
        // are a bootloader plus a second, unpacked process) and every
        // yt-dlp subprocess spawned per download, without touching any of
        // that spawn code.
        pub fn add(&self, child: &Child) -> bool {
            unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE) != 0 }
        }
    }

    impl Drop for ProcessTreeJob {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}

#[cfg(target_os = "windows")]
use process_tree_job::ProcessTreeJob;

#[cfg(not(target_os = "windows"))]
struct ProcessTreeJob;

#[cfg(not(target_os = "windows"))]
impl ProcessTreeJob {
    fn new() -> Option<Self> {
        Some(Self)
    }
    fn add(&self, _child: &Child) -> bool {
        true
    }
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

// Logs an emit() failure instead of silently discarding it (the previous
// `let _ = app.emit(...)` left zero trace if this ever failed, e.g. the
// window having already been destroyed while a reader/writer thread was
// still running) -- there's no logging crate wired up here, so stderr is
// the best available trail for a future "events stopped arriving" report.
fn emit_or_log(app: &AppHandle, event: &str, payload: impl serde::Serialize + Clone) {
    if let Err(err) = app.emit(event, payload) {
        eprintln!("failed to emit \"{event}\": {err}");
    }
}

pub fn start(app: &AppHandle) {
    let mut child = match spawn_engine_process() {
        Ok(child) => child,
        Err(err) => {
            // A PyInstaller-frozen sidecar getting quarantined by
            // antivirus, or simply missing/corrupted, used to `.expect()`
            // its way into panicking the whole app during setup -- instead,
            // log it and register a disabled handle so send_engine_command
            // reports "engine process is not running" (its existing error
            // path for a None writer) the moment the frontend tries to use
            // it, and the window still opens rather than the app failing
            // to launch outright.
            eprintln!("failed to start veloci_engine sidecar: {err}");
            app.manage(EngineHandle {
                writer: Mutex::new(None),
                _job: None,
            });
            return;
        }
    };

    // Best-effort: an older Windows without job-nesting support, or the
    // rare case where job creation itself fails, just means we're back to
    // the old orphaning behavior rather than the app failing to start.
    let job = ProcessTreeJob::new();
    if let Some(job) = &job {
        let _ = job.add(&child);
    }

    let mut stdin = child.stdin.take().expect("child stdin was not piped");
    let stdout = child.stdout.take().expect("child stdout was not piped");
    let stderr = child.stderr.take().expect("child stderr was not piped");

    // Dedicated writer thread: send_engine_command only ever enqueues a
    // line here (never blocks), while this thread does the actual
    // synchronous pipe write -- so one slow/stalled write can no longer
    // serialize every other queued command behind it.
    let (tx, rx) = mpsc::channel::<String>();
    let writer_app = app.clone();
    std::thread::spawn(move || {
        for line in rx {
            if let Err(err) = stdin.write_all(line.as_bytes()).and_then(|_| stdin.flush()) {
                emit_or_log(
                    &writer_app,
                    "engine-event",
                    serde_json::json!({
                        "event": "error",
                        "message": format!("failed to write to engine: {err}"),
                    }),
                );
            }
        }
    });

    app.manage(EngineHandle {
        writer: Mutex::new(Some(tx)),
        _job: job,
    });

    let stdout_app = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if line.trim().is_empty() {
                continue;
            }
            match serde_json::from_str::<Value>(&line) {
                Ok(payload) => {
                    emit_or_log(&stdout_app, "engine-event", payload);
                }
                Err(err) => {
                    emit_or_log(
                        &stdout_app,
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
            emit_or_log(&stderr_app, "engine-log", line);
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
    let mut line = serde_json::to_string(&payload).map_err(|e| e.to_string())?;
    line.push('\n');
    let guard = handle.writer.lock().map_err(|e| e.to_string())?;
    let tx = guard.as_ref().ok_or("engine process is not running")?;
    tx.send(line).map_err(|_| "engine process is not running".to_string())
}
