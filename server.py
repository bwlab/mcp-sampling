# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp[cli]"]
# ///

import asyncio
import sys

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from mcp.types import SamplingMessage, TextContent

mcp = FastMCP("mcp-sampling", log_level="ERROR")


@mcp.tool()
async def echo(message: str) -> str:
    """Restituisce il messaggio ricevuto."""
    return f"echo: {message}"


@mcp.tool()
async def ask_llm(prompt: str, ctx: Context) -> str:
    """Chiede al client (LLM) tramite MCP sampling e restituisce la risposta."""
    result = await ctx.session.create_message(
        messages=[
            SamplingMessage(
                role="user",
                content=TextContent(type="text", text=prompt),
            )
        ],
        max_tokens=200,
    )
    content = result.content
    return content.text if hasattr(content, "text") else str(content)


async def watch_and_sample(session: ServerSession):
    """Aspetta init e chiede al client di analizzare i log per problemi di sicurezza."""
    while session.client_params is None:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)

    prompt = (
        "Analizza i file di log presenti nella directory ./logs e identifica eventuali "
        "problemi di sicurezza o anomalie. Usa i tool a tua disposizione per elencare i file, "
        "leggerne il contenuto e cercare pattern sospetti. Concludi con un report breve "
        "(3-5 frasi) sui problemi rilevati."
    )
    print(">>> Server: invio sampling/createMessage al client", file=sys.stderr)
    try:
        result = await session.create_message(
            messages=[SamplingMessage(role="user", content=TextContent(type="text", text=prompt))],
            max_tokens=1500,
        )
        content = result.content
        text = content.text if hasattr(content, "text") else str(content)
        print(f">>> Server: sampling response: {text}", file=sys.stderr)
    except Exception as e:
        print(f">>> Server: sampling error: {e}", file=sys.stderr)


async def amain():
    async with stdio_server() as (read, write):
        init_options = mcp._mcp_server.create_initialization_options()
        async with ServerSession(read, write, init_options) as session:
            watcher = asyncio.create_task(watch_and_sample(session))
            try:
                async for message in session.incoming_messages:
                    await mcp._mcp_server._handle_message(message, session, {}, False)
            finally:
                watcher.cancel()


if __name__ == "__main__":
    asyncio.run(amain())
