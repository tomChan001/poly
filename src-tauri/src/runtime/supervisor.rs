use std::collections::VecDeque;
use std::future::{poll_fn, Future};
use std::path::PathBuf;
use std::pin::Pin;
use std::time::{Duration, Instant};

use rand::Rng as _;
use thiserror::Error;
use tokio::sync::{mpsc, watch};

use super::process::{
    drain_stderr, launch_runtime, read_runtime_event, DiagnosticLog, LaunchError, ProcessError,
    RunningRuntime, RuntimeLauncher, RuntimeStreamItem,
};
use super::protocol::{FailureCode, RuntimeEvent};
use super::state::{Supervisor, SupervisorState, TransitionError};

const MAX_RESTARTS: u8 = 3;
const CRASH_WINDOW: Duration = Duration::from_secs(60);
const STABLE_RUN: Duration = Duration::from_secs(5 * 60);
const MAX_DELAY: Duration = Duration::from_secs(8);
const MAX_JITTER: Duration = Duration::from_millis(250);

type StderrDrain = Pin<Box<dyn Future<Output = std::io::Result<()>> + Send>>;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum JitterMode {
    Production,
    Disabled,
}

#[derive(Debug)]
pub struct RestartPolicy {
    crash_times: VecDeque<Duration>,
    jitter: JitterMode,
    terminal: bool,
}

impl RestartPolicy {
    pub fn production() -> Self {
        Self {
            crash_times: VecDeque::with_capacity(usize::from(MAX_RESTARTS) + 1),
            jitter: JitterMode::Production,
            terminal: false,
        }
    }

    pub fn deterministic() -> Self {
        Self {
            crash_times: VecDeque::with_capacity(usize::from(MAX_RESTARTS) + 1),
            jitter: JitterMode::Disabled,
            terminal: false,
        }
    }

    pub fn record_crash(&mut self, now: Duration, runtime_uptime: Duration) -> RestartDecision {
        if self.terminal {
            return RestartDecision::Terminal;
        }
        if runtime_uptime >= STABLE_RUN {
            self.crash_times.clear();
        }
        while self
            .crash_times
            .front()
            .is_some_and(|crash| now.saturating_sub(*crash) >= CRASH_WINDOW)
        {
            self.crash_times.pop_front();
        }
        self.crash_times.push_back(now);
        let attempt = self.crash_times.len() as u8;
        if attempt > MAX_RESTARTS {
            self.terminal = true;
            return RestartDecision::Terminal;
        }

        let base_seconds = 1_u64 << (attempt - 1);
        let base = Duration::from_secs(base_seconds).min(MAX_DELAY);
        let jitter = match self.jitter {
            JitterMode::Disabled => Duration::ZERO,
            JitterMode::Production => {
                let millis = rand::rng().random_range(0..=MAX_JITTER.as_millis() as u64);
                Duration::from_millis(millis)
            }
        };
        RestartDecision::Retry {
            attempt,
            delay: (base + jitter).min(MAX_DELAY),
        }
    }

    pub fn explicit_retry(&mut self) {
        self.crash_times.clear();
        self.terminal = false;
    }
}

impl Default for RestartPolicy {
    fn default() -> Self {
        Self::production()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RestartDecision {
    Retry { attempt: u8, delay: Duration },
    Terminal,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FailureDisposition {
    Retryable,
    Terminal,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RuntimeFailure {
    Reported(FailureCode),
    PermissionDenied,
    ResourceMissing,
    Protocol,
    UnexpectedExit,
    CleanupFailed,
}

pub const fn classify_failure(failure: &RuntimeFailure) -> FailureDisposition {
    match failure {
        RuntimeFailure::UnexpectedExit
        | RuntimeFailure::Reported(FailureCode::RuntimeUnavailable) => {
            FailureDisposition::Retryable
        }
        RuntimeFailure::Reported(_)
        | RuntimeFailure::PermissionDenied
        | RuntimeFailure::ResourceMissing
        | RuntimeFailure::Protocol
        | RuntimeFailure::CleanupFailed => FailureDisposition::Terminal,
    }
}

pub trait SupervisorClock: Send + Sync {
    fn now(&self) -> Duration;
    fn sleep<'a>(&'a self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send + 'a>>;
}

#[derive(Debug)]
pub struct SystemClock {
    started: Instant,
}

impl SystemClock {
    pub fn new() -> Self {
        Self {
            started: Instant::now(),
        }
    }
}

impl Default for SystemClock {
    fn default() -> Self {
        Self::new()
    }
}

impl SupervisorClock for SystemClock {
    fn now(&self) -> Duration {
        self.started.elapsed()
    }

    fn sleep<'a>(&'a self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send + 'a>> {
        Box::pin(tokio::time::sleep(duration))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RuntimeSupervisorNotice {
    Runtime(RuntimeEvent),
    RestartScheduled { attempt: u8, delay: Duration },
    Terminal(RuntimeFailure),
    Stopped,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SupervisionOutcome {
    Terminal(RuntimeFailure),
    Stopped,
}

#[derive(Debug, Error)]
pub enum RuntimeSupervisorError {
    #[error(transparent)]
    State(#[from] TransitionError),
}

pub struct RuntimeSupervisor<L, C> {
    launcher: L,
    clock: C,
    data_dir: PathBuf,
    runtime_dir: PathBuf,
    application_support: PathBuf,
    policy: RestartPolicy,
    state: Supervisor,
    terminal_failure: Option<RuntimeFailure>,
    latest_notice: watch::Sender<Option<RuntimeSupervisorNotice>>,
}

impl<L> RuntimeSupervisor<L, SystemClock>
where
    L: RuntimeLauncher,
{
    pub fn production(
        launcher: L,
        data_dir: PathBuf,
        runtime_dir: PathBuf,
        application_support: PathBuf,
    ) -> Self {
        Self::new(
            launcher,
            SystemClock::new(),
            data_dir,
            runtime_dir,
            application_support,
            RestartPolicy::production(),
        )
    }
}

impl<L, C> RuntimeSupervisor<L, C>
where
    L: RuntimeLauncher,
    C: SupervisorClock,
{
    pub fn new(
        launcher: L,
        clock: C,
        data_dir: PathBuf,
        runtime_dir: PathBuf,
        application_support: PathBuf,
        policy: RestartPolicy,
    ) -> Self {
        let (latest_notice, _) = watch::channel(None);
        Self {
            launcher,
            clock,
            data_dir,
            runtime_dir,
            application_support,
            policy,
            state: Supervisor::new(),
            terminal_failure: None,
            latest_notice,
        }
    }

    pub const fn state(&self) -> &SupervisorState {
        self.state.state()
    }

    pub fn subscribe_notices(&self) -> watch::Receiver<Option<RuntimeSupervisorNotice>> {
        self.latest_notice.subscribe()
    }

    pub fn explicit_operator_retry(&mut self) -> Result<(), RuntimeSupervisorError> {
        self.state.reset_for_operator_retry()?;
        self.policy.explicit_retry();
        self.terminal_failure = None;
        Ok(())
    }

    pub async fn supervise_until_terminal(
        &mut self,
        notices: mpsc::Sender<RuntimeSupervisorNotice>,
    ) -> Result<SupervisionOutcome, RuntimeSupervisorError> {
        if let Some(failure) = self.terminal_failure {
            publish_notice(
                &self.latest_notice,
                &notices,
                RuntimeSupervisorNotice::Terminal(failure),
            );
            return Ok(SupervisionOutcome::Terminal(failure));
        }
        loop {
            let started = self.clock.now();
            let mut running =
                match launch_runtime(&self.launcher, &self.data_dir, &self.runtime_dir).await {
                    Ok(running) => running,
                    Err(error) => {
                        let failure = classify_process_error(&error);
                        if classify_failure(&failure) == FailureDisposition::Terminal {
                            return self.publish_terminal(&notices, failure).await;
                        }
                        match self.policy.record_crash(self.clock.now(), Duration::ZERO) {
                            RestartDecision::Retry { attempt, delay } => {
                                self.state.begin_restart(attempt)?;
                                publish_notice(
                                    &self.latest_notice,
                                    &notices,
                                    RuntimeSupervisorNotice::RestartScheduled { attempt, delay },
                                );
                                self.clock.sleep(delay).await;
                                continue;
                            }
                            RestartDecision::Terminal => {
                                return self.publish_terminal(&notices, failure).await;
                            }
                        }
                    }
                };

            let mut stdout = match running.take_stdout() {
                Ok(stdout) => stdout,
                Err(error) => {
                    let failure = if running.force_owned_cleanup().await.is_err() {
                        RuntimeFailure::CleanupFailed
                    } else {
                        classify_process_error(&error)
                    };
                    return self.publish_terminal(&notices, failure).await;
                }
            };
            let stderr = match running.take_stderr() {
                Ok(stderr) => stderr,
                Err(error) => {
                    let failure = if running.force_owned_cleanup().await.is_err() {
                        RuntimeFailure::CleanupFailed
                    } else {
                        classify_process_error(&error)
                    };
                    return self.publish_terminal(&notices, failure).await;
                }
            };
            let diagnostic_log =
                match DiagnosticLog::under_application_support(&self.application_support) {
                    Ok(log) => log,
                    Err(error) => {
                        let failure = if running.force_owned_cleanup().await.is_err() {
                            RuntimeFailure::CleanupFailed
                        } else if error.kind() == std::io::ErrorKind::PermissionDenied {
                            RuntimeFailure::PermissionDenied
                        } else {
                            RuntimeFailure::ResourceMissing
                        };
                        return self.publish_terminal(&notices, failure).await;
                    }
                };
            let mut stderr_drain =
                Some(Box::pin(drain_stderr(stderr, diagnostic_log)) as StderrDrain);

            let observed = loop {
                let mut event_future = Box::pin(read_runtime_event(&mut stdout));
                let mut wait_future = Box::pin(running.wait());
                let signal = poll_fn(|context| {
                    if let Some(task) = stderr_drain.as_mut() {
                        if let std::task::Poll::Ready(result) = task.as_mut().poll(context) {
                            return std::task::Poll::Ready(AttemptSignal::Stderr(result));
                        }
                    }
                    if let std::task::Poll::Ready(item) = event_future.as_mut().poll(context) {
                        return std::task::Poll::Ready(AttemptSignal::Stream(item));
                    }
                    if let std::task::Poll::Ready(result) = wait_future.as_mut().poll(context) {
                        return std::task::Poll::Ready(AttemptSignal::Exit(result));
                    }
                    std::task::Poll::Pending
                })
                .await;
                drop(event_future);
                drop(wait_future);

                match signal {
                    AttemptSignal::Stderr(Ok(())) => {
                        stderr_drain = None;
                    }
                    AttemptSignal::Stderr(Err(error)) => {
                        drop(stderr_drain.take());
                        let failure = if running.force_owned_cleanup().await.is_err() {
                            RuntimeFailure::CleanupFailed
                        } else {
                            classify_stderr_error(&error)
                        };
                        return self.publish_terminal(&notices, failure).await;
                    }
                    AttemptSignal::Stream(Ok(RuntimeStreamItem::Event(event))) => {
                        if self.state.apply(event.clone()).is_err() {
                            break ObservedFailure {
                                failure: RuntimeFailure::Protocol,
                                child_exited: false,
                                drop_stderr: true,
                            };
                        }
                        publish_notice(
                            &self.latest_notice,
                            &notices,
                            RuntimeSupervisorNotice::Runtime(event.clone()),
                        );
                        if let RuntimeEvent::Failed { code, .. } = event {
                            break ObservedFailure {
                                failure: RuntimeFailure::Reported(code),
                                child_exited: false,
                                drop_stderr: true,
                            };
                        }
                    }
                    AttemptSignal::Stream(Ok(RuntimeStreamItem::Eof)) => {
                        let result = running.wait().await;
                        if result.is_ok() && matches!(self.state.state(), SupervisorState::Stopped)
                        {
                            return self
                                .complete_clean_stop(&notices, &mut running, stderr_drain)
                                .await;
                        }
                        if running.force_owned_cleanup().await.is_err() {
                            drop(stderr_drain.take());
                            return self
                                .publish_terminal(&notices, RuntimeFailure::CleanupFailed)
                                .await;
                        }
                        break ObservedFailure {
                            failure: RuntimeFailure::UnexpectedExit,
                            child_exited: true,
                            drop_stderr: true,
                        };
                    }
                    AttemptSignal::Stream(Err(_)) => {
                        break ObservedFailure {
                            failure: RuntimeFailure::Protocol,
                            child_exited: false,
                            drop_stderr: true,
                        };
                    }
                    AttemptSignal::Exit(result) => {
                        if result.is_ok() && matches!(self.state.state(), SupervisorState::Stopped)
                        {
                            return self
                                .complete_clean_stop(&notices, &mut running, stderr_drain)
                                .await;
                        }
                        if running.force_owned_cleanup().await.is_err() {
                            drop(stderr_drain.take());
                            return self
                                .publish_terminal(&notices, RuntimeFailure::CleanupFailed)
                                .await;
                        }
                        break ObservedFailure {
                            failure: RuntimeFailure::UnexpectedExit,
                            child_exited: true,
                            drop_stderr: true,
                        };
                    }
                }
            };

            let mut observed = observed;
            if !observed.child_exited {
                if observed.failure == RuntimeFailure::Protocol {
                    if running.force_owned_cleanup().await.is_err() {
                        observed.failure = RuntimeFailure::CleanupFailed;
                    }
                } else if running
                    .shutdown()
                    .await
                    .is_err_and(|error| shutdown_cleanup_failed(&error))
                {
                    observed.failure = RuntimeFailure::CleanupFailed;
                }
            }
            if observed.failure == RuntimeFailure::CleanupFailed {
                drop(stderr_drain.take());
                return self
                    .publish_terminal(&notices, RuntimeFailure::CleanupFailed)
                    .await;
            }
            if observed.drop_stderr {
                stderr_drain = None;
            }
            if let Some(stderr_drain) = stderr_drain {
                match stderr_drain.await {
                    Ok(()) => {}
                    Err(error) => {
                        let failure = if running.force_owned_cleanup().await.is_err() {
                            RuntimeFailure::CleanupFailed
                        } else {
                            classify_stderr_error(&error)
                        };
                        return self.publish_terminal(&notices, failure).await;
                    }
                }
            }

            if classify_failure(&observed.failure) == FailureDisposition::Terminal {
                return self.publish_terminal(&notices, observed.failure).await;
            }

            let uptime = self.clock.now().saturating_sub(started);
            match self.policy.record_crash(self.clock.now(), uptime) {
                RestartDecision::Retry { attempt, delay } => {
                    self.state.begin_restart(attempt)?;
                    publish_notice(
                        &self.latest_notice,
                        &notices,
                        RuntimeSupervisorNotice::RestartScheduled { attempt, delay },
                    );
                    self.clock.sleep(delay).await;
                }
                RestartDecision::Terminal => {
                    return self.publish_terminal(&notices, observed.failure).await;
                }
            }
        }
    }

    async fn publish_terminal(
        &mut self,
        notices: &mpsc::Sender<RuntimeSupervisorNotice>,
        failure: RuntimeFailure,
    ) -> Result<SupervisionOutcome, RuntimeSupervisorError> {
        if !matches!(self.state.state(), SupervisorState::Failed { .. }) {
            let (code, detail) = failure_state(&failure);
            self.state.mark_terminal_failure(code, detail.to_owned());
        }
        self.terminal_failure = Some(failure);
        publish_notice(
            &self.latest_notice,
            notices,
            RuntimeSupervisorNotice::Terminal(failure),
        );
        Ok(SupervisionOutcome::Terminal(failure))
    }

    async fn complete_clean_stop(
        &mut self,
        notices: &mpsc::Sender<RuntimeSupervisorNotice>,
        running: &mut RunningRuntime,
        stderr_drain: Option<StderrDrain>,
    ) -> Result<SupervisionOutcome, RuntimeSupervisorError> {
        if running.force_owned_cleanup().await.is_err() {
            drop(stderr_drain);
            return self
                .publish_terminal(notices, RuntimeFailure::CleanupFailed)
                .await;
        }
        drop(stderr_drain);
        publish_notice(
            &self.latest_notice,
            notices,
            RuntimeSupervisorNotice::Stopped,
        );
        Ok(SupervisionOutcome::Stopped)
    }
}

struct ObservedFailure {
    failure: RuntimeFailure,
    child_exited: bool,
    drop_stderr: bool,
}

enum AttemptSignal {
    Stderr(std::io::Result<()>),
    Stream(Result<RuntimeStreamItem, ProcessError>),
    Exit(Result<(), ProcessError>),
}

fn publish_notice(
    latest: &watch::Sender<Option<RuntimeSupervisorNotice>>,
    notices: &mpsc::Sender<RuntimeSupervisorNotice>,
    notice: RuntimeSupervisorNotice,
) {
    latest.send_replace(Some(notice.clone()));
    match notices.try_send(notice) {
        Ok(()) | Err(mpsc::error::TrySendError::Full(_)) => {}
        Err(mpsc::error::TrySendError::Closed(_)) => {}
    }
}

fn classify_stderr_error(error: &std::io::Error) -> RuntimeFailure {
    if error.kind() == std::io::ErrorKind::PermissionDenied {
        RuntimeFailure::PermissionDenied
    } else {
        RuntimeFailure::ResourceMissing
    }
}

fn classify_process_error(error: &ProcessError) -> RuntimeFailure {
    match error {
        ProcessError::Launch(LaunchError::NotExecutable) => RuntimeFailure::PermissionDenied,
        ProcessError::Launch(LaunchError::Io(source))
            if source.kind() == std::io::ErrorKind::PermissionDenied =>
        {
            RuntimeFailure::PermissionDenied
        }
        ProcessError::StartCommandWrite {
            cleanup_failed: true,
            ..
        } => RuntimeFailure::CleanupFailed,
        ProcessError::StartCommandWrite {
            kind: std::io::ErrorKind::BrokenPipe,
            cleanup_failed: false,
        } => RuntimeFailure::UnexpectedExit,
        ProcessError::StartCommandWrite { kind, .. }
            if *kind == std::io::ErrorKind::PermissionDenied =>
        {
            RuntimeFailure::PermissionDenied
        }
        ProcessError::StartCommandWrite { .. } => RuntimeFailure::ResourceMissing,
        ProcessError::Launch(_)
        | ProcessError::DirectoryNotAbsolute(_)
        | ProcessError::TokenGeneration
        | ProcessError::MissingPipe(_) => RuntimeFailure::ResourceMissing,
        ProcessError::Io(source) if source.kind() == std::io::ErrorKind::PermissionDenied => {
            RuntimeFailure::PermissionDenied
        }
        ProcessError::Io(_)
        | ProcessError::ForcedCleanup(_)
        | ProcessError::ShutdownFailed { .. } => RuntimeFailure::ResourceMissing,
        ProcessError::EventLineTooLong | ProcessError::UnexpectedEof | ProcessError::Protocol => {
            RuntimeFailure::Protocol
        }
    }
}

fn failure_state(failure: &RuntimeFailure) -> (FailureCode, &'static str) {
    match failure {
        RuntimeFailure::Reported(code) => (*code, "runtime reported a terminal failure"),
        RuntimeFailure::PermissionDenied => (
            FailureCode::DatabaseUnavailable,
            "runtime permission denied",
        ),
        RuntimeFailure::ResourceMissing => {
            (FailureCode::ResourceMissing, "runtime resource unavailable")
        }
        RuntimeFailure::Protocol => (FailureCode::InvalidStartCommand, "runtime protocol failed"),
        RuntimeFailure::UnexpectedExit => (
            FailureCode::RuntimeUnavailable,
            "runtime exited unexpectedly",
        ),
        RuntimeFailure::CleanupFailed => {
            (FailureCode::ShutdownFailed, "owned runtime cleanup failed")
        }
    }
}

fn shutdown_cleanup_failed(error: &ProcessError) -> bool {
    !matches!(
        error,
        ProcessError::ShutdownFailed {
            cleanup_failed: false,
            ..
        }
    )
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::future::Future;
    use std::io;
    use std::io::Cursor;
    use std::path::PathBuf;
    use std::pin::Pin;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use tokio::io::{AsyncBufRead, BufReader};
    use tokio::sync::mpsc;

    use super::*;
    use crate::runtime::process::{LaunchError, LaunchRequest, RuntimeChild, RuntimeLauncher};
    use crate::runtime::protocol::FailureCode;

    type IoFuture<'a> = Pin<Box<dyn Future<Output = io::Result<()>> + Send + 'a>>;

    struct ChildScript {
        stdout: Vec<u8>,
        stderr: Vec<u8>,
        uptime: Duration,
        wait_error: bool,
        stderr_error: Option<io::ErrorKind>,
        stdout_pending: bool,
        wait_pending: bool,
        kill_error: bool,
        stderr_pending: bool,
        start_write_error: bool,
    }

    impl ChildScript {
        fn crash(stdout: &str, uptime: Duration) -> Self {
            Self {
                stdout: stdout.as_bytes().to_vec(),
                stderr: Vec::new(),
                uptime,
                wait_error: false,
                stderr_error: None,
                stdout_pending: false,
                wait_pending: false,
                kill_error: false,
                stderr_pending: false,
                start_write_error: false,
            }
        }

        fn wait_error() -> Self {
            Self {
                stdout: Vec::new(),
                stderr: Vec::new(),
                uptime: Duration::ZERO,
                wait_error: true,
                stderr_error: None,
                stdout_pending: false,
                wait_pending: false,
                kill_error: false,
                stderr_pending: false,
                start_write_error: false,
            }
        }

        fn stderr_error(kind: io::ErrorKind) -> Self {
            Self {
                stdout: b"{\"version\":1,\"state\":\"initializing\"}\n".to_vec(),
                stderr: Vec::new(),
                uptime: Duration::ZERO,
                wait_error: false,
                stderr_error: Some(kind),
                stdout_pending: false,
                wait_pending: false,
                kill_error: false,
                stderr_pending: false,
                start_write_error: false,
            }
        }

        fn active_with_stderr_error(kind: io::ErrorKind) -> Self {
            Self {
                stdout: Vec::new(),
                stderr: Vec::new(),
                uptime: Duration::ZERO,
                wait_error: false,
                stderr_error: Some(kind),
                stdout_pending: true,
                wait_pending: true,
                kill_error: false,
                stderr_pending: false,
                start_write_error: false,
            }
        }

        fn wait_and_kill_error() -> Self {
            Self {
                stdout: Vec::new(),
                stderr: Vec::new(),
                uptime: Duration::ZERO,
                wait_error: true,
                stderr_error: None,
                stdout_pending: false,
                wait_pending: false,
                kill_error: true,
                stderr_pending: false,
                start_write_error: false,
            }
        }

        fn wait_and_kill_error_with_pending_stderr() -> Self {
            Self {
                stderr_pending: true,
                ..Self::wait_and_kill_error()
            }
        }

        fn start_write_error() -> Self {
            Self {
                stdout: Vec::new(),
                stderr: Vec::new(),
                uptime: Duration::ZERO,
                wait_error: false,
                stderr_error: None,
                stdout_pending: false,
                wait_pending: false,
                kill_error: false,
                stderr_pending: false,
                start_write_error: true,
            }
        }
    }

    #[derive(Default)]
    struct LauncherState {
        scripts: VecDeque<ChildScript>,
        launches: usize,
        writes: Vec<Vec<u8>>,
        kills: usize,
        drop_signals: usize,
        stderr_drops: usize,
    }

    #[derive(Clone, Default)]
    struct FakeClock {
        inner: Arc<Mutex<FakeClockState>>,
    }

    #[derive(Default)]
    struct FakeClockState {
        now: Duration,
        sleeps: Vec<Duration>,
    }

    impl SupervisorClock for FakeClock {
        fn now(&self) -> Duration {
            self.inner.lock().unwrap().now
        }

        fn sleep<'a>(
            &'a self,
            duration: Duration,
        ) -> Pin<Box<dyn Future<Output = ()> + Send + 'a>> {
            Box::pin(async move {
                let mut state = self.inner.lock().unwrap();
                state.sleeps.push(duration);
                state.now += duration;
            })
        }
    }

    struct FakeLauncher {
        state: Arc<Mutex<LauncherState>>,
        clock: FakeClock,
    }

    struct FakeChild {
        state: Arc<Mutex<LauncherState>>,
        clock: FakeClock,
        stdout: Option<Vec<u8>>,
        stderr: Option<Vec<u8>>,
        uptime: Duration,
        wait_error: bool,
        stderr_error: Option<io::ErrorKind>,
        stdout_pending: bool,
        wait_pending: bool,
        kill_error: bool,
        stderr_pending: bool,
        start_write_error: bool,
    }

    impl RuntimeLauncher for FakeLauncher {
        fn launch(&self, _request: LaunchRequest) -> Result<Box<dyn RuntimeChild>, LaunchError> {
            let script = {
                let mut state = self.state.lock().unwrap();
                let script = state.scripts.pop_front().ok_or_else(|| {
                    LaunchError::Io(io::Error::new(io::ErrorKind::NotFound, "no child script"))
                })?;
                state.launches += 1;
                script
            };
            Ok(Box::new(FakeChild {
                state: Arc::clone(&self.state),
                clock: self.clock.clone(),
                stdout: Some(script.stdout),
                stderr: Some(script.stderr),
                uptime: script.uptime,
                wait_error: script.wait_error,
                stderr_error: script.stderr_error,
                stdout_pending: script.stdout_pending,
                wait_pending: script.wait_pending,
                kill_error: script.kill_error,
                stderr_pending: script.stderr_pending,
                start_write_error: script.start_write_error,
            }))
        }
    }

    impl RuntimeChild for FakeChild {
        fn write_stdin<'a>(&'a mut self, bytes: &'a [u8]) -> IoFuture<'a> {
            let state = Arc::clone(&self.state);
            let start_write_error = self.start_write_error;
            Box::pin(async move {
                state.lock().unwrap().writes.push(bytes.to_vec());
                if start_write_error {
                    Err(io::Error::new(
                        io::ErrorKind::BrokenPipe,
                        "token=start-secret",
                    ))
                } else {
                    Ok(())
                }
            })
        }

        fn take_stdout(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>> {
            if self.stdout_pending {
                if self.stdout.as_ref().is_some_and(|bytes| !bytes.is_empty()) {
                    return self.stdout.take().map(|bytes| {
                        Box::new(BufReader::new(PrefixPendingReader { bytes, offset: 0 }))
                            as Box<dyn AsyncBufRead + Send + Unpin>
                    });
                }
                return Some(Box::new(BufReader::new(PendingReader)));
            }
            self.stdout.take().map(|bytes| {
                Box::new(BufReader::new(Cursor::new(bytes))) as Box<dyn AsyncBufRead + Send + Unpin>
            })
        }

        fn take_stderr(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>> {
            if let Some(kind) = self.stderr_error.take() {
                return Some(Box::new(BufReader::new(FailingReader { kind })));
            }
            if self.stderr_pending {
                return Some(Box::new(BufReader::new(TrackedPendingReader(Arc::clone(
                    &self.state,
                )))));
            }
            self.stderr.take().map(|bytes| {
                Box::new(BufReader::new(Cursor::new(bytes))) as Box<dyn AsyncBufRead + Send + Unpin>
            })
        }

        fn wait_for_exit_preserving_stdin<'a>(&'a mut self) -> IoFuture<'a> {
            let clock = self.clock.clone();
            let uptime = self.uptime;
            let wait_error = self.wait_error;
            let wait_pending = self.wait_pending;
            Box::pin(async move {
                if wait_pending {
                    return std::future::pending().await;
                }
                clock.inner.lock().unwrap().now += uptime;
                if wait_error {
                    Err(io::Error::other("wait failed"))
                } else {
                    Ok(())
                }
            })
        }

        fn kill_owned<'a>(&'a mut self) -> IoFuture<'a> {
            let state = Arc::clone(&self.state);
            let kill_error = self.kill_error;
            Box::pin(async move {
                state.lock().unwrap().kills += 1;
                if kill_error {
                    Err(io::Error::other("token=cleanup-secret"))
                } else {
                    Ok(())
                }
            })
        }

        fn signal_owned_on_drop(&mut self) {
            self.state.lock().unwrap().drop_signals += 1;
        }
    }

    struct FailingReader {
        kind: io::ErrorKind,
    }

    struct PendingReader;

    struct PrefixPendingReader {
        bytes: Vec<u8>,
        offset: usize,
    }

    struct TrackedPendingReader(Arc<Mutex<LauncherState>>);

    impl Drop for TrackedPendingReader {
        fn drop(&mut self) {
            self.0.lock().unwrap().stderr_drops += 1;
        }
    }

    impl tokio::io::AsyncRead for PendingReader {
        fn poll_read(
            self: Pin<&mut Self>,
            _context: &mut std::task::Context<'_>,
            _buffer: &mut tokio::io::ReadBuf<'_>,
        ) -> std::task::Poll<io::Result<()>> {
            std::task::Poll::Pending
        }
    }

    impl tokio::io::AsyncRead for PrefixPendingReader {
        fn poll_read(
            mut self: Pin<&mut Self>,
            _context: &mut std::task::Context<'_>,
            buffer: &mut tokio::io::ReadBuf<'_>,
        ) -> std::task::Poll<io::Result<()>> {
            if self.offset == self.bytes.len() {
                return std::task::Poll::Pending;
            }
            let available = &self.bytes[self.offset..];
            let length = available.len().min(buffer.remaining());
            buffer.put_slice(&available[..length]);
            self.offset += length;
            std::task::Poll::Ready(Ok(()))
        }
    }

    impl tokio::io::AsyncRead for TrackedPendingReader {
        fn poll_read(
            self: Pin<&mut Self>,
            _context: &mut std::task::Context<'_>,
            _buffer: &mut tokio::io::ReadBuf<'_>,
        ) -> std::task::Poll<io::Result<()>> {
            std::task::Poll::Pending
        }
    }

    impl tokio::io::AsyncRead for FailingReader {
        fn poll_read(
            self: Pin<&mut Self>,
            _context: &mut std::task::Context<'_>,
            _buffer: &mut tokio::io::ReadBuf<'_>,
        ) -> std::task::Poll<io::Result<()>> {
            std::task::Poll::Ready(Err(io::Error::new(self.kind, "stderr failed")))
        }
    }

    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap()
    }

    fn test_paths(name: &str) -> (PathBuf, PathBuf, PathBuf) {
        let root = std::env::temp_dir().join(format!(
            "poly-supervisor-{name}-{}",
            crate::runtime::process::generate_launch_token().unwrap()
        ));
        (root.join("data"), root.join("runtime"), root)
    }

    fn fake_supervisor(
        scripts: Vec<ChildScript>,
        name: &str,
    ) -> (
        RuntimeSupervisor<FakeLauncher, FakeClock>,
        Arc<Mutex<LauncherState>>,
        FakeClock,
        PathBuf,
    ) {
        let state = Arc::new(Mutex::new(LauncherState {
            scripts: scripts.into(),
            ..LauncherState::default()
        }));
        let clock = FakeClock::default();
        let launcher = FakeLauncher {
            state: Arc::clone(&state),
            clock: clock.clone(),
        };
        let (data_dir, runtime_dir, support_dir) = test_paths(name);
        let supervisor = RuntimeSupervisor::new(
            launcher,
            clock.clone(),
            data_dir,
            runtime_dir,
            support_dir.clone(),
            RestartPolicy::deterministic(),
        );
        (supervisor, state, clock, support_dir)
    }

    #[test]
    fn retries_three_crashes_with_exponential_delays_then_stops() {
        let mut policy = RestartPolicy::deterministic();

        for (second, attempt, delay) in [(0, 1, 1), (1, 2, 2), (2, 3, 4)] {
            assert_eq!(
                policy.record_crash(Duration::from_secs(second), Duration::from_secs(10)),
                RestartDecision::Retry {
                    attempt,
                    delay: Duration::from_secs(delay)
                }
            );
        }
        assert_eq!(
            policy.record_crash(Duration::from_secs(3), Duration::from_secs(10)),
            RestartDecision::Terminal
        );
    }

    #[test]
    fn crash_window_and_explicit_retry_reset_attempts() {
        let mut policy = RestartPolicy::deterministic();
        assert!(matches!(
            policy.record_crash(Duration::ZERO, Duration::ZERO),
            RestartDecision::Retry { attempt: 1, .. }
        ));
        assert!(matches!(
            policy.record_crash(Duration::from_secs(61), Duration::ZERO),
            RestartDecision::Retry { attempt: 1, .. }
        ));

        policy.record_crash(Duration::from_secs(62), Duration::ZERO);
        policy.explicit_retry();
        assert!(matches!(
            policy.record_crash(Duration::from_secs(63), Duration::ZERO),
            RestartDecision::Retry { attempt: 1, .. }
        ));
    }

    #[test]
    fn stable_five_minute_run_resets_attempts() {
        let mut policy = RestartPolicy::deterministic();
        policy.record_crash(Duration::ZERO, Duration::from_secs(10));
        policy.record_crash(Duration::from_secs(1), Duration::from_secs(10));

        assert_eq!(
            policy.record_crash(Duration::from_secs(301), Duration::from_secs(300)),
            RestartDecision::Retry {
                attempt: 1,
                delay: Duration::from_secs(1)
            }
        );
    }

    #[test]
    fn terminal_crash_limit_stays_latched_until_explicit_retry() {
        let mut policy = RestartPolicy::deterministic();
        for second in 0..4 {
            policy.record_crash(Duration::from_secs(second), Duration::ZERO);
        }

        assert_eq!(
            policy.record_crash(Duration::from_secs(10_000), Duration::from_secs(300)),
            RestartDecision::Terminal
        );
        policy.explicit_retry();
        assert!(matches!(
            policy.record_crash(Duration::from_secs(10_001), Duration::ZERO),
            RestartDecision::Retry { attempt: 1, .. }
        ));
    }

    #[test]
    fn classifies_deterministic_failures_as_terminal_and_exits_as_retryable() {
        for failure in [
            RuntimeFailure::Reported(FailureCode::MigrationFailed),
            RuntimeFailure::PermissionDenied,
            RuntimeFailure::ResourceMissing,
            RuntimeFailure::Protocol,
            RuntimeFailure::CleanupFailed,
        ] {
            assert_eq!(classify_failure(&failure), FailureDisposition::Terminal);
        }
        assert_eq!(
            classify_failure(&RuntimeFailure::UnexpectedExit),
            FailureDisposition::Retryable
        );
        assert_eq!(
            classify_failure(&RuntimeFailure::Reported(FailureCode::RuntimeUnavailable)),
            FailureDisposition::Retryable
        );
    }

    #[test]
    fn runtime_supervisor_restarts_three_times_then_stops_without_a_fifth_child() {
        let initializing = "{\"version\":1,\"state\":\"initializing\"}\n";
        let scripts = (0..4)
            .map(|_| ChildScript::crash(initializing, Duration::ZERO))
            .collect();
        let (mut supervisor, state, clock, support_dir) = fake_supervisor(scripts, "crash-limit");
        let (sender, mut receiver) = mpsc::channel(32);

        let outcome = runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();

        assert_eq!(
            outcome,
            SupervisionOutcome::Terminal(RuntimeFailure::UnexpectedExit)
        );
        assert_eq!(state.lock().unwrap().launches, 4);
        assert_eq!(state.lock().unwrap().writes.len(), 4);
        assert_eq!(
            clock.inner.lock().unwrap().sleeps,
            vec![
                Duration::from_secs(1),
                Duration::from_secs(2),
                Duration::from_secs(4)
            ]
        );
        let mut restarts = Vec::new();
        while let Ok(notice) = receiver.try_recv() {
            if let RuntimeSupervisorNotice::RestartScheduled { attempt, delay } = notice {
                restarts.push((attempt, delay));
            }
        }
        assert_eq!(restarts.len(), 3);

        let (sender, _receiver) = mpsc::channel(4);
        let second = runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();
        assert_eq!(second, outcome);
        assert_eq!(state.lock().unwrap().launches, 4);
        if support_dir.exists() {
            std::fs::remove_dir_all(support_dir).unwrap();
        }
    }

    #[test]
    fn runtime_supervisor_retries_an_exit_before_the_first_event() {
        let scripts = (0..4)
            .map(|_| ChildScript::crash("", Duration::ZERO))
            .collect();
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(scripts, "pre-event-exit");
        let (sender, _receiver) = mpsc::channel(16);

        let outcome = runtime().block_on(supervisor.supervise_until_terminal(sender));

        assert_eq!(
            outcome.unwrap(),
            SupervisionOutcome::Terminal(RuntimeFailure::UnexpectedExit)
        );
        assert_eq!(state.lock().unwrap().launches, 4);
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn cancelling_active_supervision_signals_the_exact_owned_child() {
        let script = ChildScript {
            stdout_pending: true,
            wait_pending: true,
            stderr_pending: true,
            ..ChildScript::crash("", Duration::ZERO)
        };
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(vec![script], "cancel-owned-child");
        let (sender, _receiver) = mpsc::channel(1);

        let executor = runtime();
        let result = executor.block_on(async {
            tokio::time::timeout(
                Duration::from_millis(10),
                supervisor.supervise_until_terminal(sender),
            )
            .await
        });

        assert!(result.is_err());
        assert_eq!(state.lock().unwrap().drop_signals, 1);
        assert_eq!(state.lock().unwrap().stderr_drops, 1);
        assert_eq!(state.lock().unwrap().launches, 1);
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn runtime_supervisor_publishes_typed_events_and_does_not_retry_terminal_failure() {
        let stdout = concat!(
            "{\"version\":1,\"state\":\"initializing\"}\n",
            "{\"version\":1,\"state\":\"failed\",\"code\":\"migration_failed\",\"detail\":\"migration failed\"}\n"
        );
        let scripts = vec![ChildScript::crash(stdout, Duration::ZERO)];
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(scripts, "terminal-event");
        let (sender, mut receiver) = mpsc::channel(16);

        let outcome = runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();

        assert_eq!(
            outcome,
            SupervisionOutcome::Terminal(RuntimeFailure::Reported(FailureCode::MigrationFailed))
        );
        assert_eq!(state.lock().unwrap().launches, 1);
        assert!(matches!(
            receiver.try_recv().unwrap(),
            RuntimeSupervisorNotice::Runtime(RuntimeEvent::Initializing)
        ));
        assert!(matches!(
            receiver.try_recv().unwrap(),
            RuntimeSupervisorNotice::Runtime(RuntimeEvent::Failed {
                code: FailureCode::MigrationFailed,
                ..
            })
        ));
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn invalid_transition_is_not_published_and_becomes_controlled_protocol_terminal() {
        let stdout = "{\"version\":1,\"state\":\"ready\",\"port\":49152,\"bootstrap_path\":\"/desktop/bootstrap/safe\"}\n";
        let scripts = vec![ChildScript::crash(stdout, Duration::ZERO)];
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(scripts, "invalid-transition");
        let (sender, mut receiver) = mpsc::channel(8);

        let outcome = runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();

        assert_eq!(
            outcome,
            SupervisionOutcome::Terminal(RuntimeFailure::Protocol)
        );
        assert_eq!(state.lock().unwrap().launches, 1);
        assert_eq!(state.lock().unwrap().kills, 1);
        assert!(!matches!(
            receiver.try_recv().unwrap(),
            RuntimeSupervisorNotice::Runtime(RuntimeEvent::Ready { .. })
        ));
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn failed_event_before_initializing_is_controlled_protocol_terminal() {
        let stdout = "{\"version\":1,\"state\":\"failed\",\"code\":\"migration_failed\",\"detail\":\"failed\"}\n";
        let scripts = vec![ChildScript::crash(stdout, Duration::ZERO)];
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(scripts, "invalid-failed-event");
        let (sender, mut receiver) = mpsc::channel(8);

        let outcome = runtime().block_on(supervisor.supervise_until_terminal(sender));

        assert_eq!(
            outcome.unwrap(),
            SupervisionOutcome::Terminal(RuntimeFailure::Protocol)
        );
        assert_eq!(state.lock().unwrap().kills, 1);
        assert!(!matches!(
            receiver.try_recv().unwrap(),
            RuntimeSupervisorNotice::Runtime(RuntimeEvent::Failed { .. })
        ));
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn invalid_transition_after_shutdown_state_is_controlled_protocol_terminal() {
        let stdout = concat!(
            "{\"version\":1,\"state\":\"initializing\"}\n",
            "{\"version\":1,\"state\":\"shutting_down\"}\n",
            "{\"version\":1,\"state\":\"ready\",\"port\":49152,\"bootstrap_path\":\"/desktop/bootstrap/safe\"}\n"
        );
        let scripts = vec![ChildScript::crash(stdout, Duration::ZERO)];
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(scripts, "invalid-after-shutdown");
        let (sender, _receiver) = mpsc::channel(8);

        let outcome = runtime().block_on(supervisor.supervise_until_terminal(sender));

        assert_eq!(
            outcome.unwrap(),
            SupervisionOutcome::Terminal(RuntimeFailure::Protocol)
        );
        assert_eq!(state.lock().unwrap().kills, 1);
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn wait_error_forces_owned_cleanup_before_retrying() {
        let scripts = vec![
            ChildScript::wait_error(),
            ChildScript::crash(
                concat!(
                    "{\"version\":1,\"state\":\"initializing\"}\n",
                    "{\"version\":1,\"state\":\"failed\",\"code\":\"migration_failed\",\"detail\":\"failed\"}\n"
                ),
                Duration::ZERO,
            ),
        ];
        let (mut supervisor, state, _clock, support_dir) = fake_supervisor(scripts, "wait-error");
        let (sender, _receiver) = mpsc::channel(8);

        runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();

        assert!(state.lock().unwrap().kills >= 1);
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn stderr_inner_error_is_terminal_and_forces_owned_cleanup() {
        let scripts = vec![ChildScript::stderr_error(io::ErrorKind::PermissionDenied)];
        let (mut supervisor, state, clock, support_dir) = fake_supervisor(scripts, "stderr-error");
        let (sender, _receiver) = mpsc::channel(8);

        let outcome = runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();

        assert_eq!(
            outcome,
            SupervisionOutcome::Terminal(RuntimeFailure::PermissionDenied)
        );
        assert_eq!(state.lock().unwrap().kills, 1);
        assert!(clock.inner.lock().unwrap().sleeps.is_empty());
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn stderr_inner_error_overrides_an_otherwise_clean_stop() {
        let scripts = vec![ChildScript {
            stdout: concat!(
                "{\"version\":1,\"state\":\"initializing\"}\n",
                "{\"version\":1,\"state\":\"shutting_down\"}\n",
                "{\"version\":1,\"state\":\"stopped\"}\n"
            )
            .as_bytes()
            .to_vec(),
            stderr: Vec::new(),
            uptime: Duration::ZERO,
            wait_error: false,
            stderr_error: Some(io::ErrorKind::PermissionDenied),
            stdout_pending: false,
            wait_pending: false,
            kill_error: false,
            stderr_pending: false,
            start_write_error: false,
        }];
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(scripts, "stderr-clean-stop");
        let (sender, _receiver) = mpsc::channel(8);

        let outcome = runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();

        assert_eq!(
            outcome,
            SupervisionOutcome::Terminal(RuntimeFailure::PermissionDenied)
        );
        assert_eq!(state.lock().unwrap().kills, 1);
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn active_stderr_error_wins_while_stdout_and_exit_remain_pending() {
        let scripts = vec![ChildScript::active_with_stderr_error(
            io::ErrorKind::PermissionDenied,
        )];
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(scripts, "active-stderr-error");
        let (sender, _receiver) = mpsc::channel(8);

        let timed = runtime().block_on(async {
            tokio::time::timeout(
                Duration::from_millis(50),
                supervisor.supervise_until_terminal(sender),
            )
            .await
        });

        assert_eq!(
            timed.unwrap().unwrap(),
            SupervisionOutcome::Terminal(RuntimeFailure::PermissionDenied)
        );
        assert_eq!(state.lock().unwrap().kills, 1);
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn cleanup_failure_is_latched_terminal_and_never_launches_another_child() {
        let scripts = vec![
            ChildScript::wait_and_kill_error(),
            ChildScript::crash(
                "{\"version\":1,\"state\":\"initializing\"}\n",
                Duration::ZERO,
            ),
        ];
        let (mut supervisor, state, clock, support_dir) =
            fake_supervisor(scripts, "cleanup-failure");
        let (sender, _receiver) = mpsc::channel(8);

        let outcome = runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();

        assert_eq!(
            outcome,
            SupervisionOutcome::Terminal(RuntimeFailure::CleanupFailed)
        );
        let (sender, _receiver) = mpsc::channel(4);
        assert_eq!(
            runtime()
                .block_on(supervisor.supervise_until_terminal(sender))
                .unwrap(),
            SupervisionOutcome::Terminal(RuntimeFailure::CleanupFailed)
        );
        let state = state.lock().unwrap();
        assert_eq!(state.launches, 1);
        assert_eq!(state.kills, 1);
        assert!(clock.inner.lock().unwrap().sleeps.is_empty());
        match supervisor.state() {
            SupervisorState::Failed { code, detail } => {
                assert_eq!(*code, FailureCode::ShutdownFailed);
                assert_eq!(detail, "owned runtime cleanup failed");
                assert!(!detail.contains("cleanup-secret"));
            }
            other => panic!("unexpected state: {other:?}"),
        }
        drop(state);
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn cleanup_failure_does_not_wait_for_a_pending_stderr_pipe() {
        let scripts = vec![
            ChildScript::wait_and_kill_error_with_pending_stderr(),
            ChildScript::crash(
                "{\"version\":1,\"state\":\"initializing\"}\n",
                Duration::ZERO,
            ),
        ];
        let (mut supervisor, state, clock, support_dir) =
            fake_supervisor(scripts, "cleanup-failure-pending-stderr");
        let (sender, mut receiver) = mpsc::channel(8);

        let timed = runtime().block_on(async {
            tokio::time::timeout(
                Duration::from_millis(50),
                supervisor.supervise_until_terminal(sender),
            )
            .await
        });

        assert_eq!(
            timed.unwrap().unwrap(),
            SupervisionOutcome::Terminal(RuntimeFailure::CleanupFailed)
        );
        assert_eq!(state.lock().unwrap().launches, 1);
        assert!(clock.inner.lock().unwrap().sleeps.is_empty());
        assert!(matches!(
            receiver.try_recv().unwrap(),
            RuntimeSupervisorNotice::Terminal(RuntimeFailure::CleanupFailed)
        ));
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn leader_exit_cleans_descendants_before_pending_stderr_and_restart() {
        let scripts = vec![
            ChildScript {
                stderr_pending: true,
                ..ChildScript::crash("", Duration::ZERO)
            },
            ChildScript::crash(
                concat!(
                    "{\"version\":1,\"state\":\"initializing\"}\n",
                    "{\"version\":1,\"state\":\"failed\",\"code\":\"migration_failed\",\"detail\":\"failed\"}\n"
                ),
                Duration::ZERO,
            ),
        ];
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(scripts, "leader-exit-pending-stderr");
        let (sender, _receiver) = mpsc::channel(16);

        let timed = runtime().block_on(async {
            tokio::time::timeout(
                Duration::from_millis(50),
                supervisor.supervise_until_terminal(sender),
            )
            .await
        });

        assert!(timed.is_ok());
        assert_eq!(state.lock().unwrap().launches, 2);
        assert!(state.lock().unwrap().kills >= 1);
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn reported_failure_cleans_descendants_before_pending_stderr() {
        let scripts = vec![ChildScript {
            stdout: concat!(
                "{\"version\":1,\"state\":\"initializing\"}\n",
                "{\"version\":1,\"state\":\"failed\",\"code\":\"migration_failed\",\"detail\":\"failed\"}\n"
            )
            .as_bytes()
            .to_vec(),
            stderr_pending: true,
            ..ChildScript::crash("", Duration::ZERO)
        }];
        let (mut supervisor, state, clock, support_dir) =
            fake_supervisor(scripts, "reported-failure-pending-stderr");
        let (sender, _receiver) = mpsc::channel(8);

        let timed = runtime().block_on(async {
            tokio::time::timeout(
                Duration::from_millis(50),
                supervisor.supervise_until_terminal(sender),
            )
            .await
        });

        assert_eq!(
            timed.unwrap().unwrap(),
            SupervisionOutcome::Terminal(RuntimeFailure::Reported(FailureCode::MigrationFailed))
        );
        assert_eq!(state.lock().unwrap().launches, 1);
        assert_eq!(state.lock().unwrap().kills, 1);
        assert!(clock.inner.lock().unwrap().sleeps.is_empty());
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn verified_stopped_does_not_wait_for_descendant_held_stderr() {
        let scripts = vec![ChildScript {
            stdout: concat!(
                "{\"version\":1,\"state\":\"initializing\"}\n",
                "{\"version\":1,\"state\":\"shutting_down\"}\n",
                "{\"version\":1,\"state\":\"stopped\"}\n"
            )
            .as_bytes()
            .to_vec(),
            stderr_pending: true,
            ..ChildScript::crash("", Duration::ZERO)
        }];
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(scripts, "stopped-pending-stderr");
        let latest = supervisor.subscribe_notices();
        let (sender, _receiver) = mpsc::channel(8);

        let timed = runtime().block_on(async {
            tokio::time::timeout(
                Duration::from_millis(50),
                supervisor.supervise_until_terminal(sender),
            )
            .await
        });

        assert_eq!(timed.unwrap().unwrap(), SupervisionOutcome::Stopped);
        assert_eq!(state.lock().unwrap().kills, 1);
        assert!(matches!(
            &*latest.borrow(),
            Some(RuntimeSupervisorNotice::Stopped)
        ));
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn full_notice_channel_never_blocks_process_ownership_loop() {
        let initializing = "{\"version\":1,\"state\":\"initializing\"}\n";
        let scripts = (0..4)
            .map(|_| ChildScript::crash(initializing, Duration::ZERO))
            .collect();
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(scripts, "notice-backpressure");
        let latest = supervisor.subscribe_notices();
        let (sender, _ignored_receiver) = mpsc::channel(1);

        let timed = runtime().block_on(async {
            tokio::time::timeout(
                Duration::from_millis(50),
                supervisor.supervise_until_terminal(sender),
            )
            .await
        });

        assert_eq!(
            timed.unwrap().unwrap(),
            SupervisionOutcome::Terminal(RuntimeFailure::UnexpectedExit)
        );
        assert_eq!(state.lock().unwrap().launches, 4);
        assert!(matches!(
            &*latest.borrow(),
            Some(RuntimeSupervisorNotice::Terminal(
                RuntimeFailure::UnexpectedExit
            ))
        ));
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn slow_consumer_can_observe_latest_ready_while_runtime_stays_active() {
        let script = ChildScript {
            stdout: concat!(
                "{\"version\":1,\"state\":\"initializing\"}\n",
                "{\"version\":1,\"state\":\"preparing_database\"}\n",
                "{\"version\":1,\"state\":\"migrating\"}\n",
                "{\"version\":1,\"state\":\"starting_services\"}\n",
                "{\"version\":1,\"state\":\"ready\",\"port\":49152,\"bootstrap_path\":\"/desktop/bootstrap/safe\"}\n"
            )
            .as_bytes()
            .to_vec(),
            stdout_pending: true,
            wait_pending: true,
            stderr_pending: true,
            ..ChildScript::crash("", Duration::ZERO)
        };
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(vec![script], "latest-ready");
        let mut latest = supervisor.subscribe_notices();
        let (sender, _ignored_receiver) = mpsc::channel(1);
        let executor = runtime();

        let observed = executor.block_on(async {
            let supervision = supervisor.supervise_until_terminal(sender);
            tokio::pin!(supervision);
            tokio::time::timeout(Duration::from_millis(50), async {
                enum Signal {
                    Supervision(Result<SupervisionOutcome, RuntimeSupervisorError>),
                    Changed(Result<(), watch::error::RecvError>),
                }
                loop {
                    let mut changed = Box::pin(latest.changed());
                    let signal = poll_fn(|context| {
                        if let std::task::Poll::Ready(result) = supervision.as_mut().poll(context) {
                            return std::task::Poll::Ready(Signal::Supervision(result));
                        }
                        if let std::task::Poll::Ready(result) = changed.as_mut().poll(context) {
                            return std::task::Poll::Ready(Signal::Changed(result));
                        }
                        std::task::Poll::Pending
                    })
                    .await;
                    drop(changed);
                    match signal {
                        Signal::Supervision(result) => {
                            panic!("runtime stopped before Ready: {result:?}")
                        }
                        Signal::Changed(result) => assert!(result.is_ok()),
                    }
                    if matches!(
                        &*latest.borrow(),
                        Some(RuntimeSupervisorNotice::Runtime(RuntimeEvent::Ready { .. }))
                    ) {
                        return latest.borrow().clone();
                    }
                }
            })
            .await
        });

        assert!(matches!(
            observed.unwrap(),
            Some(RuntimeSupervisorNotice::Runtime(RuntimeEvent::Ready { .. }))
        ));
        assert_eq!(state.lock().unwrap().launches, 1);
        assert_eq!(state.lock().unwrap().drop_signals, 1);
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn broken_pipe_start_write_uses_retry_policy_and_latches_on_fourth() {
        let scripts = (0..4).map(|_| ChildScript::start_write_error()).collect();
        let (mut supervisor, state, clock, support_dir) =
            fake_supervisor(scripts, "start-broken-pipe");
        let (sender, _receiver) = mpsc::channel(16);

        let outcome = runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();

        assert_eq!(
            outcome,
            SupervisionOutcome::Terminal(RuntimeFailure::UnexpectedExit)
        );
        assert_eq!(state.lock().unwrap().launches, 4);
        assert_eq!(state.lock().unwrap().kills, 4);
        assert_eq!(
            clock.inner.lock().unwrap().sleeps,
            vec![
                Duration::from_secs(1),
                Duration::from_secs(2),
                Duration::from_secs(4)
            ]
        );
        if support_dir.exists() {
            std::fs::remove_dir_all(support_dir).unwrap();
        }
    }

    #[test]
    fn nested_launch_permission_error_is_terminal_permission_failure() {
        let error = ProcessError::Launch(LaunchError::Io(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "path denied",
        )));

        assert_eq!(
            classify_process_error(&error),
            RuntimeFailure::PermissionDenied
        );
    }

    #[test]
    fn explicit_operator_retry_unlatches_and_allows_exactly_one_new_launch() {
        let initializing = "{\"version\":1,\"state\":\"initializing\"}\n";
        let migration_failure = concat!(
            "{\"version\":1,\"state\":\"initializing\"}\n",
            "{\"version\":1,\"state\":\"failed\",\"code\":\"migration_failed\",\"detail\":\"migration failed\"}\n"
        );
        let mut scripts: Vec<_> = (0..4)
            .map(|_| ChildScript::crash(initializing, Duration::ZERO))
            .collect();
        scripts.push(ChildScript::crash(migration_failure, Duration::ZERO));
        let (mut supervisor, state, _clock, support_dir) =
            fake_supervisor(scripts, "operator-retry");
        let (sender, _receiver) = mpsc::channel(32);
        runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();
        assert_eq!(state.lock().unwrap().launches, 4);

        supervisor.explicit_operator_retry().unwrap();
        let (sender, _receiver) = mpsc::channel(8);
        runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();

        assert_eq!(state.lock().unwrap().launches, 5);
        std::fs::remove_dir_all(support_dir).unwrap();
    }

    #[test]
    fn runtime_supervisor_passes_stable_uptime_to_authoritative_policy() {
        let initializing = "{\"version\":1,\"state\":\"initializing\"}\n";
        let migration_failure = concat!(
            "{\"version\":1,\"state\":\"initializing\"}\n",
            "{\"version\":1,\"state\":\"failed\",\"code\":\"migration_failed\",\"detail\":\"migration failed\"}\n"
        );
        let scripts = vec![
            ChildScript::crash(initializing, Duration::ZERO),
            ChildScript::crash(initializing, Duration::from_secs(300)),
            ChildScript::crash(migration_failure, Duration::ZERO),
        ];
        let (mut supervisor, _state, clock, support_dir) = fake_supervisor(scripts, "stable-reset");
        let (sender, _receiver) = mpsc::channel(16);

        runtime()
            .block_on(supervisor.supervise_until_terminal(sender))
            .unwrap();

        assert_eq!(
            clock.inner.lock().unwrap().sleeps,
            vec![Duration::from_secs(1), Duration::from_secs(1)]
        );
        std::fs::remove_dir_all(support_dir).unwrap();
    }
}
