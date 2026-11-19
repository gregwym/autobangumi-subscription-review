"""Business-operation logs; never include request bodies or signed URLs."""

import contextvars
import functools
import json
import logging
import re
import secrets
import time

from fastapi.routing import APIRoute

_run = contextvars.ContextVar("plugin_audit_run", default=None)


def safe(value):
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    if isinstance(value, str):
        # Paths are useful; feed tokens, URL passwords and query strings are not.
        return re.sub(r"(?:https?|magnet)://?[^\s\"'<>]+|magnet:\?[^\s\"'<>]+", "[URL]", value)
    return value


class Audit:
    def __init__(self, log=None):
        self.log = log or logging.getLogger("plugin.subscription-review")
        self.heartbeats = {}

    def emit(self, event, *, level=logging.INFO, **fields):
        self.log.log(
            level,
            "audit %s",
            json.dumps(
                safe({"event": event, "run": _run.get() or secrets.token_hex(6), **fields}),
                ensure_ascii=False,
            ),
        )

    def periodic(self, event, **fields):
        now = time.monotonic()
        last, previous = self.heartbeats.get(event, (-3600, None))
        # Every tick is available at DEBUG. INFO records changes and an hourly heartbeat.
        state = json.dumps(safe(fields), sort_keys=True, default=str)
        loud = now - last >= 3600 or state != previous
        self.heartbeats[event] = (now if loud else last, state)
        self.emit(event, level=logging.INFO if loud else logging.DEBUG, **fields)

    def route(self):
        audit = self

        class LoggedRoute(APIRoute):
            def get_route_handler(self):
                handler = super().get_route_handler()

                async def handle(request):
                    if request.method == "GET":
                        return await handler(request)
                    token = _run.set(secrets.token_hex(6))
                    started = time.monotonic()
                    fields = {
                        "method": request.method,
                        "route": self.path,
                        "params": request.path_params,
                    }
                    audit.emit("api.start", **fields)
                    try:
                        response = await handler(request)
                        audit.emit(
                            "api.finish",
                            **fields,
                            status=response.status_code,
                            seconds=round(time.monotonic() - started, 3),
                        )
                        return response
                    except BaseException as error:
                        audit.emit(
                            "api.failed",
                            level=logging.WARNING,
                            **fields,
                            error=type(error).__name__,
                            status=getattr(error, "status_code", None),
                        )
                        raise
                    finally:
                        _run.reset(token)

                return handle

        return LoggedRoute


def traced(event, *, quiet=False, audit=None):
    def decorate(fn):
        @functools.wraps(fn)
        async def run(self, *args, **kwargs):
            token = _run.set(_run.get() or secrets.token_hex(6))
            started = time.monotonic()
            audit_log = audit or self.audit
            audit_log.emit(event + ".start", level=logging.DEBUG if quiet else logging.INFO)
            try:
                result = await fn(self, *args, **kwargs)
                fields = {
                    k: result[k]
                    for k in (
                        "backup",
                        "installed",
                        "previous_code",
                        "automatic",
                        "error",
                        "current",
                        "available",
                        "version",
                        "sent",
                        "held",
                    )
                    if isinstance(result, dict) and k in result
                }
                audit_log.emit(
                    event + ".finish",
                    level=logging.WARNING
                    if fields.get("error")
                    else logging.DEBUG
                    if quiet
                    else logging.INFO,
                    **fields,
                    seconds=round(time.monotonic() - started, 3),
                )
                return result
            except BaseException as error:
                audit_log.emit(
                    event + ".failed",
                    level=logging.WARNING,
                    error=type(error).__name__,
                    detail=str(error),
                    seconds=round(time.monotonic() - started, 3),
                )
                raise
            finally:
                _run.reset(token)

        return run

    return decorate
