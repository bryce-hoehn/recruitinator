"""Census agent: for each IND sponsor, let an LLM agent with MCP tools (SearXNG
metasearch, fetch, ...) find where the company posts its jobs and whether there's
an API, then have it append its findings to census.csv.

    for company in companies:
        prompt = "find where this company posts their jobs, and determine whether
                  or not they have an api. append your findings to census.csv"

MCP servers (searxng-http-mcp, mcp-server-fetch) are launched from mcp.json and
their tools are given to the model alongside one local tool, append_census_row.
Companies are processed concurrently (--concurrency, default 6) — search load is
absorbed by the self-hosted SearXNG instance from docker-compose.yml rather than
a rate-limited third-party endpoint. Resumable: companies already in the CSV are
skipped.

Usage:
  uv run python scripts/census.py --limit 5
  uv run python scripts/census.py --search Adyen
  uv run python scripts/census.py --redo
  uv run python scripts/census.py --concurrency 10
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
# Endpoint of the compose `searxng` service; on the host use http://localhost:8080
os.environ.setdefault("SEARXNG_URL", "http://searxng:8080")

SPONSORS_CSV = ROOT / "netherlands-sponsors.csv"
MCP_JSON = ROOT / "mcp.json"
DEFAULT_CSV = ROOT / "data" / "census.csv"
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_CONCURRENCY = 6
TOOL_TIMEOUT = 90.0  # seconds per MCP tool call (slow job-board pages included)
LLM_RETRIES = 3  # attempts per chat.completions call, with exponential backoff

CSV_COLUMNS = ["company_name", "job_postings_url", "has_api", "notes"]

APPEND_TOOL = {
    "type": "function",
    "function": {
        "name": "append_census_row",
        "description": "Append your final findings for one company to census.csv. Call exactly once per company.",
        "parameters": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string"},
                "job_postings_url": {
                    "type": "string",
                    "description": "Direct URL of the company's job postings page, or empty string if none found.",
                },
                "has_api": {
                    "type": "string",
                    "enum": ["yes", "partial", "no", "unknown", "not_found"],
                    "description": "yes = public JSON API / RSS feed; partial = embedded JSON/JSON-LD; no = plain HTML only; not_found = no jobs page located.",
                },
                "notes": {"type": "string", "description": "ATS/platform used, API endpoint if known, anything notable."},
            },
            "required": ["company_name", "job_postings_url", "has_api", "notes"],
        },
    },
}

SYSTEM_PROMPT = (
    "You are an autonomous web-research agent. You are given a Dutch company name from the "
    "IND recognized-sponsor register. Find where this company posts its job openings, and "
    "determine whether the postings are available through an API, RSS feed, or other easily "
    "machine-parsable format versus plain HTML.\n\n"
    "Method: search the web for the company's careers/jobs page, fetch it to inspect it, and "
    "follow redirects to the actual board (Greenhouse, Ashby, Lever, SmartRecruiters, Workday, "
    "Teamtailor, Workable, Recruitee, etc. all indicate a parsable backend). Prefer the "
    "company's own careers site or ATS board; ignore aggregators like LinkedIn/Indeed.\n\n"
    "When done, call append_census_row exactly once with your findings, then stop. Work "
    "efficiently: typically one search plus one or two fetches is enough. Never invent URLs "
    "you did not see in search results or fetched pages."
)


def load_companies(path: Path) -> list[str]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with path.open(newline="", encoding=enc) as f:
                reader = csv.reader(f)
                next(reader)  # header
                names = []
                for row in reader:
                    if row and row[0].strip():
                        name = row[0].replace('""', '"').strip().strip('"').strip()
                        if name:
                            names.append(name)
                return names
        except UnicodeDecodeError:
            continue
    return []


def load_done(csv_path: Path) -> set[str]:
    if not csv_path.exists():
        return set()
    with csv_path.open(newline="", encoding="utf-8") as f:
        return {row["company_name"] for row in csv.DictReader(f)}


def append_row(csv_path: Path, row: dict) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow({c: row.get(c, "") for c in CSV_COLUMNS})


class CensusAgent:
    def __init__(self, csv_path: Path, max_steps: int) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            print("ERROR: OPENAI_API_KEY not set (copy .env.example to .env).")
            sys.exit(1)
        self.oai = AsyncOpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or None)
        self.csv_path = csv_path
        self.max_steps = max_steps
        self.tools: list[dict] = [APPEND_TOOL]
        self.targets: dict[str, tuple[object, str]] = {}  # prefixed name -> (session, real name)
        self.stack = AsyncExitStack()

    async def start(self) -> None:
        if not MCP_JSON.exists():
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        servers = json.loads(MCP_JSON.read_text()).get("mcpServers", {})

        async def start_server(name: str, cfg: dict) -> int:
            try:
                params = StdioServerParameters(
                    command=cfg["command"],
                    args=cfg.get("args", []),
                    # real environment wins over mcp.json so SEARXNG_URL etc.
                    # from .env / the shell always reach the server process
                    env={**cfg.get("env", {}), **os.environ},
                )
                read, write = await self.stack.enter_async_context(stdio_client(params))
                session = await self.stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                added = 0
                for tool in (await session.list_tools()).tools:
                    full = f"{name}__{tool.name}"
                    self.targets[full] = (session, tool.name)
                    self.tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": full,
                                "description": tool.description or "",
                                "parameters": tool.input_schema or {"type": "object", "properties": {}},
                            },
                        }
                    )
                    added += 1
                return added
            except Exception as e:
                print(f"WARN: MCP server '{name}' failed to start: {e}")
                return 0

        # launch all MCP servers at once instead of one after another
        counts = await asyncio.gather(*(start_server(n, c) for n, c in servers.items()))
        for (name, _), added in zip(servers.items(), counts):
            if added:
                print(f"  mcp[{name}] ready ({added} tools)")

    async def call_tool(self, name: str, arguments: dict) -> str:
        if name == "append_census_row":
            append_row(self.csv_path, arguments)
            url = arguments.get("job_postings_url") or "-"
            return f"OK: appended to census.csv ({arguments.get('company_name')}: {url}, has_api={arguments.get('has_api')})"
        if name not in self.targets:
            return f"Error: unknown tool '{name}'"
        session, real_name = self.targets[name]
        try:
            result = await asyncio.wait_for(session.call_tool(real_name, arguments), timeout=TOOL_TIMEOUT)
        except asyncio.TimeoutError:
            return f"Error: tool '{name}' timed out after {TOOL_TIMEOUT:.0f}s"
        except Exception as e:
            return f"Error calling tool: {e}"
        parts = [getattr(c, "text", "") for c in result.content]
        return "\n".join(p for p in parts if p) or "(no content)"

    async def chat(self, messages: list[dict]):
        """chat.completions.create with retries and exponential backoff."""
        delay = 2.0
        for attempt in range(1, LLM_RETRIES + 1):
            try:
                return await self.oai.chat.completions.create(
                    model=MODEL, messages=messages, tools=self.tools
                )
            except Exception:
                if attempt == LLM_RETRIES:
                    raise
                await asyncio.sleep(delay + random.random())
                delay *= 2

    async def run_company(self, company: str) -> str:
        """One agent conversation for one company. Returns the appended row's has_api/url summary."""
        appended = {}

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Company: {company}\n\n"
                "Find where this company posts their jobs, and determine whether or not they "
                "have an api. Append your findings to census.csv by calling append_census_row."
            )},
        ]

        async def execute(tc) -> str:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if tc.function.name == "append_census_row":
                args.setdefault("company_name", company)
                appended.update(args)
                return await self._append_local(args)
            return await self.call_tool(tc.function.name, args)

        for _ in range(self.max_steps):
            try:
                response = await self.chat(messages)
            except Exception as e:
                return f"llm-error: {e}"
            msg = response.choices[0].message
            if not msg.tool_calls:
                break
            messages.append(msg)
            # run all tool calls of this turn concurrently (searches/fetches don't
            # depend on each other unless the model says so by splitting turns)
            outputs = await asyncio.gather(*(execute(tc) for tc in msg.tool_calls))
            for tc, output in zip(msg.tool_calls, outputs):
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
            if appended:
                break

        if not appended:  # agent never called append — record a miss so we don't loop forever
            append_row(self.csv_path, {
                "company_name": company, "job_postings_url": "",
                "has_api": "unknown", "notes": "agent did not call append_census_row",
            })
            return "no-append (recorded unknown)"

        url = appended.get("job_postings_url") or "-"
        return f"{url}  has_api={appended.get('has_api')}"

    async def _append_local(self, args: dict) -> str:
        append_row(self.csv_path, args)
        return f"OK: appended to census.csv ({args.get('company_name')})"

    async def close(self) -> None:
        await self.stack.aclose()


async def main_async(args: argparse.Namespace) -> None:
    companies = load_companies(SPONSORS_CSV)
    if args.search:
        companies = [c for c in companies if args.search.lower() in c.lower()]
    if args.offset:
        companies = companies[args.offset:]
    if args.limit:
        companies = companies[: args.limit]
    done = set() if args.redo else load_done(Path(args.csv))
    todo = [c for c in companies if c not in done]

    print(
        f"{len(companies)} selected, {len(todo)} to process, model={MODEL}, "
        f"concurrency={args.concurrency}, tools via mcp.json, csv={args.csv}"
    )
    if not todo:
        return

    agent = CensusAgent(Path(args.csv), args.max_steps)
    await agent.start()

    started = time.time()
    progress = 0
    sem = asyncio.Semaphore(args.concurrency)

    async def worker(company: str) -> None:
        nonlocal progress
        async with sem:
            try:
                summary = await agent.run_company(company)
            except Exception as e:  # one bad company must not kill the batch
                summary = f"error: {e}"
        progress += 1
        print(f"[{progress}/{len(todo)}] {company} -> {summary}")

    try:
        await asyncio.gather(*(worker(c) for c in todo))
    except KeyboardInterrupt:
        print("\nInterrupted — progress saved in the CSV; re-run to resume.")
    finally:
        await agent.close()

    mins = (time.time() - started) / 60
    rate = len(todo) / mins if mins > 0 else 0.0
    print(f"Done: {len(todo)} processed in {mins:.1f} min ({rate:.1f} companies/min). Results in {args.csv}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Agent census of sponsor job-posting sites.")
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--search", help="only companies whose name contains this substring")
    ap.add_argument("--max-steps", type=int, default=15, help="max model turns per company")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="companies researched in parallel (default %(default)s)")
    ap.add_argument("--redo", action="store_true", help="reprocess companies already in the CSV")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
