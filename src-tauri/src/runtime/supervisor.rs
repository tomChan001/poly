use std::collections::VecDeque;
use std::future::{poll_fn, Future};
use std::path::PathBuf;
use std::pin::Pin;
use std::time::{Duration, Instant};

use rand::Rng as _;
use thiserror::Error;
use tokio::sync::mpsc;

use super::process::{
    drain_stderr, launch_runtime, read_runtime_event, DiagnosticLog, LaunchError, ProcessError,
    RuntimeLauncher, RuntimeStreamItem,
};
use super::protocol::{FailureCode, RuntimeEvent};
use super::state::{Supervisor, SupervisorState, TransitionError};

const MAX_RESTARTS: u8 = 3;
const CRASH_WINDOW: Duration = Duration::from_secs(60);
const STABLE_RUN: Duration = Duration::from_secs(5 * 60);
const MAX_DELAY: Duration = Duration::from_secs(8);
const MAX_JITTER: Duration = Duration::from_millis(250);

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
        | RuntimeFailure::Protocol => FailureDisposition::Terminal,
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
        Self {
            launcher,
            clock,
            data_dir,
            runtime_dir,
            application_support,
            policy,
            state: Supervisor::new(),
        }
    }

    pub const fn state(&self) -> &SupervisorState {
        self.state.state()
    }

    pub fn explicit_operator_retry(&mut self) -> Result<(), RuntimeSupervisorError> {
        self.state.reset_for_operator_retry()?;
        self.policy.explicit_retry();
        Ok(())
    }

    pub async fn supervise_until_terminal(
        &mut self,
        notices: mpsc::Sender<RuntimeSupervisorNotice>,
    ) -> Result<SupervisionOutcome, RuntimeSupervisorError> {
        loop {
            let started = self.clock.now();
            let mut running =
                match launch_runtime(&self.launcher, &self.data_dir, &self.runtime_dir).await {
                    Ok(running) => running,
                    Err(error) => {
                        let failure = classify_process_error(&error);
                        return self.publish_terminal(&notices, failure).await;
                    }
                };

            let mut stdout = match running.take_stdout() {
                Ok(stdout) => stdout,
                Err(error) => {
                    let _ = running.shutdown().await;
                    let failure = classify_process_error(&error);
                    return self.publish_terminal(&notices, failure).await;
                }
            };
            let stderr = match running.take_stderr() {
                Ok(stderr) => stderr,
                Err(error) => {
                    let _ = running.shutdown().await;
                    let failure = classify_process_error(&error);
                    return self.publish_terminal(&notices, failure).await;
                }
            };
            let diagnostic_log =
                match DiagnosticLog::under_application_support(&self.application_support) {
                    Ok(log) => log,
                    Err(error) => {
                        let _ = running.shutdown().await;
                        let failure = if error.kind() == std::io::ErrorKind::PermissionDenied {
                            RuntimeFailure::PermissionDenied
                        } else {
                            RuntimeFailure::ResourceMissing
                        };
                        return self.publish_terminal(&notices, failure).await;
                    }
                };
            let stderr_task = tokio::spawn(drain_stderr(stderr, diagnostic_log));

            let observed = loop {
                let mut event_future = Box::pin(read_runtime_event(&mut stdout));
                let mut wait_future = Box::pin(running.wait());
                let signal = poll_fn(|context| {
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
                    AttemptSignal::Stream(Ok(RuntimeStreamItem::Event(event))) => {
                        let _ = notices
                            .send(RuntimeSupervisorNotice::Runtime(event.clone()))
                            .await;
                        if let RuntimeEvent::Failed { code, .. } = event {
                            self.state.apply(event)?;
                            break ObservedFailure {
                                failure: RuntimeFailure::Reported(code),
                                child_exited: false,
                            };
                        }
                        if self.state.apply(event).is_err() {
                            break ObservedFailure {
                                failure: RuntimeFailure::Protocol,
                                child_exited: false,
                            };
                        }
                    }
                    AttemptSignal::Stream(Ok(RuntimeStreamItem::Eof)) => {
                        let result = running.wait().await;
                        if result.is_ok() && matches!(self.state.state(), SupervisorState::Stopped)
                        {
                            let _ = stderr_task.await;
                            let _ = notices.send(RuntimeSupervisorNotice::Stopped).await;
                            return Ok(SupervisionOutcome::Stopped);
                        }
                        break ObservedFailure {
                            failure: RuntimeFailure::UnexpectedExit,
                            child_exited: true,
                        };
                    }
                    AttemptSignal::Stream(Err(_)) => {
                        break ObservedFailure {
                            failure: RuntimeFailure::Protocol,
                            child_exited: false,
                        };
                    }
                    AttemptSignal::Exit(result) => {
                        if result.is_ok() && matches!(self.state.state(), SupervisorState::Stopped)
                        {
                            let _ = stderr_task.await;
                            let _ = notices.send(RuntimeSupervisorNotice::Stopped).await;
                            return Ok(SupervisionOutcome::Stopped);
                        }
                        break ObservedFailure {
                            failure: RuntimeFailure::UnexpectedExit,
                            child_exited: true,
                        };
                    }
                }
            };

            if !observed.child_exited {
                let _ = running.shutdown().await;
            }
            if stderr_task.await.is_err() {
                return self
                    .publish_terminal(&notices, RuntimeFailure::ResourceMissing)
                    .await;
            }

            if classify_failure(&observed.failure) == FailureDisposition::Terminal {
                return self.publish_terminal(&notices, observed.failure).await;
            }

            let uptime = self.clock.now().saturating_sub(started);
            match self.policy.record_crash(self.clock.now(), uptime) {
                RestartDecision::Retry { attempt, delay } => {
                    self.state.begin_restart(attempt)?;
                    let _ = notices
                        .send(RuntimeSupervisorNotice::RestartScheduled { attempt, delay })
                        .await;
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
            self.state.mark_terminal_failure(code, detail.to_owned())?;
        }
        let _ = notices
            .send(RuntimeSupervisorNotice::Terminal(failure))
            .await;
        Ok(SupervisionOutcome::Terminal(failure))
    }
}

struct ObservedFailure {
    failure: RuntimeFailure,
    child_exited: bool,
}

enum AttemptSignal {
    Stream(Result<RuntimeStreamItem, ProcessError>),
    Exit(Result<(), ProcessError>),
}

fn classify_process_error(error: &ProcessError) -> RuntimeFailure {
    match error {
        ProcessError::Launch(LaunchError::NotExecutable) => RuntimeFailure::PermissionDenied,
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
    }
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
    }

    impl ChildScript {
        fn crash(stdout: &str, uptime: Duration) -> Self {
            Self {
                stdout: stdout.as_bytes().to_vec(),
                stderr: Vec::new(),
                uptime,
            }
        }
    }

    #[derive(Default)]
    struct LauncherState {
        scripts: VecDeque<ChildScript>,
        launches: usize,
        writes: Vec<Vec<u8>>,
        kills: usize,
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
            }))
        }
    }

    impl RuntimeChild for FakeChild {
        fn write_stdin<'a>(&'a mut self, bytes: &'a [u8]) -> IoFuture<'a> {
            let state = Arc::clone(&self.state);
            Box::pin(async move {
                state.lock().unwrap().writes.push(bytes.to_vec());
                Ok(())
            })
        }

        fn take_stdout(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>> {
            self.stdout.take().map(|bytes| {
                Box::new(BufReader::new(Cursor::new(bytes))) as Box<dyn AsyncBufRead + Send + Unpin>
            })
        }

        fn take_stderr(&mut self) -> Option<Box<dyn AsyncBufRead + Send + Unpin>> {
            self.stderr.take().map(|bytes| {
                Box::new(BufReader::new(Cursor::new(bytes))) as Box<dyn AsyncBufRead + Send + Unpin>
            })
        }

        fn wait<'a>(&'a mut self) -> IoFuture<'a> {
            let clock = self.clock.clone();
            let uptime = self.uptime;
            Box::pin(async move {
                clock.inner.lock().unwrap().now += uptime;
                Ok(())
            })
        }

        fn kill_owned<'a>(&'a mut self) -> IoFuture<'a> {
            let state = Arc::clone(&self.state);
            Box::pin(async move {
                state.lock().unwrap().kills += 1;
                Ok(())
            })
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
        std::fs::remove_dir_all(support_dir).unwrap();
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
