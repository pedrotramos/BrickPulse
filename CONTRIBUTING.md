# Contributing to BrickPulse

Thanks for your interest in improving BrickPulse. New cost rules, bug fixes, translations and docs are all
welcome. By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- **Report a bug** or **propose a feature** through the
  [issue templates](https://github.com/pedrotramos/BrickPulse/issues/new/choose).
- **Propose a new cost rule** with the *New rule* template. Saying which system tables it reads and how it
  estimates savings early saves review time.
- **Improve translations** of the UI (`frontend/src/locales/`, `src/brickpulse/server/i18n/`) or of the user
  guides (`docs/es/`, `docs/pt-BR/`).
- **Fix docs**, including typos. Small PRs are great.

For anything large, such as a new data source, a change to the data model or a UI redesign, open an issue
first so we can agree on the approach before you invest time in it.

## Development setup

Follow [getting started](docs/en/getting-started.md). In short:

```sh
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
npm --prefix frontend ci
npm run dev
```

## Making a change

1. Fork the repository and create a branch from `main`, e.g. `fix/warehouse-autostop-threshold`.
2. Make your change, with tests.
3. Run the checks (CI runs the same ones):

   ```sh
   .venv/bin/python -m ruff check .
   .venv/bin/python -m pytest -q
   npm --prefix frontend run typecheck
   npm run build
   ```

4. Update [CHANGELOG.md](CHANGELOG.md) under **Unreleased** if users will notice the change.
5. Open a pull request and fill in the template.

Write commit messages in the imperative ("Add warehouse idle rule", "Fix spend total for empty scope").
[Conventional Commits](https://www.conventionalcommits.org/) prefixes are welcome but not required.

## Project conventions

- **Read-only, always.** BrickPulse never changes the resources it analyzes. PRs that add write paths to
  customer resources won't be accepted.
- **Contracts are additive.** The `finops_*` table names, finding and group ids, and the recommendation
  schema are stable. Add columns; never rename or repurpose them. See [DATA_MODEL.md](docs/DATA_MODEL.md).
- **Financial baseline.** `tests/fixtures/financial_baseline.json` pins the numbers. If your change is meant
  to move them, regenerate the baseline as described in [TESTING.md](docs/TESTING.md) and explain the diff
  in the PR.
- **Unknown is not zero.** A missing or unreadable source must show up as *unknown*, never as zero spend or
  "no findings".
- **Frontend build output is committed.** The deployed app serves `src/brickpulse/server/static` from the
  synced sources. When you change `frontend/`, run `npm run build` and commit the result.
  Dependabot can't build, so on its frontend PRs the *Rebuild frontend for Dependabot* workflow commits
  the rebuilt UI to the branch. Because of that commit, Dependabot no longer rebases those PRs by itself;
  comment `@dependabot recreate` when one falls behind `main`.
- **Python style.** Run `ruff check` (line length 110, Python 3.10 target) and match the style of the
  surrounding code.

## Adding a rule

Rules are self-contained plugins: a Python class, a Jinja SQL template and a thresholds block. The registry
discovers them, so no shared list needs editing. [ADDING_A_RULE.md](docs/ADDING_A_RULE.md) walks through the
three pieces, the shared pricing macro, the observation guard and the translations each rule needs.

## Translations

BrickPulse supports **English, Spanish and Brazilian Portuguese**.

- **UI strings.** Every key must exist in all three catalogs: `frontend/src/locales/{en,es,pt-BR}.json`
  for the frontend and `src/brickpulse/server/i18n/{en,es,pt-BR}.json` for the API. The test suite checks
  parity, so a missing key fails CI.
- **User guides.** `docs/en/` is the source of truth, and `docs/es/` and `docs/pt-BR/` mirror it with the
  same headings. If you change an English guide and can't update the translations, that's fine: say so in
  the PR and a maintainer will add the `translation-needed` label.
- **READMEs.** `README.md`, `README.es.md` and `README.pt-BR.md` follow the same rule.
- Contributor references (`docs/*.md`) are English only.

## Licensing of contributions

BrickPulse is licensed under the [Apache License 2.0](LICENSE). Under section 5 of the license, any
contribution you submit is licensed under the same terms, with no separate CLA. Only submit work you have
the right to license this way.

## Getting help

If something in this guide is unclear, open an issue. Improving this document counts as a contribution.
