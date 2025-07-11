from io import StringIO
import math
import pprint
import sys
from time import time_ns
import traceback
from types import TracebackType
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Text
from typing import Type
from typing import Union
from typing import cast

from ddtrace._trace._limits import MAX_SPAN_META_VALUE_LEN
from ddtrace._trace._span_link import SpanLink
from ddtrace._trace._span_link import SpanLinkKind
from ddtrace._trace._span_pointer import _SpanPointer
from ddtrace._trace._span_pointer import _SpanPointerDirection
from ddtrace._trace.context import Context
from ddtrace._trace.types import _AttributeValueType
from ddtrace._trace.types import _MetaDictType
from ddtrace._trace.types import _MetricDictType
from ddtrace._trace.types import _TagNameType
from ddtrace.constants import _SAMPLING_AGENT_DECISION
from ddtrace.constants import _SAMPLING_LIMIT_DECISION
from ddtrace.constants import _SAMPLING_RULE_DECISION
from ddtrace.constants import _SPAN_MEASURED_KEY
from ddtrace.constants import ERROR_MSG
from ddtrace.constants import ERROR_STACK
from ddtrace.constants import ERROR_TYPE
from ddtrace.constants import MANUAL_DROP_KEY
from ddtrace.constants import MANUAL_KEEP_KEY
from ddtrace.constants import SERVICE_KEY
from ddtrace.constants import SERVICE_VERSION_KEY
from ddtrace.constants import USER_KEEP
from ddtrace.constants import USER_REJECT
from ddtrace.constants import VERSION_KEY
from ddtrace.ext import http
from ddtrace.ext import net
from ddtrace.internal import core
from ddtrace.internal._rand import rand64bits as _rand64bits
from ddtrace.internal._rand import rand128bits as _rand128bits
from ddtrace.internal.compat import NumericType
from ddtrace.internal.compat import ensure_text
from ddtrace.internal.compat import is_integer
from ddtrace.internal.constants import MAX_INT_64BITS as _MAX_INT_64BITS
from ddtrace.internal.constants import MAX_UINT_64BITS as _MAX_UINT_64BITS
from ddtrace.internal.constants import MIN_INT_64BITS as _MIN_INT_64BITS
from ddtrace.internal.constants import SPAN_API_DATADOG
from ddtrace.internal.logger import get_logger
from ddtrace.internal.sampling import SamplingMechanism
from ddtrace.internal.sampling import set_sampling_decision_maker
from ddtrace.internal.utils.deprecations import DDTraceDeprecationWarning
from ddtrace.settings._config import config
from ddtrace.vendor.debtcollector import deprecate


class SpanEvent:
    __slots__ = ["name", "attributes", "time_unix_nano"]

    def __init__(
        self,
        name: str,
        attributes: Optional[Dict[str, _AttributeValueType]] = None,
        time_unix_nano: Optional[int] = None,
    ):
        self.name: str = name
        if time_unix_nano is None:
            time_unix_nano = time_ns()
        self.time_unix_nano: int = time_unix_nano
        self.attributes: dict = attributes if attributes else {}

    def __dict__(self):
        d = {"name": self.name, "time_unix_nano": self.time_unix_nano}
        if self.attributes:
            d["attributes"] = self.attributes
        return d

    def __str__(self):
        """
        Stringify and return value.
        Attribute value can be either str, bool, int, float, or a list of these.
        """

        attrs_str = ",".join(f"{k}:{v}" for k, v in self.attributes.items())
        return f"name={self.name} time={self.time_unix_nano} attributes={attrs_str}"

    def __iter__(self):
        yield "name", self.name
        yield "time_unix_nano", self.time_unix_nano
        if self.attributes:
            yield "attributes", self.attributes


log = get_logger(__name__)


def _get_64_lowest_order_bits_as_int(large_int: int) -> int:
    """Get the 64 lowest order bits from a 128bit integer"""
    return _MAX_UINT_64BITS & large_int


def _get_64_highest_order_bits_as_hex(large_int: int) -> str:
    """Get the 64 highest order bits from a 128bit integer"""
    return "{:032x}".format(large_int)[:16]


class Span(object):
    __slots__ = [
        # Public span attributes
        "service",
        "name",
        "_resource",
        "_span_api",
        "span_id",
        "trace_id",
        "parent_id",
        "_meta",
        "_meta_struct",
        "error",
        "_metrics",
        "_store",
        "span_type",
        "start_ns",
        "duration_ns",
        # Internal attributes
        "_context",
        "_parent_context",
        "_local_root_value",
        "_parent",
        "_ignored_exceptions",
        "_on_finish_callbacks",
        "_links",
        "_events",
        "__weakref__",
    ]

    def __init__(
        self,
        name: str,
        service: Optional[str] = None,
        resource: Optional[str] = None,
        span_type: Optional[str] = None,
        trace_id: Optional[int] = None,
        span_id: Optional[int] = None,
        parent_id: Optional[int] = None,
        start: Optional[int] = None,
        context: Optional[Context] = None,
        on_finish: Optional[List[Callable[["Span"], None]]] = None,
        span_api: str = SPAN_API_DATADOG,
        links: Optional[List[SpanLink]] = None,
    ) -> None:
        """
        Create a new span. Call `finish` once the traced operation is over.

        **Note:** A ``Span`` should only be accessed or modified in the process
        that it was created in. Using a ``Span`` from within a child process
        could result in a deadlock or unexpected behavior.

        :param str name: the name of the traced operation.

        :param str service: the service name
        :param str resource: the resource name
        :param str span_type: the span type

        :param int trace_id: the id of this trace's root span.
        :param int parent_id: the id of this span's direct parent span.
        :param int span_id: the id of this span.

        :param int start: the start time of request as a unix epoch in seconds
        :param object context: the Context of the span.
        :param on_finish: list of functions called when the span finishes.
        """
        if not (span_id is None or isinstance(span_id, int)):
            if config._raise:
                raise TypeError("span_id must be an integer")
            return
        if not (trace_id is None or isinstance(trace_id, int)):
            if config._raise:
                raise TypeError("trace_id must be an integer")
            return
        if not (parent_id is None or isinstance(parent_id, int)):
            if config._raise:
                raise TypeError("parent_id must be an integer")
            return
        self.name = name
        self.service = service
        self._resource = [resource or name]
        self.span_type = span_type
        self._span_api = span_api

        self._meta: _MetaDictType = {}
        self.error = 0
        self._metrics: _MetricDictType = {}

        self._meta_struct: Dict[str, Dict[str, Any]] = {}

        self.start_ns: int = time_ns() if start is None else int(start * 1e9)
        self.duration_ns: Optional[int] = None

        if trace_id is not None:
            self.trace_id: int = trace_id
        elif config._128_bit_trace_id_enabled:
            self.trace_id: int = _rand128bits()  # type: ignore[no-redef]
        else:
            self.trace_id: int = _rand64bits()  # type: ignore[no-redef]
        self.span_id: int = span_id or _rand64bits()
        self.parent_id: Optional[int] = parent_id
        self._on_finish_callbacks = [] if on_finish is None else on_finish

        self._parent_context: Optional[Context] = context
        self._context = context.copy(self.trace_id, self.span_id) if context else None

        self._links: List[Union[SpanLink, _SpanPointer]] = []
        if links:
            for new_link in links:
                self._set_link_or_append_pointer(new_link)

        self._events: List[SpanEvent] = []
        self._parent: Optional["Span"] = None
        self._ignored_exceptions: Optional[List[Type[Exception]]] = None
        self._local_root_value: Optional["Span"] = None  # None means this is the root span.
        self._store: Optional[Dict[str, Any]] = None

    def _update_tags_from_context(self) -> None:
        context = self._context
        if context is None:
            return

        with context:
            for tag in context._meta:
                self._meta.setdefault(tag, context._meta[tag])
            for metric in context._metrics:
                self._metrics.setdefault(metric, context._metrics[metric])

    def _ignore_exception(self, exc: Type[Exception]) -> None:
        if self._ignored_exceptions is None:
            self._ignored_exceptions = [exc]
        else:
            self._ignored_exceptions.append(exc)

    def _set_ctx_item(self, key: str, val: Any) -> None:
        if not self._store:
            self._store = {}
        self._store[key] = val

    def _set_ctx_items(self, items: Dict[str, Any]) -> None:
        if not self._store:
            self._store = {}
        self._store.update(items)

    def _get_ctx_item(self, key: str) -> Optional[Any]:
        if not self._store:
            return None
        return self._store.get(key)

    @property
    def _trace_id_64bits(self) -> int:
        return _get_64_lowest_order_bits_as_int(self.trace_id)

    @property
    def start(self) -> float:
        """The start timestamp in Unix epoch seconds."""
        return self.start_ns / 1e9

    @start.setter
    def start(self, value: Union[int, float]) -> None:
        self.start_ns = int(value * 1e9)

    @property
    def resource(self) -> str:
        return self._resource[0]

    @resource.setter
    def resource(self, value: str) -> None:
        self._resource[0] = value

    @property
    def finished(self) -> bool:
        return self.duration_ns is not None

    @finished.setter
    def finished(self, value: bool) -> None:
        """Finishes the span if set to a truthy value.

        If the span is already finished and a truthy value is provided
        no action will occur.
        """
        if value:
            if not self.finished:
                self.duration_ns = time_ns() - self.start_ns
        else:
            self.duration_ns = None

    @property
    def duration(self) -> Optional[float]:
        """The span duration in seconds."""
        if self.duration_ns is not None:
            return self.duration_ns / 1e9
        return None

    @duration.setter
    def duration(self, value: float) -> None:
        self.duration_ns = int(value * 1e9)

    def finish(self, finish_time: Optional[float] = None) -> None:
        """Mark the end time of the span and submit it to the tracer.
        If the span has already been finished don't do anything.

        :param finish_time: The end time of the span, in seconds. Defaults to ``now``.
        """
        if finish_time is None:
            self._finish_ns(time_ns())
        else:
            self._finish_ns(int(finish_time * 1e9))

    def _finish_ns(self, finish_time_ns: int) -> None:
        if self.duration_ns is not None:
            return

        # be defensive so we don't die if start isn't set
        self.duration_ns = finish_time_ns - (self.start_ns or finish_time_ns)

        for cb in self._on_finish_callbacks:
            cb(self)

    def _override_sampling_decision(self, decision: Optional[NumericType]):
        self.context.sampling_priority = decision
        set_sampling_decision_maker(self.context, SamplingMechanism.MANUAL)
        if self._local_root:
            for key in (_SAMPLING_RULE_DECISION, _SAMPLING_AGENT_DECISION, _SAMPLING_LIMIT_DECISION):
                if key in self._local_root._metrics:
                    del self._local_root._metrics[key]

    def set_tag(self, key: _TagNameType, value: Any = None) -> None:
        """Set a tag key/value pair on the span.

        Keys must be strings, values must be ``str``-able.

        :param key: Key to use for the tag
        :type key: str
        :param value: Value to assign for the tag
        :type value: ``str``-able value
        """

        if not isinstance(key, str):
            log.warning("Ignoring tag pair %s:%s. Key must be a string.", key, value)
            return

        # Special case, force `http.status_code` as a string
        # DEV: `http.status_code` *has* to be in `meta` for metrics
        #   calculated in the trace agent
        if key == http.STATUS_CODE:
            value = str(value)

        # Determine once up front
        val_is_an_int = is_integer(value)

        # Explicitly try to convert expected integers to `int`
        # DEV: Some integrations parse these values from strings, but don't call `int(value)` themselves
        INT_TYPES = (net.TARGET_PORT,)
        if key in INT_TYPES and not val_is_an_int:
            try:
                value = int(value)
                val_is_an_int = True
            except (ValueError, TypeError):
                pass

        # Set integers that are less than equal to 2^53 as metrics
        if value is not None and val_is_an_int and abs(value) <= 2**53:
            self.set_metric(key, value)
            return

        # All floats should be set as a metric
        elif isinstance(value, float):
            self.set_metric(key, value)
            return

        elif key == MANUAL_KEEP_KEY:
            self._override_sampling_decision(USER_KEEP)
            return
        elif key == MANUAL_DROP_KEY:
            self._override_sampling_decision(USER_REJECT)
            return
        elif key == SERVICE_KEY:
            self.service = value
        elif key == SERVICE_VERSION_KEY:
            # Also set the `version` tag to the same value
            # DEV: Note that we do no return, we want to set both
            self.set_tag(VERSION_KEY, value)
        elif key == _SPAN_MEASURED_KEY:
            # Set `_dd.measured` tag as a metric
            # DEV: `set_metric` will ensure it is an integer 0 or 1
            if value is None:
                value = 1
            self.set_metric(key, value)
            return

        try:
            self._meta[key] = str(value)
            if key in self._metrics:
                del self._metrics[key]
        except Exception:
            log.warning("error setting tag %s, ignoring it", key, exc_info=True)

    def set_struct_tag(self, key: str, value: Dict[str, Any]) -> None:
        """
        Set a tag key/value pair on the span meta_struct
        Currently it will only be exported with V4 encoding
        """
        self._meta_struct[key] = value

    def get_struct_tag(self, key: str) -> Optional[Dict[str, Any]]:
        """Return the given struct or None if it doesn't exist."""
        return self._meta_struct.get(key, None)

    def set_tag_str(self, key: _TagNameType, value: Text) -> None:
        """Set a value for a tag. Values are coerced to unicode in Python 2 and
        str in Python 3, with decoding errors in conversion being replaced with
        U+FFFD.
        """
        try:
            self._meta[key] = ensure_text(value, errors="replace")
        except Exception as e:
            if config._raise:
                raise e
            log.warning("Failed to set text tag '%s'", key, exc_info=True)

    def get_tag(self, key: _TagNameType) -> Optional[Text]:
        """Return the given tag or None if it doesn't exist."""
        return self._meta.get(key, None)

    def get_tags(self) -> _MetaDictType:
        """Return all tags."""
        return self._meta.copy()

    def set_tags(self, tags: Dict[_TagNameType, Any]) -> None:
        """Set a dictionary of tags on the given span. Keys and values
        must be strings (or stringable)
        """
        if tags:
            for k, v in iter(tags.items()):
                self.set_tag(k, v)

    def set_metric(self, key: _TagNameType, value: NumericType) -> None:
        """This method sets a numeric tag value for the given key."""
        # Enforce a specific constant for `_dd.measured`
        if key == _SPAN_MEASURED_KEY:
            try:
                value = int(bool(value))
            except (ValueError, TypeError):
                log.warning("failed to convert %r tag to an integer from %r", key, value)
                return

        # FIXME[matt] we could push this check to serialization time as well.
        # only permit types that are commonly serializable (don't use
        # isinstance so that we convert unserializable types like numpy
        # numbers)
        if not isinstance(value, (int, float)):
            try:
                value = float(value)
            except (ValueError, TypeError):
                log.debug("ignoring not number metric %s:%s", key, value)
                return

        # don't allow nan or inf
        if math.isnan(value) or math.isinf(value):
            log.debug("ignoring not real metric %s:%s", key, value)
            return

        if key in self._meta:
            del self._meta[key]
        self._metrics[key] = value

    def set_metrics(self, metrics: _MetricDictType) -> None:
        """Set a dictionary of metrics on the given span. Keys must be
        must be strings (or stringable). Values must be numeric.
        """
        if metrics:
            for k, v in metrics.items():
                self.set_metric(k, v)

    def get_metric(self, key: _TagNameType) -> Optional[NumericType]:
        """Return the given metric or None if it doesn't exist."""
        return self._metrics.get(key)

    def _add_event(
        self, name: str, attributes: Optional[Dict[str, _AttributeValueType]] = None, timestamp: Optional[int] = None
    ) -> None:
        self._events.append(SpanEvent(name, attributes, timestamp))

    def _add_on_finish_exception_callback(self, callback: Callable[["Span"], None]):
        """Add an errortracking related callback to the on_finish_callback array"""
        self._on_finish_callbacks.insert(0, callback)

    def get_metrics(self) -> _MetricDictType:
        """Return all metrics."""
        return self._metrics.copy()

    def set_traceback(self, limit: Optional[int] = None):
        """If the current stack has an exception, tag the span with the
        relevant error info. If not, tag it with the current python stack.
        """
        (exc_type, exc_val, exc_tb) = sys.exc_info()

        if exc_type and exc_val and exc_tb:
            if limit:
                limit = -abs(limit)
            self.set_exc_info(exc_type, exc_val, exc_tb, limit=limit)
        else:
            if limit is None:
                limit = config._span_traceback_max_size
            tb = "".join(traceback.format_stack(limit=limit + 1)[:-1])
            self._meta[ERROR_STACK] = tb

    def _get_traceback(
        self,
        exc_type: Type[BaseException],
        exc_val: BaseException,
        exc_tb: Optional[TracebackType],
        limit: Optional[int] = None,
    ) -> str:
        """
        Return a formatted traceback as a string.
        If the traceback is too long, it will be truncated to the limit parameter,
        but from the end of the traceback (keeping the most recent frames).

        If the traceback surpasses the MAX_SPAN_META_VALUE_LEN limit, it will
        try to reduce the traceback size by half until it fits
        within this limit (limit for tag values).

        :param exc_type: the exception type
        :param exc_val: the exception value
        :param exc_tb: the exception traceback
        :param limit: the maximum number of frames to keep
        :return: the formatted traceback as a string
        """
        # If limit is None, use the default value from the configuration
        if limit is None:
            limit = config._span_traceback_max_size
        # Ensure the limit is negative for traceback.print_exception (to keep most recent frames)
        limit: int = -abs(limit)  # type: ignore[no-redef]

        # Create a buffer to hold the traceback
        buff = StringIO()
        # Print the exception traceback to the buffer with the specified limit
        traceback.print_exception(exc_type, exc_val, exc_tb, file=buff, limit=limit)
        tb = buff.getvalue()

        # Check if the traceback exceeds the maximum allowed length
        while len(tb) > MAX_SPAN_META_VALUE_LEN and abs(limit) > 1:
            # Reduce the limit by half and print the traceback again
            limit //= 2
            buff = StringIO()
            traceback.print_exception(exc_type, exc_val, exc_tb, file=buff, limit=limit)
            tb = buff.getvalue()

        return tb

    def set_exc_info(
        self,
        exc_type: Type[BaseException],
        exc_val: BaseException,
        exc_tb: Optional[TracebackType],
        limit: Optional[int] = None,
    ) -> None:
        """Tag the span with an error tuple as from `sys.exc_info()`."""
        if not (exc_type and exc_val and exc_tb):
            return  # nothing to do

        # SystemExit(0) is not an error
        if issubclass(exc_type, SystemExit) and cast(SystemExit, exc_val).code == 0:
            return

        if self._ignored_exceptions and any([issubclass(exc_type, e) for e in self._ignored_exceptions]):
            return

        self.error = 1
        tb = self._get_traceback(exc_type, exc_val, exc_tb, limit=limit)

        # readable version of type (e.g. exceptions.ZeroDivisionError)
        exc_type_str = "%s.%s" % (exc_type.__module__, exc_type.__name__)
        self._meta[ERROR_TYPE] = exc_type_str

        try:
            self._meta[ERROR_MSG] = str(exc_val)
        except Exception:
            # An exception can occur if a custom Exception overrides __str__
            # If this happens str(exc_val) won't work, so best we can do is print the class name
            # Otherwise, don't try to set an error message
            if exc_val and hasattr(exc_val, "__class__"):
                self._meta[ERROR_MSG] = exc_val.__class__.__name__

        self._meta[ERROR_STACK] = tb

        # some web integrations like bottle rely on set_exc_info to get the error tags, so we need to dispatch
        # this event such that the additional tags for inferred aws api gateway spans can be appended here.
        core.dispatch("web.request.final_tags", (self,))

        core.dispatch("span.exception", (self, exc_type, exc_val, exc_tb))

    def record_exception(
        self,
        exception: BaseException,
        attributes: Optional[Dict[str, _AttributeValueType]] = None,
        timestamp: Optional[int] = None,
        escaped: bool = False,
    ) -> None:
        """
        Records an exception as a span event. Multiple exceptions can be recorded on a span.

        :param exception: The exception to record.
        :param attributes: Optional dictionary of additional attributes to add to the exception event.
            These attributes will override the default exception attributes if they contain the same keys.
            Valid attribute values include (homogeneous array of) strings, booleans, integers, floats.
        :param timestamp: Deprecated.
        :param escaped: Deprecated.
        """
        if escaped:
            deprecate(
                prefix="The escaped argument is deprecated for record_exception",
                message="""If an exception exits the scope of the span, it will automatically be
                reported in the span tags.""",
                category=DDTraceDeprecationWarning,
                removal_version="4.0.0",
            )
        if timestamp is not None:
            deprecate(
                prefix="The timestamp argument is deprecated for record_exception",
                message="""The timestamp of the span event should correspond to the time when the
                error is recorded which is set automatically.""",
                category=DDTraceDeprecationWarning,
                removal_version="4.0.0",
            )

        tb = self._get_traceback(type(exception), exception, exception.__traceback__)

        attrs: Dict[str, _AttributeValueType] = {
            "exception.type": "%s.%s" % (exception.__class__.__module__, exception.__class__.__name__),
            "exception.message": str(exception),
            "exception.stacktrace": tb,
        }
        if attributes:
            attributes = {k: v for k, v in attributes.items() if self._validate_attribute(k, v)}

            # User provided attributes must take precedence over attrs
            attrs.update(attributes)

        self._add_event(name="exception", attributes=attrs, timestamp=time_ns())

    def _validate_attribute(self, key: str, value: object) -> bool:
        if isinstance(value, (str, bool, int, float)):
            return self._validate_scalar(key, value)

        if not isinstance(value, list):
            log.warning("record_exception: Attribute %s must be a string, number, or boolean: %s.", key, value)
            return False

        if len(value) == 0:
            return True

        if not isinstance(value[0], (str, bool, int, float)):
            log.warning("record_exception: List values %s must be string, number, or boolean: %s.", key, value)
            return False

        first_type = type(value[0])
        for val in value:
            if not isinstance(val, first_type) or not self._validate_scalar(key, val):
                log.warning("record_exception: Attribute %s array must be homogeneous: %s.", key, value)
                return False
        return True

    def _validate_scalar(self, key: str, value: Union[bool, str, int, float]) -> bool:
        if isinstance(value, (bool, str)):
            return True

        if isinstance(value, int):
            if value < _MIN_INT_64BITS or value > _MAX_INT_64BITS:
                log.warning(
                    "record_exception: Attribute %s must be within the range of a signed 64-bit integer: %s.",
                    key,
                    value,
                )
                return False
            return True

        if isinstance(value, float):
            if not math.isfinite(value):
                log.warning("record_exception: Attribute %s must be a finite number: %s.", key, value)
                return False
            return True

        return False

    def _pprint(self) -> str:
        """Return a human readable version of the span."""
        data = [
            ("name", self.name),
            ("id", self.span_id),
            ("trace_id", self.trace_id),
            ("parent_id", self.parent_id),
            ("service", self.service),
            ("resource", self.resource),
            ("type", self.span_type),
            ("start", self.start),
            ("end", None if not self.duration else self.start + self.duration),
            ("duration", self.duration),
            ("error", self.error),
            ("tags", dict(sorted(self._meta.items()))),
            ("metrics", dict(sorted(self._metrics.items()))),
            ("links", ", ".join([str(link) for link in self._links])),
            ("events", ", ".join([str(e) for e in self._events])),
        ]
        return " ".join(
            # use a large column width to keep pprint output on one line
            "%s=%s" % (k, pprint.pformat(v, width=1024**2).strip())
            for (k, v) in data
        )

    @property
    def context(self) -> Context:
        """Return the trace context for this span."""
        if self._context is None:
            self._context = Context(trace_id=self.trace_id, span_id=self.span_id, is_remote=False)
        return self._context

    @property
    def _local_root(self) -> "Span":
        if self._local_root_value is None:
            return self
        return self._local_root_value

    @_local_root.setter
    def _local_root(self, value: "Span") -> None:
        if value is not self:
            self._local_root_value = value
        else:
            self._local_root_value = None

    @_local_root.deleter
    def _local_root(self) -> None:
        del self._local_root_value

    def link_span(self, context: Context, attributes: Optional[Dict[str, Any]] = None) -> None:
        """Defines a causal relationship between two spans"""
        if not context.trace_id or not context.span_id:
            msg = f"Invalid span or trace id. trace_id:{context.trace_id} span_id:{context.span_id}"
            if config._raise:
                raise ValueError(msg)
            else:
                log.warning(msg)

        if context.trace_id and context.span_id:
            self.set_link(
                trace_id=context.trace_id,
                span_id=context.span_id,
                tracestate=context._tracestate,
                flags=int(context._traceflags),
                attributes=attributes,
            )

    def set_link(
        self,
        trace_id: int,
        span_id: int,
        tracestate: Optional[str] = None,
        flags: Optional[int] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        if attributes is None:
            attributes = dict()

        self._set_link_or_append_pointer(
            SpanLink(
                trace_id=trace_id,
                span_id=span_id,
                tracestate=tracestate,
                flags=flags,
                attributes=attributes,
            )
        )

    def _add_span_pointer(
        self,
        pointer_kind: str,
        pointer_direction: _SpanPointerDirection,
        pointer_hash: str,
        extra_attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        # This is a Private API for now.

        self._set_link_or_append_pointer(
            _SpanPointer(
                pointer_kind=pointer_kind,
                pointer_direction=pointer_direction,
                pointer_hash=pointer_hash,
                extra_attributes=extra_attributes,
            )
        )

    def _set_link_or_append_pointer(self, link: Union[SpanLink, _SpanPointer]) -> None:
        if link.kind == SpanLinkKind.SPAN_POINTER.value:
            self._links.append(link)
            return

        try:
            existing_link_idx_with_same_span_id = [link.span_id for link in self._links].index(link.span_id)

            log.debug(
                "Span %d already linked to span %d. Overwriting existing link: %s",
                self.span_id,
                link.span_id,
                str(self._links[existing_link_idx_with_same_span_id]),
            )

            self._links[existing_link_idx_with_same_span_id] = link

        except ValueError:
            self._links.append(link)

    def finish_with_ancestors(self) -> None:
        """Finish this span along with all (accessible) ancestors of this span.

        This method is useful if a sudden program shutdown is required and finishing
        the trace is desired.
        """
        span: Optional["Span"] = self
        while span is not None:
            span.finish()
            span = span._parent

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type: Type[BaseException], exc_val: BaseException, exc_tb: Optional[TracebackType]) -> None:
        try:
            if exc_type:
                self.set_exc_info(exc_type, exc_val, exc_tb)
            self.finish()
        except Exception:
            log.exception("error closing trace")

    def __repr__(self) -> str:
        return "<Span(id=%s,trace_id=%s,parent_id=%s,name=%s)>" % (
            self.span_id,
            self.trace_id,
            self.parent_id,
            self.name,
        )

    @property
    def _is_top_level(self) -> bool:
        """Return whether the span is a "top level" span.

        Top level meaning the root of the trace or a child span
        whose service is different from its parent.
        """
        return (self._local_root is self) or (
            self._parent is not None and self._parent.service != self.service and self.service is not None
        )
