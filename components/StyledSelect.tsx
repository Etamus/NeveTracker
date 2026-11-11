import { useEffect, useRef, useState } from "react";
import { CheckCircle2, ChevronDown } from "lucide-react";

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
};

export const StyledSelect = <T extends string | number,>({
  value,
  options,
  onChange,
  ariaLabel,
  disabled = false,
  placeholder,
}: StyledSelectProps<T>) => {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const matchedIndex = options.findIndex((option) => option.value === value);
  const selectedIndex = Math.max(0, matchedIndex);
  const selectedOption = placeholder && matchedIndex < 0 ? undefined : options[selectedIndex];

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open]);

  const selectAt = (index: number) => {
    const option = options[index];
    if (!option) return;
    onChange(option.value);
  };

  return (
    <div ref={rootRef} className={open ? "styled-select styled-select--open" : "styled-select"}>
      <button
        type="button"
        className="styled-select__trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setOpen(false);
            return;
          }
          if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          setOpen(true);
          if (event.key === "Home") return selectAt(0);
          if (event.key === "End") return selectAt(options.length - 1);
          const direction = event.key === "ArrowDown" ? 1 : -1;
          selectAt((selectedIndex + direction + options.length) % options.length);
        }}
      >
        <span>{selectedOption?.label || placeholder || "Selecione"}</span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {open && (
        <div className="styled-select__menu" role="listbox" aria-label={ariaLabel}>
          {options.map((option) => {
            const selected = option.value === value;
            return (
              <button
                key={String(option.value)}
                type="button"
                role="option"
                aria-selected={selected}
                className={selected ? "styled-select__option styled-select__option--selected" : "styled-select__option"}
                onClick={() => {
                  onChange(option.value);
                  setOpen(false);
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

