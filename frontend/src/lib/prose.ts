/**
 * English sentences some rules store inside evidence (warnings, how the savings
 * range was built). Persisted rows keep the English text, so the UI recognises
 * each known sentence and renders its localized message instead; anything it
 * does not recognise is shown as-is, marked as English.
 */
type Pattern = {
  test: RegExp;
  key: string;
  params?: (m: RegExpMatchArray) => Record<string, string>;
};

const PATTERNS: Pattern[] = [
  {
    test: /^No request has ever been logged for this endpoint/,
    key: "untrackedEndpoint",
  },
  { test: /^We can't tell what this pool costs you/, key: "unpricedPool" },
  {
    test: /^([\d,]+) of the job's (\d+) source tables \((.*?)\) show no write at all/,
    key: "unwrittenSources",
    params: (m) => ({ count: m[1], total: m[2], tables: m[3] }),
  },
  { test: /^Low = the DBU-only saving billing can prove/, key: "rangeDbuOnly" },
  {
    test: /^Low = \$0 — this cluster is also used interactively/,
    key: "rangeSharedCluster",
  },
  {
    test: /^Low = \$0 — the jobs-compute rate wasn't in list_prices for this cloud, so the figure is a conservative (\d+)%/,
    key: "rangeNoJobsRate",
    params: (m) => ({ pct: m[1] }),
  },
  {
    test: /^Low = the reclaim from simply turning auto-stop on at a safe (\d+)-minute setting.*?the full (\d+)-minute setting.*?~([\d.]+) restart/,
    key: "rangeAutostopTiers",
    params: (m) => ({ baseline: m[1], proposed: m[2], stops: m[3] }),
  },
  {
    test: /^Low = the reclaim from a conservative, low-churn auto-stop setting; high = the full (\d+)-minute setting, which adds ~([\d.]+)/,
    key: "rangeAutostopConservative",
    params: (m) => ({ proposed: m[1], stops: m[2] }),
  },
  {
    test: /^Low = \$0 if the ~([\d.]+) extra restart\(s\)\/day a (\d+)-min auto-stop adds/,
    key: "rangeAutostopChurn",
    params: (m) => ({ stops: m[1], proposed: m[2] }),
  },
  {
    test: /^Low = \$0 — with no Spark task data the target size is inferred from query behaviour.*?one-step rate cut if (.+?) keeps up/,
    key: "rangeRightsizeInferred",
    params: (m) => ({ size: m[1] }),
  },
  {
    test: /^Low = the one measured step to (.+?) \(peak slot demand fits it with room to spare\); high = (?!the deeper)(.+?), the deeper size/,
    key: "rangeRightsizeFloor",
    params: (m) => ({ size: m[1], floor: m[2] }),
  },
  {
    test: /^Low = the one measured step to (.+?) \(peak slot demand fits it with room to spare\); high = the deeper size/,
    key: "rangeRightsizeDeeper",
    params: (m) => ({ size: m[1] }),
  },
];

const CLUSTER_KINDS: Record<string, string> = {
  "All-purpose cluster (created in the UI)": "kindUi",
  "All-purpose cluster (created via API/Terraform/DAB)": "kindApi",
  "Job cluster": "kindJob",
  "Pipeline (DLT) cluster": "kindPipeline",
  "Pipeline maintenance cluster": "kindPipelineMaintenance",
  "SQL warehouse cluster": "kindSql",
  "Model serving cluster": "kindModels",
};

/** The `prose.*` message for a stored English sentence, or null if unknown. */
export function proseMessage(
  text: string,
): { key: string; params: Record<string, string> } | null {
  const kind = CLUSTER_KINDS[text.trim()];
  if (kind) return { key: `prose.${kind}`, params: {} };
  for (const p of PATTERNS) {
    const m = text.match(p.test);
    if (m) return { key: `prose.${p.key}`, params: p.params?.(m) ?? {} };
  }
  return null;
}
