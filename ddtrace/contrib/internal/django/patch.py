"""
The Django patching works as follows:

Django internals are instrumented via normal `patch()`.

`django.apps.registry.Apps.populate` is patched to add instrumentation for any
specific Django apps like Django Rest Framework (DRF).
"""

from collections.abc import Iterable
import functools
from inspect import getmro
from inspect import isclass
from inspect import isfunction
from inspect import unwrap
import os
from typing import Dict

import wrapt
from wrapt.importer import when_imported

from ddtrace import config
from ddtrace.appsec._utils import _UserInfoRetriever
from ddtrace.constants import SPAN_KIND
from ddtrace.contrib import dbapi
from ddtrace.contrib import trace_utils
from ddtrace.contrib.internal.trace_utils import _convert_to_string
from ddtrace.contrib.internal.trace_utils import _get_request_header_user_agent
from ddtrace.ext import SpanKind
from ddtrace.ext import SpanTypes
from ddtrace.ext import db
from ddtrace.ext import http
from ddtrace.ext import net
from ddtrace.ext import sql as sqlx
from ddtrace.internal import core
from ddtrace.internal._exceptions import BlockingException
from ddtrace.internal.compat import maybe_stringify
from ddtrace.internal.constants import COMPONENT
from ddtrace.internal.core.event_hub import ResultType
from ddtrace.internal.logger import get_logger
from ddtrace.internal.schema import schematize_service_name
from ddtrace.internal.schema import schematize_url_operation
from ddtrace.internal.schema.span_attribute_schema import SpanDirection
from ddtrace.internal.utils import get_argument_value
from ddtrace.internal.utils import get_blocked
from ddtrace.internal.utils import http as http_utils
from ddtrace.internal.utils import set_blocked
from ddtrace.internal.utils.formats import asbool
from ddtrace.internal.utils.importlib import func_name
from ddtrace.propagation._database_monitoring import _DBM_Propagator
from ddtrace.settings.asm import config as asm_config
from ddtrace.settings.integration import IntegrationConfig
from ddtrace.trace import Pin
from ddtrace.vendor.packaging.version import parse as parse_version


log = get_logger(__name__)

config._add(
    "django",
    dict(
        _default_service=schematize_service_name("django"),
        cache_service_name=os.getenv("DD_DJANGO_CACHE_SERVICE_NAME", default="django"),
        database_service_name_prefix=os.getenv("DD_DJANGO_DATABASE_SERVICE_NAME_PREFIX", default=""),
        database_service_name=os.getenv("DD_DJANGO_DATABASE_SERVICE_NAME", default=""),
        trace_fetch_methods=asbool(os.getenv("DD_DJANGO_TRACE_FETCH_METHODS", default=False)),
        distributed_tracing_enabled=True,
        instrument_middleware=asbool(os.getenv("DD_DJANGO_INSTRUMENT_MIDDLEWARE", default=True)),
        instrument_templates=asbool(os.getenv("DD_DJANGO_INSTRUMENT_TEMPLATES", default=True)),
        instrument_databases=asbool(os.getenv("DD_DJANGO_INSTRUMENT_DATABASES", default=True)),
        instrument_caches=asbool(os.getenv("DD_DJANGO_INSTRUMENT_CACHES", default=True)),
        trace_query_string=None,  # Default to global config
        include_user_name=asm_config._django_include_user_name,
        include_user_email=asm_config._django_include_user_email,
        include_user_login=asm_config._django_include_user_login,
        include_user_realname=asm_config._django_include_user_realname,
        use_handler_with_url_name_resource_format=asbool(
            os.getenv("DD_DJANGO_USE_HANDLER_WITH_URL_NAME_RESOURCE_FORMAT", default=False)
        ),
        use_handler_resource_format=asbool(os.getenv("DD_DJANGO_USE_HANDLER_RESOURCE_FORMAT", default=False)),
        use_legacy_resource_format=asbool(os.getenv("DD_DJANGO_USE_LEGACY_RESOURCE_FORMAT", default=False)),
        _trace_asgi_websocket=os.getenv("DD_ASGI_TRACE_WEBSOCKET", default=False),
        obfuscate_404_resource=os.getenv("DD_ASGI_OBFUSCATE_404_RESOURCE", default=False),
    ),
)

_NotSet = object()
psycopg_cursor_cls = Psycopg2TracedCursor = Psycopg3TracedCursor = _NotSet


DB_CONN_ATTR_BY_TAG = {
    net.TARGET_HOST: "HOST",
    net.TARGET_PORT: "PORT",
    net.SERVER_ADDRESS: "HOST",
    db.USER: "USER",
    db.NAME: "NAME",
}


def get_version():
    # type: () -> str
    import django

    return django.__version__


def _supported_versions() -> Dict[str, str]:
    return {"django": ">=2.2.8"}


def patch_conn(django, conn):
    global psycopg_cursor_cls, Psycopg2TracedCursor, Psycopg3TracedCursor

    if psycopg_cursor_cls is _NotSet:
        try:
            from psycopg.cursor import Cursor as psycopg_cursor_cls

            from ddtrace.contrib.internal.psycopg.cursor import Psycopg3TracedCursor
        except ImportError:
            Psycopg3TracedCursor = None
            try:
                from psycopg2._psycopg import cursor as psycopg_cursor_cls

                from ddtrace.contrib.internal.psycopg.cursor import Psycopg2TracedCursor
            except ImportError:
                psycopg_cursor_cls = None
                Psycopg2TracedCursor = None

    tags = {}
    settings_dict = getattr(conn, "settings_dict", {})
    for tag, attr in DB_CONN_ATTR_BY_TAG.items():
        if attr in settings_dict:
            try:
                tags[tag] = _convert_to_string(conn.settings_dict.get(attr))
            except Exception:
                tags[tag] = str(conn.settings_dict.get(attr))
    conn._datadog_tags = tags

    def cursor(django, pin, func, instance, args, kwargs):
        alias = getattr(conn, "alias", "default")

        if config.django.database_service_name:
            service = config.django.database_service_name
        else:
            database_prefix = config.django.database_service_name_prefix
            service = "{}{}{}".format(database_prefix, alias, "db")
            service = schematize_service_name(service)

        vendor = getattr(conn, "vendor", "db")
        prefix = sqlx.normalize_vendor(vendor)

        tags = {"django.db.vendor": vendor, "django.db.alias": alias}
        tags.update(getattr(conn, "_datadog_tags", {}))

        tracer = pin.tracer
        pin = Pin(service, tags=tags)
        pin._tracer = tracer

        cursor = func(*args, **kwargs)

        traced_cursor_cls = dbapi.TracedCursor
        try:
            if cursor.cursor.__class__.__module__.startswith("psycopg2."):
                # Import lazily to avoid importing psycopg2 if not already imported.
                from ddtrace.contrib.internal.psycopg.cursor import Psycopg2TracedCursor

                traced_cursor_cls = Psycopg2TracedCursor
            elif type(cursor.cursor).__name__ == "Psycopg3TracedCursor":
                # Import lazily to avoid importing psycopg if not already imported.
                from ddtrace.contrib.internal.psycopg.cursor import Psycopg3TracedCursor

                traced_cursor_cls = Psycopg3TracedCursor
        except AttributeError:
            pass

        # Each db alias will need its own config for dbapi
        cfg = IntegrationConfig(
            config.django.global_config,
            "{}-{}".format("django", alias),  # name not used but set anyway
            _default_service=config.django._default_service,
            _dbapi_span_name_prefix=prefix,
            trace_fetch_methods=config.django.trace_fetch_methods,
            _dbm_propagator=_DBM_Propagator(0, "query"),
        )
        return traced_cursor_cls(cursor, pin, cfg)

    if not isinstance(conn.cursor, wrapt.ObjectProxy):
        conn.cursor = wrapt.FunctionWrapper(conn.cursor, trace_utils.with_traced_module(cursor)(django))


def instrument_dbs(django):
    def get_connection(wrapped, instance, args, kwargs):
        conn = wrapped(*args, **kwargs)
        try:
            patch_conn(django, conn)
        except Exception:
            log.debug("Error instrumenting database connection %r", conn, exc_info=True)
        return conn

    if not isinstance(django.db.utils.ConnectionHandler.__getitem__, wrapt.ObjectProxy):
        django.db.utils.ConnectionHandler.__getitem__ = wrapt.FunctionWrapper(
            django.db.utils.ConnectionHandler.__getitem__, get_connection
        )


@trace_utils.with_traced_module
def traced_cache(django, pin, func, instance, args, kwargs):
    from . import utils

    if not config.django.instrument_caches:
        return func(*args, **kwargs)

    cache_backend = "{}.{}".format(instance.__module__, instance.__class__.__name__)
    tags = {COMPONENT: config.django.integration_name, "django.cache.backend": cache_backend}
    if args:
        keys = utils.quantize_key_values(args[0])
        tags["django.cache.key"] = keys

    with core.context_with_data(
        "django.cache",
        span_name="django.cache",
        span_type=SpanTypes.CACHE,
        service=schematize_service_name(config.django.cache_service_name),
        resource=utils.resource_from_cache_prefix(func_name(func), instance),
        tags=tags,
        pin=pin,
    ) as ctx, ctx.span:
        result = func(*args, **kwargs)
        rowcount = 0
        if func.__name__ == "get_many":
            rowcount = sum(1 for doc in result if doc) if result and isinstance(result, Iterable) else 0
        elif func.__name__ == "get":
            try:
                # check also for special case for Django~3.2 that returns an empty Sentinel
                # object for empty results
                # also check if result is Iterable first since some iterables return ambiguous
                # truth results with ``==``
                if result is None or (
                    not isinstance(result, Iterable) and result == getattr(instance, "_missing_key", None)
                ):
                    rowcount = 0
                else:
                    rowcount = 1
            except (AttributeError, NotImplementedError, ValueError):
                pass
        core.dispatch("django.cache", (ctx, rowcount))
        return result


def instrument_caches(django):
    cache_backends = set([cache["BACKEND"] for cache in django.conf.settings.CACHES.values()])
    for cache_path in cache_backends:
        split = cache_path.split(".")
        cache_module = ".".join(split[:-1])
        cache_cls = split[-1]
        for method in ["get", "set", "add", "delete", "incr", "decr", "get_many", "set_many", "delete_many"]:
            try:
                cls = django.utils.module_loading.import_string(cache_path)
                # DEV: this can be removed when we add an idempotent `wrap`
                if not trace_utils.iswrapped(cls, method):
                    trace_utils.wrap(cache_module, "{0}.{1}".format(cache_cls, method), traced_cache(django))
            except Exception:
                log.debug("Error instrumenting cache %r", cache_path, exc_info=True)


@trace_utils.with_traced_module
def traced_populate(django, pin, func, instance, args, kwargs):
    """django.apps.registry.Apps.populate is the method used to populate all the apps.

    It is used as a hook to install instrumentation for 3rd party apps (like DRF).

    `populate()` works in 3 phases:

        - Phase 1: Initializes the app configs and imports the app modules.
        - Phase 2: Imports models modules for each app.
        - Phase 3: runs ready() of each app config.

    If all 3 phases successfully run then `instance.ready` will be `True`.
    """

    # populate() can be called multiple times, we don't want to instrument more than once
    if instance.ready:
        log.debug("Django instrumentation already installed, skipping.")
        return func(*args, **kwargs)

    ret = func(*args, **kwargs)

    if not instance.ready:
        log.debug("populate() failed skipping instrumentation.")
        return ret

    settings = django.conf.settings

    # Instrument databases
    if config.django.instrument_databases:
        try:
            instrument_dbs(django)
        except Exception:
            log.debug("Error instrumenting Django database connections", exc_info=True)

    # Instrument caches
    if config.django.instrument_caches:
        try:
            instrument_caches(django)
        except Exception:
            log.debug("Error instrumenting Django caches", exc_info=True)

    # Instrument Django Rest Framework if it's installed
    INSTALLED_APPS = getattr(settings, "INSTALLED_APPS", [])

    if "rest_framework" in INSTALLED_APPS:
        try:
            from .restframework import patch_restframework

            patch_restframework(django)
        except Exception:
            log.debug("Error patching rest_framework", exc_info=True)

    return ret


def traced_func(django, name, resource=None, ignored_excs=None):
    def wrapped(django, pin, func, instance, args, kwargs):
        tags = {COMPONENT: config.django.integration_name}
        with core.context_with_data(
            "django.func.wrapped", span_name=name, resource=resource, tags=tags, pin=pin
        ) as ctx, ctx.span:
            core.dispatch(
                "django.func.wrapped",
                (
                    args,
                    kwargs,
                    django.core.handlers.wsgi.WSGIRequest if hasattr(django.core.handlers, "wsgi") else object,
                    ctx,
                    ignored_excs,
                ),
            )
            return func(*args, **kwargs)

    return trace_utils.with_traced_module(wrapped)(django)


def traced_process_exception(django, name, resource=None):
    def wrapped(django, pin, func, instance, args, kwargs):
        tags = {COMPONENT: config.django.integration_name}
        with core.context_with_data(
            "django.process_exception", span_name=name, resource=resource, tags=tags, pin=pin
        ) as ctx, ctx.span:
            resp = func(*args, **kwargs)
            core.dispatch(
                "django.process_exception", (ctx, hasattr(resp, "status_code") and 500 <= resp.status_code < 600)
            )
            return resp

    return trace_utils.with_traced_module(wrapped)(django)


@trace_utils.with_traced_module
def traced_load_middleware(django, pin, func, instance, args, kwargs):
    """
    Patches django.core.handlers.base.BaseHandler.load_middleware to instrument all
    middlewares.
    """
    settings_middleware = []
    # Gather all the middleware
    if getattr(django.conf.settings, "MIDDLEWARE", None):
        settings_middleware += django.conf.settings.MIDDLEWARE
    if getattr(django.conf.settings, "MIDDLEWARE_CLASSES", None):
        settings_middleware += django.conf.settings.MIDDLEWARE_CLASSES

    # Iterate over each middleware provided in settings.py
    # Each middleware can either be a function or a class
    for mw_path in settings_middleware:
        mw = django.utils.module_loading.import_string(mw_path)

        # Instrument function-based middleware
        if isfunction(mw) and not trace_utils.iswrapped(mw):
            split = mw_path.split(".")
            if len(split) < 2:
                continue
            base = ".".join(split[:-1])
            attr = split[-1]

            # DEV: We need to have a closure over `mw_path` for the resource name or else
            # all function based middleware will share the same resource name
            def _wrapper(resource):
                # Function-based middleware is a factory which returns a handler function for
                # requests.
                # So instead of tracing the factory, we want to trace its returned value.
                # We wrap the factory to return a traced version of the handler function.
                def wrapped_factory(func, instance, args, kwargs):
                    # r is the middleware handler function returned from the factory
                    r = func(*args, **kwargs)
                    if r:
                        return wrapt.FunctionWrapper(
                            r,
                            traced_func(django, "django.middleware", resource=resource),
                        )
                    # If r is an empty middleware function (i.e. returns None), don't wrap since
                    # NoneType cannot be called
                    else:
                        return r

                return wrapped_factory

            trace_utils.wrap(base, attr, _wrapper(resource=mw_path))

        # Instrument class-based middleware
        elif isclass(mw):
            for hook in [
                "process_request",
                "process_response",
                "process_view",
                "process_template_response",
                "__call__",
            ]:
                if hasattr(mw, hook) and not trace_utils.iswrapped(mw, hook):
                    trace_utils.wrap(
                        mw, hook, traced_func(django, "django.middleware", resource=mw_path + ".{0}".format(hook))
                    )
            # Do a little extra for `process_exception`
            if hasattr(mw, "process_exception") and not trace_utils.iswrapped(mw, "process_exception"):
                res = mw_path + ".{0}".format("process_exception")
                trace_utils.wrap(
                    mw, "process_exception", traced_process_exception(django, "django.middleware", resource=res)
                )

    return func(*args, **kwargs)


def _gather_block_metadata(request, request_headers, ctx: core.ExecutionContext):
    from . import utils

    try:
        metadata = {http.STATUS_CODE: "403", http.METHOD: request.method}
        url = utils.get_request_uri(request)
        query = request.META.get("QUERY_STRING", "")
        if query and config.django.trace_query_string:
            metadata[http.QUERY_STRING] = query
        user_agent = _get_request_header_user_agent(request_headers)
        if user_agent:
            metadata[http.USER_AGENT] = user_agent
    except Exception as e:
        log.warning("Could not gather some metadata on blocked request: %s", str(e))  # noqa: G200
    core.dispatch("django.block_request_callback", (ctx, metadata, config.django, url, query))


def _block_request_callable(request, request_headers, ctx: core.ExecutionContext):
    # This is used by user-id blocking to block responses. It could be called
    # at any point so it's a callable stored in the ASM context.
    from django.core.exceptions import PermissionDenied

    set_blocked()
    _gather_block_metadata(request, request_headers, ctx)
    raise PermissionDenied()


@trace_utils.with_traced_module
def traced_get_response(django, pin, func, instance, args, kwargs):
    """Trace django.core.handlers.base.BaseHandler.get_response() (or other implementations).

    This is the main entry point for requests.

    Django requests are handled by a Handler.get_response method (inherited from base.BaseHandler).
    This method invokes the middleware chain and returns the response generated by the chain.
    """
    from ddtrace.contrib.internal.django.compat import get_resolver

    from . import utils

    request = get_argument_value(args, kwargs, 0, "request")
    if request is None:
        return func(*args, **kwargs)

    request_headers = utils._get_request_headers(request)

    with core.context_with_data(
        "django.traced_get_response",
        remote_addr=request.META.get("REMOTE_ADDR"),
        headers=request_headers,
        headers_case_sensitive=True,
        span_name=schematize_url_operation("django.request", protocol="http", direction=SpanDirection.INBOUND),
        resource=utils.REQUEST_DEFAULT_RESOURCE,
        service=trace_utils.int_service(pin, config.django),
        span_type=SpanTypes.WEB,
        tags={COMPONENT: config.django.integration_name, SPAN_KIND: SpanKind.SERVER},
        integration_config=config.django,
        distributed_headers=request_headers,
        activate_distributed_headers=True,
        pin=pin,
    ) as ctx, ctx.span:
        core.dispatch(
            "django.traced_get_response.pre",
            (
                functools.partial(_block_request_callable, request, request_headers, ctx),
                ctx,
                request,
                utils._before_request_tags,
            ),
        )

        response = None

        def blocked_response():
            from django.http import HttpResponse

            block_config = get_blocked() or {}
            desired_type = block_config.get("type", "auto")
            status = block_config.get("status_code", 403)
            if desired_type == "none":
                response = HttpResponse("", status=status)
                location = block_config.get("location", "")
                if location:
                    response["location"] = location
            else:
                ctype = block_config.get("content-type", "application/json")
                content = http_utils._get_blocked_template(ctype)
                response = HttpResponse(content, content_type=ctype, status=status)
                response.content = content
                response["Content-Length"] = len(content.encode())
            utils._after_request_tags(pin, ctx.span, request, response)
            return response

        try:
            if get_blocked():
                response = blocked_response()
                return response

            query = request.META.get("QUERY_STRING", "")
            uri = utils.get_request_uri(request)
            if uri is not None and query:
                uri += "?" + query
            resolver = get_resolver(getattr(request, "urlconf", None))
            if resolver:
                try:
                    path = resolver.resolve(request.path_info).kwargs
                    log.debug("resolver.pattern %s", path)
                except Exception:
                    path = None

            core.dispatch(
                "django.start_response", (ctx, request, utils._extract_body, utils._remake_body, query, uri, path)
            )
            core.dispatch("django.start_response.post", ("Django",))

            if get_blocked():
                response = blocked_response()
                return response

            try:
                response = func(*args, **kwargs)
            except BlockingException as e:
                set_blocked(e.args[0])
                response = blocked_response()
                return response

            if get_blocked():
                response = blocked_response()
                return response

            return response
        finally:
            core.dispatch("django.finalize_response.pre", (ctx, utils._after_request_tags, request, response))
            if not get_blocked():
                core.dispatch("django.finalize_response", ("Django",))
                if get_blocked():
                    response = blocked_response()
                    return response  # noqa: B012


@trace_utils.with_traced_module
def traced_template_render(django, pin, wrapped, instance, args, kwargs):
    # DEV: Check here in case this setting is configured after a template has been instrumented
    if not config.django.instrument_templates:
        return wrapped(*args, **kwargs)

    template_name = maybe_stringify(getattr(instance, "name", None))
    if template_name:
        resource = template_name
    else:
        resource = "{0}.{1}".format(func_name(instance), wrapped.__name__)

    tags = {COMPONENT: config.django.integration_name}
    if template_name:
        tags["django.template.name"] = template_name
    engine = getattr(instance, "engine", None)
    if engine:
        tags["django.template.engine.class"] = func_name(engine)

    with core.context_with_data(
        "django.template.render",
        span_name="django.template.render",
        resource=resource,
        span_type=http.TEMPLATE,
        tags=tags,
        pin=pin,
    ) as ctx, ctx.span:
        return wrapped(*args, **kwargs)


def instrument_view(django, view):
    """
    Helper to wrap Django views.

    We want to wrap all lifecycle/http method functions for every class in the MRO for this view
    """
    if hasattr(view, "__mro__"):
        for cls in reversed(getmro(view)):
            _instrument_view(django, cls)

    return _instrument_view(django, view)


def _instrument_view(django, view):
    """Helper to wrap Django views."""
    from . import utils

    # All views should be callable, double check before doing anything
    if not callable(view):
        return view

    # Patch view HTTP methods and lifecycle methods
    http_method_names = getattr(view, "http_method_names", ("get", "delete", "post", "options", "head"))
    lifecycle_methods = ("setup", "dispatch", "http_method_not_allowed")
    for name in list(http_method_names) + list(lifecycle_methods):
        try:
            func = getattr(view, name, None)
            if not func or isinstance(func, wrapt.ObjectProxy):
                continue

            resource = "{0}.{1}".format(func_name(view), name)
            op_name = "django.view.{0}".format(name)
            trace_utils.wrap(view, name, traced_func(django, name=op_name, resource=resource))
        except Exception:
            log.debug("Failed to instrument Django view %r function %s", view, name, exc_info=True)

    # Patch response methods
    response_cls = getattr(view, "response_class", None)
    if response_cls:
        methods = ("render",)
        for name in methods:
            try:
                func = getattr(response_cls, name, None)
                # Do not wrap if the method does not exist or is already wrapped
                if not func or isinstance(func, wrapt.ObjectProxy):
                    continue

                resource = "{0}.{1}".format(func_name(response_cls), name)
                op_name = "django.response.{0}".format(name)
                trace_utils.wrap(response_cls, name, traced_func(django, name=op_name, resource=resource))
            except Exception:
                log.debug("Failed to instrument Django response %r function %s", response_cls, name, exc_info=True)

    # If the view itself is not wrapped, wrap it
    if not isinstance(view, wrapt.ObjectProxy):
        view = utils.DjangoViewProxy(
            view, traced_func(django, "django.view", resource=func_name(view), ignored_excs=[django.http.Http404])
        )
    return view


@trace_utils.with_traced_module
def traced_urls_path(django, pin, wrapped, instance, args, kwargs):
    """Wrapper for url path helpers to ensure all views registered as urls are traced."""
    try:
        from_args = False
        view = kwargs.pop("view", None)
        if view is None:
            view = args[1]
            from_args = True

        core.dispatch("service_entrypoint.patch", (unwrap(view),))

        if from_args:
            args = list(args)
            args[1] = instrument_view(django, view)
            args = tuple(args)
        else:
            kwargs["view"] = instrument_view(django, view)
    except Exception:
        log.debug("Failed to instrument Django url path %r %r", args, kwargs, exc_info=True)
    return wrapped(*args, **kwargs)


@trace_utils.with_traced_module
def traced_as_view(django, pin, func, instance, args, kwargs):
    """
    Wrapper for django's View.as_view class method
    """
    try:
        instrument_view(django, instance)
    except Exception:
        log.debug("Failed to instrument Django view %r", instance, exc_info=True)
    view = func(*args, **kwargs)
    return wrapt.FunctionWrapper(view, traced_func(django, "django.view", resource=func_name(instance)))


@trace_utils.with_traced_module
def traced_technical_500_response(django, pin, func, instance, args, kwargs):
    """
    Wrapper for django's views.debug.technical_500_response
    """
    response = func(*args, **kwargs)
    try:
        request = get_argument_value(args, kwargs, 0, "request")
        exc_type = get_argument_value(args, kwargs, 1, "exc_type")
        exc_value = get_argument_value(args, kwargs, 2, "exc_value")
        tb = get_argument_value(args, kwargs, 3, "tb")
        core.dispatch("django.technical_500_response", (request, response, exc_type, exc_value, tb))
    except Exception:
        log.debug("Error while trying to trace Django technical 500 response", exc_info=True)
    return response


@trace_utils.with_traced_module
def traced_get_asgi_application(django, pin, func, instance, args, kwargs):
    from ddtrace.contrib.asgi import TraceMiddleware
    from ddtrace.internal.constants import COMPONENT

    def django_asgi_modifier(span, scope):
        span.name = schematize_url_operation("django.request", protocol="http", direction=SpanDirection.INBOUND)
        span.set_tag_str(COMPONENT, config.django.integration_name)

    return TraceMiddleware(func(*args, **kwargs), integration_config=config.django, span_modifier=django_asgi_modifier)


class _DjangoUserInfoRetriever(_UserInfoRetriever):
    def __init__(self, user, credentials=None):
        super(_DjangoUserInfoRetriever, self).__init__(user)

        self.credentials = credentials if credentials else {}
        if self.credentials and not user:
            self._try_load_user()

    def _try_load_user(self):
        self.user_model = None

        try:
            from django.contrib.auth import get_user_model
        except ImportError:
            log.debug("user_exist: Could not import Django get_user_model", exc_info=True)
            return

        try:
            self.user_model = get_user_model()
            if not self.user_model:
                return
        except Exception:
            log.debug("user_exist: Could not get the user model", exc_info=True)
            return

        login_field = asm_config._user_model_login_field
        login_field_value = self.credentials.get(login_field, None) if login_field else None

        if not login_field or not login_field_value:
            # Try to get the username from the credentials
            for possible_login_field in self.possible_login_fields:
                if possible_login_field in self.credentials:
                    login_field = possible_login_field
                    login_field_value = self.credentials[login_field]
                    break
            else:
                # Could not get what the login field, so we can't check if the user exists
                log.debug("try_load_user_model: could not get the login field from the credentials")
                return

        try:
            self.user = self.user_model.objects.get(**{login_field: login_field_value})
        except self.user_model.DoesNotExist:
            log.debug("try_load_user_model: could not load user model", exc_info=True)

    def user_exists(self):
        return self.user is not None

    def get_username(self):
        if hasattr(self.user, "USERNAME_FIELD") and not asm_config._user_model_name_field:
            user_type = type(self.user)
            return getattr(self.user, user_type.USERNAME_FIELD, None)

        return super(_DjangoUserInfoRetriever, self).get_username()

    def get_name(self):
        if not asm_config._user_model_name_field:
            if hasattr(self.user, "get_full_name"):
                try:
                    return self.user.get_full_name()
                except Exception:
                    log.debug("User model get_full_name member produced an exception: ", exc_info=True)

            if hasattr(self.user, "first_name") and hasattr(self.user, "last_name"):
                return "%s %s" % (self.user.first_name, self.user.last_name)

        return super(_DjangoUserInfoRetriever, self).get_name()

    def get_user_email(self):
        if hasattr(self.user, "EMAIL_FIELD") and not asm_config._user_model_name_field:
            user_type = type(self.user)
            return getattr(self.user, user_type.EMAIL_FIELD, None)

        return super(_DjangoUserInfoRetriever, self).get_user_email()


@trace_utils.with_traced_module
def traced_login(django, pin, func, instance, args, kwargs):
    func(*args, **kwargs)
    mode = asm_config._user_event_mode
    if mode == "disabled":
        return
    try:
        request = get_argument_value(args, kwargs, 0, "request")
        user = get_argument_value(args, kwargs, 1, "user")
        core.dispatch("django.login", (pin, request, user, mode, _DjangoUserInfoRetriever(user), config.django))
    except Exception:
        log.debug("Error while trying to trace Django login", exc_info=True)


@trace_utils.with_traced_module
def traced_authenticate(django, pin, func, instance, args, kwargs):
    result_user = func(*args, **kwargs)
    mode = asm_config._user_event_mode
    if mode == "disabled":
        return result_user
    try:
        result = core.dispatch_with_results(
            "django.auth",
            (result_user, mode, kwargs, pin, _DjangoUserInfoRetriever(result_user, credentials=kwargs), config.django),
        ).user
        if result and result.value[0]:
            return result.value[1]
    except Exception:
        log.debug("Error while trying to trace Django authenticate", exc_info=True)

    return result_user


@trace_utils.with_traced_module
def traced_process_request(django, pin, func, instance, args, kwargs):
    tags = {COMPONENT: config.django.integration_name}
    with core.context_with_data(
        "django.func.wrapped",
        span_name="django.middleware",
        resource="django.contrib.auth.middleware.AuthenticationMiddleware.process_request",
        tags=tags,
        pin=pin,
    ) as ctx, ctx.span:
        core.dispatch(
            "django.func.wrapped",
            (
                args,
                kwargs,
                django.core.handlers.wsgi.WSGIRequest if hasattr(django.core.handlers, "wsgi") else object,
                ctx,
                None,
            ),
        )
        func(*args, **kwargs)
        mode = asm_config._user_event_mode
        if mode == "disabled":
            return
        try:
            request = get_argument_value(args, kwargs, 0, "request")
            if request:
                if hasattr(request, "user") and hasattr(request.user, "_setup"):
                    request.user._setup()
                    request_user = request.user._wrapped
                else:
                    request_user = request.user
                if hasattr(request, "session") and hasattr(request.session, "session_key"):
                    session_key = request.session.session_key
                else:
                    session_key = None
                core.dispatch(
                    "django.process_request",
                    (
                        request_user,
                        session_key,
                        mode,
                        kwargs,
                        pin,
                        _DjangoUserInfoRetriever(request_user, credentials=kwargs),
                        config.django,
                    ),
                )
        except Exception:
            log.debug("Error while trying to trace Django AuthenticationMiddleware process_request", exc_info=True)


@trace_utils.with_traced_module
def patch_create_user(django, pin, func, instance, args, kwargs):
    user = func(*args, **kwargs)
    core.dispatch(
        "django.create_user", (config.django, pin, func, instance, args, kwargs, user, _DjangoUserInfoRetriever(user))
    )
    return user


def unwrap_views(func, instance, args, kwargs):
    """
    Django channels uses path() and re_path() to route asgi applications. This broke our initial
    assumption that
    django path/re_path/url functions only accept views. Here we unwrap ddtrace view
    instrumentation from asgi
    applications.

    Ex. ``channels.routing.URLRouter([path('', get_asgi_application())])``
    On startup ddtrace.contrib.internal.django.path.instrument_view() will wrap get_asgi_application in a
    DjangoViewProxy.
    Since get_asgi_application is not a django view callback this function will unwrap it.
    """
    from . import utils

    routes = get_argument_value(args, kwargs, 0, "routes")
    for route in routes:
        if isinstance(route.callback, utils.DjangoViewProxy):
            route.callback = route.callback.__wrapped__

    return func(*args, **kwargs)


def _patch(django):
    Pin().onto(django)

    when_imported("django.apps.registry")(lambda m: trace_utils.wrap(m, "Apps.populate", traced_populate(django)))

    if config.django.instrument_middleware:
        when_imported("django.core.handlers.base")(
            lambda m: trace_utils.wrap(m, "BaseHandler.load_middleware", traced_load_middleware(django))
        )

    when_imported("django.core.handlers.wsgi")(lambda m: trace_utils.wrap(m, "WSGIRequest.__init__", wrap_wsgi_environ))
    core.dispatch("django.patch", ())

    @when_imported("django.core.handlers.base")
    def _(m):
        import django

        trace_utils.wrap(m, "BaseHandler.get_response", traced_get_response(django))
        if django.VERSION >= (3, 1):
            # Have to inline this import as the module contains syntax incompatible with Python 3.5 and below
            from ._asgi import traced_get_response_async

            trace_utils.wrap(m, "BaseHandler.get_response_async", traced_get_response_async(django))

    @when_imported("django.contrib.auth")
    def _(m):
        trace_utils.wrap(m, "login", traced_login(django))
        trace_utils.wrap(m, "authenticate", traced_authenticate(django))

    @when_imported("django.contrib.auth.middleware")
    def _(m):
        trace_utils.wrap(m, "AuthenticationMiddleware.process_request", traced_process_request(django))

    # Only wrap get_asgi_application if get_response_async exists. Otherwise we will effectively double-patch
    # because get_response and get_asgi_application will be used. We must rely on the version instead of coalescing
    # with the previous patching hook because of circular imports within `django.core.asgi`.
    if django.VERSION >= (3, 1):
        when_imported("django.core.asgi")(
            lambda m: trace_utils.wrap(m, "get_asgi_application", traced_get_asgi_application(django))
        )

    if config.django.instrument_templates:
        when_imported("django.template.base")(
            lambda m: trace_utils.wrap(m, "Template.render", traced_template_render(django))
        )

    if django.VERSION < (4, 0, 0):
        when_imported("django.conf.urls")(lambda m: trace_utils.wrap(m, "url", traced_urls_path(django)))

    if django.VERSION >= (2, 0, 0):

        @when_imported("django.urls")
        def _(m):
            trace_utils.wrap(m, "path", traced_urls_path(django))
            trace_utils.wrap(m, "re_path", traced_urls_path(django))

    when_imported("django.views.generic.base")(lambda m: trace_utils.wrap(m, "View.as_view", traced_as_view(django)))
    when_imported("django.views.debug")(
        lambda m: trace_utils.wrap(m, "technical_500_response", traced_technical_500_response(django))
    )

    @when_imported("channels.routing")
    def _(m):
        import channels

        channels_version = parse_version(channels.__version__)
        if channels_version >= parse_version("3.0"):
            # ASGI3 is only supported in channels v3.0+
            trace_utils.wrap(m, "URLRouter.__init__", unwrap_views)

    when_imported("django.contrib.auth.models")(
        lambda m: trace_utils.wrap(m, "UserManager.create_user", patch_create_user(django))
    )


def wrap_wsgi_environ(wrapped, _instance, args, kwargs):
    result = core.dispatch_with_results("django.wsgi_environ", (wrapped, _instance, args, kwargs)).wrapped_result
    # if the callback is registered and runs, return the result
    if result:
        return result.value
    # if the callback is not registered, return the original result
    elif result.response_type == ResultType.RESULT_UNDEFINED:
        return wrapped(*args, **kwargs)
    # if an exception occurs, raise it. It should never happen.
    elif result.exception:
        raise result.exception


def patch():
    import django

    if getattr(django, "_datadog_patch", False):
        return
    _patch(django)

    django._datadog_patch = True


def _unpatch(django):
    trace_utils.unwrap(django.apps.registry.Apps, "populate")
    trace_utils.unwrap(django.core.handlers.base.BaseHandler, "load_middleware")
    trace_utils.unwrap(django.core.handlers.base.BaseHandler, "get_response")
    trace_utils.unwrap(django.core.handlers.base.BaseHandler, "get_response_async")
    trace_utils.unwrap(django.template.base.Template, "render")
    trace_utils.unwrap(django.conf.urls.static, "static")
    trace_utils.unwrap(django.conf.urls, "url")
    trace_utils.unwrap(django.contrib.auth.login, "login")
    trace_utils.unwrap(django.contrib.auth.authenticate, "authenticate")
    trace_utils.unwrap(django.view.debug.technical_500_response, "technical_500_response")
    if django.VERSION >= (2, 0, 0):
        trace_utils.unwrap(django.urls, "path")
        trace_utils.unwrap(django.urls, "re_path")
    trace_utils.unwrap(django.views.generic.base.View, "as_view")
    for conn in django.db.connections.all():
        trace_utils.unwrap(conn, "cursor")
    trace_utils.unwrap(django.db.utils.ConnectionHandler, "__getitem__")


def unpatch():
    import django

    if not getattr(django, "_datadog_patch", False):
        return

    _unpatch(django)

    django._datadog_patch = False
