# recruitinator

Maps every company in the IND recognized-sponsor register to their job-postings site and whether it offers a machine-parsable API. (Larger always-on job-monitor design notes live in [plans/architecture.md](plans/architecture.md) — backburnered.)

## How it works

`scripts/census.py` runs an LLM agent once per company from `netherlands-sponsors.csv`. The agent gets every MCP tool defined in `mcp.json` (DuckDuckGo search + fetch) plus one local tool, `append_census_row`, which it calls to record its findings in `census.csv`.

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

Resumable: companies already present in `data/census.csv` are skipped on restart. Useful flags: `--search <substring>`, `--limit N`, `--offset N`, `--redo`, `--max-steps N` (default 15).

## Files

- `scripts/census.py` — the census agent
- `mcp.json` — MCP tool servers given to the model (`uvx duckduckgo-mcp-server`, `uvx mcp-server-fetch`)
- `netherlands-sponsors.csv` — IND recognized-sponsor register (input, ~13k companies)
- `data/census.csv` — output: `company_name, job_postings_url, has_api, notes` (volume-mounted in Docker)
- `Dockerfile` / `docker-compose.yml` — containerized run; MCP servers pre-installed so startup needs no network
