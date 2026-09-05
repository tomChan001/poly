import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { DesktopBootScreen, desktopBootPropsFromLocation } from './desktop/DesktopBootScreen.tsx'
import { installDesktopAdapter } from './desktop/desktopAdapter.ts'
import { installDesktopSessionFromLocation } from './api/desktopSession.ts'

const desktopBootProps = desktopBootPropsFromLocation(window.location)
if (desktopBootProps) installDesktopAdapter()
installDesktopSessionFromLocation(window.location, window.history)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {desktopBootProps ? <DesktopBootScreen {...desktopBootProps} /> : <App />}
  </StrictMode>,
)
