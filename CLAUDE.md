# Local command centre (D:\vault\automation)

## Rules
- Never read or print `.env` (denied in `.claude/settings.json`; it holds API keys and mail credentials).
- Research and Campaign output must be facts copied from sources, never invented.
- The digest may add analysis only when each such sentence starts with "Possible India impact:".
- Make small edits. Copy a file to a `.bak` before editing it.
- Restart the app with: `Stop-ScheduledTask -TaskName "CommandCentre"; Start-ScheduledTask -TaskName "CommandCentre"`

## Map
- `leads/leads_app.py` — Flask app on port 5000, run by the scheduled task "CommandCentre". Single file (~2000 lines), SQLite at `leads/leads.db`, local Ollama model `qwen2.5:3b`, optional Gemini.
  - Campaigns: `find`, `plan_leads`, `osm_fetch`, `web`, `extract_businesses`, `clean_phones`, `clean_emails`, `relevant`, `run_campaign`, `draft_all`; progress at `/api/log/<id>`; lists at `/leads`.
  - Digest control: `/digests`, `/api/digest/*` (schedule, run, log).
  - `/notes`, `/testing` (Playwright-style test runner), `/research` (`run_research`, `verify_answer`, `link_cites`).
- `research/morning_digest.py` — builds and emails the daily digest (Google News RSS, yfinance market table, trafilatura, local LLM). Sections and tickers are constants at the top; log at `D:\vault\outputs\logs\digest.log`, reports in `D:\vault\outputs\reports`.
- `research/daily_brief.py` — older brief script.
- `leads/show_last.py` — prints the latest campaign and its leads from the DB.
- `leads/patch_*.py`, `research/patch_*.py` — one-off patch scripts (gitignored); `*.bak*` are manual backups.
- `mailtest*.py` — SMTP test scripts. `n8n/` — ignored.

## Leads engine (Stage 1 / 1.5)
- **/system** page: Gemini usage (`gemini_calls`: kind `call`, `fallback`, `digest`), model counts, last error.
- **Master list** `/businesses` (+ `/businesses/export.csv`): tables `businesses`, `campaign_businesses`, `coverage_log`, `business_review`, `entity_profiles`. Every campaign merges into it.
  - Dedupe: same phone, or same `name_key` + city (or very close lat/lon) merges into the existing row (`merge_rec`); an uncertain match goes to `business_review` (`/businesses/review`) for a manual merge/keep-separate decision.
  - Pipeline fields: `status`, `last_contacted`, `owner_notes`, `do_not_contact`, `quality_score`, `last_verified`.
  - 30-day banner: businesses seen within `TRUST_DAYS` (30) are offered on the campaign pre-start (`/campaign/<id>/prestart`) before a new search.
- **Phone finder** `find_phones`: searches for a business name's phone from public pages; only numbers copied from sources.
- **Flagged leads**: name and address from a directory page with no published phone are kept but flagged.
- **Map (OSM) search**: `osm_fetch` builds tags plus a name regex (`name~`, shop-tagged places); answers cached 24 h in `leads/osm_cache.json` (gitignored). Tags/names = union of the `entity_profiles` AI profile, the planner's `osm` tags and `ENTITY_TAGS`/`ENTITY_NAMES`.
- **Saturation rule**: web search stops after 12 consecutive searches with results but nothing new (failed/empty searches don't count).
- **entity_profiles** cache: one AI call per entity (tags + local-language name words), cached by lowercase entity key.
- Backups (`*.bak*`, `leads.db.bak*`) and `osm_cache.json` are gitignored.
