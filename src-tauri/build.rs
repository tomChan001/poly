fn main() {
    #[cfg(target_os = "macos")]
    tauri_build::build();

    #[cfg(not(target_os = "macos"))]
    println!("cargo:rerun-if-changed=tauri.conf.json");
}
