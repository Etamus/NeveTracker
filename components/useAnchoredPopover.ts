import { useLayoutEffect } from "react";
import type { RefObject } from "react";

/** Keep the popup inside its owning dialog in the DOM and above its scrolling surface. */
export function useAnchoredPopover(
  open: boolean,
  trigger: RefObject<HTMLButtonElement | null>,
  popup: RefObject<HTMLDivElement | null>,
  matchWidth = false,
  showAllOptions = false,
) {
  useLayoutEffect(() => {
    const anchor = trigger.current, menu = popup.current;
    if (!open || !anchor || !menu) return;
    menu.showPopover?.();
    const position = () => {
      const zoom = menu.currentCSSZoom || 1;
      const bounds = anchor.getBoundingClientRect();
      const rect = { top: bounds.top / zoom, bottom: bounds.bottom / zoom, left: bounds.left / zoom, width: bounds.width / zoom };
      const width = window.innerWidth / zoom;
      const height = window.innerHeight / zoom;
      const margin = 8, gap = 5;
      const menuStyle = getComputedStyle(menu);
      if (matchWidth) menu.style.width = `${Math.min(rect.width, width - 2 * margin)}px`;
      const above = Math.max(0, rect.top - gap - margin);
      const below = Math.max(0, height - rect.bottom - gap - margin);
      const limit = showAllOptions ? height - 2 * margin : parseFloat(menuStyle.getPropertyValue("--menu-limit")) || height;
      const desired = Math.min(limit, menu.scrollHeight + menu.offsetHeight - menu.clientHeight);
      const upwards = below < desired && above > below;
      menu.style.maxHeight = `${showAllOptions ? limit : Math.min(limit, upwards ? above : below)}px`;
      const top = upwards ? rect.top - gap - menu.offsetHeight : rect.bottom + gap;
      menu.style.top = `${Math.max(margin, Math.min(top, height - menu.offsetHeight - margin))}px`;
      const align = menuStyle.getPropertyValue("--menu-align").trim();
      let left = rect.left;
      if (align === "end") left += rect.width - menu.offsetWidth;
      if (align === "viewport-center") left = (width - menu.offsetWidth) / 2;
      menu.style.left = `${Math.max(margin, Math.min(left, width - menu.offsetWidth - margin))}px`;
    };
    const onScroll = (event: Event) => {
      if (!(event.target instanceof Node) || !menu.contains(event.target)) position();
    };
    position();
    const observer = new ResizeObserver(position);
    observer.observe(menu);
    observer.observe(anchor);
    window.addEventListener("resize", position);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", onScroll, true);
      if (menu.isConnected) menu.hidePopover?.();
    };
  }, [open, matchWidth, showAllOptions, popup, trigger]);
}
