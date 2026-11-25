type ListLayout = {
  viewport: string;
  rows: string;
  header?: string;
  fit?: boolean;
};

const layouts: ListLayout[] = [
  { viewport: ".paginated-list", rows: "tbody > tr, :scope > .catalog-card" },
  { viewport: ".analytics-sheet__scroll, .lead-time-scroll", rows: "tbody > tr", header: "thead th" },
  { viewport: ".sku-database-table", rows: ".sku-status-row", header: ".sku-status-table-head" },
  { viewport: ".source-history-table", rows: ".source-history-list > .source-history-row", header: ".source-history-row--head" },
  { viewport: ".sku-selector-list", rows: ":scope > .sku-selector-row" },
  { viewport: ".styled-select__menu", rows: ":scope > .styled-select__option" },
  { viewport: ".sort-menu", rows: ":scope > button" },
  { viewport: ".workspace-filter-popover", rows: ".filter-section__trigger, .filter-option, .price-range-filter, .analytics-sheet__period", header: ".workspace-filter-popover__header" },
  { viewport: ".extraction-store-options, .extraction-seller-list", rows: ":scope > .filter-option" },
  { viewport: ".product-analytics-offers", rows: ":scope > .detail-offer-row" },
  { viewport: ".store-ranking-popover", rows: ":scope > .store-ranking-popover__row" },
  { viewport: ".tab-scroll-region", rows: ".store-ranking-row, .analytics-product-row", fit: false },
  { viewport: ".freight-map__state-list, .admin-user-list", rows: ":scope > button, :scope > .admin-user-row" },
  { viewport: ".modal-sidebar", rows: ".modal-sidebar__item, .extraction-tab" },
  { viewport: ".date-period__menu", rows: ".date-period__calendar > button, .date-period__month, .date-period__range, .date-period__presets > button, .date-period__footer", fit: false },
];

const cutoffAttribute = "data-row-clipped";
const fittedHeight = "--whole-rows-height";
const tolerance = 0.75;

function scrollOffset(row: DOMRect, top: number, bottom: number) {
  if (row.top < top - tolerance) return row.top - top;
  if (row.bottom > bottom + tolerance) return row.bottom - bottom;
  return 0;
}

function scrollBounds(viewport: HTMLElement) {
  const zoom = viewport.currentCSSZoom || 1;
  const bounds = viewport.getBoundingClientRect();
  const top = bounds.top + viewport.clientTop * zoom;
  const bottomEdge = viewport.offsetHeight - viewport.clientHeight - viewport.clientTop;
  return {top, bottom: bounds.bottom - bottomEdge * zoom, zoom};
}

function visibleBounds(viewport: HTMLElement, header?: HTMLElement | null) {
  let {top, bottom, zoom} = scrollBounds(viewport);
  let capacity = Math.min(innerHeight, bottom - top);
  let container = viewport;
  // A top-layer popover or modal escapes the clipping of its DOM ancestors.
  while (!container.matches(":popover-open, :modal") && container.parentElement) {
    container = container.parentElement;
    if (/^(auto|scroll|hidden|clip)$/.test(getComputedStyle(container).overflowY)) {
      const bounds = scrollBounds(container);
      top = Math.max(top, bounds.top);
      bottom = Math.min(bottom, bounds.bottom);
      capacity = Math.min(capacity, bounds.bottom - bounds.top);
    }
  }
  if (header) {
    const bounds = header.getBoundingClientRect();
    // Count a header only while it intersects the visible top edge.
    if (bounds.top <= top + tolerance && bounds.bottom > top) {
      capacity -= bounds.bottom - top;
      top = bounds.bottom;
    }
  }
  return {top: Math.max(0, top), bottom: Math.min(innerHeight, bottom), zoom, capacity};
}

type List = {viewport: HTMLElement; rows: HTMLElement[]; header: HTMLElement | null; fit: boolean};

function fullRowHeight({viewport, rows, header, fit}: List, scrollTop: number) {
  if (!fit || !viewport.clientHeight || viewport.scrollHeight <= viewport.clientHeight) return null;
  const scrollport = scrollBounds(viewport);
  const {top, bottom, zoom} = visibleBounds(viewport, header);
  if (bottom <= top || scrollport.bottom > bottom + tolerance) return null;
  const shift = (scrollTop - viewport.scrollTop) * zoom;
  let lastBottom = -Infinity;
  for (const row of rows) {
    const bounds = row.getBoundingClientRect();
    if (bounds.top >= top + shift - tolerance && bounds.bottom <= bottom + shift + tolerance) {
      lastBottom = Math.max(lastBottom, bounds.bottom - shift);
    }
  }
  if (!Number.isFinite(lastBottom) || bottom - lastBottom <= tolerance) return null;
  return (viewport.getBoundingClientRect().height - (bottom - lastBottom)) / zoom;
}

function rowStarts({viewport, rows, header}: List) {
  const {top, zoom} = scrollBounds(viewport);
  const padding = header?.getBoundingClientRect().height ?? 0;
  let last = 0;
  const starts = [0];
  for (const row of rows) {
    const offset = viewport.scrollTop + (row.getBoundingClientRect().top - top - padding) / zoom;
    if (offset > last + tolerance) {
      starts.push(offset);
      last = offset;
    }
  }
  return starts;
}

/**
 * Fit each scrollport to the last complete row when its layout settles. During
 * native scrolling, temporarily mask rows at the edges without changing height.
 */
export function installWholeRows() {
  let lists: List[] = [];
  let observed = new Set<HTMLElement>();
  let clipped = new Set<HTMLElement>();
  let fitted = new Set<HTMLElement>();
  let refresh = true;
  let reflow = true;
  let frame = 0;
  const restoredScroll = new WeakMap<HTMLElement, number>();
  const requestedScroll = new Map<HTMLElement, number>();
  const wheelInput = new WeakMap<HTMLElement, {delta: number; at: number}>();

  const fitLists = () => {
    // Measure the natural limit again before sizing. This lets a list grow when
    // the window grows and avoids using its previous fitted height as a ceiling.
    const offsets = new Map(lists.map(({viewport}) =>
      [viewport, requestedScroll.get(viewport) ?? viewport.scrollTop]));
    requestedScroll.clear();
    for (const viewport of fitted) {
      viewport.style.removeProperty(fittedHeight);
      viewport.removeAttribute("data-rows-fitted");
    }
    fitted = new Set();

    const measure = (entries: List[]) => entries
      .map(list => ({viewport: list.viewport, height: fullRowHeight(list, offsets.get(list.viewport) ?? 0)}));
    for (const {viewport, height} of measure(lists)) {
      if (height === null) continue;
      viewport.style.setProperty(fittedHeight, `${height}px`);
      viewport.setAttribute("data-rows-fitted", "");
      fitted.add(viewport);
    }
    // Auto-sized dialogs can redistribute space after the first fit. Read
    // their final scrollport and close any remaining gap before painting.
    for (const {viewport, height} of measure(lists.filter(list => fitted.has(list.viewport)))) {
      if (height !== null) viewport.style.setProperty(fittedHeight, `${height}px`);
    }
    for (const [viewport, offset] of offsets) {
      if (Math.abs(offset - viewport.scrollTop) > tolerance) {
        viewport.scrollTop = offset;
        restoredScroll.set(viewport, viewport.scrollTop);
      }
    }
  };

  const update = () => {
    frame = 0;
    if (refresh) {
      refresh = false;
      lists = layouts.flatMap(({viewport, rows, header, fit = true}) =>
        Array.from(document.querySelectorAll<HTMLElement>(viewport), element => ({
          viewport: element,
          rows: Array.from(element.querySelectorAll<HTMLElement>(rows)),
          header: header ? element.querySelector<HTMLElement>(header) : null,
          fit,
        })).filter(list => list.rows.length > 0),
      );
      const next = new Set(lists.flatMap(list => [list.viewport, list.viewport.parentElement, ...list.rows]).filter((element): element is HTMLElement => !!element));
      for (const element of observed) if (!next.has(element)) resize.unobserve(element);
      for (const element of next) if (!observed.has(element)) resize.observe(element);
      observed = next;
    }

    if (reflow) {
      reflow = false;
      fitLists();
    }

    const nextClipped = new Set<HTMLElement>();
    for (const {viewport, rows, header} of lists) {
      if (!viewport.clientHeight) continue;
      const {top, bottom, capacity} = visibleBounds(viewport, header);
      if (bottom <= top) continue;
      for (const row of rows) {
        const bounds = row.getBoundingClientRect();
        // An oversized row remains readable by scrolling through its content.
        if (bounds.height > capacity + tolerance) continue;
        if (bounds.bottom <= top || bounds.top >= bottom) continue;
        if (bounds.top < top - tolerance || bounds.bottom > bottom + tolerance) nextClipped.add(row);
      }
    }
    for (const row of clipped) if (!nextClipped.has(row)) row.removeAttribute(cutoffAttribute);
    for (const row of nextClipped) if (!clipped.has(row)) row.setAttribute(cutoffAttribute, "");
    clipped = nextClipped;
  };
  const schedule = () => {
    if (!frame) frame = requestAnimationFrame(update);
  };
  const scheduleFit = () => {
    reflow = true;
    schedule();
  };
  const nextOffset = (list: List, forward: boolean) => {
    const {viewport} = list;
    const current = requestedScroll.get(viewport) ?? viewport.scrollTop;
    const maximum = viewport.scrollHeight - viewport.clientHeight;
    if (maximum <= 1 || (forward ? current >= maximum - tolerance : current <= tolerance)) return;
    const starts = rowStarts(list);
    // Catalog cards and menu options can start after a small inset.
    if (starts[1] <= 16) starts.splice(1, 1);
    return forward ? starts.find(offset => offset > current + tolerance)
      : starts.reverse().find(offset => offset < current - tolerance);
  };
  const wheel = (event: WheelEvent) => {
    if (event.defaultPrevented || event.ctrlKey || event.shiftKey || !event.deltaY ||
        Math.abs(event.deltaX) >= Math.abs(event.deltaY) || !(event.target instanceof Element)) return;
    const owner = event.composedPath().find(node => lists.some(candidate => candidate.viewport === node));
    const list = lists.find(candidate => candidate.viewport === owner);
    if (!list?.fit) return;
    const {viewport} = list;
    const next = nextOffset(list, event.deltaY > 0);
    if (next === undefined) return;
    event.preventDefault();
    if (event.deltaMode === 0) {
      const input = wheelInput.get(viewport);
      const now = performance.now();
      const delta = input && now - input.at < 160 && input.delta * event.deltaY > 0 ? input.delta + event.deltaY : event.deltaY;
      wheelInput.set(viewport, {delta: Math.abs(delta) < 28 ? delta : 0, at: now});
      if (Math.abs(delta) < 28) return;
    }
    requestedScroll.set(viewport, next);
    scheduleFit();
  };
  const arrow = (event: KeyboardEvent) => {
    if (event.defaultPrevented || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
    const list = lists.find(candidate => candidate.fit && candidate.viewport === event.target);
    if (!list) return;
    const next = nextOffset(list, event.key === "ArrowDown");
    if (next === undefined) return;
    event.preventDefault();
    requestedScroll.set(list.viewport, next);
    scheduleFit();
  };
  const focus = (event: FocusEvent) => {
    if (!(event.target instanceof Element)) return;
    const target = event.target;
    const list = lists.find(({rows}) => rows.some(row => row.contains(target)));
    const row = list?.rows.find(element => element.contains(target));
    if (!list || !row) return;
    const {top, bottom, zoom, capacity} = visibleBounds(list.viewport, list.header);
    const bounds = row.getBoundingClientRect();
    if (bounds.height > capacity + tolerance) return;
    const offset = scrollOffset(bounds, top, bottom);
    if (offset) list.viewport.scrollTop += offset / zoom;
    schedule();
  };
  const settle = (event: Event) => {
    const list = lists.find(({viewport}) => viewport === event.target);
    if (!list) return;
    const restored = restoredScroll.get(list.viewport);
    restoredScroll.delete(list.viewport);
    if (restored !== undefined && Math.abs(restored - list.viewport.scrollTop) <= tolerance) return;
    if (list.fit) {
      const {viewport} = list;
      const offset = viewport.scrollTop;
      if (offset > tolerance) {
        const maximum = viewport.scrollHeight - viewport.clientHeight;
        const starts = rowStarts(list);
        const next = offset >= maximum - tolerance
          ? starts.find(value => value >= offset - tolerance)
          : starts.reduce((closest, value) => Math.abs(value - offset) < Math.abs(closest - offset) ? value : closest, 0);
        if (next !== undefined) requestedScroll.set(viewport, next);
      }
      scheduleFit();
      return;
    }
    const {top, bottom, zoom} = visibleBounds(list.viewport, list.header);
    let closest = Infinity;
    for (const row of list.rows) {
      const bounds = row.getBoundingClientRect();
      if (bounds.height > bottom - top + tolerance || bounds.bottom <= top || bounds.top >= bottom) continue;
      const offset = scrollOffset(bounds, top, bottom);
      if (!offset) return;
      if (Math.abs(offset) < Math.abs(closest)) closest = offset;
    }
    // A short viewport can stop between two rows with neither fully visible.
    if (Number.isFinite(closest)) list.viewport.scrollTop += closest / zoom;
  };

  const resize = new ResizeObserver(scheduleFit);
  const mutations = new MutationObserver(() => { refresh = true; scheduleFit(); });
  mutations.observe(document.body, {childList: true, subtree: true});
  window.addEventListener("resize", scheduleFit);
  document.addEventListener("scroll", schedule, {capture: true, passive: true});
  document.addEventListener("wheel", wheel, {capture: true, passive: false});
  document.addEventListener("keydown", arrow);
  document.addEventListener("scrollend", settle, true);
  document.addEventListener("focusin", focus);
  schedule();
  return () => {
    if (frame) cancelAnimationFrame(frame);
    resize.disconnect();
    mutations.disconnect();
    window.removeEventListener("resize", scheduleFit);
    document.removeEventListener("scroll", schedule, true);
    document.removeEventListener("wheel", wheel, true);
    document.removeEventListener("keydown", arrow);
    document.removeEventListener("scrollend", settle, true);
    document.removeEventListener("focusin", focus);
    for (const row of clipped) row.removeAttribute(cutoffAttribute);
    for (const viewport of fitted) {
      viewport.style.removeProperty(fittedHeight);
      viewport.removeAttribute("data-rows-fitted");
    }
  };
}
