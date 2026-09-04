use std::num::NonZeroU16;

use thiserror::Error;

use super::protocol::{FailureCode, RuntimeEvent, RuntimeState};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SupervisorState {
    Idle,
    Initializing,
    PreparingDatabase,
    Migrating,
    StartingServices,
    Ready {
        port: NonZeroU16,
        bootstrap_path: String,
    },
    Restarting {
        attempt: u8,
    },
    ShuttingDown,
    Stopped,
    Failed {
        code: FailureCode,
        detail: String,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SupervisorAction {
    None,
    Retry { attempt: u8 },
    Terminal,
}

#[derive(Debug)]
pub struct Supervisor {
    state: SupervisorState,
    retry_attempts: u8,
    max_retries: u8,
}

impl Supervisor {
    pub const fn new(max_retries: u8) -> Self {
        Self {
            state: SupervisorState::Idle,
            retry_attempts: 0,
            max_retries,
        }
    }

    pub const fn state(&self) -> &SupervisorState {
        &self.state
    }

    pub fn apply(&mut self, event: RuntimeEvent) -> Result<SupervisorAction, TransitionError> {
        if let RuntimeEvent::Failed { code, detail } = event {
            return self.apply_failure(code, detail);
        }

        if !self.accepts(event.state()) {
            return Err(TransitionError::Invalid {
                from: self.state.clone(),
                event: event.state(),
            });
        }

        self.state = match event {
            RuntimeEvent::Initializing => SupervisorState::Initializing,
            RuntimeEvent::PreparingDatabase => SupervisorState::PreparingDatabase,
            RuntimeEvent::Migrating { .. } => SupervisorState::Migrating,
            RuntimeEvent::StartingServices => SupervisorState::StartingServices,
            RuntimeEvent::Ready {
                port,
                bootstrap_path,
            } => SupervisorState::Ready {
                port,
                bootstrap_path,
            },
            RuntimeEvent::ShuttingDown => SupervisorState::ShuttingDown,
            RuntimeEvent::Stopped { .. } => SupervisorState::Stopped,
            RuntimeEvent::Failed { .. } => unreachable!("failure handled above"),
        };
        Ok(SupervisorAction::None)
    }

    fn accepts(&self, event: RuntimeState) -> bool {
        matches!(
            (&self.state, event),
            (SupervisorState::Idle, RuntimeState::Initializing)
                | (
                    SupervisorState::Restarting { .. },
                    RuntimeState::Initializing
                )
                | (
                    SupervisorState::Initializing,
                    RuntimeState::PreparingDatabase
                )
                | (SupervisorState::PreparingDatabase, RuntimeState::Migrating)
                | (SupervisorState::Migrating, RuntimeState::StartingServices)
                | (SupervisorState::StartingServices, RuntimeState::Ready)
                | (SupervisorState::Initializing, RuntimeState::ShuttingDown)
                | (
                    SupervisorState::PreparingDatabase,
                    RuntimeState::ShuttingDown
                )
                | (SupervisorState::Migrating, RuntimeState::ShuttingDown)
                | (
                    SupervisorState::StartingServices,
                    RuntimeState::ShuttingDown
                )
                | (SupervisorState::Ready { .. }, RuntimeState::ShuttingDown)
                | (
                    SupervisorState::Restarting { .. },
                    RuntimeState::ShuttingDown
                )
                | (SupervisorState::ShuttingDown, RuntimeState::Stopped)
        )
    }

    fn apply_failure(
        &mut self,
        code: FailureCode,
        detail: String,
    ) -> Result<SupervisorAction, TransitionError> {
        if matches!(
            self.state,
            SupervisorState::Idle | SupervisorState::Stopped | SupervisorState::Failed { .. }
        ) {
            return Err(TransitionError::Invalid {
                from: self.state.clone(),
                event: RuntimeState::Failed,
            });
        }

        if is_retryable_failure(code) && self.retry_attempts < self.max_retries {
            self.retry_attempts += 1;
            self.state = SupervisorState::Restarting {
                attempt: self.retry_attempts,
            };
            return Ok(SupervisorAction::Retry {
                attempt: self.retry_attempts,
            });
        }

        self.state = SupervisorState::Failed { code, detail };
        Ok(SupervisorAction::Terminal)
    }
}

fn is_retryable_failure(code: FailureCode) -> bool {
    code == FailureCode::RuntimeUnavailable
}

#[derive(Debug, Error, Eq, PartialEq)]
pub enum TransitionError {
    #[error("invalid runtime transition from {from:?} using {event:?}")]
    Invalid {
        from: SupervisorState,
        event: RuntimeState,
    },
}
