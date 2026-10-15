"""HTTP API.

``POST /v1/transactions`` answers ``201`` with a new decision, or ``200`` plus
``Idempotent-Replayed: true`` when the ``event_id`` was already decided with the same
payload. Every error is ``{"error": {"code", "message", "details"}}`` with a stable code.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from sentinel.config import Settings
from sentinel.domain import parse_json
from sentinel.errors import ErrorCode, SentinelError, ValidationError
from sentinel.logs import configure_logging
from sentinel.rules import load_ruleset_file
from sentinel.service import DecisionService
from sentinel.store.db import create_pool

MAX_BODY_BYTES: Final = 16 * 1024

log = logging.getLogger(__name__)


def _error(code: ErrorCode, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code.value, "message": message, "details": {}}}, status_code=status
    )


async def _read_body(request: Request) -> bytes:
    too_large = ValidationError(
        ErrorCode.REQUEST_TOO_LARGE,
        f"request body exceeds {MAX_BODY_BYTES} bytes",
        details={"max_bytes": MAX_BODY_BYTES},
    )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise too_large
    return bytes(body)


def create_app(
    service: DecisionService,
    *,
    lifespan: Callable[[Starlette], AbstractAsyncContextManager[None]] | None = None,
) -> Starlette:
    async def submit(request: Request) -> Response:
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type != "application/json":
            raise ValidationError(
                ErrorCode.REQUEST_UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json"
            )
        payload = parse_json(await _read_body(request))
        submission = await run_in_threadpool(service.submit, payload)
        body = {**submission.decision.to_json(), "replayed": submission.replayed}
        headers = {"Location": f"/v1/decisions/{submission.decision.decision_id}"}
        if submission.replayed:
            headers["Idempotent-Replayed"] = "true"
        return JSONResponse(body, status_code=200 if submission.replayed else 201, headers=headers)

    async def get_decision(request: Request) -> Response:
        decision = await run_in_threadpool(service.decision, request.path_params["decision_id"])
        return JSONResponse(decision.to_json())

    async def get_audit(request: Request) -> Response:
        report = await run_in_threadpool(service.audit_report, request.path_params["decision_id"])
        return JSONResponse(
            {
                "decision": report.decision.to_json(),
                "verification": report.verification.to_json(),
                "record": report.record,
            }
        )

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def readyz(request: Request) -> Response:
        await run_in_threadpool(service.ping)
        return JSONResponse({"status": "ready", "ruleset_version": service.ruleset.version})

    async def metrics(request: Request) -> Response:
        return PlainTextResponse(service.metrics.render(), media_type="text/plain; version=0.0.4")

    async def sentinel_error(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, SentinelError):  # pragma: no cover - registered for SentinelError
            raise exc
        return JSONResponse({"error": exc.to_dict()}, status_code=exc.http_status)

    async def http_error(request: Request, exc: Exception) -> Response:
        status = exc.status_code if isinstance(exc, HTTPException) else 500
        if status == 405:
            return _error(ErrorCode.METHOD_NOT_ALLOWED, "method not allowed", 405)
        return _error(ErrorCode.ROUTE_NOT_FOUND, "no such route", 404)

    async def unexpected_error(request: Request, exc: Exception) -> Response:
        log.error("unhandled error", exc_info=exc)
        return _error(ErrorCode.INTERNAL_ERROR, "internal error", 500)

    return Starlette(
        routes=[
            Route("/v1/transactions", submit, methods=["POST"]),
            Route("/v1/decisions/{decision_id:uuid}", get_decision, methods=["GET"]),
            Route("/v1/decisions/{decision_id:uuid}/audit", get_audit, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
            Route("/readyz", readyz, methods=["GET"]),
            Route("/metrics", metrics, methods=["GET"]),
        ],
        exception_handlers={
            SentinelError: sentinel_error,
            HTTPException: http_error,
            Exception: unexpected_error,
        },
        lifespan=lifespan,
    )


def pool_lifespan(
    close: Callable[[], None],
) -> Callable[[Starlette], AbstractAsyncContextManager[None]]:
    """A lifespan that runs ``close`` (e.g. ``pool.close``) when the server shuts down."""

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            close()

    return lifespan


def app_from_env() -> Starlette:
    """ASGI factory (``uvicorn --factory sentinel.api:app_from_env``): one pool per worker."""
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    pool = create_pool(
        settings.require_database_url(),
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
    )
    service = DecisionService(
        pool, load_ruleset_file(settings.ruleset_path), budget_ms=settings.budget_ms
    )
    service.register_ruleset()
    return create_app(service, lifespan=pool_lifespan(pool.close))
