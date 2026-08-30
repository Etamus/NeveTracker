import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

const previousThemeKeys = [
  `${['neve', 'track'].join('')}-theme`,
  `${['neve', 'price'].join('')}-theme`,
]
const storedTheme = localStorage.getItem('nevetracker-theme')
  ?? previousThemeKeys.map((key) => localStorage.getItem(key)).find(Boolean)
const initialTheme = storedTheme === 'light' || storedTheme === 'dark'
  ? storedTheme
  : window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'

document.documentElement.dataset.theme = initialTheme
localStorage.setItem('nevetracker-theme', initialTheme)
previousThemeKeys.forEach((key) => localStorage.removeItem(key))

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
