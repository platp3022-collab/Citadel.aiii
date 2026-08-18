# Claude Code skills (vendored)

Design skills downloaded from GitHub and vendored here so they load
automatically in this repo (`.claude/skills/<name>/SKILL.md`).

| Skill | Purpose | Source | Commit | License |
|---|---|---|---|---|
| `ui-ux-pro-max` | Design intelligence: searchable DB of styles, palettes, font pairings, charts, UX guidelines, per-stack rules | [nextlevelbuilder/ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) (`.claude/skills/ui-ux-pro-max`) | `8a1a6d8` | MIT |
| `design-taste-frontend` | Anti-slop frontend: landing pages, portfolios, redesigns that don't look templated | [leonxlnx/taste-skill](https://github.com/leonxlnx/taste-skill) (`skills/taste-skill`) | `dfb6f9f` | MIT |
| `high-end-visual-design` | Agency-grade polish: fonts, spacing, shadows, cards, motion — the "expensive" feel | [leonxlnx/taste-skill](https://github.com/leonxlnx/taste-skill) (`skills/soft-skill`) | `dfb6f9f` | MIT |
| `minimalist-ui` | Clean editorial interfaces, warm monochrome, flat bento grids, no visual noise | [leonxlnx/taste-skill](https://github.com/leonxlnx/taste-skill) (`skills/minimalist-skill`) | `dfb6f9f` | MIT |
| `shadcn` | Official shadcn/ui skill: component patterns, CLI, registry, MCP, composition rules | [shadcn-ui/ui](https://github.com/shadcn-ui/ui) (`skills/shadcn`) | `5c8f5b0` | MIT |
| `migrate-radix-to-base` | Миграция React/shadcn проектов с Radix UI на Base UI (по компоненту или целиком) | [shadcn-ui/ui](https://github.com/shadcn-ui/ui) (`skills/migrate-radix-to-base`) | `5c8f5b0` | MIT |

Each upstream project is MIT-licensed; copyright stays with the original authors
(Next Level Builder, Leonxlnx, shadcn).

## Updating

Re-copy the folder from upstream, e.g.:

```bash
git clone --depth 1 https://github.com/leonxlnx/taste-skill /tmp/taste-skill
cp -r /tmp/taste-skill/skills/minimalist-skill .claude/skills/minimalist-ui
```

## Alternative: install as plugins instead of vendoring

```bash
/plugin marketplace add nextlevelbuilder/ui-ux-pro-max-skill
/plugin install ui-ux-pro-max@ui-ux-pro-max-skill

/plugin marketplace add leonxlnx/taste-skill
/plugin install taste-skill@taste-skill
```

## MCP-серверы

`.mcp.json` в корне репозитория подключает hosted Supabase MCP server
([supabase/mcp](https://github.com/supabase/mcp), Apache-2.0):

```json
{ "mcpServers": { "supabase": { "type": "http", "url": "https://mcp.supabase.com/mcp?read_only=true" } } }
```

В репозитории `supabase/mcp` нет скиллов — это исходники самого MCP-сервера,
поэтому он подключается как сервис, а не копируется в `.claude/skills/`.
`read_only=true` стоит по умолчанию из соображений безопасности; убери параметр,
если нужны записывающие инструменты. При первом использовании клиент попросит
залогиниться в Supabase (OAuth), локальный CLI отдаёт сервер на
`http://localhost:54321/mcp`.
