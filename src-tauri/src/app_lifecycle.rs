use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::Arc;
#[cfg(any(target_os = "macos", test))]
use std::sync::RwLock;

use url::Url;

use crate::webview::{status_url, DesktopUiState};

pub const APPLICATION_QUIT_REASON: &str = "application quit";

const ACTIVE: u8 = 0;
const SHUTTING_DOWN: u8 = 1;
const SHUTDOWN_COMPLETE: u8 = 2;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CloseDecision {
    PreventAndShutdown,
    PreventAlreadyShuttingDown,
    Allow,
}

pub trait LifecycleWindow: Send + Sync {
    fn show(&self);
    fn unminimize(&self);
    fn focus(&self);
    fn navigate(&self, url: Url);
}

pub trait LifecycleApplication: Send + Sync {
    fn main_window(&self) -> Option<Box<dyn LifecycleWindow>>;
    fn exit(&self, code: i32);
    fn report_shutdown_failure(&self);
}

pub type ShutdownFuture<'a> = Pin<Box<dyn Future<Output = Result<(), ()>> + Send + 'a>>;

pub trait RuntimeShutdown: Send + Sync {
    fn shutdown(&self, reason: &'static str) -> ShutdownFuture<'_>;
    fn cleanup_owned(&self);
    fn wait_for_cleanup(&self) -> ShutdownFuture<'_> {
        Box::pin(async { Ok(()) })
    }
}

pub fn handle_second_instance(app: &dyn LifecycleApplication) {
    if let Some(window) = app.main_window() {
        window.show();
        window.unminimize();
        window.focus();
    }
}

pub struct AppLifecycle {
    phase: AtomicU8,
    cleanup_requested: AtomicBool,
}

impl Default for AppLifecycle {
    fn default() -> Self {
        Self::new()
    }
}

impl AppLifecycle {
    pub const fn new() -> Self {
        Self {
            phase: AtomicU8::new(ACTIVE),
            cleanup_requested: AtomicBool::new(false),
        }
    }

    pub fn close_requested(&self, window: &dyn LifecycleWindow) -> CloseDecision {
        match self.phase.compare_exchange(
            ACTIVE,
            SHUTTING_DOWN,
            Ordering::AcqRel,
            Ordering::Acquire,
        ) {
            Ok(_) => {
                window.navigate(status_url(DesktopUiState::ShuttingDown));
                CloseDecision::PreventAndShutdown
            }
            Err(SHUTTING_DOWN) => CloseDecision::PreventAlreadyShuttingDown,
            Err(SHUTDOWN_COMPLETE) => CloseDecision::Allow,
            Err(_) => CloseDecision::PreventAlreadyShuttingDown,
        }
    }

    pub async fn shutdown_and_exit(
        self: Arc<Self>,
        app: Arc<dyn LifecycleApplication>,
        runtime: Arc<dyn RuntimeShutdown>,
    ) {
        if runtime.shutdown(APPLICATION_QUIT_REASON).await.is_err() {
            self.cleanup_once(runtime.as_ref());
            let _ = runtime.wait_for_cleanup().await;
            app.report_shutdown_failure();
        }
        self.phase.store(SHUTDOWN_COMPLETE, Ordering::Release);
        app.exit(0);
    }

    pub fn teardown(&self, runtime: &dyn RuntimeShutdown) {
        if self.phase.load(Ordering::Acquire) == SHUTDOWN_COMPLETE {
            return;
        }
        self.cleanup_once(runtime);
    }

    pub async fn teardown_and_wait(&self, runtime: &dyn RuntimeShutdown) -> Result<(), ()> {
        self.teardown(runtime);
        runtime.wait_for_cleanup().await
    }

    pub async fn teardown_and_report(
        &self,
        app: &dyn LifecycleApplication,
        runtime: &dyn RuntimeShutdown,
    ) {
        if self.teardown_and_wait(runtime).await.is_err() {
            app.report_shutdown_failure();
        }
    }

    fn cleanup_once(&self, runtime: &dyn RuntimeShutdown) {
        if self.cleanup_requested.swap(true, Ordering::AcqRel) {
            return;
        }
        runtime.cleanup_owned();
    }
}

#[cfg(any(target_os = "macos", test))]
#[derive(Default)]
pub(crate) struct RuntimeShutdownRegistry {
    current: RwLock<Option<Arc<dyn RuntimeShutdown>>>,
    closing: AtomicBool,
}

#[cfg(any(target_os = "macos", test))]
impl RuntimeShutdownRegistry {
    pub(crate) fn install(&self, shutdown: Arc<dyn RuntimeShutdown>) -> bool {
        let mut current = self
            .current
            .write()
            .expect("runtime shutdown lock poisoned");
        if self.closing.load(Ordering::Acquire) {
            return false;
        }
        *current = Some(shutdown);
        true
    }
}

#[cfg(any(target_os = "macos", test))]
impl RuntimeShutdown for RuntimeShutdownRegistry {
    fn shutdown(&self, reason: &'static str) -> ShutdownFuture<'_> {
        self.closing.store(true, Ordering::Release);
        let current = self
            .current
            .read()
            .expect("runtime shutdown lock poisoned")
            .clone();
        Box::pin(async move {
            if let Some(current) = current {
                current.shutdown(reason).await
            } else {
                Ok(())
            }
        })
    }

    fn cleanup_owned(&self) {
        self.closing.store(true, Ordering::Release);
        if let Some(current) = self
            .current
            .read()
            .expect("runtime shutdown lock poisoned")
            .as_ref()
        {
            current.cleanup_owned();
        }
    }

    fn wait_for_cleanup(&self) -> ShutdownFuture<'_> {
        let current = self
            .current
            .read()
            .expect("runtime shutdown lock poisoned")
            .clone();
        Box::pin(async move {
            if let Some(current) = current {
                current.wait_for_cleanup().await
            } else {
                Ok(())
            }
        })
    }
}

#[cfg(any(target_os = "macos", test))]
#[derive(Clone)]
struct TauriLifecycleWindow {
    window: tauri::WebviewWindow,
}

#[cfg(any(target_os = "macos", test))]
impl LifecycleWindow for TauriLifecycleWindow {
    fn show(&self) {
        let _ = self.window.show();
    }

    fn unminimize(&self) {
        let _ = self.window.unminimize();
    }

    fn focus(&self) {
        let _ = self.window.set_focus();
    }

    fn navigate(&self, url: Url) {
        let _ = self.window.navigate(url);
    }
}

#[cfg(any(target_os = "macos", test))]
#[derive(Clone)]
struct TauriLifecycleApplication {
    app: tauri::AppHandle,
}

#[cfg(any(target_os = "macos", test))]
impl LifecycleApplication for TauriLifecycleApplication {
    fn main_window(&self) -> Option<Box<dyn LifecycleWindow>> {
        use tauri::Manager as _;

        self.app
            .get_webview_window("main")
            .map(|window| Box::new(TauriLifecycleWindow { window }) as Box<dyn LifecycleWindow>)
    }

    fn exit(&self, code: i32) {
        self.app.exit(code);
    }

    fn report_shutdown_failure(&self) {
        use tauri::Manager as _;

        if let Some(state) = self.app.try_state::<crate::DesktopAppState>() {
            crate::desktop_service::DesktopDiagnosticReporter::report(
                state.setup.installer().reporter.as_ref(),
                crate::desktop_service::DesktopDiagnosticEvent::ShutdownFailed,
            );
        }
    }
}

#[cfg(any(target_os = "macos", test))]
fn request_tauri_shutdown(
    app: &tauri::AppHandle,
    window: Option<tauri::WebviewWindow>,
) -> CloseDecision {
    use tauri::Manager as _;

    let Some(state) = app.try_state::<crate::DesktopAppState>() else {
        return CloseDecision::Allow;
    };
    let application = Arc::new(TauriLifecycleApplication { app: app.clone() });
    let window = window
        .map(|window| Box::new(TauriLifecycleWindow { window }) as Box<dyn LifecycleWindow>)
        .or_else(|| LifecycleApplication::main_window(application.as_ref()));
    let Some(window) = window else {
        state.lifecycle.teardown(state.runtime_shutdown.as_ref());
        return CloseDecision::Allow;
    };
    let decision = state.lifecycle.close_requested(window.as_ref());
    if decision == CloseDecision::PreventAndShutdown {
        let lifecycle = Arc::clone(&state.lifecycle);
        let shutdown = Arc::clone(&state.runtime_shutdown);
        tauri::async_runtime::spawn(async move {
            lifecycle
                .shutdown_and_exit(
                    application as Arc<dyn LifecycleApplication>,
                    shutdown as Arc<dyn RuntimeShutdown>,
                )
                .await;
        });
    }
    decision
}

#[cfg(any(target_os = "macos", test))]
fn teardown_tauri_runtime(app: &tauri::AppHandle) {
    use tauri::Manager as _;

    if let Some(state) = app.try_state::<crate::DesktopAppState>() {
        let application = TauriLifecycleApplication { app: app.clone() };
        tauri::async_runtime::block_on(
            state
                .lifecycle
                .teardown_and_report(&application, state.runtime_shutdown.as_ref()),
        );
    }
}

#[cfg(any(target_os = "macos", test))]
pub(crate) fn configure_tauri_builder(
    builder: tauri::Builder<tauri::Wry>,
) -> tauri::Builder<tauri::Wry> {
    use tauri::Manager as _;

    builder
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            handle_second_instance(&TauriLifecycleApplication { app: app.clone() });
        }))
        .invoke_handler(tauri::generate_handler![
            crate::retry_desktop_runtime,
            crate::reveal_desktop_logs
        ])
        .setup(crate::setup_desktop_runtime)
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return;
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let webview = window.app_handle().get_webview_window("main");
                if request_tauri_shutdown(window.app_handle(), webview) != CloseDecision::Allow {
                    api.prevent_close();
                }
            }
        })
}

#[cfg(any(target_os = "macos", test))]
pub(crate) fn handle_tauri_run_event(app: &tauri::AppHandle, event: tauri::RunEvent) {
    use tauri::Manager as _;

    if is_teardown_event(&event) {
        teardown_tauri_runtime(app);
        return;
    }
    if let tauri::RunEvent::ExitRequested { api, .. } = event {
        if request_tauri_shutdown(app, app.get_webview_window("main")) != CloseDecision::Allow {
            api.prevent_exit();
        }
    }
}

#[cfg(any(target_os = "macos", test))]
const fn is_teardown_event(event: &tauri::RunEvent) -> bool {
    matches!(event, tauri::RunEvent::Exit)
}

#[cfg(test)]
mod tests {
    #[test]
    fn production_exit_event_maps_to_confirmed_teardown() {
        assert!(super::is_teardown_event(&tauri::RunEvent::Exit));
    }
}
