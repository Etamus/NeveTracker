export function installTooltips() {
  const listener = new AbortController();
  const options = { signal: listener.signal };
  const tooltip = document.createElement("div");
  tooltip.className = "ui-tooltip";
  tooltip.setAttribute("popover", "manual");
  // The controls already expose names and descriptions to assistive technology.
  tooltip.setAttribute("aria-hidden", "true");
  document.body.append(tooltip);

  let target: Element | null = null;
  let timer: number | undefined;
  const find = (node: EventTarget | null) =>
    node instanceof Element ? node.closest("[data-tooltip]") : null;
  const close = () => {
    window.clearTimeout(timer);
    target = null;
    if (tooltip.matches(":popover-open")) tooltip.hidePopover();
  };
  const open = (element: Element | null, delay: number) => {
    if (target === element) return;
    close();
    if (!element?.getAttribute("data-tooltip")) return;
    target = element;
    timer = window.setTimeout(() => {
      if (!element.isConnected || !element.getClientRects().length ||
          element.getAttribute("aria-expanded") === "true") return;
      tooltip.textContent = element.getAttribute("data-tooltip");
      tooltip.showPopover();
      const rect = element.getBoundingClientRect();
      const { width, height } = tooltip.getBoundingClientRect();
      const zoom = tooltip.currentCSSZoom || 1;
      tooltip.style.left = `${Math.max(6, Math.min(rect.left + (rect.width - width) / 2, innerWidth - width - 6)) / zoom}px`;
      tooltip.style.top = `${Math.max(4, Math.min(rect.top >= height + 10 ? rect.top - height - 4 : rect.bottom + 4, innerHeight - height - 4)) / zoom}px`;
    }, delay);
  };
  const hover = (event: PointerEvent) => {
    if (event.pointerType !== "touch") open(find(event.target), 350);
  };
  const leave = (event: PointerEvent) => {
    if (target && find(event.target) === target && find(event.relatedTarget) !== target) close();
  };
  document.addEventListener("pointerover", hover, options);
  document.addEventListener("pointerout", leave, options);
  document.addEventListener("focusin", event => {
    if (event.target instanceof Element && event.target.matches(":focus-visible")) open(find(event.target), 0);
  }, options);
  document.addEventListener("focusout", close, options);
  document.addEventListener("click", close, { ...options, capture: true });
  window.addEventListener("scroll", close, { ...options, capture: true });
  window.addEventListener("resize", close, options);
  document.addEventListener("keydown", event => { if (event.key === "Escape") close(); }, options);
  return () => {
    listener.abort();
    close();
    tooltip.remove();
  };
}
