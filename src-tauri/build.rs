fn main() {
    #[cfg(target_os = "macos")]
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "retry_desktop_runtime",
            "reveal_desktop_logs",
            "open_market_url",
        ]),
    ))
    .expect("failed to build desktop permissions");

    #[cfg(not(target_os = "macos"))]
    println!("cargo:rerun-if-changed=tauri.conf.json");
}
