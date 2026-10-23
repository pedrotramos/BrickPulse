---
name: BrickPulse
description: A light analytical workbench for Databricks financial observability.
colors:
  logo-orange: "#b94b2c"
  accent: "#115e67"
  accent-hover: "#0c4c54"
  selected: "#dcebee"
  canvas: "#f5f7f7"
  surface: "#fff"
  sidebar: "#eef2f2"
  ink: "#243439"
  muted: "#596d73"
  line: "#dce4e5"
  control-border: "#c7d3d6"
  control-hover: "#e9eff0"
  control-active: "#dce6e8"
  focus: "#218492"
  chart-observed: "#354e56"
  chart-prior: "#7a8d93"
  chart-active: "#23606c"
  chart-idle: "#c08a35"
  chart-off: "#e3eaec"
  warning: "#80530c"
  warning-bg: "#fff7e7"
  error: "#aa3743"
  badge-neutral-bg: "#edf1f2"
  badge-neutral-ink: "#52676e"
  badge-warning-bg: "#fff0d7"
  badge-warning-ink: "#805214"
  badge-estimated-bg: "#edf2f4"
  badge-estimated-ink: "#456472"
  badge-pass-bg: "#e4f2eb"
  badge-pass-ink: "#2c6a50"
  badge-fail-bg: "#fbe9eb"
  badge-fail-ink: "#a33343"
  badge-advisory-bg: "#eff1f2"
  badge-advisory-ink: "#596d73"
typography:
  display:
    fontFamily: '"Instrument Sans Variable", "IBM Plex Sans", sans-serif'
    fontSize: "28px"
    fontWeight: 650
    lineHeight: 1.18
    letterSpacing: "-0.025em"
  headline:
    fontFamily: '"Instrument Sans Variable", "IBM Plex Sans", sans-serif'
    fontSize: "17px"
    fontWeight: 650
    lineHeight: 1.4
    letterSpacing: "-0.012em"
  title:
    fontFamily: '"Instrument Sans Variable", "IBM Plex Sans", sans-serif'
    fontSize: "15px"
    fontWeight: 650
    lineHeight: 1.4
  body:
    fontFamily: '"Instrument Sans Variable", "IBM Plex Sans", sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: '"Instrument Sans Variable", "IBM Plex Sans", sans-serif'
    fontSize: "13px"
    fontWeight: 550
    lineHeight: 1.4
  data:
    fontFamily: '"JetBrains Mono", "IBM Plex Mono", monospace'
    fontSize: "0.9em"
  metric:
    fontFamily: '"JetBrains Mono", "IBM Plex Mono", monospace'
    fontSize: "1.5rem"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "-0.035em"
rounded:
  heat-cell: "2px"
  badge: "4px"
  control: "6px"
  menu: "8px"
  panel: "12px"
spacing:
  small-gap: "8px"
  control-inline: "12px"
  row: "16px"
  section-heading: "18px"
  chart-gap: "22px"
  panel-inline: "24px"
  desktop-gutter: "36px"
components:
  button-default:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "7px 12px"
  button-default-hover:
    backgroundColor: "{colors.control-hover}"
  button-default-active:
    backgroundColor: "{colors.control-active}"
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "7px 12px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
    textColor: "{colors.surface}"
  button-subtle:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.control}"
    padding: "7px 12px"
  button-icon:
    backgroundColor: "transparent"
    textColor: "#60757c"
    rounded: "{rounded.control}"
    padding: "6px"
    width: "34px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "7px 10px"
  navigation-selected:
    backgroundColor: "{colors.selected}"
    textColor: "#0c505b"
    rounded: "{rounded.control}"
    padding: "9px 12px"
  badge:
    backgroundColor: "{colors.badge-neutral-bg}"
    textColor: "{colors.badge-neutral-ink}"
    rounded: "{rounded.badge}"
    padding: "3px 7px"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "22px 24px"
  metric:
    textColor: "{colors.ink}"
    typography: "{typography.metric}"
---

# Design System: BrickPulse

## Overview

**Creative North Star: "Analytical Workbench"**

BrickPulse uses a light, precise workbench for engineers investigating Databricks spend and leaders reading the same evidence at a glance. Cool neutral surroundings, white work surfaces, graphite text and a petrol accent make financial facts, priorities and resource relationships easy to scan. Density comes from compact typography, aligned values and restrained dividers.

The visual system keeps explanation beside the number: scope, period, confidence, source and freshness remain legible context. Panels organize evidence without decorative depth. The original Frequência mark provides identity through five rounded bars in Logo Orange; charts, tables and text provide the substance. The implemented interface requires no photographic or illustrative raster assets.

**Key Characteristics:**

- Light neutral canvas and white analytical surfaces.
- Graphite reading text with petrol actions, selection and opportunity values.
- The original five-bar Frequência logo in orange, separate from the petrol interface accent.
- Instrument Sans for interface language; JetBrains Mono for values and technical evidence.
- Flat panels, precise dividers and compact controls.
- Explicit financial context, honest unavailable states and accessible data alternatives.

## Colors

The palette has a petrol interface accent, a separate orange logo, cool neutrals and semantic status treatments. Frontmatter values are extracted from `frontend/src/styles.css`, except `logo-orange`, which comes from the fill in `frontend/logo-options/svg/03-frequencia-symbol-color.svg`; these are the normative values. The logo color does not change the interface accent.

### Primary

- **Petrol** (`accent`): links, primary actions and opportunity amounts.
- **Deep Petrol** (`accent-hover`): hover feedback for primary actions and pressed links.
- **Pale Petrol** (`selected`): selected navigation, selected filters and highlighted menu items.

### Secondary

- **Logo Orange** (`logo-orange`): the original Frequência symbol in the sidebar and browser favicon. This is an SVG asset color, not a CSS custom property or a replacement for petrol actions and selection.

### Neutral

- **Cool Canvas** (`canvas`), **White Surface** (`surface`) and **Sidebar Neutral** (`sidebar`): distinct application, work-surface and navigation planes.
- **Graphite** (`ink`): principal reading text.
- **Muted Graphite** (`muted`): supporting explanations, resource metadata and footer text. Preserve this shared token in the latter two locations; their earlier lighter colors were corrected in the finish review.
- **Divider Neutral** (`line`) and **Control Stroke** (`control-border`): structural boundaries.
- **Control Hover** (`control-hover`) and **Control Pressed** (`control-active`): neutral action feedback.
- **Focus Petrol** (`focus`): the visible keyboard outline.

### Data and semantic roles

- **Observed Graphite** (`chart-observed`): active spend and evidence series. **Prior Gray** (`chart-prior`) is the reference layer: the 7-day average, limit lines, the last-active marker and candidate values outside an acceptability limit. It meets 3:1 against white so a reference is never fainter than WCAG 1.4.11 allows.
- **In-use Petrol** (`chart-active`), **Idle Ochre** (`chart-idle`) and **Off Mist** (`chart-off`) are the resource-state vocabulary shared by every usage chart: in use, running but idle (the reclaimable spend) and off/not billed. Intensity heatmaps step from Off Mist to In-use Petrol in five levels, and any non-zero value is at least the first step, because one query is enough to keep a warehouse warm.
- **Warning Amber** (`warning`, `warning-bg`) and **Error Red** (`error`): declared warning and error vocabulary. Warning notices use the warning background with their own darker copy and border; do not substitute badge colors indiscriminately.
- Badge pairs separately encode neutral, warning/high/spike, estimated/medium/step-change, pass/bookable, fail and advisory/low/disabled states. Every badge includes a readable text label.

**The Meaning Rule.** Use petrol for actions, selection and opportunity emphasis; retain graphite for the implemented observed data series. A color alone must not establish whether a value is observed, estimated, unavailable or synthetic.

## Typography

**Display and Body Font:** Instrument Sans Variable, with IBM Plex Sans and sans-serif fallbacks.

**Data and Technical Font:** JetBrains Mono, with IBM Plex Mono and monospace fallbacks.

Both active families are self-hosted through Fontsource imports in `frontend/src/main.tsx`. Instrument Sans and JetBrains Mono were the disclosed locally available substitution for IBM Plex. JetBrains Mono loads its regular (400) and medium (500) weights through unicode-range files, so browsers fetch only the subsets a page uses; metric values render at the medium weight.

The hierarchy is compact and role-based rather than a regular mathematical scale. The frontmatter records desktop roles. Page headings shrink on narrow screens; data values adjust to their container.

- **Display:** page heading, using the display role; balances wrapping.
- **Headline:** panel heading, using the headline role.
- **Title:** subsection heading, using the title role.
- **Body:** general interface text, using the body role.
- **Label:** metric labels, using the label role. Navigation uses the same size and weight with inherited line height.
- **Data:** money, numeric columns, units, code and evidence use the mono role and tabular numerals.
- **Metric:** large financial values use the metric role. Values become (22px) at the medium and phone breakpoints and (30px) above the wide breakpoint.
- Sizes are rem role tokens on a 100% root, so the reader's browser font size scales the whole interface: headline, title, body (14px), label (13px), small (12px) and micro (11px). Notes, footnotes, controls and table text use small; table headers, badges, chart axes and secondary cell metadata use micro, the floor. Inline code never renders below micro. On phones, form fields render at 16px to prevent focus zoom while placeholders stay at body size. Explanatory prose is constrained by context, including page descriptions (74ch), guidance (70ch) and notices (75ch).

**The Display and Data Rule.** Keep interface language in Instrument Sans and comparable values or technical evidence in JetBrains Mono. Right-align numeric table cells and retain tabular numerals.

**The Locale Rule.** Format visible currency, numbers and dates for pt-BR, English or Spanish while keeping USD and the underlying calculations independent of language. Preserve text wrapping for localized labels.

## Layout

The desktop shell uses a fixed sidebar (232px), a toolbar (70px) and a fluid main column capped at (1740px). The main gutter is (36px); white panels use the extracted panel padding. Internal layouts use flexible tracks with a zero minimum so the work area can shrink without forcing the page wider.

The default metric strip has three equal columns with vertical separators. Overview charts use a wide evidence track and a compact confidence track, with a gap from the extracted chart-gap token. Filters and evidence metadata wrap. Long identifiers wrap or truncate in their own context; they do not widen the application shell.

Responsive behavior follows the implemented viewport thresholds:

- At or below (1190px), the sidebar narrows to (210px), content gutters become (24px), charts stack and the confidence panel uses two internal columns.
- At or below (850px), the sidebar becomes an off-canvas drawer (250px), the workspace fills the viewport and the toolbar becomes (64px). The drawer has a scrim, Escape handling, contained keyboard focus and inert background content while open.
- At or below (560px), content gutters become (16px), the main title becomes (25px), metrics stack into label/value rows, panels tighten their padding, filters wrap and guidance becomes one column. The language button retains its accessible name while hiding the visible language word.
- At or above (1550px), content gutters expand to (42px) and the overview chart proportion increases.
- At or below (860px) tall, the whole sidebar scrolls and its vertical rhythm tightens, so every destination stays visible on short screens. On narrow phones the workspace picker truncates before any toolbar button shrinks.

**The Table Continuity Rule.** Preserve the table's columns in a horizontally scrollable, focusable region with an accessible caption/name. Keep numeric values unbroken and aligned; do not remove evidence columns to make a phone screenshot fit. The current review confirmed this structure in source but did not exercise scrolling or keyboard behavior.

## Elevation & Depth

The interface is flat at rest. White work surfaces, cool surroundings, one-pixel borders and section dividers establish grouping. Panels have no decorative shadows. The language menu alone uses the implemented overlay shadow (`0 8px 30px #24343926`) to separate floating choices from the workbench. The mobile drawer uses a translucent scrim (`#172d3b66`).

**The Structural Depth Rule.** Use borders and surface tones for analytical grouping; reserve the existing shadow for the floating menu.

Motion is limited to control background/color feedback (0.16s ease-out) and drawer movement (0.18s ease-out). Shared charts disable entry animation. The reduced-motion query removes animations, transitions and smooth scrolling.

## Shapes

Panels and findings use softly rounded corners from the panel token. Controls and navigation use the control token; badges use the tighter badge token. The menu has its own corner size, and heatmap cells have a minimal rounding. Borders remain crisp and one pixel wide. The original Frequência brand mark is a vector of five vertical rounded bars in a square viewBox (96 × 96): each bar is (12) units wide with a corner radius (6), and their heights are (22, 50, 80, 44, 24) units from left to right.

**The Shape Hierarchy Rule.** Keep work surfaces, controls and status tags on their existing separate corner sizes; this small hierarchy supplies the workbench's form language.

**The Original Logo Rule.** Use the exact original Frequência symbol from `frontend/logo-options/svg/03-frequencia-symbol-color.svg`, preserving all five rounded bars, their positions and Logo Orange. The sidebar and favicon use the same SVG; do not redraw, recolor or combine the approved symbol with another mark.

## Components

### Buttons

Compact and explicit. Standard buttons use a white surface, graphite text, a control stroke and the extracted button padding, with a minimum height (36px). Primary actions use petrol and white; their hover uses Deep Petrol. Secondary actions retain neutral hover and pressed surfaces. Subtle buttons remove the visible default stroke/background and use muted text. Icon buttons are compact (34px wide with a 34px minimum height); supply an accessible name. On touch input (`pointer: coarse`), whatever the viewport, standalone controls, icon buttons, navigation rows and menu items reach 44px; standalone text links and disclosure summaries keep their visual size and gain an invisible 44px hit area. Hover feedback applies only on hover-capable devices, so taps do not leave stuck highlights.

All keyboard-focusable controls inherit a visible focus outline (3px) offset by (3px). Disabled buttons use reduced opacity (0.45) and a not-allowed cursor. Hover/active rules exclude disabled buttons. Primary links styled as buttons use the same geometry with (12px) text.

### Inputs / Fields

Native inputs, selects and textareas share the control stroke, control radius and a minimum height (36px). Their default padding is (7px 10px). Search combines an inline icon with an unbordered input inside one stroked container; focus within adds a petrol outline (2px) offset by (1px). Native field focus remains visibly outlined, and text-entry caret color uses petrol. Labels and accessible names remain part of the field.

### Navigation

Desktop rows combine a small line icon with a text label. Default rows are muted, hover adds a neutral fill, and the active route uses Pale Petrol with darker petrol text and stronger weight. Groups use small letter-spaced labels. Scoped links retain the workspace query context. On small screens the same navigation becomes the drawer described in Layout.

### Chips / Badges

Status badges are small rectangular tags with tight corners, padding (3px 7px), a label (10px) and line height (1.4). Their neutral, warning, estimated, pass, fail and advisory pairs carry semantic meaning. Selected preset filters are buttons, visually distinct from passive badges; their active state adds Pale Petrol and a visible stroke.

### Cards / Containers

Panels are white, outlined and softly rounded, with default padding (22px 24px). Nested panels remove redundant framing. Resource-table panels remove interior padding and clip the outer shape, then give headings and cells their own spacing. On phones ordinary panels use (18px 16px).

### Financial metrics

Metrics are a description list: label, value and explanatory note. Opportunity values can use petrol. Null values render a localized unavailable label rather than zero. Notes keep the financial period and caveat attached to the amount; tracked savings show the actual start/end window or the localized unknown-window fallback. Monthly estimates and savings tracked over a specific observation window remain distinct.

**The Evidence Rule.** Keep period, source, confidence, freshness and estimate caveats close to the amount they explain. Preserve visible demo labeling and distinguish estimated opportunity from observed spend or tracked changes; never imply an invoice or guaranteed saving.

### Tables and charts

Tables use small neutral headers, fine row separators, white bodies and a restrained row-hover fill. Resource names lead; supporting metadata uses Muted Graphite. Numeric columns align right and stay unbroken. Scroll regions are keyboard-focusable and named. Shared charts use the observed series color, a zero baseline, horizontal grid lines, localized axes and tooltips, plus a disclosure exposing the underlying data.

Evidence on a resource is drawn per decision, not per payload. Each evidence kind with a shape of its own has a dedicated view (auto-stop trade-off, warehouse right-size headroom, cluster daily activity, weekday × hour usage), and the rest fall back to the shared bar chart with localized series names. The patterns:

- **Setting change:** today → proposed as two small mono values, the proposal tinted with the selected fill. An auto-stop that is off today is said in a warning notice, not implied.
- **One scale per signal:** guard signals (slots, queue, spill, scan) each get their own gauge against their own limit, with "x% of the limit" in text and a minimum visible fill. Units are never mixed on one axis.
- **Proposal versus limit:** the proposed candidate uses the accent; candidates outside the acceptability limit drop to Prior Gray, and if the proposal itself is outside the limit a warning says so even when the payload did not flag it.
- **Hour → minute drill:** hour bars are buttons when a minute profile exists; focus moves to "Back to hours" and returns to the hour on the way back. On touch the bars keep a 24px target and scroll horizontally instead of shrinking.
- **Partial days:** the current UTC day is dashed and excluded from the 7-day average, so an accruing day never reads as a drop.
- **Provenance:** the rule's own measurement note sits in a "How it was measured" disclosure, kept in English with original table names.

Overview breakdowns (by category, biggest levers, top workspaces) are ranked horizontal share bars rather than donuts, and each breakdown is composed per spend scope so it adds up exactly to the estimated opportunity.

On a resource with several overlapping options, the best-ranked option opens expanded under a "Best option" label and the alternatives collapse to one-line summaries (title, monthly amount, effort, priority, status); a link to an option's anchor opens it.

### Notices and states

Information and warning notices use their own light fills, border treatments and explicit copy. Empty, error and loading states reserve a stable content area. Errors expose retry; empty states link to data health or offer filter reset. Skeletons are static blocks. Missing data retains an honest unavailable state and accompanying context.

This document records the implemented system; it is not a design review verdict.

## Do's and Don'ts

### Do:

- **Do** preserve the cool neutral canvas, white surfaces, graphite text and petrol action/selection accent.
- **Do** separate interface typography from tabular numeric and technical typography.
- **Do** keep financial period, source, confidence, freshness and estimate caveats beside their values.
- **Do** retain meaningful status text, visible focus and horizontal access to complete tables.
- **Do** carry workspace scope and localized formatting consistently between routes.

### Don't:

- **Don't** render missing information as zero spend or no opportunity.
- **Don't** present estimates as invoices, guaranteed savings or executed resource changes.
- **Don't** rely on color alone to distinguish observed, estimated, unavailable or demo information.
- **Don't** replace the flat panel hierarchy with decorative elevation.
- **Don't** remove financial context or evidence columns to compress a narrow layout.

