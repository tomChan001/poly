use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use thiserror::Error;
use tokio::sync::{mpsc, watch, Mutex};
use url::Url;

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
        for directory in [&self.data_dir, &self.runtime_dir, &self.logs_dir] {
            reject_existing_symlink(directory)?;
            std::fs::create_dir_all(directory).map_err(DesktopServiceError::Directory)?;
        }
        Ok(())
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
}

fn validate_absolute(path: &Path, field: &'static str) -> Result<(), DesktopServiceError> {
    if path.is_absolute() {
        Ok(())
    } else {
        Err(DesktopServiceError::InvalidPath(field))
    }
}

fn reject_existing_symlink(path: &Path) -> Result<(), DesktopServiceError> {
    match std::fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => Err(DesktopServiceError::SymbolicLink),
        Ok(_) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(DesktopServiceError::Directory(error)),
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
    supervision_running: AtomicBool,
}

impl<R, N> DesktopRuntimeService<R, N>
where
    R: DesktopRuntime,
    N: DesktopNavigator,
{
    pub fn new(runtime: R, navigator: N) -> Self {
        Self {
            runtime: Mutex::new(runtime),
            navigator,
            supervision_running: AtomicBool::new(false),
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
                let url = DesktopNavigationController::navigation_for_notice(&notice)
                    .unwrap_or_else(|_| safe_navigation_failure_url());
                if self.navigator.navigate(url).is_err() {
                    let _ = self.navigator.navigate(safe_navigation_failure_url());
                }
            }
        }
        Ok(())
    }

    pub fn start_supervision(self: &Arc<Self>) -> bool {
        if self
            .supervision_running
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return false;
        }

        let service = Arc::clone(self);
        tauri::async_runtime::spawn(async move {
            let mut runtime = service.runtime.lock().await;
            let result = runtime.supervise_until_terminal().await;
            service.supervision_running.store(false, Ordering::Release);
            drop(runtime);
            if result.is_err() {
                let _ = service.navigator.navigate(status_url_with_logs(
                    DesktopUiState::RuntimeUnavailable,
                    true,
                ));
            }
        });
        true
    }

    pub async fn retry(self: &Arc<Self>) -> Result<(), DesktopServiceError> {
        let mut runtime = self.runtime.lock().await;
        if !runtime.is_terminal() {
            return Err(DesktopServiceError::RetryNotTerminal);
        }
        runtime.explicit_operator_retry()?;
        drop(runtime);

        if self.start_supervision() {
            Ok(())
        } else {
            Err(DesktopServiceError::AlreadyRunning)
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

    use tokio::sync::{watch, Notify};
    use url::Url;

    use super::{
        finder_reveal_command, DesktopNavigator, DesktopPaths, DesktopRuntime,
        DesktopRuntimeService, DesktopServiceError,
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
                self.ready_seen.store(true, Ordering::SeqCst);
                self.ready_observed.notify_one();
            }
            self.urls.lock().unwrap().push(url);
            Ok(())
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
    fn retry_rejects_a_nonterminal_runtime_without_starting_another_supervision() {
        tauri::async_runtime::block_on(async {
            let fixture = fake_service();

            assert!(fixture.service.retry().await.is_err());
            assert_eq!(fixture.retry_calls.load(Ordering::SeqCst), 0);
            assert_eq!(fixture.supervise_calls.load(Ordering::SeqCst), 0);
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
