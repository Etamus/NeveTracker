import { useState, type KeyboardEvent } from "react";
import type { LucideIcon } from "lucide-react";

type ViewTabsProps<Key extends string> = {
  className: string;
  label: string;
  panelId: string;
  tabs: readonly { key: Key; label: string; icon: LucideIcon }[];
  value: Key;
  onChange: (value: Key) => void;
};

export default function ViewTabs<Key extends string>({ className, label, panelId, tabs, value, onChange }: ViewTabsProps<Key>) {
  const [focused, setFocused] = useState<Key | null>(null);
  function moveFocus(event: KeyboardEvent<HTMLDivElement>) {
    const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
    let next: number;
    switch (event.key) {
      case "Home": next = 0; break;
      case "End": next = buttons.length - 1; break;
      case "ArrowLeft": next = (current - 1 + buttons.length) % buttons.length; break;
      case "ArrowRight": next = (current + 1) % buttons.length; break;
      default: return;
    }
    event.preventDefault();
    buttons[next]?.focus();
  }

  return (
    <div className={className} role="tablist" aria-label={label} onKeyDown={moveFocus}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setFocused(null); }}>
      {tabs.map(({ key, label, icon: Icon }) => (
        <button key={key} id={`${panelId}-tab-${key}`} type="button" role="tab"
          aria-selected={value === key} aria-controls={panelId} tabIndex={(focused ?? value) === key ? 0 : -1}
          aria-label={label} data-tooltip={label} onFocus={() => setFocused(key)} onClick={() => onChange(key)}>
          <Icon size={18} aria-hidden="true" />
        </button>
      ))}
    </div>
  );
}
