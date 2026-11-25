export type ThemeMode = "system" | "light" | "dark";

export const THEME_STORAGE_KEY = "nevetracker-theme";
export const themeMedia = "(prefers-color-scheme: dark)";

export function resolveTheme(mode: ThemeMode) {
  return mode === "system" ? (window.matchMedia(themeMedia).matches ? "dark" : "light") : mode;
}

export function parseTheme(value: string | null | undefined): ThemeMode {
  return value === "light" || value === "dark" ? value : "system";
}
