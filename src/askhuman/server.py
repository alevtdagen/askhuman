"""REST API, human inbox, and delivery worker. Start with `askhuman serve`."""

import asyncio
import contextlib
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi import Request as HTTPRequest
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .channels import reply_token, worker
from .config import EMBEDDED_TYPES, FIELDS, RoutingConfig
from .models import AnswerInput, Question, Request, Status
from .settings import Settings
from .store import Conflict, Store


def create_app(settings: Settings | None = None, *, run_worker: bool = True) -> FastAPI:
    settings = settings or Settings.load()
    store = Store(settings.data_dir / "askhuman.sqlite3")

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker(store, settings)) if run_worker else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="AskHuman",
        version="0.2.0",
        lifespan=lifespan,
        description="A universal human-input API for agents.",
    )
    app.state.store = store
    app.state.settings = settings
    web = Path(__file__).parent / "web"
    app.mount("/assets", StaticFiles(directory=web), name="assets")

    def check_token(authorization: str | None, expected: str):
        if not authorization or not secrets.compare_digest(
            authorization.encode(), f"Bearer {expected}".encode()
        ):
            raise HTTPException(401, "Invalid or missing credential")

    def agent(authorization: Annotated[str | None, Header()] = None):
        check_token(authorization, settings.api_key)

    def admin(authorization: Annotated[str | None, Header()] = None):
        check_token(authorization, settings.admin_key)

    def human(request_id: str, authorization: Annotated[str | None, Header()] = None):
        check_token(authorization, reply_token(settings, request_id))

    @app.middleware("http")
    async def security_headers(request: HTTPRequest, call_next):
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def missing_handler(request, exc):
        return JSONResponse(status_code=404, content={"detail": "Request not found"})

    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.2.0"}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(web / "index.html")

    @app.get("/reply/{request_id}", include_in_schema=False)
    def reply_page(request_id: str):
        return FileResponse(web / "reply.html")

    @app.post("/v1/requests", response_model=Request, dependencies=[Depends(agent)])
    def create(
        question: Question,
        idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None,
    ):
        return store.create(question, idempotency_key)

    @app.get("/v1/requests/{request_id}", response_model=Request, dependencies=[Depends(agent)])
    def get(request_id: str):
        return store.get(request_id)

    @app.get(
        "/v1/requests/{request_id}/wait", response_model=Request, dependencies=[Depends(agent)]
    )
    async def wait(request_id: str, seconds: int = Query(default=25, ge=0, le=25)):
        end = asyncio.get_running_loop().time() + seconds
        while True:
            result = store.get(request_id)
            if result.status != "pending" or asyncio.get_running_loop().time() >= end:
                return result
            await asyncio.sleep(0.25)

    @app.post(
        "/v1/requests/{request_id}/cancel", response_model=Request, dependencies=[Depends(agent)]
    )
    def cancel(request_id: str):
        return store.cancel(request_id)

    @app.get("/api/admin/requests", dependencies=[Depends(admin)])
    def inbox(
        status: Status | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return store.list(status, limit, offset)

    @app.post(
        "/api/admin/requests/{request_id}/answer",
        response_model=Request,
        dependencies=[Depends(admin)],
    )
    def answer_admin(request_id: str, answer: AnswerInput):
        return store.answer(request_id, answer, "admin")

    @app.get("/api/admin/requests/{request_id}/events", dependencies=[Depends(admin)])
    def events(request_id: str):
        return store.events(request_id)

    @app.get("/api/admin/config", dependencies=[Depends(admin)])
    def get_config():
        return store.config()

    @app.put("/api/admin/config", dependencies=[Depends(admin)])
    def put_config(config: RoutingConfig):
        config.require_server()
        store.configure(config)
        return {"saved": True}

    @app.get("/api/admin/channel-types", dependencies=[Depends(admin)])
    def channel_types():
        return {name: fields for name, fields in FIELDS.items() if name not in EMBEDDED_TYPES}

    @app.get("/api/replies/{request_id}", response_model=Request, dependencies=[Depends(human)])
    def get_reply(request_id: str):
        request = store.get(request_id)
        # Channel identifiers and delivery failures are operational details for the operator.
        request.deliveries = []
        return request

    @app.post("/api/replies/{request_id}", response_model=Request, dependencies=[Depends(human)])
    def answer_reply(request_id: str, answer: AnswerInput):
        request = store.answer(request_id, answer, "reply_link")
        request.deliveries = []
        return request

    return app
