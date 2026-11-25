import { useEffect, useState } from "react";

export function useFilterExpansion<Group extends string>(view: string, defaults: Record<Group, boolean>) {
  const key = `nevetracker-filter-expansion:${view}`;
  const [expanded, setExpanded] = useState<Record<Group, boolean>>(() => {
    try {
      const stored = JSON.parse(sessionStorage.getItem(key) || "{}");
      return Object.assign({}, defaults, ...Object.keys(defaults)
        .filter(group => typeof stored?.[group] === "boolean")
        .map(group => ({ [group]: stored[group] })));
    } catch {
      return defaults;
    }
  });
  useEffect(() => {
    try {
      sessionStorage.setItem(key, JSON.stringify(expanded));
    } catch {
      // Expansion still works in memory when session storage is unavailable.
    }
  }, [key, expanded]);
  return [expanded, setExpanded] as const;
}
