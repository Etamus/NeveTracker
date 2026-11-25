import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { CheckCircle2, ChevronDown } from "lucide-react";
import { useAnchoredPopover } from "./useAnchoredPopover";

type StyledSelectOption<T extends string | number> = {
  value: T;
  label: string;
};

type StyledSelectProps<T extends string | number> = {
  value: T;
  options: StyledSelectOption<T>[];
  onChange: (value: T) => void;
  ariaLabel: string;
  disabled?: boolean;
  placeholder?: string;
  showAllOptions?: boolean;
};

export const StyledSelect = <T extends string | number,>({
  value,
  options,
  onChange,
  ariaLabel,
  disabled = false,
  placeholder,
  showAllOptions = false,
}: StyledSelectProps<T>) => {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const initialFocus = useRef(0);
  const typed = useRef({ value: "", at: 0 });
  const menuId = useId();
  const matchedIndex = options.findIndex((option) => option.value === value);
  const selectedIndex = Math.max(0, matchedIndex);
  const selectedOption = placeholder && matchedIndex < 0 ? undefined : options[selectedIndex];
  useAnchoredPopover(open, trigger, menu, true, showAllOptions);
  useLayoutEffect(() => {
    if (open) menu.current?.querySelector<HTMLButtonElement>(`[data-index="${initialFocus.current}"]`)?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open]);

  const show = (index: number) => {
    if (!options.length || disabled) return;
    initialFocus.current = index;
    typed.current.value = "";
    setOpen(true);
  };
  const close = () => {
    setOpen(false);
    trigger.current?.focus({ preventScroll: true });
  };

  return (
    <div ref={rootRef} className={open ? "styled-select styled-select--open" : "styled-select"} onBlur={(event) => {
      if (open && event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }} onKeyDown={(event) => {
      if (open && event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close();
      }
    }}>
      <button
        ref={trigger}
        type="button"
        className="styled-select__trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={disabled}
        onClick={() => open ? close() : show(selectedIndex)}
        onKeyDown={(event) => {
          if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          show(event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : selectedIndex);
        }}
      >
        <span>{selectedOption?.label || placeholder || "Selecione"}</span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {open && (
        <div ref={menu} id={menuId} popover="manual" className="styled-select__menu" role="listbox" aria-label={ariaLabel}
          onKeyDown={(event) => {
            const current = Number((event.target as HTMLElement).closest<HTMLElement>("[data-index]")?.dataset.index);
            if (!Number.isInteger(current)) return;
            let target = current;
            if (event.key === "ArrowDown") target = (current + 1) % options.length;
            else if (event.key === "ArrowUp") target = (current + options.length - 1) % options.length;
            else if (event.key === "Home") target = 0;
            else if (event.key === "End") target = options.length - 1;
            else if (event.key === "Tab") {
              // Let the native Tab move to the adjacent control from the trigger.
              close();
              return;
            } else if (event.key.length === 1 && event.key !== " " && !event.ctrlKey && !event.altKey && !event.metaKey) {
              const now = performance.now();
              typed.current.value = (now - typed.current.at < 650 ? typed.current.value : "") + event.key.toLocaleLowerCase("pt-BR");
              typed.current.at = now;
              target = options.findIndex((option) => option.label.toLocaleLowerCase("pt-BR").startsWith(typed.current.value));
            } else return;
            event.preventDefault();
            menu.current?.querySelector<HTMLButtonElement>(`[data-index="${target}"]`)?.focus();
          }}>
          {options.map((option, index) => {
            const selected = option.value === value;
            return (
              <button
                key={String(option.value)}
                type="button"
                role="option"
                data-index={index}
                tabIndex={-1}
                aria-selected={selected}
                className={selected ? "styled-select__option styled-select__option--selected" : "styled-select__option"}
                onClick={() => {
                  onChange(option.value);
                  close();
                }}
              >
                <span>{option.label}</span>
                {selected && <CheckCircle2 size={14} aria-hidden="true" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
};

