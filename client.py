# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp[cli]"]
# ///

import asyncio
import sys
from datetime import datetime

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.context import RequestContext
from mcp.types import (
    CreateMessageRequestParams,
    CreateMessageResult,
    TextContent,
)


LOG_DELAY = 3.0


def log(label: str, msg: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {label} {msg}", file=sys.stderr, flush=True)


async def alog(label: str, msg: str = "") -> None:
    log(label, msg)
    await asyncio.sleep(LOG_DELAY)


async def sampling_callback(
    context: RequestContext[ClientSession, None],
    params: CreateMessageRequestParams,
) -> CreateMessageResult:
    """Auto-approve sampling. Logga richiesta e risposta finta."""
    await alog("◀── SAMPLING REQUEST dal server")
    for i, m in enumerate(params.messages):
        text = m.content.text if hasattr(m.content, "text") else str(m.content)
        await alog(f"    msg[{i}] role={m.role}", text)
    await alog(f"    maxTokens={params.maxTokens}")

    fake_response = "Hello World! (risposta auto-approvata dal client)"
    await alog("──▶ AUTO-APPROVE, invio risposta finta", fake_response)

    return CreateMessageResult(
        role="assistant",
        content=TextContent(type="text", text=fake_response),
        model="fake-model-1.0",
        stopReason="endTurn",
    )


async def amain():
    await alog("STEP 1", "spawn server via stdio")
    server_params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", "/media/extra/Progetti/mcp/mcp-sampling", "server.py"],
    )

    async with stdio_client(server_params) as (read, write):
        await alog("STEP 2", "stdio connesso, creo ClientSession con sampling_callback")
        async with ClientSession(
            read, write, sampling_callback=sampling_callback
        ) as session:
            await alog("STEP 3", "invio initialize")
            init = await session.initialize()
            await alog("STEP 4", f"initialize OK — server: {init.serverInfo.name} v{init.serverInfo.version}")
            await alog("    capabilities", str(init.capabilities))

            await alog("STEP 5", "client pronto. Aspetto sampling dal server...")
            await asyncio.sleep(5)
            await alog("STEP 6", "chiudo session")


if __name__ == "__main__":
    asyncio.run(amain())
