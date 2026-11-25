import { useEffect, useState } from "react";
import { createCurrencyFormatter, getInterfacePreferences, INTERFACE_PREFERENCES_EVENT, type InterfacePreferences } from "./preferences";

export function useCurrencyFormatter() {
  const [formatter, setFormatter] = useState(() => createCurrencyFormatter(getInterfacePreferences()));
  useEffect(() => {
    const sync = (event?: Event) => {
      const preferences = (event as CustomEvent<InterfacePreferences> | undefined)?.detail ?? getInterfacePreferences();
      setFormatter(createCurrencyFormatter(preferences));
    };
    window.addEventListener(INTERFACE_PREFERENCES_EVENT, sync);
    sync();
    return () => window.removeEventListener(INTERFACE_PREFERENCES_EVENT, sync);
  }, []);
  return formatter;
}
