import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  BarChart3,
  CalendarClock,
  CheckCircle2,
  Database,
  Download,
  Grid2X2,
  LoaderCircle,
  LogOut,
  Moon,
  PackageSearch,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  Settings,
  Sun,
  UserRound,
  X,
} from "lucide-react";
import SlidingTabs from "../SlidingTabs";
import type {
  SkuBatchResponse,
  SkuBatchResult,
  SkuStatusItem,
  SkuStatusResponse,
} from "../../models/product.model";
import useHeroSection, { getApiBaseUrl } from "./viewModel";
import logoUrl from "../../static/logo.png";
import logoWhiteUrl from "../../static/logowhite.png";
import {
  currencyOptions,
  getInterfacePreferences,
  localeOptions,
  saveInterfacePreferences,
  STORE_OPTIONS,
  type InterfacePreferences,
} from "../../src/preferences";

const AUTH_USERS = [{ username: "DEUSM2", password: "1234" }];
const AUTH_STORAGE_KEY = "neveprice-auth-user";
const THEME_STORAGE_KEY = "neveprice-theme";
const SCHEDULE_STORAGE_KEY = "neveprice-extraction-schedule";

const navigation = [
  { label: "Ofertas", description: "Comparação de preços", icon: PackageSearch },
  { label: "Catálogo", description: "Visualização em grade", icon: Grid2X2 },
  { label: "Analytics", description: "Leitura de mercado", icon: BarChart3 },
];

type ThemeMode = "light" | "dark";
type ExtractionTab = "extract" | "database" | "schedule";

type ScheduledExtractionSettings = {
  enabled: boolean;
  time: string;
  endTime: string;
  days: number[];
  repeatEveryHours: number;
  forceRefresh: boolean;
  lastRunKey?: string;
};

const weekDays = [
  { value: 1, label: "Seg" },
  { value: 2, label: "Ter" },
  { value: 3, label: "Qua" },
  { value: 4, label: "Qui" },
  { value: 5, label: "Sex" },
  { value: 6, label: "Sáb" },
  { value: 0, label: "Dom" },
];

const frequencyOptions = [0, 1, 2, 3, 4, 6, 8, 12];

const defaultScheduleSettings: ScheduledExtractionSettings = {
  enabled: false,
  time: "09:00",
  endTime: "18:00",
  days: [1],
  repeatEveryHours: 0,
  forceRefresh: true,
};

const getInitialTheme = (): ThemeMode => {
  const storedTheme = window.localStorage.getItem(THEME_STORAGE_KEY);
  if (storedTheme === "light" || storedTheme === "dark") return storedTheme;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
};

const getInitialScheduleSettings = (): ScheduledExtractionSettings => {
  try {
    const storedSettings = window.localStorage.getItem(SCHEDULE_STORAGE_KEY);
    if (!storedSettings) return defaultScheduleSettings;

    const parsed = JSON.parse(storedSettings) as Partial<ScheduledExtractionSettings>;
    const storedRepeatEveryHours = Number(parsed.repeatEveryHours) || 0;
    const repeatEveryHours = frequencyOptions.includes(storedRepeatEveryHours) ? storedRepeatEveryHours : 0;
    const days = Array.isArray(parsed.days)
      ? parsed.days.filter((day) => Number.isInteger(day) && day >= 0 && day <= 6)
      : defaultScheduleSettings.days;

    return {
      ...defaultScheduleSettings,
      ...parsed,
      enabled: Boolean(parsed.enabled),
      time: typeof parsed.time === "string" && /^\d{2}:\d{2}$/.test(parsed.time) ? parsed.time : "09:00",
      endTime: typeof parsed.endTime === "string" && /^\d{2}:\d{2}$/.test(parsed.endTime) ? parsed.endTime : "18:00",
      days: days.length ? days : defaultScheduleSettings.days,
      repeatEveryHours,
      forceRefresh: parsed.forceRefresh !== false,
    };
  } catch {
    return defaultScheduleSettings;
  }
};

const parseSkuInput = (value: string) => {
  const matches = value.toUpperCase().match(/\bW\d{6,}\b|\b\d{8,}\b/g) || [];
  const fallback = value
    .toUpperCase()
    .split(/[\s,;]+/)
    .map((item) => item.trim())
    .filter((item) => item.length >= 2);
  return [...new Set(matches.length ? matches : fallback)];
};

const formatDateTime = (value?: string | null) => {
  if (!value) return "Nunca";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Nunca";
  return new Intl.DateTimeFormat("pt-BR", {
    day: "2-digit",
    month: "short",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
};

export const HeroSection = () => {
  const [activeTab, setActiveTab] = useState(0);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [extractionsOpen, setExtractionsOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [loginUser, setLoginUser] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [theme, setTheme] = useState<ThemeMode>(getInitialTheme);
  const [extractionTab, setExtractionTab] = useState<ExtractionTab>("extract");
  const [scheduleSettings, setScheduleSettings] = useState<ScheduledExtractionSettings>(getInitialScheduleSettings);
  const [scheduleStatus, setScheduleStatus] = useState("");
  const [interfacePreferences, setInterfacePreferences] = useState<InterfacePreferences>(getInterfacePreferences);
  const [settingsStatus, setSettingsStatus] = useState("");
  const [skuInput, setSkuInput] = useState("");
  const [forceExtraction, setForceExtraction] = useState(true);
  const [skuStatusItems, setSkuStatusItems] = useState<SkuStatusItem[]>([]);
  const [skuStatusLoading, setSkuStatusLoading] = useState(false);
  const [extractionRunning, setExtractionRunning] = useState(false);
  const [extractionResults, setExtractionResults] = useState<SkuBatchResult[]>([]);
  const [extractionError, setExtractionError] = useState("");
  const accountRef = useRef<HTMLDivElement>(null);
  const scheduleRunningRef = useRef(false);
  const [currentUser, setCurrentUser] = useState<string | null>(() => {
    const storedUser = window.localStorage.getItem(AUTH_STORAGE_KEY);
    return AUTH_USERS.find((user) => user.username === storedUser)?.username || null;
  });
  const {
    productName,
    products,
    comparisonRows,
    loading,
    lastQuery,
    setProductName,
    handleSearch,
  } = useHeroSection();

  useEffect(() => {
    const closeMenus = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setLoginOpen(false);
        setSettingsOpen(false);
        setExtractionsOpen(false);
        setAccountOpen(false);
        setSidebarOpen(false);
      }
    };

    window.addEventListener("keydown", closeMenus);
    return () => window.removeEventListener("keydown", closeMenus);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  useEffect(() => {
    window.localStorage.setItem(SCHEDULE_STORAGE_KEY, JSON.stringify(scheduleSettings));
  }, [scheduleSettings]);

  useEffect(() => {
    const closeAccountMenu = (event: PointerEvent) => {
      if (!accountRef.current?.contains(event.target as Node)) {
        setAccountOpen(false);
      }
    };

    document.addEventListener("pointerdown", closeAccountMenu, true);
    return () => document.removeEventListener("pointerdown", closeAccountMenu, true);
  }, []);

  useEffect(() => {
    const runScheduledExtraction = async () => {
      if (!scheduleSettings.enabled || scheduleRunningRef.current || !scheduleSettings.days.length) return;

      const now = new Date();
      const [scheduledHour, scheduledMinute] = scheduleSettings.time.split(":").map(Number);
      const [endHour, endMinute] = scheduleSettings.endTime.split(":").map(Number);
      const nowMinutes = now.getHours() * 60 + now.getMinutes();
      const scheduledMinutes = scheduledHour * 60 + scheduledMinute;
      const endMinutes = Math.max(endHour * 60 + endMinute, scheduledMinutes);
      const intervalMinutes = scheduleSettings.repeatEveryHours * 60;

      if (
        nowMinutes < scheduledMinutes ||
        nowMinutes > endMinutes ||
        !scheduleSettings.days.includes(now.getDay())
      ) {
        return;
      }

      const elapsedMinutes = nowMinutes - scheduledMinutes;
      const shouldRunOnce = scheduleSettings.repeatEveryHours === 0 && elapsedMinutes === 0;
      const shouldRunRepeated = intervalMinutes > 0 && elapsedMinutes % intervalMinutes === 0;
      const runSlot = intervalMinutes > 0 ? elapsedMinutes / intervalMinutes : 0;
      const runKey = `${now.getFullYear()}-${now.getMonth()}-${now.getDate()}-${scheduleSettings.time}-${scheduleSettings.endTime}-${scheduleSettings.repeatEveryHours}-${runSlot}`;

      if ((!shouldRunOnce && !shouldRunRepeated) || scheduleSettings.lastRunKey === runKey) return;

      scheduleRunningRef.current = true;
      setScheduleStatus("Executando atualização programada...");

      try {
        const statusResponse = await fetch(`${getApiBaseUrl()}/products/skus/status`);
        if (!statusResponse.ok) throw new Error(`HTTP ${statusResponse.status}`);

        const statusData = (await statusResponse.json()) as SkuStatusResponse;
        const skus = [
          ...new Set((statusData.items || []).map((item) => item.sku).filter((sku): sku is string => Boolean(sku))),
        ];

        if (skus.length) {
          const extractionResponse = await fetch(`${getApiBaseUrl()}/products/batch-search`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ skus, force_refresh: scheduleSettings.forceRefresh }),
          });

          if (!extractionResponse.ok) throw new Error(`HTTP ${extractionResponse.status}`);

          const query = `?q=${encodeURIComponent(skus.join(","))}`;
          const updatedStatusResponse = await fetch(`${getApiBaseUrl()}/products/skus/status${query}`);
          if (updatedStatusResponse.ok) {
            const updatedStatusData = (await updatedStatusResponse.json()) as SkuStatusResponse;
            setSkuStatusItems(updatedStatusData.items || []);
          }

          setScheduleStatus(`Atualização programada concluída para ${skus.length} SKUs.`);
        } else {
          setScheduleStatus("Nenhum SKU encontrado no banco local.");
        }

        setScheduleSettings((currentSettings) => ({ ...currentSettings, lastRunKey: runKey }));
      } catch (error) {
        console.error("Erro na extração programada:", error);
        setScheduleStatus("Falha na atualização programada.");
      } finally {
        scheduleRunningRef.current = false;
      }
    };

    const intervalId = window.setInterval(() => void runScheduledExtraction(), 30000);
    void runScheduledExtraction();
    return () => window.clearInterval(intervalId);
  }, [scheduleSettings]);

  const submitSearch = (event?: FormEvent) => {
    event?.preventDefault();
    if (loading || !productName.trim()) return;
    setActiveTab(0);
    setSidebarOpen(false);
    handleSearch();
  };

  const openLogin = () => {
    setAccountOpen(false);
    setAuthError("");
    setLoginOpen(true);
  };

  const closeLogin = () => {
    setLoginOpen(false);
    setAuthError("");
    setLoginPassword("");
  };

  const handleLogin = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedUser = loginUser.trim().toUpperCase();
    const user = AUTH_USERS.find(
      (candidate) =>
        candidate.username === normalizedUser &&
        candidate.password === loginPassword,
    );

    if (!user) {
      setAuthError("Login ou senha inválidos.");
      return;
    }

    setCurrentUser(user.username);
    window.localStorage.setItem(AUTH_STORAGE_KEY, user.username);
    setLoginUser("");
    setLoginPassword("");
    setAuthError("");
    setLoginOpen(false);
  };

  const handleLogout = () => {
    setCurrentUser(null);
    setAccountOpen(false);
    window.localStorage.removeItem(AUTH_STORAGE_KEY);
  };

  const toggleTheme = () => {
    setTheme((currentTheme) => (currentTheme === "dark" ? "light" : "dark"));
  };

  const updateInterfacePreferences = (updater: (currentPreferences: InterfacePreferences) => InterfacePreferences) => {
    setInterfacePreferences((currentPreferences) => {
      const nextPreferences = updater(currentPreferences);
      saveInterfacePreferences(nextPreferences);
      setSettingsStatus("Configuração salva.");
      return nextPreferences;
    });
  };

  const toggleDefaultStore = (store: string) => {
    updateInterfacePreferences((currentPreferences) => {
      const defaultStores = currentPreferences.defaultStores.includes(store)
        ? currentPreferences.defaultStores.length > 1
          ? currentPreferences.defaultStores.filter((selectedStore) => selectedStore !== store)
          : currentPreferences.defaultStores
        : [...currentPreferences.defaultStores, store];

      return { ...currentPreferences, defaultStores };
    });
  };

  const loadSkuStatus = async (skus?: string[]) => {
    setSkuStatusLoading(true);
    const query = skus?.length ? `?q=${encodeURIComponent(skus.join(","))}` : "";

    try {
      const response = await fetch(`${getApiBaseUrl()}/products/skus/status${query}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = (await response.json()) as SkuStatusResponse;
      setSkuStatusItems(data.items || []);
    } catch (error) {
      console.error("Erro ao carregar SKUs:", error);
      setExtractionError("Não foi possível carregar os SKUs do banco.");
    } finally {
      setSkuStatusLoading(false);
    }
  };

  const openSettings = () => {
    setAccountOpen(false);
    setSettingsOpen(true);
  };

  const openExtractions = () => {
    setAccountOpen(false);
    setExtractionError("");
    setExtractionResults([]);
    setExtractionTab("extract");
    setExtractionsOpen(true);
    void loadSkuStatus();
  };

  const toggleScheduleDay = (day: number) => {
    setScheduleSettings((currentSettings) => {
      const days = currentSettings.days.includes(day)
        ? currentSettings.days.length > 1
          ? currentSettings.days.filter((selectedDay) => selectedDay !== day)
          : currentSettings.days
        : [...currentSettings.days, day];

      return { ...currentSettings, days };
    });
  };

  const saveSchedule = () => {
    setScheduleStatus("Programação salva.");
  };

  const runExtractions = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const skus = parseSkuInput(skuInput);

    if (!skus.length) {
      setExtractionError("Informe ao menos um SKU.");
      return;
    }

    setExtractionRunning(true);
    setExtractionError("");
    setExtractionResults([]);

    try {
      const response = await fetch(`${getApiBaseUrl()}/products/batch-search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skus, force_refresh: forceExtraction }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        throw new Error(errorData?.detail || `HTTP ${response.status}`);
      }

      const data = (await response.json()) as SkuBatchResponse;
      setExtractionResults(data.items || []);
      await loadSkuStatus(skus);
    } catch (error) {
      console.error("Erro ao extrair SKUs:", error);
      setExtractionError(error instanceof Error ? error.message : "Falha ao extrair SKUs.");
    } finally {
      setExtractionRunning(false);
    }
  };

  const switchTab = (index: number) => {
    setActiveTab(index);
    setSidebarOpen(false);
  };

  const toggleSidebar = () => {
    if (window.matchMedia("(min-width: 1024px)").matches) {
      setSidebarCollapsed((collapsed) => !collapsed);
      return;
    }

    setSidebarOpen((open) => !open);
  };

  return (
    <div className={`app-shell ${sidebarCollapsed ? "app-shell--sidebar-collapsed" : ""}`}>
      {sidebarOpen && (
        <button
          type="button"
          className="fixed inset-0 z-40 bg-slate-950/30 backdrop-blur-[2px] lg:hidden"
          aria-label="Fechar navegação"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside className={`app-sidebar ${sidebarOpen ? "app-sidebar--open" : ""}`}>
        <div className="flex h-16 items-center justify-start border-b border-[var(--border)] px-5">
          <img src={theme === "dark" ? logoWhiteUrl : logoUrl} alt="NevePrice" className="h-8 w-auto" />
        </div>

        <div className="flex min-h-0 flex-1 flex-col px-3 py-5">
          <p className="sidebar-label">Workspace</p>
          <nav className="mt-2 space-y-1" aria-label="Navegação principal">
            {navigation.map((item, index) => {
              const Icon = item.icon;
              const active = activeTab === index;

              return (
                <button
                  key={item.label}
                  type="button"
                  onClick={() => switchTab(index)}
                  className={`sidebar-item ${active ? "sidebar-item--active" : ""}`}
                >
                  <span className="sidebar-item__icon">
                    <Icon size={18} strokeWidth={2} />
                  </span>
                  <span className="min-w-0 text-left">
                    <span className="block text-[13px] font-semibold leading-5">{item.label}</span>
                    <span className="block truncate text-xs font-medium text-[var(--muted)]">
                      {item.description}
                    </span>
                  </span>
                </button>
              );
            })}
          </nav>

          <div ref={accountRef} className="account-menu-root mt-auto border-t border-[var(--border)] pt-4">
            {currentUser && accountOpen && (
              <div className="account-popover">
                <button type="button" className="account-popover__item" onClick={openExtractions}>
                  <Download size={15} />
                  Extrações
                </button>
                <button type="button" className="account-popover__item" onClick={openSettings}>
                  <Settings size={15} />
                  Configurações
                </button>
                <button
                  type="button"
                  onClick={handleLogout}
                  className="account-popover__item account-popover__item--danger"
                >
                  <LogOut size={15} />
                  Encerrar sessão
                </button>
              </div>
            )}
            <button
              type="button"
              onClick={currentUser ? () => setAccountOpen((open) => !open) : openLogin}
              className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left transition-colors hover:bg-[var(--surface-subtle)]"
            >
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-slate-900 text-xs font-bold text-white">
                {currentUser ? currentUser.slice(0, 2) : <UserRound size={17} />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-semibold text-[var(--foreground)]">
                  {currentUser || "Entrar na conta"}
                </span>
              </span>
            </button>
          </div>
        </div>
      </aside>

      <section className="app-main">
        <header className="app-header">
          <div className="flex min-w-0 items-center gap-2">
            <button
              type="button"
              aria-label="Alternar navegação"
              className="icon-button"
              onClick={toggleSidebar}
            >
              <span className="flex lg:hidden">
                {sidebarOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
              </span>
              <span className="hidden lg:flex">
                {sidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
              </span>
            </button>
            <div className="hidden items-center gap-2 text-xs md:flex">
              <span className="font-medium text-[var(--muted)]">Workspace</span>
              <span className="text-[var(--border-strong)]">/</span>
              <span className="font-semibold text-[var(--foreground)]">{navigation[activeTab].label}</span>
            </div>
          </div>

          <form onSubmit={submitSearch} className="header-search">
            <input
              type="search"
              value={productName}
              onChange={(event) => setProductName(event.target.value)}
              placeholder="Buscar produto, modelo ou SKU..."
              disabled={loading}
              aria-label="Buscar produtos"
            />
            <button
              type="submit"
              disabled={loading || !productName.trim()}
              className="search-submit"
              aria-label="Buscar"
            >
              {loading ? <span className="button-spinner" /> : <Search size={17} />}
            </button>
          </form>

          <div className="header-actions">
            <button
              type="button"
              className="icon-button"
              aria-label={theme === "dark" ? "Usar tema claro" : "Usar tema escuro"}
              title={theme === "dark" ? "Usar tema claro" : "Usar tema escuro"}
              onClick={toggleTheme}
            >
              {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
            </button>
          </div>
        </header>

        <main className="app-content">
          <SlidingTabs
            activeTab={activeTab}
            onTabChange={setActiveTab}
            loading={loading}
            products={products}
            comparisonRows={comparisonRows}
            lastQuery={lastQuery}
          />
        </main>
      </section>

      {loginOpen && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={closeLogin}>
          <form
            onSubmit={handleLogin}
            className="auth-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="Acesso"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-slate-900 text-white">
                  <UserRound size={19} />
                </span>
                <h2 className="mt-5 text-lg font-semibold text-[var(--foreground)]">Acesso</h2>
                <p className="mt-1 text-sm text-[var(--muted)]">Use suas credenciais para continuar.</p>
              </div>
              <button type="button" className="icon-button" onClick={closeLogin} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <label className="form-field mt-6">
              <span>Login</span>
              <input
                type="text"
                value={loginUser}
                onChange={(event) => {
                  setLoginUser(event.target.value);
                  setAuthError("");
                }}
                autoComplete="username"
                autoFocus
              />
            </label>
            <label className="form-field mt-4">
              <span>Senha</span>
              <input
                type="password"
                value={loginPassword}
                onChange={(event) => {
                  setLoginPassword(event.target.value);
                  setAuthError("");
                }}
                autoComplete="current-password"
              />
            </label>

            {authError && <p className="auth-error">{authError}</p>}

            <div className="mt-6 flex justify-end gap-2">
              <button type="submit" className="button-primary">Entrar</button>
            </div>
          </form>
        </div>
      )}

      {settingsOpen && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={() => setSettingsOpen(false)}>
          <section
            className="workspace-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="Configurações"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="dialog-header">
              <div>
                <span className="dialog-icon"><Settings size={19} /></span>
                <h2>Configurações</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setSettingsOpen(false)} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <div className="settings-panel">
              <section className="settings-section">
                <div className="settings-section__head">
                  <h3>Formato</h3>
                </div>
                <div className="settings-grid">
                  <label className="settings-field">
                    <span>Moeda</span>
                    <select
                      value={interfacePreferences.currency}
                      onChange={(event) =>
                        updateInterfacePreferences((currentPreferences) => ({
                          ...currentPreferences,
                          currency: event.target.value,
                        }))
                      }
                    >
                      {currencyOptions.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label className="settings-field">
                    <span>Formato numérico</span>
                    <select
                      value={interfacePreferences.locale}
                      onChange={(event) =>
                        updateInterfacePreferences((currentPreferences) => ({
                          ...currentPreferences,
                          locale: event.target.value,
                        }))
                      }
                    >
                      {localeOptions.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
              </section>

              <section className="settings-section">
                <div className="settings-section__head">
                  <h3>Lojas padrão</h3>
                  <div className="settings-inline-actions">
                    <button
                      type="button"
                      onClick={() =>
                        updateInterfacePreferences((currentPreferences) => ({
                          ...currentPreferences,
                          defaultStores: STORE_OPTIONS.map((store) => store.label),
                        }))
                      }
                    >
                      Todas
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        updateInterfacePreferences((currentPreferences) => ({
                          ...currentPreferences,
                          defaultStores: [STORE_OPTIONS[0].label],
                        }))
                      }
                    >
                      Mínimo
                    </button>
                  </div>
                </div>

                <div className="settings-store-grid">
                  {STORE_OPTIONS.map((store) => {
                    const active = interfacePreferences.defaultStores.includes(store.label);

                    return (
                      <button
                        key={store.label}
                        type="button"
                        className={active ? "settings-store settings-store--active" : "settings-store"}
                        onClick={() => toggleDefaultStore(store.label)}
                      >
                        <span>{store.label}</span>
                        {active && <CheckCircle2 size={14} />}
                      </button>
                    );
                  })}
                </div>
              </section>

              {settingsStatus && <p className="settings-status">{settingsStatus}</p>}
            </div>
          </section>
        </div>
      )}

      {extractionsOpen && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={() => setExtractionsOpen(false)}>
          <section
            className="workspace-dialog workspace-dialog--wide"
            role="dialog"
            aria-modal="true"
            aria-label="Extrações"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="dialog-header">
              <div>
                <span className="dialog-icon"><Database size={19} /></span>
                <h2>Extrações</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setExtractionsOpen(false)} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <div className="extraction-tabs" role="tablist" aria-label="Áreas de extração">
              <button
                type="button"
                role="tab"
                aria-selected={extractionTab === "extract"}
                className={extractionTab === "extract" ? "extraction-tab extraction-tab--active" : "extraction-tab"}
                onClick={() => setExtractionTab("extract")}
              >
                <Download size={15} />
                Extração
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={extractionTab === "schedule"}
                className={extractionTab === "schedule" ? "extraction-tab extraction-tab--active" : "extraction-tab"}
                onClick={() => setExtractionTab("schedule")}
              >
                <CalendarClock size={15} />
                Programado
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={extractionTab === "database"}
                className={extractionTab === "database" ? "extraction-tab extraction-tab--active" : "extraction-tab"}
                onClick={() => setExtractionTab("database")}
              >
                <Database size={15} />
                Banco local
              </button>
            </div>

            {extractionTab === "extract" ? (
              <div className="extraction-panel" role="tabpanel">
                <form onSubmit={runExtractions} className="extraction-form">
                  <label className="textarea-field">
                    <span>SKUs</span>
                    <textarea
                      value={skuInput}
                      onChange={(event) => {
                        setSkuInput(event.target.value);
                        setExtractionError("");
                      }}
                    />
                  </label>

                  <div className="extraction-actions">
                    <label className="check-field">
                      <input
                        type="checkbox"
                        checked={forceExtraction}
                        onChange={(event) => setForceExtraction(event.target.checked)}
                      />
                      <span>Atualizar via APIs</span>
                    </label>
                    <button type="submit" className="button-primary" disabled={extractionRunning}>
                      {extractionRunning ? <LoaderCircle size={15} className="animate-spin" /> : <Download size={15} />}
                      Extrair
                    </button>
                  </div>
                </form>

                {extractionError && <p className="auth-error">{extractionError}</p>}

                {extractionResults.length > 0 && (
                  <div className="extraction-results">
                    {extractionResults.map((item) => (
                      <div key={item.sku} className="extraction-result-row">
                        <span className={`status-dot status-dot--${item.status}`} />
                        <div className="min-w-0 flex-1">
                          <strong>{item.sku}</strong>
                          <small>{item.message}</small>
                        </div>
                        <span>{item.stores_count} lojas</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ) : extractionTab === "schedule" ? (
              <div className="extraction-panel" role="tabpanel">
                <div className="schedule-card">
                  <div className="schedule-card__head">
                    <div>
                      <h3>Atualização programada</h3>
                      <p>{skuStatusItems.length} SKUs do banco local</p>
                    </div>
                    <label className="schedule-toggle">
                      <input
                        type="checkbox"
                        checked={scheduleSettings.enabled}
                        onChange={(event) =>
                          setScheduleSettings((currentSettings) => ({
                            ...currentSettings,
                            enabled: event.target.checked,
                          }))
                        }
                      />
                      <span>{scheduleSettings.enabled ? "Ativo" : "Inativo"}</span>
                    </label>
                  </div>

                  <div className="schedule-grid">
                    <label className="schedule-field">
                      <span>Horário inicial</span>
                      <input
                        type="time"
                        value={scheduleSettings.time}
                        onChange={(event) =>
                          setScheduleSettings((currentSettings) => ({
                            ...currentSettings,
                            time: event.target.value,
                          }))
                        }
                      />
                    </label>

                    <label className="schedule-field">
                      <span>Horário final</span>
                      <input
                        type="time"
                        value={scheduleSettings.endTime}
                        disabled={scheduleSettings.repeatEveryHours === 0}
                        onChange={(event) =>
                          setScheduleSettings((currentSettings) => ({
                            ...currentSettings,
                            endTime: event.target.value,
                          }))
                        }
                      />
                    </label>

                    <label className="schedule-field">
                      <span>Frequência</span>
                      <select
                        value={scheduleSettings.repeatEveryHours}
                        onChange={(event) =>
                          setScheduleSettings((currentSettings) => ({
                            ...currentSettings,
                            repeatEveryHours: Number(event.target.value) || 0,
                          }))
                        }
                      >
                        {frequencyOptions.map((hour) => (
                          <option key={hour} value={hour}>
                            {hour === 0 ? "Não repetir" : `A cada ${hour} ${hour === 1 ? "hora" : "horas"}`}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>

                  <div className="schedule-section">
                    <span>Dias</span>
                    <div className="schedule-days">
                      {weekDays.map((day) => {
                        const active = scheduleSettings.days.includes(day.value);

                        return (
                          <button
                            key={day.value}
                            type="button"
                            className={active ? "schedule-day schedule-day--active" : "schedule-day"}
                            onClick={() => toggleScheduleDay(day.value)}
                          >
                            {day.label}
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  <div className="schedule-row">
                    <label className="check-field">
                      <input
                        type="checkbox"
                        checked={scheduleSettings.forceRefresh}
                        onChange={(event) =>
                          setScheduleSettings((currentSettings) => ({
                            ...currentSettings,
                            forceRefresh: event.target.checked,
                          }))
                        }
                      />
                      <span>Atualizar via APIs</span>
                    </label>
                    <button type="button" className="button-primary" onClick={saveSchedule}>
                      <CheckCircle2 size={15} />
                      Salvar
                    </button>
                  </div>

                  <div className="schedule-summary">
                    <span>{scheduleSettings.days.length} dias selecionados</span>
                    <strong>
                      {scheduleSettings.repeatEveryHours === 0
                        ? `${scheduleSettings.time} · uma vez`
                        : `${scheduleSettings.time}-${scheduleSettings.endTime} · ${scheduleSettings.repeatEveryHours}h`}
                    </strong>
                  </div>

                  {scheduleStatus && <p className="schedule-status">{scheduleStatus}</p>}
                </div>
              </div>
            ) : (
              <div className="extraction-panel" role="tabpanel">
                <div className="sku-status-card">
                  <div className="sku-status-card__header">
                    <div>
                      <h3>Banco local</h3>
                      <p>{skuStatusItems.length} SKUs acompanhados</p>
                    </div>
                    <button type="button" className="table-action" onClick={() => void loadSkuStatus()} aria-label="Atualizar SKUs">
                      {skuStatusLoading ? <LoaderCircle size={16} className="animate-spin" /> : <CheckCircle2 size={16} />}
                    </button>
                  </div>
                  <div className="sku-status-list">
                    {skuStatusLoading && !skuStatusItems.length ? (
                      <p className="sku-status-empty">Carregando...</p>
                    ) : skuStatusItems.length ? (
                      skuStatusItems.map((item) => (
                        <div key={item.sku} className="sku-status-row">
                          <strong>{item.sku}</strong>
                          <span>{item.offer_count} ofertas</span>
                          <span>{item.store_count} lojas</span>
                          <time>{formatDateTime(item.last_update)}</time>
                        </div>
                      ))
                    ) : (
                      <p className="sku-status-empty">Nenhum SKU no banco local.</p>
                    )}
                  </div>
                </div>
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
};
