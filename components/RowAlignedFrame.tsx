import { useLayoutEffect, useRef } from "react";
import type { ReactNode } from "react";

export default function RowAlignedFrame({ children, loading = false, rowCount }: { children: ReactNode; loading?: boolean; rowCount?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const metrics = useRef({rowHeight:42,count:50,emptyHeight:72});
  useLayoutEffect(() => {
    const frame = ref.current;
    const parent = frame?.parentElement;
    if (!frame || !parent) return;
    const align = () => {
      const row = frame.querySelector<HTMLElement>(".sku-status-row");
      const header = frame.querySelector<HTMLElement>(".sku-status-table-head");
      const footer = frame.querySelector<HTMLElement>(".panel-footer");
      const table = frame.querySelector<HTMLElement>(".sku-database-table");
      if (!header || !footer) return;
      const zoom = frame.currentCSSZoom || 1;
      const available = (parent.getBoundingClientRect().bottom - frame.getBoundingClientRect().top) / zoom - 2;
      const rowHeight = row ? row.getBoundingClientRect().height / zoom : metrics.current.rowHeight;
      metrics.current.rowHeight=rowHeight;
      const fixed = (header.getBoundingClientRect().height + footer.getBoundingClientRect().height) / zoom
        + (table ? table.offsetHeight - table.clientHeight : 0);
      const capacity = Math.max(1, Math.floor((available - fixed) / rowHeight));
      const knownCount = loading ? rowCount || metrics.current.count : rowCount ?? frame.querySelectorAll('.sku-status-row').length;
      if(!loading)metrics.current.count=knownCount;
      const count=Math.min(capacity,Math.max(1,knownCount));
      if(!loading && knownCount===0)metrics.current.emptyHeight=frame.querySelector<HTMLElement>('.sku-status-empty')?.offsetHeight || rowHeight;
      const bodyHeight=knownCount===0 ? metrics.current.emptyHeight : count*rowHeight;
      frame.style.height = `${fixed + bodyHeight + 2}px`;
      frame.style.maxHeight = frame.style.height;
    };
    align();
    const observer = new ResizeObserver(align);
    observer.observe(parent);
    return () => observer.disconnect();
  }, [children, loading, rowCount]);
  return <div ref={ref} className="sku-database-frame">{children}</div>;
}
