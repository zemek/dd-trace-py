import inspect
import os
from typing import Any  # noqa:F401
from typing import Dict  # noqa:F401
from typing import List  # noqa:F401
from typing import Optional  # noqa:F401

import starlette
from starlette import requests as starlette_requests
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from wrapt import ObjectProxy
from wrapt import wrap_function_wrapper as _w

from ddtrace import config
from ddtrace.contrib import trace_utils
from ddtrace.contrib.asgi import TraceMiddleware
from ddtrace.contrib.internal.trace_utils import with_traced_module
from ddtrace.ext import http
from ddtrace.internal import core
from ddtrace.internal._exceptions import BlockingException
from ddtrace.internal.logger import get_logger
from ddtrace.internal.schema import schematize_service_name
from ddtrace.internal.utils import get_argument_value
from ddtrace.internal.utils import get_blocked
from ddtrace.internal.utils import set_argument_value
from ddtrace.internal.utils.wrappers import unwrap as _u
from ddtrace.settings.asm import config as asm_config
from ddtrace.trace import Pin
from ddtrace.trace import Span  # noqa:F401
from ddtrace.vendor.packaging.version import parse as parse_version


log = get_logger(__name__)

config._add(
    "starlette",
    dict(
        _default_service=schematize_service_name("starlette"),
        request_span_name="starlette.request",
        distributed_tracing=True,
        obfuscate_404_resource=os.getenv("DD_ASGI_OBFUSCATE_404_RESOURCE", default=False),
        _trace_asgi_websocket=os.getenv("DD_ASGI_TRACE_WEBSOCKET", default=False),
    ),
)


def get_version():
    # type: () -> str
    return getattr(starlette, "__version__", "")


_STARLETTE_VERSION = parse_version(get_version())


def _supported_versions() -> Dict[str, str]:
    return {"starlette": ">=0.14.0"}


def traced_init(wrapped, instance, args, kwargs):
    mw = kwargs.pop("middleware", [])
    mw.insert(0, Middleware(TraceMiddleware, integration_config=config.starlette))
    kwargs.update({"middleware": mw})

    wrapped(*args, **kwargs)


def traced_route_init(wrapped, _instance, args, kwargs):
    handler = get_argument_value(args, kwargs, 1, "endpoint")

    core.dispatch("service_entrypoint.patch", (inspect.unwrap(handler),))

    return wrapped(*args, **kwargs)


def patch():
    if getattr(starlette, "_datadog_patch", False):
        return

    starlette._datadog_patch = True

    _w("starlette.applications", "Starlette.__init__", traced_init)
    Pin().onto(starlette)

    # We need to check that Fastapi instrumentation hasn't already patched these
    if not isinstance(starlette.routing.Route.__init__, ObjectProxy):
        _w("starlette.routing", "Route.__init__", traced_route_init)
    if not isinstance(starlette.routing.Route.handle, ObjectProxy):
        _w("starlette.routing", "Route.handle", traced_handler)
    if not isinstance(starlette.routing.Mount.handle, ObjectProxy):
        _w("starlette.routing", "Mount.handle", traced_handler)

    if not isinstance(starlette.background.BackgroundTasks.add_task, ObjectProxy):
        _w("starlette.background", "BackgroundTasks.add_task", _trace_background_tasks(starlette))


def unpatch():
    if not getattr(starlette, "_datadog_patch", False):
        return

    starlette._datadog_patch = False

    _u(starlette.applications.Starlette, "__init__")

    # We need to check that Fastapi instrumentation hasn't already unpatched these
    if isinstance(starlette.routing.Route.handle, ObjectProxy):
        _u(starlette.routing.Route, "handle")

    if isinstance(starlette.routing.Mount.handle, ObjectProxy):
        _u(starlette.routing.Mount, "handle")

    if isinstance(starlette.background.BackgroundTasks.add_task, ObjectProxy):
        _u(starlette.background.BackgroundTasks, "add_task")


def traced_handler(wrapped, instance, args, kwargs):
    # Since handle can be called multiple times for one request, we take the path of each instance
    # Then combine them at the end to get the correct resource names
    scope = get_argument_value(args, kwargs, 0, "scope")  # type: Optional[Dict[str, Any]]
    if not scope:
        return wrapped(*args, **kwargs)

    # Our ASGI TraceMiddleware has not been called, skip since
    # we won't have a request span to attach this information onto
    # DEV: This can happen if patching happens after the app has been created
    if "datadog" not in scope:
        log.warning("datadog context not present in ASGI request scope, trace middleware may be missing")
        return wrapped(*args, **kwargs)

    # Add the path to the resource_paths list
    if "resource_paths" not in scope["datadog"]:
        scope["datadog"]["resource_paths"] = [instance.path]
    else:
        scope["datadog"]["resource_paths"].append(instance.path)

    request_spans = scope["datadog"].get("request_spans", [])  # type: List[Span]
    resource_paths = scope["datadog"].get("resource_paths", [])  # type: List[str]

    if len(request_spans) == len(resource_paths):
        # Iterate through the request_spans and assign the correct resource name to each
        for index, span in enumerate(request_spans):
            # We want to set the full resource name on the first request span
            # And one part less of the full resource name for each proceeding request span
            # e.g. full path is /subapp/hello/{name}, first request span gets that as resource name
            # Second request span gets /hello/{name}
            path = "".join(resource_paths[index:])

            if scope.get("method"):
                span.resource = "{} {}".format(scope["method"], path)
            else:
                span.resource = path
            # route should only be in the root span
            if index == 0:
                span.set_tag_str(http.ROUTE, path)
    # at least always update the root asgi span resource name request_spans[0].resource = "".join(resource_paths)
    elif request_spans and resource_paths:
        route = "".join(resource_paths)
        if scope.get("method"):
            request_spans[0].resource = "{} {}".format(scope["method"], route)
        else:
            request_spans[0].resource = route
        request_spans[0].set_tag_str(http.ROUTE, route)
    else:
        log.debug(
            "unable to update the request span resource name, request_spans:%r, resource_paths:%r",
            request_spans,
            resource_paths,
        )
    request_cookies = ""
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            request_cookies = value.decode("utf-8", errors="ignore")
            break

    if request_spans:
        if asm_config._iast_enabled:
            from ddtrace.appsec._iast._handlers import _iast_instrument_starlette_scope

            _iast_instrument_starlette_scope(scope, request_spans[0].get_tag(http.ROUTE))

        trace_utils.set_http_meta(
            request_spans[0],
            "starlette",
            request_path_params=scope.get("path_params"),
            request_cookies=starlette_requests.cookie_parser(request_cookies),
            route=request_spans[0].get_tag(http.ROUTE),
        )
    core.dispatch("asgi.start_request", ("starlette",))
    blocked = get_blocked()
    if blocked:
        raise BlockingException(blocked)

    # https://github.com/encode/starlette/issues/1336
    if _STARLETTE_VERSION <= parse_version("0.33.0") and len(request_spans) > 1:
        request_spans[-1].set_tag(http.URL, request_spans[0].get_tag(http.URL))

    return wrapped(*args, **kwargs)


@with_traced_module
def _trace_background_tasks(module, pin, wrapped, instance, args, kwargs):
    task = get_argument_value(args, kwargs, 0, "func")
    current_span = pin.tracer.current_span()
    module_name = getattr(module, "__name__", "<unknown>")
    task_name = getattr(task, "__name__", "<unknown>")

    async def traced_task(*args, **kwargs):
        with pin.tracer.start_span(
            f"{module_name}.background_task", resource=task_name, child_of=None, activate=True
        ) as span:
            if current_span:
                span.link_span(current_span.context)
            if inspect.iscoroutinefunction(task):
                await task(*args, **kwargs)
            else:
                await run_in_threadpool(task, *args, **kwargs)

    args, kwargs = set_argument_value(args, kwargs, 0, "func", traced_task)
    wrapped(*args, **kwargs)
