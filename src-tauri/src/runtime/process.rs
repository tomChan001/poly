use std::ffi::OsString;
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::future::Future;
use std::io::{self, Write as _};
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use base64::Engine as _;
use rand::TryRngCore as _;
use serde::Serialize;
use thiserror::Error;
use tokio::io::{AsyncBufRead, AsyncBufReadExt as _, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::mpsc;

use super::protocol::{RuntimeEvent, PROTOCOL_VERSION};

pub const MAX_EVENT_LINE_BYTES: usize = 64 * 1024;
pub const MAX_DIAGNOSTIC_FILE_BYTES: u64 = 1024 * 1024;
pub const DEFAULT_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(15);

type ChildFuture<'a> = Pin<Box<dyn Future<Output = io::Result<()>> + Send + 'a>>;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LaunchRequest {
    data_dir: PathBuf,
    runtime_dir: PathBuf,
    args: Vec<OsString>,
}

impl LaunchRequest {
    fn new(data_dir: PathBuf, runtime_dir: PathBuf) -> Self {
        Self {
            data_dir,
            runtime_dir,
            args: Vec::new(),
        }
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    pub fn runtime_dir(&self) -> &Path {
        &self.runtime_dir
    }

    pub fn args(&self) -> &[OsString] {
        &self.args
    }
}

pub trait RuntimeLauncher: Send + Sync {
    fn launch(&self, request: LaunchRequest) -> Result<Box<dyn RuntimeChild>, LaunchError>;
}

pub trait OwnedRuntimeCleanup: Send + Sync {
    fn signal(&self) -> io::Result<()>;
    fn confirm(&self) -> Pin<Box<dyn Future<Output = io::Result<()>> + Send + '_>>;
}

pub trait RuntimeChild: Send {
    fn write_stdin<'a>(&'a mut self, bytes: &'a [u8]) -> ChildFuture<'a>;
    fn take_stdout(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>>;
    fn take_stderr(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>>;
    /// Monitoring exit must never close or take the parent-owned stdin lease.
    /// The future is cancellation-safe and later `write_stdin` calls remain valid.
    fn wait_for_exit_preserving_stdin<'a>(&'a mut self) -> ChildFuture<'a>;
    fn kill_owned<'a>(&'a mut self) -> ChildFuture<'a>;
    /// Best-effort synchronous cleanup used only when the async owner is cancelled.
    /// Implementations must target only the child or process group captured at launch.
    fn signal_owned_on_drop(&mut self);
    fn owned_cleanup_handle(&self) -> Option<Arc<dyn OwnedRuntimeCleanup>> {
        None
    }
}

#[derive(Debug, Error)]
pub enum LaunchError {
    #[error("runtime resource directory is invalid")]
    InvalidResourceDirectory,
    #[error("runtime executable is missing")]
    MissingExecutable,
    #[error("runtime executable path contains a symbolic link")]
    SymbolicLink,
    #[error("runtime executable escaped the resource directory")]
    PathEscape,
    #[error("runtime resource is not a regular file")]
    NotAFile,
    #[error("runtime resource is not executable")]
    NotExecutable,
    #[error("runtime launch request contained arguments")]
    UnexpectedArguments,
    #[error("could not launch runtime: {0}")]
    Io(#[source] io::Error),
}

#[derive(Debug, Error)]
pub enum ProcessError {
    #[error("runtime directory must be absolute")]
    DirectoryNotAbsolute(PathBuf),
    #[error("could not generate runtime launch token")]
    TokenGeneration,
    #[error(transparent)]
    Launch(#[from] LaunchError),
    #[error("runtime process I/O failed")]
    Io(#[source] io::Error),
    #[error("runtime exited before accepting its start command")]
    StartCommandWrite {
        kind: io::ErrorKind,
        cleanup_failed: bool,
    },
    #[error("runtime event exceeded 64 KiB")]
    EventLineTooLong,
    #[error("runtime event stream ended mid-line")]
    UnexpectedEof,
    #[error("runtime protocol event was invalid")]
    Protocol,
    #[error("runtime process did not expose {0}")]
    MissingPipe(&'static str),
    #[error("runtime shutdown timed out and owned process cleanup failed")]
    ForcedCleanup(#[source] io::Error),
    #[error("runtime shutdown failed during {stage:?}; owned cleanup failed: {cleanup_failed}")]
    ShutdownFailed {
        stage: ShutdownStage,
        cleanup_failed: bool,
        #[source]
        source: ShutdownIoError,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ShutdownStage {
    WriteCommand,
    Wait,
}

pub struct ShutdownIoError {
    source: io::Error,
}

impl ShutdownIoError {
    pub fn kind(&self) -> io::ErrorKind {
        self.source.kind()
    }
}

impl fmt::Debug for ShutdownIoError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ShutdownIoError")
            .field("kind", &self.source.kind())
            .finish()
    }
}

impl fmt::Display for ShutdownIoError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("runtime process I/O failed")
    }
}

impl std::error::Error for ShutdownIoError {}

impl ProcessError {
    pub const fn shutdown_stage(&self) -> Option<ShutdownStage> {
        match self {
            Self::ShutdownFailed { stage, .. } => Some(*stage),
            _ => None,
        }
    }

    pub fn shutdown_source_kind(&self) -> Option<io::ErrorKind> {
        match self {
            Self::ShutdownFailed { source, .. } => Some(source.kind()),
            _ => None,
        }
    }
}

#[derive(Serialize)]
struct StartCommand<'a> {
    version: u8,
    command: &'static str,
    data_dir: &'a Path,
    runtime_dir: &'a Path,
    launch_token: &'a str,
}

#[derive(Serialize)]
struct ShutdownCommand {
    version: u8,
    command: &'static str,
}

pub fn generate_launch_token() -> Result<String, ProcessError> {
    let mut bytes = [0_u8; 32];
    rand::rngs::OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|_| ProcessError::TokenGeneration)?;
    Ok(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes))
}

fn command_line<T: Serialize>(command: &T) -> Result<Vec<u8>, ProcessError> {
    let mut line = serde_json::to_vec(command)
        .map_err(|error| ProcessError::Io(io::Error::new(io::ErrorKind::InvalidData, error)))?;
    line.push(b'\n');
    Ok(line)
}

pub async fn launch_runtime<L: RuntimeLauncher>(
    launcher: &L,
    data_dir: &Path,
    runtime_dir: &Path,
) -> Result<RunningRuntime, ProcessError> {
    validate_absolute_directory(data_dir)?;
    validate_absolute_directory(runtime_dir)?;

    let launch_token = generate_launch_token()?;
    let request = LaunchRequest::new(data_dir.to_path_buf(), runtime_dir.to_path_buf());
    let line = command_line(&StartCommand {
        version: PROTOCOL_VERSION,
        command: "start",
        data_dir,
        runtime_dir,
        launch_token: &launch_token,
    })?;
    let child = launcher.launch(request)?;
    let cleanup_armed = Arc::new(AtomicBool::new(true));
    let owned_cleanup = child.owned_cleanup_handle().map(|cleanup| {
        Arc::new(DisarmingOwnedCleanup {
            cleanup,
            armed: Arc::clone(&cleanup_armed),
        }) as Arc<dyn OwnedRuntimeCleanup>
    });
    let mut running = RunningRuntime {
        child,
        cleanup_armed,
        owned_cleanup,
    };
    if let Err(source) = running.child.write_stdin(&line).await {
        let kind = source.kind();
        let cleanup_failed = running.force_owned_cleanup().await.is_err();
        return Err(ProcessError::StartCommandWrite {
            kind,
            cleanup_failed,
        });
    }

    Ok(running)
}

fn validate_absolute_directory(path: &Path) -> Result<(), ProcessError> {
    if path.is_absolute() {
        Ok(())
    } else {
        Err(ProcessError::DirectoryNotAbsolute(path.to_path_buf()))
    }
}

pub struct RunningRuntime {
    child: Box<dyn RuntimeChild>,
    cleanup_armed: Arc<AtomicBool>,
    owned_cleanup: Option<Arc<dyn OwnedRuntimeCleanup>>,
}

impl Drop for RunningRuntime {
    fn drop(&mut self) {
        if self.cleanup_armed.swap(false, Ordering::AcqRel) {
            self.child.signal_owned_on_drop();
        }
    }
}

impl fmt::Debug for RunningRuntime {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RunningRuntime")
            .finish_non_exhaustive()
    }
}

impl RunningRuntime {
    pub fn owned_cleanup_handle(&self) -> Option<Arc<dyn OwnedRuntimeCleanup>> {
        self.owned_cleanup.clone()
    }
    pub fn take_stdout(&mut self) -> Result<Box<dyn AsyncBufRead + Send + Unpin>, ProcessError> {
        self.child
            .take_stdout()
            .ok_or(ProcessError::MissingPipe("stdout"))
    }

    pub fn take_stderr(&mut self) -> Result<Box<dyn AsyncBufRead + Send + Unpin>, ProcessError> {
        self.child
            .take_stderr()
            .ok_or(ProcessError::MissingPipe("stderr"))
    }

    pub async fn shutdown(&mut self) -> Result<(), ProcessError> {
        self.shutdown_with_timeout(DEFAULT_SHUTDOWN_TIMEOUT).await
    }

    pub async fn wait(&mut self) -> Result<(), ProcessError> {
        self.child
            .wait_for_exit_preserving_stdin()
            .await
            .map_err(ProcessError::Io)
    }

    pub async fn force_owned_cleanup(&mut self) -> Result<(), ProcessError> {
        if self
            .cleanup_armed
            .compare_exchange(true, false, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Ok(());
        }
        match self.child.kill_owned().await {
            Ok(()) => Ok(()),
            Err(error) => {
                self.cleanup_armed.store(true, Ordering::Release);
                Err(ProcessError::ForcedCleanup(error))
            }
        }
    }

    pub async fn shutdown_with_timeout(&mut self, timeout: Duration) -> Result<(), ProcessError> {
        let line = command_line(&ShutdownCommand {
            version: PROTOCOL_VERSION,
            command: "shutdown",
        })?;
        if let Err(source) = self.child.write_stdin(&line).await {
            let cleanup_failed = self.force_owned_cleanup().await.is_err();
            return Err(ProcessError::ShutdownFailed {
                stage: ShutdownStage::WriteCommand,
                cleanup_failed,
                source: ShutdownIoError { source },
            });
        }

        if !timeout.is_zero() {
            if let Ok(result) =
                tokio::time::timeout(timeout, self.child.wait_for_exit_preserving_stdin()).await
            {
                match result {
                    Ok(()) => return self.force_owned_cleanup().await,
                    Err(source) => {
                        let cleanup_failed = self.force_owned_cleanup().await.is_err();
                        return Err(ProcessError::ShutdownFailed {
                            stage: ShutdownStage::Wait,
                            cleanup_failed,
                            source: ShutdownIoError { source },
                        });
                    }
                }
            }
        }

        self.force_owned_cleanup().await
    }
}

struct DisarmingOwnedCleanup {
    cleanup: Arc<dyn OwnedRuntimeCleanup>,
    armed: Arc<AtomicBool>,
}

impl OwnedRuntimeCleanup for DisarmingOwnedCleanup {
    fn signal(&self) -> io::Result<()> {
        if self
            .armed
            .compare_exchange(true, false, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Ok(());
        }
        match self.cleanup.signal() {
            Ok(()) => Ok(()),
            Err(error) => {
                self.armed.store(true, Ordering::Release);
                Err(error)
            }
        }
    }

    fn confirm(&self) -> Pin<Box<dyn Future<Output = io::Result<()>> + Send + '_>> {
        self.cleanup.confirm()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RuntimeStreamItem {
    Event(RuntimeEvent),
    Eof,
}

pub async fn read_runtime_event<R>(reader: &mut R) -> Result<RuntimeStreamItem, ProcessError>
where
    R: AsyncBufRead + Unpin,
{
    let mut line = Vec::new();
    loop {
        let buffer = reader.fill_buf().await.map_err(ProcessError::Io)?;
        if buffer.is_empty() {
            return if line.is_empty() {
                Ok(RuntimeStreamItem::Eof)
            } else {
                Err(ProcessError::UnexpectedEof)
            };
        }

        let newline = buffer.iter().position(|&byte| byte == b'\n');
        let content_length = newline.unwrap_or(buffer.len());
        let framing_length = usize::from(newline.is_some());
        if line.len() + content_length + framing_length > MAX_EVENT_LINE_BYTES {
            return Err(ProcessError::EventLineTooLong);
        }
        line.extend_from_slice(&buffer[..content_length]);
        let consumed = content_length + usize::from(newline.is_some());
        reader.consume(consumed);

        if newline.is_some() {
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            let text = std::str::from_utf8(&line).map_err(|_| ProcessError::Protocol)?;
            let event = RuntimeEvent::parse_line(text).map_err(|_| ProcessError::Protocol)?;
            return Ok(RuntimeStreamItem::Event(event));
        }
    }
}

pub async fn publish_runtime_events<R>(
    mut reader: R,
    sender: mpsc::Sender<RuntimeEvent>,
) -> Result<(), ProcessError>
where
    R: AsyncBufRead + Unpin,
{
    loop {
        match read_runtime_event(&mut reader).await? {
            RuntimeStreamItem::Event(event) => {
                if sender.send(event).await.is_err() {
                    return Ok(());
                }
            }
            RuntimeStreamItem::Eof => return Ok(()),
        }
    }
}

pub struct ProductionLauncher {
    resource_dir: PathBuf,
}

impl ProductionLauncher {
    pub fn new(resource_dir: PathBuf) -> Self {
        Self { resource_dir }
    }
}

impl RuntimeLauncher for ProductionLauncher {
    fn launch(&self, request: LaunchRequest) -> Result<Box<dyn RuntimeChild>, LaunchError> {
        if !request.args.is_empty() {
            return Err(LaunchError::UnexpectedArguments);
        }
        let executable = resolve_runtime_executable(&self.resource_dir)?;
        let mut command = Command::new(executable);
        command
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true);
        copy_allowlisted_environment(&mut command);
        configure_process_group(&mut command);
        let child = command.spawn().map_err(LaunchError::Io)?;
        Ok(Box::new(ProductionChild::new(child)))
    }
}

fn copy_allowlisted_environment(command: &mut Command) {
    const ALLOWLIST: &[&str] = &[
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "HOME",
        "USER",
        "LOGNAME",
        "SECURITYSESSIONID",
    ];
    for name in ALLOWLIST {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
}

fn resolve_runtime_executable(resource_dir: &Path) -> Result<PathBuf, LaunchError> {
    let root = resource_dir
        .canonicalize()
        .map_err(|_| LaunchError::InvalidResourceDirectory)?;
    let bundle_dir = resource_dir.join("poly-runtime");
    let executable = bundle_dir.join("poly-runtime");

    reject_symlink(&bundle_dir)?;
    reject_symlink(&executable)?;
    let resolved = executable
        .canonicalize()
        .map_err(|_| LaunchError::MissingExecutable)?;
    if !resolved.starts_with(&root) {
        return Err(LaunchError::PathEscape);
    }
    let metadata = fs::metadata(&resolved).map_err(LaunchError::Io)?;
    if !metadata.is_file() {
        return Err(LaunchError::NotAFile);
    }
    validate_executable_permissions(&metadata)?;
    Ok(resolved)
}

fn reject_symlink(path: &Path) -> Result<(), LaunchError> {
    let metadata = fs::symlink_metadata(path).map_err(|error| {
        if error.kind() == io::ErrorKind::NotFound {
            LaunchError::MissingExecutable
        } else {
            LaunchError::Io(error)
        }
    })?;
    if metadata.file_type().is_symlink() {
        Err(LaunchError::SymbolicLink)
    } else {
        Ok(())
    }
}

#[cfg(unix)]
fn validate_executable_permissions(metadata: &fs::Metadata) -> Result<(), LaunchError> {
    use std::os::unix::fs::PermissionsExt as _;

    if metadata.permissions().mode() & 0o111 == 0 {
        Err(LaunchError::NotExecutable)
    } else {
        Ok(())
    }
}

#[cfg(not(unix))]
fn validate_executable_permissions(_metadata: &fs::Metadata) -> Result<(), LaunchError> {
    Ok(())
}

#[cfg(unix)]
fn configure_process_group(command: &mut Command) {
    command.process_group(0);
}

#[cfg(not(unix))]
fn configure_process_group(_command: &mut Command) {}

struct ProductionChild {
    child: Child,
    stdin: Option<tokio::process::ChildStdin>,
    #[cfg(unix)]
    process_group: Option<i32>,
}

impl ProductionChild {
    fn new(mut child: Child) -> Self {
        #[cfg(unix)]
        let process_group = child.id().and_then(|id| i32::try_from(id).ok());
        let stdin = child.stdin.take();
        Self {
            child,
            stdin,
            #[cfg(unix)]
            process_group,
        }
    }
}

impl RuntimeChild for ProductionChild {
    fn write_stdin<'a>(&'a mut self, bytes: &'a [u8]) -> ChildFuture<'a> {
        Box::pin(async move {
            let stdin = self
                .stdin
                .as_mut()
                .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "stdin unavailable"))?;
            tokio::io::AsyncWriteExt::write_all(stdin, bytes).await?;
            tokio::io::AsyncWriteExt::flush(stdin).await
        })
    }

    fn take_stdout(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>> {
        self.child
            .stdout
            .take()
            .map(|stdout| Box::new(BufReader::new(stdout)) as Box<_>)
    }

    fn take_stderr(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>> {
        self.child
            .stderr
            .take()
            .map(|stderr| Box::new(BufReader::new(stderr)) as Box<_>)
    }

    fn wait_for_exit_preserving_stdin<'a>(&'a mut self) -> ChildFuture<'a> {
        Box::pin(async move { self.child.wait().await.map(|_| ()) })
    }

    fn kill_owned<'a>(&'a mut self) -> ChildFuture<'a> {
        Box::pin(async move {
            #[cfg(unix)]
            let process_group = self.process_group;
            kill_owned_process_group(self).await?;
            self.child.wait().await?;
            #[cfg(unix)]
            if let Some(group) = process_group {
                confirm_owned_process_group_gone(group).await?;
            }
            Ok(())
        })
    }

    fn signal_owned_on_drop(&mut self) {
        signal_owned_process_group(self);
    }

    fn owned_cleanup_handle(&self) -> Option<Arc<dyn OwnedRuntimeCleanup>> {
        #[cfg(unix)]
        {
            self.process_group.map(|group| {
                Arc::new(ProductionOwnedCleanup { group }) as Arc<dyn OwnedRuntimeCleanup>
            })
        }
        #[cfg(not(unix))]
        {
            None
        }
    }
}

#[cfg(unix)]
struct ProductionOwnedCleanup {
    group: i32,
}

#[cfg(unix)]
impl OwnedRuntimeCleanup for ProductionOwnedCleanup {
    fn signal(&self) -> io::Result<()> {
        signal_exact_process_group(self.group)
    }

    fn confirm(&self) -> Pin<Box<dyn Future<Output = io::Result<()>> + Send + '_>> {
        Box::pin(confirm_owned_process_group_gone(self.group))
    }
}

#[cfg(unix)]
fn signal_exact_process_group(group: i32) -> io::Result<()> {
    const SIGKILL: i32 = 9;
    extern "C" {
        fn kill(pid: i32, signal: i32) -> i32;
    }

    let result = unsafe { kill(-group, SIGKILL) };
    if result == 0 {
        Ok(())
    } else {
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(3) {
            Ok(())
        } else {
            Err(error)
        }
    }
}

#[cfg(unix)]
fn signal_owned_process_group(child: &mut ProductionChild) {
    if let Some(group) = child.process_group {
        // The group id was captured from this exact child after it became group leader.
        let _ = signal_exact_process_group(group);
    } else {
        let _ = child.child.start_kill();
    }
}

#[cfg(not(unix))]
fn signal_owned_process_group(child: &mut ProductionChild) {
    let _ = child.child.start_kill();
}

#[cfg(unix)]
async fn kill_owned_process_group(child: &mut ProductionChild) -> io::Result<()> {
    const SIGKILL: i32 = 9;
    extern "C" {
        fn kill(pid: i32, signal: i32) -> i32;
    }

    let Some(group) = child.process_group else {
        return child.child.kill().await;
    };
    // The group id was captured from this exact child after spawning it as a new group leader.
    let result = unsafe { kill(-group, SIGKILL) };
    if result == 0 {
        Ok(())
    } else {
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(3) {
            Ok(())
        } else {
            Err(error)
        }
    }
}

#[cfg(any(unix, test))]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ProcessGroupPresence {
    Present,
    Gone,
}

#[cfg(any(unix, test))]
fn classify_process_group_probe(
    result: i32,
    raw_os_error: Option<i32>,
) -> io::Result<ProcessGroupPresence> {
    const EPERM: i32 = 1;
    const ESRCH: i32 = 3;

    if result == 0 || raw_os_error == Some(EPERM) {
        Ok(ProcessGroupPresence::Present)
    } else if raw_os_error == Some(ESRCH) {
        Ok(ProcessGroupPresence::Gone)
    } else {
        Err(raw_os_error.map_or_else(io::Error::last_os_error, io::Error::from_raw_os_error))
    }
}

#[cfg(unix)]
async fn confirm_owned_process_group_gone(group: i32) -> io::Result<()> {
    const PROBE_SIGNAL: i32 = 0;
    const CONFIRMATION_TIMEOUT: Duration = Duration::from_secs(1);
    const PROBE_INTERVAL: Duration = Duration::from_millis(10);
    extern "C" {
        fn kill(pid: i32, signal: i32) -> i32;
    }

    let deadline = tokio::time::Instant::now() + CONFIRMATION_TIMEOUT;
    loop {
        let result = unsafe { kill(-group, PROBE_SIGNAL) };
        let raw_os_error = (result != 0)
            .then(|| io::Error::last_os_error().raw_os_error())
            .flatten();
        match classify_process_group_probe(result, raw_os_error)? {
            ProcessGroupPresence::Gone => return Ok(()),
            ProcessGroupPresence::Present if tokio::time::Instant::now() >= deadline => {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "owned process group did not exit",
                ));
            }
            ProcessGroupPresence::Present => tokio::time::sleep(PROBE_INTERVAL).await,
        }
    }
}

#[cfg(not(unix))]
async fn kill_owned_process_group(child: &mut ProductionChild) -> io::Result<()> {
    child.child.kill().await
}

pub struct DiagnosticLog {
    path: PathBuf,
    rotated_path: PathBuf,
    file: File,
    length: u64,
}

impl DiagnosticLog {
    pub fn under_application_support(application_support: &Path) -> io::Result<Self> {
        let directory = application_support.join("Poly").join("logs");
        fs::create_dir_all(&directory)?;
        let path = directory.join("runtime.stderr.log");
        let rotated_path = directory.join("runtime.stderr.log.1");
        bound_existing_file(&path)?;
        bound_existing_file(&rotated_path)?;
        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        let length = file.metadata()?.len();
        Ok(Self {
            path,
            rotated_path,
            file,
            length,
        })
    }

    pub fn write_redacted(&mut self, diagnostic: &str) -> io::Result<()> {
        let redacted = redact_diagnostic_line(diagnostic);
        let maximum = MAX_DIAGNOSTIC_FILE_BYTES as usize;
        let end = if redacted.len() > maximum {
            let mut boundary = maximum;
            while !redacted.is_char_boundary(boundary) {
                boundary -= 1;
            }
            boundary
        } else {
            redacted.len()
        };
        let bytes = &redacted.as_bytes()[..end];
        if self.length + bytes.len() as u64 > MAX_DIAGNOSTIC_FILE_BYTES {
            self.rotate()?;
        }
        self.file.write_all(bytes)?;
        self.length += bytes.len() as u64;
        Ok(())
    }

    fn rotate(&mut self) -> io::Result<()> {
        self.file.flush()?;
        if self.rotated_path.exists() {
            fs::remove_file(&self.rotated_path)?;
        }
        if self.path.exists() {
            fs::rename(&self.path, &self.rotated_path)?;
        }
        self.file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&self.path)?;
        self.length = 0;
        Ok(())
    }
}

fn bound_existing_file(path: &Path) -> io::Result<()> {
    match OpenOptions::new().write(true).open(path) {
        Ok(file) => {
            if file.metadata()?.len() > MAX_DIAGNOSTIC_FILE_BYTES {
                file.set_len(MAX_DIAGNOSTIC_FILE_BYTES)?;
            }
            Ok(())
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

pub async fn drain_stderr<R>(mut reader: R, mut log: DiagnosticLog) -> io::Result<()>
where
    R: AsyncBufRead + Unpin,
{
    while let Some(line) = read_diagnostic_line(&mut reader).await? {
        let text = String::from_utf8_lossy(&line);
        log.write_redacted(&text)?;
    }
    Ok(())
}

async fn read_diagnostic_line<R>(reader: &mut R) -> io::Result<Option<Vec<u8>>>
where
    R: AsyncBufRead + Unpin,
{
    const TRUNCATED: &[u8] = b"[diagnostic line truncated]\n";

    let mut line = Vec::new();
    let mut truncated = false;
    loop {
        let buffer = reader.fill_buf().await?;
        if buffer.is_empty() {
            return if line.is_empty() && !truncated {
                Ok(None)
            } else if truncated {
                Ok(Some(TRUNCATED.to_vec()))
            } else {
                Ok(Some(line))
            };
        }

        let newline = buffer.iter().position(|&byte| byte == b'\n');
        let consumed = newline.map_or(buffer.len(), |index| index + 1);
        if !truncated {
            if line.len() + consumed <= MAX_DIAGNOSTIC_FILE_BYTES as usize {
                line.extend_from_slice(&buffer[..consumed]);
            } else {
                line.clear();
                truncated = true;
            }
        }
        reader.consume(consumed);

        if newline.is_some() {
            return if truncated {
                Ok(Some(TRUNCATED.to_vec()))
            } else {
                Ok(Some(line))
            };
        }
    }
}

const SENSITIVE_KEYS: &[&str] = &[
    "authorization",
    "cookie",
    "privatekey",
    "secret",
    "signature",
    "token",
    "apikey",
    "apitoken",
    "apisecret",
    "password",
    "passphrase",
    "clientsecret",
    "accesstoken",
    "credential",
    "credentials",
    "refreshtoken",
];

pub fn redact_diagnostic_line(line: &str) -> String {
    let (content, line_ending) = if let Some(content) = line.strip_suffix("\r\n") {
        (content, "\r\n")
    } else if let Some(content) = line.strip_suffix('\n') {
        (content, "\n")
    } else {
        (line, "")
    };

    if let Ok(mut value) = serde_json::from_str::<serde_json::Value>(content) {
        redact_json_value(&mut value);
        if let Ok(mut serialized) = serde_json::to_string(&value) {
            serialized.push_str(line_ending);
            return serialized;
        }
    }

    if contains_sensitive_assignment(content) {
        let mut redacted = "[REDACTED]".to_owned();
        redacted.push_str(line_ending);
        return redacted;
    }

    let mut result = Vec::new();
    let mut suppress_value = false;
    for word in content.split_whitespace() {
        if let Some((key, delimiter, value)) = split_field(word) {
            suppress_value = false;
            if is_sensitive_key(key) {
                if value.is_empty() {
                    let mut redacted = "[REDACTED]".to_owned();
                    redacted.push_str(line_ending);
                    return redacted;
                }
                result.push(format!("{key}{delimiter}[REDACTED]"));
                suppress_value = value.is_empty() || key.eq_ignore_ascii_case("authorization");
            } else {
                result.push(word.to_owned());
            }
        } else if !suppress_value {
            result.push(word.to_owned());
        }
    }
    let mut redacted = result.join(" ");
    redacted.push_str(line_ending);
    redacted
}

fn normalize_sensitive_key(key: &str) -> String {
    key.bytes()
        .filter(u8::is_ascii_alphanumeric)
        .map(|byte| byte.to_ascii_lowercase() as char)
        .collect()
}

fn is_sensitive_key(key: &str) -> bool {
    let normalized = normalize_sensitive_key(key);
    SENSITIVE_KEYS
        .iter()
        .any(|candidate| normalized == *candidate)
}

fn contains_sensitive_assignment(content: &str) -> bool {
    for (delimiter, _) in content.match_indices([':', '=']) {
        let prefix = content[..delimiter].trim_end();
        for (start, character) in prefix.char_indices() {
            let begins_key = character.is_ascii_alphanumeric()
                && (start == 0 || !prefix.as_bytes()[start - 1].is_ascii_alphanumeric());
            if begins_key && is_sensitive_key(&prefix[start..]) {
                return true;
            }
        }
    }
    false
}

fn redact_json_value(value: &mut serde_json::Value) {
    match value {
        serde_json::Value::Object(object) => {
            for (key, value) in object {
                if is_sensitive_key(key) {
                    *value = serde_json::Value::String("[REDACTED]".to_owned());
                } else {
                    redact_json_value(value);
                }
            }
        }
        serde_json::Value::Array(array) => {
            for value in array {
                redact_json_value(value);
            }
        }
        _ => {}
    }
}

fn split_field(word: &str) -> Option<(&str, char, &str)> {
    let index = word.find(['=', ':'])?;
    let key = &word[..index];
    if key.is_empty()
        || !key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
    {
        return None;
    }
    let delimiter = word.as_bytes()[index] as char;
    Some((key, delimiter, &word[index + 1..]))
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::future::Future;
    use std::io;
    use std::path::{Path, PathBuf};
    use std::pin::Pin;
    use std::sync::{Arc, Mutex};
    use std::task::{Context, Poll};
    use std::time::Duration;

    use base64::Engine as _;
    use tokio::io::{AsyncBufRead, AsyncRead, BufReader, ReadBuf};

    use super::*;

    type IoFuture<'a> = Pin<Box<dyn Future<Output = io::Result<()>> + Send + 'a>>;

    #[derive(Default)]
    struct FakeState {
        requests: Vec<LaunchRequest>,
        writes: Vec<Vec<u8>>,
        write_results: VecDeque<io::Result<()>>,
        write_pending: bool,
        waits: VecDeque<io::Result<()>>,
        killed: usize,
        exact_signals: usize,
        exact_confirmations: usize,
    }

    struct FakeLauncher(Arc<Mutex<FakeState>>);

    struct FakeChild(Arc<Mutex<FakeState>>);

    struct FakeOwnedCleanup(Arc<Mutex<FakeState>>);

    impl OwnedRuntimeCleanup for FakeOwnedCleanup {
        fn signal(&self) -> io::Result<()> {
            self.0.lock().unwrap().exact_signals += 1;
            Ok(())
        }

        fn confirm(&self) -> IoFuture<'_> {
            Box::pin(async move {
                self.0.lock().unwrap().exact_confirmations += 1;
                Ok(())
            })
        }
    }

    struct OneByteReader {
        bytes: Vec<u8>,
        offset: usize,
    }

    impl AsyncRead for OneByteReader {
        fn poll_read(
            mut self: Pin<&mut Self>,
            _context: &mut Context<'_>,
            buffer: &mut ReadBuf<'_>,
        ) -> Poll<io::Result<()>> {
            if let Some(byte) = self.bytes.get(self.offset).copied() {
                buffer.put_slice(&[byte]);
                self.offset += 1;
            }
            Poll::Ready(Ok(()))
        }
    }

    impl RuntimeLauncher for FakeLauncher {
        fn launch(&self, request: LaunchRequest) -> Result<Box<dyn RuntimeChild>, LaunchError> {
            self.0.lock().unwrap().requests.push(request);
            Ok(Box::new(FakeChild(Arc::clone(&self.0))))
        }
    }

    impl RuntimeChild for FakeChild {
        fn write_stdin<'a>(&'a mut self, bytes: &'a [u8]) -> IoFuture<'a> {
            let state = Arc::clone(&self.0);
            Box::pin(async move {
                let (write_pending, result) = {
                    let mut state = state.lock().unwrap();
                    state.writes.push(bytes.to_vec());
                    let write_pending = state.write_pending;
                    let result =
                        (!write_pending).then(|| state.write_results.pop_front().unwrap_or(Ok(())));
                    (write_pending, result)
                };
                if write_pending {
                    std::future::pending().await
                } else {
                    result.expect("non-pending write has a result")
                }
            })
        }

        fn take_stdout(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>> {
            None
        }

        fn take_stderr(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>> {
            None
        }

        fn wait_for_exit_preserving_stdin<'a>(&'a mut self) -> IoFuture<'a> {
            let result = self.0.lock().unwrap().waits.pop_front().unwrap_or(Ok(()));
            Box::pin(async move { result })
        }

        fn kill_owned<'a>(&'a mut self) -> IoFuture<'a> {
            let state = Arc::clone(&self.0);
            Box::pin(async move {
                state.lock().unwrap().killed += 1;
                Ok(())
            })
        }

        fn signal_owned_on_drop(&mut self) {
            self.0.lock().unwrap().killed += 1;
        }

        fn owned_cleanup_handle(&self) -> Option<Arc<dyn OwnedRuntimeCleanup>> {
            Some(Arc::new(FakeOwnedCleanup(Arc::clone(&self.0))))
        }
    }

    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap()
    }

    fn absolute_test_dirs() -> (PathBuf, PathBuf) {
        let root = std::env::current_dir().unwrap();
        (root.join("data"), root.join("runtime"))
    }

    #[test]
    fn start_uses_no_arguments_and_sends_one_versioned_json_line_to_stdin() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let launcher = FakeLauncher(Arc::clone(&state));
        let (data_dir, runtime_dir) = absolute_test_dirs();

        let _running = runtime()
            .block_on(launch_runtime(&launcher, &data_dir, &runtime_dir))
            .unwrap();

        let state = state.lock().unwrap();
        assert!(state.requests[0].args().is_empty());
        assert_eq!(state.writes.len(), 1);
        assert!(state.writes[0].ends_with(b"\n"));
        assert_eq!(
            state.writes[0]
                .iter()
                .filter(|&&byte| byte == b'\n')
                .count(),
            1
        );
        let json: serde_json::Value =
            serde_json::from_slice(&state.writes[0][..state.writes[0].len() - 1]).unwrap();
        assert_eq!(json["version"], 1);
        assert_eq!(json["command"], "start");
        assert_eq!(json["data_dir"], data_dir.to_string_lossy().as_ref());
        assert_eq!(json["runtime_dir"], runtime_dir.to_string_lossy().as_ref());
        assert_eq!(json.as_object().unwrap().len(), 5);
    }

    #[test]
    fn launch_token_is_256_bit_base64url_without_padding() {
        let token = generate_launch_token().unwrap();
        assert!(token.len() >= 43);
        assert!(!token.contains('='));
        let decoded = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(token)
            .unwrap();
        assert_eq!(decoded.len(), 32);
    }

    #[test]
    fn start_rejects_non_absolute_directories_before_launch() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let launcher = FakeLauncher(Arc::clone(&state));

        let error = runtime()
            .block_on(launch_runtime(
                &launcher,
                Path::new("relative-data"),
                Path::new("relative-runtime"),
            ))
            .unwrap_err();

        assert!(matches!(error, ProcessError::DirectoryNotAbsolute(_)));
        assert!(state.lock().unwrap().requests.is_empty());
    }

    #[test]
    fn event_reader_enforces_line_limit_parses_strict_events_and_reports_eof() {
        let valid = b"{\"version\":1,\"state\":\"initializing\"}\n";
        let mut reader = BufReader::new(&valid[..]);
        let first = runtime().block_on(read_runtime_event(&mut reader)).unwrap();
        assert!(matches!(
            first,
            RuntimeStreamItem::Event(RuntimeEvent::Initializing)
        ));
        assert_eq!(
            runtime().block_on(read_runtime_event(&mut reader)).unwrap(),
            RuntimeStreamItem::Eof
        );

        let invalid = b"not json\n";
        let mut reader = BufReader::new(&invalid[..]);
        assert!(matches!(
            runtime().block_on(read_runtime_event(&mut reader)),
            Err(ProcessError::Protocol)
        ));

        let oversized = vec![b'x'; MAX_EVENT_LINE_BYTES + 1];
        let mut reader = BufReader::new(&oversized[..]);
        assert!(matches!(
            runtime().block_on(read_runtime_event(&mut reader)),
            Err(ProcessError::EventLineTooLong)
        ));

        let base = b"{\"version\":1,\"state\":\"initializing\"}";
        let mut exactly_maximum = base.to_vec();
        exactly_maximum.resize(MAX_EVENT_LINE_BYTES - 1, b' ');
        exactly_maximum.push(b'\n');
        let mut reader = BufReader::new(exactly_maximum.as_slice());
        assert!(matches!(
            runtime().block_on(read_runtime_event(&mut reader)),
            Ok(RuntimeStreamItem::Event(RuntimeEvent::Initializing))
        ));

        let mut over_framing_limit = base.to_vec();
        over_framing_limit.resize(MAX_EVENT_LINE_BYTES, b' ');
        over_framing_limit.push(b'\n');
        let mut reader = BufReader::new(over_framing_limit.as_slice());
        assert!(matches!(
            runtime().block_on(read_runtime_event(&mut reader)),
            Err(ProcessError::EventLineTooLong)
        ));
    }

    #[test]
    fn diagnostic_redaction_hides_all_sensitive_key_values_case_insensitively() {
        let line = "Authorization=abc cookie=session PRIVATE_KEY=pem secret=hush Signature=sig ToKeN=tok safe=value";
        let redacted = redact_diagnostic_line(line);

        for value in ["abc", "session", "pem", "hush", "sig", "tok"] {
            assert!(!redacted.contains(value), "leaked {value}: {redacted}");
        }
        assert!(redacted.contains("[REDACTED]"));

        for ambiguous in [
            "cookie: session=abc safe=value",
            "Authorization: Bearer abc next=x",
            "token = split-secret safe=value",
            "INFO {\"token\":\"prefixed-secret\",\"safe\":\"visible\"}",
            "private_key : pem-secret",
            "SeCrEt = hush-secret",
            "signature: signature-secret",
        ] {
            let redacted = redact_diagnostic_line(ambiguous);
            for secret in [
                "session=abc",
                "Bearer abc",
                "split-secret",
                "prefixed-secret",
                "pem-secret",
                "hush-secret",
                "signature-secret",
            ] {
                assert!(!redacted.contains(secret), "leaked {secret}: {redacted}");
            }
        }

        let json = redact_diagnostic_line(
            r#"{"TOKEN":"json-secret","nested":{"private_key":"pem-secret"},"safe":"visible"}"#,
        );
        assert!(!json.contains("json-secret"));
        assert!(!json.contains("pem-secret"));
        assert!(json.contains("visible"));
    }

    #[test]
    fn graceful_shutdown_sends_command_and_forces_only_owned_child_after_timeout() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let launcher = FakeLauncher(Arc::clone(&state));
        let (data_dir, runtime_dir) = absolute_test_dirs();
        let mut running = runtime()
            .block_on(launch_runtime(&launcher, &data_dir, &runtime_dir))
            .unwrap();

        runtime()
            .block_on(running.shutdown_with_timeout(Duration::ZERO))
            .unwrap();

        let state = state.lock().unwrap();
        assert_eq!(state.killed, 1);
        let shutdown = state.writes.last().unwrap();
        assert_eq!(shutdown, b"{\"version\":1,\"command\":\"shutdown\"}\n");
    }

    #[test]
    fn cloneable_cleanup_handle_signals_and_confirms_the_exact_owned_group() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let launcher = FakeLauncher(Arc::clone(&state));
        let (data_dir, runtime_dir) = absolute_test_dirs();
        let running = runtime()
            .block_on(launch_runtime(&launcher, &data_dir, &runtime_dir))
            .unwrap();
        let cleanup = running.owned_cleanup_handle().unwrap();

        cleanup.signal().unwrap();
        runtime().block_on(cleanup.confirm()).unwrap();
        drop(running);

        let state = state.lock().unwrap();
        assert_eq!(state.exact_signals, 1);
        assert_eq!(state.exact_confirmations, 1);
        assert_eq!(state.killed, 0);
    }

    #[test]
    fn graceful_shutdown_confirms_the_owned_group_after_the_leader_exits() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let launcher = FakeLauncher(Arc::clone(&state));
        let (data_dir, runtime_dir) = absolute_test_dirs();
        let mut running = runtime()
            .block_on(launch_runtime(&launcher, &data_dir, &runtime_dir))
            .unwrap();

        runtime()
            .block_on(running.shutdown_with_timeout(Duration::from_secs(1)))
            .unwrap();

        drop(running);
        assert_eq!(state.lock().unwrap().killed, 1);
    }

    #[test]
    fn shutdown_write_failure_still_kills_the_owned_child() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let launcher = FakeLauncher(Arc::clone(&state));
        let (data_dir, runtime_dir) = absolute_test_dirs();
        let mut running = runtime()
            .block_on(launch_runtime(&launcher, &data_dir, &runtime_dir))
            .unwrap();
        state
            .lock()
            .unwrap()
            .write_results
            .push_back(Err(io::Error::new(io::ErrorKind::BrokenPipe, "closed")));

        let result = runtime().block_on(running.shutdown_with_timeout(Duration::from_secs(1)));

        assert!(result.is_err());
        drop(running);
        assert_eq!(state.lock().unwrap().killed, 1);
    }

    #[test]
    fn shutdown_wait_failure_still_kills_the_owned_child() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let launcher = FakeLauncher(Arc::clone(&state));
        let (data_dir, runtime_dir) = absolute_test_dirs();
        let mut running = runtime()
            .block_on(launch_runtime(&launcher, &data_dir, &runtime_dir))
            .unwrap();
        state
            .lock()
            .unwrap()
            .waits
            .push_back(Err(io::Error::other("wait failed")));

        let result = runtime().block_on(running.shutdown_with_timeout(Duration::from_secs(1)));

        assert!(result.is_err());
        drop(running);
        assert_eq!(state.lock().unwrap().killed, 1);
    }

    #[test]
    fn diagnostic_files_remain_bounded_when_one_message_is_oversized() {
        let root = std::env::temp_dir().join(format!(
            "poly-runtime-log-test-{}",
            generate_launch_token().unwrap()
        ));
        let mut log = DiagnosticLog::under_application_support(&root).unwrap();
        let oversized = "x".repeat(MAX_DIAGNOSTIC_FILE_BYTES as usize + 100);

        log.write_redacted(&oversized).unwrap();

        let log_dir = root.join("Poly").join("logs");
        assert!(
            fs::metadata(log_dir.join("runtime.stderr.log"))
                .unwrap()
                .len()
                <= MAX_DIAGNOSTIC_FILE_BYTES
        );
        if let Ok(metadata) = fs::metadata(log_dir.join("runtime.stderr.log.1")) {
            assert!(metadata.len() <= MAX_DIAGNOSTIC_FILE_BYTES);
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn stderr_redaction_is_safe_across_arbitrary_read_boundaries() {
        let root = std::env::temp_dir().join(format!(
            "poly-runtime-stream-test-{}",
            generate_launch_token().unwrap()
        ));
        let reader = OneByteReader {
            bytes: br#"{"token":"split-secret","safe":"visible"}
"#
            .to_vec(),
            offset: 0,
        };
        let log = DiagnosticLog::under_application_support(&root).unwrap();

        runtime()
            .block_on(drain_stderr(BufReader::new(reader), log))
            .unwrap();

        let path = root.join("Poly").join("logs").join("runtime.stderr.log");
        let contents = fs::read_to_string(path).unwrap();
        assert!(!contents.contains("split-secret"));
        assert!(contents.contains("[REDACTED]"));
        assert!(contents.contains("visible"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn opening_an_oversized_existing_log_restores_the_one_mib_bound() {
        let root = std::env::temp_dir().join(format!(
            "poly-runtime-existing-log-test-{}",
            generate_launch_token().unwrap()
        ));
        let log_dir = root.join("Poly").join("logs");
        fs::create_dir_all(&log_dir).unwrap();
        fs::write(
            log_dir.join("runtime.stderr.log"),
            vec![b'x'; MAX_DIAGNOSTIC_FILE_BYTES as usize + 1],
        )
        .unwrap();

        let _log = DiagnosticLog::under_application_support(&root).unwrap();

        for name in ["runtime.stderr.log", "runtime.stderr.log.1"] {
            if let Ok(metadata) = fs::metadata(log_dir.join(name)) {
                assert!(metadata.len() <= MAX_DIAGNOSTIC_FILE_BYTES, "{name}");
            }
        }
        fs::remove_dir_all(root).unwrap();
    }

    struct LeaseState {
        lease_open: bool,
        writes: Vec<Vec<u8>>,
        kills: usize,
    }

    struct LeaseLauncher(Arc<Mutex<LeaseState>>);

    struct LeaseChild(Arc<Mutex<LeaseState>>);

    impl RuntimeLauncher for LeaseLauncher {
        fn launch(&self, _request: LaunchRequest) -> Result<Box<dyn RuntimeChild>, LaunchError> {
            self.0.lock().unwrap().lease_open = true;
            Ok(Box::new(LeaseChild(Arc::clone(&self.0))))
        }
    }

    impl RuntimeChild for LeaseChild {
        fn write_stdin<'a>(&'a mut self, bytes: &'a [u8]) -> IoFuture<'a> {
            let state = Arc::clone(&self.0);
            Box::pin(async move {
                let mut state = state.lock().unwrap();
                if !state.lease_open {
                    return Err(io::Error::new(io::ErrorKind::BrokenPipe, "lease closed"));
                }
                state.writes.push(bytes.to_vec());
                Ok(())
            })
        }

        fn take_stdout(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>> {
            None
        }

        fn take_stderr(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>> {
            None
        }

        fn wait_for_exit_preserving_stdin<'a>(&'a mut self) -> IoFuture<'a> {
            Box::pin(std::future::pending())
        }

        fn kill_owned<'a>(&'a mut self) -> IoFuture<'a> {
            let state = Arc::clone(&self.0);
            Box::pin(async move {
                let mut state = state.lock().unwrap();
                state.kills += 1;
                state.lease_open = false;
                Ok(())
            })
        }

        fn signal_owned_on_drop(&mut self) {
            let mut state = self.0.lock().unwrap();
            state.kills += 1;
            state.lease_open = false;
        }
    }

    #[test]
    fn cancelled_exit_monitor_preserves_parent_lease_for_later_shutdown() {
        let state = Arc::new(Mutex::new(LeaseState {
            lease_open: false,
            writes: Vec::new(),
            kills: 0,
        }));
        let launcher = LeaseLauncher(Arc::clone(&state));
        let (data_dir, runtime_dir) = absolute_test_dirs();
        let mut running = runtime()
            .block_on(launch_runtime(&launcher, &data_dir, &runtime_dir))
            .unwrap();

        let quiet = runtime().block_on(async {
            tokio::time::timeout(Duration::from_millis(1), running.wait()).await
        });
        assert!(quiet.is_err());
        assert!(state.lock().unwrap().lease_open);

        runtime()
            .block_on(running.shutdown_with_timeout(Duration::ZERO))
            .unwrap();
        let state = state.lock().unwrap();
        assert_eq!(state.writes.len(), 2);
        assert_eq!(
            state.writes[1],
            b"{\"version\":1,\"command\":\"shutdown\"}\n"
        );
        assert_eq!(state.kills, 1);
    }

    #[test]
    fn failed_initial_start_write_cleans_up_the_exact_owned_child() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        state
            .lock()
            .unwrap()
            .write_results
            .push_back(Err(io::Error::new(
                io::ErrorKind::BrokenPipe,
                "token=start-secret",
            )));
        let launcher = FakeLauncher(Arc::clone(&state));
        let (data_dir, runtime_dir) = absolute_test_dirs();

        let result = runtime().block_on(launch_runtime(&launcher, &data_dir, &runtime_dir));

        assert!(result.is_err());
        assert_eq!(state.lock().unwrap().killed, 1);
    }

    #[test]
    fn cancelling_a_pending_start_write_signals_the_exact_owned_child() {
        let state = Arc::new(Mutex::new(FakeState {
            write_pending: true,
            ..FakeState::default()
        }));
        let launcher = FakeLauncher(Arc::clone(&state));
        let (data_dir, runtime_dir) = absolute_test_dirs();
        let executor = runtime();

        let result = executor.block_on(async {
            tokio::time::timeout(
                Duration::from_millis(10),
                launch_runtime(&launcher, &data_dir, &runtime_dir),
            )
            .await
        });

        assert!(result.is_err());
        assert_eq!(state.lock().unwrap().killed, 1);
    }

    #[test]
    fn shutdown_error_preserves_typed_source_without_exposing_child_text() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let launcher = FakeLauncher(Arc::clone(&state));
        let (data_dir, runtime_dir) = absolute_test_dirs();
        let mut running = runtime()
            .block_on(launch_runtime(&launcher, &data_dir, &runtime_dir))
            .unwrap();
        state
            .lock()
            .unwrap()
            .write_results
            .push_back(Err(io::Error::new(
                io::ErrorKind::BrokenPipe,
                "token=shutdown-secret",
            )));

        let error = runtime()
            .block_on(running.shutdown_with_timeout(Duration::from_secs(1)))
            .unwrap_err();

        assert_eq!(error.shutdown_stage(), Some(ShutdownStage::WriteCommand));
        assert_eq!(
            error.shutdown_source_kind(),
            Some(io::ErrorKind::BrokenPipe)
        );
        assert!(std::error::Error::source(&error).is_some());
        for exposed in [format!("{error}"), format!("{error:?}")] {
            assert!(!exposed.contains("shutdown-secret"), "leaked: {exposed}");
        }
    }

    #[test]
    fn expanded_sensitive_key_vocabulary_never_persists_values() {
        for key in [
            "privateKey",
            "private-key",
            "api_key",
            "api-key",
            "apiKey",
            "api_token",
            "apiToken",
            "api-secret",
            "apiSecret",
            "password",
            "passphrase",
            "client_secret",
            "client-secret",
            "clientSecret",
            "access_token",
            "accessToken",
            "credential",
            "credentials",
            "refresh_token",
            "refresh-token",
            "refreshToken",
            "api.key",
            "api key",
        ] {
            let secret = format!("value-for-{key}");
            for line in [
                format!("INFO {key} = {secret}"),
                format!(r#"{{"{key}":"{secret}","safe":"visible"}}"#),
            ] {
                let redacted = redact_diagnostic_line(&line);
                assert!(!redacted.contains(&secret), "leaked {key}: {redacted}");
            }
        }
    }

    #[test]
    fn process_group_probe_only_confirms_esrch_as_gone() {
        assert_eq!(
            classify_process_group_probe(0, None).unwrap(),
            ProcessGroupPresence::Present
        );
        assert_eq!(
            classify_process_group_probe(-1, Some(3)).unwrap(),
            ProcessGroupPresence::Gone
        );
        assert_eq!(
            classify_process_group_probe(-1, Some(1)).unwrap(),
            ProcessGroupPresence::Present
        );
        assert!(classify_process_group_probe(-1, Some(5)).is_err());
    }
}
