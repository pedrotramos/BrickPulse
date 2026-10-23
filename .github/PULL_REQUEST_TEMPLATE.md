## What and why

<!-- What does this change do, and why? Link the issue it closes: "Closes #123". -->

## How it was tested

<!-- Commands you ran, screenshots for UI changes, workspace checks for SQL/pipeline changes. -->

## Checklist

- [ ] `ruff check .` and `pytest -q` pass
- [ ] `npm --prefix frontend run typecheck` and `npm run build` pass (frontend changes)
- [ ] Rebuilt `src/brickpulse/server/static` is committed (frontend changes)
- [ ] UI strings exist in all three locales (`en`, `es`, `pt-BR`)
- [ ] User-facing docs updated, in all three languages or flagged for `translation-needed`
- [ ] `CHANGELOG.md` updated under **Unreleased** (user-visible changes)
- [ ] The financial baseline is unchanged, or the diff is intended and explained below
- [ ] No write path to analyzed resources was added (BrickPulse stays read-only)
