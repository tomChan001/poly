use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::Arc;
use std::sync::Mutex as SyncMutex;

use thiserror::Error;
use tokio::sync::{mpsc, watch, Mutex};
use url::Url;

use crate::app_lifecycle::{RuntimeShutdown, ShutdownFuture};
use crate::runtime::process::RuntimeLauncher;
use crate::runtime::state::SupervisorState;
use crate::runtime::supervisor::{RuntimeSupervisor, RuntimeSupervisorNotice, SupervisorClock};
use crate::webview::{status_url, status_url_with_logs, DesktopUiState};
use crate::DesktopNavigationController;

pub type DesktopRuntimeFuture<'a> =
    Pin<Box<dyn Future<Output = Result<(), DesktopServiceError>> + Send + 'a>>;

#[derive(Debug, Error)]
pub enum DesktopServiceError {
    #[error("desktop path is not an absolute resolved path: {0}")]
    InvalidPath(&'static str),
    #[error("desktop data path must have an application support parent")]
    MissingApplicationSupport,
    #[error("desktop runtime directory is a symbolic link")]
    SymbolicLink,
    #[error("desktop directory setup failed")]
    Directory(#[source] std::io::Error),
    #[error("desktop webview navigation failed")]
    Navigation,
    #[error("desktop runtime supervision failed")]
    Runtime,
    #[error("desktop runtime retry is only available from a terminal state")]
    RetryNotTerminal,
    #[error("desktop runtime supervision is already active")]
    AlreadyRunning,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DesktopPaths {
    resource_dir: PathBuf,
    application_support: PathBuf,
    data_dir: PathBuf,
    runtime_dir: PathBuf,
    logs_dir: PathBuf,
}

struct DirectorySecurityPlan<'a> {
    shared_root: &'a Path,
    shared_cache_root: &'a Path,
    cache_root: &'a Path,
    owned_directories: [&'a Path; 4],
}

impl DesktopPaths {
    pub fn from_resolved(
        resource_dir: PathBuf,
        app_data_dir: PathBuf,
        app_cache_dir: PathBuf,
    ) -> Result<Self, DesktopServiceError> {
        validate_absolute(&resource_dir, "resource_dir")?;
        validate_absolute(&app_data_dir, "app_data_dir")?;
        validate_absolute(&app_cache_dir, "app_cache_dir")?;
        let application_support = app_data_dir
            .parent()
            .filter(|parent| parent.is_absolute())
            .ok_or(DesktopServiceError::MissingApplicationSupport)?
            .to_path_buf();
        let data_dir = application_support.join("Poly");
        let logs_dir = data_dir.join("logs");
        let runtime_dir = app_cache_dir.join("runtime");
        Ok(Self {
            resource_dir,
            application_support,
            data_dir,
            runtime_dir,
            logs_dir,
        })
    }

    pub fn prepare_directories(&self) -> Result<(), DesktopServiceError> {
        reject_symlink_ancestors(&self.resource_dir)?;
        std::fs::canonicalize(&self.resource_dir).map_err(DesktopServiceError::Directory)?;
        let plan = self.directory_security_plan()?;
        validate_shared_root(plan.shared_root, "application_support")?;
        validate_shared_root(plan.shared_cache_root, "cache_root")?;
        for directory in plan.owned_directories {
            secure_create_dir_all(directory)?;
        }
        ensure_canonical_child(&self.application_support, &self.data_dir)?;
        ensure_canonical_child(&self.data_dir, &self.logs_dir)?;
        ensure_canonical_child(plan.shared_cache_root, plan.cache_root)?;
        ensure_canonical_child(plan.cache_root, &self.runtime_dir)?;
        Ok(())
    }

    fn directory_security_plan(&self) -> Result<DirectorySecurityPlan<'_>, DesktopServiceError> {
        let cache_root = self
            .runtime_dir
            .parent()
            .ok_or(DesktopServiceError::InvalidPath("app_cache_dir"))?;
        let shared_cache_root = cache_root
            .parent()
            .filter(|parent| parent.is_absolute())
            .ok_or(DesktopServiceError::InvalidPath("app_cache_dir"))?;
        if cache_root == self.application_support {
            return Err(DesktopServiceError::InvalidPath("app_cache_dir"));
        }
        Ok(DirectorySecurityPlan {
            shared_root: &self.application_support,
            shared_cache_root,
            cache_root,
            owned_directories: [
                &self.data_dir,
                &self.logs_dir,
                cache_root,
                &self.runtime_dir,
            ],
        })
    }

    pub fn resource_dir(&self) -> &Path {
        &self.resource_dir
    }

    pub fn application_support(&self) -> &Path {
        &self.application_support
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    pub fn runtime_dir(&self) -> &Path {
        &self.runtime_dir
    }

    pub fn logs_dir(&self) -> &Path {
        &self.logs_dir
    }

    pub fn revealable_logs_dir(&self) -> Option<PathBuf> {
        let metadata = std::fs::symlink_metadata(&self.logs_dir).ok()?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return None;
        }
        ensure_canonical_child(&self.application_support, &self.logs_dir)
            .ok()
            .map(|()| self.logs_dir.clone())
    }
}

fn validate_absolute(path: &Path, field: &'static str) -> Result<(), DesktopServiceError> {
    if path.is_absolute()
        && !path.components().any(|component| {
            matches!(
                component,
                std::path::Component::ParentDir | std::path::Component::CurDir
            )
        })
    {
        Ok(())
    } else {
        Err(DesktopServiceError::InvalidPath(field))
    }
}

fn validate_shared_root(path: &Path, field: &'static str) -> Result<(), DesktopServiceError> {
    validate_absolute(path, field)?;
    reject_symlink_ancestors(path)?;
    std::fs::canonicalize(path).map_err(DesktopServiceError::Directory)?;
    Ok(())
}

fn secure_create_dir_all(path: &Path) -> Result<(), DesktopServiceError> {
    reject_symlink_ancestors(path)?;
    std::fs::create_dir_all(path).map_err(DesktopServiceError::Directory)?;
    reject_symlink_ancestors(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
            .map_err(DesktopServiceError::Directory)?;
    }
    Ok(())
}

fn reject_symlink_ancestors(path: &Path) -> Result<(), DesktopServiceError> {
    for ancestor in path.ancestors() {
        match std::fs::symlink_metadata(ancestor) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(DesktopServiceError::SymbolicLink);
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(DesktopServiceError::Directory(error)),
        }
    }
    Ok(())
}

fn ensure_canonical_child(root: &Path, child: &Path) -> Result<(), DesktopServiceError> {
    let canonical_root = std::fs::canonicalize(root).map_err(DesktopServiceError::Directory)?;
    let canonical_child = std::fs::canonicalize(child).map_err(DesktopServiceError::Directory)?;
    if canonical_child.starts_with(canonical_root) {
        Ok(())
    } else {
        Err(DesktopServiceError::InvalidPath("owned desktop directory"))
    }
}

pub fn finder_reveal_command(logs_dir: &Path) -> tokio::process::Command {
    let mut command = tokio::process::Command::new("/usr/bin/open");
    command.arg("-R").arg(logs_dir);
    command
}

pub trait DesktopNavigator: Send + Sync + 'static {
    fn navigate(&self, url: Url) -> Result<(), DesktopServiceError>;
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DesktopDiagnosticEvent {
    NoticeRejected,
    NavigationFailed,
    NavigationFallbackFailed,
    SupervisionFailed,
    SetupFailed,
    ShutdownFailed,
}

pub trait DesktopDiagnosticReporter: Send + Sync + 'static {
    fn report(&self, event: DesktopDiagnosticEvent);
}

#[derive(Default)]
struct NoopDiagnosticReporter;

impl DesktopDiagnosticReporter for NoopDiagnosticReporter {
    fn report(&self, _event: DesktopDiagnosticEvent) {}
}

pub trait DesktopRuntime: Send + 'static {
    fn subscribe_notices(&self) -> watch::Receiver<Option<RuntimeSupervisorNotice>>;
    fn is_terminal(&self) -> bool;
    fn explicit_operator_retry(&mut self) -> Result<(), DesktopServiceError>;
    fn supervise_until_terminal(&mut self) -> DesktopRuntimeFuture<'_>;
}

impl<L, C> DesktopRuntime for RuntimeSupervisor<L, C>
where
    L: RuntimeLauncher + Send + Sync + 'static,
    C: SupervisorClock + Send + Sync + 'static,
{
    fn subscribe_notices(&self) -> watch::Receiver<Option<RuntimeSupervisorNotice>> {
        RuntimeSupervisor::subscribe_notices(self)
    }

    fn is_terminal(&self) -> bool {
        matches!(self.state(), SupervisorState::Failed { .. })
    }

    fn explicit_operator_retry(&mut self) -> Result<(), DesktopServiceError> {
        RuntimeSupervisor::explicit_operator_retry(self).map_err(|_| DesktopServiceError::Runtime)
    }

    fn supervise_until_terminal(&mut self) -> DesktopRuntimeFuture<'_> {
        Box::pin(async move {
            let (notices, _unused_receiver) = mpsc::channel(1);
            RuntimeSupervisor::supervise_until_terminal(self, notices)
                .await
                .map(|_| ())
                .map_err(|_| DesktopServiceError::Runtime)
        })
    }
}

pub struct DesktopRuntimeService<R, N> {
    runtime: Mutex<R>,
    navigator: N,
    phase: AtomicU8,
    reporter: Arc<dyn DesktopDiagnosticReporter>,
    shutdown: Arc<dyn RuntimeShutdown>,
    supervision_task: SyncMutex<Option<tauri::async_runtime::JoinHandle<()>>>,
    cleanup_requested: AtomicBool,
}

const PHASE_IDLE: u8 = 0;
const PHASE_RUNNING: u8 = 1;
const PHASE_TERMINAL: u8 = 2;
const PHASE_RETRYING: u8 = 3;
const SHUTDOWN_COMPLETION_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(17);
const TASK_CANCELLATION_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(1);

impl<R, N> DesktopRuntimeService<R, N>
where
    R: DesktopRuntime,
    N: DesktopNavigator,
{
    pub fn new(runtime: R, navigator: N) -> Self {
        Self::with_reporter(runtime, navigator, Arc::new(NoopDiagnosticReporter))
    }

    pub fn with_reporter(
        runtime: R,
        navigator: N,
        reporter: Arc<dyn DesktopDiagnosticReporter>,
    ) -> Self {
        Self::with_reporter_and_shutdown(
            runtime,
            navigator,
            reporter,
            Arc::new(NoopRuntimeShutdown),
        )
    }

    pub fn with_reporter_and_shutdown(
        runtime: R,
        navigator: N,
        reporter: Arc<dyn DesktopDiagnosticReporter>,
        shutdown: Arc<dyn RuntimeShutdown>,
    ) -> Self {
        Self {
            runtime: Mutex::new(runtime),
            navigator,
            phase: AtomicU8::new(PHASE_IDLE),
            reporter,
            shutdown,
            supervision_task: SyncMutex::new(None),
            cleanup_requested: AtomicBool::new(false),
        }
    }

    pub fn show_initializing(&self) -> Result<(), DesktopServiceError> {
        self.navigator
            .navigate(status_url(DesktopUiState::Initializing))
    }

    pub async fn observe_notices(
        self: Arc<Self>,
        mut notices: watch::Receiver<Option<RuntimeSupervisorNotice>>,
    ) -> Result<(), DesktopServiceError> {
        while notices.changed().await.is_ok() {
            let notice = notices.borrow_and_update().clone();
            if let Some(notice) = notice {
                let url = match DesktopNavigationController::navigation_for_notice(&notice) {
                    Ok(url) => url,
                    Err(_) => {
                        self.reporter.report(DesktopDiagnosticEvent::NoticeRejected);
                        safe_navigation_failure_url()
                    }
                };
                if !self.navigate_with_reconciliation(url).await
                    && self
                        .navigator
                        .navigate(safe_navigation_failure_url())
                        .is_err()
                {
                    self.reporter
                        .report(DesktopDiagnosticEvent::NavigationFallbackFailed);
                }
            }
        }
        Ok(())
    }

    pub fn start_supervision(self: &Arc<Self>) -> bool {
        if self
            .phase
            .compare_exchange(
                PHASE_IDLE,
                PHASE_RUNNING,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_err()
        {
            return false;
        }

        let service = Arc::clone(self);
        let task = tauri::async_runtime::spawn(async move {
            let mut runtime = service.runtime.lock().await;
            let result = runtime.supervise_until_terminal().await;
            let terminal = runtime.is_terminal() || result.is_err();
            drop(runtime);
            service.phase.store(
                if terminal { PHASE_TERMINAL } else { PHASE_IDLE },
                Ordering::Release,
            );
            if result.is_err() {
                service
                    .reporter
                    .report(DesktopDiagnosticEvent::SupervisionFailed);
                if service
                    .navigator
                    .navigate(status_url_with_logs(
                        DesktopUiState::RuntimeUnavailable,
                        true,
                    ))
                    .is_err()
                {
                    service
                        .reporter
                        .report(DesktopDiagnosticEvent::NavigationFallbackFailed);
                }
            }
        });
        *self
            .supervision_task
            .lock()
            .expect("desktop supervision task lock poisoned") = Some(task);
        true
    }

    pub async fn retry(self: &Arc<Self>) -> Result<(), DesktopServiceError> {
        match self.phase.compare_exchange(
            PHASE_TERMINAL,
            PHASE_RETRYING,
            Ordering::AcqRel,
            Ordering::Acquire,
        ) {
            Ok(_) => {}
            Err(PHASE_RUNNING | PHASE_RETRYING) => {
                return Err(DesktopServiceError::AlreadyRunning);
            }
            Err(_) => return Err(DesktopServiceError::RetryNotTerminal),
        }

        let mut runtime = match self.runtime.try_lock() {
            Ok(runtime) => runtime,
            Err(_) => {
                self.phase.store(PHASE_TERMINAL, Ordering::Release);
                return Err(DesktopServiceError::AlreadyRunning);
            }
        };
        if !runtime.is_terminal() {
            self.phase.store(PHASE_IDLE, Ordering::Release);
            return Err(DesktopServiceError::RetryNotTerminal);
        }
        if let Err(error) = runtime.explicit_operator_retry() {
            self.phase.store(PHASE_TERMINAL, Ordering::Release);
            return Err(error);
        }
        drop(runtime);
        self.phase.store(PHASE_IDLE, Ordering::Release);

        if self.start_supervision() {
            Ok(())
        } else {
            Err(DesktopServiceError::AlreadyRunning)
        }
    }

    async fn navigate_with_reconciliation(&self, url: Url) -> bool {
        let is_ready = url.scheme() == "http" && url.host_str() == Some("127.0.0.1");
        let attempts = if is_ready { 3 } else { 1 };
        for attempt in 0..attempts {
            if self.navigator.navigate(url.clone()).is_ok() {
                return true;
            }
            self.reporter
                .report(DesktopDiagnosticEvent::NavigationFailed);
            if attempt + 1 < attempts {
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
            }
        }
        false
    }
}

struct NoopRuntimeShutdown;

impl RuntimeShutdown for NoopRuntimeShutdown {
    fn shutdown(&self, _reason: &'static str) -> ShutdownFuture<'_> {
        Box::pin(async { Ok(()) })
    }

    fn cleanup_owned(&self) {}
}

impl<R, N> RuntimeShutdown for DesktopRuntimeService<R, N>
where
    R: DesktopRuntime,
    N: DesktopNavigator,
{
    fn shutdown(&self, reason: &'static str) -> ShutdownFuture<'_> {
        Box::pin(async move {
            let task = self
                .supervision_task
                .lock()
                .expect("desktop supervision task lock poisoned")
                .take();
            let Some(task) = task else {
                return Ok(());
            };
            if task.inner().is_finished() {
                let _ = task.await;
                return Ok(());
            }
            let result =
                tokio::time::timeout(SHUTDOWN_COMPLETION_TIMEOUT, self.shutdown.shutdown(reason))
                    .await
                    .map_err(|_| ())
                    .and_then(std::convert::identity);
            let mut task = task;
            if result.is_err() {
                task.abort();
            }
            let joined = match tokio::time::timeout(TASK_CANCELLATION_TIMEOUT, &mut task).await {
                Ok(_) => Ok(()),
                Err(_) => {
                    task.abort();
                    let _ = tokio::time::timeout(TASK_CANCELLATION_TIMEOUT, task).await;
                    Err(())
                }
            };
            result.and(joined)
        })
    }

    fn cleanup_owned(&self) {
        if self.cleanup_requested.swap(true, Ordering::AcqRel) {
            return;
        }
        self.shutdown.cleanup_owned();
        if let Some(task) = self
            .supervision_task
            .lock()
            .expect("desktop supervision task lock poisoned")
            .as_ref()
        {
            task.abort();
        }
    }

    fn wait_for_cleanup(&self) -> ShutdownFuture<'_> {
        let task = self
            .supervision_task
            .lock()
            .expect("desktop supervision task lock poisoned")
            .take();
        let shutdown = Arc::clone(&self.shutdown);
        Box::pin(async move {
            let task_result = if let Some(task) = task {
                tokio::time::timeout(TASK_CANCELLATION_TIMEOUT, task)
                    .await
                    .map(|_| ())
                    .map_err(|_| ())
            } else {
                Ok(())
            };
            let cleanup_result = shutdown.wait_for_cleanup().await;
            task_result.and(cleanup_result)
        })
    }
}

pub trait DesktopRetryControl: Send + Sync + 'static {
    fn retry(&self) -> DesktopRuntimeFuture<'_>;
}

impl<R, N> DesktopRetryControl for Arc<DesktopRuntimeService<R, N>>
where
    R: DesktopRuntime,
    N: DesktopNavigator,
{
    fn retry(&self) -> DesktopRuntimeFuture<'_> {
        Box::pin(async move { DesktopRuntimeService::retry(self).await })
    }
}

pub type DesktopInstallFuture<'a> =
    Pin<Box<dyn Future<Output = Result<InstalledDesktopRuntime, DesktopSetupFailure>> + Send + 'a>>;

pub trait DesktopInstaller: Send + Sync + 'static {
    fn install(&self) -> DesktopInstallFuture<'_>;
}

pub struct InstalledDesktopRuntime {
    control: Arc<dyn DesktopRetryControl>,
    logs_dir: Option<PathBuf>,
}

impl InstalledDesktopRuntime {
    pub fn new(control: Arc<dyn DesktopRetryControl>, logs_dir: Option<PathBuf>) -> Self {
        Self { control, logs_dir }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DesktopSetupFailure {
    PermissionDenied { logs_dir: Option<PathBuf> },
    ResourceMissing { logs_dir: Option<PathBuf> },
    RuntimeUnavailable { logs_dir: Option<PathBuf> },
}

impl DesktopSetupFailure {
    fn status(self) -> (DesktopUiState, bool) {
        match self {
            Self::PermissionDenied { logs_dir } => {
                (DesktopUiState::PermissionDenied, logs_dir.is_some())
            }
            Self::ResourceMissing { logs_dir } => {
                (DesktopUiState::ResourceMissing, logs_dir.is_some())
            }
            Self::RuntimeUnavailable { logs_dir } => {
                (DesktopUiState::RuntimeUnavailable, logs_dir.is_some())
            }
        }
    }

    fn logs_dir(&self) -> Option<PathBuf> {
        match self {
            Self::PermissionDenied { logs_dir }
            | Self::ResourceMissing { logs_dir }
            | Self::RuntimeUnavailable { logs_dir } => logs_dir.clone(),
        }
    }
}

const SETUP_NEW: u8 = 0;
const SETUP_INSTALLING: u8 = 1;
const SETUP_FAILED: u8 = 2;
const SETUP_INSTALLED: u8 = 3;

pub struct RecoverableDesktopSetup<I, N> {
    installer: I,
    navigator: N,
    reporter: Arc<dyn DesktopDiagnosticReporter>,
    phase: AtomicU8,
    installed: std::sync::RwLock<Option<InstalledDesktopRuntime>>,
    available_logs: std::sync::RwLock<Option<PathBuf>>,
}

impl<I, N> RecoverableDesktopSetup<I, N>
where
    I: DesktopInstaller,
    N: DesktopNavigator,
{
    pub fn new(installer: I, navigator: N, reporter: Arc<dyn DesktopDiagnosticReporter>) -> Self {
        Self {
            installer,
            navigator,
            reporter,
            phase: AtomicU8::new(SETUP_NEW),
            installed: std::sync::RwLock::new(None),
            available_logs: std::sync::RwLock::new(None),
        }
    }

    pub async fn initialize(&self) {
        if self
            .phase
            .compare_exchange(
                SETUP_NEW,
                SETUP_INSTALLING,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_err()
        {
            return;
        }
        self.show_initializing();
        let _ = self.attempt_install().await;
    }

    pub async fn retry(&self) -> Result<(), DesktopServiceError> {
        match self.phase.load(Ordering::Acquire) {
            SETUP_INSTALLED => {
                let control = self
                    .installed
                    .read()
                    .expect("desktop setup lock poisoned")
                    .as_ref()
                    .map(|installed| Arc::clone(&installed.control))
                    .ok_or(DesktopServiceError::Runtime)?;
                control.retry().await
            }
            SETUP_FAILED => {
                if self
                    .phase
                    .compare_exchange(
                        SETUP_FAILED,
                        SETUP_INSTALLING,
                        Ordering::AcqRel,
                        Ordering::Acquire,
                    )
                    .is_err()
                {
                    return Err(DesktopServiceError::AlreadyRunning);
                }
                self.show_initializing();
                self.attempt_install().await
            }
            SETUP_INSTALLING => Err(DesktopServiceError::AlreadyRunning),
            _ => Err(DesktopServiceError::RetryNotTerminal),
        }
    }

    pub fn logs_dir(&self) -> Option<PathBuf> {
        self.available_logs
            .read()
            .expect("desktop setup lock poisoned")
            .clone()
    }

    pub fn installer(&self) -> &I {
        &self.installer
    }

    fn show_initializing(&self) {
        if self
            .navigator
            .navigate(status_url(DesktopUiState::Initializing))
            .is_err()
        {
            self.reporter
                .report(DesktopDiagnosticEvent::NavigationFailed);
        }
    }

    async fn attempt_install(&self) -> Result<(), DesktopServiceError> {
        match self.installer.install().await {
            Ok(installed) => {
                *self
                    .available_logs
                    .write()
                    .expect("desktop setup lock poisoned") = installed.logs_dir.clone();
                *self.installed.write().expect("desktop setup lock poisoned") = Some(installed);
                self.phase.store(SETUP_INSTALLED, Ordering::Release);
                Ok(())
            }
            Err(failure) => {
                self.reporter.report(DesktopDiagnosticEvent::SetupFailed);
                *self
                    .available_logs
                    .write()
                    .expect("desktop setup lock poisoned") = failure.logs_dir();
                let (state, can_reveal_logs) = failure.status();
                if self
                    .navigator
                    .navigate(status_url_with_logs(state, can_reveal_logs))
                    .is_err()
                {
                    self.reporter
                        .report(DesktopDiagnosticEvent::NavigationFallbackFailed);
                }
                self.phase.store(SETUP_FAILED, Ordering::Release);
                Err(DesktopServiceError::Runtime)
            }
        }
    }
}

fn safe_navigation_failure_url() -> Url {
    status_url_with_logs(DesktopUiState::RuntimeUnavailable, true)
}

#[cfg(test)]
mod tests {
    use std::future::Future;
    use std::num::NonZeroU16;
    use std::path::PathBuf;
    use std::pin::Pin;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use tokio::sync::{watch, Barrier, Notify};
    use url::Url;

    use super::{
        finder_reveal_command, DesktopDiagnosticEvent, DesktopDiagnosticReporter, DesktopInstaller,
        DesktopNavigator, DesktopPaths, DesktopRetryControl, DesktopRuntime, DesktopRuntimeService,
        DesktopServiceError, DesktopSetupFailure, InstalledDesktopRuntime, RecoverableDesktopSetup,
        PHASE_TERMINAL,
    };
    use crate::app_lifecycle::{
        cleanup_rejected_runtime, AppLifecycle, CloseDecision, LifecycleApplication,
        LifecycleWindow, RuntimeShutdown, RuntimeShutdownRegistry, ShutdownFuture,
        APPLICATION_QUIT_REASON,
    };
    use crate::runtime::protocol::RuntimeEvent;
    use crate::runtime::supervisor::{RuntimeFailure, RuntimeSupervisorNotice};

    type RuntimeFuture<'a> =
        Pin<Box<dyn Future<Output = Result<(), DesktopServiceError>> + Send + 'a>>;

    #[derive(Clone, Default)]
    struct FakeNavigator {
        urls: Arc<Mutex<Vec<Url>>>,
        restart_seen: Arc<AtomicBool>,
        ready_seen: Arc<AtomicBool>,
        restart_observed: Arc<Notify>,
        ready_observed: Arc<Notify>,
        fail_next_restart: Arc<AtomicBool>,
        ready_failures: Arc<AtomicUsize>,
    }

    impl DesktopNavigator for FakeNavigator {
        fn navigate(&self, url: Url) -> Result<(), DesktopServiceError> {
            if url
                .query_pairs()
                .any(|(key, value)| key == "desktop-state" && value == "restarting")
            {
                self.restart_seen.store(true, Ordering::SeqCst);
                self.restart_observed.notify_one();
                if self.fail_next_restart.swap(false, Ordering::SeqCst) {
                    return Err(DesktopServiceError::Navigation);
                }
            }
            if url.host_str() == Some("127.0.0.1") {
                if self
                    .ready_failures
                    .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |remaining| {
                        remaining.checked_sub(1)
                    })
                    .is_ok()
                {
                    return Err(DesktopServiceError::Navigation);
                }
                self.ready_seen.store(true, Ordering::SeqCst);
                self.ready_observed.notify_one();
            }
            self.urls.lock().unwrap().push(url);
            Ok(())
        }
    }

    #[derive(Default)]
    struct RecordingReporter {
        events: Mutex<Vec<DesktopDiagnosticEvent>>,
    }

    impl DesktopDiagnosticReporter for RecordingReporter {
        fn report(&self, event: DesktopDiagnosticEvent) {
            self.events.lock().unwrap().push(event);
        }
    }

    #[derive(Default)]
    struct LifecycleTestWindow;

    impl LifecycleWindow for LifecycleTestWindow {
        fn show(&self) {}
        fn unminimize(&self) {}
        fn focus(&self) {}
        fn navigate(&self, _url: Url) {}
    }

    #[derive(Default)]
    struct LifecycleTestApplication {
        exits: AtomicUsize,
        reports: AtomicUsize,
    }

    impl LifecycleApplication for LifecycleTestApplication {
        fn main_window(&self) -> Option<Box<dyn LifecycleWindow>> {
            Some(Box::new(LifecycleTestWindow))
        }

        fn exit(&self, code: i32) {
            assert_eq!(code, 0);
            self.exits.fetch_add(1, Ordering::SeqCst);
        }

        fn report_shutdown_failure(&self) {
            self.reports.fetch_add(1, Ordering::SeqCst);
        }
    }

    struct PendingRuntime {
        notices: watch::Sender<Option<RuntimeSupervisorNotice>>,
        entered: Arc<Notify>,
        release: Arc<Notify>,
    }

    struct CompletedRuntime {
        notices: watch::Sender<Option<RuntimeSupervisorNotice>>,
        terminal: bool,
    }

    impl DesktopRuntime for CompletedRuntime {
        fn subscribe_notices(&self) -> watch::Receiver<Option<RuntimeSupervisorNotice>> {
            self.notices.subscribe()
        }

        fn is_terminal(&self) -> bool {
            self.terminal
        }

        fn explicit_operator_retry(&mut self) -> Result<(), DesktopServiceError> {
            Ok(())
        }

        fn supervise_until_terminal(&mut self) -> RuntimeFuture<'_> {
            Box::pin(async { Ok(()) })
        }
    }

    #[derive(Default)]
    struct NeverCompletingShutdown {
        calls: AtomicUsize,
    }

    struct CancellationRuntime {
        notices: watch::Sender<Option<RuntimeSupervisorNotice>>,
        entered: Arc<Notify>,
        cancellations: Arc<AtomicUsize>,
    }

    struct CancellationGuard(Arc<AtomicUsize>);

    impl Drop for CancellationGuard {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }

    impl DesktopRuntime for CancellationRuntime {
        fn subscribe_notices(&self) -> watch::Receiver<Option<RuntimeSupervisorNotice>> {
            self.notices.subscribe()
        }

        fn is_terminal(&self) -> bool {
            false
        }

        fn explicit_operator_retry(&mut self) -> Result<(), DesktopServiceError> {
            Ok(())
        }

        fn supervise_until_terminal(&mut self) -> RuntimeFuture<'_> {
            Box::pin(async move {
                let _guard = CancellationGuard(Arc::clone(&self.cancellations));
                self.entered.notify_one();
                std::future::pending().await
            })
        }
    }

    #[derive(Default)]
    struct ConfirmingCleanup {
        cleanup_calls: AtomicUsize,
        confirmation_calls: AtomicUsize,
        fail_confirmation: AtomicBool,
    }

    struct BlockingCleanupConfirmation {
        cleanup_calls: AtomicUsize,
        confirmation_calls: AtomicUsize,
        confirmation_started: Arc<Notify>,
        release_confirmation: Arc<Notify>,
    }

    impl RuntimeShutdown for BlockingCleanupConfirmation {
        fn shutdown(&self, _reason: &'static str) -> ShutdownFuture<'_> {
            Box::pin(async { Ok(()) })
        }

        fn cleanup_owned(&self) {
            self.cleanup_calls.fetch_add(1, Ordering::SeqCst);
        }

        fn wait_for_cleanup(&self) -> ShutdownFuture<'_> {
            self.confirmation_calls.fetch_add(1, Ordering::SeqCst);
            let confirmation_started = Arc::clone(&self.confirmation_started);
            let release_confirmation = Arc::clone(&self.release_confirmation);
            Box::pin(async move {
                confirmation_started.notify_one();
                release_confirmation.notified().await;
                Ok(())
            })
        }
    }

    impl RuntimeShutdown for ConfirmingCleanup {
        fn shutdown(&self, _reason: &'static str) -> ShutdownFuture<'_> {
            Box::pin(async { Ok(()) })
        }

        fn cleanup_owned(&self) {
            self.cleanup_calls.fetch_add(1, Ordering::SeqCst);
        }

        fn wait_for_cleanup(&self) -> ShutdownFuture<'_> {
            self.confirmation_calls.fetch_add(1, Ordering::SeqCst);
            let failed = self.fail_confirmation.load(Ordering::SeqCst);
            Box::pin(async move {
                if failed {
                    Err(())
                } else {
                    Ok(())
                }
            })
        }
    }

    impl RuntimeShutdown for NeverCompletingShutdown {
        fn shutdown(&self, _reason: &'static str) -> ShutdownFuture<'_> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            Box::pin(std::future::pending())
        }

        fn cleanup_owned(&self) {}
    }

    #[derive(Default)]
    struct FakeRetryControl {
        retries: AtomicUsize,
    }

    impl DesktopRetryControl for FakeRetryControl {
        fn retry(&self) -> RuntimeFuture<'_> {
            Box::pin(async move {
                self.retries.fetch_add(1, Ordering::SeqCst);
                Ok(())
            })
        }
    }

    struct RecoveringInstaller {
        attempts: AtomicUsize,
        second_entered: Arc<Notify>,
        release_second: Arc<Notify>,
        control: Arc<FakeRetryControl>,
    }

    impl DesktopInstaller for RecoveringInstaller {
        fn install(&self) -> super::DesktopInstallFuture<'_> {
            Box::pin(async move {
                let attempt = self.attempts.fetch_add(1, Ordering::SeqCst);
                if attempt == 0 {
                    return Err(DesktopSetupFailure::PermissionDenied { logs_dir: None });
                }
                self.second_entered.notify_one();
                self.release_second.notified().await;
                Ok(InstalledDesktopRuntime::new(
                    self.control.clone(),
                    Some(PathBuf::from("/safe/Application Support/Poly/logs")),
                ))
            })
        }
    }

    impl DesktopRuntime for PendingRuntime {
        fn subscribe_notices(&self) -> watch::Receiver<Option<RuntimeSupervisorNotice>> {
            self.notices.subscribe()
        }

        fn is_terminal(&self) -> bool {
            false
        }

        fn explicit_operator_retry(&mut self) -> Result<(), DesktopServiceError> {
            panic!("an active runtime must not be retried")
        }

        fn supervise_until_terminal(&mut self) -> RuntimeFuture<'_> {
            Box::pin(async move {
                self.entered.notify_one();
                self.release.notified().await;
                Ok(())
            })
        }
    }

    struct FakeRuntime {
        notices: watch::Sender<Option<RuntimeSupervisorNotice>>,
        supervise_calls: Arc<AtomicUsize>,
        retry_calls: Arc<AtomicUsize>,
        terminal: Arc<AtomicBool>,
        restart_seen: Arc<AtomicBool>,
        ready_seen: Arc<AtomicBool>,
        restart_observed: Arc<Notify>,
        ready_observed: Arc<Notify>,
    }

    impl DesktopRuntime for FakeRuntime {
        fn subscribe_notices(&self) -> watch::Receiver<Option<RuntimeSupervisorNotice>> {
            self.notices.subscribe()
        }

        fn is_terminal(&self) -> bool {
            self.terminal.load(Ordering::SeqCst)
        }

        fn explicit_operator_retry(&mut self) -> Result<(), DesktopServiceError> {
            self.retry_calls.fetch_add(1, Ordering::SeqCst);
            self.terminal.store(false, Ordering::SeqCst);
            Ok(())
        }

        fn supervise_until_terminal(&mut self) -> RuntimeFuture<'_> {
            Box::pin(async move {
                let call = self.supervise_calls.fetch_add(1, Ordering::SeqCst);
                if call == 0 {
                    self.notices
                        .send_replace(Some(RuntimeSupervisorNotice::Runtime(
                            RuntimeEvent::Initializing,
                        )));
                    self.notices
                        .send_replace(Some(RuntimeSupervisorNotice::RestartScheduled {
                            attempt: 1,
                            delay: Duration::from_secs(1),
                        }));
                    self.restart_observed.notified().await;
                    assert!(self.restart_seen.load(Ordering::SeqCst));
                    self.notices
                        .send_replace(Some(RuntimeSupervisorNotice::Runtime(
                            RuntimeEvent::Ready {
                                port: NonZeroU16::new(49152).unwrap(),
                                bootstrap_path: "/desktop/bootstrap/safe".to_owned(),
                            },
                        )));
                    self.ready_observed.notified().await;
                    assert!(self.ready_seen.load(Ordering::SeqCst));
                    self.terminal.store(true, Ordering::SeqCst);
                    self.notices
                        .send_replace(Some(RuntimeSupervisorNotice::Terminal(
                            RuntimeFailure::UnexpectedExit,
                        )));
                }
                Ok(())
            })
        }
    }

    struct FakeServiceFixture {
        service: Arc<DesktopRuntimeService<FakeRuntime, FakeNavigator>>,
        notices: watch::Receiver<Option<RuntimeSupervisorNotice>>,
        navigator: FakeNavigator,
        supervise_calls: Arc<AtomicUsize>,
        retry_calls: Arc<AtomicUsize>,
    }

    fn fake_service() -> FakeServiceFixture {
        let (notices, _) = watch::channel(None);
        let navigator = FakeNavigator::default();
        let supervise_calls = Arc::new(AtomicUsize::new(0));
        let retry_calls = Arc::new(AtomicUsize::new(0));
        let runtime = FakeRuntime {
            notices,
            supervise_calls: Arc::clone(&supervise_calls),
            retry_calls: Arc::clone(&retry_calls),
            terminal: Arc::new(AtomicBool::new(false)),
            restart_seen: Arc::clone(&navigator.restart_seen),
            ready_seen: Arc::clone(&navigator.ready_seen),
            restart_observed: Arc::clone(&navigator.restart_observed),
            ready_observed: Arc::clone(&navigator.ready_observed),
        };
        let receiver = runtime.subscribe_notices();
        let service = Arc::new(DesktopRuntimeService::new(runtime, navigator.clone()));
        FakeServiceFixture {
            service,
            notices: receiver,
            navigator,
            supervise_calls,
            retry_calls,
        }
    }

    #[test]
    fn resolves_only_absolute_product_paths_and_pins_logs_to_poly() {
        let root = std::env::current_dir().unwrap().join("desktop-path-test");
        let resource_dir = root.join("Poly.app").join("Contents").join("Resources");
        let application_support = root.join("Application Support");
        let app_data_dir = application_support.join("com.poly.desktop");
        let app_cache_dir = root.join("Caches").join("com.poly.desktop");
        let paths =
            DesktopPaths::from_resolved(resource_dir, app_data_dir, app_cache_dir.clone()).unwrap();

        assert_eq!(paths.data_dir(), application_support.join("Poly"));
        assert_eq!(
            paths.logs_dir(),
            application_support.join("Poly").join("logs")
        );
        assert_eq!(paths.runtime_dir(), app_cache_dir.join("runtime"));
        assert!(DesktopPaths::from_resolved(
            PathBuf::from("relative/resources"),
            PathBuf::from("/absolute/data"),
            PathBuf::from("/absolute/cache"),
        )
        .is_err());
        assert!(DesktopPaths::from_resolved(
            root.join("Resources"),
            root.join("Application Support").join("app").join(".."),
            root.join("Caches").join("..").join("outside"),
        )
        .is_err());
    }

    #[test]
    fn directory_security_plan_never_treats_shared_support_as_owned() {
        let root = std::env::current_dir()
            .unwrap()
            .join("desktop-security-plan-test");
        let application_support = root.join("Application Support");
        let paths = DesktopPaths::from_resolved(
            root.join("Resources"),
            application_support.join("com.poly.desktop"),
            root.join("Caches").join("com.poly.desktop"),
        )
        .unwrap();

        let plan = paths.directory_security_plan().unwrap();

        assert_eq!(plan.shared_root, application_support.as_path());
        assert_eq!(plan.shared_cache_root, root.join("Caches"));
        assert!(!plan
            .owned_directories
            .iter()
            .any(|directory| *directory == application_support
                || *directory == plan.shared_cache_root));
        assert!(plan
            .owned_directories
            .iter()
            .any(|directory| *directory == paths.data_dir()));
    }

    #[cfg(unix)]
    #[test]
    fn directory_preparation_preserves_shared_support_permissions() {
        use std::os::unix::fs::PermissionsExt as _;

        let root =
            std::env::temp_dir().join(format!("poly-desktop-shared-mode-{}", std::process::id()));
        let resources = root.join("Resources");
        let application_support = root.join("Application Support");
        let app_data = application_support.join("com.poly.desktop");
        let shared_cache_root = root.join("Caches");
        let cache = shared_cache_root.join("com.poly.desktop");
        std::fs::create_dir_all(&resources).unwrap();
        std::fs::create_dir_all(&application_support).unwrap();
        std::fs::create_dir_all(&shared_cache_root).unwrap();
        std::fs::set_permissions(&application_support, std::fs::Permissions::from_mode(0o755))
            .unwrap();
        std::fs::set_permissions(&shared_cache_root, std::fs::Permissions::from_mode(0o755))
            .unwrap();
        let before = std::fs::metadata(&application_support)
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        let cache_before = std::fs::metadata(&shared_cache_root)
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        let paths = DesktopPaths::from_resolved(resources, app_data, cache).unwrap();

        paths.prepare_directories().unwrap();

        let after = std::fs::metadata(&application_support)
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(after, before);
        assert_eq!(
            std::fs::metadata(&shared_cache_root)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            cache_before
        );
        assert_eq!(
            std::fs::metadata(paths.data_dir())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            std::fs::metadata(paths.runtime_dir())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn directory_preparation_rejects_a_symlinked_owned_path() {
        use std::os::unix::fs::symlink;

        let root = std::env::temp_dir().join(format!("poly-desktop-paths-{}", std::process::id()));
        let resources = root.join("Resources");
        let support = root.join("Application Support");
        let app_data = support.join("com.poly.desktop");
        let cache = root.join("Caches").join("com.poly.desktop");
        std::fs::create_dir_all(&resources).unwrap();
        std::fs::create_dir_all(&support).unwrap();
        std::fs::create_dir_all(cache.parent().unwrap()).unwrap();
        symlink(root.join("outside"), support.join("Poly")).unwrap();
        let paths = DesktopPaths::from_resolved(resources, app_data, cache).unwrap();

        assert!(matches!(
            paths.prepare_directories(),
            Err(DesktopServiceError::SymbolicLink)
        ));
        std::fs::remove_file(support.join("Poly")).unwrap();
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn finder_reveal_uses_a_fixed_binary_and_the_exact_logs_path() {
        let logs_dir = std::env::current_dir()
            .unwrap()
            .join("Application Support")
            .join("Poly")
            .join("logs");
        let command = finder_reveal_command(&logs_dir);
        let command = command.as_std();

        assert_eq!(command.get_program(), "/usr/bin/open");
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            vec![std::ffi::OsStr::new("-R"), logs_dir.as_os_str()]
        );
    }

    #[test]
    fn service_starts_off_thread_observes_latest_notices_and_retries_terminal_runtime() {
        tauri::async_runtime::block_on(async {
            let FakeServiceFixture {
                service,
                notices,
                navigator,
                supervise_calls,
                retry_calls,
            } = fake_service();
            service.show_initializing().unwrap();
            let observer =
                tauri::async_runtime::spawn(Arc::clone(&service).observe_notices(notices));

            assert!(service.start_supervision());
            wait_for(|| supervise_calls.load(Ordering::SeqCst) == 1).await;
            wait_for(|| navigator.urls.lock().unwrap().iter().any(is_terminal_url)).await;

            let urls = navigator.urls.lock().unwrap().clone();
            assert_eq!(desktop_state(&urls[0]).as_deref(), Some("initializing"));
            let restarting = urls.iter().position(is_restarting_url).unwrap();
            let ready = urls
                .iter()
                .position(|url| url.host_str() == Some("127.0.0.1"))
                .unwrap();
            assert!(restarting < ready);
            assert!(is_terminal_url(urls.last().unwrap()));

            service.retry().await.unwrap();
            wait_for(|| supervise_calls.load(Ordering::SeqCst) == 2).await;
            assert_eq!(retry_calls.load(Ordering::SeqCst), 1);
            observer.abort();
        });
    }

    #[test]
    fn shutdown_returns_promptly_after_terminal_supervision_already_finished() {
        tauri::async_runtime::block_on(async {
            let (notices, _) = watch::channel(None);
            let shutdown = Arc::new(NeverCompletingShutdown::default());
            let service = Arc::new(DesktopRuntimeService::with_reporter_and_shutdown(
                CompletedRuntime {
                    notices,
                    terminal: true,
                },
                FakeNavigator::default(),
                Arc::new(RecordingReporter::default()),
                Arc::clone(&shutdown) as Arc<dyn RuntimeShutdown>,
            ));
            assert!(service.start_supervision());
            wait_for(|| {
                service
                    .supervision_task
                    .lock()
                    .unwrap()
                    .as_ref()
                    .is_some_and(|task| task.inner().is_finished())
            })
            .await;

            tokio::time::timeout(
                Duration::from_millis(50),
                RuntimeShutdown::shutdown(service.as_ref(), "application quit"),
            )
            .await
            .expect("completed supervision shutdown must not hang")
            .unwrap();

            assert_eq!(shutdown.calls.load(Ordering::SeqCst), 0);
        });
    }

    #[test]
    fn shutdown_without_an_active_supervision_task_returns_promptly() {
        tauri::async_runtime::block_on(async {
            let (notices, _) = watch::channel(None);
            let shutdown = Arc::new(NeverCompletingShutdown::default());
            let service = DesktopRuntimeService::with_reporter_and_shutdown(
                CompletedRuntime {
                    notices,
                    terminal: false,
                },
                FakeNavigator::default(),
                Arc::new(RecordingReporter::default()),
                Arc::clone(&shutdown) as Arc<dyn RuntimeShutdown>,
            );

            tokio::time::timeout(
                Duration::from_millis(50),
                RuntimeShutdown::shutdown(&service, "application quit"),
            )
            .await
            .expect("no-active-child shutdown must not hang")
            .unwrap();

            assert_eq!(shutdown.calls.load(Ordering::SeqCst), 0);
        });
    }

    #[test]
    fn active_service_teardown_awaits_task_cancellation_and_exact_cleanup_once() {
        tauri::async_runtime::block_on(async {
            let (notices, _) = watch::channel(None);
            let entered = Arc::new(Notify::new());
            let cancellations = Arc::new(AtomicUsize::new(0));
            let cleanup = Arc::new(ConfirmingCleanup::default());
            let service = Arc::new(DesktopRuntimeService::with_reporter_and_shutdown(
                CancellationRuntime {
                    notices,
                    entered: Arc::clone(&entered),
                    cancellations: Arc::clone(&cancellations),
                },
                FakeNavigator::default(),
                Arc::new(RecordingReporter::default()),
                Arc::clone(&cleanup) as Arc<dyn RuntimeShutdown>,
            ));
            assert!(service.start_supervision());
            entered.notified().await;

            service.cleanup_owned();
            service.cleanup_owned();
            tokio::time::timeout(Duration::from_millis(50), service.wait_for_cleanup())
                .await
                .expect("teardown confirmation must be bounded")
                .unwrap();

            assert_eq!(cancellations.load(Ordering::SeqCst), 1);
            assert_eq!(cleanup.cleanup_calls.load(Ordering::SeqCst), 1);
            assert_eq!(cleanup.confirmation_calls.load(Ordering::SeqCst), 1);
        });
    }

    #[test]
    fn active_service_teardown_surfaces_sanitized_cleanup_failure() {
        tauri::async_runtime::block_on(async {
            let (notices, _) = watch::channel(None);
            let entered = Arc::new(Notify::new());
            let cancellations = Arc::new(AtomicUsize::new(0));
            let cleanup = Arc::new(ConfirmingCleanup::default());
            cleanup.fail_confirmation.store(true, Ordering::SeqCst);
            let service = Arc::new(DesktopRuntimeService::with_reporter_and_shutdown(
                CancellationRuntime {
                    notices,
                    entered: Arc::clone(&entered),
                    cancellations: Arc::clone(&cancellations),
                },
                FakeNavigator::default(),
                Arc::new(RecordingReporter::default()),
                Arc::clone(&cleanup) as Arc<dyn RuntimeShutdown>,
            ));
            assert!(service.start_supervision());
            entered.notified().await;

            service.cleanup_owned();
            assert!(service.wait_for_cleanup().await.is_err());

            assert_eq!(cancellations.load(Ordering::SeqCst), 1);
            assert_eq!(cleanup.cleanup_calls.load(Ordering::SeqCst), 1);
            assert_eq!(cleanup.confirmation_calls.load(Ordering::SeqCst), 1);
        });
    }

    #[test]
    fn rejected_install_awaits_owned_cleanup_confirmation_before_returning() {
        tauri::async_runtime::block_on(async {
            let (notices, _) = watch::channel(None);
            let entered = Arc::new(Notify::new());
            let cancellations = Arc::new(AtomicUsize::new(0));
            let confirmation_started = Arc::new(Notify::new());
            let release_confirmation = Arc::new(Notify::new());
            let cleanup = Arc::new(BlockingCleanupConfirmation {
                cleanup_calls: AtomicUsize::new(0),
                confirmation_calls: AtomicUsize::new(0),
                confirmation_started: Arc::clone(&confirmation_started),
                release_confirmation: Arc::clone(&release_confirmation),
            });
            let reporter = Arc::new(RecordingReporter::default());
            let service = Arc::new(DesktopRuntimeService::with_reporter_and_shutdown(
                CancellationRuntime {
                    notices,
                    entered: Arc::clone(&entered),
                    cancellations: Arc::clone(&cancellations),
                },
                FakeNavigator::default(),
                Arc::clone(&reporter) as Arc<dyn DesktopDiagnosticReporter>,
                Arc::clone(&cleanup) as Arc<dyn RuntimeShutdown>,
            ));
            assert!(service.start_supervision());
            entered.notified().await;

            let registry = RuntimeShutdownRegistry::default();
            registry.shutdown(APPLICATION_QUIT_REASON).await.unwrap();
            assert!(!registry.install(Arc::clone(&service) as Arc<dyn RuntimeShutdown>));

            let completed = Arc::new(AtomicBool::new(false));
            let completed_after_cleanup = Arc::clone(&completed);
            let service_for_cleanup = Arc::clone(&service);
            let reporter_for_cleanup = Arc::clone(&reporter);
            let cleanup_task = tauri::async_runtime::spawn(async move {
                cleanup_rejected_runtime(
                    service_for_cleanup as Arc<dyn RuntimeShutdown>,
                    reporter_for_cleanup as Arc<dyn DesktopDiagnosticReporter>,
                )
                .await;
                completed_after_cleanup.store(true, Ordering::SeqCst);
            });

            confirmation_started.notified().await;
            assert!(!completed.load(Ordering::SeqCst));
            assert_eq!(cancellations.load(Ordering::SeqCst), 1);
            release_confirmation.notify_one();
            cleanup_task.await.unwrap();

            assert!(completed.load(Ordering::SeqCst));
            assert!(service.supervision_task.lock().unwrap().is_none());
            assert_eq!(cleanup.cleanup_calls.load(Ordering::SeqCst), 1);
            assert_eq!(cleanup.confirmation_calls.load(Ordering::SeqCst), 1);
            assert!(reporter.events.lock().unwrap().is_empty());
        });
    }

    #[test]
    fn rejected_install_reports_only_a_sanitized_cleanup_failure() {
        tauri::async_runtime::block_on(async {
            let (notices, _) = watch::channel(None);
            let cleanup = Arc::new(ConfirmingCleanup::default());
            cleanup.fail_confirmation.store(true, Ordering::SeqCst);
            let reporter = Arc::new(RecordingReporter::default());
            let service = Arc::new(DesktopRuntimeService::with_reporter_and_shutdown(
                CompletedRuntime {
                    notices,
                    terminal: false,
                },
                FakeNavigator::default(),
                Arc::clone(&reporter) as Arc<dyn DesktopDiagnosticReporter>,
                Arc::clone(&cleanup) as Arc<dyn RuntimeShutdown>,
            ));
            let registry = RuntimeShutdownRegistry::default();
            registry.shutdown(APPLICATION_QUIT_REASON).await.unwrap();
            assert!(!registry.install(Arc::clone(&service) as Arc<dyn RuntimeShutdown>));

            cleanup_rejected_runtime(
                service as Arc<dyn RuntimeShutdown>,
                Arc::clone(&reporter) as Arc<dyn DesktopDiagnosticReporter>,
            )
            .await;

            assert_eq!(
                *reporter.events.lock().unwrap(),
                vec![DesktopDiagnosticEvent::ShutdownFailed]
            );
        });
    }

    #[test]
    fn close_during_setup_waits_for_rejected_runtime_cleanup_before_exit() {
        tauri::async_runtime::block_on(async {
            let registry = Arc::new(RuntimeShutdownRegistry::default());
            let setup_guard = registry
                .begin_setup()
                .expect("setup must begin while the registry is active");
            let (notices, _) = watch::channel(None);
            let entered = Arc::new(Notify::new());
            let cancellations = Arc::new(AtomicUsize::new(0));
            let confirmation_started = Arc::new(Notify::new());
            let release_confirmation = Arc::new(Notify::new());
            let cleanup = Arc::new(BlockingCleanupConfirmation {
                cleanup_calls: AtomicUsize::new(0),
                confirmation_calls: AtomicUsize::new(0),
                confirmation_started: Arc::clone(&confirmation_started),
                release_confirmation: Arc::clone(&release_confirmation),
            });
            let reporter = Arc::new(RecordingReporter::default());
            let service = Arc::new(DesktopRuntimeService::with_reporter_and_shutdown(
                CancellationRuntime {
                    notices,
                    entered: Arc::clone(&entered),
                    cancellations: Arc::clone(&cancellations),
                },
                FakeNavigator::default(),
                Arc::clone(&reporter) as Arc<dyn DesktopDiagnosticReporter>,
                Arc::clone(&cleanup) as Arc<dyn RuntimeShutdown>,
            ));
            assert!(service.start_supervision());
            entered.notified().await;

            let lifecycle = Arc::new(AppLifecycle::new());
            let app = Arc::new(LifecycleTestApplication::default());
            assert_eq!(
                lifecycle.close_requested(&LifecycleTestWindow),
                CloseDecision::PreventAndShutdown
            );
            let shutdown_task =
                tauri::async_runtime::spawn(Arc::clone(&lifecycle).shutdown_and_exit(
                    Arc::clone(&app) as Arc<dyn LifecycleApplication>,
                    Arc::clone(&registry) as Arc<dyn RuntimeShutdown>,
                ));
            while let Some(guard) = registry.begin_setup() {
                drop(guard);
                tokio::task::yield_now().await;
            }

            assert!(!registry.install(Arc::clone(&service) as Arc<dyn RuntimeShutdown>));
            let setup_task = tauri::async_runtime::spawn(async move {
                cleanup_rejected_runtime(
                    service as Arc<dyn RuntimeShutdown>,
                    reporter as Arc<dyn DesktopDiagnosticReporter>,
                )
                .await;
                drop(setup_guard);
            });

            confirmation_started.notified().await;
            assert_eq!(app.exits.load(Ordering::SeqCst), 0);
            assert_eq!(
                lifecycle.close_requested(&LifecycleTestWindow),
                CloseDecision::PreventAlreadyShuttingDown
            );
            release_confirmation.notify_one();
            setup_task.await.unwrap();
            shutdown_task.await.unwrap();

            assert_eq!(app.exits.load(Ordering::SeqCst), 1);
            assert_eq!(app.reports.load(Ordering::SeqCst), 0);
            assert_eq!(cancellations.load(Ordering::SeqCst), 1);
            assert_eq!(cleanup.cleanup_calls.load(Ordering::SeqCst), 1);
            assert_eq!(cleanup.confirmation_calls.load(Ordering::SeqCst), 1);
        });
    }

    #[test]
    fn hanging_setup_times_out_reports_once_and_allows_exit() {
        tauri::async_runtime::block_on(async {
            let registry = Arc::new(RuntimeShutdownRegistry::with_setup_wait_timeout(
                Duration::from_millis(20),
            ));
            let setup_guard = registry
                .begin_setup()
                .expect("setup must begin while the registry is active");
            let lifecycle = Arc::new(AppLifecycle::new());
            let app = Arc::new(LifecycleTestApplication::default());

            assert_eq!(
                lifecycle.close_requested(&LifecycleTestWindow),
                CloseDecision::PreventAndShutdown
            );
            assert_eq!(
                lifecycle.close_requested(&LifecycleTestWindow),
                CloseDecision::PreventAlreadyShuttingDown
            );
            tokio::time::timeout(
                Duration::from_millis(100),
                Arc::clone(&lifecycle).shutdown_and_exit(
                    Arc::clone(&app) as Arc<dyn LifecycleApplication>,
                    registry as Arc<dyn RuntimeShutdown>,
                ),
            )
            .await
            .expect("setup shutdown barrier must be bounded");

            assert_eq!(app.exits.load(Ordering::SeqCst), 1);
            assert_eq!(app.reports.load(Ordering::SeqCst), 1);
            assert_eq!(
                lifecycle.close_requested(&LifecycleTestWindow),
                CloseDecision::Allow
            );
            drop(setup_guard);
        });
    }

    #[test]
    fn retry_rejects_a_nonterminal_runtime_without_starting_another_supervision() {
        tauri::async_runtime::block_on(async {
            let fixture = fake_service();

            assert!(fixture.service.retry().await.is_err());
            assert_eq!(fixture.retry_calls.load(Ordering::SeqCst), 0);
            assert_eq!(fixture.supervise_calls.load(Ordering::SeqCst), 0);
        });
    }

    #[test]
    fn retry_returns_promptly_while_runtime_supervision_is_active() {
        tauri::async_runtime::block_on(async {
            let (notices, _) = watch::channel(None);
            let entered = Arc::new(Notify::new());
            let release = Arc::new(Notify::new());
            let service = Arc::new(DesktopRuntimeService::new(
                PendingRuntime {
                    notices,
                    entered: Arc::clone(&entered),
                    release: Arc::clone(&release),
                },
                FakeNavigator::default(),
            ));
            assert!(service.start_supervision());
            entered.notified().await;

            let result = tokio::time::timeout(Duration::from_millis(50), service.retry())
                .await
                .expect("retry must not queue behind the active runtime");

            assert!(matches!(result, Err(DesktopServiceError::AlreadyRunning)));
            release.notify_one();
        });
    }

    #[test]
    fn concurrent_terminal_retries_start_exactly_one_new_run() {
        tauri::async_runtime::block_on(async {
            let fixture = fake_service();
            let observer = tauri::async_runtime::spawn(
                Arc::clone(&fixture.service).observe_notices(fixture.notices),
            );
            assert!(fixture.service.start_supervision());
            wait_for(|| fixture.service.phase.load(Ordering::Acquire) == PHASE_TERMINAL).await;

            let barrier = Arc::new(Barrier::new(3));
            let first = {
                let service = Arc::clone(&fixture.service);
                let barrier = Arc::clone(&barrier);
                tauri::async_runtime::spawn(async move {
                    barrier.wait().await;
                    service.retry().await
                })
            };
            let second = {
                let service = Arc::clone(&fixture.service);
                let barrier = Arc::clone(&barrier);
                tauri::async_runtime::spawn(async move {
                    barrier.wait().await;
                    service.retry().await
                })
            };
            barrier.wait().await;
            let (first, second) = tokio::time::timeout(Duration::from_millis(100), async {
                (first.await.unwrap(), second.await.unwrap())
            })
            .await
            .expect("duplicate retry must be rejected without queuing");

            assert_eq!(usize::from(first.is_ok()) + usize::from(second.is_ok()), 1);
            wait_for(|| fixture.supervise_calls.load(Ordering::SeqCst) == 2).await;
            assert_eq!(fixture.retry_calls.load(Ordering::SeqCst), 1);
            observer.abort();
        });
    }

    #[test]
    fn observer_recovers_from_a_navigation_error_and_processes_later_notices() {
        tauri::async_runtime::block_on(async {
            let fixture = fake_service();
            fixture
                .navigator
                .fail_next_restart
                .store(true, Ordering::SeqCst);
            let observer = tauri::async_runtime::spawn(
                Arc::clone(&fixture.service).observe_notices(fixture.notices),
            );

            fixture
                .service
                .runtime
                .lock()
                .await
                .notices
                .send_replace(Some(RuntimeSupervisorNotice::RestartScheduled {
                    attempt: 1,
                    delay: Duration::from_secs(1),
                }));
            wait_for(|| !fixture.navigator.fail_next_restart.load(Ordering::SeqCst)).await;
            fixture
                .service
                .runtime
                .lock()
                .await
                .notices
                .send_replace(Some(RuntimeSupervisorNotice::Runtime(
                    RuntimeEvent::Ready {
                        port: NonZeroU16::new(49152).unwrap(),
                        bootstrap_path: "/desktop/bootstrap/safe".to_owned(),
                    },
                )));

            wait_for(|| fixture.navigator.ready_seen.load(Ordering::SeqCst)).await;
            observer.abort();
        });
    }

    #[test]
    fn observer_retries_a_failed_ready_navigation_without_another_notice() {
        tauri::async_runtime::block_on(async {
            let fixture = fake_service();
            fixture.navigator.ready_failures.store(1, Ordering::SeqCst);
            let observer = tauri::async_runtime::spawn(
                Arc::clone(&fixture.service).observe_notices(fixture.notices),
            );
            fixture
                .service
                .runtime
                .lock()
                .await
                .notices
                .send_replace(Some(RuntimeSupervisorNotice::Runtime(
                    RuntimeEvent::Ready {
                        port: NonZeroU16::new(49152).unwrap(),
                        bootstrap_path: "/desktop/bootstrap/safe".to_owned(),
                    },
                )));

            wait_for(|| fixture.navigator.ready_seen.load(Ordering::SeqCst)).await;
            assert!(fixture
                .navigator
                .urls
                .lock()
                .unwrap()
                .iter()
                .any(|url| url.host_str() == Some("127.0.0.1")));
            observer.abort();
        });
    }

    #[test]
    fn navigation_failures_are_reported_as_sanitized_events() {
        tauri::async_runtime::block_on(async {
            let (notices, _) = watch::channel(None);
            let navigator = FakeNavigator::default();
            navigator.fail_next_restart.store(true, Ordering::SeqCst);
            let reporter = Arc::new(RecordingReporter::default());
            let runtime = PendingRuntime {
                notices,
                entered: Arc::new(Notify::new()),
                release: Arc::new(Notify::new()),
            };
            let receiver = runtime.subscribe_notices();
            let service = Arc::new(DesktopRuntimeService::with_reporter(
                runtime,
                navigator,
                reporter.clone(),
            ));
            let observer =
                tauri::async_runtime::spawn(Arc::clone(&service).observe_notices(receiver));
            service.runtime.lock().await.notices.send_replace(Some(
                RuntimeSupervisorNotice::RestartScheduled {
                    attempt: 1,
                    delay: Duration::from_secs(1),
                },
            ));

            wait_for(|| !reporter.events.lock().unwrap().is_empty()).await;
            assert_eq!(
                reporter.events.lock().unwrap().as_slice(),
                &[DesktopDiagnosticEvent::NavigationFailed]
            );
            observer.abort();
        });
    }

    #[test]
    fn setup_failure_keeps_status_ui_alive_and_retry_recovers_single_flight() {
        tauri::async_runtime::block_on(async {
            let navigator = FakeNavigator::default();
            let reporter = Arc::new(RecordingReporter::default());
            let second_entered = Arc::new(Notify::new());
            let release_second = Arc::new(Notify::new());
            let installer = RecoveringInstaller {
                attempts: AtomicUsize::new(0),
                second_entered: Arc::clone(&second_entered),
                release_second: Arc::clone(&release_second),
                control: Arc::new(FakeRetryControl::default()),
            };
            let setup = Arc::new(RecoverableDesktopSetup::new(
                installer,
                navigator.clone(),
                reporter.clone(),
            ));

            setup.initialize().await;
            assert!(navigator
                .urls
                .lock()
                .unwrap()
                .iter()
                .any(|url| { desktop_state(url).as_deref() == Some("permission_denied") }));
            assert_eq!(
                reporter.events.lock().unwrap().as_slice(),
                &[DesktopDiagnosticEvent::SetupFailed]
            );

            let first = {
                let setup = Arc::clone(&setup);
                tauri::async_runtime::spawn(async move { setup.retry().await })
            };
            wait_for(|| setup.installer().attempts.load(Ordering::SeqCst) == 2).await;
            tokio::time::timeout(Duration::from_millis(50), second_entered.notified())
                .await
                .expect("the setup-entry signal must be retained until observed");
            let duplicate = tokio::time::timeout(Duration::from_millis(50), setup.retry())
                .await
                .expect("a duplicate setup retry must return promptly");
            assert!(matches!(
                duplicate,
                Err(DesktopServiceError::AlreadyRunning)
            ));
            release_second.notify_one();
            first.await.unwrap().unwrap();

            assert_eq!(setup.installer().attempts.load(Ordering::SeqCst), 2);
            assert_eq!(
                setup.logs_dir(),
                Some(PathBuf::from("/safe/Application Support/Poly/logs"))
            );
        });
    }

    async fn wait_for(mut condition: impl FnMut() -> bool) {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
        loop {
            if condition() {
                return;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "condition was not observed"
            );
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    }

    fn desktop_state(url: &Url) -> Option<String> {
        url.query_pairs()
            .find(|(key, _)| key == "desktop-state")
            .map(|(_, value)| value.into_owned())
    }

    fn is_restarting_url(url: &Url) -> bool {
        desktop_state(url).as_deref() == Some("restarting")
    }

    fn is_terminal_url(url: &Url) -> bool {
        desktop_state(url).as_deref() == Some("runtime_unavailable")
    }
}
