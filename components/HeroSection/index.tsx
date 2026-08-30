import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  BarChart3,
  CalendarClock,
  CheckCircle2,
  ChevronDown,
  Database,
  Download,
  Eye,
  EyeOff,
  ExternalLink,
  FileDown,
  Grid2X2,
  KeyRound,
  ListChecks,
  LoaderCircle,
  LogOut,
  PackageSearch,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  Trash2,
  Upload,
  UserPlus,
  UserRound,
  X,
} from "lucide-react";
import SlidingTabs from "../SlidingTabs";
import type {
  ActiveExtractionJobResponse,
  AuthLoginResponse,
  AuthUser,
  AuthUsersResponse,
  ExtractionJob,
  IntegrationProviderSettings,
  IntegrationSettingsResponse,
  SkuBatchResult,
  SkuImportResponse,
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

const AUTH_TOKEN_STORAGE_KEY = "nevetracker-auth-token";
const THEME_STORAGE_KEY = "nevetracker-theme";
const SCHEDULE_STORAGE_KEY = "nevetracker-extraction-schedule";
const EXTRACTION_JOB_STORAGE_KEY = "nevetracker-active-extraction-job";
const LEGACY_STORAGE_PREFIXES = [
  ["neve", "track"].join(""),
  ["neve", "price"].join(""),
];
const legacyStorageKeys = (suffix: string) =>
  LEGACY_STORAGE_PREFIXES.map((prefix) => `${prefix}-${suffix}`);

const readMigratedStorageValue = (key: string, previousKeys: string[]) => {
  const currentValue = window.localStorage.getItem(key);
  if (currentValue !== null) return currentValue;
  for (const previousKey of previousKeys) {
    const previousValue = window.localStorage.getItem(previousKey);
    if (previousValue === null) continue;
    window.localStorage.setItem(key, previousValue);
    window.localStorage.removeItem(previousKey);
    return previousValue;
  }
  return null;
};

const navigation = [
  { label: "Ofertas", description: "Comparação de preços", icon: PackageSearch },
  { label: "Catálogo", description: "Visualização em grade", icon: Grid2X2 },
  { label: "Analytics", description: "Leitura de mercado", icon: BarChart3 },
];

type ThemeMode = "light" | "dark";
type ExtractionTab = "extract" | "database" | "schedule";

const TERMINAL_EXTRACTION_STATUSES = new Set([
  "completed",
  "completed_with_errors",
  "cancelled",
  "failed",
]);

const isExtractionJobActive = (job?: ExtractionJob | null) =>
  Boolean(job && !TERMINAL_EXTRACTION_STATUSES.has(job.status));

const extractionJobTitle = (job: ExtractionJob) => {
  if (job.cancel_requested && isExtractionJobActive(job)) return "Cancelando fila";
  if (job.status === "queued") return "Fila aguardando processamento";
  if (job.status === "running") return "Extração em andamento";
  if (job.status === "completed") return "Extração concluída";
  if (job.status === "completed_with_errors") return "Concluída com ocorrências";
  if (job.status === "cancelled") return "Fila cancelada";
  return "Falha na extração";
};

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

const weekDayNames = new Map([
  [1, "segunda-feira"],
  [2, "terça-feira"],
  [3, "quarta-feira"],
  [4, "quinta-feira"],
  [5, "sexta-feira"],
  [6, "sábado"],
  [0, "domingo"],
]);

const formatScheduleDays = (days: number[]) => {
  const orderedDays = weekDays.map((day) => day.value).filter((day) => days.includes(day));
  if (orderedDays.length === 7) return "Todos os dias";
  if ([1, 2, 3, 4, 5].every((day) => orderedDays.includes(day)) && orderedDays.length === 5) {
    return "Segunda a sexta";
  }
  if ([6, 0].every((day) => orderedDays.includes(day)) && orderedDays.length === 2) {
    return "Fins de semana";
  }

  const labels = orderedDays.map((day) => weekDayNames.get(day) || "");
  if (labels.length === 1) return `Toda ${labels[0]}`;
  const lastLabel = labels.pop();
  const joinedLabels = `${labels.join(", ")} e ${lastLabel}`;
  return joinedLabels.charAt(0).toUpperCase() + joinedLabels.slice(1);
};

const getScheduleSummary = (settings: ScheduledExtractionSettings) => {
  const repeats = settings.repeatEveryHours > 0;
  return {
    primary: repeats ? `${settings.time} às ${settings.endTime}` : `Às ${settings.time}`,
    secondary: repeats
      ? `A cada ${settings.repeatEveryHours} ${settings.repeatEveryHours === 1 ? "hora" : "horas"} · ${formatScheduleDays(settings.days)}`
      : `Uma vez ao dia · ${formatScheduleDays(settings.days)}`,
  };
};

const defaultScheduleSettings: ScheduledExtractionSettings = {
  enabled: false,
  time: "09:00",
  endTime: "18:00",
  days: [1],
  repeatEveryHours: 0,
  forceRefresh: true,
};

const getInitialTheme = (): ThemeMode => {
  const storedTheme = readMigratedStorageValue(THEME_STORAGE_KEY, legacyStorageKeys("theme"));
  const initialTheme = storedTheme === "light" || storedTheme === "dark"
    ? storedTheme
    : window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";

  document.documentElement.dataset.theme = initialTheme;
  window.localStorage.setItem(THEME_STORAGE_KEY, initialTheme);
  return initialTheme;
};

const getInitialScheduleSettings = (): ScheduledExtractionSettings => {
  try {
    const storedSettings = readMigratedStorageValue(
      SCHEDULE_STORAGE_KEY,
      legacyStorageKeys("extraction-schedule"),
    );
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
  const candidates = value
    .normalize("NFKC")
    .toUpperCase()
    .split(/[\s,;]+/)
    .map((item) => item.trim())
    .filter((item) => /^[A-Z0-9._/-]{2,80}$/.test(item));
  return [...new Set(candidates)];
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

const getAuthToken = () =>
  readMigratedStorageValue(AUTH_TOKEN_STORAGE_KEY, legacyStorageKeys("auth-token")) || "";

const authorizedHeaders = (initial?: HeadersInit) => {
  const headers = new Headers(initial);
  const token = getAuthToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return headers;
};

const apiErrorMessage = (data: unknown, fallback: string) => {
  if (
    data &&
    typeof data === "object" &&
    "detail" in data &&
    typeof data.detail === "string"
  ) {
    return data.detail;
  }
  return fallback;
};

export const HeroSection = () => {
  const [activeTab, setActiveTab] = useState(0);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [integrationsOpen, setIntegrationsOpen] = useState(false);
  const [extractionsOpen, setExtractionsOpen] = useState(false);
  const [administrationOpen, setAdministrationOpen] = useState(false);
  const [adminEditorOpen, setAdminEditorOpen] = useState(false);
  const [skuSelectorOpen, setSkuSelectorOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [loginUser, setLoginUser] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [authLoading, setAuthLoading] = useState(false);
  const [showLoginPassword, setShowLoginPassword] = useState(false);
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  const [adminUsers, setAdminUsers] = useState<AuthUser[]>([]);
  const [selectedAdminUser, setSelectedAdminUser] = useState<AuthUser | null>(null);
  const [adminUsername, setAdminUsername] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  const [adminIsAdmin, setAdminIsAdmin] = useState(false);
  const [adminLoading, setAdminLoading] = useState(false);
  const [adminSaving, setAdminSaving] = useState(false);
  const [adminError, setAdminError] = useState("");
  const [adminSearch, setAdminSearch] = useState("");
  const [showAdminPassword, setShowAdminPassword] = useState(false);
  const [adminDeleteTarget, setAdminDeleteTarget] = useState<AuthUser | null>(null);
  const [adminDeleteError, setAdminDeleteError] = useState("");
  const [theme, setTheme] = useState<ThemeMode>(getInitialTheme);
  const [extractionTab, setExtractionTab] = useState<ExtractionTab>("extract");
  const [scheduleSettings, setScheduleSettings] = useState<ScheduledExtractionSettings>(getInitialScheduleSettings);
  const [scheduleStatus, setScheduleStatus] = useState("");
  const [scheduleSaving, setScheduleSaving] = useState(false);
  const [interfacePreferences, setInterfacePreferences] = useState<InterfacePreferences>(getInterfacePreferences);
  const [integrationProviders, setIntegrationProviders] = useState<IntegrationProviderSettings[]>([]);
  const [integrationKeys, setIntegrationKeys] = useState<Record<string, string>>({});
  const [integrationsLoading, setIntegrationsLoading] = useState(false);
  const [integrationsSaving, setIntegrationsSaving] = useState(false);
  const [integrationsError, setIntegrationsError] = useState("");
  const [skuInput, setSkuInput] = useState("");
  const [forceExtraction, setForceExtraction] = useState(true);
  const [skuStatusItems, setSkuStatusItems] = useState<SkuStatusItem[]>([]);
  const [skuStatusLoading, setSkuStatusLoading] = useState(false);
  const [skuDatabaseFilter, setSkuDatabaseFilter] = useState("");
  const [deletingSku, setDeletingSku] = useState<string | null>(null);
  const [extractionRunning, setExtractionRunning] = useState(false);
  const [extractionProgress, setExtractionProgress] = useState("");
  const [activeExtractionJob, setActiveExtractionJob] = useState<ExtractionJob | null>(null);
  const [extractionResults, setExtractionResults] = useState<SkuBatchResult[]>([]);
  const [extractionError, setExtractionError] = useState("");
  const [skuImporting, setSkuImporting] = useState(false);
  const [skuImportSummary, setSkuImportSummary] = useState<SkuImportResponse | null>(null);
  const [refreshingSku, setRefreshingSku] = useState<string | null>(null);
  const [refreshingAllSkus, setRefreshingAllSkus] = useState(false);
  const [selectedSearchSkus, setSelectedSearchSkus] = useState<string[]>([]);
  const [draftSearchSkus, setDraftSearchSkus] = useState<string[]>([]);
  const [skuSelectorFilter, setSkuSelectorFilter] = useState("");
  const [skuSelectorError, setSkuSelectorError] = useState("");
  const accountRef = useRef<HTMLDivElement>(null);
  const skuFileInputRef = useRef<HTMLInputElement>(null);
  const themeTransitionTimerRef = useRef<number | null>(null);
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
    legacyStorageKeys("auth-user").forEach((key) => window.localStorage.removeItem(key));
    const token = getAuthToken();
    if (!token) return;

    let active = true;
    void fetch(`${getApiBaseUrl()}/auth/me`, { headers: authorizedHeaders() })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<AuthUser>;
      })
      .then((user) => {
        if (active) setCurrentUser(user);
      })
      .catch(() => {
        window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
        if (active) setCurrentUser(null);
      });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const closeMenus = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setLoginOpen(false);
        setSettingsOpen(false);
        setIntegrationsOpen(false);
        setExtractionsOpen(false);
        setAdminDeleteTarget(null);
        setAdminDeleteError("");
        setAdminEditorOpen(false);
        setAdministrationOpen(false);
        setSkuSelectorOpen(false);
        setAccountOpen(false);
        setSidebarOpen(false);
      }
    };

    window.addEventListener("keydown", closeMenus);
    return () => window.removeEventListener("keydown", closeMenus);
  }, []);

  useEffect(() => () => {
    if (themeTransitionTimerRef.current !== null) {
      window.clearTimeout(themeTransitionTimerRef.current);
    }
    document.documentElement.classList.remove("theme-transition");
  }, []);

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
    setSidebarOpen(false);
    setAuthError("");
    setLoginOpen(true);
  };

  const closeLogin = () => {
    setLoginOpen(false);
    setAuthError("");
    setLoginPassword("");
    setShowLoginPassword(false);
  };

  const handleLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (authLoading) return;
    setAuthLoading(true);
    setAuthError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: loginUser.trim().toUpperCase(),
          password: loginPassword,
        }),
      });
      const data = await response.json().catch(() => null) as AuthLoginResponse | { detail?: string } | null;
      if (!response.ok || !data || !("access_token" in data)) {
        throw new Error(apiErrorMessage(data, "Login ou senha inválidos."));
      }

      window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, data.access_token);
      setCurrentUser(data.user);
      setLoginUser("");
      setLoginPassword("");
      setShowLoginPassword(false);
      setLoginOpen(false);
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "Login ou senha inválidos.");
    } finally {
      setAuthLoading(false);
    }
  };

  const handleLogout = () => {
    const token = getAuthToken();
    if (token) {
      void fetch(`${getApiBaseUrl()}/auth/logout`, {
        method: "POST",
        headers: authorizedHeaders(),
      }).catch(() => undefined);
    }
    setCurrentUser(null);
    setAccountOpen(false);
    setIntegrationsOpen(false);
    setAdminEditorOpen(false);
    setAdministrationOpen(false);
    window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
  };

  const updateTheme = (nextTheme: ThemeMode) => {
    if (nextTheme === theme) return;

    const root = document.documentElement;
    root.classList.add("theme-transition");
    root.dataset.theme = nextTheme;
    window.localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
    setTheme(nextTheme);

    if (themeTransitionTimerRef.current !== null) {
      window.clearTimeout(themeTransitionTimerRef.current);
    }
    themeTransitionTimerRef.current = window.setTimeout(() => {
      root.classList.remove("theme-transition");
      themeTransitionTimerRef.current = null;
    }, 220);
  };

  const updateInterfacePreferences = (updater: (currentPreferences: InterfacePreferences) => InterfacePreferences) => {
    setInterfacePreferences((currentPreferences) => {
      const nextPreferences = updater(currentPreferences);
      saveInterfacePreferences(nextPreferences);
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

  const loadIntegrationSettings = async () => {
    if (!currentUser?.is_admin) return;
    setIntegrationsLoading(true);
    setIntegrationsError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/integrations`, {
        headers: authorizedHeaders(),
      });
      const data = await response.json().catch(() => null) as IntegrationSettingsResponse | { detail?: string } | null;
      if (!response.ok || !data || !("items" in data)) {
        throw new Error(apiErrorMessage(data, "Não foi possível carregar as integrações."));
      }
      setIntegrationProviders(data.items);
      setIntegrationKeys({});
    } catch (error) {
      setIntegrationsError(error instanceof Error ? error.message : "Não foi possível carregar as integrações.");
    } finally {
      setIntegrationsLoading(false);
    }
  };

  const updateIntegrationProvider = (
    provider: IntegrationProviderSettings["provider"],
    changes: Partial<IntegrationProviderSettings>,
  ) => {
    setIntegrationProviders((currentProviders) => currentProviders.map((currentProvider) => (
      currentProvider.provider === provider
        ? { ...currentProvider, ...changes }
        : currentProvider
    )));
  };

  const saveIntegrationSettings = async () => {
    if (integrationsSaving) return;
    setIntegrationsSaving(true);
    setIntegrationsError("");
    try {
      const updatedProviders: IntegrationProviderSettings[] = [];
      for (const provider of integrationProviders) {
        const apiKey = integrationKeys[provider.provider]?.trim();
        const response = await fetch(`${getApiBaseUrl()}/integrations/${provider.provider}`, {
          method: "PUT",
          headers: authorizedHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({
            enabled: provider.enabled,
            usage_mode: provider.usage_mode,
            max_results: provider.max_results,
            zone: provider.zone,
            actor_id: provider.actor_id,
            login: provider.login,
            ...(apiKey ? { api_key: apiKey } : {}),
          }),
        });
        const data = await response.json().catch(() => null) as IntegrationProviderSettings | { detail?: string } | null;
        if (!response.ok || !data || !("provider" in data)) {
          throw new Error(apiErrorMessage(data, `Não foi possível salvar ${provider.label}.`));
        }
        updatedProviders.push(data);
      }
      setIntegrationProviders(updatedProviders);
      setIntegrationKeys({});
    } catch (error) {
      setIntegrationsError(error instanceof Error ? error.message : "Não foi possível salvar as integrações.");
    } finally {
      setIntegrationsSaving(false);
    }
  };

  const openSettings = () => {
    setAccountOpen(false);
    setSidebarOpen(false);
    setSettingsOpen(true);
  };

  const openIntegrations = () => {
    if (!currentUser?.is_admin) return;
    setAccountOpen(false);
    setSidebarOpen(false);
    setIntegrationsOpen(true);
    void loadIntegrationSettings();
  };

  const openExtractions = () => {
    setAccountOpen(false);
    setSidebarOpen(false);
    setExtractionError("");
    if (!activeExtractionJob) setExtractionResults([]);
    setSkuImportSummary(null);
    setSkuDatabaseFilter("");
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

  const loadAdminUsers = async (preferredUserId?: number) => {
    setAdminLoading(true);
    setAdminError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/auth/users`, {
        headers: authorizedHeaders(),
      });
      const data = await response.json().catch(() => null) as AuthUsersResponse | { detail?: string } | null;
      if (!response.ok || !data || !("items" in data)) {
        throw new Error(apiErrorMessage(data, "Não foi possível carregar os usuários."));
      }
      setAdminUsers(data.items);
      if (preferredUserId) {
        setSelectedAdminUser(data.items.find((user) => user.id === preferredUserId) || null);
      }
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : "Não foi possível carregar os usuários.");
    } finally {
      setAdminLoading(false);
    }
  };

  const resetAdminEditor = () => {
    setSelectedAdminUser(null);
    setAdminUsername("");
    setAdminPassword("");
    setAdminIsAdmin(false);
    setAdminError("");
    setShowAdminPassword(false);
    setAdminDeleteTarget(null);
    setAdminDeleteError("");
  };

  const openAdministration = () => {
    if (!currentUser?.is_admin) return;
    setAccountOpen(false);
    setSidebarOpen(false);
    resetAdminEditor();
    setAdminEditorOpen(false);
    setAdminSearch("");
    setAdministrationOpen(true);
    void loadAdminUsers();
  };

  const closeAdministration = () => {
    setAdminEditorOpen(false);
    setAdministrationOpen(false);
    resetAdminEditor();
  };

  const openAdminCreate = () => {
    resetAdminEditor();
    setAdminEditorOpen(true);
  };

  const closeAdminEditor = () => {
    setAdminEditorOpen(false);
    resetAdminEditor();
  };

  const selectAdminUser = (user: AuthUser) => {
    setSelectedAdminUser(user);
    setAdminUsername(user.username);
    setAdminPassword("");
    setAdminIsAdmin(user.is_admin);
    setAdminError("");
    setShowAdminPassword(false);
    setAdminDeleteTarget(null);
    setAdminDeleteError("");
    setAdminEditorOpen(true);
  };

  const saveAdminUser = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (adminSaving) return;

    const editingUser = selectedAdminUser;
    const payload: { username?: string; password?: string; is_admin?: boolean } = {};
    if (editingUser) {
      if (adminPassword) payload.password = adminPassword;
      if (currentUser?.is_owner && !editingUser.is_owner && adminIsAdmin !== editingUser.is_admin) {
        payload.is_admin = adminIsAdmin;
      }
      if (!Object.keys(payload).length) {
        setAdminError("Informe uma nova senha ou altere o perfil administrativo.");
        return;
      }
    } else {
      payload.username = adminUsername.trim().toUpperCase();
      payload.password = adminPassword;
      payload.is_admin = currentUser?.is_owner ? adminIsAdmin : false;
    }

    setAdminSaving(true);
    setAdminError("");
    try {
      const response = await fetch(
        editingUser
          ? `${getApiBaseUrl()}/auth/users/${editingUser.id}`
          : `${getApiBaseUrl()}/auth/users`,
        {
          method: editingUser ? "PATCH" : "POST",
          headers: authorizedHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify(payload),
        },
      );
      const data = await response.json().catch(() => null) as AuthUser | { detail?: string } | null;
      if (!response.ok || !data || !("username" in data)) {
        throw new Error(apiErrorMessage(data, "Não foi possível salvar o usuário."));
      }

      if (editingUser?.id === currentUser?.id && adminPassword) {
        handleLogout();
        return;
      }

      setAdminPassword("");
      await loadAdminUsers();
      setSelectedAdminUser(null);
      setAdminEditorOpen(false);
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : "Não foi possível salvar o usuário.");
    } finally {
      setAdminSaving(false);
    }
  };

  const deleteAdminUser = async (user: AuthUser) => {
    if (adminSaving) return;

    setAdminSaving(true);
    setAdminDeleteError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/auth/users/${user.id}`, {
        method: "DELETE",
        headers: authorizedHeaders(),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => null) as { detail?: string } | null;
        throw new Error(apiErrorMessage(data, "Não foi possível remover o usuário."));
      }
      setAdminDeleteTarget(null);
      resetAdminEditor();
      setAdminEditorOpen(false);
      await loadAdminUsers();
    } catch (error) {
      setAdminDeleteError(error instanceof Error ? error.message : "Não foi possível remover o usuário.");
    } finally {
      setAdminSaving(false);
    }
  };

  const generateAdminPassword = () => {
    const characters = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%";
    const randomValues = new Uint32Array(14);
    window.crypto.getRandomValues(randomValues);
    setAdminPassword(
      Array.from(randomValues, (value) => characters[value % characters.length]).join(""),
    );
    setShowAdminPassword(true);
    setAdminError("");
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
    if (scheduleSaving) return;
    setScheduleSaving(true);
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
    } finally {
      setScheduleSaving(false);
    }
  };

  const runExtractions = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const skus = parseSkuInput(skuInput);

    if (!skus.length) {
      setExtractionError("Informe ao menos um SKU.");
      return;
    }

    if (extractionRunning) {
      setExtractionError("Ja existe uma fila de extracao em andamento.");
      return;
    }

    setExtractionRunning(true);
    setExtractionProgress(`0 de ${skus.length}`);
    setExtractionError("");
    setExtractionResults([]);

    try {
      const response = await fetch(`${getApiBaseUrl()}/products/extraction-jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          skus,
          force_refresh: forceExtraction,
          refresh_if_stale: false,
          postal_code: interfacePreferences.logisticsPostalCode || null,
          scope: "all",
        }),
      });
      const data = await response.json().catch(() => null) as ExtractionJob | { detail?: string } | null;
      if (!response.ok || !data || !("id" in data)) {
        throw new Error(apiErrorMessage(data, `HTTP ${response.status}`));
      }
      setActiveExtractionJob(data);
      setExtractionProgress(`${data.processed_skus} de ${data.total_skus}`);
      window.localStorage.setItem(EXTRACTION_JOB_STORAGE_KEY, data.id);
    } catch (error) {
      console.error("Erro ao extrair SKUs:", error);
      setExtractionError(error instanceof Error ? error.message : "Falha ao extrair SKUs.");
      setExtractionRunning(false);
      setExtractionProgress("");
    }
  };

  const importSkuFile = async (file?: File) => {
    if (!file || skuImporting) return;
    setSkuImporting(true);
    setExtractionError("");
    setSkuImportSummary(null);

    try {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch(`${getApiBaseUrl()}/products/import/skus`, {
        method: "POST",
        body,
      });
      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        throw new Error(errorData?.detail || `HTTP ${response.status}`);
      }

      const data = (await response.json()) as SkuImportResponse;
      setSkuImportSummary(data);
      setSkuInput(data.skus.join("\n"));
      await loadSkuStatus();
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Falha ao importar a planilha.");
    } finally {
      setSkuImporting(false);
      if (skuFileInputRef.current) skuFileInputRef.current.value = "";
    }
  };

  const downloadSkuTemplate = () => {
    window.open(`${getApiBaseUrl()}/products/import/skus/template`, "_blank", "noopener,noreferrer");
  };

  const refreshSingleSku = async (sku: string) => {
    if (refreshingSku || extractionRunning) return;
    setRefreshingSku(sku);
    setExtractionRunning(true);
    setExtractionProgress("0 de 1");
    setExtractionError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/extraction-jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          skus: [sku],
          force_refresh: true,
          postal_code: interfacePreferences.logisticsPostalCode || null,
          scope: "all",
        }),
      });
      const data = await response.json().catch(() => null) as ExtractionJob | { detail?: string } | null;
      if (!response.ok || !data || !("id" in data)) {
        throw new Error(apiErrorMessage(data, `HTTP ${response.status}`));
      }
      setActiveExtractionJob(data);
      window.localStorage.setItem(EXTRACTION_JOB_STORAGE_KEY, data.id);
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Falha ao atualizar o SKU.");
      setRefreshingSku(null);
      setExtractionRunning(false);
      setExtractionProgress("");
    }
  };

  const deleteLocalSku = async (sku: string) => {
    if (deletingSku || extractionRunning) return;
    setDeletingSku(sku);
    setExtractionError("");
    try {
      const response = await fetch(
        `${getApiBaseUrl()}/products/skus/${encodeURIComponent(sku)}`,
        { method: "DELETE" },
      );
      const data = await response.json().catch(() => null) as { detail?: string } | null;
      if (!response.ok) {
        throw new Error(apiErrorMessage(data, "Não foi possível excluir o SKU."));
      }
      setSkuStatusItems((items) => items.filter((item) => item.sku !== sku));
      setSelectedSearchSkus((items) => items.filter((item) => item !== sku));
      setDraftSearchSkus((items) => items.filter((item) => item !== sku));
      setExtractionResults((items) => items.filter((item) => item.sku !== sku));
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Não foi possível excluir o SKU.");
    } finally {
      setDeletingSku(null);
    }
  };

  const refreshAllSkus = async () => {
    const skus = skuStatusItems.map((item) => item.sku).filter(Boolean);
    if (!skus.length || refreshingAllSkus || extractionRunning) return;

    setRefreshingAllSkus(true);
    setExtractionRunning(true);
    setExtractionProgress(`0 de ${skus.length}`);
    setExtractionError("");
    setExtractionResults([]);

    try {
      const response = await fetch(`${getApiBaseUrl()}/products/extraction-jobs/database`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          force_refresh: true,
          refresh_if_stale: false,
          postal_code: interfacePreferences.logisticsPostalCode || null,
          scope: "all",
        }),
      });
      const data = await response.json().catch(() => null) as ExtractionJob | { detail?: string } | null;
      if (!response.ok || !data || !("id" in data)) {
        throw new Error(apiErrorMessage(data, `HTTP ${response.status}`));
      }
      setActiveExtractionJob(data);
      window.localStorage.setItem(EXTRACTION_JOB_STORAGE_KEY, data.id);
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Falha ao atualizar o banco local.");
      setRefreshingAllSkus(false);
      setExtractionRunning(false);
      setExtractionProgress("");
    }
  };

  const cancelActiveExtraction = async () => {
    if (!activeExtractionJob || !isExtractionJobActive(activeExtractionJob)) return;
    try {
      const response = await fetch(
        `${getApiBaseUrl()}/products/extraction-jobs/${activeExtractionJob.id}/cancel`,
        { method: "POST" },
      );
      const data = await response.json().catch(() => null) as ExtractionJob | { detail?: string } | null;
      if (!response.ok || !data || !("id" in data)) {
        throw new Error(apiErrorMessage(data, `HTTP ${response.status}`));
      }
      setActiveExtractionJob(data);
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Falha ao cancelar a fila.");
    }
  };

  useEffect(() => {
    let disposed = false;

    const restoreJob = async () => {
      const storedJobId = window.localStorage.getItem(EXTRACTION_JOB_STORAGE_KEY);
      const storedUrl = storedJobId
        ? `${getApiBaseUrl()}/products/extraction-jobs/${storedJobId}`
        : null;

      try {
        let job: ExtractionJob | null = null;
        if (storedUrl) {
          const storedResponse = await fetch(storedUrl);
          if (storedResponse.ok) job = await storedResponse.json() as ExtractionJob;
        }
        if (!job || !isExtractionJobActive(job)) {
          const activeResponse = await fetch(`${getApiBaseUrl()}/products/extraction-jobs/active`);
          if (activeResponse.ok) {
            const data = await activeResponse.json() as ActiveExtractionJobResponse;
            job = data.job || job;
          }
        }
        if (disposed || !job) return;
        setActiveExtractionJob(job);
        if (isExtractionJobActive(job)) {
          setExtractionRunning(true);
          setExtractionProgress(`${job.processed_skus} de ${job.total_skus}`);
          setRefreshingAllSkus(job.kind === "database");
          window.localStorage.setItem(EXTRACTION_JOB_STORAGE_KEY, job.id);
        } else {
          window.localStorage.removeItem(EXTRACTION_JOB_STORAGE_KEY);
        }
      } catch {
        window.localStorage.removeItem(EXTRACTION_JOB_STORAGE_KEY);
      }
    };

    void restoreJob();
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    if (!activeExtractionJob || !isExtractionJobActive(activeExtractionJob)) return;
    const jobId = activeExtractionJob.id;
    let disposed = false;
    let timer: number | undefined;

    const pollJob = async () => {
      try {
        const response = await fetch(`${getApiBaseUrl()}/products/extraction-jobs/${jobId}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        let job = await response.json() as ExtractionJob;
        if (disposed) return;

        setActiveExtractionJob(job);
        setExtractionRunning(isExtractionJobActive(job));
        setExtractionProgress(`${job.processed_skus} de ${job.total_skus}`);
        setRefreshingAllSkus(isExtractionJobActive(job) && job.kind === "database");

        if (!isExtractionJobActive(job)) {
          const completedResponse = await fetch(
            `${getApiBaseUrl()}/products/extraction-jobs/${jobId}?include_items=true`,
          );
          if (completedResponse.ok) job = await completedResponse.json() as ExtractionJob;
          if (disposed) return;
          const items = job.items || [];
          const orderedItems = [
            ...items.filter((item) => item.status === "error"),
            ...items.filter((item) => item.status !== "error"),
          ];
          setActiveExtractionJob(job);
          setExtractionResults(orderedItems.slice(0, 300));
          setExtractionRunning(false);
          setExtractionProgress("");
          setRefreshingAllSkus(false);
          setRefreshingSku(null);
          window.localStorage.removeItem(EXTRACTION_JOB_STORAGE_KEY);
          await loadSkuStatus();
          return;
        }
      } catch (error) {
        if (!disposed) {
          console.error("Erro ao acompanhar fila de extracao:", error);
          setExtractionError("A fila continua no backend, mas o progresso nao pode ser consultado agora.");
        }
      }
      if (!disposed) timer = window.setTimeout(pollJob, 1400);
    };

    timer = window.setTimeout(pollJob, 500);
    return () => {
      disposed = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [activeExtractionJob?.id, activeExtractionJob?.status]);

  const openSkuSelector = () => {
    setSidebarOpen(false);
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
  const normalizedSkuDatabaseFilter = skuDatabaseFilter.trim().toLowerCase();
  const visibleDatabaseSkuItems = skuStatusItems.filter((item) =>
    [item.sku, item.description, item.subfamily, item.sku_category, item.cluster]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(normalizedSkuDatabaseFilter)),
  );
  const visibleAdminUsers = adminUsers.filter((user) =>
    user.username.toLowerCase().includes(adminSearch.trim().toLowerCase()),
  );
  const scheduleSummary = getScheduleSummary(scheduleSettings);
  const adminTargetLocked = Boolean(
    selectedAdminUser &&
    !currentUser?.is_owner &&
    selectedAdminUser.is_admin &&
    selectedAdminUser.id !== currentUser?.id,
  );
  const canDeleteAdminUser = (user: AuthUser) => Boolean(
    !user.is_owner &&
    user.id !== currentUser?.id &&
    (currentUser?.is_owner || !user.is_admin)
  );
  const integrationGroups = [
    { category: "discovery", title: "Descoberta de lojistas" },
    { category: "store", title: "APIs por loja" },
  ] as const;

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
          <img src={theme === "dark" ? logoWhiteUrl : logoUrl} alt="NeveTracker" className="h-8 w-auto" />
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
                {currentUser.is_admin && (
                  <button type="button" className="account-popover__item" onClick={openIntegrations}>
                    <Plug size={15} />
                    Integrações
                  </button>
                )}
                {currentUser.is_admin && (
                  <button type="button" className="account-popover__item" onClick={openAdministration}>
                    <ShieldCheck size={15} />
                    Administração
                  </button>
                )}
                <button
                  type="button"
                  onClick={handleLogout}
                  className="account-popover__item"
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
              <span className="account-avatar">
                {currentUser ? currentUser.username.slice(0, 2) : <UserRound size={17} />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-semibold text-[var(--foreground)]">
                  {currentUser?.username || "Entrar na conta"}
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
              aria-label="Abrir configurações"
              title="Configurações"
              onClick={openSettings}
            >
              <Settings size={17} />
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
                <span className="auth-dialog__icon">
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
                required
                minLength={3}
                maxLength={40}
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
              <div className="password-input">
                <input
                  type={showLoginPassword ? "text" : "password"}
                  value={loginPassword}
                  required
                  minLength={4}
                  maxLength={200}
                  onChange={(event) => {
                    setLoginPassword(event.target.value);
                    setAuthError("");
                  }}
                  autoComplete="current-password"
                />
                <button
                  type="button"
                  onClick={() => setShowLoginPassword((visible) => !visible)}
                  aria-label={showLoginPassword ? "Ocultar senha" : "Mostrar senha"}
                  title={showLoginPassword ? "Ocultar senha" : "Mostrar senha"}
                >
                  {showLoginPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </label>

            {authError && <p className="auth-error">{authError}</p>}

            <div className="mt-6 flex justify-end gap-2">
              <button type="submit" className="button-primary async-button" disabled={authLoading}>
                <span className={authLoading ? "async-button__content async-button__content--hidden" : "async-button__content"}>
                  Entrar
                </span>
                {authLoading && (
                  <span className="async-button__loader" aria-hidden="true">
                    <LoaderCircle size={16} className="animate-spin" />
                  </span>
                )}
              </button>
            </div>
          </form>
        </div>
      )}

      {administrationOpen && currentUser?.is_admin && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={closeAdministration}>
          <section
            className="workspace-dialog workspace-dialog--admin-list"
            role="dialog"
            aria-modal="true"
            aria-label="Administração de usuários"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="dialog-header">
              <div>
                <span className="dialog-icon"><ShieldCheck size={19} /></span>
                <h2>Administração</h2>
              </div>
              <button type="button" className="icon-button" onClick={closeAdministration} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <div className="admin-user-pane">
              <div className="admin-pane-header">
                <div>
                  <strong>Usuários</strong>
                  <span>{adminUsers.length} cadastrados</span>
                </div>
                <button type="button" className="button-primary" onClick={openAdminCreate}>
                  <UserPlus size={15} />
                  Criar usuário
                </button>
              </div>

              <div className="admin-user-search">
                <Search size={15} />
                <input
                  type="search"
                  value={adminSearch}
                  onChange={(event) => setAdminSearch(event.target.value)}
                  placeholder="Buscar usuário..."
                  aria-label="Buscar usuário"
                />
                {adminSearch && (
                  <button type="button" onClick={() => setAdminSearch("")} aria-label="Limpar busca">
                    <X size={14} />
                  </button>
                )}
              </div>

              <div className="admin-user-list">
                {adminLoading && !adminUsers.length ? (
                  <p className="admin-empty">Carregando usuários...</p>
                ) : visibleAdminUsers.length ? (
                  visibleAdminUsers.map((user) => {
                    return (
                      <div
                        key={user.id}
                        className={selectedAdminUser?.id === user.id ? "admin-user-row admin-user-row--active" : "admin-user-row"}
                      >
                        <button type="button" className="admin-user-row__main" onClick={() => selectAdminUser(user)}>
                          <span className="admin-user-avatar">{user.username.slice(0, 2)}</span>
                          <span className="min-w-0 flex-1">
                            <strong>{user.username}</strong>
                            <small>{user.is_owner ? "Principal" : user.is_admin ? "Administrador" : "Usuário"}</small>
                          </span>
                        </button>
                        {user.is_admin && (
                          <span
                            className="admin-user-role-icon"
                            aria-label={user.is_owner ? "Usuário principal" : "Administrador"}
                            title={user.is_owner ? "Usuário principal" : "Administrador"}
                          >
                            <ShieldCheck size={15} />
                          </span>
                        )}
                        {canDeleteAdminUser(user) && (
                          <button
                            type="button"
                            className="admin-user-delete"
                            disabled={adminSaving}
                            onClick={() => {
                              setAdminDeleteTarget(user);
                              setAdminDeleteError("");
                            }}
                            aria-label={`Remover ${user.username}`}
                            title="Remover usuário"
                          >
                            <Trash2 size={15} />
                          </button>
                        )}
                      </div>
                    );
                  })
                ) : (
                  <p className="admin-empty">Nenhum usuário encontrado.</p>
                )}
              </div>
              {!adminEditorOpen && adminError && <p className="auth-error admin-directory-message">{adminError}</p>}
            </div>
          </section>
        </div>
      )}

      {administrationOpen && adminEditorOpen && currentUser?.is_admin && (
        <div className="dialog-backdrop dialog-backdrop--nested" role="presentation" onMouseDown={closeAdminEditor}>
          <section
            className="workspace-dialog workspace-dialog--admin-editor"
            role="dialog"
            aria-modal="true"
            aria-label={selectedAdminUser ? "Gerenciar acesso" : "Novo acesso"}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="dialog-header">
              <div>
                <span className="dialog-icon">{selectedAdminUser ? <ShieldCheck size={19} /> : <UserPlus size={19} />}</span>
                <h2>{selectedAdminUser ? "Gerenciar acesso" : "Novo acesso"}</h2>
              </div>
              <button type="button" className="icon-button" onClick={closeAdminEditor} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <form className="admin-access-form" onSubmit={saveAdminUser}>
              {selectedAdminUser && (
                <div className="admin-editor__head">
                  <div>
                    <h3>{selectedAdminUser.username}</h3>
                    <small>Último acesso: {formatDateTime(selectedAdminUser.last_login)}</small>
                  </div>
                  {selectedAdminUser.is_owner && <span className="admin-role-badge">Principal</span>}
                </div>
              )}

              <label className="form-field">
                <span>Login</span>
                <input
                  type="text"
                  value={adminUsername}
                  disabled={Boolean(selectedAdminUser)}
                  required={!selectedAdminUser}
                  minLength={3}
                  maxLength={40}
                  pattern="[A-Za-z0-9._-]+"
                  onChange={(event) => {
                    setAdminUsername(event.target.value.toUpperCase());
                    setAdminError("");
                  }}
                  autoComplete="off"
                />
              </label>

              <label className="form-field">
                <span>{selectedAdminUser ? "Nova senha" : "Senha"}</span>
                <div className="password-input">
                  <input
                    type={showAdminPassword ? "text" : "password"}
                    value={adminPassword}
                    disabled={adminTargetLocked}
                    required={!selectedAdminUser}
                    minLength={4}
                    maxLength={200}
                    onChange={(event) => {
                      setAdminPassword(event.target.value);
                      setAdminError("");
                    }}
                    autoComplete="new-password"
                  />
                  <button
                    type="button"
                    disabled={adminTargetLocked}
                    onClick={() => setShowAdminPassword((visible) => !visible)}
                    aria-label={showAdminPassword ? "Ocultar senha" : "Mostrar senha"}
                    title={showAdminPassword ? "Ocultar senha" : "Mostrar senha"}
                  >
                    {showAdminPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
                {!adminTargetLocked && (
                  <span className="password-field-tools password-field-tools--end">
                    <button type="button" onClick={generateAdminPassword}>
                      <RefreshCw size={13} />
                      Gerar senha
                    </button>
                  </span>
                )}
              </label>

              {currentUser.is_owner && !selectedAdminUser?.is_owner && (
                <label className="admin-toggle-row">
                  <span>
                    <strong>Administrador</strong>
                    <small>Acesso à gestão de usuários</small>
                  </span>
                  <span className="schedule-toggle">
                    <input
                      type="checkbox"
                      checked={adminIsAdmin}
                      onChange={(event) => setAdminIsAdmin(event.target.checked)}
                    />
                    <span className="schedule-toggle__track"><span className="schedule-toggle__thumb" /></span>
                  </span>
                </label>
              )}

              {adminTargetLocked && (
                <p className="admin-notice">Somente DEUSM2 pode alterar outro administrador.</p>
              )}
              {adminError && <p className="auth-error">{adminError}</p>}

              <div className="admin-editor__actions">
                <button type="submit" className="button-primary async-button" disabled={adminSaving || adminTargetLocked}>
                  <span className={adminSaving ? "async-button__content async-button__content--hidden" : "async-button__content"}>
                    {selectedAdminUser ? "Salvar" : "Criar usuário"}
                  </span>
                  {adminSaving && (
                    <span className="async-button__loader" aria-hidden="true">
                      <LoaderCircle size={16} className="animate-spin" />
                    </span>
                  )}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}

      {administrationOpen && adminDeleteTarget && currentUser?.is_admin && (
        <div
          className="dialog-backdrop dialog-backdrop--nested dialog-backdrop--confirmation"
          role="presentation"
          onMouseDown={() => {
            if (adminSaving) return;
            setAdminDeleteTarget(null);
            setAdminDeleteError("");
          }}
        >
          <section
            className="workspace-dialog workspace-dialog--confirmation"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="admin-delete-title"
            aria-describedby="admin-delete-description"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="confirmation-icon" aria-hidden="true">
              <Trash2 size={19} />
            </div>
            <h2 id="admin-delete-title">Excluir usuário?</h2>
            <p id="admin-delete-description">
              O acesso de <strong>{adminDeleteTarget.username}</strong> será removido permanentemente.
            </p>
            {adminDeleteError && <p className="auth-error">{adminDeleteError}</p>}
            <div className="confirmation-actions">
              <button
                type="button"
                className="button-secondary"
                disabled={adminSaving}
                onClick={() => {
                  setAdminDeleteTarget(null);
                  setAdminDeleteError("");
                }}
              >
                Cancelar
              </button>
              <button
                type="button"
                className="button-primary async-button"
                disabled={adminSaving}
                onClick={() => void deleteAdminUser(adminDeleteTarget)}
              >
                <span className={adminSaving ? "async-button__content async-button__content--hidden" : "async-button__content"}>
                  Excluir
                </span>
                {adminSaving && (
                  <span className="async-button__loader" aria-hidden="true">
                    <LoaderCircle size={16} className="animate-spin" />
                  </span>
                )}
              </button>
            </div>
          </section>
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
            className="workspace-dialog workspace-dialog--settings"
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
              <section className="settings-section settings-section--appearance">
                <div className="settings-section__head">
                  <h3>Aparência</h3>
                </div>
                <div className="settings-grid">
                  <label className="settings-field">
                    <span>Tema</span>
                    <div className="settings-select">
                      <select
                        value={theme}
                        onChange={(event) => updateTheme(event.target.value as ThemeMode)}
                      >
                        <option value="light">Claro</option>
                        <option value="dark">Escuro</option>
                      </select>
                      <ChevronDown size={16} aria-hidden="true" />
                    </div>
                  </label>
                </div>
              </section>

              <section className="settings-section settings-section--format">
                <div className="settings-section__head">
                  <h3>Formato</h3>
                </div>
                <div className="settings-grid">
                  <label className="settings-field">
                    <span>Moeda</span>
                    <div className="settings-select">
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
                      <ChevronDown size={16} aria-hidden="true" />
                    </div>
                  </label>

                  <label className="settings-field">
                    <span>Formato numérico</span>
                    <div className="settings-select">
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
                      <ChevronDown size={16} aria-hidden="true" />
                    </div>
                  </label>
                </div>
              </section>

              <section className="settings-section settings-section--logistics">
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

            </div>
          </section>
        </div>
      )}

      {integrationsOpen && currentUser?.is_admin && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={() => setIntegrationsOpen(false)}>
          <section
            className="workspace-dialog workspace-dialog--integrations"
            role="dialog"
            aria-modal="true"
            aria-label="Integrações"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="dialog-header">
              <div>
                <span className="dialog-icon"><Plug size={19} /></span>
                <h2>Integrações</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setIntegrationsOpen(false)} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <div className="integration-modal-body">
              {integrationsLoading ? (
                <div className="integration-loading">
                  <LoaderCircle size={17} className="animate-spin" />
                  Carregando integrações
                </div>
              ) : (
                integrationGroups.map((group) => {
                  const providers = integrationProviders.filter((provider) => provider.category === group.category);
                  if (!providers.length) return null;

                  return (
                    <section className="integration-group" key={group.category}>
                      <div className="integration-group__head">
                        <h3>{group.title}</h3>
                      </div>
                      <div className="integration-list">
                        {providers.map((provider) => {
                          const extraField = provider.provider === "bright_data"
                            ? { label: "Zona SERP", value: provider.zone || "", key: "zone" as const }
                            : provider.provider === "apify"
                              ? { label: "Actor ID", value: provider.actor_id || "", key: "actor_id" as const }
                              : provider.provider === "dataforseo"
                                ? { label: "Login", value: provider.login || "", key: "login" as const }
                                : null;

                          return (
                            <div className={`integration-provider integration-provider--${provider.category}`} key={provider.provider}>
                              <div className="integration-provider__head">
                                <div>
                                  <div className="integration-provider__title">
                                    <strong>{provider.label}</strong>
                                    <a
                                      href={provider.credential_url}
                                      target="_blank"
                                      rel="noreferrer"
                                      onClick={(event) => event.stopPropagation()}
                                    >
                                      Obter credencial
                                      <ExternalLink size={12} aria-hidden="true" />
                                    </a>
                                  </div>
                                  <span>{provider.description}</span>
                                  <span className={`integration-quota ${provider.quota.hard_locked ? "integration-quota--locked" : ""}`}>
                                    <ShieldCheck size={13} aria-hidden="true" />
                                    {provider.quota.limit === null
                                      ? "Sem cobrança por uso: nenhuma cota financeira necessária"
                                      : provider.quota.hard_locked
                                        ? `Cota gratuita esgotada: novas chamadas estão bloqueadas`
                                        : `${provider.quota.remaining?.toLocaleString("pt-BR")} de ${provider.quota.limit.toLocaleString("pt-BR")} ${provider.quota.unit} disponíveis ${provider.quota.period === "monthly" ? "neste mês" : "na franquia gratuita"}`}
                                  </span>
                                </div>
                                <label className="schedule-toggle">
                                  <input
                                    type="checkbox"
                                    checked={provider.enabled}
                                    onChange={(event) => updateIntegrationProvider(provider.provider, { enabled: event.target.checked })}
                                  />
                                  <span className="schedule-toggle__track" aria-hidden="true">
                                    <span className="schedule-toggle__thumb" />
                                  </span>
                                  {provider.enabled ? "Ativo" : "Inativo"}
                                </label>
                              </div>

                              <div className="integration-fields">
                                <label className="settings-field integration-field--key">
                                  <span>{provider.credential_label}</span>
                                  <input
                                    type="password"
                                    autoComplete="off"
                                    value={integrationKeys[provider.provider] || ""}
                                    placeholder={provider.api_key_masked || "Cole a credencial"}
                                    onChange={(event) => setIntegrationKeys((currentKeys) => ({
                                      ...currentKeys,
                                      [provider.provider]: event.target.value,
                                    }))}
                                  />
                                </label>

                                {extraField && (
                                  <label className="settings-field integration-field--endpoint">
                                    <span>{extraField.label}</span>
                                    <input
                                      value={extraField.value}
                                      onChange={(event) => updateIntegrationProvider(provider.provider, {
                                        [extraField.key]: event.target.value,
                                      })}
                                    />
                                  </label>
                                )}

                                {provider.supports_usage_mode && (
                                  <label className="settings-field integration-field--mode">
                                    <span>Quando usar</span>
                                    <div className="settings-select">
                                      <select
                                        value={provider.usage_mode}
                                        onChange={(event) => updateIntegrationProvider(provider.provider, {
                                          usage_mode: event.target.value as IntegrationProviderSettings["usage_mode"],
                                        })}
                                      >
                                        <option value="fallback">Completar lacunas</option>
                                        <option value="always">Toda extração</option>
                                      </select>
                                      <ChevronDown size={16} aria-hidden="true" />
                                    </div>
                                  </label>
                                )}

                                {provider.supports_limit && (
                                  <label className="settings-field integration-field--limit">
                                    <span>Resultados por SKU</span>
                                    <div className="settings-select">
                                      <select
                                        value={provider.max_results}
                                        onChange={(event) => updateIntegrationProvider(provider.provider, {
                                          max_results: Number(event.target.value),
                                        })}
                                      >
                                        {[10, 20, 30, 40, 50, 100].map((limit) => (
                                          <option key={limit} value={limit}>{limit}</option>
                                        ))}
                                      </select>
                                      <ChevronDown size={16} aria-hidden="true" />
                                    </div>
                                  </label>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </section>
                  );
                })
              )}
            </div>

            {integrationsError && <p className="auth-error">{integrationsError}</p>}
            <div className="integration-modal-footer">
              <button
                type="button"
                className="button-primary"
                onClick={() => void saveIntegrationSettings()}
                disabled={integrationsSaving || integrationsLoading || !integrationProviders.length}
              >
                {integrationsSaving ? <LoaderCircle size={15} className="animate-spin" /> : <KeyRound size={15} />}
                Salvar
              </button>
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

            {activeExtractionJob && (
              <div
                className={
                  isExtractionJobActive(activeExtractionJob)
                    ? "extraction-job-card extraction-job-card--active"
                    : "extraction-job-card"
                }
                role="status"
                aria-live="polite"
              >
                <div className="extraction-job-card__head">
                  <span className="extraction-job-card__icon">
                    {isExtractionJobActive(activeExtractionJob)
                      ? <LoaderCircle size={16} className="animate-spin" />
                      : <ListChecks size={16} />}
                  </span>
                  <span className="extraction-job-card__copy">
                    <strong>{extractionJobTitle(activeExtractionJob)}</strong>
                    <small>
                      {activeExtractionJob.processed_skus} de {activeExtractionJob.total_skus} SKUs
                      {activeExtractionJob.batches_total > 1
                        ? ` · lotes de ${activeExtractionJob.batch_size}`
                        : ""}
                    </small>
                  </span>
                  {isExtractionJobActive(activeExtractionJob) && !activeExtractionJob.cancel_requested && (
                    <button type="button" className="button-quiet" onClick={() => void cancelActiveExtraction()}>
                      Cancelar
                    </button>
                  )}
                </div>
                <div className="extraction-job-progress" aria-hidden="true">
                  <span style={{ width: `${Math.min(100, Math.max(0, activeExtractionJob.progress_percent))}%` }} />
                </div>
                <div className="extraction-job-card__meta">
                  <span>
                    {activeExtractionJob.current_sku
                      ? `Processando ${activeExtractionJob.current_sku}`
                      : activeExtractionJob.message}
                  </span>
                  <span>
                    {activeExtractionJob.success_count} concluídos
                    {activeExtractionJob.error_count > 0 ? ` · ${activeExtractionJob.error_count} erros` : ""}
                  </span>
                </div>
              </div>
            )}

            {extractionTab === "extract" ? (
              <div className="extraction-panel" role="tabpanel">
                <form onSubmit={runExtractions} className="extraction-form extraction-form--fill">
                  <div className="sku-import-block">
                    <div className="sku-import-block__head">
                      <div>
                        <strong>Importar planilha</strong>
                        <span>XLSX, XLSM ou CSV com uma coluna SKU</span>
                      </div>
                      <button type="button" className="button-quiet" onClick={downloadSkuTemplate}>
                        <FileDown size={15} />
                        Baixar modelo
                      </button>
                    </div>
                    <input
                      ref={skuFileInputRef}
                      type="file"
                      className="sr-only"
                      accept=".xlsx,.xlsm,.csv"
                      onChange={(event) => void importSkuFile(event.target.files?.[0])}
                    />
                    <button
                      type="button"
                      className="sku-import-dropzone"
                      disabled={skuImporting}
                      onClick={() => skuFileInputRef.current?.click()}
                      onDragOver={(event) => event.preventDefault()}
                      onDrop={(event) => {
                        event.preventDefault();
                        void importSkuFile(event.dataTransfer.files?.[0]);
                      }}
                    >
                      {skuImporting ? <LoaderCircle size={17} className="animate-spin" /> : <Upload size={17} />}
                      <span>{skuImporting ? "Importando..." : "Selecionar ou arrastar arquivo"}</span>
                    </button>
                    {skuImportSummary && (
                      <div className="sku-import-summary" role="status">
                        <strong>{skuImportSummary.imported_count} SKUs importados</strong>
                        <span>
                          {skuImportSummary.created_count} novos · {skuImportSummary.updated_count} atualizados
                          {skuImportSummary.skipped_count > 0 ? ` · ${skuImportSummary.skipped_count} ignorados` : ""}
                        </span>
                      </div>
                    )}
                  </div>

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
                      {extractionRunning ? `Na fila ${extractionProgress}` : "Extrair"}
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
                    <div className={scheduleSettings.enabled ? "schedule-summary" : "schedule-summary schedule-summary--inactive"}>
                      <span className="schedule-summary__icon"><CalendarClock size={17} /></span>
                      <span className="schedule-summary__content">
                        <strong>{scheduleSummary.primary}</strong>
                        <small>{scheduleSummary.secondary}</small>
                      </span>
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
                    <button
                      type="button"
                      className="button-primary async-button"
                      onClick={() => void saveSchedule()}
                      disabled={scheduleSaving}
                    >
                      <span className={scheduleSaving ? "async-button__content async-button__content--hidden" : "async-button__content"}>
                        <CheckCircle2 size={15} />
                        Salvar
                      </span>
                      {scheduleSaving && (
                        <span className="async-button__loader" aria-hidden="true">
                          <LoaderCircle size={16} className="animate-spin" />
                        </span>
                      )}
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
                      <p>
                        {normalizedSkuDatabaseFilter
                          ? `${visibleDatabaseSkuItems.length} de ${skuStatusItems.length} SKUs`
                          : `${skuStatusItems.length} SKUs acompanhados`}
                      </p>
                    </div>
                    <button
                      type="button"
                      className="button-secondary sku-refresh-all"
                      onClick={() => void refreshAllSkus()}
                      disabled={extractionRunning || refreshingAllSkus || !skuStatusItems.length}
                    >
                      {refreshingAllSkus && <LoaderCircle size={15} className="animate-spin" />}
                      {refreshingAllSkus ? extractionProgress || "Atualizando" : "Atualizar"}
                    </button>
                  </div>
                  <div className="sku-status-search">
                    <div className="sku-status-search__field">
                      <Search size={15} />
                      <input
                        type="search"
                        value={skuDatabaseFilter}
                        onChange={(event) => setSkuDatabaseFilter(event.target.value)}
                        placeholder="Pesquisar no banco local..."
                        aria-label="Pesquisar SKUs do banco local"
                      />
                    </div>
                  </div>
                  <div className="sku-status-list">
                    {skuStatusLoading && !skuStatusItems.length ? (
                      <p className="sku-status-empty">Carregando...</p>
                    ) : visibleDatabaseSkuItems.length ? (
                      visibleDatabaseSkuItems.map((item) => (
                        <div key={item.sku} className="sku-status-row">
                          <strong>{item.sku}</strong>
                          <span className="sku-status-row__offers">{item.offer_count} ofertas</span>
                          <span className="sku-status-row__stores">{item.store_count} lojas</span>
                          <time>{formatDateTime(item.last_update)}</time>
                          <span className="sku-status-actions">
                            <button
                              type="button"
                              className="table-action"
                              onClick={() => void deleteLocalSku(item.sku)}
                              disabled={extractionRunning || Boolean(deletingSku)}
                              aria-label={`Excluir ${item.sku}`}
                              title="Excluir SKU"
                            >
                              {deletingSku === item.sku
                                ? <LoaderCircle size={15} className="animate-spin" />
                                : <Trash2 size={15} />}
                            </button>
                            <button
                              type="button"
                              className="table-action"
                              onClick={() => void refreshSingleSku(item.sku)}
                              disabled={extractionRunning || Boolean(refreshingSku) || Boolean(deletingSku)}
                              aria-label={`Atualizar ${item.sku}`}
                              title="Atualizar SKU"
                            >
                              {refreshingSku === item.sku
                                ? <LoaderCircle size={15} className="animate-spin" />
                                : <RefreshCw size={15} />}
                            </button>
                          </span>
                        </div>
                      ))
                    ) : (
                      <p className="sku-status-empty">
                        {skuStatusItems.length ? "Nenhum SKU encontrado." : "Nenhum SKU no banco local."}
                      </p>
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
