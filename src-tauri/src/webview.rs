use serde::Serialize;
use thiserror::Error;
use url::Url;

const BOOTSTRAP_PREFIX: &str = "/desktop/bootstrap/";

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DesktopUiState {
    Initializing,
    PreparingDatabase,
    Migrating,
    StartingServices,
    Restarting,
    KeychainDenied,
    MigrationFailed,
    RuntimeUnavailable,
    PermissionDenied,
    ResourceMissing,
    ProtocolFailed,
    ShuttingDown,
}

impl DesktopUiState {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Initializing => "initializing",
            Self::PreparingDatabase => "preparing_database",
            Self::Migrating => "migrating",
            Self::StartingServices => "starting_services",
            Self::Restarting => "restarting",
            Self::KeychainDenied => "keychain_denied",
            Self::MigrationFailed => "migration_failed",
            Self::RuntimeUnavailable => "runtime_unavailable",
            Self::PermissionDenied => "permission_denied",
            Self::ResourceMissing => "resource_missing",
            Self::ProtocolFailed => "protocol_failed",
            Self::ShuttingDown => "shutting_down",
        }
    }
}

#[derive(Debug, Error)]
pub enum NavigationError {
    #[error("ready URL port must be nonzero")]
    InvalidPort,
    #[error("unsafe bootstrap path")]
    UnsafeBootstrapPath,
    #[error("invalid ready URL: {0}")]
    InvalidUrl(#[from] url::ParseError),
    #[error("ready URL must be plain HTTP on the exact IPv4 loopback host")]
    NonLoopbackUrl,
}

pub fn status_url(state: DesktopUiState) -> Url {
    status_url_with_logs(state, false)
}

pub(crate) fn status_url_with_logs(state: DesktopUiState, can_reveal_logs: bool) -> Url {
    let mut url = Url::parse("tauri://localhost/").expect("static bundled URL is valid");
    {
        let mut query = url.query_pairs_mut();
        query.append_pair("desktop-state", state.as_str());
        if can_reveal_logs {
            query.append_pair("can-reveal-logs", "true");
        }
    }
    url
}

pub fn ready_url(port: u16, bootstrap_path: &str) -> Result<Url, NavigationError> {
    if port == 0 {
        return Err(NavigationError::InvalidPort);
    }
    validate_bootstrap_path(bootstrap_path)?;

    let url = Url::parse(&format!("http://127.0.0.1:{port}{bootstrap_path}"))?;
    validate_loopback_url(&url, port, bootstrap_path)?;
    Ok(url)
}

fn validate_bootstrap_path(path: &str) -> Result<(), NavigationError> {
    let token = path
        .strip_prefix(BOOTSTRAP_PREFIX)
        .ok_or(NavigationError::UnsafeBootstrapPath)?;
    let safe = !token.is_empty()
        && token.len() <= 256
        && token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'));
    if safe {
        Ok(())
    } else {
        Err(NavigationError::UnsafeBootstrapPath)
    }
}

fn validate_loopback_url(
    url: &Url,
    expected_port: u16,
    expected_path: &str,
) -> Result<(), NavigationError> {
    let safe = url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port() == Some(expected_port)
        && url.username().is_empty()
        && url.password().is_none()
        && url.path() == expected_path
        && url.query().is_none()
        && url.fragment().is_none();
    if safe {
        Ok(())
    } else {
        Err(NavigationError::NonLoopbackUrl)
    }
}

#[cfg(test)]
mod tests {
    use super::{ready_url, status_url, validate_loopback_url, DesktopUiState};

    #[test]
    fn builds_an_internal_status_url_from_a_serializable_state() {
        let url = status_url(DesktopUiState::PreparingDatabase);

        assert_eq!(url.scheme(), "tauri");
        assert_eq!(url.host_str(), Some("localhost"));
        assert_eq!(
            url.query_pairs().collect::<Vec<_>>(),
            vec![("desktop-state".into(), "preparing_database".into())]
        );
    }

    #[test]
    fn builds_a_safe_loopback_ready_url() {
        let url = ready_url(49152, "/desktop/bootstrap/safe_token-1").unwrap();

        assert_eq!(
            url.as_str(),
            "http://127.0.0.1:49152/desktop/bootstrap/safe_token-1"
        );
        assert_eq!(url.username(), "");
        assert_eq!(url.password(), None);
        assert_eq!(url.query(), None);
        assert_eq!(url.fragment(), None);
    }

    #[test]
    fn rejects_zero_port_and_unsafe_bootstrap_paths() {
        for (port, path) in [
            (0, "/desktop/bootstrap/safe"),
            (49152, "/other/safe"),
            (49152, "/desktop/bootstrap/"),
            (49152, "/desktop/bootstrap/../admin"),
            (49152, "/desktop/bootstrap/safe?leak=1"),
            (49152, "/desktop/bootstrap/safe#fragment"),
            (49152, "//attacker.invalid/desktop/bootstrap/safe"),
            (49152, "http://attacker.invalid/desktop/bootstrap/safe"),
        ] {
            assert!(ready_url(port, path).is_err(), "accepted {port} {path}");
        }
    }

    #[test]
    fn rejects_nonloopback_ready_urls() {
        let url = url::Url::parse("http://attacker.invalid:49152/desktop/bootstrap/safe").unwrap();

        assert!(validate_loopback_url(&url, 49152, "/desktop/bootstrap/safe").is_err());
    }
}
