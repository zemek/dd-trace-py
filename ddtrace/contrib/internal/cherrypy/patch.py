"""
Datadog trace code for cherrypy.
"""
import logging
import os
from typing import Dict

import cherrypy
from cherrypy.lib.httputil import valid_status

from ddtrace import config
from ddtrace.constants import ERROR_MSG
from ddtrace.constants import ERROR_STACK
from ddtrace.constants import ERROR_TYPE
from ddtrace.contrib import trace_utils
from ddtrace.ext import SpanTypes
from ddtrace.internal import compat
from ddtrace.internal import core
from ddtrace.internal.schema import SpanDirection
from ddtrace.internal.schema import schematize_service_name
from ddtrace.internal.schema import schematize_url_operation
from ddtrace.internal.utils.formats import asbool


log = logging.getLogger(__name__)


# Configure default configuration
config._add(
    "cherrypy",
    dict(
        distributed_tracing=asbool(os.getenv("DD_CHERRYPY_DISTRIBUTED_TRACING", default=True)),
    ),
)


def get_version():
    # type: () -> str
    return getattr(cherrypy, "__version__", "")


def _supported_versions() -> Dict[str, str]:
    return {"cherrypy": ">=17.0.0"}


SPAN_NAME = schematize_url_operation("cherrypy.request", protocol="http", direction=SpanDirection.INBOUND)


class TraceTool(cherrypy.Tool):
    def __init__(self, app, tracer, service, use_distributed_tracing=None):
        self.app = app
        self._tracer = tracer
        self.service = service
        if use_distributed_tracing is not None:
            self.use_distributed_tracing = use_distributed_tracing

        # CherryPy uses priority to determine which tools act first on each event. The lower the number, the higher
        # the priority. See: https://docs.cherrypy.org/en/latest/extend.html#tools-ordering
        cherrypy.Tool.__init__(self, "on_start_resource", self._on_start_resource, priority=95)

    @property
    def use_distributed_tracing(self):
        return config.cherrypy.distributed_tracing

    @use_distributed_tracing.setter
    def use_distributed_tracing(self, use_distributed_tracing):
        config.cherrypy["distributed_tracing"] = asbool(use_distributed_tracing)

    @property
    def service(self):
        return config.cherrypy.get("service", "cherrypy")

    @service.setter
    def service(self, service):
        config.cherrypy["service"] = schematize_service_name(service)

    def _setup(self):
        cherrypy.Tool._setup(self)
        cherrypy.request.hooks.attach("on_end_request", self._on_end_request, priority=5)
        cherrypy.request.hooks.attach("after_error_response", self._after_error_response, priority=5)

    def _on_start_resource(self):
        with core.context_with_data(
            "cherrypy.request",
            span_name=SPAN_NAME,
            span_type=SpanTypes.WEB,
            service=trace_utils.int_service(None, config.cherrypy, default="cherrypy"),
            tags={},
            tracer=self._tracer,
            distributed_headers=cherrypy.request.headers,
            integration_config=config.cherrypy,
            activate_distributed_headers=True,
            headers_case_sensitive=True,
        ) as ctx:
            req_span = ctx.span

            ctx.set_item("req_span", req_span)
            core.dispatch("web.request.start", (ctx, config.cherrypy))

            cherrypy.request._datadog_span = req_span

    def _after_error_response(self):
        span = getattr(cherrypy.request, "_datadog_span", None)

        if not span:
            log.warning("cherrypy: tracing tool after_error_response hook called, but no active span found")
            return

        span.error = 1
        span.set_tag_str(ERROR_TYPE, str(cherrypy._cperror._exc_info()[0]))
        span.set_tag_str(ERROR_MSG, str(cherrypy._cperror._exc_info()[1]))
        span.set_tag_str(ERROR_STACK, cherrypy._cperror.format_exc())

        self._close_span(span)

    def _on_end_request(self):
        span = getattr(cherrypy.request, "_datadog_span", None)

        if not span:
            log.warning("cherrypy: tracing tool on_end_request hook called, but no active span found")
            return

        self._close_span(span)

    def _close_span(self, span):
        # Let users specify their own resource in middleware if they so desire.
        # See case https://github.com/DataDog/dd-trace-py/issues/353
        if span.resource == SPAN_NAME:
            # In the future, mask virtual path components in a
            # URL e.g. /dispatch/abc123 becomes /dispatch/{{test_value}}/
            # Following investigation, this should be possible using
            # [find_handler](https://docs.cherrypy.org/en/latest/_modules/cherrypy/_cpdispatch.html#Dispatcher.find_handler)
            # but this may not be as easy as `cherrypy.request.dispatch.find_handler(cherrypy.request.path_info)` as
            # this function only ever seems to return an empty list for the virtual path components.

            # For now, default resource is method and path:
            #   GET /
            #   POST /save
            resource = "{} {}".format(cherrypy.request.method, cherrypy.request.path_info)
            span.resource = compat.to_unicode(resource)

        url = compat.to_unicode(cherrypy.request.base + cherrypy.request.path_info)
        status_code, _, _ = valid_status(cherrypy.response.status)

        core.dispatch(
            "web.request.finish",
            (
                span,
                config.cherrypy,
                cherrypy.request.method,
                url,
                status_code,
                None,
                cherrypy.request.headers,
                cherrypy.response.headers,
                None,
                True,
            ),
        )

        # Clear our span just in case.
        cherrypy.request._datadog_span = None


class TraceMiddleware(object):
    def __init__(self, app, tracer, service="cherrypy", distributed_tracing=None):
        self.app = app

        self.app.tools.tracer = TraceTool(app, tracer, service, distributed_tracing)
