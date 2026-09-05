import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { DesktopBootScreen, desktopBootPropsFromLocation } from './desktop/DesktopBootScreen.tsx'

const desktopBootProps = desktopBootPropsFromLocation(window.location)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {desktopBootProps ? <DesktopBootScreen {...desktopBootProps} /> : <App />}
  </StrictMode>,
)
