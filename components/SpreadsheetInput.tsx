import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";

const OVERSCAN = 5;
const MIN_ROWS = 20;
const ROW_HEIGHT = 33;

function firstRowToShow(index: number, scrollTop: number, visibleCount: number) {
  const first = Math.round(scrollTop / ROW_HEIGHT);
  return Math.max(0, Math.min(first, index), index - visibleCount + 1);
}

export default function SpreadsheetInput({ label, value, onChange, action, kind = "sku", disabled = false }: {
  label: string; value: string; onChange: (value: string) => void; action?: ReactNode; kind?: "sku" | "postal";
  disabled?: boolean;
}) {
  const field = useRef<HTMLDivElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const pendingFocus = useRef<number | null>(null);
  const [focused, setFocused] = useState<number | null>(null);
  const [firstVisible, setFirstVisible] = useState(0);
  const [visibleCount, setVisibleCount] = useState(MIN_ROWS);
  const rows = value.split(/\r?\n/);
  const total = Math.max(MIN_ROWS, rows.length + 1);
  const windowed = total > visibleCount + OVERSCAN * 2;
  const first = windowed ? Math.max(0, Math.min(total - visibleCount, firstVisible) - OVERSCAN) : 0;
  const end = windowed ? Math.min(total, first + visibleCount + OVERSCAN * 2) : total;

  useLayoutEffect(() => {
    const root = field.current, scroll = viewport.current;
    if (!root || !scroll) return;
    const header = root.querySelector<HTMLElement>(".textarea-field__head");
    if (!header) return;
    const align = () => {
      const available = root.clientHeight - header.offsetHeight - parseFloat(getComputedStyle(header).marginBottom);
      const style = getComputedStyle(scroll);
      const borders = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
      const count = Math.max(1, Math.floor((available - borders) / ROW_HEIGHT));
      scroll.style.height = `${count * ROW_HEIGHT + borders}px`;
      setVisibleCount(count);
    };
    align();
    const observer = new ResizeObserver(align);
    observer.observe(root);
    observer.observe(header);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    const index = pendingFocus.current;
    if (index === null) return;
    const scroll = viewport.current;
    const input = scroll?.querySelector<HTMLInputElement>(`[data-row="${index}"] input`);
    if (!scroll || !input) return;
    pendingFocus.current = null;
    scroll.scrollTop = firstRowToShow(index, scroll.scrollTop, visibleCount) * ROW_HEIGHT;
    input.focus({ preventScroll: true });
  }, [first, end, total, visibleCount]);

  useEffect(() => {
    const scroll = viewport.current;
    if (!scroll) return;
    let delta = 0;
    let lastWheel = 0;
    const wheel = (event: WheelEvent) => {
      if (event.defaultPrevented || event.ctrlKey || event.shiftKey || !event.deltaY ||
          Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
      const max = scroll.scrollHeight - scroll.clientHeight;
      const direction = Math.sign(event.deltaY);
      if (direction > 0 ? scroll.scrollTop >= max - 0.5 : scroll.scrollTop <= 0.5) return;
      event.preventDefault();
      const now = performance.now();
      delta = now - lastWheel < 160 && Math.sign(delta) === direction ? delta + event.deltaY : event.deltaY;
      lastWheel = now;
      if (event.deltaMode === 0 && Math.abs(delta) < 28) return;
      delta = 0;
      const row = Math.round(scroll.scrollTop / ROW_HEIGHT);
      scroll.scrollTop = Math.min(max, Math.max(0, row + direction) * ROW_HEIGHT);
    };
    const alignRow = () => {
      const max = scroll.scrollHeight - scroll.clientHeight;
      const top = Math.min(max, Math.round(scroll.scrollTop / ROW_HEIGHT) * ROW_HEIGHT);
      if (Math.abs(top - scroll.scrollTop) > 0.5) scroll.scrollTop = top;
    };
    scroll.addEventListener("wheel", wheel, { passive: false });
    scroll.addEventListener("scrollend", alignRow);
    return () => {
      scroll.removeEventListener("wheel", wheel);
      scroll.removeEventListener("scrollend", alignRow);
    };
  }, []);

  useLayoutEffect(() => {
    if (value || !viewport.current) return;
    viewport.current.scrollTop = 0;
    setFirstVisible(0);
  }, [value]);

  const replace = (index: number, cells: string[]) => {
    const next = [...rows];
    cells.forEach((cell, offset) => { next[index + offset] = cell; });
    onChange(next.join("\n"));
  };
  const focusRow = (target: number) => {
    const scroll = viewport.current;
    if (!scroll) return;
    const firstRow = firstRowToShow(target, scroll.scrollTop, visibleCount);
    const input = scroll.querySelector<HTMLInputElement>(`[data-row="${target}"] input`);
    if (input) {
      scroll.scrollTop = firstRow * ROW_HEIGHT;
      input.focus({ preventScroll: true });
      return;
    }
    pendingFocus.current = target;
    if (target >= total) replace(target, [""]);
    scroll.scrollTop = firstRow * ROW_HEIGHT;
    setFirstVisible(firstRow);
  };

  const renderRow = (index: number) => (
    <tr key={index} data-row={index} aria-rowindex={index + 1}>
      <th scope="row">{index + 1}</th>
      <td><input aria-label={`${label}, linha ${index + 1}`} value={rows[index] || ""}
        inputMode={kind === "postal" ? "numeric" : "text"} spellCheck={false} disabled={disabled}
        onChange={event => replace(index, [event.target.value])}
        onFocus={() => setFocused(index)}
        onBlur={() => setFocused(null)}
        onKeyDown={event => {
          let target;
          if (event.key === "ArrowUp") target = Math.max(0, index - 1);
          else if (event.key === "Enter" || event.key === "ArrowDown") target = index + 1;
          else if (event.key === "Tab" && windowed) {
            target = index + (event.shiftKey ? -1 : 1);
            if (target < 0 || target >= total) return;
          } else return;
          event.preventDefault();
          focusRow(target);
        }}
        onPaste={event => {
          const text = event.clipboardData.getData("text");
          const cells = text.split(kind === "postal" ? /[\s,;]+/ : /[\r\n\t,;]+/).map(cell => cell.trim()).filter(Boolean);
          if (cells.length) { event.preventDefault(); replace(index, cells); }
        }}
      /></td>
    </tr>
  );

  const visible = Array.from({ length: end - first }, (_, offset) => first + offset);
  // Preserve the cursor and selection when the user scrolls away from the edited cell.
  if (focused !== null && focused < total) {
    if (focused < first) visible.unshift(focused);
    if (focused >= end) visible.push(focused);
  }
  const body: ReactNode[] = [];
  let nextRow = 0;
  const spacer = (until: number) => {
    if (until <= nextRow) return;
    body.push(<tr key={`gap-${nextRow}`} className="extraction-sheet-gap" aria-hidden="true">
      <td colSpan={2} style={{ height: (until - nextRow) * ROW_HEIGHT }} />
    </tr>);
  };
  for (const index of visible) {
    spacer(index);
    body.push(renderRow(index));
    nextRow = index + 1;
  }
  spacer(total);
  return <div className="extraction-sheet-field" ref={field} style={{ "--spreadsheet-row-height": `${ROW_HEIGHT}px` } as CSSProperties}>
    <div className="textarea-field__head"><strong>{label}</strong>{action}</div>
    <div ref={viewport} className="extraction-sheet-scroll"
      onScroll={event => setFirstVisible(Math.floor((event.currentTarget.scrollTop + 0.5) / ROW_HEIGHT))}>
      <table className="extraction-input-sheet" aria-label={label} aria-rowcount={total}>
        <colgroup><col className="extraction-sheet-row-number" /><col /></colgroup>
        <tbody>{body}</tbody>
      </table>
    </div>
  </div>;
}
