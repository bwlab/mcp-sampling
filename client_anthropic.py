# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp[cli]", "anthropic>=0.40.0", "python-dotenv"]
# ///

import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from anthropic import AsyncAnthropic, APIStatusError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.context import RequestContext
from mcp.types import (
    CreateMessageRequestParams,
    CreateMessageResult,
    TextContent,
)

LOG_DELAY = 3.0
MODEL = "claude-opus-4-7"

anthropic = AsyncAnthropic()  # legge ANTHROPIC_API_KEY da env


def log(label: str, msg: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {label} {msg}", file=sys.stderr, flush=True)


async def alog(label: str, msg: str = "") -> None:
    log(label, msg)
    await asyncio.sleep(LOG_DELAY)


BASE_DIR = Path(__file__).parent

CLIENT_TOOLS = [
    {
        "name": "list_files",
        "description": "Elenca i file in una directory relativa alla base del progetto.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory relativa (es. 'logs')"}},
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Legge il contenuto di un file di testo.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relativo (es. 'logs/auth.log')"}},
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": "Cerca un pattern regex in un file e restituisce le righe matching.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern", "path"],
        },
    },
]


def safe_path(rel: str) -> Path:
    p = (BASE_DIR / rel).resolve()
    if not str(p).startswith(str(BASE_DIR.resolve())):
        raise ValueError(f"path fuori dalla base: {rel}")
    return p


def tool_list_files(path: str) -> str:
    p = safe_path(path)
    if not p.is_dir():
        return f"errore: {path} non e' una directory"
    return "\n".join(sorted(f.name for f in p.iterdir()))


def tool_read_file(path: str) -> str:
    p = safe_path(path)
    if not p.is_file():
        return f"errore: {path} non e' un file"
    return p.read_text()


def tool_grep(pattern: str, path: str) -> str:
    p = safe_path(path)
    if not p.is_file():
        return f"errore: {path} non e' un file"
    rx = re.compile(pattern)
    return "\n".join(line.rstrip() for line in p.read_text().splitlines() if rx.search(line))


def execute_tool(name: str, args: dict) -> str:
    if name == "list_files":
        return tool_list_files(args["path"])
    if name == "read_file":
        return tool_read_file(args["path"])
    if name == "grep":
        return tool_grep(args["pattern"], args["path"])
    return f"tool sconosciuto: {name}"


def to_anthropic_messages(mcp_messages):
    out = []
    for m in mcp_messages:
        text = m.content.text if hasattr(m.content, "text") else str(m.content)
        out.append({"role": m.role, "content": text})
    return out


sampling_done = asyncio.Event()


async def sampling_callback(
    context: RequestContext[ClientSession, None],
    params: CreateMessageRequestParams,
) -> CreateMessageResult:
    """Loop agentico: Claude usa i tool del client (list_files, read_file, grep)."""
    log("◀── SAMPLING REQUEST dal server")
    for i, m in enumerate(params.messages):
        text = m.content.text if hasattr(m.content, "text") else str(m.content)
        preview = text if len(text) < 200 else text[:200] + "..."
        log(f"    msg[{i}] role={m.role}", preview)
    log(f"    maxTokens={params.maxTokens}, modello={MODEL}")

    messages = to_anthropic_messages(params.messages)
    system_prompt = params.systemPrompt or None

    max_iters = 10
    for it in range(max_iters):
        log(f"──▶ Anthropic API call #{it + 1}")
        kwargs = dict(
            model=MODEL,
            max_tokens=params.maxTokens or 1500,
            tools=CLIENT_TOOLS,
            messages=messages,
        )
        if system_prompt:
            kwargs["system"] = system_prompt

        response = None
        for attempt in range(5):
            try:
                response = await anthropic.messages.create(**kwargs)
                break
            except APIStatusError as e:
                if e.status_code in (429, 529, 503):
                    delay = 2 ** attempt
                    log(f"    ⚠️  {e.status_code} retry #{attempt + 1} dopo {delay}s")
                    await asyncio.sleep(delay)
                    continue
                raise
        if response is None:
            log("──▶ API non disponibile dopo retry, abort")
            sampling_done.set()
            return CreateMessageResult(
                role="assistant",
                content=TextContent(type="text", text="(API overloaded)"),
                model=MODEL,
                stopReason="endTurn",
            )
        log(f"    stop_reason={response.stop_reason}")

        if response.stop_reason == "end_turn":
            final = "".join(b.text for b in response.content if b.type == "text")
            log("──▶ Claude FINAL response", final)
            sampling_done.set()
            return CreateMessageResult(
                role="assistant",
                content=TextContent(type="text", text=final),
                model=response.model,
                stopReason="endTurn",
            )

        if response.stop_reason != "tool_use":
            log(f"    stop_reason inatteso, abort")
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                log(f"    🔧 Claude chiama tool: {block.name}({block.input})")
                try:
                    result = execute_tool(block.name, block.input)
                except Exception as e:
                    result = f"errore: {e}"
                preview = result if len(result) < 300 else result[:300] + "..."
                log(f"    🔧 risultato", preview)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result}
                )

        messages.append({"role": "user", "content": tool_results})

    log("──▶ max iterazioni raggiunto")
    sampling_done.set()
    return CreateMessageResult(
        role="assistant",
        content=TextContent(type="text", text="(max iterazioni)"),
        model=MODEL,
        stopReason="endTurn",
    )


async def amain():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERRORE: variabile ANTHROPIC_API_KEY non impostata", file=sys.stderr)
        sys.exit(1)

    await alog("STEP 1", "spawn server via stdio")
    server_params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", "/media/extra/Progetti/mcp/mcp-sampling", "server.py"],
    )

    async with stdio_client(server_params) as (read, write):
        await alog("STEP 2", "stdio connesso, creo ClientSession con sampling Anthropic")
        async with ClientSession(
            read, write, sampling_callback=sampling_callback
        ) as session:
            await alog("STEP 3", "invio initialize")
            init = await session.initialize()
            await alog(
                "STEP 4",
                f"initialize OK — server: {init.serverInfo.name} v{init.serverInfo.version}",
            )

            await alog("STEP 5", "aspetto sampling dal server (max 60s)...")
            try:
                await asyncio.wait_for(sampling_done.wait(), timeout=60)
                await alog("STEP 6", "sampling completato, attendo invio finale al server")
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                await alog("STEP 6", "timeout: nessun sampling ricevuto")
            await alog("STEP 7", "chiudo session")


if __name__ == "__main__":
    asyncio.run(amain())
