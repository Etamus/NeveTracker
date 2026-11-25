import ThemeSelector from "../ThemeSelector";
import { parseTheme, resolveTheme, THEME_STORAGE_KEY, themeMedia, type ThemeMode } from "../../src/theme";
import DialogBackdrop from "../DialogBackdrop";
import ClearIcon from "../ClearIcon";
import ExtractionSellerSelect, { type SellerSelection } from "../ExtractionSellerSelect";
import DatePeriodSelect from "../DatePeriodSelect";
import RowAlignedFrame from "../RowAlignedFrame";
import OrbitingSpheres from "../OrbitingSpheres";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, FormEvent, PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from "react";
import {
  BarChart3,
  Camera,
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  Eye,
  EyeOff,
  ExternalLink,
  FileSpreadsheet,
  FileDown,
  ClipboardList,
  Grid2X2,
  Info,
  KeyRound,
  LogIn,
  LogOut,
  PackageSearch,
  PanelLeftDashed,
  PanelLeft,
  Plug,
  RefreshCw,
  Search,
  Scale,
  Settings,
  Shield,
  SlidersHorizontal,
  ShieldCheck,
  SquareCheckBig,
  Trash2,
  Truck,
  Upload,
  UserPlus,
  UserRound,
  MapPin,
  Wrench,
  X,
} from "lucide-react";
import SlidingTabs, { type AnalyticsView } from "../SlidingTabs";
import { StyledSelect } from "../StyledSelect";
import SkuMetadataEditor from "../SkuMetadataEditor";
import SkuPickerDialog from "../SkuPickerDialog";
import BrowserAccountConnection from "../BrowserAccountConnection";
import PostalCodesDialog from "../PostalCodesDialog";
import ExtractionStoreSelect from "../ExtractionStoreSelect";
import PaginationFooter from "../PaginationFooter";
import { LOCAL_DATA_CHANGED } from "../../src/localData";
import ExtractionProgressToast from "../ExtractionProgressToast";
import ExtractionElapsedTime from "../ExtractionElapsedTime";
import SpreadsheetInput from "../SpreadsheetInput";
import { downloadExcel } from "../../src/download";
import type {
  ActiveExtractionJobResponse,
  AuthLoginResponse,
  AuthUser,
  AuthUsersResponse,
  ComparisonRow,
  CrawlerControlSettings,
  ExtractionJob,
  IntegrationProviderSettings,
  IntegrationSettingsResponse,
  ProductSourceItem,
  ProductSourceResponse,
  SkuImportResponse,
  SkuStatusItem,
  SkuStatusResponse,
} from "../../models/product.model";
import useHeroSection, { getApiBaseUrl } from "./viewModel";
import logoUrl from "../../static/logo.png";
import logoWhiteUrl from "../../static/logowhite.png";
import {
  getInterfacePreferences,
  regionFormatOptions,
  saveInterfacePreferences,
  setPreferenceUser,
  type InterfacePreferences,
} from "../../src/preferences";

const LeadTimeView = lazy(() => import("../LeadTimeView"));
const defaultSearchPostals = ["01001000"];
const FreightMap = lazy(() => import("../FreightMap"));
const AnalysisReports = lazy(() => import("../AnalysisReports"));
const viewLoading = <div className="freight-map__empty" role="status" aria-label="Carregando"><OrbitingSpheres size={24} /></div>;

const AUTH_TOKEN_STORAGE_KEY = "nevetracker-auth-token";
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
  { tab: 0, label: "Ofertas", icon: PackageSearch },
  { tab: 1, label: "Catálogo", icon: Grid2X2 },
  { tab: 2, label: "Analytics", icon: BarChart3 },
  { tab: 4, label: "Lead Time", icon: Truck },
  { tab: 3, label: "Database", icon: FileSpreadsheet },
];

const analyticsViews = [
  { key: "overview", label: "Visão geral" },
  { key: "comparison", label: "Comparar lojas" },
  { key: "history", label: "Histórico de preços" },
] as const;

type UserRole = AuthUser["role"];
type ExtractionTab = "extract" | "database" | "schedule" | "control" | "metadata";
type ExtractionMode = "quick" | "precise";
type SettingsTab = "general" | "about";
type AdminEditorTab = "access" | "profile";

const defaultCrawlerControl: CrawlerControlSettings = {
  default_mode: "quick",
  api_with_scraping: true,
  external_discovery_fallback: true,
  playwright_product_fallback: true,
  playwright_shipping_fallback: true,
  concurrent_requests_per_domain: 2,
  request_timeout_seconds: 15,
  precise_sku_workers: 8,
  quick_sku_workers: 4,
  quote_workers: 12,
  quick_timeout_seconds: 5,
  discovery_stores_per_round: 3,
  adaptive_parallelism: true,
  hardware_acceleration: false,
  reuse_public_browser: true,
  browser_failure_cooldown_seconds: 30,
};
const ADMIN_USERS_PAGE_SIZE = 6;
const AVATAR_CROP_PREVIEW_SIZE = 320;

const resolvedUserRole = (user?: AuthUser | null): UserRole => {
  if (!user) return "viewer";
  if (user.role) return user.role;
  if (user.is_owner) return "developer";
  return user.is_admin ? "governor" : "viewer";
};

const hasFullAccess = (role: UserRole) => role === "developer" || role === "administrator";

const userRoleLabel = (user: AuthUser) => {
  const role = resolvedUserRole(user);
  if (hasFullAccess(role)) return "Administrador";
  if (role === "governor") return "Governante";
  return "Visualizador";
};

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
  if (job.status === "waiting_access") {
    return job.waiting_access?.some(access => access.status === "waiting")
      ? "Aguardando verificação de acesso"
      : "Aguardando resposta das lojas";
  }
  if (job.status === "paused") return "Extração pausada";
  if (job.status === "completed") return "Extração concluída";
  if (job.status === "completed_with_errors") return "Concluída com ocorrências";
  if (job.status === "cancelled") return "Fila cancelada";
  return "Falha na extração";
};

type ScheduledExtractionSettings = {
  enabled: boolean;
  onlyStale: boolean;
  time: string;
  endTime: string;
  days: number[];
  repeatEveryHours: number;
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
  [1, "segunda"],
  [2, "terça"],
  [3, "quarta"],
  [4, "quinta"],
  [5, "sexta"],
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
  onlyStale: false,
  time: "09:00",
  endTime: "18:00",
  days: [1],
  repeatEveryHours: 0,
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
      onlyStale: Boolean(parsed.onlyStale),
      time: typeof parsed.time === "string" && /^\d{2}:\d{2}$/.test(parsed.time) ? parsed.time : "09:00",
      endTime: typeof parsed.endTime === "string" && /^\d{2}:\d{2}$/.test(parsed.endTime) ? parsed.endTime : "18:00",
      days: days.length ? days : defaultScheduleSettings.days,
      repeatEveryHours,
    };
  } catch {
    return defaultScheduleSettings;
  }
};

const parseSkuInput = (value: string) => {
  const candidates = value
    .normalize("NFKC")
    .toUpperCase()
    .split(/[\r\n\t,;]+/)
    .map((item) => item.trim())
    .flatMap((item) => {
      const words = item.split(/\s+/);
      return words.length > 1 && words.every((word) => /\d/.test(word) && /^[A-Z0-9._/-]{2,80}$/.test(word)) ? words : [item];
    })
    .filter((item) => /^[A-Z0-9._/ -]{2,80}$/.test(item));
  return [...new Set(candidates)];
};

const parseApiDateTime = (value: string) =>
  new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`);

const dateTimeFormat = (month: "short" | "2-digit") => new Intl.DateTimeFormat("pt-BR", {
  day: "2-digit", month, year: "2-digit", hour: "2-digit", minute: "2-digit",
});
const fullDateTime = dateTimeFormat("short");
const compactDateTime = dateTimeFormat("2-digit");

const formatDateTime = (value?: string | null) => {
  if (!value) return "Nunca";
  const date = parseApiDateTime(value);
  if (Number.isNaN(date.getTime())) return "Nunca";
  return fullDateTime.format(date);
};

const formatCompactDateTime = (value?: string | null) => {
  if (!value) return "Sem execução";
  const date = parseApiDateTime(value);
  if (Number.isNaN(date.getTime())) return "Sem execução";
  return compactDateTime.format(date);
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
  const [analyticsMode, setAnalyticsMode] = useState<AnalyticsView>("overview");
  const [analyticsMenuOpen, setAnalyticsMenuOpen] = useState(false);
  const [selectedAnalyticsRow, setSelectedAnalyticsRow] = useState<ComparisonRow | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [integrationsOpen, setIntegrationsOpen] = useState(false);
  const [extractionsOpen, setExtractionsOpen] = useState(false);
  const [administrationOpen, setAdministrationOpen] = useState(false);
  const [adminEditorOpen, setAdminEditorOpen] = useState(false);
  const [skuSelectorOpen, setSkuSelectorOpen] = useState(false);
  const [skuSelectorTarget, setSkuSelectorTarget] = useState<"search" | "extraction">("search");
  const [accountOpen, setAccountOpen] = useState(false);
  const [loginUser, setLoginUser] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [authLoading, setAuthLoading] = useState(false);
  const [showLoginPassword, setShowLoginPassword] = useState(false);
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  const extractionJobStorageKey = `${EXTRACTION_JOB_STORAGE_KEY}:${currentUser?.id ?? "guest"}`;
  const [adminUsers, setAdminUsers] = useState<AuthUser[]>([]);
  const [selectedAdminUser, setSelectedAdminUser] = useState<AuthUser | null>(null);
  const [adminUsername, setAdminUsername] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  const [adminRole, setAdminRole] = useState<UserRole>("viewer");
  const [adminLoading, setAdminLoading] = useState(false);
  const [adminSaving, setAdminSaving] = useState(false);
  const [adminError, setAdminError] = useState("");
  const [adminSearch, setAdminSearch] = useState("");
  const [adminPage, setAdminPage] = useState(1);
  const [showAdminPassword, setShowAdminPassword] = useState(false);
  const [adminDeleteTarget, setAdminDeleteTarget] = useState<AuthUser | null>(null);
  const [adminDeleteError, setAdminDeleteError] = useState("");
  const [adminAvatarDraft, setAdminAvatarDraft] = useState<string | null | undefined>(undefined);
  const [adminEditorTab, setAdminEditorTab] = useState<AdminEditorTab>("access");
  const [avatarCropOpen, setAvatarCropOpen] = useState(false);
  const [avatarCropSource, setAvatarCropSource] = useState("");
  const [avatarCropZoom, setAvatarCropZoom] = useState(1);
  const [avatarCropX, setAvatarCropX] = useState(0);
  const [avatarCropY, setAvatarCropY] = useState(0);
  const [avatarCropError, setAvatarCropError] = useState("");
  const [avatarImageSize, setAvatarImageSize] = useState({ width: 0, height: 0 });
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => parseTheme(window.localStorage.getItem(THEME_STORAGE_KEY)));
  const [theme, setTheme] = useState(() => resolveTheme(themeMode));
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("general");
  const [extractionTab, setExtractionTab] = useState<ExtractionTab>("extract");
  const [metadataFooter, setMetadataFooter] = useState<HTMLDivElement | null>(null);
  const [scheduleSettings, setScheduleSettings] = useState<ScheduledExtractionSettings>(getInitialScheduleSettings);
  const [scheduleSaving, setScheduleSaving] = useState(false);
  const [scheduleError, setScheduleError] = useState("");
  const [interfacePreferences, setInterfacePreferences] = useState<InterfacePreferences>(getInterfacePreferences);
  const [integrationProviders, setIntegrationProviders] = useState<IntegrationProviderSettings[]>([]);
  const [activeIntegrationProvider, setActiveIntegrationProvider] = useState<string | null>(null);
  const [integrationKeys, setIntegrationKeys] = useState<Record<string, string>>({});
  const [integrationsLoading, setIntegrationsLoading] = useState(false);
  const [integrationsSaving, setIntegrationsSaving] = useState(false);
  const [integrationsError, setIntegrationsError] = useState("");
  const [skuInput, setSkuInput] = useState("");
  const [extractionMode, setExtractionMode] = useState<ExtractionMode>("quick");
  const [crawlerControl, setCrawlerControl] = useState<CrawlerControlSettings>(defaultCrawlerControl);
  const [crawlerSources, setCrawlerSources] = useState<ProductSourceItem[]>([]);
  const [crawlerSourcesHistoryOpen, setCrawlerSourcesHistoryOpen] = useState(false);
  const [sourcePage, setSourcePage] = useState(1);
  const [sourcePageSize, setSourcePageSize] = useState(() => Math.max(1, Math.floor((Math.min(710, window.innerHeight - 48) - 190) / 42)));
  useEffect(() => {
    const resize = () => { setSourcePageSize(Math.max(1, Math.floor((Math.min(710, window.innerHeight - 48) - 190) / 42))); setSourcePage(1); };
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const [sourceTotal, setSourceTotal] = useState(0);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState("");
  const [sourceRevision, setSourceRevision] = useState(0);
  const [crawlerActiveCount, setCrawlerActiveCount] = useState(0);
  const [crawlerInvalidCount, setCrawlerInvalidCount] = useState(0);
  const [crawlerControlLoading, setCrawlerControlLoading] = useState(false);
  const [crawlerControlSaving, setCrawlerControlSaving] = useState(false);
  const [rediscoveringSku, setRediscoveringSku] = useState<string | null>(null);
  const [skuStatusItems, setSkuStatusItems] = useState<SkuStatusItem[]>([]);
  const skuStatusRequest = useRef<AbortController | null>(null);
  const [latestExtractionAt, setLatestExtractionAt] = useState<string | null>(null);
  const [databaseItems,setDatabaseItems]=useState<SkuStatusItem[]>([]);
  const [databasePage,setDatabasePage]=useState(1);
  const [databaseTotal,setDatabaseTotal]=useState(0);
  const [databaseLoading,setDatabaseLoading]=useState(true);
  const [databaseRevision,setDatabaseRevision]=useState(0);
  const [databaseTransferBusy,setDatabaseTransferBusy]=useState(false);
  const databaseImportRef=useRef<HTMLInputElement>(null);
  const [extractionStores,setExtractionStores]=useState<string[]>([]);
  const [extractionSellers,setExtractionSellers]=useState<SellerSelection>({});
  const [exportFrom,setExportFrom]=useState("");
  const [exportTo,setExportTo]=useState("");
  const [searchPostals,setSearchPostals]=useState<string[]>([]);
  const [postalDialogOpen,setPostalDialogOpen]=useState(false);
  const [skuDatabaseFilter, setSkuDatabaseFilter] = useState("");
  const [databaseQuery, setDatabaseQuery] = useState("");
  const [deletingSku, setDeletingSku] = useState<string | null>(null);
  const [extractionRunning, setExtractionRunning] = useState(false);
  const [extractionProgress, setExtractionProgress] = useState("");
  const [extractionPostals, setExtractionPostals] = useState("");
  const [exportingComplete, setExportingComplete] = useState(false);
  const [activeExtractionJob, setActiveExtractionJob] = useState<ExtractionJob | null>(null);
  const extractionHasActiveJob=Boolean(activeExtractionJob&&isExtractionJobActive(activeExtractionJob));
  const extractionDialogMessage = activeExtractionJob?.message?.replace(/;\s*CEP\s*\d{5}-?\d{3}\s*$/i, "");
  const [accessActionBusy, setAccessActionBusy] = useState<string | null>(null);
  const [accessActionMessage, setAccessActionMessage] = useState("");
  const [extractionConnectionLost, setExtractionConnectionLost] = useState(false);
  const [cancelRequest, setCancelRequest] = useState<{ jobId: string; pending: boolean; error?: string } | null>(null);
  const [extractionError, setExtractionError] = useState("");
  const [skuImporting, setSkuImporting] = useState(false);
  const [skuImportSummary, setSkuImportSummary] = useState<SkuImportResponse | null>(null);
  const [refreshingSku, setRefreshingSku] = useState<string | null>(null);
  const [refreshingAllSkus, setRefreshingAllSkus] = useState(false);
  const [selectedSearchSkus, setSelectedSearchSkus] = useState<string[]>([]);
  const accountRef = useRef<HTMLDivElement>(null);
  const skuFileInputRef = useRef<HTMLInputElement>(null);
  const avatarFileInputRef = useRef<HTMLInputElement>(null);
  const avatarDragRef = useRef<{ pointerId: number; clientX: number; clientY: number; x: number; y: number } | null>(null);
  const themeTransitionTimerRef = useRef<number | null>(null);
  const {
    productName,
    products,
    comparisonRows,
    loading,
    lastQuery,
    searchCleared,
    searchError,
    setProductName,
    handleSearch,
  } = useHeroSection(searchPostals.length ? searchPostals : defaultSearchPostals);

  useEffect(() => {
    legacyStorageKeys("auth-user").forEach((key) => window.localStorage.removeItem(key));
    const token = getAuthToken();
    if (!token) return;

    let active = true;
    let retryTimer: number | undefined;
    const restoreUser = async () => {
      try {
        const response = await fetch(`${getApiBaseUrl()}/auth/me`, { headers: authorizedHeaders() });
        if (!active) return;
        if (response.status === 401 || response.status === 403) {
          window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
          setCurrentUser(null);
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const user = await response.json() as AuthUser;
        if (active) setCurrentUser(user);
      } catch {
        if (active) retryTimer = window.setTimeout(() => void restoreUser(), 3000);
      }
    };
    void restoreUser();

    return () => {
      active = false;
      window.clearTimeout(retryTimer);
    };
  }, []);

  useEffect(() => {
    const closeMenus = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented && !document.querySelector("dialog:modal")) {
        if (accountRef.current?.querySelector(".account-popover")) {
          accountRef.current.querySelector<HTMLButtonElement>("button[aria-expanded]")?.focus();
        }
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
    if (activeTab === 4) return;
    if (activeTab !== 2) setActiveTab(0);
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

  useEffect(() => {
    if (themeMode !== "system") return;
    const media = window.matchMedia(themeMedia);
    const sync = () => {
      const next = resolveTheme(themeMode);
      document.documentElement.dataset.theme = next;
      setTheme(next);
    };
    media.addEventListener("change", sync);
    sync();
    return () => media.removeEventListener("change", sync);
  }, [themeMode]);

  const updateTheme = (nextTheme: ThemeMode) => {
    if (nextTheme === themeMode) return;

    const root = document.documentElement;
    root.classList.add("theme-transition");
    root.dataset.theme = resolveTheme(nextTheme);
    window.localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
    setThemeMode(nextTheme);
    setTheme(resolveTheme(nextTheme));

    if (themeTransitionTimerRef.current !== null) {
      window.clearTimeout(themeTransitionTimerRef.current);
    }
    themeTransitionTimerRef.current = window.setTimeout(() => {
      root.classList.remove("theme-transition");
      themeTransitionTimerRef.current = null;
    }, 220);
  };

  useEffect(() => {
    setInterfacePreferences(setPreferenceUser(currentUser?.id ?? null, currentUser?.logistics_postal_code || (currentUser ? "01001000" : undefined)));
  }, [currentUser?.id]);

  const updateInterfacePreferences = (updater: (currentPreferences: InterfacePreferences) => InterfacePreferences) => {
    const nextPreferences = updater(interfacePreferences);
    setInterfacePreferences(nextPreferences);
    saveInterfacePreferences(nextPreferences);

  };

  useEffect(() => () => skuStatusRequest.current?.abort(), []);
  const loadSkuStatus = async () => {
    skuStatusRequest.current?.abort();
    const controller = new AbortController();
    skuStatusRequest.current = controller;
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/skus/status`, { signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = (await response.json()) as SkuStatusResponse;
      if (!controller.signal.aborted) setSkuStatusItems(data.items || []);
    } catch (error) {
      if (controller.signal.aborted) return;
      console.error("Erro ao carregar SKUs:", error);
      setExtractionError("Não foi possível carregar os SKUs do banco.");
    } finally {
      if (!controller.signal.aborted) {
        skuStatusRequest.current = null;
      }
    }
  };

  useEffect(()=>{
    try {setSearchPostals(JSON.parse(window.localStorage.getItem(`nevetracker-search-postals:${currentUser?.id}`)||'[]'));} catch {setSearchPostals([]);}
  },[currentUser?.id]);

  useEffect(() => {
    if (skuDatabaseFilter === databaseQuery) return;
    const apply = () => { setDatabaseQuery(skuDatabaseFilter); setDatabasePage(1); };
    if (!skuDatabaseFilter) { apply(); return; }
    const timer = window.setTimeout(apply, 250);
    return () => window.clearTimeout(timer);
  }, [skuDatabaseFilter, databaseQuery]);

  useEffect(()=>{
    if(!extractionsOpen || extractionTab!=="database")return;
    setDatabaseLoading(true);
    const controller=new AbortController();
      const params=new URLSearchParams({page:String(databasePage),page_size:'50',q:databaseQuery});
      fetch(`${getApiBaseUrl()}/products/skus/status?${params}`,{signal:controller.signal,headers:authorizedHeaders()})
        .then(async response=>{if(!response.ok)throw new Error('Falha ao carregar materiais.');return response.json();})
        .then(data=>{if(controller.signal.aborted)return;setDatabaseItems(data.items||[]);setDatabaseTotal(data.total||0);if(databasePage>Math.max(1,Math.ceil(data.total/50)))setDatabasePage(Math.max(1,Math.ceil(data.total/50)));})
        .catch(error=>{if(!controller.signal.aborted)setExtractionError(String(error));})
        .finally(()=>{if(!controller.signal.aborted)setDatabaseLoading(false);});
    return()=>controller.abort();
  },[extractionsOpen,extractionTab,databasePage,databaseQuery,databaseRevision]);

  const loadCrawlerControl = async () => {
    setCrawlerControlLoading(true);
    try {
      const controlResponse = await fetch(`${getApiBaseUrl()}/products/crawler-control`, {
        headers: authorizedHeaders(),
      });
      if (!controlResponse.ok) throw new Error("Falha ao carregar o motor de coleta.");
      const { last_extraction_at, ...control } = await controlResponse.json() as CrawlerControlSettings & { last_extraction_at?: string | null };
      const settings = { ...defaultCrawlerControl, ...control };
      setLatestExtractionAt(last_extraction_at ?? null);
      setCrawlerControl(settings);
      setExtractionMode(settings.default_mode);
      if (hasFullAccess(resolvedUserRole(currentUser))) {
        const sourceResponse = await fetch(`${getApiBaseUrl()}/products/sources?page=1&page_size=1`, {
          headers: authorizedHeaders(),
        });
        if (!sourceResponse.ok) throw new Error("Falha ao carregar as fontes recentes.");
        const sources = await sourceResponse.json() as ProductSourceResponse;
        setCrawlerActiveCount(sources.active_count || 0);
        setCrawlerInvalidCount(sources.invalid_count || 0);
      }
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Falha ao carregar o motor de coleta.");
    } finally {
      setCrawlerControlLoading(false);
    }
  };



  const saveCrawlerControl = async (settings = crawlerControl) => {
    if (crawlerControlSaving) return;
    setCrawlerControlSaving(true);
    setExtractionError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/crawler-control`, {
        method: "PUT",
        headers: authorizedHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(settings),
      });
      const data = await response.json().catch(() => null) as CrawlerControlSettings | { detail?: string } | null;
      if (!response.ok || !data || !("default_mode" in data)) {
        throw new Error(apiErrorMessage(data, "Não foi possível salvar o controle de coleta."));
      }
      if (typeof data.hardware_acceleration !== "boolean" && settings.hardware_acceleration) {
        throw new Error("O backend em execução é de uma versão anterior à aceleração. Reinicie o backend e salve novamente.");
      }
      if (typeof data.hardware_acceleration === "boolean" && data.hardware_acceleration !== settings.hardware_acceleration) {
        throw new Error("O servidor não confirmou a alteração da aceleração. Tente salvar novamente.");
      }
      setCrawlerControl({ ...defaultCrawlerControl, ...data });
      setExtractionMode(data.default_mode);
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Não foi possível salvar o controle de coleta.");
    } finally {
      setCrawlerControlSaving(false);
    }
  };

  const rediscoverSourceSku = async (sku: string) => {
    if (extractionRunning || rediscoveringSku) return;
    setRediscoveringSku(sku);
    setExtractionError("");
    try {
      const response = await fetch(
        `${getApiBaseUrl()}/products/sources/${encodeURIComponent(sku)}/rediscover`,
        { method: "POST", headers: authorizedHeaders() },
      );
      const data = await response.json().catch(() => null) as ExtractionJob | { detail?: string } | null;
      if (!response.ok || !data || !("id" in data)) {
        throw new Error(apiErrorMessage(data, "Não foi possível redescobrir o SKU."));
      }
      setActiveExtractionJob(data);
      setExtractionRunning(true);
      window.localStorage.setItem(extractionJobStorageKey, data.id);
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Não foi possível redescobrir o SKU.");
    } finally {
      setRediscoveringSku(null);
    }
  };

  const loadIntegrationSettings = async () => {
    if (!hasFullAccess(resolvedUserRole(currentUser))) return;
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
      setActiveIntegrationProvider((currentProvider) => (
        currentProvider === "shopee" || data.items.some((provider) => provider.provider === currentProvider)
          ? currentProvider
          : data.items[0]?.provider || null
      ));
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
    setSettingsTab("general");
    setSettingsOpen(true);
  };

  const openIntegrations = () => {
    if (!hasFullAccess(resolvedUserRole(currentUser))) return;
    setAccountOpen(false);
    setSidebarOpen(false);
    setIntegrationsOpen(true);
    void loadIntegrationSettings();
  };

  const openExtractions = () => {
    if (!currentUser || resolvedUserRole(currentUser) === "viewer") return;
    setAccountOpen(false);
    setSidebarOpen(false);
    setExtractionError("");
    setSkuImportSummary(null);
    setSkuDatabaseFilter("");
    setExtractionTab("extract");
    setExtractionsOpen(true);
    void loadCrawlerControl();
    void fetch(`${getApiBaseUrl()}/products/schedule`, { headers: authorizedHeaders() })
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!data) return;
        setScheduleSettings((currentSettings) => ({
          ...currentSettings,
          enabled: Boolean(data.enabled),
          onlyStale: Boolean(data.only_stale),
          time: typeof data.start_time === "string" ? data.start_time : currentSettings.time,
          endTime: typeof data.end_time === "string" ? data.end_time : currentSettings.endTime,
          days: Array.isArray(data.days) && data.days.length ? data.days : currentSettings.days,
          repeatEveryHours: Math.min(12, Math.max(0, Math.trunc(Number(data.repeat_every_hours) || 0))),
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
    setAdminRole("viewer");
    setAdminError("");
    setShowAdminPassword(false);
    setAdminDeleteTarget(null);
    setAdminDeleteError("");
    setAdminAvatarDraft(undefined);
    setAdminEditorTab("access");
    setAvatarCropOpen(false);
    setAvatarCropSource("");
    setAvatarCropError("");
  };

  const openAdministration = () => {
    if (!currentUser || resolvedUserRole(currentUser) === "viewer") return;
    setAccountOpen(false);
    setSidebarOpen(false);
    resetAdminEditor();
    setAdminEditorOpen(false);
    setAdminSearch("");
    setAdminPage(1);
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
    setAdminRole(resolvedUserRole(user));
    setAdminError("");
    setShowAdminPassword(false);
    setAdminDeleteTarget(null);
    setAdminDeleteError("");
    setAdminAvatarDraft(undefined);
    setAdminEditorTab("access");
    setAdminEditorOpen(true);
  };

  const selectAvatarFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
      setAdminError("Use uma imagem JPG, PNG ou WebP.");
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      setAdminError("A imagem original deve ter no máximo 8 MB.");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      setAvatarCropSource(String(reader.result || ""));
      setAvatarCropZoom(1);
      setAvatarCropX(0);
      setAvatarCropY(0);
      setAvatarImageSize({ width: 0, height: 0 });
      setAvatarCropError("");
      setAvatarCropOpen(true);
    };
    reader.onerror = () => setAdminError("Não foi possível ler a imagem.");
    reader.readAsDataURL(file);
  };

  const applyAvatarCrop = async () => {
    if (!avatarCropSource) return;
    try {
      const image = new Image();
      image.src = avatarCropSource;
      await image.decode();
      const size = 256;
      const canvas = document.createElement("canvas");
      canvas.width = size;
      canvas.height = size;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Canvas indisponível.");
      const scale = Math.max(size / image.naturalWidth, size / image.naturalHeight) * avatarCropZoom;
      const width = image.naturalWidth * scale;
      const height = image.naturalHeight * scale;
      const previewScale = size / AVATAR_CROP_PREVIEW_SIZE;
      const left = (size - width) / 2 + avatarCropX * previewScale;
      const top = (size - height) / 2 + avatarCropY * previewScale;
      context.drawImage(image, left, top, width, height);
      setAdminAvatarDraft(canvas.toDataURL("image/jpeg", 0.86));
      setAvatarCropOpen(false);
      setAvatarCropSource("");
      setAdminError("");
    } catch {
      setAvatarCropError("Não foi possível processar esta imagem.");
    }
  };

  const clampAvatarPosition = (x: number, y: number, zoom = avatarCropZoom) => {
    if (!avatarImageSize.width || !avatarImageSize.height) return { x, y };
    const previewSize = AVATAR_CROP_PREVIEW_SIZE;
    const baseScale = Math.max(previewSize / avatarImageSize.width, previewSize / avatarImageSize.height);
    const maxX = Math.max(0, (avatarImageSize.width * baseScale * zoom - previewSize) / 2);
    const maxY = Math.max(0, (avatarImageSize.height * baseScale * zoom - previewSize) / 2);
    return {
      x: Math.max(-maxX, Math.min(maxX, x)),
      y: Math.max(-maxY, Math.min(maxY, y)),
    };
  };

  const startAvatarDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    avatarDragRef.current = {
      pointerId: event.pointerId,
      clientX: event.clientX,
      clientY: event.clientY,
      x: avatarCropX,
      y: avatarCropY,
    };
  };

  const moveAvatarDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = avatarDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const position = clampAvatarPosition(
      drag.x + event.clientX - drag.clientX,
      drag.y + event.clientY - drag.clientY,
    );
    setAvatarCropX(position.x);
    setAvatarCropY(position.y);
  };

  const stopAvatarDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (avatarDragRef.current?.pointerId === event.pointerId) avatarDragRef.current = null;
  };

  const zoomAvatarCrop = (event: ReactWheelEvent<HTMLDivElement>) => {
    event.preventDefault();
    const nextZoom = Math.max(1, Math.min(3, avatarCropZoom + (event.deltaY < 0 ? 0.12 : -0.12)));
    const position = clampAvatarPosition(avatarCropX, avatarCropY, nextZoom);
    setAvatarCropZoom(nextZoom);
    setAvatarCropX(position.x);
    setAvatarCropY(position.y);
  };

  const saveAdminUser = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (adminSaving) return;

    const editingUser = selectedAdminUser;
    const payload: { username?: string; password?: string; role?: UserRole; avatar_data_url?: string | null } = {};
    if (editingUser) {
      if (adminPassword) payload.password = adminPassword;
      if (
        hasFullAccess(resolvedUserRole(currentUser)) &&
        !editingUser.is_owner &&
        adminRole !== resolvedUserRole(editingUser)
      ) {
        payload.role = adminRole;
      }
      if (!(!hasFullAccess(resolvedUserRole(currentUser)) && resolvedUserRole(editingUser) !== "viewer" && editingUser.id !== currentUser?.id) && adminAvatarDraft !== undefined) payload.avatar_data_url = adminAvatarDraft;
      if (!Object.keys(payload).length) {
        setAdminError("Altere a foto, a senha ou as permissões.");
        return;
      }
    } else {
      payload.username = adminUsername.trim().toUpperCase();
      payload.password = adminPassword;
      payload.role = hasFullAccess(resolvedUserRole(currentUser)) ? adminRole : "viewer";
      if (adminAvatarDraft !== undefined) payload.avatar_data_url = adminAvatarDraft;
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
      if (editingUser?.id === currentUser?.id) setCurrentUser(data);

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
    setScheduleError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/schedule`, {
        method: "PUT",
        headers: authorizedHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          enabled: scheduleSettings.enabled,
          start_time: scheduleSettings.time,
          end_time: scheduleSettings.endTime,
          days: scheduleSettings.days,
          repeat_every_hours: scheduleSettings.repeatEveryHours,
          only_stale: scheduleSettings.onlyStale,
          force_refresh: true,
          postal_code: "01001000",
        }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } catch (error) {
      console.error("Erro ao salvar programação:", error);
      setScheduleError("Não foi possível salvar o agendamento. Tente novamente.");
    } finally {
      setScheduleSaving(false);
    }
  };

  const runExtractions = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const skus = parseSkuInput(skuInput);
    const postals = [...new Set(extractionPostals.split(/[\s,;]+/).filter(Boolean))];
    if (postals.some((value) => !/^\d{5}-?\d{3}$/.test(value))) {
      setExtractionError("Informe CEPs com oito dígitos, separados por linha, espaço ou vírgula.");
      return;
    }

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

    try {
      const response = await fetch(`${getApiBaseUrl()}/products/extraction-jobs`, {
        method: "POST",
        headers: authorizedHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          skus,
          postal_codes: postals,
          stores: extractionStores,
          sellers: Object.fromEntries(Object.entries(extractionSellers).filter(([store]) => !extractionStores.length || extractionStores.includes(store))),
          force_refresh: true,
          refresh_if_stale: false,
          postal_code: "01001000",
          scope: "all",
          mode: extractionMode,
        }),
      });
      const data = await response.json().catch(() => null) as ExtractionJob | { detail?: string } | null;
      if (!response.ok || !data || !("id" in data)) {
        throw new Error(apiErrorMessage(data, `HTTP ${response.status}`));
      }
      setActiveExtractionJob(data);
      setExtractionProgress(`${data.processed_skus} de ${data.total_skus}`);
      window.localStorage.setItem(extractionJobStorageKey, data.id);
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
        headers: authorizedHeaders(),
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
        headers: authorizedHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          skus: [sku],
          force_refresh: true,
          postal_code: "01001000",
          scope: "all",
          mode: extractionMode,
        }),
      });
      const data = await response.json().catch(() => null) as ExtractionJob | { detail?: string } | null;
      if (!response.ok || !data || !("id" in data)) {
        throw new Error(apiErrorMessage(data, `HTTP ${response.status}`));
      }
      setActiveExtractionJob(data);
      window.localStorage.setItem(extractionJobStorageKey, data.id);
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
        { method: "DELETE", headers: authorizedHeaders() },
      );
      const data = await response.json().catch(() => null) as { detail?: string } | null;
      if (!response.ok) {
        throw new Error(apiErrorMessage(data, "Não foi possível excluir o SKU."));
      }
      setSkuStatusItems((items) => items.filter((item) => item.sku !== sku));
      setDatabaseRevision(value=>value+1);
      window.dispatchEvent(new CustomEvent(LOCAL_DATA_CHANGED,{detail:{sku}}));
      setSelectedSearchSkus((items) => items.filter((item) => item !== sku));
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Não foi possível excluir o SKU.");
    } finally {
      setDeletingSku(null);
    }
  };

  const refreshAllSkus = async () => {
    if (!databaseTotal || refreshingAllSkus || extractionRunning || (activeExtractionJob && isExtractionJobActive(activeExtractionJob))) return;

    setRefreshingAllSkus(true);
    setExtractionRunning(true);
    setExtractionProgress(`0 de ${databaseTotal}`);
    setExtractionError("");

    try {
      const response = await fetch(`${getApiBaseUrl()}/products/extraction-jobs/database`, {
        method: "POST",
        headers: authorizedHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          force_refresh: true,
          refresh_if_stale: false,
          postal_code: "01001000",
          scope: "all",
          mode: extractionMode,
        }),
      });
      const data = await response.json().catch(() => null) as ExtractionJob | { detail?: string } | null;
      if (!response.ok || !data || !("id" in data)) {
        throw new Error(apiErrorMessage(data, `HTTP ${response.status}`));
      }
      setActiveExtractionJob(data);
      window.localStorage.setItem(extractionJobStorageKey, data.id);
    } catch (error) {
      setExtractionError(error instanceof Error ? error.message : "Falha ao atualizar o banco local.");
      setRefreshingAllSkus(false);
      setExtractionRunning(false);
      setExtractionProgress("");
    }
  };

  const exportComplete = async () => {
    if (exportingComplete) return;
    setExportingComplete(true); setExtractionError("");
    const params = new URLSearchParams();
    if (activeExtractionJob) params.set("job_id",activeExtractionJob.id);
    if (exportFrom) params.set("date_from",exportFrom);
    if (exportTo) params.set("date_to",exportTo);
    try { await downloadExcel(`${getApiBaseUrl()}/analysis/export/all?${params}`, "nevetracker-visoes-completas.xlsx", authorizedHeaders(), 600_000); }
    catch (reason) { setExtractionError(reason instanceof Error ? reason.message : "Falha ao exportar."); }
    finally { setExportingComplete(false); }
  };

  const downloadDatabase = async () => {
    setDatabaseTransferBusy(true);setExtractionError("");
    try {await downloadExcel(`${getApiBaseUrl()}/products/database/backup`,"nevetracker-banco-completo.db",authorizedHeaders());}
    catch(reason){setExtractionError(reason instanceof Error?reason.message:"Falha ao baixar banco.");}
    finally{setDatabaseTransferBusy(false);}
  };

  const importDatabase = async (event:ChangeEvent<HTMLInputElement>) => {
    const file=event.target.files?.[0];event.target.value="";
    if(!file)return;
    if(!window.confirm("Substituir todo o banco pelo arquivo selecionado? Uma cópia de segurança será salva antes da importação. As contas e senhas do arquivo serão usadas; entre novamente após importar."))return;
    setDatabaseTransferBusy(true);setExtractionError("");
    const controller=new AbortController();const timer=window.setTimeout(()=>controller.abort(),300_000);
    try{
      const data=new FormData();data.append("file",file);
      const response=await fetch(`${getApiBaseUrl()}/products/database/restore`,{method:"POST",headers:authorizedHeaders(),body:data,signal:controller.signal});
      const result=await response.json() as {detail?:string};
      if(!response.ok)throw new Error(result.detail||"Falha ao importar banco.");
      handleLogout();window.location.reload();
    }catch(reason){setExtractionError(reason instanceof Error?reason.message:"Falha ao importar banco.");}
    finally{window.clearTimeout(timer);setDatabaseTransferBusy(false);}
  };

  const resolveExtractionAccess = async (store: string, action: "open" | "confirm") => {
    if (!activeExtractionJob || activeExtractionJob.owner_user_id !== currentUser?.id) return;
    setAccessActionBusy(store); setAccessActionMessage("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/extraction-jobs/${activeExtractionJob.id}/access/${store}/${action}`, { method: "POST", headers: authorizedHeaders() });
      const data = await response.json() as { status?: string; message?: string; detail?: string };
      if (!response.ok) throw new Error(data.detail || "Não foi possível verificar o acesso.");
      setAccessActionMessage(store === "shopee" && data.status === "opening" ? "" : data.message || "Verificando acesso na loja.");
    } catch (reason) { setAccessActionMessage(reason instanceof Error ? reason.message : "Falha ao verificar acesso."); }
    finally { setAccessActionBusy(null); }
  };

  const canCancelActiveExtraction = Boolean(currentUser && activeExtractionJob &&
    (activeExtractionJob.owner_user_id === currentUser.id || hasFullAccess(resolvedUserRole(currentUser))));
  const currentCancelRequest = cancelRequest?.jobId === activeExtractionJob?.id ? cancelRequest : null;
  const finishExtraction = useCallback((job: ExtractionJob) => {
    setActiveExtractionJob(job);
    setExtractionRunning(false);
    setExtractionConnectionLost(false);
    setExtractionProgress("");
    setRefreshingAllSkus(false);
    setRefreshingSku(null);
    window.localStorage.removeItem(extractionJobStorageKey);
    setDatabaseRevision(value => value + 1);
  }, [extractionJobStorageKey]);

  const cancelActiveExtraction = async () => {
    if (!activeExtractionJob || !isExtractionJobActive(activeExtractionJob) || !canCancelActiveExtraction ||
        activeExtractionJob.cancel_requested || currentCancelRequest?.pending) return;
    const jobId = activeExtractionJob.id;
    setCancelRequest({ jobId, pending: true });
    try {
      const response = await fetch(
        `${getApiBaseUrl()}/products/extraction-jobs/${jobId}/cancel`,
        { method: "POST", headers: authorizedHeaders(), signal: AbortSignal.timeout(15_000) },
      );
      const data = await response.json().catch(() => null) as ExtractionJob | { detail?: string } | null;
      if (!response.ok || !data || !("id" in data)) {
        throw new Error(apiErrorMessage(data, `HTTP ${response.status}`));
      }
      if (isExtractionJobActive(data)) {
        setActiveExtractionJob(current => current?.id === jobId && isExtractionJobActive(current) ? data : current);
      } else {
        finishExtraction(data);
      }
      setCancelRequest(null);
    } catch (error) {
      const message = error instanceof Error && error.name === "TimeoutError"
        ? "O servidor demorou para confirmar o cancelamento. Confira o estado da extração e tente novamente."
        : error instanceof Error ? error.message : "Falha ao cancelar a fila.";
      setCancelRequest({ jobId, pending: false, error: message });
    }
  };

  useEffect(() => {
    if (!currentUser || resolvedUserRole(currentUser) === "viewer") {
      setActiveExtractionJob(null); setExtractionRunning(false); setRefreshingSku(null); setRefreshingAllSkus(false); setExtractionConnectionLost(false);
      return;
    }
    let disposed = false;
    let retryTimer: number | undefined;

    const restoreJob = async () => {
      const storedJobId = window.localStorage.getItem(extractionJobStorageKey);
      const storedUrl = storedJobId
        ? `${getApiBaseUrl()}/products/extraction-jobs/${storedJobId}`
        : null;

      try {
        let job: ExtractionJob | null = null;
        const activeResponse=await fetch(`${getApiBaseUrl()}/products/extraction-jobs/active`,{headers:authorizedHeaders()});
        if(!activeResponse.ok)throw new Error("Não foi possível consultar a extração em andamento.");
        const activeData=await activeResponse.json() as ActiveExtractionJobResponse;
        job=activeData.job||null;
        if (!job && storedUrl) {
          const storedResponse = await fetch(storedUrl, { headers: authorizedHeaders() });
          if (storedResponse.ok) job = await storedResponse.json() as ExtractionJob;
        }
        if (disposed) return;
        if (!job) {retryTimer=window.setTimeout(()=>void restoreJob(),5000);return;}
        if (job.status === "paused" && !job.cancel_requested && job.owner_user_id === currentUser.id) {
          const resumed = await fetch(`${getApiBaseUrl()}/products/extraction-jobs/${job.id}/resume`, { method: "POST", headers: authorizedHeaders() });
          if (!resumed.ok) throw new Error("Não foi possível retomar a extração pausada.");
          job = await resumed.json() as ExtractionJob;
          if (disposed) return;
        }
        setExtractionConnectionLost(false);
        setActiveExtractionJob(job);
        if (isExtractionJobActive(job)) {
          setExtractionRunning(true);
          setExtractionProgress(job.mode === "precise"
            ? `${job.visited_skus} visitados`
            : `${job.processed_skus} de ${job.total_skus}`);
          setRefreshingAllSkus(job.kind === "database");
          window.localStorage.setItem(extractionJobStorageKey, job.id);
        } else {
          window.localStorage.removeItem(extractionJobStorageKey);
          retryTimer=window.setTimeout(()=>void restoreJob(),5000);
        }
      } catch {
        if (!disposed) retryTimer = window.setTimeout(() => void restoreJob(), 3000);
      }
    };

    void restoreJob();
    return () => {
      disposed = true;
      if (retryTimer) window.clearTimeout(retryTimer);
    };
  }, [currentUser?.id, currentUser?.role,extractionHasActiveJob]);

  useEffect(() => {
    if (!currentUser || resolvedUserRole(currentUser) === "viewer" || !activeExtractionJob || !isExtractionJobActive(activeExtractionJob)) return;
    const jobId = activeExtractionJob.id;
    let disposed = false;
    let timer: number | undefined;

    const pollJob = async () => {
      try {
        const response = await fetch(`${getApiBaseUrl()}/products/extraction-jobs/${jobId}?include_metrics=false`, {
          headers: authorizedHeaders(),
        });
        if (response.status === 401 || response.status === 403) {
          if (!disposed) { setActiveExtractionJob(null); setExtractionRunning(false); setCurrentUser(null); }
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        let job = await response.json() as ExtractionJob;
        if (disposed) return;
        setExtractionConnectionLost(false);
        setExtractionError("");

        if (!isExtractionJobActive(job)) {
          if (job.status === "cancelled") {
            finishExtraction(job);
            return;
          }
          try {
            const completedResponse = await fetch(
              `${getApiBaseUrl()}/products/extraction-jobs/${jobId}?include_items=true`,
              { headers: authorizedHeaders() },
            );
            if (completedResponse.ok) job = await completedResponse.json() as ExtractionJob;
            else setExtractionError("A extração terminou, mas os detalhes do resultado não puderam ser carregados.");
          } catch {
            setExtractionError("A extração terminou, mas os detalhes do resultado não puderam ser carregados.");
          }
          if (disposed) return;
          finishExtraction(job);
          return;
        }
        setActiveExtractionJob(job);
        setExtractionRunning(true);
        setExtractionProgress(job.mode === "precise"
          ? `${job.visited_skus} visitados`
          : `${job.processed_skus} de ${job.total_skus}`);
        setRefreshingAllSkus(job.kind === "database");
      } catch (error) {
        if (!disposed) {
          console.error("Erro ao acompanhar fila de extracao:", error);
          setExtractionConnectionLost(true);
          setExtractionRunning(false);
          setRefreshingAllSkus(false);
          setRefreshingSku(null);
          setExtractionError("Sem conexao com o backend. Aguardando reconexao para consultar a fila.");
        }
      }
      if (!disposed) timer = window.setTimeout(pollJob, 1400);
    };

    timer = window.setTimeout(pollJob, 500);
    return () => {
      disposed = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [activeExtractionJob?.id, activeExtractionJob?.status, activeExtractionJob?.cancel_requested, currentUser?.id, currentUser?.role, finishExtraction]);

  const openSkuSelector = (target: "search" | "extraction" = "search") => {
    setSidebarOpen(false);
    setSkuSelectorTarget(target);
    setSkuSelectorOpen(true);
  };

  const searchSelectedSkus = (skus: string[]) => {
    if (skuSelectorTarget === "extraction") {
      setSkuInput(skus.join("\n"));
      setExtractionError("");
      setSkuSelectorOpen(false);
      return;
    }
    if (loading) return;

    const label = `${skus.length} ${skus.length === 1 ? "SKU selecionado" : "SKUs selecionados"}`;
    setSelectedSearchSkus(skus);
    setProductName(label);
    setSkuSelectorOpen(false);
    if (activeTab === 4) {
      setSidebarOpen(false);
      return;
    }
    if (activeTab !== 2) setActiveTab(0);
    setSidebarOpen(false);
    void handleSearch(skus, label);
  };

  useEffect(() => {
    if (!currentUser && [2, 3, 4].includes(activeTab)) setActiveTab(0);
    if (!hasFullAccess(resolvedUserRole(currentUser)) && ["metadata", "database", "control"].includes(extractionTab)) setExtractionTab("extract");
  }, [currentUser?.id, currentUser?.role, activeTab, extractionTab]);

  const switchTab = (index: number) => {
    setSelectedAnalyticsRow(null);
    setActiveTab(!currentUser && [2, 3, 4].includes(index) ? 0 : index);
    setSidebarOpen(false);
    if (sidebarOpen) setAnalyticsMenuOpen(false);
  };

  const toggleAnalyticsMenu = () => {
    const desktop = window.matchMedia("(min-width: 1024px)").matches;
    if (desktop ? sidebarCollapsed : !sidebarOpen) {
      if (desktop) setSidebarCollapsed(false);
      else setSidebarOpen(true);
      setAnalyticsMenuOpen(true);
      return;
    }
    setAnalyticsMenuOpen(open => !open);
  };

  const toggleSidebar = () => {
    setAnalyticsMenuOpen(false);
    if (window.matchMedia("(min-width: 1024px)").matches) {
      setSidebarCollapsed((collapsed) => !collapsed);
      return;
    }

    setSidebarOpen((open) => !open);
  };

  const normalizedSkuDatabaseFilter = skuDatabaseFilter.trim().toLowerCase();
  const visibleDatabaseSkuItems = databaseItems;
  const visibleAdminUsers = adminUsers.filter((user) => {
    const term = adminSearch.trim().toLowerCase();
    return user.username.toLowerCase().includes(term);
  });
  const adminTotalPages = Math.max(1, Math.ceil(visibleAdminUsers.length / ADMIN_USERS_PAGE_SIZE));
  const effectiveAdminPage = Math.min(adminPage, adminTotalPages);
  const adminPageStart = (effectiveAdminPage - 1) * ADMIN_USERS_PAGE_SIZE;
  const paginatedAdminUsers = visibleAdminUsers.slice(
    adminPageStart,
    adminPageStart + ADMIN_USERS_PAGE_SIZE,
  );
  const adminVisiblePageCount = Math.min(5, adminTotalPages);
  const adminFirstVisiblePage = Math.max(
    1,
    Math.min(effectiveAdminPage - 2, adminTotalPages - adminVisiblePageCount + 1),
  );
  const adminVisiblePages = Array.from(
    { length: adminVisiblePageCount },
    (_, index) => adminFirstVisiblePage + index,
  );
  const adminAvatarValue = adminAvatarDraft === undefined
    ? selectedAdminUser?.avatar_data_url
    : adminAvatarDraft;
  const adminAvatarName = selectedAdminUser?.username || adminUsername;
  const scheduleSummary = getScheduleSummary(scheduleSettings);
  const currentUserRole = resolvedUserRole(currentUser);
  const canUseExtractions = Boolean(currentUser && currentUserRole !== "viewer");
  useEffect(() => {
    if (!extractionsOpen || !crawlerSourcesHistoryOpen || !canUseExtractions) return;
    const controller = new AbortController();
    setSourceLoading(true);
    setSourceError("");
    const timer = window.setTimeout(() => { controller.abort(); setSourceError("Tempo esgotado ao carregar fontes. Tente atualizar."); setSourceLoading(false); }, 20_000);
    fetch(`${getApiBaseUrl()}/products/sources?page=${sourcePage}&page_size=${sourcePageSize}`, { headers: authorizedHeaders(), signal: controller.signal })
      .then(async (response) => { if (!response.ok) throw new Error("Falha ao carregar as fontes recentes."); return response.json() as Promise<ProductSourceResponse>; })
      .then((result) => { if (controller.signal.aborted) return; setCrawlerSources(result.items); setSourceTotal(result.total); setSourcePage(result.page); setCrawlerActiveCount(result.active_count); setCrawlerInvalidCount(result.invalid_count); })
      .catch((error: unknown) => { if (!controller.signal.aborted) setSourceError(error instanceof Error ? error.message : "Falha ao carregar fontes."); })
      .finally(() => { window.clearTimeout(timer); if (!controller.signal.aborted) setSourceLoading(false); });
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [extractionsOpen, crawlerSourcesHistoryOpen, canUseExtractions, sourcePage, sourcePageSize, sourceRevision]);
  const canManageIntegrations = hasFullAccess(currentUserRole);
  const canManageUsers = Boolean(currentUser && currentUserRole !== "viewer");
  const canManageAdvancedExtraction = hasFullAccess(currentUserRole);
  const pendingPreciseSku = activeExtractionJob?.kind === "single" &&
    activeExtractionJob.mode === "precise" &&
    !isExtractionJobActive(activeExtractionJob) &&
    activeExtractionJob.items?.[0]?.message.includes("Cobertura parcial")
      ? activeExtractionJob.items[0].sku
      : null;
  const adminTargetLocked = Boolean(
    selectedAdminUser &&
    !hasFullAccess(currentUserRole) &&
    resolvedUserRole(selectedAdminUser) !== "viewer" &&
    selectedAdminUser.id !== currentUser?.id,
  );
  const canDeleteAdminUser = (user: AuthUser) => Boolean(
    !user.is_owner &&
    user.id !== currentUser?.id &&
    (hasFullAccess(currentUserRole) || resolvedUserRole(user) === "viewer")
  );
  const integrationGroups = [
    { category: "discovery", title: "Descoberta de lojistas" },
    { category: "store", title: "APIs por loja" },
  ] as const;
  const selectedIntegrationProvider = integrationProviders.find(
    (provider) => provider.provider === activeIntegrationProvider,
  ) || integrationProviders[0] || null;

  // Polling the extraction queue and editing dialogs should not repaint the results underneath.
  const signedIn = Boolean(currentUser);
  const workspace = useMemo(() => {
    if (activeTab === 4) return <Suspense fallback={viewLoading}><LeadTimeView query={productName} selectedSkus={selectedSearchSkus} canRefresh={canUseExtractions} headers={authorizedHeaders} /></Suspense>;
    const refresh = () => void handleSearch(selectedSearchSkus, selectedSearchSkus.length ? `${selectedSearchSkus.length} SKUs selecionados` : undefined);
    const errorPanel = searchError && <div className="search-failure">
      <p role="alert">{searchError}</p>
      <button type="button" className="toolbar-button" onClick={refresh}><RefreshCw size={15} />Tentar novamente</button>
    </div>;
    if (errorPanel && (activeTab === 0 || activeTab === 1)) return errorPanel;
    return <SlidingTabs
      activeTab={activeTab}
      onTabChange={setActiveTab}
      analyticsMode={analyticsMode}
      selectedRow={selectedAnalyticsRow}
      setSelectedRow={setSelectedAnalyticsRow}
      loading={loading}
      products={products}
      comparisonRows={comparisonRows}
      lastQuery={lastQuery}
      searchCleared={searchCleared}
      searchFailure={errorPanel}
      isAuthenticated={signedIn}
      onRefresh={refresh}
      analyticsMap={signedIn ? (stores) => <Suspense fallback={viewLoading}><FreightMap query={productName} selectedSkus={selectedSearchSkus} sources={stores} canRefresh={canUseExtractions} headers={authorizedHeaders} /></Suspense> : undefined}
      analyticsReport={(mode, stores, brands, controls) => <Suspense fallback={viewLoading}><AnalysisReports mode={mode} query={productName} selectedSkus={selectedSearchSkus} sources={stores} brands={brands} {...controls} /></Suspense>}
    />;
  }, [activeTab, analyticsMode, selectedAnalyticsRow, productName, selectedSearchSkus, canUseExtractions, handleSearch, searchError, loading, products, comparisonRows, lastQuery, searchCleared, signedIn]);

  return (
    <div className={`app-shell ${sidebarCollapsed ? "app-shell--sidebar-collapsed" : ""}`}>
      {sidebarOpen && (
        <button
          type="button"
          className="fixed inset-0 z-40 bg-slate-950/30 backdrop-blur-[2px] lg:hidden"
          aria-label="Fechar navegação"
          onClick={() => { setSidebarOpen(false); setAnalyticsMenuOpen(false); }}
        />
      )}

      <aside className={`app-sidebar ${sidebarOpen ? "app-sidebar--open" : ""}`}>
        <div className="app-sidebar__brand">
          <img src={theme === "dark" ? logoWhiteUrl : logoUrl} alt="NeveTracker" className="h-8 w-auto" />
            <button
              type="button"
              aria-label="Alternar navegação"
              className="icon-only-button icon-button sidebar-toggle"
              onClick={toggleSidebar}
            >
              <span className="flex lg:hidden">
                {sidebarOpen ? <PanelLeft size={19} /> : <PanelLeftDashed size={19} />}
              </span>
              <span className="hidden lg:flex">
                {sidebarCollapsed ? <PanelLeftDashed size={19} /> : <PanelLeft size={19} />}
              </span>
            </button>
        </div>

        <div className="app-sidebar__content flex min-h-0 flex-1 flex-col px-3 py-5">
          <p className="sidebar-label">Workspace</p>
          <nav className="sidebar-nav mt-2" aria-label="Navegação principal">
            {navigation.filter((item) => currentUser || ![2, 3, 4].includes(item.tab)).map((item) => {
              const Icon = item.icon;
              const active = activeTab === item.tab;
              if (item.tab === 2) return (
                <div key={item.label} className="sidebar-analytics">
                  <button type="button" className={`sidebar-item ${active ? "sidebar-item--active" : ""}`}
                    aria-label={item.label} data-tooltip={item.label} aria-expanded={analyticsMenuOpen}
                    aria-controls="analytics-sidebar-views" onClick={toggleAnalyticsMenu}>
                    <span className="sidebar-item__icon"><Icon size={18} strokeWidth={2} /></span>
                    <span className="sidebar-analytics__label">
                      <span className="text-[13.5px] font-semibold leading-5">{item.label}</span>
                      <ChevronRight size={14} aria-hidden="true" />
                    </span>
                  </button>
                  <ul id="analytics-sidebar-views" className="sidebar-analytics__views" hidden={!analyticsMenuOpen}>
                    {analyticsViews.map(({ key, label }) => (
                      <li key={key}>
                        <button type="button" className="sidebar-analytics__view" id={`analytics-sidebar-${key}`}
                          aria-label={label} data-tooltip={label}
                          aria-current={active && !selectedAnalyticsRow && analyticsMode === key ? "page" : undefined}
                          onClick={() => { setAnalyticsMode(key); switchTab(2); }}>
                          {label}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              );

              return (
                <button
                  key={item.label}
                  type="button"
                  onClick={() => switchTab(item.tab)}
                  aria-label={item.label}
                  aria-current={active ? "page" : undefined}
                  data-tooltip={item.label}
                  className={`sidebar-item ${active ? "sidebar-item--active" : ""}`}
                >
                  <span className="sidebar-item__icon">
                    <Icon size={18} strokeWidth={2} />
                  </span>
                  <span className="min-w-0 text-left">
                    <span className="block text-[13.5px] font-semibold leading-5">{item.label}</span>
                  </span>
                </button>
              );
            })}
          </nav>

          <div ref={accountRef} className="account-menu-root mt-auto pt-4" onBlur={(event) => {
            if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) setAccountOpen(false);
          }}>
            <button
              type="button"
              onClick={() => setAccountOpen((open) => !open)}
              aria-label={currentUser ? `Conta de ${currentUser.username}` : "Entrar na conta"}
              data-tooltip={currentUser ? `Conta de ${currentUser.username}` : "Entrar na conta"}
              aria-expanded={accountOpen}
              aria-controls={accountOpen ? "account-actions" : undefined}
              className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left transition-colors hover:bg-[var(--surface-subtle)]"
            >
              <span className="account-avatar">
                {currentUser?.avatar_data_url
                  ? <img src={currentUser.avatar_data_url} alt="" />
                  : currentUser ? currentUser.username.slice(0, 2) : <UserRound size={17} />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-semibold text-[var(--foreground)]">
                  {currentUser?.username || "Entrar na conta"}
                </span>
              </span>
            </button>
            {accountOpen && (
              <div id="account-actions" className="account-popover" onClickCapture={event => {
                // The menu item disappears when its dialog opens. Leave a persistent focus target.
                if ((event.target as Element).closest("button")) accountRef.current?.querySelector<HTMLButtonElement>("button[aria-expanded]")?.focus({ preventScroll: true });
              }}>
                {!currentUser && (
                  <button type="button" className="account-popover__item" onClick={openLogin}>
                    <LogIn size={15} />
                    Entrar
                  </button>
                )}
                {currentUser && (
                  <>
                    {canUseExtractions && (
                      <button type="button" className="account-popover__item" onClick={openExtractions}>
                        <Download size={15} />
                        Extrações
                      </button>
                    )}
                    {canManageIntegrations && (
                      <button type="button" className="account-popover__item" onClick={openIntegrations}>
                        <Plug size={15} />
                        Integrações
                      </button>
                    )}
                    {canManageUsers && (
                      <button type="button" className="account-popover__item" onClick={openAdministration}>
                        <ShieldCheck size={15} />
                        Administração
                      </button>
                    )}
                  </>
                )}
                <button type="button" className="account-popover__item" onClick={openSettings}>
                  <Settings size={15} />
                  Configurações
                </button>
                {currentUser && (
                  <button
                    type="button"
                    onClick={handleLogout}
                    className="account-popover__item"
                  >
                    <LogOut size={15} />
                    Encerrar sessão
                  </button>
                )}
              </div>
            )}
          </div>
        </div>
      </aside>

      <section className="app-main">
        <header className="app-header">
          <div className="header-search-group">
          <form onSubmit={submitSearch} className="header-search">
            <button
              type="button"
              className={selectedSearchSkus.length ? "sku-select-trigger sku-select-trigger--active" : "sku-select-trigger"}
              onClick={() => openSkuSelector("search")}
              aria-label="Selecionar SKUs do banco local"
              data-tooltip="Selecionar SKUs"
            >
              <SquareCheckBig size={17} />
              {selectedSearchSkus.length > 0 && <span>{selectedSearchSkus.length}</span>}
            </button>
            <input
              type="search"
              value={productName}
              onChange={(event) => {
                setSelectedSearchSkus([]);
                setProductName(event.target.value);
              }}
              placeholder="Buscar produto ou modelo"
              aria-label="Buscar produtos"
            />
            {selectedSearchSkus.length > 0 && (
              <button
                type="button"
                className="icon-only-button header-search-clear"
                onClick={() => {
                  setSelectedSearchSkus([]);
                  setProductName("");
                }}
                aria-label="Limpar SKUs selecionados"
                data-tooltip="Limpar SKUs"
              >
                <X size={14} />
              </button>
            )}
          </form>

          {currentUser && <button type="button" className="icon-button" aria-label="Definir CEPs da pesquisa" data-tooltip="Definir CEPs" onClick={()=>setPostalDialogOpen(true)}><MapPin size={18}/>{searchPostals.length>0&&<span className="filter-count">{searchPostals.length}</span>}</button>}
          </div>
          <div className="header-actions" aria-hidden="true" />
        </header>

        <main className="app-content app-content--fixed">
          {workspace}
        </main>
      </section>

      {canUseExtractions && activeExtractionJob && isExtractionJobActive(activeExtractionJob) && <ExtractionProgressToast
        key={activeExtractionJob.id} job={activeExtractionJob} title={extractionJobTitle(activeExtractionJob)}
        startedAt={parseApiDateTime(activeExtractionJob.started_at || activeExtractionJob.created_at).getTime()}
        currentUserId={currentUser?.id} canCancel={canCancelActiveExtraction} cancelPending={!!currentCancelRequest?.pending}
        cancelError={currentCancelRequest?.error} connectionLost={extractionConnectionLost} accessBusy={accessActionBusy}
        accessMessage={accessActionMessage} onCancel={() => void cancelActiveExtraction()}
        onResolveAccess={(store, action) => void resolveExtractionAccess(store, action)}
      />}

      {loginOpen && (
        <DialogBackdrop aria-label="Acesso" onDismiss={closeLogin}>
          <form
            onSubmit={handleLogin}
            className="auth-dialog"
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-lg font-semibold text-[var(--foreground)]">Acesso</h2>
                <p className="mt-1 text-sm text-[var(--muted)]">Use suas credenciais para continuar.</p>
              </div>
              <button type="button" className="icon-only-button icon-button" onClick={closeLogin} aria-label="Fechar">
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
                data-dialog-autofocus
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
                <button className="icon-only-button"
                  type="button"
                  onClick={() => setShowLoginPassword((visible) => !visible)}
                  aria-label={showLoginPassword ? "Ocultar senha" : "Mostrar senha"}
                  data-tooltip={showLoginPassword ? "Ocultar senha" : "Mostrar senha"}
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
                    <OrbitingSpheres size={16} className="animate-spin" />
                  </span>
                )}
              </button>
            </div>
          </form>
        </DialogBackdrop>
      )}

      {administrationOpen && canManageUsers && (
        <DialogBackdrop aria-label="Administração de usuários" onDismiss={closeAdministration}>
          <section
            className="workspace-dialog workspace-dialog--admin-list"
          >
            <div className="dialog-header">
              <div>
                <h2>Administração</h2>
              </div>
              <button type="button" className="icon-only-button icon-button" onClick={closeAdministration} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <div className="admin-user-pane">
              <div className="admin-directory-toolbar">
                <div className="admin-user-search">
                  <Search size={15} />
                  <input
                    type="search"
                    value={adminSearch}
                    onChange={(event) => {
                      setAdminSearch(event.target.value);
                      setAdminPage(1);
                    }}
                    placeholder="Buscar usuário..."
                    aria-label="Buscar usuário"
                  />
                  {adminSearch && (
                    <button className="icon-only-button"
                      type="button"
                      onClick={() => {
                        setAdminSearch("");
                        setAdminPage(1);
                      }}
                      aria-label="Limpar busca"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
                <button type="button" className="button-primary admin-create-button" onClick={openAdminCreate}>
                  <UserPlus size={15} />
                  Criar usuário
                </button>
              </div>

              <div className="admin-user-list">
                {adminLoading && !adminUsers.length ? (
                  <p className="admin-empty">Carregando usuários...</p>
                ) : paginatedAdminUsers.length ? (
                  paginatedAdminUsers.map((user) => {
                    return (
                      <div
                        key={user.id}
                        className={selectedAdminUser?.id === user.id ? "admin-user-row admin-user-row--active" : "admin-user-row"}
                      >
                        <button type="button" className="admin-user-row__main" onClick={() => selectAdminUser(user)}>
                          <span className="admin-user-avatar">
                            {user.avatar_data_url
                              ? <img src={user.avatar_data_url} alt="" />
                              : user.username.slice(0, 2)}
                          </span>
                          <span className="min-w-0 flex-1">
                            <strong>{user.username}</strong>
                            <small>{userRoleLabel(user)}</small>
                          </span>
                        </button>
                        {resolvedUserRole(user) !== "viewer" && (
                          <span
                            className="admin-user-role-icon"
                            aria-label={userRoleLabel(user)}
                            data-tooltip={userRoleLabel(user)}
                          >
                            {resolvedUserRole(user) === "governor"
                              ? <Shield size={15} />
                              : <ShieldCheck size={15} />}
                          </span>
                        )}
                        {canDeleteAdminUser(user) && (
                          <button
                            type="button"
                            className="icon-only-button admin-user-delete"
                            disabled={adminSaving}
                            onClick={() => {
                              setAdminDeleteTarget(user);
                              setAdminDeleteError("");
                            }}
                            aria-label={`Remover ${user.username}`}
                            data-tooltip="Remover usuário"
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
              {visibleAdminUsers.length > 0 && (
                <div className="panel-footer panel-footer--pagination admin-pagination">
                  <nav className="pagination" aria-label="Paginação de usuários">
                    <button className="icon-only-button"
                      type="button"
                      onClick={() => setAdminPage(effectiveAdminPage - 1)}
                      disabled={effectiveAdminPage <= 1}
                      aria-label="Página anterior"
                    >
                      <ChevronLeft size={15} />
                    </button>
                    {adminVisiblePages.map((pageNumber) => (
                      <button
                        key={pageNumber}
                        type="button"
                        className={pageNumber === effectiveAdminPage ? "pagination__page pagination__page--active" : "pagination__page"}
                        onClick={() => setAdminPage(pageNumber)}
                        aria-current={pageNumber === effectiveAdminPage ? "page" : undefined}
                      >
                        {pageNumber}
                      </button>
                    ))}
                    <button className="icon-only-button"
                      type="button"
                      onClick={() => setAdminPage(effectiveAdminPage + 1)}
                      disabled={effectiveAdminPage >= adminTotalPages}
                      aria-label="Próxima página"
                    >
                      <ChevronRight size={15} />
                    </button>
                  </nav>
                </div>
              )}
              {!adminEditorOpen && adminError && <p className="auth-error admin-directory-message">{adminError}</p>}
            </div>
          </section>
        </DialogBackdrop>
      )}

      {administrationOpen && adminEditorOpen && canManageUsers && (
        <DialogBackdrop aria-label={selectedAdminUser ? "Gerenciar usuário" : "Novo usuário"} className="dialog-backdrop dialog-backdrop--nested" onDismiss={closeAdminEditor}>
          <section
            className={selectedAdminUser
              ? "workspace-dialog workspace-dialog--admin-editor workspace-dialog--admin-manage"
              : "workspace-dialog workspace-dialog--admin-editor workspace-dialog--admin-create"}
          >
            <div className="dialog-header">
              <div>
                <h2>{selectedAdminUser ? "Gerenciar usuário" : "Novo usuário"}</h2>
              </div>
              <button type="button" className="icon-only-button icon-button" onClick={closeAdminEditor} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <form className="admin-access-form" onSubmit={saveAdminUser}>
              <div className="admin-editor__content">
              {selectedAdminUser && (
                <div className="admin-editor__head">
                  <div className="admin-editor__identity">
                    <span className="admin-editor-avatar">
                      {adminAvatarValue
                        ? <img src={adminAvatarValue} alt="Foto de perfil" />
                        : adminAvatarName.slice(0, 2)}
                      <button
                        type="button"
                        className="icon-only-button admin-avatar-edit-button admin-avatar-edit-button--compact"
                        disabled={adminTargetLocked}
                        onClick={() => {
                          if (adminAvatarValue) {
                            setAdminAvatarDraft(null);
                          } else {
                            avatarFileInputRef.current?.click();
                          }
                        }}
                        aria-label={adminAvatarValue ? "Remover foto de perfil" : "Trocar foto"}
                        data-tooltip={adminAvatarValue ? "Remover foto" : "Trocar foto"}
                      >
                        {adminAvatarValue ? <Trash2 size={10} /> : <Camera size={10} />}
                      </button>
                    </span>
                    <div>
                      <h3>{selectedAdminUser.username}</h3>
                      <small>Último acesso: {formatDateTime(selectedAdminUser.last_login)}</small>
                    </div>
                  </div>
                  {selectedAdminUser.is_owner && <span className="admin-role-badge">Administrador</span>}
                  <input
                    ref={avatarFileInputRef}
                    type="file"
                    className="sr-only"
                    accept="image/jpeg,image/png,image/webp"
                    onChange={selectAvatarFile}
                  />
                </div>
              )}

              {!selectedAdminUser?.is_owner && (
                <div className="admin-editor-tabs" role="tablist" aria-label="Dados do usuário">
                  <button type="button" role="tab" aria-selected={adminEditorTab === "access"} className={adminEditorTab === "access" ? "admin-editor-tab admin-editor-tab--active" : "admin-editor-tab"} onClick={() => setAdminEditorTab("access")}>
                    <KeyRound size={14} /> Acesso
                  </button>
                  <button type="button" role="tab" aria-selected={adminEditorTab === "profile"} className={adminEditorTab === "profile" ? "admin-editor-tab admin-editor-tab--active" : "admin-editor-tab"} onClick={() => setAdminEditorTab("profile")}>
                    <Shield size={14} /> Permissões
                  </button>
                </div>
              )}

              {adminEditorTab === "access" && (
                <div className="admin-editor-panel">
                  <label className="form-field">
                    <span>Login</span>
                    <input
                      type="text"
                      value={adminUsername}
                      disabled={Boolean(selectedAdminUser)}
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
                    minLength={4}
                    maxLength={200}
                    onChange={(event) => {
                      setAdminPassword(event.target.value);
                      setAdminError("");
                    }}
                    autoComplete="new-password"
                  />
                  <button className="icon-only-button"
                    type="button"
                    disabled={adminTargetLocked}
                    onClick={() => setShowAdminPassword((visible) => !visible)}
                    aria-label={showAdminPassword ? "Ocultar senha" : "Mostrar senha"}
                    data-tooltip={showAdminPassword ? "Ocultar senha" : "Mostrar senha"}
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
                </div>
              )}

              {adminEditorTab === "profile" && hasFullAccess(currentUserRole) && !selectedAdminUser?.is_owner && (
                <label className="settings-field admin-role-field">
                  <span>Perfil</span>
                  <StyledSelect
                    value={adminRole === "developer" ? "governor" : adminRole}
                    ariaLabel="Perfil de acesso"
                    options={[
                      { value: "administrator", label: "Administrador" },
                      { value: "governor", label: "Governante" },
                      { value: "viewer", label: "Visualizador" },
                    ]}
                    onChange={(role) => {
                      setAdminRole(role as UserRole);
                      setAdminError("");
                    }}
                  />
                </label>
              )}

              {adminEditorTab === "profile" && !hasFullAccess(currentUserRole) && !selectedAdminUser?.is_owner && (
                <div className="admin-profile-static">
                  <span>Perfil</span>
                  <strong>Visualizador</strong>
                </div>
              )}

              {adminEditorTab === "profile" && adminTargetLocked && (
                <p className="admin-notice">Governantes só podem gerenciar Visualizadores e a própria conta.</p>
              )}
              {adminError && <p className="auth-error">{adminError}</p>}
              </div>

              <div className="modal-action-footer admin-editor__actions">
                <button type="submit" className="button-primary async-button" disabled={adminSaving || adminTargetLocked}>
                  <span className={adminSaving ? "async-button__content async-button__content--hidden" : "async-button__content"}>
                    {selectedAdminUser ? "Salvar" : "Criar usuário"}
                  </span>
                  {adminSaving && (
                    <span className="async-button__loader" aria-hidden="true">
                      <OrbitingSpheres size={16} className="animate-spin" />
                    </span>
                  )}
                </button>
              </div>
            </form>
          </section>
        </DialogBackdrop>
      )}

      {administrationOpen && adminEditorOpen && avatarCropOpen && canManageUsers && (
        <DialogBackdrop aria-label="Ajustar foto de perfil" className="dialog-backdrop dialog-backdrop--nested dialog-backdrop--avatar-crop" onDismiss={() => setAvatarCropOpen(false)}>
          <section
            className="workspace-dialog workspace-dialog--avatar-crop"
          >
            <div className="dialog-header">
              <div>
                <h2>Ajustar foto</h2>
              </div>
              <button type="button" className="icon-only-button icon-button" onClick={() => setAvatarCropOpen(false)} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <div
              className="avatar-crop-preview"
              aria-label="Prévia do recorte"
              onPointerDown={startAvatarDrag}
              onPointerMove={moveAvatarDrag}
              onPointerUp={stopAvatarDrag}
              onPointerCancel={stopAvatarDrag}
              onWheel={zoomAvatarCrop}
            >
              <img
                src={avatarCropSource}
                alt="Prévia da foto"
                draggable={false}
                onLoad={(event) => setAvatarImageSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })}
                style={{
                  transform: `translate(${avatarCropX}px, ${avatarCropY}px) scale(${avatarCropZoom})`,
                }}
              />
              <span className="avatar-crop-grid" aria-hidden="true" />
            </div>
            {avatarCropError && <p className="auth-error">{avatarCropError}</p>}
            <div className="modal-action-footer avatar-crop-actions">
              <button type="button" className="button-secondary" onClick={() => setAvatarCropOpen(false)}>Cancelar</button>
              <button type="button" className="button-primary" onClick={() => void applyAvatarCrop()}>Aplicar</button>
            </div>
          </section>
        </DialogBackdrop>
      )}

      {administrationOpen && adminDeleteTarget && canManageUsers && (
        <DialogBackdrop role="alertdialog" aria-labelledby="admin-delete-title" aria-describedby="admin-delete-description" className="dialog-backdrop dialog-backdrop--nested dialog-backdrop--confirmation"
          onDismiss={() => {
            if (adminSaving) return;
            setAdminDeleteTarget(null);
            setAdminDeleteError("");
          }}>
          <section
            className="workspace-dialog workspace-dialog--confirmation"
          >
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
                    <OrbitingSpheres size={16} className="animate-spin" />
                  </span>
                )}
              </button>
            </div>
          </section>
        </DialogBackdrop>
      )}

      {skuSelectorOpen && (
        <SkuPickerDialog
          initialSelection={skuSelectorTarget === "extraction" ? parseSkuInput(skuInput) : selectedSearchSkus}
          action={skuSelectorTarget === "extraction" ? "Usar SKUs" : "Pesquisar"}
          actionBusy={skuSelectorTarget === "search" && loading}
          onApply={searchSelectedSkus}
          onDismiss={() => setSkuSelectorOpen(false)}
        />
      )}

      {settingsOpen && (
        <DialogBackdrop aria-label="Configurações" onDismiss={() => setSettingsOpen(false)}>
          <section
            className="workspace-dialog workspace-dialog--settings"
          >
            <div className="dialog-header">
              <div>
                <h2>Configurações</h2>
              </div>
              <button type="button" className="icon-only-button icon-button" onClick={() => setSettingsOpen(false)} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <div className="modal-workspace settings-workspace">
              <div className="modal-sidebar settings-tabs" role="tablist" aria-label="Áreas de configurações">
                <button type="button" role="tab" aria-selected={settingsTab === "general"} className={settingsTab === "general" ? "extraction-tab extraction-tab--active" : "extraction-tab"} onClick={() => setSettingsTab("general")}>
                  <Settings size={15} />
                  Geral
                </button>
                <button type="button" role="tab" aria-selected={settingsTab === "about"} className={settingsTab === "about" ? "extraction-tab extraction-tab--active" : "extraction-tab"} onClick={() => setSettingsTab("about")}>
                  <Info size={15} />
                  Sobre
                </button>
              </div>

              <div className="modal-content-panel">
                <div className="modal-content-scroll settings-panel">
                  {settingsTab === "general" && (
                    <section className="settings-section" role="tabpanel">
                      <div className="settings-grid settings-grid--rows">
                        <div className="settings-field settings-theme">
                          <span>Tema</span>
                          <ThemeSelector value={themeMode} onChange={updateTheme} />
                        </div>
                        <label className="settings-field">
                          <span>Formato monetário</span>
                          <StyledSelect
                            value={interfacePreferences.currency}
                            ariaLabel="Formato monetário"
                            options={regionFormatOptions}
                            onChange={(currency) => updateInterfacePreferences((currentPreferences) => ({
                              ...currentPreferences,
                              currency,
                              locale: regionFormatOptions.find((option) => option.value === currency)?.locale || "pt-BR",
                            }))}
                          />
                        </label>
                      </div>
                    </section>
                  )}
                  {settingsTab === "about" && (
                    <section className="settings-about" role="tabpanel">
                      <strong className="settings-about__title">NeveTracker</strong>
                      <div className="settings-about__copyright">
                        <p>Copyright © 2026 Mateus Lopes</p>
                        <p>Todos os direitos reservados.</p>
                      </div>
                      <div className="settings-about__group">
                        <h3>Informações Legais</h3>
                        <a
                          className="settings-about__link"
                          href="https://github.com/Etamus/NeveTracker/blob/main/LICENSE.txt"
                          target="_blank"
                          rel="noreferrer"
                        >
                          <Scale size={16} aria-hidden="true" />
                          <span>Licença</span>
                        </a>
                      </div>
                    </section>
                  )}
                </div>
              </div>
            </div>
          </section>
        </DialogBackdrop>
      )}

      {integrationsOpen && canManageIntegrations && (
        <DialogBackdrop aria-label="Integrações" onDismiss={() => setIntegrationsOpen(false)}>
          <section
            className="workspace-dialog workspace-dialog--integrations"
          >
            <div className="dialog-header">
              <div>
                <h2>Integrações</h2>
              </div>
              <button type="button" className="icon-only-button icon-button" onClick={() => setIntegrationsOpen(false)} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <div className="modal-workspace integration-workspace">
              <nav className="modal-sidebar integration-sidebar" aria-label="Provedores de integração">
                {integrationGroups.map((group) => {
                  const providers = integrationProviders.filter((provider) => provider.category === group.category);
                  if (!providers.length) return null;
                  return (
                    <div className="modal-sidebar__group" key={group.category}>
                      <span>{group.title}</span>
                      {providers.map((provider) => (
                        <button
                          type="button"
                          key={provider.provider}
                          className={activeIntegrationProvider === provider.provider ? "modal-sidebar__item modal-sidebar__item--active" : "modal-sidebar__item"}
                          onClick={() => setActiveIntegrationProvider(provider.provider)}
                        >
                          <span>{provider.label}</span>
                          {provider.enabled && provider.provider !== "mercado_livre" && <i className="status-dot status-dot--updated" aria-hidden="true" />}
                        </button>
                      ))}
                      {group.category === "store" && <button type="button" className={activeIntegrationProvider === "shopee" ? "modal-sidebar__item modal-sidebar__item--active" : "modal-sidebar__item"} onClick={() => setActiveIntegrationProvider("shopee")}><span>Shopee</span></button>}
                    </div>
                  );
                })}
              </nav>

              <div className="modal-content-panel">
                <div className="modal-content-scroll integration-modal-body">
                  {integrationsLoading ? (
                    <div className="integration-loading">
                      <OrbitingSpheres size={17} className="animate-spin" />
                      Carregando integrações
                    </div>
                  ) : activeIntegrationProvider === "shopee" || activeIntegrationProvider === "mercado_livre" ? (
                    <BrowserAccountConnection key={activeIntegrationProvider} store={activeIntegrationProvider} />
                  ) : selectedIntegrationProvider ? (() => {
                    const provider = selectedIntegrationProvider;
                    const quotaPercent = provider.quota.limit === null
                      ? 100
                      : Math.max(0, Math.min(100, Math.round(
                        ((provider.quota.remaining || 0) / provider.quota.limit) * 100,
                      )));
                    const quotaLabel = provider.quota.limit === null
                      ? "Sem limite"
                      : provider.quota.hard_locked
                        ? "Cota esgotada"
                        : `${quotaPercent}% disponível`;
                    const credentialLabel = provider.credential_label === "Access token"
                      ? "Token"
                      : ["API key", "API token", "API password"].includes(provider.credential_label)
                        ? "API"
                        : provider.credential_label;
                    const extraField = provider.provider === "bright_data"
                      ? { label: "Zona SERP", value: provider.zone || "", key: "zone" as const }
                      : provider.provider === "apify"
                        ? { label: "Actor ID", value: provider.actor_id || "", key: "actor_id" as const }
                        : provider.provider === "dataforseo"
                          ? { label: "Login", value: provider.login || "", key: "login" as const }
                          : null;
                    return (
                      <div className={`integration-provider integration-provider--${provider.category}`}>
                        <div className="integration-provider__head">
                          <div>
                            <div className="integration-provider__title">
                              <strong>{provider.label}</strong>
                              <a href={provider.credential_url} target="_blank" rel="noreferrer">
                                Obter credencial
                                <ExternalLink size={12} aria-hidden="true" />
                              </a>
                            </div>
                          </div>
                          <label className="schedule-toggle">
                            <input type="checkbox" aria-label={`Usar ${provider.label}`} checked={provider.enabled} onChange={(event) => updateIntegrationProvider(provider.provider, { enabled: event.target.checked })} />
                            <span className="schedule-toggle__track" aria-hidden="true"><span className="schedule-toggle__thumb" /></span>
                          </label>
                          {provider.provider !== "mercado_livre" && (
                            <div
                              className={`integration-quota-meter ${provider.quota.hard_locked ? "integration-quota-meter--locked" : ""}`}
                              aria-label={quotaLabel}
                            >
                              <div className="integration-quota-meter__label">
                                  <span>Cota</span>
                                <strong>{quotaLabel}</strong>
                              </div>
                              <span className="integration-quota-meter__track" aria-hidden="true">
                                <span style={{ width: `${quotaPercent}%` }} />
                              </span>
                            </div>
                          )}
                        </div>

                        <div className="integration-config">
                          <section className="integration-config-section">
                            <div className="integration-config-section__head">
                              <strong>Credenciais</strong>
                              <span>Dados de acesso fornecidos pelo provedor</span>
                            </div>
                            <div className="integration-fields integration-fields--credentials">
                              <label className="settings-field integration-field--key">
                                <span>{credentialLabel}</span>
                                <input
                                  type="password"
                                  autoComplete="off"
                                  value={integrationKeys[provider.provider] || ""}
                                  placeholder={provider.api_key_masked || ""}
                                  onChange={(event) => setIntegrationKeys((currentKeys) => ({ ...currentKeys, [provider.provider]: event.target.value }))}
                                />
                              </label>
                              {extraField && (
                                <label className="settings-field integration-field--endpoint">
                                  <span>{extraField.label}</span>
                                  <input value={extraField.value} onChange={(event) => updateIntegrationProvider(provider.provider, { [extraField.key]: event.target.value })} />
                                </label>
                              )}
                            </div>
                          </section>

                          {(provider.supports_usage_mode || provider.supports_limit) && (
                            <section className="integration-config-section">
                              <div className="integration-config-section__head">
                                <strong>Comportamento</strong>
                                <span>Regras de uso durante as extrações</span>
                              </div>
                              <div className="integration-fields integration-fields--behavior">
                                {provider.supports_usage_mode && (
                                  <label className="settings-field integration-field--mode">
                                    <span>Função</span>
                                    <StyledSelect
                                      value={provider.usage_mode}
                                      ariaLabel="Função"
                                      options={[
                                        { value: "fallback", label: "Completar lacunas" },
                                        { value: "always", label: "Toda extração" },
                                      ]}
                                      onChange={(usageMode) => updateIntegrationProvider(provider.provider, {
                                        usage_mode: usageMode as IntegrationProviderSettings["usage_mode"],
                                      })}
                                    />
                                  </label>
                                )}
                                {provider.supports_limit && (
                                  <label className="settings-field integration-field--limit">
                                    <span>Quantidade de resultados</span>
                                    <StyledSelect
                                      value={provider.max_results}
                                      ariaLabel="Quantidade de resultados"
                                      options={(provider.provider === "mercado_livre"
                                        ? [50, 100, 200, 300, 500]
                                        : [10, 20, 30, 40, 50, 100]
                                      ).map((limit) => ({ value: limit, label: String(limit) }))}
                                      onChange={(maxResults) => updateIntegrationProvider(provider.provider, { max_results: maxResults })}
                                    />
                                  </label>
                                )}
                              </div>
                            </section>
                          )}
                        </div>
                      </div>
                    );
                  })() : null}
                  {integrationsError && <p className="auth-error">{integrationsError}</p>}
                </div>

                <div className="modal-action-footer integration-modal-footer" hidden={activeIntegrationProvider === "shopee" || activeIntegrationProvider === "mercado_livre"}>
              <button
                type="button"
                className="button-primary"
                onClick={() => void saveIntegrationSettings()}
                disabled={integrationsSaving || integrationsLoading || !integrationProviders.length}
              >
                {integrationsSaving && <OrbitingSpheres size={15} />}
                Salvar
              </button>
                </div>
              </div>
            </div>
          </section>
        </DialogBackdrop>
      )}

      {postalDialogOpen && currentUser && <PostalCodesDialog values={searchPostals} onClose={()=>setPostalDialogOpen(false)} onSave={values=>{setSearchPostals(values);window.localStorage.setItem(`nevetracker-search-postals:${currentUser.id}`,JSON.stringify(values));}}/>}
      {extractionsOpen && canUseExtractions && (
        <DialogBackdrop aria-label="Extrações"
          onDismiss={() => {
          setCrawlerSourcesHistoryOpen(false);
          setExtractionsOpen(false);
        }}>
          <section
            className="workspace-dialog workspace-dialog--wide workspace-dialog--extractions"
          >
            <div className="dialog-header extraction-dialog-header">
              <div>
                <h2>Extrações</h2>
              </div>

              {activeExtractionJob&&isExtractionJobActive(activeExtractionJob)&&<button
                type="button" className="extraction-dialog-progress" aria-label="Ver andamento da extração"
                data-tooltip={extractionDialogMessage || "Ver andamento da extração"}
                onClick={()=>{setCrawlerSourcesHistoryOpen(false);setExtractionsOpen(false);}}
              >
                <OrbitingSpheres size={17} paused={activeExtractionJob.status === "paused"}/>
                <span><strong>{extractionJobTitle(activeExtractionJob)}{activeExtractionJob.active_skus?.length ? ` · ${activeExtractionJob.active_skus.length} SKUs em execução` : ""}</strong><small>{extractionDialogMessage || activeExtractionJob.current_sku || "Preparando coleta"}</small></span>
                <ChevronRight size={16}/>
              </button>}

              <button type="button" className="icon-only-button icon-button" onClick={() => {
                setCrawlerSourcesHistoryOpen(false);
                setExtractionsOpen(false);
              }} aria-label="Fechar">
                <X size={17} />
              </button>
            </div>

            <div className="modal-workspace extraction-workspace">
            <div className="modal-sidebar extraction-navigation"><div className="extraction-tabs" role="tablist" aria-label="Áreas de extração">
              <button
                type="button"
                role="tab"
                aria-selected={extractionTab === "extract"}
                className={extractionTab === "extract" ? "extraction-tab extraction-tab--active" : "extraction-tab"}
                onClick={() => setExtractionTab("extract")}
              >
                <Download size={15} />
                Execução
              </button>
              {canManageAdvancedExtraction && (
                <button
                  type="button"
                  role="tab"
                  aria-selected={extractionTab === "control"}
                  className={extractionTab === "control" ? "extraction-tab extraction-tab--active" : "extraction-tab"}
                  onClick={() => setExtractionTab("control")}
                >
                  <Wrench size={15} />
                  Definições
                </button>
              )}
              <button
                type="button"
                role="tab"
                aria-selected={extractionTab === "schedule"}
                className={extractionTab === "schedule" ? "extraction-tab extraction-tab--active" : "extraction-tab"}
                onClick={() => setExtractionTab("schedule")}
              >
                <CalendarClock size={15} />
                Agendamento
              </button>
              {canManageAdvancedExtraction && <button type="button" role="tab" aria-selected={extractionTab === "metadata"} className={extractionTab === "metadata" ? "extraction-tab extraction-tab--active" : "extraction-tab"} onClick={() => { setExtractionTab("metadata"); void loadSkuStatus(); }}><ClipboardList size={15} />Cadastros</button>}
              {canManageAdvancedExtraction && (
                <button
                  type="button"
                  role="tab"
                  aria-selected={extractionTab === "database"}
                  className={extractionTab === "database" ? "extraction-tab extraction-tab--active" : "extraction-tab"}
                  onClick={() => setExtractionTab("database")}
                >
                  <Database size={15} />
                  Dados
                </button>
              )}
            </div>

            </div>

            <div className="modal-content-panel">
            <div className={`modal-content-scroll extraction-content-scroll extraction-content-scroll--${extractionTab}`}>

            {extractionTab === "extract" ? (
              <div className="extraction-panel" role="tabpanel">
                <form id="extraction-run-form" onSubmit={runExtractions} className="extraction-form extraction-form--fill">
                  <div className="sku-import-block">
                    <div className="sku-import-block__head">
                      <div>
                        <strong>Importar planilha</strong>
                        <span>XLSX, XLSM ou CSV.</span>
                      </div>
                      <div className="extraction-import-actions">
                        <DatePeriodSelect from={exportFrom} to={exportTo} onChange={(from,to) => {setExportFrom(from);setExportTo(to);}} compact ariaLabel="Data da extração para exportar" />
                        <button type="button" className="icon-only-button icon-button" aria-label="Exportar dados" data-tooltip="Exportar dados" disabled={exportingComplete} onClick={() => void exportComplete()}>{exportingComplete ? <OrbitingSpheres size={17} /> : <FileSpreadsheet size={17} />}</button>
                        <button type="button" className="icon-only-button icon-button extraction-template-button" aria-label="Baixar modelo" data-tooltip="Baixar modelo" onClick={downloadSkuTemplate}><FileDown size={17} /></button>
                      </div>
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
                      {skuImporting ? <OrbitingSpheres size={17} className="animate-spin" /> : <Upload size={17} />}
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

                  <div className="extraction-input-grid">
                    <SpreadsheetInput label="Preenchimento manual" value={skuInput} onChange={(value) => { setSkuInput(value); setExtractionError(""); }} action={<div className="extraction-sheet-actions"><button type="button" className="icon-only-button icon-button" aria-label="Limpar SKUs" data-tooltip="Limpar preenchimento" onClick={() => { setSkuInput(""); setExtractionError(""); }}><ClearIcon /></button><button type="button" className="icon-only-button icon-button extraction-select-skus" aria-label="Selecionar SKUs" onClick={() => openSkuSelector("extraction")} data-tooltip="Selecionar SKUs do banco"><SquareCheckBig size={16} /></button></div>} />
                    <SpreadsheetInput label="CEPs" kind="postal" value={extractionPostals} onChange={setExtractionPostals} action={<button type="button" className="icon-only-button icon-button" aria-label="Limpar CEPs" data-tooltip="Limpar CEPs" onClick={() => setExtractionPostals("")}><ClearIcon /></button>} />
                  </div>
                  <div className="extraction-targets"><div className="textarea-field"><span className="textarea-field__head"><strong>Lojas</strong></span><ExtractionStoreSelect values={extractionStores} onChange={setExtractionStores}/></div><div className="textarea-field"><span className="textarea-field__head"><strong>Lojistas</strong></span><ExtractionSellerSelect stores={extractionStores} values={extractionSellers} onChange={setExtractionSellers}/></div></div>
                </form>

                {extractionError && <p className="auth-error">{extractionError}</p>}


              </div>
            ) : extractionTab === "metadata" && canManageAdvancedExtraction ? (
              <SkuMetadataEditor items={skuStatusItems} headers={authorizedHeaders} onSaved={() => loadSkuStatus()} footerContainer={metadataFooter} />
            ) : extractionTab === "schedule" ? (
              <div className="extraction-panel" role="tabpanel">
                <div className="schedule-card">
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

                    <label className="schedule-field schedule-field--frequency">
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
                            aria-pressed={active}
                            onClick={() => toggleScheduleDay(day.value)}
                          >
                            {day.label}
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  <div className="schedule-card__head schedule-card__head--footer">
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
                        aria-label="Ativar agendamento"
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
                    </label>
                  </div>

                  <div className="schedule-card__head schedule-stale-setting">
                    <span>Consultar apenas desatualizados</span>
                    <label className="schedule-toggle">
                      <input type="checkbox" aria-label="Consultar apenas desatualizados" checked={scheduleSettings.onlyStale} onChange={(event) => setScheduleSettings((current) => ({ ...current, onlyStale: event.target.checked }))} />
                      <span className="schedule-toggle__track" aria-hidden="true"><span className="schedule-toggle__thumb" /></span>
                    </label>
                  </div>

                </div>
                {scheduleError && <p role="alert" className="auth-error">{scheduleError}</p>}
              </div>
            ) : extractionTab === "database" ? (
              <div className="extraction-panel" role="tabpanel">
                <div className="sku-status-card">
                  <div className="sku-database-toolbar">
                    <div className="sku-database-actions">
                      <div className="sku-status-search">
                        <div className="sku-status-search__field">
                          <Search size={15} />
                          <input
                            type="search"
                            value={skuDatabaseFilter}
                            onChange={(event) => setSkuDatabaseFilter(event.target.value)}
                            placeholder="Pesquisar materiais..."
                            aria-label="Pesquisar SKUs do banco local"
                          />
                          {skuDatabaseFilter && (
                            <button
                              type="button"
                              className="icon-only-button sku-status-search__clear"
                              onClick={() => setSkuDatabaseFilter("")}
                              aria-label="Limpar pesquisa"
                              data-tooltip="Limpar pesquisa"
                            >
                              <X size={14} />
                            </button>
                          )}
                          <span className="sku-status-search__count">
                            <Database size={13} aria-hidden="true" />
                            {normalizedSkuDatabaseFilter
                              ? `${databaseTotal} SKUs`
                              : `${databaseTotal} SKUs disponíveis`}
                          </span>
                        </div>
                      </div>
                      {canManageAdvancedExtraction && <>
                        <input ref={databaseImportRef} type="file" accept=".db,.sqlite,.sqlite3" hidden onChange={event=>void importDatabase(event)}/>
                        <button type="button" className="icon-only-button button-secondary" data-tooltip="Baixar banco completo" aria-label="Baixar banco completo" disabled={databaseTransferBusy} onClick={()=>void downloadDatabase()}>{databaseTransferBusy?<OrbitingSpheres size={15} className="animate-spin"/>:<Download size={15}/>}</button>
                        <button type="button" className="icon-only-button button-secondary" data-tooltip="Importar banco completo" aria-label="Importar banco completo" disabled={databaseTransferBusy||extractionRunning||(activeExtractionJob!==null&&isExtractionJobActive(activeExtractionJob))} onClick={()=>databaseImportRef.current?.click()}><Upload size={15}/></button>
                      </>}
                      <button
                        type="button"
                        className="icon-only-button button-secondary sku-refresh-all" aria-label="Atualizar banco de dados" data-tooltip="Atualizar"
                        onClick={() => void refreshAllSkus()}
                        disabled={extractionRunning || refreshingAllSkus || (activeExtractionJob !== null && isExtractionJobActive(activeExtractionJob)) || !databaseTotal}
                      >
                        {refreshingAllSkus ? <OrbitingSpheres size={15} className="animate-spin" /> : <RefreshCw size={14} />}

                      </button>
                    </div>
                  </div>
                  <RowAlignedFrame loading={databaseLoading} rowCount={databaseItems.length}>
                  <div className={normalizedSkuDatabaseFilter
                    ? "sku-database-table sku-database-table--filtered"
                    : "sku-database-table"}
                  >
                    <div className="sku-status-table-head" aria-hidden="true">
                      <span>SKU</span>
                      <span>Ofertas</span>
                      <span>Lojas</span>
                      <span>Verificado em</span>
                      <span>Ações</span>
                    </div>
                    <div className={databaseLoading ? "sku-status-list sku-status-list--loading" : "sku-status-list"}>
                      {databaseLoading ? (
                        <div className="sku-status-empty" role="status" aria-label="Carregando dados"><OrbitingSpheres size={22} className="animate-spin"/></div>
                      ) : visibleDatabaseSkuItems.length ? (
                        visibleDatabaseSkuItems.map((item) => (
                          <div key={item.sku} className="sku-status-row">
                            <strong>{item.sku}</strong>
                            <span className="sku-status-row__offers">{item.offer_count}</span>
                            <span className="sku-status-row__stores">{item.store_count}</span>
                            <time>{formatDateTime(item.last_update)}</time>
                            <span className="sku-status-actions">
                              <button
                                type="button"
                                className="icon-only-button table-action"
                                onClick={() => void deleteLocalSku(item.sku)}
                                disabled={extractionRunning || Boolean(deletingSku)}
                                aria-label={`Excluir ${item.sku}`}
                                data-tooltip="Excluir SKU"
                              >
                                {deletingSku === item.sku
                                  ? <OrbitingSpheres size={15} className="animate-spin" />
                                  : <Trash2 size={15} />}
                              </button>
                              <button
                                type="button"
                                className="icon-only-button table-action"
                                onClick={() => void refreshSingleSku(item.sku)}
                                disabled={extractionRunning || Boolean(refreshingSku) || Boolean(deletingSku)}
                                aria-label={`${pendingPreciseSku === item.sku ? "Continuar varredura de" : "Atualizar"} ${item.sku}`}
                                data-tooltip={pendingPreciseSku === item.sku ? "Continuar varredura de vendedores" : "Atualizar SKU"}
                              >
                                {refreshingSku === item.sku
                                  ? <OrbitingSpheres size={15} className="animate-spin" />
                                  : <RefreshCw size={15} />}
                              </button>
                            </span>
                          </div>
                        ))
                      ) : (
                        <p className="sku-status-empty">
                          {databaseTotal ? "Nenhum SKU encontrado." : "Nenhum SKU no banco local."}
                        </p>
                      )}
                    </div>
                  </div>
                  <PaginationFooter page={databasePage} pageSize={50} total={databaseTotal} onPageChange={setDatabasePage}/>
                  </RowAlignedFrame>
                </div>
                {extractionError && <p className="auth-error">{extractionError}</p>}
              </div>
            ) : (
              <div className="extraction-panel" role="tabpanel">
                <div className="crawler-control">
                  <div className="crawler-stats" aria-label="Saúde das fontes">
                    <div className="crawler-stat crawler-stat--sources">
                      <span>Fontes ativas</span>
                      <strong>{crawlerActiveCount}</strong>
                      <button
                        type="button"
                        className="icon-only-button crawler-stat__info"
                        onClick={() => {
                          setSourcePage(1);
                          setCrawlerSourcesHistoryOpen(true);
                        }}
                        aria-label="Ver fontes recentes"
                        data-tooltip="Ver fontes recentes"
                      >
                        <Info size={14} />
                      </button>
                    </div>
                    <div className="crawler-stat">
                      <span>Inválidas</span>
                      <strong>{crawlerInvalidCount}</strong>
                    </div>
                    <div className="crawler-stat crawler-stat--date">
                      <span>Última execução</span>
                      <strong data-tooltip={latestExtractionAt ? formatDateTime(latestExtractionAt) : "Sem execução"}>
                        {formatCompactDateTime(latestExtractionAt)}
                      </strong>
                    </div>
                  </div>

                  <div className="crawler-control-grid">
                    <label className="settings-field">
                      <span>Modo</span>
                      <StyledSelect
                        value={crawlerControl.default_mode}
                        ariaLabel="Modo"
                        options={[
                          { value: "quick", label: "Rápida" },
                          { value: "precise", label: "Precisa" },
                        ]}
                        onChange={(defaultMode) => setCrawlerControl((current) => ({
                          ...current,
                          default_mode: defaultMode as ExtractionMode,
                        }))}
                      />
                    </label>
                    <label className="settings-field">
                      <span>Requisições por loja</span>
                      <StyledSelect
                        value={crawlerControl.concurrent_requests_per_domain}
                        ariaLabel="Requisições por domínio"
                        options={[1, 2, 3, 4].map((requestCount) => ({
                          value: requestCount,
                          label: String(requestCount),
                        }))}
                        onChange={(requestCount) => setCrawlerControl((current) => ({
                          ...current,
                          concurrent_requests_per_domain: requestCount,
                        }))}
                      />
                    </label>
                    {[
                      {field: crawlerControl.default_mode === "precise" ? "precise_sku_workers" : "quick_sku_workers", label: "SKUs em paralelo", values: [1, 2, 3, 4, 6, 8]},
                      ...(crawlerControl.default_mode === "precise" ? [
                        {field: "quote_workers", label: "Capacidade de requisições", values: [2, 4, 6, 8, 12, 16, 24, 32]},
                      ] : [{field: "quick_timeout_seconds", label: "Timeout da busca rápida", values: [3, 4, 5, 6, 8, 10]}]),
                    ].map(control => <label className="settings-field" key={control.field}>
                      <span>{control.label}</span>
                      <StyledSelect value={Number(crawlerControl[control.field as keyof CrawlerControlSettings])} ariaLabel={control.label} showAllOptions={control.field === "precise_sku_workers" || control.field === "quick_sku_workers"}
                        options={control.values.map(value => ({value, label: `${value}${control.field.endsWith("_seconds") ? " segundos" : ""}`}))}
                        onChange={value => setCrawlerControl(current => ({...current, [control.field]: value}))} />
                    </label>)}
                  </div>

                  {crawlerControl.default_mode === "precise" && <div className="crawler-toggles">
                    {[
                      ["api_with_scraping", "APIs simultâneas ao scraping"],
                      ["hardware_acceleration", "Acelerar extração precisa"],
                    ].map(([field, label]) => (
                      <label key={field} className="crawler-toggle-row"
                        data-tooltip={field === "hardware_acceleration" ? "Libera Mercado Livre e Shopee à medida que cada SKU termina nas demais lojas e adapta o paralelismo ao hardware. Mantém os limites por loja." : undefined}>
                        <span>{label}</span>
                        <span className="schedule-toggle">
                          <input
                            type="checkbox"
                            checked={Boolean(crawlerControl[field as keyof CrawlerControlSettings])}
                            onChange={(event) => setCrawlerControl((current) => ({
                              ...current,
                              [field]: event.target.checked,
                            }))}
                          />
                          <span className="schedule-toggle__track" aria-hidden="true">
                            <span className="schedule-toggle__thumb" />
                          </span>
                        </span>
                      </label>
                    ))}
                  </div>}

                </div>
                {extractionError && <p className="auth-error">{extractionError}</p>}
              </div>
            )}
            </div>

            {extractionTab === "metadata" && canManageAdvancedExtraction && <div ref={setMetadataFooter} className="modal-action-footer extraction-action-footer sku-metadata-footer" />}
            {extractionTab !== "database" && extractionTab !== "metadata" && (
              <div className="modal-action-footer extraction-action-footer">
                {extractionTab === "extract" && (
                  <>
                  {activeExtractionJob?.mode === "precise" && activeExtractionJob.status === "completed" &&
                    activeExtractionJob.started_at && activeExtractionJob.completed_at &&
                    <ExtractionElapsedTime
                      startedAt={parseApiDateTime(activeExtractionJob.started_at).getTime()}
                      endedAt={parseApiDateTime(activeExtractionJob.completed_at).getTime()}
                    />}
                  <button type="submit" form="extraction-run-form" className="button-primary" disabled={extractionRunning}>
                    {extractionRunning && <OrbitingSpheres size={15} />}
                    {extractionRunning ? `Na fila ${extractionProgress}` : "Extrair"}
                  </button></>
                )}
                {extractionTab === "schedule" && (
                  <button type="button" className="button-primary async-button" onClick={() => void saveSchedule()} disabled={scheduleSaving}>
                    <span className={scheduleSaving ? "async-button__content async-button__content--hidden" : "async-button__content"}>
                      Salvar
                    </span>
                    {scheduleSaving && <span className="async-button__loader" aria-hidden="true"><OrbitingSpheres size={16} className="animate-spin" /></span>}
                  </button>
                )}
                {extractionTab === "control" && (
                  <>
                  <button type="button" className="button-secondary" onClick={() => void saveCrawlerControl(defaultCrawlerControl)} disabled={crawlerControlSaving || crawlerControlLoading}>Redefinir</button>
                  <button type="button" className="button-primary async-button" onClick={() => void saveCrawlerControl()} disabled={crawlerControlSaving || crawlerControlLoading}>
                    <span className={crawlerControlSaving ? "async-button__content async-button__content--hidden" : "async-button__content"}>
                      Salvar
                    </span>
                    {crawlerControlSaving && <span className="async-button__loader"><OrbitingSpheres size={16} className="animate-spin" /></span>}
                  </button>
                  </>
                )}
              </div>
            )}
            </div>
            </div>
          </section>
        </DialogBackdrop>
      )}

      {extractionsOpen && crawlerSourcesHistoryOpen && canUseExtractions && (
        <DialogBackdrop aria-labelledby="source-history-title" className="dialog-backdrop dialog-backdrop--nested" onDismiss={() => setCrawlerSourcesHistoryOpen(false)}>
          <section
            className="workspace-dialog workspace-dialog--source-history"
          >
            <div className="dialog-header source-history-header">
              <div className="dialog-heading">
                <div>
                  <h2 id="source-history-title">Fontes recentes</h2>
                  <p>{sourceTotal} {sourceTotal === 1 ? "fonte registrada" : "fontes registradas"}</p>
                </div>
              </div>
              <div className="source-history-header__actions">
                <button
                  type="button"
                  className="icon-only-button icon-button"
                  onClick={() => setSourceRevision((value) => value + 1)}
                  disabled={sourceLoading}
                  aria-label="Atualizar fontes"
                  data-tooltip="Atualizar fontes"
                >
                  {sourceLoading
                    ? <OrbitingSpheres size={15} className="animate-spin" />
                    : <RefreshCw size={15} />}
                </button>
                <button
                  type="button"
                  className="icon-only-button icon-button"
                  onClick={() => setCrawlerSourcesHistoryOpen(false)}
                  aria-label="Fechar histórico"
                >
                  <X size={17} />
                </button>
              </div>
            </div>

            <div className="source-history-table">
              <div className="source-history-row source-history-row--head" aria-hidden="true">
                <span>Status</span>
                <span>SKU</span>
                <span>Loja</span>
                <span>Lojista</span>
                <span>Confiança</span>
                <span>Ação</span>
              </div>
              <div className="source-history-list">
                {sourceLoading ? <div className="source-history-empty" role="status" aria-label="Carregando fontes"><OrbitingSpheres size={24} /></div> : sourceError ? <p className="source-history-empty" role="alert">{sourceError}</p> : crawlerSources.length ? crawlerSources.map((source) => (
                  <div key={source.id} className="source-history-row">
                    <span className="source-history-status">
                      {source.active ? "Ativa" : "Inválida"}
                    </span>
                    <strong data-tooltip={source.sku}>{source.sku}</strong>
                    <span data-tooltip={source.store.replaceAll("_", " ")}>{source.store.replaceAll("_", " ")}</span>
                    <span data-tooltip={source.seller || "Não informado"}>{source.seller || "-"}</span>
                    <span>{Math.round(source.match_confidence * 100)}%</span>
                    <button
                      type="button"
                      className="icon-only-button table-action"
                      onClick={() => void rediscoverSourceSku(source.sku)}
                      disabled={extractionRunning || Boolean(rediscoveringSku)}
                      aria-label={`Redescobrir ${source.sku}`}
                      data-tooltip="Redescobrir SKU"
                    >
                      {rediscoveringSku === source.sku
                        ? <OrbitingSpheres size={14} className="animate-spin" />
                        : <RefreshCw size={14} />}
                    </button>
                  </div>
                )) : (
                  <p className="source-history-empty">As fontes aparecerão após a primeira extração precisa.</p>
                )}
              </div>
            </div>
            <PaginationFooter page={sourcePage} pageSize={sourcePageSize} total={sourceTotal} onPageChange={setSourcePage} />
          </section>
        </DialogBackdrop>
      )}
    </div>
  );
};
