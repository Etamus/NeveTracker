import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  BarChart3,
  CalendarClock,
  CheckCircle2,
  Database,
  Download,
  Grid2X2,
  ListChecks,
  LoaderCircle,
  LogOut,
  Moon,
  PackageSearch,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
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
    const storedRepeatEveryHours = Math.trunc(Number(parsed.repeatEveryHours) || 0);
    const repeatEveryHours = Math.min(12, Math.max(0, storedRepeatEveryHours));
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
  const [skuSelectorOpen, setSkuSelectorOpen] = useState(false);
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
  const [refreshingSku, setRefreshingSku] = useState<string | null>(null);
  const [refreshingAllSkus, setRefreshingAllSkus] = useState(false);
  const [selectedSearchSkus, setSelectedSearchSkus] = useState<string[]>([]);
  const [draftSearchSkus, setDraftSearchSkus] = useState<string[]>([]);
  const [skuSelectorFilter, setSkuSelectorFilter] = useState("");
  const [skuSelectorError, setSkuSelectorError] = useState("");
  const accountRef = useRef<HTMLDivElement>(null);
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
        setSkuSelectorOpen(false);
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

  const submitSearch = (event?: FormEvent) => {
    event?.preventDefault();
    if (loading || !productName.trim()) return;
    setActiveTab(0);
    setSidebarOpen(false);
    void handleSearch(
      selectedSearchSkus,
      selectedSearchSkus.length ? `${selectedSearchSkus.length} SKUs selecionados` : undefined,
    );
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
    void fetch(`${getApiBaseUrl()}/products/schedule`)
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!data) return;
        setScheduleSettings((currentSettings) => ({
          ...currentSettings,
          enabled: Boolean(data.enabled),
          time: typeof data.start_time === "string" ? data.start_time : currentSettings.time,
          endTime: typeof data.end_time === "string" ? data.end_time : currentSettings.endTime,
          days: Array.isArray(data.days) && data.days.length ? data.days : currentSettings.days,
          repeatEveryHours: Math.min(12, Math.max(0, Math.trunc(Number(data.repeat_every_hours) || 0))),
          forceRefresh: data.force_refresh !== false,
          lastRunKey: typeof data.last_run_key === "string" ? data.last_run_key : undefined,
        }));
      })
      .catch(() => undefined);
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

  const saveSchedule = async () => {
    setScheduleStatus("Salvando...");
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/schedule`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: scheduleSettings.enabled,
          start_time: scheduleSettings.time,
          end_time: scheduleSettings.endTime,
          days: scheduleSettings.days,
          repeat_every_hours: scheduleSettings.repeatEveryHours,
          force_refresh: scheduleSettings.forceRefresh,
          postal_code: interfacePreferences.logisticsPostalCode || null,
        }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setScheduleStatus("Programação salva.");
    } catch (error) {
      console.error("Erro ao salvar programação:", error);
      setScheduleStatus("Não foi possível salvar a programação.");
    }
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
        body: JSON.stringify({
          skus,
          force_refresh: forceExtraction,
          postal_code: interfacePreferences.logisticsPostalCode || null,
          scope: "all",
        }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        throw new Error(errorData?.detail || `HTTP ${response.status}`);
      }

      const data = (await response.json()) as SkuBatchResponse;
      setExtractionResults(data.items || []);
      await loadSkuStatus();
    } catch (error) {
      console.error("Erro ao extrair SKUs:", error);
      setExtractionError(error instanceof Error ? error.message : "Falha ao extrair SKUs.");
    } finally {
      setExtractionRunning(false);
    }
  };

  const refreshSingleSku = async (sku: string) => {
    if (refreshingSku) return;
    setRefreshingSku(sku);
    setExtractionError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/batch-search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          skus: [sku],
          force_refresh: true,
          postal_code: interfacePreferences.logisticsPostalCode || null,
          scope: "all",
        }),
      });
      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        throw new Error(errorData?.detail || `HTTP ${response.status}`);
      }
      const data = (await response.json()) as SkuBatchResponse;
      setExtractionResults(data.items || []);
      await loadSkuStatus();
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Falha ao atualizar o SKU.");
    } finally {
      setRefreshingSku(null);
    }
  };

  const refreshAllSkus = async () => {
    const skus = skuStatusItems.map((item) => item.sku).filter(Boolean);
    if (!skus.length || refreshingAllSkus) return;

    setRefreshingAllSkus(true);
    setExtractionError("");
    setExtractionResults([]);
    const results: SkuBatchResult[] = [];

    try {
      for (let index = 0; index < skus.length; index += 30) {
        const response = await fetch(`${getApiBaseUrl()}/products/batch-search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            skus: skus.slice(index, index + 30),
            force_refresh: true,
            postal_code: interfacePreferences.logisticsPostalCode || null,
            scope: "all",
          }),
        });
        if (!response.ok) {
          const errorData = await response.json().catch(() => null);
          throw new Error(errorData?.detail || `HTTP ${response.status}`);
        }
        const data = (await response.json()) as SkuBatchResponse;
        results.push(...(data.items || []));
      }

      setExtractionResults(results);
      await loadSkuStatus();
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Falha ao atualizar o banco local.");
    } finally {
      setRefreshingAllSkus(false);
    }
  };

  const openSkuSelector = () => {
    setDraftSearchSkus(selectedSearchSkus);
    setSkuSelectorFilter("");
    setSkuSelectorError("");
    setSkuSelectorOpen(true);
    void loadSkuStatus();
  };

  const toggleDraftSearchSku = (sku: string) => {
    setDraftSearchSkus((currentSkus) => {
      if (currentSkus.includes(sku)) {
        setSkuSelectorError("");
        return currentSkus.filter((item) => item !== sku);
      }
      if (currentSkus.length >= 30) {
        setSkuSelectorError("Selecione no máximo 30 SKUs por pesquisa.");
        return currentSkus;
      }
      setSkuSelectorError("");
      return [...currentSkus, sku];
    });
  };

  const searchSelectedSkus = () => {
    if (!draftSearchSkus.length || loading) {
      setSkuSelectorError("Selecione ao menos um SKU.");
      return;
    }

    const label = `${draftSearchSkus.length} ${draftSearchSkus.length === 1 ? "SKU selecionado" : "SKUs selecionados"}`;
    setSelectedSearchSkus(draftSearchSkus);
    setProductName(label);
    setSkuSelectorOpen(false);
    setActiveTab(0);
    setSidebarOpen(false);
    void handleSearch(draftSearchSkus, label);
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

  const visibleSkuSelectorItems = skuStatusItems.filter((item) =>
    item.sku.toLowerCase().includes(skuSelectorFilter.trim().toLowerCase()),
  );

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
            <button
              type="button"
              className={selectedSearchSkus.length ? "sku-select-trigger sku-select-trigger--active" : "sku-select-trigger"}
              onClick={openSkuSelector}
              aria-label="Selecionar SKUs do banco local"
              title="Selecionar vários SKUs"
            >
              <ListChecks size={17} />
              {selectedSearchSkus.length > 0 && <span>{selectedSearchSkus.length}</span>}
            </button>
            <input
              type="search"
              value={productName}
              onChange={(event) => {
                setSelectedSearchSkus([]);
                setProductName(event.target.value);
              }}
              placeholder="Buscar produto ou modelo..."
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

      {skuSelectorOpen && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={() => setSkuSelectorOpen(false)}>
          <section
            className="workspace-dialog workspace-dialog--sku-selector"
            role="dialog"
            aria-modal="true"
            aria-label="Selecionar SKUs"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="dialog-header">
              <div>
                <span className="dialog-icon"><ListChecks size={19} /></span>
                <h2>Selecionar SKUs</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setSkuSelectorOpen(false)} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <div className="sku-selector-toolbar">
              <Search size={16} />
              <input
                type="search"
                value={skuSelectorFilter}
                onChange={(event) => setSkuSelectorFilter(event.target.value)}
                placeholder="Filtrar SKUs do banco..."
                aria-label="Filtrar SKUs"
                autoFocus
              />
            </div>

            <div className="sku-selector-list">
              {skuStatusLoading && !skuStatusItems.length ? (
                <p className="sku-status-empty">Carregando...</p>
              ) : visibleSkuSelectorItems.length ? (
                visibleSkuSelectorItems.map((item) => {
                  const checked = draftSearchSkus.includes(item.sku);
                  const disabled = !checked && draftSearchSkus.length >= 30;
                  return (
                    <label key={item.sku} className={disabled ? "sku-selector-row sku-selector-row--disabled" : "sku-selector-row"}>
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={disabled}
                        onChange={() => toggleDraftSearchSku(item.sku)}
                      />
                      <span className="min-w-0 flex-1">
                        <strong>{item.sku}</strong>
                        <small>{item.offer_count} ofertas · {item.store_count} lojas · {formatDateTime(item.last_update)}</small>
                      </span>
                    </label>
                  );
                })
              ) : (
                <p className="sku-status-empty">Nenhum SKU encontrado.</p>
              )}
            </div>

            {skuSelectorError && <p className="auth-error">{skuSelectorError}</p>}

            <div className="sku-selector-footer">
              <span>{draftSearchSkus.length} de 30 selecionados</span>
              <div className="sku-selector-footer__actions">
                {draftSearchSkus.length > 0 && (
                  <button
                    type="button"
                    className="button-secondary"
                    onClick={() => {
                      setDraftSearchSkus([]);
                      setSkuSelectorError("");
                    }}
                  >
                    Desmarcar
                  </button>
                )}
                <button type="button" className="button-primary" onClick={searchSelectedSkus} disabled={!draftSearchSkus.length || loading}>
                  {loading ? <LoaderCircle size={15} className="animate-spin" /> : <Search size={15} />}
                  Pesquisar
                </button>
              </div>
            </div>
          </section>
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
                  <h3>Logística</h3>
                </div>
                <div className="settings-grid">
                  <label className="settings-field">
                    <span>CEP de referência</span>
                    <input
                      inputMode="numeric"
                      value={interfacePreferences.logisticsPostalCode}
                      onChange={(event) =>
                        updateInterfacePreferences((currentPreferences) => ({
                          ...currentPreferences,
                          logisticsPostalCode: event.target.value.replace(/\D/g, "").slice(0, 8),
                        }))
                      }
                    />
                  </label>
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
                <form onSubmit={runExtractions} className="extraction-form extraction-form--fill">
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
                      <span className="schedule-toggle__track" aria-hidden="true">
                        <span className="schedule-toggle__thumb" />
                      </span>
                      <span>{scheduleSettings.enabled ? "Ativo" : "Inativo"}</span>
                    </label>
                  </div>

                  <div className="schedule-grid">
                    <label className="schedule-field schedule-field--frequency">
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
                      <span className="schedule-frequency-input">
                        <input
                          type="number"
                          min="0"
                          max="12"
                          step="1"
                          value={scheduleSettings.repeatEveryHours}
                          onChange={(event) => {
                            const value = Math.trunc(Number(event.target.value) || 0);
                            setScheduleSettings((currentSettings) => ({
                              ...currentSettings,
                              repeatEveryHours: Math.min(12, Math.max(0, value)),
                            }));
                          }}
                        />
                        <span>horas</span>
                      </span>
                      <small>{scheduleSettings.repeatEveryHours === 0 ? "Não repetir" : `Repetir a cada ${scheduleSettings.repeatEveryHours}h`}</small>
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

                  {scheduleStatus && <p className="schedule-status">{scheduleStatus}</p>}

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
                    <button type="button" className="button-primary" onClick={() => void saveSchedule()}>
                      <CheckCircle2 size={15} />
                      Salvar
                    </button>
                  </div>

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
                    <button
                      type="button"
                      className="button-secondary sku-refresh-all"
                      onClick={() => void refreshAllSkus()}
                      disabled={refreshingAllSkus || !skuStatusItems.length}
                    >
                      {refreshingAllSkus && <LoaderCircle size={15} className="animate-spin" />}
                      Atualizar
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
                          <button
                            type="button"
                            className="table-action"
                            onClick={() => void refreshSingleSku(item.sku)}
                            disabled={Boolean(refreshingSku)}
                            aria-label={`Atualizar ${item.sku}`}
                            title="Atualizar SKU"
                          >
                            {refreshingSku === item.sku
                              ? <LoaderCircle size={15} className="animate-spin" />
                              : <RefreshCw size={15} />}
                          </button>
                        </div>
                      ))
                    ) : (
                      <p className="sku-status-empty">Nenhum SKU no banco local.</p>
                    )}
                  </div>
                </div>
                {extractionError && <p className="auth-error">{extractionError}</p>}
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
};
