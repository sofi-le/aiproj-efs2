# Google Calendar through the @cocal/google-calendar-mcp MCP server (needs gcp-oauth.keys.json).
import asyncio, os
from datetime import datetime, timedelta
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False

SERVER_CMD  = ["npx", "-y", "@cocal/google-calendar-mcp@2.8.1"]
CALENDAR_ID = os.environ.get("GCAL_CALENDAR_ID", "primary")
CREDENTIALS = os.environ.get("GOOGLE_OAUTH_CREDENTIALS") or str(Path(__file__).parent / "gcp-oauth.keys.json")


def enabled() -> bool:
    return HAVE_MCP and os.path.exists(CREDENTIALS)


def local_timezone() -> str:
    try:
        return os.path.realpath("/etc/localtime").split("zoneinfo/")[1]
    except IndexError:
        return os.environ.get("TZ", "America/New_York")


async def _call(tool: str, args: dict) -> tuple[bool, str]:
    params = StdioServerParameters(command=SERVER_CMD[0], args=SERVER_CMD[1:], env={**os.environ, "GOOGLE_OAUTH_CREDENTIALS": CREDENTIALS})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            text = " ".join(getattr(c, "text", "") for c in result.content)
            return (not result.isError), text


def add_event(title: str, start: str | None = None, minutes: int = 30, description: str = "") -> bool:
    if not enabled():
        return False
    try:
        begin = datetime.fromisoformat(start) if start else datetime.now()
    except ValueError:
        begin = datetime.now()
    begin = begin.replace(tzinfo=None, microsecond=0)
    end = begin + timedelta(minutes=max(5, min(int(minutes or 30), 24 * 60)))
    args = {
        "calendarId": CALENDAR_ID,
        "summary": title[:200],
        "description": description,
        "start": begin.strftime("%Y-%m-%dT%H:%M:%S"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "timeZone": local_timezone(),
        "allowDuplicates": True,
    }
    try:
        ok, text = asyncio.run(asyncio.wait_for(_call("create-event", args), 40))
    except Exception as e:
        ok, text = False, f"{type(e).__name__}: {e}"
    print("Calendar:", args["summary"], "@", args["start"], "->", "ok" if ok else text[:300])
    return ok
