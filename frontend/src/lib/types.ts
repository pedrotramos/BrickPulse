export type Locale = "en" | "pt-BR" | "es";
export type Json =
  | null
  | boolean
  | number
  | string
  | Json[]
  | { [key: string]: Json };
export interface Workspace {
  workspace_id: string;
  workspace_name?: string;
  monthly_savings_usd: number;
  monthly_savings_addressable_usd: number;
  recommendation_count: number;
  monthly_waste_usd: number;
}
export interface SavingsSlice {
  key: string;
  monthly_savings_usd: number;
  resource_count: number;
}
export interface Overview {
  total_monthly_savings_usd: number;
  total_monthly_savings_addressable_usd: number;
  total_monthly_savings_low_usd: number;
  total_monthly_savings_high_usd: number;
  recommendation_count: number;
  workspace_count: number;
  workspaces: Workspace[];
  savings_tiers: { tier: string; monthly_savings_usd: number }[];
  // Composed like the tiers: each list sums to the addressable total.
  savings_by_category?: SavingsSlice[];
  savings_by_rule?: SavingsSlice[];
  priced?: {
    contract_available: boolean;
    monthly_savings_addressable_usd: number;
    contract_part_usd: number;
    list_part_usd: number;
    monthly_cloud_savings_usd: number;
    cloud_available: boolean;
  };
}
export interface Day {
  usage_date: string;
  spend_usd?: number;
  cost_usd?: number;
  dbus?: number;
}
export interface Spend {
  unavailable: boolean;
  lookback_days: number;
  total_monthly_spend_usd: number;
  total_monthly_spend_contract_usd: number | null;
  contract_priced_share: number | null;
  total_monthly_cloud_cost_usd: number | null;
  total_classic_spend_usd: number;
  total_serverless_spend_usd: number;
  out_of_scope_workspaces: number;
  workspaces: WorkspaceSpend[];
  by_day: Day[];
  by_sku: { sku_group: string; spend_usd: number }[];
}
export interface WorkspaceSpend {
  workspace_id: string;
  workspace_name?: string;
  monthly_spend_usd: number;
  total_spend_usd: number;
  by_day: Day[];
}
export interface Finding {
  recommendation_id: string;
  finding_key: string;
  group_id: string;
  rule_id: string;
  rule_title: string;
  rule_version: string;
  resource_type: string;
  resource_name: string;
  resource_id: string;
  workspace_id: string;
  workspace_name?: string;
  workspace_url?: string;
  resource_owner?: string;
  why: string;
  how: string;
  monthly_savings_usd: number;
  monthly_savings_low_usd: number;
  monthly_savings_high_usd: number;
  monthly_waste_usd: number;
  monthly_savings_contract_usd: number | null;
  monthly_cloud_savings_usd: number | null;
  price_basis: string | null;
  savings_status: string;
  priority: string;
  effort_band: string;
  nba_score: number;
  evidence: Record<string, Json>;
  computed_at: string;
  presentation?: {
    title: string;
    why: string;
    how: string;
    caution: string;
    sources: string[];
    legacy: boolean;
    original_why: string;
    original_how: string;
  };
}
export interface Group {
  group_id: string;
  workspace_id: string;
  workspace_name?: string;
  workspace_url?: string;
  resource_id: string;
  resource_name: string;
  resource_type: string;
  action_label: string;
  option_count: number;
  monthly_savings_usd: number;
  monthly_waste_usd: number;
  monthly_savings_contract_usd?: number | null;
  monthly_cloud_savings_usd?: number | null;
  effort_band: string;
  priority: string;
  nba_score: number;
  options: Finding[];
  serverless_fit?: {
    verdict: string;
    headline: string;
    reason: string;
    projected_monthly_savings_usd: number | null;
  };
}
export interface Page {
  items: Group[];
  total: number;
  pagination: { page: number; page_size: number; pages: number };
  summary: {
    resources: number;
    potential_usd: number;
    advisory_only: number;
    quick_wins: { count: number; value_usd: number };
    views: Record<string, number>;
  };
}
export interface Config {
  brand: string;
  version: string;
  compute_mode: string;
  auth_mode: string;
  currency: string;
  lookback_days: number;
  pricing_mode: "auto" | "list" | "account";
  experimental_enabled: boolean;
  apps_tab_enabled: boolean;
  genie_costs_tab_enabled: boolean;
  genie_enabled: boolean;
  genie_mode: string;
  genie_embed_url: string;
}
export interface Health {
  checked_at: string;
  degraded: boolean;
  unknown: boolean;
  message: string;
  unavailable_rules: { rule_id: string; missing_sources: string[] }[];
  sources: {
    name: string;
    status: string;
    required: boolean;
    message: string;
    blocks_rules: string[];
  }[];
  last_run: null | {
    unavailable: boolean;
    finished_at: string | null;
    ok: boolean | null;
    failed_flows: string[];
  };
}
export interface Rule {
  id: string;
  title: string;
  version: string;
  category: string;
  resource_type: string;
  enabled: boolean;
  thresholds: Record<string, Json>;
}
