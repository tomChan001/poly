use std::collections::VecDeque;
use std::time::Duration;

use rand::Rng as _;

use super::protocol::FailureCode;

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

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;
    use crate::runtime::protocol::FailureCode;

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
}
