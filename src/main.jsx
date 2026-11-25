import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { parseTheme, resolveTheme, THEME_STORAGE_KEY } from './theme'
import { installTooltips } from './tooltips'
import { installWholeRows } from './wholeRows'

const previousThemeKeys = [
  `${['neve', 'track'].join('')}-theme`,
  `${['neve', 'price'].join('')}-theme`,
]
const storedTheme = localStorage.getItem(THEME_STORAGE_KEY)
  ?? previousThemeKeys.map((key) => localStorage.getItem(key)).find(Boolean)
const initialTheme = parseTheme(storedTheme)
document.documentElement.dataset.theme = resolveTheme(initialTheme)
localStorage.setItem(THEME_STORAGE_KEY, initialTheme)
previousThemeKeys.forEach((key) => localStorage.removeItem(key))
const disposeTooltips = installTooltips()
const disposeWholeRows = installWholeRows()
if (import.meta.hot) import.meta.hot.dispose(() => {
  disposeTooltips()
  disposeWholeRows()
})

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
