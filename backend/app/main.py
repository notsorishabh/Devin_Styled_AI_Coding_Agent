import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.api import auth, health, repos, sandboxes, tasks
from app.config import settings
from app.db import engine
from app.models import Sandbox, Task, TaskEvent, User
from app.sandbox import get_sandbox_provider
from app.sandbox.reaper import timeout_watchdog


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create, checkfirst=True)
        await conn.run_sync(Sandbox.__table__.create, checkfirst=True)
        await conn.run_sync(Task.__table__.create, checkfirst=True)
        await conn.run_sync(TaskEvent.__table__.create, checkfirst=True)

    app.state.sandbox_provider = get_sandbox_provider()
    stop = asyncio.Event()
    watchdog = asyncio.create_task(timeout_watchdog(app.state.sandbox_provider, stop))
    yield
    stop.set()
    await watchdog


app = FastAPI(
    title="Devin",
    version="0.1.0",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True, "withCredentials": True},
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.cookie_secure,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(repos.router)
app.include_router(sandboxes.router)
app.include_router(tasks.router)
