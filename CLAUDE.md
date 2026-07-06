# CLAUDE.md

Guidance for Claude Code working in this repository. Everything substantive —
the goal & principles, layout, commands, architecture, serving model, CI, and
conventions — lives in [CONTRIBUTING.md](CONTRIBUTING.md); read it first.
Agent-specific notes:

- **Preview freely.** `just preview` streams sources from R2 and downloads no
  datasets — it's the intended fast feedback loop, not an expensive operation.
  Use it whenever you touch shading, contours, or the viewer. Inspect any
  `.pmtiles` by dragging it into https://protomaps.github.io/PMTiles/.
- **Check [sources/README.md](sources/README.md) before researching any data
  source** — it catalogs built, open, and ruled-out sources; the ruled-out table
  exists precisely so nobody re-researches them.
- Planned work lives in GitHub issues (`gh issue list`).
