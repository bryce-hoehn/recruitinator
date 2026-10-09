"""Census agent: for each IND sponsor, let an LLM agent with MCP tools (web search,
fetch, ...) find where the company posts its jobs and whether there's an API, then
have it append its findings to census.csv.

    for company in companies:
        prompt = "find where this company posts their jobs, and determine whether
                  or not they have an api. append your findings to census.csv"

MCP servers (e.g. ddg-search, fetch) are launched from mcp.json and their tools are
given to the model alongside one local tool, append_census_row. Resumable: companies
already present in the CSV are skipped.

Usage:
  uv run python scripts/census.py --limit 5
  uv run python scripts/census.py --search Adyen
  uv run python scripts/census.py --redo
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

SPONSORS_CSV = ROOT / "netherlands-sponsors.csv"
MCP_JSON = ROOT / "mcp.json"
DEFAULT_CSV = ROOT / "data" / "census.csv"
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
COMPANY_DELAY = 2.0

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
        for name, cfg in servers.items():
            try:
                params = StdioServerParameters(
                    command=cfg["command"],
                    args=cfg.get("args", []),
                    env={**os.environ, **cfg.get("env", {})},
                )
                read, write = await self.stack.enter_async_context(stdio_client(params))
                session = await self.stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                for tool in (await session.list_tools()).tools:
                    full = f"{name}__{tool.name}"
                    self.targets[full] = (session, tool.name)
                    self.tools.append({
                        "type": "function",
                        "function": {
                            "name": full,
                            "description": tool.description or "",
                            "parameters": tool.input_schema or {"type": "object", "properties": {}},
                        },
                    })
                print(f"  mcp[{name}] ready ({len(self.targets)} tools total)")
            except Exception as e:
                print(f"WARN: MCP server '{name}' failed to start: {e}")

    async def call_tool(self, name: str, arguments: dict) -> str:
        if name == "append_census_row":
            append_row(self.csv_path, arguments)
            url = arguments.get("job_postings_url") or "-"
            return f"OK: appended to census.csv ({arguments.get('company_name')}: {url}, has_api={arguments.get('has_api')})"
        if name not in self.targets:
            return f"Error: unknown tool '{name}'"
        session, real_name = self.targets[name]
        try:
            result = await session.call_tool(real_name, arguments)
        except Exception as e:
            return f"Error calling tool: {e}"
        parts = [getattr(c, "text", "") for c in result.content]
        return "\n".join(p for p in parts if p) or "(no content)"

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
        for _ in range(self.max_steps):
            try:
                response = await self.oai.chat.completions.create(
                    model=MODEL, messages=messages, tools=self.tools
                )
            except Exception as e:
                return f"llm-error: {e}"
            msg = response.choices[0].message
            if not msg.tool_calls:
                break
            messages.append(msg)
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if tc.function.name == "append_census_row":
                    args.setdefault("company_name", company)
                    appended.update(args)
                    output = await self._append_local(args)
                else:
                    output = await self.call_tool(tc.function.name, args)
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

    print(f"{len(companies)} selected, {len(todo)} to process, model={MODEL}, tools via mcp.json, csv={args.csv}")
    if not todo:
        return

    agent = CensusAgent(Path(args.csv), args.max_steps)
    await agent.start()

    started = time.time()
    try:
        for i, company in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {company}")
            try:
                summary = await agent.run_company(company)
            except Exception as e:
                summary = f"error: {e}"
            print(f"    -> {summary}")
            time.sleep(COMPANY_DELAY + (i % 3) * 0.5)
    except KeyboardInterrupt:
        print("\nInterrupted — progress saved in the CSV; re-run to resume.")
    finally:
        await agent.close()

    mins = (time.time() - started) / 60
    print(f"Done: {len(todo)} processed in {mins:.1f} min. Results in {args.csv}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Agent census of sponsor job-posting sites.")
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--search", help="only companies whose name contains this substring")
    ap.add_argument("--max-steps", type=int, default=15, help="max model turns per company")
    ap.add_argument("--redo", action="store_true", help="reprocess companies already in the CSV")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
