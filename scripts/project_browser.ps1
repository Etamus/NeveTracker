function Open-ProjectBrowser([string]$Url, [string]$ProjectRoot) {
    # Let Windows open the normal default browser. Scraper browsers and their
    # profiles belong only to the collectors, never to the application's UI.
    Start-Process -FilePath $Url -ErrorAction Stop
}
