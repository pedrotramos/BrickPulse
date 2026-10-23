import { test, expect } from "@playwright/test";

for (const [locale, heading] of [
  ["en", "Overview"],
  ["pt-BR", "Visão geral"],
  ["es", "Resumen"],
]) {
  test(`localized overview ${locale}`, async ({ page }) => {
    await page.addInitScript(
      (locale) => localStorage.setItem("brickpulse.locale", locale),
      locale,
    );
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: heading, exact: true }),
    ).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", locale);
    await expect(page.getByRole("alert")).toHaveCount(0);
    expect(
      await page.evaluate(() => document.body.scrollWidth <= innerWidth),
    ).toBe(true);
  });
}

test("opportunity filters, detail and scope survive navigation", async ({
  page,
}) => {
  await page.addInitScript(() =>
    localStorage.setItem("brickpulse.locale", "en"),
  );
  await page.goto("/recommendations");
  const resource = page.locator("a.resource-link").first();
  await expect(resource).toBeVisible();
  await resource.click();
  await expect(
    page
      .getByRole("heading", { name: "Measured evidence", exact: true })
      .first(),
  ).toBeVisible();
  const response = await page.request.get("/api/workspaces");
  const workspaces = await response.json();
  const id = workspaces[0].workspace_id;
  await page.goto(`/recommendations?workspace_id=${id}`);
  await expect(
    page.getByRole("combobox", { name: "Workspace", exact: true }),
  ).toHaveValue(id);
});

test("all analysis routes load", async ({ page }) => {
  for (const path of [
    "/next-best-actions",
    "/workspaces",
    "/cost-anomalies",
    "/tagging",
    "/savings-realized",
    "/serverless",
    "/labs",
    "/rules",
    "/data-health",
    "/settings",
    "/apps",
    "/genie-costs",
  ]) {
    await page.goto(path);
    await expect(page.locator("h1")).toBeVisible();
    await expect(page.getByRole("alert")).toHaveCount(0);
  }
});

/** Opens the first resource a rule flags and expands that rule's option if collapsed. */
async function openRule(page, ruleId: string) {
  await page.addInitScript(() =>
    localStorage.setItem("brickpulse.locale", "en"),
  );
  const rules = await (await page.request.get("/api/rules")).json();
  const title = rules.find((r) => r.id === ruleId).title;
  await page.goto(`/recommendations?rule_ids=${ruleId}`);
  await page.locator("a.resource-link").first().click();
  await expect(page.locator("h1")).toBeVisible();
  const summary = page.locator("details.option-collapsed > summary", {
    hasText: title,
  });
  if (await summary.count()) await summary.first().click();
}

test("serverless auto-stop evidence shows the decision, not raw keys", async ({
  page,
}) => {
  await openRule(page, "warehouse_autostop_serverless");
  await expect(
    page.getByRole("heading", { name: "Settings compared" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "How long the idle gaps between queries are",
    }),
  ).toBeVisible();
  await expect(page.getByText("gap_buckets.lo")).toHaveCount(0);
});

test("an hour drills into its minutes and back", async ({ page }) => {
  await openRule(page, "cluster_autotermination");
  const hour = page.locator(".hour-bars button").nth(9);
  await hour.scrollIntoViewIfNeeded();
  await hour.click();
  const back = page.getByRole("button", { name: "Back to hours" });
  await expect(back).toBeFocused();
  await expect(page.locator(".hour-bars").first().locator("> *")).toHaveCount(
    60,
  );
  await back.click();
  await expect(page.locator(".hour-bars button").nth(9)).toBeFocused();
});

test("warehouse right-size shows one gauge per guard signal", async ({
  page,
}) => {
  await openRule(page, "warehouse_rightsize");
  await expect(page.locator(".gauge-list li")).toHaveCount(4);
  await expect(page.getByText("Peak Spark slots used")).toBeVisible();
});

test("overview breakdowns add up to the headline estimate", async ({
  page,
}) => {
  await page.addInitScript(() =>
    localStorage.setItem("brickpulse.locale", "en"),
  );
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Biggest levers" }),
  ).toBeVisible();
  const overview = await (
    await page.request.get("/api/account/overview")
  ).json();
  for (const slices of [
    overview.savings_by_category,
    overview.savings_by_rule,
  ]) {
    const sum = slices.reduce((s, x) => s + x.monthly_savings_usd, 0);
    expect(
      Math.abs(sum - overview.total_monthly_savings_addressable_usd),
    ).toBeLessThan(0.05);
  }
});

test("observed spend shows a skeleton, not 'unavailable', while it loads", async ({
  page,
}) => {
  await page.addInitScript(() =>
    localStorage.setItem("brickpulse.locale", "en"),
  );
  await page.route("**/api/spend", async (route) => {
    await new Promise((r) => setTimeout(r, 1500));
    await route.continue();
  });
  await page.goto("/");
  const spend = page.locator(".metric", { hasText: "Observed spend" });
  await expect(spend.locator(".metric-pending")).toBeVisible();
  await expect(spend.getByText("Unavailable")).toHaveCount(0);
  await expect(
    page.getByText("Account · metastore region", { exact: true }),
  ).toHaveCount(0);
});

test("every generic evidence chart is titled, localized and has no raw keys", async ({
  page,
}) => {
  await page.addInitScript(() =>
    localStorage.setItem("brickpulse.locale", "pt-BR"),
  );
  for (const rule of [
    "alert_hygiene",
    "idle_serving_endpoints",
    "cluster_autotermination",
    "unpruned_repeated_queries",
  ]) {
    await page.goto(`/recommendations?rule_ids=${rule}`);
    await page.locator("a.resource-link").first().click();
    // The rule's own option may be collapsed under "other options" when it isn't the best.
    const section = page.locator(".evidence-section");
    await expect(section.locator(".ev-panel h4").first()).toBeAttached();
    await expect(section.locator("select")).toHaveCount(0);
    for (const raw of ["days.w", "days.a", "series.ts", "evaluations"])
      await expect(section.getByText(raw, { exact: true })).toHaveCount(0);
  }
  await page.goto("/recommendations?rule_ids=alert_hygiene");
  await page.locator("a.resource-link").first().click();
  await expect(
    page.getByRole("heading", { name: "Avaliações do alerta por dia" }),
  ).toBeVisible();
});

test("observed metrics read as names with their system-table source", async ({
  page,
}) => {
  await openRule(page, "idle_serving_endpoints");
  const metrics = page.locator("details.raw-evidence", {
    hasText: "Observed metrics",
  });
  await metrics.locator("summary").click();
  await expect(metrics.getByText("Days observed")).toBeVisible();
  await expect(
    metrics.locator("td code", { hasText: /^observed_days$/ }),
  ).toHaveCount(0);
  // The toggletip closes on scroll, and a late scroll can land right after the click.
  await expect(async () => {
    await metrics.getByRole("button", { name: "About: Days observed" }).click();
    await expect(page.locator(".info-tip-bubble")).toContainText(
      "system.billing.usage",
      { timeout: 1_000 },
    );
  }).toPass();
});

test("the state heatmap reads out hours and lists a day's periods", async ({
  page,
}) => {
  await openRule(page, "warehouse_keepalive_queries");
  const day = page.locator(".heatmap-day").nth(2);
  await day.scrollIntoViewIfNeeded();
  await day.click();
  await expect(day).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".run-list li").first()).toBeVisible();
  await page.locator(".state-grid .heat-cell").nth(30).hover();
  await expect(page.locator(".heatmap-readout")).toContainText("UTC");
});

test("the selected workspace survives in-app navigation; only the picker clears it", async ({
  page,
}) => {
  await page.goto("/workspaces");
  const link = page.locator("td .resource-link").first();
  const name = (await link.innerText()).trim();
  await link.click();
  const picker = page.locator(".workspace-picker select");
  await expect(picker.locator("option:checked")).toHaveText(name);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Overview");
  await page.locator(".resource-panel .text-action").click();
  await expect(page).toHaveURL(/\/recommendations\?workspace_id=/);
  await expect(picker.locator("option:checked")).toHaveText(name);
  if (await page.locator(".mobile-open").isVisible())
    await page.locator(".mobile-open").click();
  await page.locator(".sidebar nav a", { hasText: "Cost anomalies" }).click();
  await expect(page).toHaveURL(/workspace_id=/);
  await expect(picker.locator("option:checked")).toHaveText(name);
  await picker.selectOption("");
  await expect(page).not.toHaveURL(/workspace_id=/);
  // Old shared links still land on the filtered page.
  await page.goto("/workspaces/6120394857612039/cost-anomalies");
  await expect(page).toHaveURL(
    /\/cost-anomalies\?workspace_id=6120394857612039/,
  );
});
