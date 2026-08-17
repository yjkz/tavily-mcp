"""Application entry point.

One process, one port:
    /mcp   FastMCP streamable-HTTP endpoint (bearer-token auth)
    /api   Dashboard admin API (session cookie)
    /      Dashboard SPA (built frontend, served as static files)
    /health  Liveness probe

The top-level ASGI app is FastMCP's own http_app() with our routes appended,
so the MCP session manager's lifespan runs in the same lifespan as everything
else (a mounted sub-app's lifespan would never run).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .admin_api import build_admin_routes
from .config import load_config
from .db import Database
from .mcp_server import QueryTokenAuthMiddleware, build_mcp
from .pool import KeyPool
from .state import AppState, set_state
from .tavily import TavilyClient
from .tasks import start_background_tasks

logger = logging.getLogger("tavily_pool")

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "dist"


async def health(request):
    return JSONResponse({"status": "ok"})


def create_app():
    config = load_config()
    logging.basicConfig(
        level=logging.DEBUG if config.dev_mode else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = Database(config.data_dir / "tavily_pool.db")
    pool = KeyPool(db, cooldown_seconds=config.cooldown_seconds)
    tavily = TavilyClient()
    state = AppState(config=config, db=db, pool=pool, tavily=tavily)
    set_state(state)

    @asynccontextmanager
    async def app_lifespan(server):
        await db.connect()
        await pool.load()
        tasks = start_background_tasks(state)
        logger.info(
            "tavily-pool-mcp started: %d key(s) in pool, MCP at /mcp, dashboard at /",
            len(pool),
        )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await tavily.aclose()
            await db.close()

    mcp = build_mcp(state, lifespan=app_lifespan)
    app = mcp.http_app(path="/mcp")
    app.routes.append(Route("/health", health, methods=["GET"]))
    app.routes.extend(build_admin_routes(config))
    if STATIC_DIR.exists():
        app.routes.append(
            Mount("/", app=StaticFiles(directory=STATIC_DIR, html=True), name="dashboard")
        )
    else:
        async def no_frontend(request):
            return JSONResponse(
                {
                    "status": "ok",
                    "hint": "dashboard frontend not built; run npm run build in dashboard/",
                }
            )

        app.routes.append(Route("/", no_frontend, methods=["GET"]))
    # Wrap the whole stack: fastmcp's AuthenticationMiddleware reads the
    # Authorization header from an outer layer, so the ?token= promotion must
    # happen before it (a middleware appended inside comes too late).
    return QueryTokenAuthMiddleware(app)


app = create_app()


if __name__ == "__main__":
    config = load_config()
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
