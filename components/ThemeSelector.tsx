import type { ThemeMode } from "../src/theme";

const themes: { mode: ThemeMode; label: string }[] = [
  { mode: "system", label: "Sistema" },
  { mode: "light", label: "Claro" },
  { mode: "dark", label: "Escuro" },
];

export default function ThemeSelector({ value, onChange }: { value: ThemeMode; onChange: (mode: ThemeMode) => void }) {
  return <div className="theme-options" role="group" aria-label="Tema">
    {themes.map(({ mode, label }) => (
      <button type="button" key={mode} className="theme-option" aria-pressed={mode === value}
        aria-label={label} data-tooltip={label} onClick={() => onChange(mode)}>
        <span className="theme-option__preview" data-mode={mode} aria-hidden="true">
          {(mode === "system" ? ["light", "dark"] : [mode]).map(palette => (
            <span key={palette} className="theme-option__pane" data-palette={palette}>
              <i /><b><em /><em /><em /></b>
            </span>
          ))}
        </span>
      </button>
    ))}
  </div>;
}
