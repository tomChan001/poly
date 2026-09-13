use tokio::process::Command;
use url::Url;

pub fn market_browser_command(value: &str) -> Result<Command, &'static str> {
    if value.chars().any(char::is_control) {
        return Err("market URL is not allowed");
    }
    let url = Url::parse(value).map_err(|_| "market URL is not allowed")?;
    let official_host = url.host_str().is_some_and(|host| {
        ["kalshi.com", "polymarket.com"]
            .iter()
            .any(|domain| host == *domain || host.ends_with(&format!(".{domain}")))
    });
    if !matches!(url.scheme(), "https" | "http")
        || !official_host
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
    {
        return Err("market URL is not allowed");
    }
    let mut command = Command::new("/usr/bin/open");
    command.arg(url.as_str());
    Ok(command)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn opens_only_official_http_urls_with_a_fixed_program_and_one_argument() {
        for value in [
            "https://kalshi.com/markets/example/event",
            "https://www.kalshi.com/markets/example",
            "http://polymarket.com/event/example",
            "https://polymarket.com/event/example?note=%24%28touch%20bad%29&x=1",
        ] {
            let command = market_browser_command(value).unwrap();
            let command = command.as_std();
            assert_eq!(command.get_program(), "/usr/bin/open");
            assert_eq!(
                command.get_args().collect::<Vec<_>>(),
                vec![std::ffi::OsStr::new(value)]
            );
        }
    }

    #[test]
    fn rejects_non_official_urls_credentials_and_unsafe_protocols_before_launch() {
        for value in [
            "javascript:alert(1)",
            "file:///tmp/a",
            "https://kalshi.com.evil.test/event",
            "https://evil.test/?url=https://polymarket.com",
            "https://kalshi.com@evil.test/",
            "https://user:password@kalshi.com/event",
            "https://127.0.0.1/event",
            "--args",
            "https://kalshi.com/\nignored",
            "https://polymarket.com:8443/event",
        ] {
            assert!(market_browser_command(value).is_err(), "accepted {value}");
        }
    }

    #[test]
    fn runtime_capability_grants_only_link_opening_and_preserves_local_startup_permissions() {
        use tauri::utils::acl::capability::Capability;

        let runtime: Capability =
            serde_json::from_str(include_str!("../capabilities/runtime-market-links.json"))
                .unwrap();
        assert!(!runtime.local);
        assert_eq!(runtime.windows, ["main"]);
        assert_eq!(runtime.remote.unwrap().urls, ["http://127.0.0.1:*/*"]);
        assert_eq!(
            serde_json::to_value(runtime.permissions).unwrap(),
            serde_json::json!(["allow-open-market-url"])
        );

        let startup: Capability =
            serde_json::from_str(include_str!("../capabilities/main.json")).unwrap();
        assert!(startup.local);
        assert!(startup.remote.is_none());
        assert_eq!(
            serde_json::to_value(startup.permissions).unwrap(),
            serde_json::json!([
                "core:default",
                "allow-retry-desktop-runtime",
                "allow-reveal-desktop-logs"
            ])
        );
    }
}
