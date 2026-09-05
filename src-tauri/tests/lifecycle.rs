use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use poly_desktop_lib::app_lifecycle::{
    handle_second_instance, AppLifecycle, CloseDecision, LifecycleApplication, LifecycleWindow,
    RuntimeShutdown, ShutdownFuture, APPLICATION_QUIT_REASON,
};
use tokio::sync::oneshot;
use url::Url;

#[derive(Default)]
struct FakeWindow {
    shows: AtomicUsize,
    unminimizes: AtomicUsize,
    focuses: AtomicUsize,
    navigations: Mutex<Vec<Url>>,
}

impl LifecycleWindow for FakeWindow {
    fn show(&self) {
        self.shows.fetch_add(1, Ordering::SeqCst);
    }

    fn unminimize(&self) {
        self.unminimizes.fetch_add(1, Ordering::SeqCst);
    }

    fn focus(&self) {
        self.focuses.fetch_add(1, Ordering::SeqCst);
    }

    fn navigate(&self, url: Url) {
        self.navigations.lock().unwrap().push(url);
    }
}

#[derive(Default)]
struct FakeApp {
    window: Arc<FakeWindow>,
    exits: AtomicUsize,
    reports: AtomicUsize,
    runtime_starts: AtomicUsize,
}

impl LifecycleApplication for FakeApp {
    fn main_window(&self) -> Option<Box<dyn LifecycleWindow>> {
        Some(Box::new(SharedWindow(Arc::clone(&self.window))))
    }

    fn exit(&self, code: i32) {
        assert_eq!(code, 0);
        self.exits.fetch_add(1, Ordering::SeqCst);
    }

    fn report_shutdown_failure(&self) {
        self.reports.fetch_add(1, Ordering::SeqCst);
    }
}

struct SharedWindow(Arc<FakeWindow>);

impl LifecycleWindow for SharedWindow {
    fn show(&self) {
        self.0.show();
    }

    fn unminimize(&self) {
        self.0.unminimize();
    }

    fn focus(&self) {
        self.0.focus();
    }

    fn navigate(&self, url: Url) {
        self.0.navigate(url);
    }
}

struct FakeShutdown {
    calls: Mutex<Vec<&'static str>>,
    cleanup_calls: AtomicUsize,
    result: Result<(), ()>,
    release: Mutex<Option<oneshot::Receiver<()>>>,
    started: AtomicBool,
}

impl FakeShutdown {
    fn immediate(result: Result<(), ()>) -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
            cleanup_calls: AtomicUsize::new(0),
            result,
            release: Mutex::new(None),
            started: AtomicBool::new(false),
        }
    }

    fn blocked() -> (Self, oneshot::Sender<()>) {
        let (sender, receiver) = oneshot::channel();
        (
            Self {
                calls: Mutex::new(Vec::new()),
                cleanup_calls: AtomicUsize::new(0),
                result: Ok(()),
                release: Mutex::new(Some(receiver)),
                started: AtomicBool::new(false),
            },
            sender,
        )
    }
}

impl RuntimeShutdown for FakeShutdown {
    fn shutdown(&self, reason: &'static str) -> ShutdownFuture<'_> {
        self.calls.lock().unwrap().push(reason);
        self.started.store(true, Ordering::SeqCst);
        let receiver = self.release.lock().unwrap().take();
        let result = self.result;
        Box::pin(async move {
            if let Some(receiver) = receiver {
                let _ = receiver.await;
            }
            result
        })
    }

    fn cleanup_owned(&self) {
        self.cleanup_calls.fetch_add(1, Ordering::SeqCst);
    }
}

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap()
}

#[test]
fn second_instance_only_restores_and_focuses_existing_window() {
    let app = FakeApp::default();

    handle_second_instance(&app);

    assert_eq!(app.window.shows.load(Ordering::SeqCst), 1);
    assert_eq!(app.window.unminimizes.load(Ordering::SeqCst), 1);
    assert_eq!(app.window.focuses.load(Ordering::SeqCst), 1);
    assert_eq!(app.runtime_starts.load(Ordering::SeqCst), 0);
}

#[test]
fn close_is_prevented_until_the_runtime_stops_then_exit_is_requested() {
    runtime().block_on(async {
        let lifecycle = Arc::new(AppLifecycle::new());
        let app = Arc::new(FakeApp::default());
        let (shutdown, release) = FakeShutdown::blocked();
        let shutdown = Arc::new(shutdown);

        assert_eq!(
            lifecycle.close_requested(app.window.as_ref()),
            CloseDecision::PreventAndShutdown
        );
        {
            let navigations = app.window.navigations.lock().unwrap();
            assert_eq!(navigations.len(), 1);
            assert_eq!(navigations[0].scheme(), "tauri");
            assert!(navigations[0]
                .query_pairs()
                .any(|(key, value)| key == "desktop-state" && value == "shutting_down"));
        }

        let task = tokio::spawn(Arc::clone(&lifecycle).shutdown_and_exit(
            Arc::clone(&app) as Arc<dyn LifecycleApplication>,
            Arc::clone(&shutdown) as Arc<dyn RuntimeShutdown>,
        ));
        tokio::task::yield_now().await;

        assert!(shutdown.started.load(Ordering::SeqCst));
        assert_eq!(app.exits.load(Ordering::SeqCst), 0);
        release.send(()).unwrap();
        task.await.unwrap();

        assert_eq!(app.exits.load(Ordering::SeqCst), 1);
        assert_eq!(
            lifecycle.close_requested(app.window.as_ref()),
            CloseDecision::Allow
        );
    });
}

#[test]
fn repeated_close_is_single_flight_and_sends_one_application_quit() {
    runtime().block_on(async {
        let lifecycle = Arc::new(AppLifecycle::new());
        let app = Arc::new(FakeApp::default());
        let shutdown = Arc::new(FakeShutdown::immediate(Ok(())));

        assert_eq!(
            lifecycle.close_requested(app.window.as_ref()),
            CloseDecision::PreventAndShutdown
        );
        assert_eq!(
            lifecycle.close_requested(app.window.as_ref()),
            CloseDecision::PreventAlreadyShuttingDown
        );
        Arc::clone(&lifecycle)
            .shutdown_and_exit(
                Arc::clone(&app) as Arc<dyn LifecycleApplication>,
                Arc::clone(&shutdown) as Arc<dyn RuntimeShutdown>,
            )
            .await;

        assert_eq!(*shutdown.calls.lock().unwrap(), [APPLICATION_QUIT_REASON]);
        assert_eq!(app.window.navigations.lock().unwrap().len(), 1);
        assert_eq!(app.exits.load(Ordering::SeqCst), 1);
    });
}

#[test]
fn shutdown_failure_is_reported_without_blocking_exit() {
    runtime().block_on(async {
        let lifecycle = Arc::new(AppLifecycle::new());
        let app = Arc::new(FakeApp::default());
        let shutdown = Arc::new(FakeShutdown::immediate(Err(())));

        assert_eq!(
            lifecycle.close_requested(app.window.as_ref()),
            CloseDecision::PreventAndShutdown
        );
        Arc::clone(&lifecycle)
            .shutdown_and_exit(
                Arc::clone(&app) as Arc<dyn LifecycleApplication>,
                Arc::clone(&shutdown) as Arc<dyn RuntimeShutdown>,
            )
            .await;

        assert_eq!(app.reports.load(Ordering::SeqCst), 1);
        assert_eq!(app.exits.load(Ordering::SeqCst), 1);
        assert_eq!(shutdown.cleanup_calls.load(Ordering::SeqCst), 1);
    });
}

#[test]
fn teardown_requests_exact_owned_cleanup_only_once() {
    let lifecycle = AppLifecycle::new();
    let shutdown = FakeShutdown::immediate(Ok(()));

    lifecycle.teardown(&shutdown);
    lifecycle.teardown(&shutdown);

    assert_eq!(shutdown.cleanup_calls.load(Ordering::SeqCst), 1);
}

#[test]
fn completed_shutdown_needs_no_teardown_cleanup() {
    runtime().block_on(async {
        let lifecycle = Arc::new(AppLifecycle::new());
        let app = Arc::new(FakeApp::default());
        let shutdown = Arc::new(FakeShutdown::immediate(Ok(())));
        assert_eq!(
            lifecycle.close_requested(app.window.as_ref()),
            CloseDecision::PreventAndShutdown
        );
        Arc::clone(&lifecycle)
            .shutdown_and_exit(
                Arc::clone(&app) as Arc<dyn LifecycleApplication>,
                Arc::clone(&shutdown) as Arc<dyn RuntimeShutdown>,
            )
            .await;

        lifecycle.teardown(shutdown.as_ref());

        assert_eq!(shutdown.cleanup_calls.load(Ordering::SeqCst), 0);
    });
}
