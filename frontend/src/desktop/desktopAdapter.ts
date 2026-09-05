import { invoke } from '@tauri-apps/api/core'

export function installDesktopAdapter() {
  window.__POLY_DESKTOP__ = {
    retry: () => invoke('retry_desktop_runtime'),
    revealLogs: () => invoke('reveal_desktop_logs'),
  }
}
