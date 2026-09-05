pub mod app_lifecycle;
pub mod desktop_service;
pub mod runtime;
pub mod webview;

pub use runtime::process::{launch_runtime, ProductionLauncher, RunningRuntime};
pub use runtime::supervisor::{
    RuntimeShutdownControl, RuntimeSupervisor, RuntimeSupervisorNotice, SupervisionOutcome,
    SystemClock,
};
use runtime::{
    protocol::{FailureCode, RuntimeEvent},
    supervisor::RuntimeFailure,
};
use url::Url;
use webview::{ready_url, status_url_with_logs, DesktopUiState, NavigationError};

#[cfg(any(target_os = "macos", test))]
use desktop_service::{
    finder_reveal_command, DesktopDiagnosticEvent, DesktopDiagnosticReporter, DesktopInstaller,
    DesktopNavigator, DesktopPaths, DesktopRuntimeService, DesktopServiceError,
    DesktopSetupFailure, InstalledDesktopRuntime, RecoverableDesktopSetup,
};
#[cfg(any(target_os = "macos", test))]
use std::sync::Arc;
#[cfg(any(target_os = "macos", test))]
use std::sync::Mutex;
#[cfg(any(target_os = "macos", test))]
use webview::status_url;

pub struct DesktopNavigationController;

impl DesktopNavigationController {
    pub fn navigation_for_notice(notice: &RuntimeSupervisorNotice) -> Result<Url, NavigationError> {
        match notice {
            RuntimeSupervisorNotice::Runtime(RuntimeEvent::Ready {
                port,
                bootstrap_path,
            }) => ready_url(port.get(), bootstrap_path),
            RuntimeSupervisorNotice::Runtime(event) => Ok(status_url_with_logs(
                ui_state_for_runtime_event(event),
                matches!(event, RuntimeEvent::Failed { .. }),
            )),
            RuntimeSupervisorNotice::RestartScheduled { .. } => {
                Ok(status_url_with_logs(DesktopUiState::Restarting, false))
            }
            RuntimeSupervisorNotice::Terminal(failure) => {
                Ok(status_url_with_logs(ui_state_for_failure(failure), true))
            }
            RuntimeSupervisorNotice::Stopped => {
                Ok(status_url_with_logs(DesktopUiState::ShuttingDown, false))
            }
        }
    }
}

#[cfg(any(target_os = "macos", test))]
#[derive(Clone)]
struct TauriDesktopNavigator {
    window: tauri::WebviewWindow,
}

#[cfg(any(target_os = "macos", test))]
impl DesktopNavigator for TauriDesktopNavigator {
    fn navigate(&self, url: Url) -> Result<(), DesktopServiceError> {
        self.window
            .navigate(url)
            .map_err(|_| DesktopServiceError::Navigation)
    }
}

#[cfg(any(target_os = "macos", test))]
struct BoundedDesktopReporter {
    log: Mutex<Option<runtime::process::DiagnosticLog>>,
    dropped: std::sync::atomic::AtomicUsize,
}

#[cfg(any(target_os = "macos", test))]
impl BoundedDesktopReporter {
    fn new() -> Self {
        Self {
            log: Mutex::new(None),
            dropped: std::sync::atomic::AtomicUsize::new(0),
        }
    }

    fn configure(&self, application_support: &std::path::Path) {
        match runtime::process::DiagnosticLog::under_application_support(application_support) {
            Ok(log) => *self.log.lock().expect("diagnostic log lock poisoned") = Some(log),
            Err(_) => {
                self.dropped
                    .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            }
        }
    }
}

#[cfg(any(target_os = "macos", test))]
impl DesktopDiagnosticReporter for BoundedDesktopReporter {
    fn report(&self, event: DesktopDiagnosticEvent) {
        let label = match event {
            DesktopDiagnosticEvent::NoticeRejected => "desktop_notice_rejected\n",
            DesktopDiagnosticEvent::NavigationFailed => "desktop_navigation_failed\n",
            DesktopDiagnosticEvent::NavigationFallbackFailed => {
                "desktop_navigation_fallback_failed\n"
            }
            DesktopDiagnosticEvent::SupervisionFailed => "desktop_supervision_failed\n",
            DesktopDiagnosticEvent::SetupFailed => "desktop_setup_failed\n",
            DesktopDiagnosticEvent::ShutdownFailed => "desktop_shutdown_failed\n",
        };
        let result = self
            .log
            .lock()
            .expect("diagnostic log lock poisoned")
            .as_mut()
            .map(|log| log.write_redacted(label));
        if !matches!(result, Some(Ok(()))) {
            self.dropped
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        }
    }
}

#[cfg(any(target_os = "macos", test))]
#[derive(Clone)]
struct ProductionDesktopInstaller {
    app: tauri::AppHandle,
    navigator: TauriDesktopNavigator,
    reporter: Arc<BoundedDesktopReporter>,
    runtime_shutdown: Arc<app_lifecycle::RuntimeShutdownRegistry>,
}

#[cfg(any(target_os = "macos", test))]
impl DesktopInstaller for ProductionDesktopInstaller {
    fn install(&self) -> desktop_service::DesktopInstallFuture<'_> {
        use tauri::Manager as _;

        Box::pin(async move {
            let paths = DesktopPaths::from_resolved(
                self.app
                    .path()
                    .resource_dir()
                    .map_err(|_| DesktopSetupFailure::ResourceMissing { logs_dir: None })?,
                self.app
                    .path()
                    .app_data_dir()
                    .map_err(|_| DesktopSetupFailure::PermissionDenied { logs_dir: None })?,
                self.app
                    .path()
                    .app_cache_dir()
                    .map_err(|_| DesktopSetupFailure::PermissionDenied { logs_dir: None })?,
            )
            .map_err(|_| DesktopSetupFailure::RuntimeUnavailable { logs_dir: None })?;
            if let Err(error) = paths.prepare_directories() {
                let logs_dir = paths.revealable_logs_dir();
                return Err(match error {
                    DesktopServiceError::Directory(source)
                        if source.kind() == std::io::ErrorKind::PermissionDenied =>
                    {
                        DesktopSetupFailure::PermissionDenied { logs_dir }
                    }
                    DesktopServiceError::SymbolicLink | DesktopServiceError::InvalidPath(_) => {
                        DesktopSetupFailure::PermissionDenied { logs_dir }
                    }
                    _ => DesktopSetupFailure::RuntimeUnavailable { logs_dir },
                });
            }
            self.reporter.configure(paths.application_support());

            let supervisor = RuntimeSupervisor::production(
                ProductionLauncher::new(paths.resource_dir().to_path_buf()),
                paths.data_dir().to_path_buf(),
                paths.runtime_dir().to_path_buf(),
                paths.application_support().to_path_buf(),
            );
            let shutdown = Arc::new(supervisor.shutdown_control());
            let notices = supervisor.subscribe_notices();
            let service = Arc::new(DesktopRuntimeService::with_reporter_and_shutdown(
                supervisor,
                self.navigator.clone(),
                self.reporter.clone(),
                shutdown,
            ));
            let observer = Arc::clone(&service);
            tauri::async_runtime::spawn(async move {
                let _ = observer.observe_notices(notices).await;
            });
            if !service.start_supervision() {
                return Err(DesktopSetupFailure::RuntimeUnavailable {
                    logs_dir: paths.revealable_logs_dir(),
                });
            }
            if !self
                .runtime_shutdown
                .install(Arc::clone(&service) as Arc<dyn app_lifecycle::RuntimeShutdown>)
            {
                app_lifecycle::RuntimeShutdown::cleanup_owned(service.as_ref());
                return Err(DesktopSetupFailure::RuntimeUnavailable {
                    logs_dir: paths.revealable_logs_dir(),
                });
            }
            Ok(InstalledDesktopRuntime::new(
                Arc::new(service),
                Some(paths.logs_dir().to_path_buf()),
            ))
        })
    }
}

#[cfg(any(target_os = "macos", test))]
type ProductionDesktopSetup =
    RecoverableDesktopSetup<ProductionDesktopInstaller, TauriDesktopNavigator>;

#[cfg(any(target_os = "macos", test))]
struct DesktopAppState {
    setup: Arc<ProductionDesktopSetup>,
    lifecycle: Arc<app_lifecycle::AppLifecycle>,
    runtime_shutdown: Arc<app_lifecycle::RuntimeShutdownRegistry>,
}

#[cfg(any(target_os = "macos", test))]
impl Drop for DesktopAppState {
    fn drop(&mut self) {
        self.lifecycle.teardown(self.runtime_shutdown.as_ref());
    }
}

#[cfg(any(target_os = "macos", test))]
#[tauri::command]
async fn retry_desktop_runtime(state: tauri::State<'_, DesktopAppState>) -> Result<(), String> {
    state
        .setup
        .retry()
        .await
        .map_err(|_| "runtime retry is unavailable".to_owned())
}

#[cfg(any(target_os = "macos", test))]
#[tauri::command]
async fn reveal_desktop_logs(state: tauri::State<'_, DesktopAppState>) -> Result<(), String> {
    let logs_dir = state
        .setup
        .logs_dir()
        .ok_or_else(|| "diagnostic logs are unavailable".to_owned())?;
    let status = finder_reveal_command(&logs_dir)
        .status()
        .await
        .map_err(|_| "diagnostic logs could not be revealed".to_owned())?;
    if status.success() {
        Ok(())
    } else {
        Err("diagnostic logs could not be revealed".to_owned())
    }
}

#[cfg(any(target_os = "macos", test))]
fn setup_desktop_runtime(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    use tauri::Manager as _;

    let Some(window) = app.get_webview_window("main") else {
        return Ok(());
    };
    let navigator = TauriDesktopNavigator { window };
    let reporter = Arc::new(BoundedDesktopReporter::new());
    let runtime_shutdown = Arc::new(app_lifecycle::RuntimeShutdownRegistry::default());
    if navigator
        .navigate(status_url(DesktopUiState::Initializing))
        .is_err()
    {
        reporter.report(DesktopDiagnosticEvent::NavigationFailed);
    }
    let setup = Arc::new(RecoverableDesktopSetup::new(
        ProductionDesktopInstaller {
            app: app.handle().clone(),
            navigator: navigator.clone(),
            reporter: reporter.clone(),
            runtime_shutdown: Arc::clone(&runtime_shutdown),
        },
        navigator,
        reporter,
    ));
    if !app.manage(DesktopAppState {
        setup: Arc::clone(&setup),
        lifecycle: Arc::new(app_lifecycle::AppLifecycle::new()),
        runtime_shutdown,
    }) {
        return Ok(());
    }
    tauri::async_runtime::spawn(async move {
        setup.initialize().await;
    });
    Ok(())
}

fn ui_state_for_runtime_event(event: &RuntimeEvent) -> DesktopUiState {
    match event {
        RuntimeEvent::Initializing => DesktopUiState::Initializing,
        RuntimeEvent::PreparingDatabase => DesktopUiState::PreparingDatabase,
        RuntimeEvent::Migrating { .. } => DesktopUiState::Migrating,
        RuntimeEvent::StartingServices => DesktopUiState::StartingServices,
        RuntimeEvent::ShuttingDown | RuntimeEvent::Stopped { .. } => DesktopUiState::ShuttingDown,
        RuntimeEvent::Failed { code, .. } => ui_state_for_failure_code(*code),
        RuntimeEvent::Ready { .. } => unreachable!("ready events are handled before status events"),
    }
}

fn ui_state_for_failure(failure: &RuntimeFailure) -> DesktopUiState {
    match failure {
        RuntimeFailure::Reported(code) => ui_state_for_failure_code(*code),
        RuntimeFailure::PermissionDenied => DesktopUiState::PermissionDenied,
        RuntimeFailure::ResourceMissing => DesktopUiState::ResourceMissing,
        RuntimeFailure::Protocol => DesktopUiState::ProtocolFailed,
        RuntimeFailure::UnexpectedExit | RuntimeFailure::CleanupFailed => {
            DesktopUiState::RuntimeUnavailable
        }
    }
}

const fn ui_state_for_failure_code(code: FailureCode) -> DesktopUiState {
    match code {
        FailureCode::MigrationFailed => DesktopUiState::MigrationFailed,
        FailureCode::ResourceMissing => DesktopUiState::ResourceMissing,
        FailureCode::DatabaseUnavailable
        | FailureCode::InvalidStartCommand
        | FailureCode::RuntimeUnavailable
        | FailureCode::ShutdownFailed => DesktopUiState::RuntimeUnavailable,
    }
}

#[cfg(target_os = "macos")]
pub fn run() {
    let app = app_lifecycle::configure_tauri_builder(tauri::Builder::default())
        .build(tauri::generate_context!())
        .expect("failed to build Poly desktop shell");
    app.run(app_lifecycle::handle_tauri_run_event);
}

#[cfg(not(target_os = "macos"))]
pub fn run() {
    panic!("Poly desktop is supported on macOS only");
}

#[cfg(test)]
mod tests {
    use std::num::NonZeroU16;

    use crate::runtime::protocol::{FailureCode, RuntimeEvent, RuntimeState};
    use crate::runtime::state::{Supervisor, SupervisorAction, SupervisorState};
    use crate::runtime::supervisor::{RuntimeFailure, RuntimeSupervisorNotice};
    use crate::webview::DesktopUiState;
    use crate::DesktopNavigationController;

    fn parse(json: &str) -> RuntimeEvent {
        RuntimeEvent::parse_line(json).expect("valid runtime event")
    }

    #[test]
    fn parses_every_exact_v1_runtime_state() {
        let cases = [
            (
                r#"{"version":1,"state":"initializing"}"#,
                RuntimeState::Initializing,
            ),
            (
                r#"{"version":1,"state":"preparing_database"}"#,
                RuntimeState::PreparingDatabase,
            ),
            (
                r#"{"version":1,"state":"migrating"}"#,
                RuntimeState::Migrating,
            ),
            (
                r#"{"version":1,"state":"starting_services"}"#,
                RuntimeState::StartingServices,
            ),
            (
                r#"{"version":1,"state":"ready","port":49152,"bootstrap_path":"/desktop/bootstrap/safe"}"#,
                RuntimeState::Ready,
            ),
            (
                r#"{"version":1,"state":"shutting_down"}"#,
                RuntimeState::ShuttingDown,
            ),
            (r#"{"version":1,"state":"stopped"}"#, RuntimeState::Stopped),
            (
                r#"{"version":1,"state":"failed","code":"migration_failed","detail":"database migration failed"}"#,
                RuntimeState::Failed,
            ),
        ];

        for (line, expected) in cases {
            assert_eq!(parse(line).state(), expected);
        }
    }

    #[test]
    fn rejects_non_v1_unknown_and_polluted_events() {
        for line in [
            r#"{"version":2,"state":"initializing"}"#,
            r#"{"version":true,"state":"initializing"}"#,
            r#"{"version":1,"state":"restarting"}"#,
            r#"{"version":1,"state":"initializing","unexpected":true}"#,
            "backend log output",
        ] {
            assert!(RuntimeEvent::parse_line(line).is_err(), "accepted {line}");
        }
    }

    #[test]
    fn ready_requires_a_nonzero_port_and_safe_bootstrap_path() {
        let ready = parse(
            r#"{"version":1,"state":"ready","port":49152,"bootstrap_path":"/desktop/bootstrap/safe_token-1"}"#,
        );
        assert_eq!(ready.ready_port(), Some(NonZeroU16::new(49152).unwrap()));
        assert_eq!(
            ready.bootstrap_path(),
            Some("/desktop/bootstrap/safe_token-1")
        );

        for line in [
            r#"{"version":1,"state":"ready","bootstrap_path":"/desktop/bootstrap/safe"}"#,
            r#"{"version":1,"state":"ready","port":0,"bootstrap_path":"/desktop/bootstrap/safe"}"#,
            r#"{"version":1,"state":"ready","port":49152,"bootstrap_path":"/other/safe"}"#,
            r#"{"version":1,"state":"ready","port":49152,"bootstrap_path":"/desktop/bootstrap/../admin"}"#,
            r#"{"version":1,"state":"ready","port":49152,"bootstrap_path":"/desktop/bootstrap/safe?leak=1"}"#,
            r#"{"version":1,"state":"ready","port":49152,"bootstrap_path":"https://attacker.invalid/desktop/bootstrap/safe"}"#,
        ] {
            assert!(RuntimeEvent::parse_line(line).is_err(), "accepted {line}");
        }
    }

    #[test]
    fn rejects_unsafe_diagnostic_data() {
        for line in [
            "{\"version\":1,\"state\":\"failed\",\"code\":\"bad code\",\"detail\":\"safe\"}",
            "{\"version\":1,\"state\":\"failed\",\"code\":\"runtime_unavailable\",\"detail\":\"token\\nleak\"}",
            r#"{"version":1,"state":"migrating","revision":"../../secret"}"#,
        ] {
            assert!(RuntimeEvent::parse_line(line).is_err(), "accepted {line}");
        }
    }

    #[test]
    fn rejects_unknown_failure_codes() {
        let line = r#"{"version":1,"state":"failed","code":"future_failure","detail":"not in the version one contract"}"#;

        assert!(RuntimeEvent::parse_line(line).is_err());
    }

    #[test]
    fn supervisor_follows_ordered_runtime_transitions() {
        let mut supervisor = Supervisor::new();
        let events = [
            parse(r#"{"version":1,"state":"initializing"}"#),
            parse(r#"{"version":1,"state":"preparing_database"}"#),
            parse(r#"{"version":1,"state":"migrating"}"#),
            parse(r#"{"version":1,"state":"starting_services"}"#),
            parse(
                r#"{"version":1,"state":"ready","port":49152,"bootstrap_path":"/desktop/bootstrap/safe"}"#,
            ),
            parse(r#"{"version":1,"state":"shutting_down"}"#),
            parse(r#"{"version":1,"state":"stopped"}"#),
        ];

        for event in events {
            assert_eq!(supervisor.apply(event).unwrap(), SupervisorAction::None);
        }
        assert_eq!(supervisor.state(), &SupervisorState::Stopped);
    }

    #[test]
    fn supervisor_rejects_out_of_order_events() {
        let mut supervisor = Supervisor::new();
        let ready = parse(
            r#"{"version":1,"state":"ready","port":49152,"bootstrap_path":"/desktop/bootstrap/safe"}"#,
        );

        assert!(supervisor.apply(ready).is_err());
        assert_eq!(supervisor.state(), &SupervisorState::Idle);
    }

    #[test]
    fn deterministic_failures_are_terminal() {
        let mut supervisor = Supervisor::new();
        supervisor
            .apply(parse(r#"{"version":1,"state":"initializing"}"#))
            .unwrap();

        let action = supervisor
            .apply(parse(
                r#"{"version":1,"state":"failed","code":"migration_failed","detail":"database migration failed"}"#,
            ))
            .unwrap();

        assert_eq!(action, SupervisorAction::Terminal);
        assert!(matches!(supervisor.state(), SupervisorState::Failed { .. }));
    }

    #[test]
    fn state_machine_has_no_competing_retry_counter() {
        let mut supervisor = Supervisor::new();
        supervisor
            .apply(parse(r#"{"version":1,"state":"initializing"}"#))
            .unwrap();
        let action = supervisor
            .apply(parse(
                r#"{"version":1,"state":"failed","code":"runtime_unavailable","detail":"runtime exited"}"#,
            ))
            .unwrap();

        assert_eq!(action, SupervisorAction::Terminal);
        assert!(matches!(supervisor.state(), SupervisorState::Failed { .. }));

        supervisor.begin_restart(2).unwrap();
        assert_eq!(
            supervisor.state(),
            &SupervisorState::Restarting { attempt: 2 }
        );
        supervisor
            .apply(parse(r#"{"version":1,"state":"initializing"}"#))
            .unwrap();
    }

    #[test]
    fn explicit_operator_retry_resets_terminal_state_to_idle() {
        let mut supervisor = Supervisor::new();
        supervisor
            .apply(parse(r#"{"version":1,"state":"initializing"}"#))
            .unwrap();
        supervisor
            .apply(parse(
                r#"{"version":1,"state":"failed","code":"migration_failed","detail":"database migration failed"}"#,
            ))
            .unwrap();

        supervisor.reset_for_operator_retry().unwrap();

        assert_eq!(supervisor.state(), &SupervisorState::Idle);
        supervisor
            .apply(parse(r#"{"version":1,"state":"initializing"}"#))
            .unwrap();
    }

    #[test]
    fn explicit_operator_retry_can_cancel_a_pending_restart() {
        let mut supervisor = Supervisor::new();
        supervisor.begin_restart(1).unwrap();

        supervisor.reset_for_operator_retry().unwrap();

        assert_eq!(supervisor.state(), &SupervisorState::Idle);
    }

    #[test]
    fn desktop_controller_maps_runtime_lifecycle_notices() {
        let cases = [
            (
                RuntimeSupervisorNotice::Runtime(RuntimeEvent::Initializing),
                DesktopUiState::Initializing,
            ),
            (
                RuntimeSupervisorNotice::Runtime(RuntimeEvent::PreparingDatabase),
                DesktopUiState::PreparingDatabase,
            ),
            (
                RuntimeSupervisorNotice::Runtime(RuntimeEvent::Migrating { revision: None }),
                DesktopUiState::Migrating,
            ),
            (
                RuntimeSupervisorNotice::Runtime(RuntimeEvent::StartingServices),
                DesktopUiState::StartingServices,
            ),
            (
                RuntimeSupervisorNotice::RestartScheduled {
                    attempt: 1,
                    delay: std::time::Duration::from_secs(1),
                },
                DesktopUiState::Restarting,
            ),
            (
                RuntimeSupervisorNotice::Terminal(RuntimeFailure::PermissionDenied),
                DesktopUiState::PermissionDenied,
            ),
            (
                RuntimeSupervisorNotice::Terminal(RuntimeFailure::ResourceMissing),
                DesktopUiState::ResourceMissing,
            ),
            (
                RuntimeSupervisorNotice::Terminal(RuntimeFailure::Protocol),
                DesktopUiState::ProtocolFailed,
            ),
            (
                RuntimeSupervisorNotice::Runtime(RuntimeEvent::Failed {
                    code: FailureCode::MigrationFailed,
                    detail: "migration failed".to_owned(),
                }),
                DesktopUiState::MigrationFailed,
            ),
            (
                RuntimeSupervisorNotice::Stopped,
                DesktopUiState::ShuttingDown,
            ),
        ];

        for (notice, expected_state) in cases {
            let url = DesktopNavigationController::navigation_for_notice(&notice).unwrap();
            assert_eq!(
                url.query_pairs()
                    .find(|(key, _)| key == "desktop-state")
                    .map(|(_, value)| value.into_owned()),
                Some(expected_state.as_str().to_owned())
            );
        }
    }

    #[test]
    fn desktop_controller_navigates_ready_only_to_validated_loopback_url() {
        let ready = RuntimeSupervisorNotice::Runtime(RuntimeEvent::Ready {
            port: NonZeroU16::new(49152).unwrap(),
            bootstrap_path: "/desktop/bootstrap/safe_token-1".to_owned(),
        });

        let url = DesktopNavigationController::navigation_for_notice(&ready).unwrap();

        assert_eq!(
            url.as_str(),
            "http://127.0.0.1:49152/desktop/bootstrap/safe_token-1"
        );
    }

    #[test]
    fn a_disconnect_notice_immediately_maps_to_restarting_before_relaunch() {
        let disconnected = RuntimeSupervisorNotice::RestartScheduled {
            attempt: 2,
            delay: std::time::Duration::from_secs(2),
        };

        let url = DesktopNavigationController::navigation_for_notice(&disconnected).unwrap();

        assert_eq!(url.scheme(), "tauri");
        assert_eq!(
            url.query_pairs()
                .find(|(key, _)| key == "desktop-state")
                .map(|(_, value)| value.into_owned()),
            Some("restarting".to_owned())
        );
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn production_desktop_setup_and_commands_are_type_checked_portably() {
        let _setup = super::setup_desktop_runtime;
        let _retry = super::retry_desktop_runtime;
        let _reveal = super::reveal_desktop_logs;
        let _configure = crate::app_lifecycle::configure_tauri_builder;
        let _run_event = crate::app_lifecycle::handle_tauri_run_event;
    }
}
