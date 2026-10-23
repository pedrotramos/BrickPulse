import {
  Component,
  type ReactNode,
  Suspense,
  lazy,
  useState,
  useEffect,
  useRef,
} from "react";
import {
  Routes,
  Route,
  NavLink,
  useSearchParams,
  useLocation,
  Navigate,
  useNavigate,
  useParams,
} from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useIsFetching, useQueryClient } from "@tanstack/react-query";
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import {
  Activity,
  ArrowRight,
  BarChart3,
  Blocks,
  BookOpen,
  Check,
  ChevronDown,
  FlaskConical,
  Globe2,
  LayoutDashboard,
  Menu,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Sparkles,
  Tag,
  Target,
  TrendingDown,
  Wallet,
  X,
  Zap,
} from "lucide-react";
import { useApi, useScope } from "../lib/api";
import { setLocale } from "../lib/i18n";
import i18n from "../lib/i18n";
import { date } from "../lib/format";
import { Heading, Notice, PAGE_EVENT, ScopedLink } from "../components/ui";
import type { Config, Health, Locale, Spend, Workspace } from "../lib/types";
import OverviewPage from "../features/overview/OverviewPage";
import OpportunitiesPage from "../features/opportunities/OpportunitiesPage";
const ResourcePage = lazy(() => import("../features/resource/ResourcePage"));
const AnalysisPage = lazy(() => import("../features/analysis/AnalysisPage"));
const SettingsPage = lazy(() => import("../features/settings/SettingsPage"));

class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: boolean }
> {
  state = { error: false };
  static getDerivedStateFromError() {
    return { error: true };
  }
  render() {
    return this.state.error ? (
      <div className="empty" role="alert">
        <h1>BrickPulse</h1>
        <p>{i18n.t("loadError")}</p>
        <button onClick={() => window.location.reload()}>
          {i18n.t("retry")}
        </button>
      </div>
    ) : (
      this.props.children
    );
  }
}
const LANGUAGES: Record<Locale, string> = {
  "pt-BR": "Português (Brasil)",
  en: "English",
  es: "Español",
};
const SHORT_LANGUAGES: Record<Locale, string> = {
  "pt-BR": "Português",
  en: "English",
  es: "Español",
};
// Announces each client-side navigation, which screen readers otherwise miss.
function RouteAnnouncer() {
  const [message, setMessage] = useState("");
  const first = useRef(true);
  useEffect(() => {
    const onPage = (event: Event) => {
      if (first.current) {
        first.current = false;
        return;
      }
      setMessage((event as CustomEvent<string>).detail);
    };
    window.addEventListener(PAGE_EVENT, onPage);
    return () => window.removeEventListener(PAGE_EVENT, onPage);
  }, []);
  return (
    <div className="sr-only" aria-live="polite" aria-atomic="true">
      {message}
    </div>
  );
}
const primary = [
  ["/", "overview", LayoutDashboard],
  ["/next-best-actions", "actions", Target],
  ["/recommendations", "opportunities", TrendingDown],
  ["/workspaces", "workspaces", Blocks],
] as const;
const secondary = [
  ["/cost-anomalies", "anomalies", Activity],
  ["/tagging", "tagging", Tag],
  ["/savings-realized", "realized", Wallet],
  ["/serverless", "serverless", Zap],
  ["/labs", "labs", FlaskConical],
] as const;
const platform = [
  ["/rules", "rules", BookOpen],
  ["/data-health", "health", ShieldCheck],
  ["/settings", "settings", Settings2],
] as const;
function Shell() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const client = useQueryClient();
  const scope = useScope();
  const [menu, setMenu] = useState(false);
  const sidebar = useRef<HTMLElement>(null);
  const workspace = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 850px)");
    const update = () => {
      if (sidebar.current) sidebar.current.inert = media.matches && !menu;
      if (workspace.current) workspace.current.inert = media.matches && menu;
    };
    update();
    media.addEventListener("change", update);
    if (media.matches && menu)
      sidebar.current?.querySelector<HTMLElement>("a,button")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (!media.matches || !menu) return;
      if (event.key === "Escape") {
        setMenu(false);
        return;
      }
      if (event.key !== "Tab") return;
      const items = Array.from(
        sidebar.current?.querySelectorAll<HTMLElement>("a,button") ?? [],
      );
      const first = items[0],
        last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      media.removeEventListener("change", update);
      document.removeEventListener("keydown", onKey);
      if (workspace.current) workspace.current.inert = false;
      if (media.matches && menu)
        document.querySelector<HTMLElement>(".mobile-open")?.focus();
    };
  }, [menu]);
  const config = useApi<Config>("/config", { scoped: false });
  const workspaces = useApi<Workspace[]>("/workspaces", { scoped: false });
  const spendInventory = useApi<Spend>("/spend", { scoped: false });
  const workspaceOptions = new Map(
    (workspaces.data ?? []).map((w) => [w.workspace_id, w]),
  );
  for (const w of spendInventory.data?.workspaces ?? []) {
    if (!workspaceOptions.has(w.workspace_id))
      workspaceOptions.set(w.workspace_id, {
        ...w,
        monthly_savings_usd: 0,
        monthly_savings_addressable_usd: 0,
        recommendation_count: 0,
        monthly_waste_usd: 0,
      });
  }
  if (scope && workspaces.isSuccess && !workspaceOptions.has(scope))
    workspaceOptions.set(scope, {
      workspace_id: scope,
    } as Workspace);
  const health = useApi<Health>("/data-health", { scoped: false });
  const fetching = useIsFetching() > 0;
  const locale = (i18n.language in LANGUAGES ? i18n.language : "en") as Locale;
  const me = useApi<{ user: string }>("/me", { scoped: false });
  useEffect(() => {
    setMenu(false);
    document.querySelector("main")?.scrollTo(0, 0);
  }, [location.pathname]);
  const nav = (
    items: readonly (readonly [string, string, typeof Activity])[],
  ) =>
    items.map(([to, key, Icon]) => (
      <NavLink
        key={to}
        end={to === "/"}
        to={{ pathname: to, search: scope ? `?workspace_id=${scope}` : "" }}
        onClick={() => setMenu(false)}
      >
        <Icon size={18} />
        <span>{t(key)}</span>
      </NavLink>
    ));
  const changeScope = (value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set("workspace_id", value);
    else next.delete("workspace_id");
    next.delete("page");
    if (
      location.pathname.startsWith("/resources/") ||
      location.pathname.startsWith("/recommendations/")
    ) {
      navigate({ pathname: "/recommendations", search: next.toString() });
    } else setParams(next);
  };
  return (
    <div className="app-shell">
      <RouteAnnouncer />
      <a href="#main" className="skip-link">
        {t("skip")}
      </a>
      {menu && (
        <button
          className="nav-scrim"
          aria-label={t("closeMenu")}
          onClick={() => setMenu(false)}
        />
      )}
      <aside
        ref={sidebar}
        id="app-sidebar"
        className={`sidebar ${menu ? "is-open" : ""}`}
        role={menu ? "dialog" : undefined}
        aria-modal={menu ? true : undefined}
        aria-label={t("navigation")}
      >
        <ScopedLink to="/" className="brand">
          <img src="/favicon.svg?v=frequency" width="32" height="32" alt="" />
          <span>BrickPulse</span>
        </ScopedLink>
        <button
          className="mobile-close icon-button"
          onClick={() => setMenu(false)}
          aria-label={t("closeMenu")}
        >
          <X size={20} />
        </button>
        <nav aria-label={t("navigation")}>
          {nav(primary)}
          <div className="nav-label">{t("analysis")}</div>
          {nav(
            secondary.filter(
              ([, key]) =>
                key !== "labs" || config.data?.experimental_enabled !== false,
            ),
          )}
          {config.data?.apps_tab_enabled && nav([["/apps", "apps", BarChart3]])}
          {config.data?.genie_costs_tab_enabled &&
            nav([["/genie-costs", "genie", Sparkles]])}
          <div className="nav-label">{t("platform")}</div>
          {nav(platform)}
        </nav>
        <div className="sidebar-bottom">
          <ShieldCheck size={16} />
          <p>{t("consultative")}</p>
          {config.data?.version && (
            <span className="version">v{config.data.version}</span>
          )}
        </div>
      </aside>
      <div ref={workspace} className="workspace">
        <header className="toolbar">
          <div className="toolbar-start">
            <button
              className="mobile-open icon-button"
              aria-label={t("openMenu")}
              aria-expanded={menu}
              aria-controls="app-sidebar"
              onClick={() => setMenu(true)}
            >
              <Menu size={21} />
            </button>
            <label className="workspace-picker">
              <Blocks size={16} />
              <span className="sr-only">{t("workspace")}</span>
              <select
                value={scope}
                onChange={(e) => changeScope(e.target.value)}
              >
                <option value="">{t("allWorkspaces")}</option>
                {Array.from(workspaceOptions.values()).map((w) => (
                  <option key={w.workspace_id} value={w.workspace_id}>
                    {w.workspace_name ?? w.workspace_id}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="toolbar-end">
            <Dropdown.Root>
              <Dropdown.Trigger className="language-button">
                <Globe2 size={16} aria-hidden="true" />
                <span className="sr-only">{t("language")}: </span>
                <span className="language-name" lang={locale}>
                  {SHORT_LANGUAGES[locale]}
                </span>
                <ChevronDown size={13} />
              </Dropdown.Trigger>
              <Dropdown.Portal>
                <Dropdown.Content className="dropdown" sideOffset={8}>
                  <Dropdown.RadioGroup
                    value={i18n.language}
                    onValueChange={(v) => setLocale(v as Locale)}
                  >
                    {(["pt-BR", "en", "es"] as const).map((l) => (
                      <Dropdown.RadioItem
                        key={l}
                        value={l}
                        className="dropdown-item"
                        lang={l}
                      >
                        {LANGUAGES[l]}
                        <Dropdown.ItemIndicator>
                          <Check size={15} aria-hidden="true" />
                        </Dropdown.ItemIndicator>
                      </Dropdown.RadioItem>
                    ))}
                  </Dropdown.RadioGroup>
                </Dropdown.Content>
              </Dropdown.Portal>
            </Dropdown.Root>
            <button
              className={`icon-button refresh${fetching ? " is-busy" : ""}`}
              aria-label={t(fetching ? "refreshing" : "refresh")}
              aria-busy={fetching}
              onClick={() => {
                if (!fetching) void client.invalidateQueries();
              }}
            >
              <RefreshCw size={17} />
            </button>
            {me.data?.user && (
              <span className="user" title={me.data.user}>
                {me.data.user.split("@")[0]}
              </span>
            )}
          </div>
        </header>
        <main id="main" tabIndex={-1}>
          <div className="scope-line">
            <span>
              {health.data?.last_run?.finished_at
                ? `${t("updated")} ${date(health.data.last_run.finished_at, i18n.language)} UTC`
                : health.isError
                  ? `${t("freshness")} · ${t("notAvailable")}`
                  : health.data
                    ? `${t("freshness")} · ${t("pending")}`
                    : null}
            </span>
          </div>
          {config.data?.compute_mode === "demo" && (
            <div className="demo-strip">{t("demo")}</div>
          )}
          {health.data?.degraded && (
            <Notice tone="warning">
              <ScopedLink to="/data-health">
                {t("degraded")} <ArrowRight size={14} aria-hidden="true" />
              </ScopedLink>
            </Notice>
          )}
          <ErrorBoundary key={location.pathname}>
            <Suspense
              fallback={
                <div
                  className="skeleton"
                  role="status"
                  aria-label={t("loading")}
                >
                  <span />
                  <span />
                </div>
              }
            >
              <Routes>
                <Route path="/" element={<OverviewPage />} />
                <Route
                  path="/workspaces/:workspaceId/*"
                  element={<WorkspaceRedirect />}
                />
                <Route
                  path="/recommendations"
                  element={<OpportunitiesPage />}
                />
                <Route
                  path="/next-best-actions"
                  element={<OpportunitiesPage actions />}
                />
                <Route path="/resources/:groupId" element={<ResourcePage />} />
                <Route
                  path="/recommendations/:recommendationId"
                  element={<ResourcePage />}
                />
                {[
                  "workspaces",
                  "cost-anomalies",
                  "tagging",
                  "savings-realized",
                  "serverless",
                  "labs",
                  "apps",
                  "genie-costs",
                  "rules",
                  "data-health",
                ].map((path) => (
                  <Route
                    key={path}
                    path={`/${path}`}
                    element={<AnalysisPage kind={path} />}
                  />
                ))}
                <Route
                  path="/settings"
                  element={<SettingsPage config={config.data} />}
                />
                <Route
                  path="*"
                  element={
                    <>
                      <Heading title="404" description={t("notFound")} />
                      <ScopedLink to="/">{t("overview")}</ScopedLink>
                    </>
                  }
                />
              </Routes>
            </Suspense>
          </ErrorBoundary>
          <footer className="page-footer">
            <span>{t("footer")}</span>
            <span>{t("noAutomations")}</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
/** Old and shared links: /workspaces/:id/<page> is <page> filtered to that workspace. */
function WorkspaceRedirect() {
  const { workspaceId = "", "*": rest = "" } = useParams();
  const [params] = useSearchParams();
  const next = new URLSearchParams(params);
  next.set("workspace_id", workspaceId);
  return <Navigate replace to={{ pathname: `/${rest}`, search: `?${next}` }} />;
}
export default function App() {
  return (
    <ErrorBoundary>
      <Shell />
    </ErrorBoundary>
  );
}
