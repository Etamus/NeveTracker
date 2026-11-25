import { useEffect, useId, useRef, useState } from "react";
import { CalendarDays, ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import { useAnchoredPopover } from "./useAnchoredPopover";

const iso = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const dateOf = (value: string) => new Date(`${value}T12:00:00`);
const label = (value: string) => value ? value.split("-").reverse().join("/") : "…";

/** Shared themed range picker. Draft dates are applied together, avoiding partial queries. */
export default function DatePeriodSelect({ from, to, onChange, allowAll = true, maxDays, ariaLabel = "Período", compact = false, singleDay = false }: {
  from: string; to: string; onChange: (from: string, to: string) => void;
  allowAll?: boolean; maxDays?: number; ariaLabel?: string;
  compact?: boolean; singleDay?: boolean;
}) {
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const [open, setOpen] = useState(false);
  const [start, setStart] = useState(from);
  const [end, setEnd] = useState(to);
  const [selectEnd, setSelectEnd] = useState(false);
  const [month, setMonth] = useState(() => dateOf(from || iso(new Date())));
  useAnchoredPopover(open, trigger, menu);
  useEffect(() => {
    if (!open) return;
    const outside = (event: MouseEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus(); } };
    document.addEventListener("mousedown", outside);
    root.current?.addEventListener("keydown", key);
    const node = root.current;
    return () => { document.removeEventListener("mousedown", outside); node?.removeEventListener("keydown", key); };
  }, [open]);
  const dismiss = () => { setOpen(false); trigger.current?.focus({ preventScroll: true }); };
  const apply = (a: string, b: string) => { onChange(a, b); dismiss(); };
  const preset = (days: number) => { const now = new Date(); const begin = new Date(now); begin.setDate(begin.getDate() - days + 1); apply(iso(begin), iso(now)); };
  const invalid = !start || !end || start > end || !!(maxDays && (dateOf(end).getTime() - dateOf(start).getTime()) / 86400000 > maxDays - 1);
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const monthLabel = month.toLocaleDateString("pt-BR", { month: "long", year: "numeric" });
  const offset = (first.getDay() + 6) % 7;
  const count = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
  return <div className="date-period" ref={root}>
    <button ref={trigger} type="button" className={`date-period__trigger ${compact ? "icon-only-button icon-button date-period__trigger--compact" : ""} ${from ? "date-period__trigger--selected" : ""}`} aria-label={ariaLabel} data-tooltip={from ? `${ariaLabel}: ${singleDay ? label(from) : `${label(from)} - ${label(to)}`}` : ariaLabel} aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? menuId : undefined} onClick={() => { if (!open) { setStart(from); setEnd(to); setSelectEnd(false); setMonth(dateOf(from || iso(new Date()))); } setOpen(!open); }}>
      <CalendarDays size={compact ? 17 : 15} />{!compact && <><span>{from || to ? `${label(from)} - ${label(to)}` : "Todo o período"}</span><ChevronDown className="date-period__chevron" size={14} aria-hidden="true" /></>}
    </button>
    {open && <div ref={menu} id={menuId} popover="manual" className="date-period__menu" role="dialog" aria-label={`Selecionar ${ariaLabel.toLowerCase()}`}>
      <div className="date-period__presets">{(singleDay ? [[1,"Hoje"]] : [[1,"Hoje"],[7,"7 dias"],[30,"30 dias"],[90,"90 dias"]]).map(([days, text]) => <button type="button" key={days} onClick={() => preset(Number(days))}>{text}</button>)}{allowAll && <button type="button" onClick={() => apply("", "")}>Todos</button>}</div>
      <div className="date-period__range"><button type="button" className={!selectEnd ? "is-active" : ""} onClick={() => setSelectEnd(false)}>{singleDay ? "Data" : "De"}<strong>{label(start)}</strong></button>{!singleDay && <button type="button" className={selectEnd ? "is-active" : ""} onClick={() => setSelectEnd(true)}>Até<strong>{label(end)}</strong></button>}</div>
      <div className="date-period__month"><button type="button" className="icon-only-button icon-button" aria-label="Mês anterior" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth()-1, 1))}><ChevronLeft size={16}/></button><span>{monthLabel[0].toLocaleUpperCase("pt-BR") + monthLabel.slice(1)}</span><button type="button" className="icon-only-button icon-button" aria-label="Próximo mês" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth()+1, 1))}><ChevronRight size={16}/></button></div>
      <div className="date-period__calendar">{["S","T","Q","Q","S","S","D"].map((day, i) => <span key={`weekday-${i}`}>{day}</span>)}{Array.from({length: offset}, (_, i) => <span key={`blank-${i}`}/>)}{Array.from({length: count}, (_, i) => {
        const value = iso(new Date(month.getFullYear(), month.getMonth(), i+1));
        return <button type="button" key={value} aria-label={label(value)} aria-pressed={value === start || value === end} className={`${value === start || value === end ? "is-selected" : ""} ${start && end && value > start && value < end ? "in-range" : ""}`} onClick={() => {
          if (singleDay) { apply(value,value); return; }
          if (!selectEnd) { setStart(value); if (end && value > end) setEnd(""); setSelectEnd(true); }
          else if (start && value < start) { setStart(value); setEnd(start); } else { setEnd(value); setSelectEnd(false); }
        }}>{i+1}</button>;
      })}</div>
      {maxDays && !invalid ? null : start && end && invalid && <p role="alert">Selecione um período válido{maxDays ? ` de até ${maxDays} dias` : ""}.</p>}
      <div className="date-period__footer"><button type="button" className="button-quiet" onClick={dismiss}>Cancelar</button><button type="button" className="button-primary" disabled={invalid} onClick={() => apply(start, end)}>Aplicar</button></div>
    </div>}
  </div>;
}
