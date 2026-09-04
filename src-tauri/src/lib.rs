pub mod runtime;

#[cfg(target_os = "macos")]
pub fn run() {
    use tauri::Manager;

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .run(tauri::generate_context!())
        .expect("failed to run Poly desktop shell");
}

#[cfg(not(target_os = "macos"))]
pub fn run() {
    panic!("Poly desktop is supported on macOS only");
}

#[cfg(test)]
mod tests {
    use std::num::NonZeroU16;

    use crate::runtime::protocol::{RuntimeEvent, RuntimeState};
    use crate::runtime::state::{Supervisor, SupervisorAction, SupervisorState};

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
        let mut supervisor = Supervisor::new(2);
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
        let mut supervisor = Supervisor::new(2);
        let ready = parse(
            r#"{"version":1,"state":"ready","port":49152,"bootstrap_path":"/desktop/bootstrap/safe"}"#,
        );

        assert!(supervisor.apply(ready).is_err());
        assert_eq!(supervisor.state(), &SupervisorState::Idle);
    }

    #[test]
    fn deterministic_failures_are_terminal() {
        let mut supervisor = Supervisor::new(2);
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
    fn retryable_failures_retry_until_the_bound_then_become_terminal() {
        let mut supervisor = Supervisor::new(2);

        for expected_attempt in 1..=2 {
            supervisor
                .apply(parse(r#"{"version":1,"state":"initializing"}"#))
                .unwrap();
            let action = supervisor
                .apply(parse(
                    r#"{"version":1,"state":"failed","code":"runtime_unavailable","detail":"runtime exited"}"#,
                ))
                .unwrap();
            assert_eq!(
                action,
                SupervisorAction::Retry {
                    attempt: expected_attempt
                }
            );
            assert_eq!(
                supervisor.state(),
                &SupervisorState::Restarting {
                    attempt: expected_attempt
                }
            );
        }

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
    }
}
