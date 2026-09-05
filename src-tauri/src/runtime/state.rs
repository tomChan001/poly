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
    Terminal,
}

#[derive(Debug)]
pub struct Supervisor {
    state: SupervisorState,
}

impl Supervisor {
    pub const fn new() -> Self {
        Self {
            state: SupervisorState::Idle,
        }
    }

    pub const fn state(&self) -> &SupervisorState {
        &self.state
    }

    pub fn begin_restart(&mut self, attempt: u8) -> Result<(), TransitionError> {
        if attempt == 0
            || matches!(
                self.state,
                SupervisorState::ShuttingDown | SupervisorState::Stopped
            )
        {
            return Err(TransitionError::RetryFrom {
                from: self.state.clone(),
                attempt,
            });
        }
        self.state = SupervisorState::Restarting { attempt };
        Ok(())
    }

    pub fn reset_for_operator_retry(&mut self) -> Result<(), TransitionError> {
        if !matches!(
            self.state,
            SupervisorState::Failed { .. } | SupervisorState::Restarting { .. }
        ) {
            return Err(TransitionError::ResetFrom {
                from: self.state.clone(),
            });
        }
        self.state = SupervisorState::Idle;
        Ok(())
    }

    pub fn mark_terminal_failure(&mut self, code: FailureCode, detail: String) {
        self.state = SupervisorState::Failed { code, detail };
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

        self.state = SupervisorState::Failed { code, detail };
        Ok(SupervisorAction::Terminal)
    }
}

impl Default for Supervisor {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, Error, Eq, PartialEq)]
pub enum TransitionError {
    #[error("invalid runtime transition from {from:?} using {event:?}")]
    Invalid {
        from: SupervisorState,
        event: RuntimeState,
    },
    #[error("cannot begin retry attempt {attempt} from {from:?}")]
    RetryFrom { from: SupervisorState, attempt: u8 },
    #[error("cannot reset runtime supervisor from {from:?}")]
    ResetFrom { from: SupervisorState },
}
