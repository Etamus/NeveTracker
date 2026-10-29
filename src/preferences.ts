export const INTERFACE_PREFERENCES_KEY = "nevetracker-interface-preferences";
export const INTERFACE_PREFERENCES_EVENT = "nevetracker-interface-preferences-updated";
const PREVIOUS_INTERFACE_PREFERENCES_KEYS = [
  `${["neve", "track"].join("")}-interface-preferences`,
  `${["neve", "price"].join("")}-interface-preferences`,
];

export const STORE_OPTIONS = [
  { label: "Electrolux", aliases: ["electrolux", "eletrolux"] },
  { label: "Midea", aliases: ["midea"] },
  { label: "Consul", aliases: ["consul"] },
  { label: "Brastemp", aliases: ["brastemp"] },
  { label: "Mercado Livre", aliases: ["mercado livre", "meli", "-ml", "mlb"] },
  { label: "Magazine Luiza", aliases: ["magazine luiza", "magalu"] },
  { label: "Amazon Brasil", aliases: ["amazon brasil", "amazon"] },
  { label: "Leroy Merlin", aliases: ["leroy merlin", "leroy"] },
  { label: "Shopee", aliases: ["shopee"] },
  { label: "Duofrio", aliases: ["duofrio", "dufrio"] },
  { label: "Friolar", aliases: ["friolar", "friolar pecas", "friolar peças"] },
  { label: "Refrigeração Mota", aliases: ["refrigeração mota", "refrigeracao mota"] },
  { label: "MG Parts", aliases: ["mg parts", "mgparts"] },
  { label: "Gold Service", aliases: ["gold service", "goldservice"] },
  { label: "ComClick", aliases: ["comclick", "com click"] },
];

export const regionFormatOptions = [
  { value: "BRL", label: "Brasil (R$)", locale: "pt-BR" },
  { value: "USD", label: "Estados Unidos (US$)", locale: "en-US" },
  { value: "EUR", label: "Europa (EUR)", locale: "de-DE" },
];

export type InterfacePreferences = {
  currency: string;
  locale: string;
  defaultStores: string[];
  logisticsPostalCode: string;
};

export const defaultInterfacePreferences: InterfacePreferences = {
  currency: "BRL",
  locale: "pt-BR",
  defaultStores: STORE_OPTIONS.map((store) => store.label),
  logisticsPostalCode: "01001000",
};

const validCurrencies = new Set(regionFormatOptions.map((option) => option.value));
const validStores = new Set(STORE_OPTIONS.map((store) => store.label));

export const getInterfacePreferences = (): InterfacePreferences => {
  try {
    let storedPreferences = window.localStorage.getItem(INTERFACE_PREFERENCES_KEY);
    if (!storedPreferences) {
      for (const previousKey of PREVIOUS_INTERFACE_PREFERENCES_KEYS) {
        storedPreferences = window.localStorage.getItem(previousKey);
        if (!storedPreferences) continue;
        window.localStorage.setItem(INTERFACE_PREFERENCES_KEY, storedPreferences);
        window.localStorage.removeItem(previousKey);
        break;
      }
    }
    if (!storedPreferences) return defaultInterfacePreferences;

    const parsed = JSON.parse(storedPreferences) as Partial<InterfacePreferences>;
    let defaultStores = Array.isArray(parsed.defaultStores)
      ? parsed.defaultStores.filter((store) => validStores.has(store))
      : defaultInterfacePreferences.defaultStores;
    const manufacturerStores = new Set(["Electrolux", "Midea", "Consul", "Brastemp"]);
    const previousStores = STORE_OPTIONS.filter((store) => !manufacturerStores.has(store.label));
    if (defaultStores.length === previousStores.length && previousStores.every((store) => defaultStores.includes(store.label))) {
      defaultStores = defaultInterfacePreferences.defaultStores;
    }

    const currency = parsed.currency && validCurrencies.has(parsed.currency) ? parsed.currency : "BRL";
    const regionFormat = regionFormatOptions.find((option) => option.value === currency)!;
    return {
      currency,
      locale: regionFormat.locale,
      defaultStores: defaultStores.length ? defaultStores : defaultInterfacePreferences.defaultStores,
      logisticsPostalCode:
        typeof parsed.logisticsPostalCode === "string" && parsed.logisticsPostalCode.replace(/\D/g, "").length === 8
          ? parsed.logisticsPostalCode.replace(/\D/g, "").slice(0, 8)
          : defaultInterfacePreferences.logisticsPostalCode,
    };
  } catch {
    return defaultInterfacePreferences;
  }
};

export const saveInterfacePreferences = (preferences: InterfacePreferences) => {
  window.localStorage.setItem(INTERFACE_PREFERENCES_KEY, JSON.stringify(preferences));
  window.dispatchEvent(new CustomEvent(INTERFACE_PREFERENCES_EVENT, { detail: preferences }));
};

export const createCurrencyFormatter = (
  preferences: InterfacePreferences,
  minimumFractionDigits = 2,
  maximumFractionDigits = 2,
) =>
  new Intl.NumberFormat(preferences.locale, {
    style: "currency",
    currency: preferences.currency,
    minimumFractionDigits,
    maximumFractionDigits,
  });

export const createCompactFormatter = (preferences: InterfacePreferences) =>
  new Intl.NumberFormat(preferences.locale, {
    notation: "compact",
    maximumFractionDigits: 1,
  });
