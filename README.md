# Opportunity Hunter

A **standalone** low-cost hiring-post discovery dashboard. It is not part of JobHunter AI. The app is a single small Python service with a static dashboard, persistent SQLite database, an hourly background scheduler, RSS/Atom ingestion, deduplication, scan logs, and opportunity status tracking.

## Current capabilities

- Standalone dashboard with opportunity inbox, search/filter, score, source, timestamp, original link, and status tracking (`new`, `saved`, `contacted`, `applied`, `dismissed`).
- Hourly scan loop (defaults to 3,600 seconds), plus a manual **Scan now** action.
- RSS 2.0 and Atom feed ingestion, including feeds from search-alert tools that surface public LinkedIn posts.
- Add posts manually with a public URL and pasted text.
- Persistent SQLite storage (Docker named volume locally; persistent disk on Render blueprint).
- URL-based deduplication with tracking query parameters removed.
- Transparent, no-cost rules-based qualification by default; optional OpenAI-compatible chat-completions enrichment with `LLM_API_KEY`.
- Scan history, feed status, health endpoint (`/health`), JSON API.
- Python standard library only; no pip dependencies.

## Important: LinkedIn access is not preconfigured

This project does **not** log into LinkedIn, use private endpoints, or scrape LinkedIn pages. It does not claim to have a LinkedIn integration. For LinkedIn-first discovery, configure an RSS/Atom feed from a search-alert provider you are authorized to use. A practical setup is to create an alert query for public hiring posts, such as `site:linkedin.com/posts (hiring OR referral) (Java OR backend)`, and add its RSS feed URL under **Discovery sources**. Feed providers differ; test that the feed actually returns relevant public posts. Alternatively paste a public post URL and its text manually. If no feed is configured, scheduled scans will run but will discover nothing.

## Run locally

```bash
cp .env.example .env  # optional; export desired values in your shell or use docker compose --env-file .env
# easiest
docker compose up --build
```

Open http://localhost:8080. To run without Docker:

```bash
python app.py
```

The database defaults to `./data/opportunity_hunter.db`. Override with `DATABASE_PATH`.

## Run tests

```bash
python -m unittest discover -s tests -v
```

## Cloud deployment

1. Push this directory as a **new repository** (for example `opportunity-hunter`) separate from `jobhunter-ai`.
2. In Render, create a new Blueprint from `render.yaml`, or create a Docker web service using this directory.
3. Keep the persistent disk mounted at `/data`; without persistent storage SQLite data can be lost on redeploy/restart.
4. Set `LINKEDIN_ALERT_RSS_URL` if you have a single primary alert feed, or add more feeds in the dashboard.
5. Deploy and verify `/health`, then use **Scan now** and inspect Scan history.

The included Render blueprint uses the `starter` plan because an always-on service and persistent disk are required. Free hosting tiers may sleep or lack persistent disk, so they cannot guarantee hourly execution. Pricing and plan availability may change; check the host before deployment. This repository does not deploy itself and no live cloud service has been created by generating these files.

## Optional AI qualification

Leave `LLM_API_KEY` empty for no model charges. If set, the app sends each newly discovered item's title, body (up to 5,000 characters), and URL to the configured OpenAI-compatible chat-completions endpoint. This is optional and may incur charges; review provider privacy and pricing. If a model call fails, the app falls back to rules-based scoring.

## API overview

- `GET /health`
- `GET /api/dashboard`
- `GET /api/opportunities?status=new&q=java&min_score=55`
- `PATCH /api/opportunities/{id}` with `{"status":"saved"}`
- `GET /api/sources`, `POST /api/sources` with `{"name":"Alert feed","feed_url":"https://..."}`
- `GET /api/scans`
- `POST /api/scan`
- `POST /api/opportunities/manual` with `title`, `url`, optional `company`, `body`

## Known MVP limitations

- Single-process scheduler: keep one web instance running to avoid duplicate hourly scans. The included host blueprint uses one instance.
- No authentication in v0.1. Do not expose this dashboard publicly without adding authentication or access controls.
- RSS/search alerts depend on third-party availability and can be delayed, incomplete, or return irrelevant results.
- Rules-based scoring is a heuristic, not a guarantee of fit. Remote eligibility, years of experience, role seniority, and application status must be verified on the original source.
- Company is not automatically extracted from every feed item; manually add company data where necessary.
- LLM qualification is optional, not enabled by default, and is not a substitute for verifying the original post.
