from typing import Any  # noqa:F401
from typing import Dict  # noqa:F401
from typing import List  # noqa:F401
from typing import Optional  # noqa:F401
from typing import Union  # noqa:F401
from urllib.parse import urlparse

import opentracing
from opentracing import Format
from opentracing import Scope  # noqa:F401
from opentracing import ScopeManager  # noqa:F401
from opentracing.scope_managers import ThreadLocalScopeManager

import ddtrace
from ddtrace import config as ddconfig
from ddtrace.internal.constants import SPAN_API_OPENTRACING
from ddtrace.internal.utils.config import get_application_name
from ddtrace.internal.writer import AgentWriterInterface
from ddtrace.settings import ConfigException
from ddtrace.trace import Context as DatadogContext  # noqa:F401
from ddtrace.trace import Span as DatadogSpan
from ddtrace.trace import Tracer as DatadogTracer

from ..internal.logger import get_logger
from .propagation import HTTPPropagator
from .settings import ConfigKeys as keys
from .settings import config_invalid_keys
from .span import Span
from .span_context import SpanContext
from .utils import get_context_provider_for_scope_manager


log = get_logger(__name__)

DEFAULT_CONFIG: Dict[str, Optional[Any]] = {
    keys.AGENT_HOSTNAME: None,
    keys.AGENT_HTTPS: None,
    keys.AGENT_PORT: None,
    keys.DEBUG: False,
    keys.ENABLED: None,
    keys.GLOBAL_TAGS: {},
    keys.SAMPLER: None,
    # Not used, priority sampling can not be disabled in +v3.0
    keys.PRIORITY_SAMPLING: None,
    keys.UDS_PATH: None,
    keys.SETTINGS: {
        "FILTERS": [],
    },
}


class Tracer(opentracing.Tracer):
    """A wrapper providing an OpenTracing API for the Datadog tracer."""

    def __init__(
        self,
        service_name: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        scope_manager: Optional[ScopeManager] = None,
        _dd_tracer: Optional[DatadogTracer] = None,
    ) -> None:
        """Initialize a new Datadog opentracer.

        :param service_name: (optional) the name of the service that this
            tracer will be used with. Note if not provided, a service name will
            try to be determined based off of ``sys.argv``. If this fails a
            :class:`ddtrace.settings.ConfigException` will be raised.
        :param config: (optional) a configuration object to specify additional
            options. See the documentation for further information.
        :param scope_manager: (optional) the scope manager for this tracer to
            use. The available managers are listed in the Python OpenTracing repo
            here: https://github.com/opentracing/opentracing-python#scope-managers.
            If ``None`` is provided, defaults to
            :class:`opentracing.scope_managers.ThreadLocalScopeManager`.
        """
        # Merge the given config with the default into a new dict
        self._config = DEFAULT_CONFIG.copy()
        if config is not None:
            self._config.update(config)
        # Pull out commonly used properties for performance
        self._service_name = service_name or get_application_name()
        self._debug = self._config.get(keys.DEBUG)

        if self._debug and ddconfig._raise:
            # Ensure there are no typos in any of the keys
            invalid_keys = config_invalid_keys(self._config)
            if invalid_keys:
                str_invalid_keys = ",".join(invalid_keys)
                raise ConfigException("invalid key(s) given ({})".format(str_invalid_keys))

        if not self._service_name and ddconfig._raise:
            raise ConfigException(
                """ Cannot detect the \'service_name\'.
                                      Please set the \'service_name=\'
                                      keyword argument.
                                  """
            )

        self._scope_manager = scope_manager or ThreadLocalScopeManager()
        self._dd_tracer = _dd_tracer or ddtrace.tracer
        self._dd_tracer.context_provider = get_context_provider_for_scope_manager(self._scope_manager)

        self._dd_tracer.set_tags(self._config.get(keys.GLOBAL_TAGS))  # type: ignore[arg-type]
        trace_processors = None
        if isinstance(self._config.get(keys.SETTINGS), dict) and self._config[keys.SETTINGS].get("FILTERS"):  # type: ignore[union-attr]
            trace_processors = self._config[keys.SETTINGS]["FILTERS"]  # type: ignore[index]
            self._dd_tracer._span_aggregator.user_processors = trace_processors

        if self._config[keys.ENABLED]:
            self._dd_tracer.enabled = self._config[keys.ENABLED]

        if (
            self._config[keys.AGENT_HOSTNAME]
            or self._config[keys.AGENT_HTTPS]
            or self._config[keys.AGENT_PORT]
            or self._config[keys.UDS_PATH]
        ):
            scheme = "https" if self._config[keys.AGENT_HTTPS] else "http"
            hostname = self._config[keys.AGENT_HOSTNAME]
            port = self._config[keys.AGENT_PORT]
            if self._dd_tracer._agent_url:
                curr_agent_url = urlparse(self._dd_tracer._agent_url)
                scheme = "https" if self._config[keys.AGENT_HTTPS] else curr_agent_url.scheme
                hostname = hostname or curr_agent_url.hostname
                port = port or curr_agent_url.port
            uds_path = self._config[keys.UDS_PATH]

            if uds_path:
                new_url = f"unix://{uds_path}"
            else:
                new_url = f"{scheme}://{hostname}:{port}"
            if isinstance(self._dd_tracer._span_aggregator.writer, AgentWriterInterface):
                self._dd_tracer._span_aggregator.writer.intake_url = new_url
            self._dd_tracer._recreate()

        if self._config[keys.SAMPLER]:
            self._dd_tracer._sampler = self._config[keys.SAMPLER]

        self._propagators = {
            Format.HTTP_HEADERS: HTTPPropagator,
            Format.TEXT_MAP: HTTPPropagator,
        }

    @property
    def scope_manager(self):
        # type: () -> ScopeManager
        """Returns the scope manager being used by this tracer."""
        return self._scope_manager

    def start_active_span(
        self,
        operation_name,  # type: str
        child_of=None,  # type: Optional[Union[Span, SpanContext]]
        references=None,  # type: Optional[List[Any]]
        tags=None,  # type: Optional[Dict[str, str]]
        start_time=None,  # type: Optional[int]
        ignore_active_span=False,  # type: bool
        finish_on_close=True,  # type: bool
    ):
        # type: (...) -> Scope
        """Returns a newly started and activated `Scope`.
        The returned `Scope` supports with-statement contexts. For example::

            with tracer.start_active_span('...') as scope:
                scope.span.set_tag('http.method', 'GET')
                do_some_work()
            # Span.finish() is called as part of Scope deactivation through
            # the with statement.

        It's also possible to not finish the `Span` when the `Scope` context
        expires::

            with tracer.start_active_span('...',
                                          finish_on_close=False) as scope:
                scope.span.set_tag('http.method', 'GET')
                do_some_work()
            # Span.finish() is not called as part of Scope deactivation as
            # `finish_on_close` is `False`.

        :param operation_name: name of the operation represented by the new
            span from the perspective of the current service.
        :param child_of: (optional) a Span or SpanContext instance representing
            the parent in a REFERENCE_CHILD_OF Reference. If specified, the
            `references` parameter must be omitted.
        :param references: (optional) a list of Reference objects that identify
            one or more parent SpanContexts. (See the Reference documentation
            for detail).
        :param tags: an optional dictionary of Span Tags. The caller gives up
            ownership of that dictionary, because the Tracer may use it as-is
            to avoid extra data copying.
        :param start_time: an explicit Span start time as a unix timestamp per
            time.time().
        :param ignore_active_span: (optional) an explicit flag that ignores
            the current active `Scope` and creates a root `Span`.
        :param finish_on_close: whether span should automatically be finished
            when `Scope.close()` is called.
        :return: a `Scope`, already registered via the `ScopeManager`.
        """
        otspan = self.start_span(
            operation_name=operation_name,
            child_of=child_of,
            references=references,
            tags=tags,
            start_time=start_time,
            ignore_active_span=ignore_active_span,
        )

        # activate this new span
        scope = self._scope_manager.activate(otspan, finish_on_close)
        self._dd_tracer.context_provider.activate(otspan._dd_span)
        return scope

    def start_span(
        self,
        operation_name: Optional[str] = None,
        child_of: Optional[Union[Span, SpanContext]] = None,
        references: Optional[List[Any]] = None,
        tags: Optional[Dict[str, str]] = None,
        start_time: Optional[int] = None,
        ignore_active_span: bool = False,
    ) -> Span:
        """Starts and returns a new Span representing a unit of work.

        Starting a root Span (a Span with no causal references)::

            tracer.start_span('...')

        Starting a child Span (see also start_child_span())::

            tracer.start_span(
                '...',
                child_of=parent_span)

        Starting a child Span in a more verbose way::

            tracer.start_span(
                '...',
                references=[opentracing.child_of(parent_span)])

        Note: the precedence when defining a relationship is the following, from highest to lowest:
        1. *child_of*
        2. *references*
        3. `scope_manager.active` (unless *ignore_active_span* is True)
        4. None

        Currently Datadog only supports `child_of` references.

        :param operation_name: name of the operation represented by the new
            span from the perspective of the current service.
        :param child_of: (optional) a Span or SpanContext instance representing
            the parent in a REFERENCE_CHILD_OF Reference. If specified, the
            `references` parameter must be omitted.
        :param references: (optional) a list of Reference objects that identify
            one or more parent SpanContexts. (See the Reference documentation
            for detail)
        :param tags: an optional dictionary of Span Tags. The caller gives up
            ownership of that dictionary, because the Tracer may use it as-is
            to avoid extra data copying.
        :param start_time: an explicit Span start time as a unix timestamp per
            time.time()
        :param ignore_active_span: an explicit flag that ignores the current
            active `Scope` and creates a root `Span`.
        :return: an already-started Span instance.
        """
        ot_parent = None  # 'ot_parent' is more readable than 'child_of'
        ot_parent_context = None  # the parent span's context
        # dd_parent: the child_of to pass to the ddtracer
        dd_parent = None  # type: Optional[Union[DatadogSpan, DatadogContext]]

        if child_of is not None:
            ot_parent = child_of  # 'ot_parent' is more readable than 'child_of'
        elif references and isinstance(references, list):
            # we currently only support child_of relations to one span
            ot_parent = references[0].referenced_context

        # - whenever child_of is not None ddspans with parent-child
        #   relationships will share a ddcontext which maintains a hierarchy of
        #   ddspans for the execution flow
        # - when child_of is a ddspan then the ddtracer uses this ddspan to
        #   create the child ddspan
        # - when child_of is a ddcontext then the ddtracer uses the ddcontext to
        #   get_current_span() for the parent
        if ot_parent is None and not ignore_active_span:
            # attempt to get the parent span from the scope manager
            scope = self._scope_manager.active
            parent_span = getattr(scope, "span", None)
            ot_parent_context = getattr(parent_span, "context", None)

            # Compare the active ot and dd spans. Using the one which
            # was created later as the parent.
            active_dd_parent = self._dd_tracer.context_provider.active()
            if parent_span and isinstance(active_dd_parent, DatadogSpan):
                dd_parent_span = parent_span._dd_span
                if active_dd_parent.start_ns >= dd_parent_span.start_ns:
                    dd_parent = active_dd_parent
                else:
                    dd_parent = dd_parent_span
            else:
                dd_parent = active_dd_parent
        elif ot_parent is not None and isinstance(ot_parent, Span):
            # a span is given to use as a parent
            ot_parent_context = ot_parent.context
            dd_parent = ot_parent._dd_span
        elif ot_parent is not None and isinstance(ot_parent, SpanContext):
            # a span context is given to use to find the parent ddspan
            dd_parent = ot_parent._dd_context
        elif ot_parent is None:
            # user wants to create a new parent span we don't have to do
            # anything
            pass
        elif ddconfig._raise:
            raise TypeError("invalid span configuration given")

        # create a new otspan and ddspan using the ddtracer and associate it
        # with the new otspan
        ddspan = self._dd_tracer.start_span(
            name=operation_name,  # type: ignore[arg-type]
            child_of=dd_parent,
            service=self._service_name,
            activate=False,
            span_api=SPAN_API_OPENTRACING,
        )

        # set the start time if one is specified
        ddspan.start = start_time or ddspan.start

        otspan = Span(self, ot_parent_context, operation_name)  # type: ignore[arg-type]
        # sync up the OT span with the DD span
        otspan._associate_dd_span(ddspan)

        if tags is not None:
            for k in tags:
                # Make sure we set the tags on the otspan to ensure that the special compatibility tags
                # are handled correctly (resource name, span type, sampling priority, etc).
                otspan.set_tag(k, tags[k])

        return otspan

    @property
    def active_span(self):
        # type: () -> Optional[Span]
        """Retrieves the active span from the opentracing scope manager

        Falls back to using the datadog active span if one is not found. This
        allows opentracing users to use datadog instrumentation.
        """
        scope = self._scope_manager.active
        if scope:
            return scope.span
        else:
            dd_span = self._dd_tracer.current_span()
            ot_span = None  # type: Optional[Span]
            if dd_span:
                ot_span = Span(self, None, dd_span.name)
                ot_span._associate_dd_span(dd_span)
            return ot_span

    def inject(self, span_context, format, carrier):  # noqa: A002
        # type: (SpanContext, str, Dict[str, str]) -> None
        """Injects a span context into a carrier.

        :param span_context: span context to inject.
        :param format: format to encode the span context with.
        :param carrier: the carrier of the encoded span context.
        """
        propagator = self._propagators.get(format, None)

        if propagator is None:
            raise opentracing.UnsupportedFormatException

        propagator.inject(span_context, carrier)

    def extract(self, format, carrier):  # noqa: A002
        # type: (str, Dict[str, str]) -> SpanContext
        """Extracts a span context from a carrier.

        :param format: format that the carrier is encoded with.
        :param carrier: the carrier to extract from.
        """
        propagator = self._propagators.get(format, None)

        if propagator is None:
            raise opentracing.UnsupportedFormatException

        # we have to manually activate the returned context from a distributed
        # trace
        ot_span_ctx = propagator.extract(carrier)
        dd_span_ctx = ot_span_ctx._dd_context
        self._dd_tracer.context_provider.activate(dd_span_ctx)
        return ot_span_ctx

    def get_log_correlation_context(self):
        # type: () -> Dict[str, str]
        """Retrieves the data used to correlate a log with the current active trace.
        Generates a dictionary for custom logging instrumentation including the trace id and
        span id of the current active span, as well as the configured service, version, and environment names.
        If there is no active span, a dictionary with an empty string for each value will be returned.
        """
        return self._dd_tracer.get_log_correlation_context()
