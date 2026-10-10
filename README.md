# recruitinator

Maps every company in the IND recognized-sponsor register to their job-postings site and whether it offers a machine-parsable API. (Larger always-on job-monitor design notes live in [plans/architecture.md](plans/architecture.md) — backburnered.)

## How it works

`scripts/census.py` runs LLM agents over `netherlands-sponsors.csv` — several companies in flight at once (`--concurrency`, default 6). Each agent gets every MCP tool defined in `mcp.json` (SearXNG metasearch + fetch) plus one local tool, `append_census_row`, which it calls to record its findings in `census.csv`. Search goes through the self-hosted SearXNG instance in `docker-compose.yml`, so parallel agents never hit a third-party rate limit.

## Run

Docker only — the container builds its own environment (`uv sync` inside the image; the host `.venv` is never used, see [.dockerignore](.dockerignore)).

```sh
cp .env.example .env   # set OPENAI_API_KEY (optional: OPENAI_BASE_URL, OPENAI_MODEL)
docker compose up -d --build     # starts the census; survives crashes, resumes from data/census.csv
docker compose logs -f census
```

One-off runs inside the container, e.g. retrying the `has_api=unknown` rows:

```sh
docker compose run --rm census python scripts/census.py --search Gradyent --redo
```

Resumable: companies already present in `data/census.csv` are skipped on restart. Useful flags: `--search <substring>`, `--limit N`, `--offset N`, `--redo`, `--max-steps N` (default 15), `--concurrency N` (default 6).

## Files

- `scripts/census.py` — the census agent (parallel across companies)
- `mcp.json` — MCP tool servers given to the model (`uvx searxng-http-mcp` against the compose `searxng` service, `uvx mcp-server-fetch`)
- `core-config/settings.yml` — SearXNG instance config (JSON API enabled, valkey wired)
- `netherlands-sponsors.csv` — IND recognized-sponsor register (input, ~13k companies)
- `data/census.csv` — output: `company_name, job_postings_url, has_api, notes` (volume-mounted in Docker)
- `Dockerfile` / `docker-compose.yml` — containerized run (census + private SearXNG + valkey); MCP servers pre-installed so startup needs no network
