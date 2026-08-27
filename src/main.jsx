import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

const storedTheme = localStorage.getItem('nevetrack-theme') ?? localStorage.getItem('neveprice-theme')
const initialTheme = storedTheme === 'light' || storedTheme === 'dark'
  ? storedTheme
  : window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'

document.documentElement.dataset.theme = initialTheme
localStorage.setItem('nevetrack-theme', initialTheme)

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
