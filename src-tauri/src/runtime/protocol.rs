use std::num::NonZeroU16;

use serde::Deserialize;
use thiserror::Error;

pub const PROTOCOL_VERSION: u8 = 1;
const BOOTSTRAP_PREFIX: &str = "/desktop/bootstrap/";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RuntimeState {
    Initializing,
    PreparingDatabase,
    Migrating,
    StartingServices,
    Ready,
    ShuttingDown,
    Stopped,
    Failed,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum FailureCode {
    DatabaseUnavailable,
    InvalidStartCommand,
    MigrationFailed,
    ResourceMissing,
    RuntimeUnavailable,
    ShutdownFailed,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RuntimeEvent {
    Initializing,
    PreparingDatabase,
    Migrating {
        revision: Option<String>,
    },
    StartingServices,
    Ready {
        port: NonZeroU16,
        bootstrap_path: String,
    },
    ShuttingDown,
    Stopped {
        self_test: Option<String>,
    },
    Failed {
        code: FailureCode,
        detail: String,
    },
}

impl RuntimeEvent {
    pub fn parse_line(line: &str) -> Result<Self, ProtocolError> {
        let event: WireRuntimeEvent = serde_json::from_str(line)?;
        event.try_into()
    }

    pub const fn state(&self) -> RuntimeState {
        match self {
            Self::Initializing => RuntimeState::Initializing,
            Self::PreparingDatabase => RuntimeState::PreparingDatabase,
            Self::Migrating { .. } => RuntimeState::Migrating,
            Self::StartingServices => RuntimeState::StartingServices,
            Self::Ready { .. } => RuntimeState::Ready,
            Self::ShuttingDown => RuntimeState::ShuttingDown,
            Self::Stopped { .. } => RuntimeState::Stopped,
            Self::Failed { .. } => RuntimeState::Failed,
        }
    }

    pub const fn ready_port(&self) -> Option<NonZeroU16> {
        match self {
            Self::Ready { port, .. } => Some(*port),
            _ => None,
        }
    }

    pub fn bootstrap_path(&self) -> Option<&str> {
        match self {
            Self::Ready { bootstrap_path, .. } => Some(bootstrap_path),
            _ => None,
        }
    }
}

#[derive(Debug, Error)]
pub enum ProtocolError {
    #[error("invalid runtime event JSON: {0}")]
    InvalidJson(#[from] serde_json::Error),
    #[error("unsupported runtime protocol version {0}")]
    UnsupportedVersion(u8),
    #[error("ready event port must be nonzero")]
    InvalidPort,
    #[error("unsafe bootstrap path")]
    UnsafeBootstrapPath,
    #[error("unsafe value for runtime event field {0}")]
    UnsafeField(&'static str),
}

#[derive(Deserialize)]
#[serde(tag = "state", deny_unknown_fields)]
enum WireRuntimeEvent {
    #[serde(rename = "initializing")]
    Initializing { version: u8 },
    #[serde(rename = "preparing_database")]
    PreparingDatabase { version: u8 },
    #[serde(rename = "migrating")]
    Migrating {
        version: u8,
        #[serde(default)]
        revision: Option<String>,
    },
    #[serde(rename = "starting_services")]
    StartingServices { version: u8 },
    #[serde(rename = "ready")]
    Ready {
        version: u8,
        port: u16,
        bootstrap_path: String,
    },
    #[serde(rename = "shutting_down")]
    ShuttingDown { version: u8 },
    #[serde(rename = "stopped")]
    Stopped {
        version: u8,
        #[serde(default)]
        self_test: Option<String>,
    },
    #[serde(rename = "failed")]
    Failed {
        version: u8,
        code: FailureCode,
        detail: String,
    },
}

impl TryFrom<WireRuntimeEvent> for RuntimeEvent {
    type Error = ProtocolError;

    fn try_from(event: WireRuntimeEvent) -> Result<Self, Self::Error> {
        match event {
            WireRuntimeEvent::Initializing { version } => {
                validate_version(version)?;
                Ok(Self::Initializing)
            }
            WireRuntimeEvent::PreparingDatabase { version } => {
                validate_version(version)?;
                Ok(Self::PreparingDatabase)
            }
            WireRuntimeEvent::Migrating { version, revision } => {
                validate_version(version)?;
                if let Some(value) = revision.as_deref() {
                    validate_identifier(value, "revision")?;
                }
                Ok(Self::Migrating { revision })
            }
            WireRuntimeEvent::StartingServices { version } => {
                validate_version(version)?;
                Ok(Self::StartingServices)
            }
            WireRuntimeEvent::Ready {
                version,
                port,
                bootstrap_path,
            } => {
                validate_version(version)?;
                let port = NonZeroU16::new(port).ok_or(ProtocolError::InvalidPort)?;
                validate_bootstrap_path(&bootstrap_path)?;
                Ok(Self::Ready {
                    port,
                    bootstrap_path,
                })
            }
            WireRuntimeEvent::ShuttingDown { version } => {
                validate_version(version)?;
                Ok(Self::ShuttingDown)
            }
            WireRuntimeEvent::Stopped { version, self_test } => {
                validate_version(version)?;
                if self_test.as_deref().is_some_and(|value| value != "ok") {
                    return Err(ProtocolError::UnsafeField("self_test"));
                }
                Ok(Self::Stopped { self_test })
            }
            WireRuntimeEvent::Failed {
                version,
                code,
                detail,
            } => {
                validate_version(version)?;
                validate_detail(&detail)?;
                Ok(Self::Failed { code, detail })
            }
        }
    }
}

fn validate_version(version: u8) -> Result<(), ProtocolError> {
    if version == PROTOCOL_VERSION {
        Ok(())
    } else {
        Err(ProtocolError::UnsupportedVersion(version))
    }
}

fn validate_bootstrap_path(path: &str) -> Result<(), ProtocolError> {
    let token = path
        .strip_prefix(BOOTSTRAP_PREFIX)
        .ok_or(ProtocolError::UnsafeBootstrapPath)?;
    let token_is_safe = !token.is_empty()
        && token.len() <= 256
        && token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'));
    if !token_is_safe {
        return Err(ProtocolError::UnsafeBootstrapPath);
    }

    let parsed = url::Url::parse(&format!("http://127.0.0.1{path}"))
        .map_err(|_| ProtocolError::UnsafeBootstrapPath)?;
    if parsed.path() != path || parsed.query().is_some() || parsed.fragment().is_some() {
        return Err(ProtocolError::UnsafeBootstrapPath);
    }
    Ok(())
}

fn validate_identifier(value: &str, field: &'static str) -> Result<(), ProtocolError> {
    let is_safe = !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'));
    if is_safe {
        Ok(())
    } else {
        Err(ProtocolError::UnsafeField(field))
    }
}

fn validate_detail(detail: &str) -> Result<(), ProtocolError> {
    let is_safe = !detail.is_empty()
        && detail.len() <= 1024
        && detail.chars().all(|character| !character.is_control());
    if is_safe {
        Ok(())
    } else {
        Err(ProtocolError::UnsafeField("detail"))
    }
}
