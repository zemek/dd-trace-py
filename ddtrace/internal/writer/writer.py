import abc
import binascii
from collections import defaultdict
import gzip
import logging
import os
import sys
import threading
from typing import TYPE_CHECKING
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import TextIO

import ddtrace
from ddtrace import config
import ddtrace.internal.utils.http
from ddtrace.internal.utils.retry import fibonacci_backoff_with_jitter
from ddtrace.settings._agent import config as agent_config
from ddtrace.settings.asm import config as asm_config

from ...constants import _KEEP_SPANS_RATE_KEY
from ...internal.utils.formats import parse_tags_str
from ...internal.utils.http import Response
from ...internal.utils.time import StopWatch
from .. import compat
from .. import periodic
from .. import service
from .._encoding import BufferFull
from .._encoding import BufferItemTooLarge
from ..agent import get_connection
from ..constants import _HTTPLIB_NO_TRACE_REQUEST
from ..encoding import JSONEncoderV2
from ..logger import get_logger
from ..serverless import in_azure_function
from ..serverless import in_gcp_function
from ..sma import SimpleMovingAverage
from .writer_client import WRITER_CLIENTS
from .writer_client import AgentWriterClientV4
from .writer_client import WriterClientBase


if TYPE_CHECKING:  # pragma: no cover
    from typing import Any  # noqa:F401
    from typing import Tuple  # noqa:F401

    from ddtrace.trace import Span  # noqa:F401
    from ddtrace.vendor.dogstatsd import DogStatsd

    from .utils.http import ConnectionType  # noqa:F401


log = get_logger(__name__)

LOG_ERR_INTERVAL = 60


class NoEncodableSpansError(Exception):
    pass


# The window size should be chosen so that the look-back period is
# greater-equal to the agent API's timeout. Although most tracers have a
# 2s timeout, the java tracer has a 10s timeout, so we set the window size
# to 10 buckets of 1s duration.
DEFAULT_SMA_WINDOW = 10


def _human_size(nbytes: float) -> str:
    """Return a human-readable size."""
    i = 0
    suffixes = ["B", "KB", "MB", "GB", "TB"]
    while nbytes >= 1000 and i < len(suffixes) - 1:
        nbytes /= 1000.0
        i += 1
    f = ("%.2f" % nbytes).rstrip("0").rstrip(".")
    return "%s%s" % (f, suffixes[i])


class TraceWriter(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def recreate(self):
        # type: () -> TraceWriter
        pass

    @abc.abstractmethod
    def stop(self, timeout: Optional[float] = None) -> None:
        pass

    @abc.abstractmethod
    def write(self, spans=None):
        # type: (Optional[List[Span]]) -> None
        pass

    @abc.abstractmethod
    def flush_queue(self) -> None:
        pass


class LogWriter(TraceWriter):
    def __init__(
        self,
        out: TextIO = sys.stdout,
    ) -> None:
        self.encoder = JSONEncoderV2()
        self.out = out

    def recreate(self):
        # type: () -> LogWriter
        """Create a new instance of :class:`LogWriter` using the same settings from this instance

        :rtype: :class:`LogWriter`
        :returns: A new :class:`LogWriter` instance
        """
        writer = self.__class__(out=self.out)
        return writer

    def stop(self, timeout: Optional[float] = None) -> None:
        return

    def write(self, spans=None):
        # type: (Optional[List[Span]]) -> None
        if not spans:
            return
        encoded = self.encoder.encode_traces([spans])
        self.out.write(encoded + "\n")
        self.out.flush()

    def flush_queue(self) -> None:
        pass


class HTTPWriter(periodic.PeriodicService, TraceWriter):
    """Writer to an arbitrary HTTP intake endpoint."""

    intake_url: str

    RETRY_ATTEMPTS = 3
    HTTP_METHOD = "PUT"
    STATSD_NAMESPACE = "tracer"

    def __init__(
        self,
        intake_url: str,
        clients: List[WriterClientBase],
        processing_interval: Optional[float] = None,
        # Match the payload size since there is no functionality
        # to flush dynamically.
        buffer_size: Optional[int] = None,
        max_payload_size: Optional[int] = None,
        timeout: Optional[float] = None,
        dogstatsd: Optional["DogStatsd"] = None,
        sync_mode: bool = False,
        reuse_connections: Optional[bool] = None,
        headers: Optional[Dict[str, str]] = None,
        report_metrics: bool = True,
        use_gzip: bool = False,
    ) -> None:
        if processing_interval is None:
            processing_interval = config._trace_writer_interval_seconds
        if timeout is None:
            timeout = agent_config.trace_agent_timeout_seconds
        super(HTTPWriter, self).__init__(interval=processing_interval)
        self.intake_url = intake_url
        self._intake_accepts_gzip = use_gzip
        self._buffer_size = buffer_size
        self._max_payload_size = max_payload_size
        self._headers = headers or {}
        self._timeout = timeout

        self._clients = clients
        self.dogstatsd = dogstatsd
        self._metrics: Dict[str, int] = defaultdict(int)
        self._report_metrics = report_metrics
        self._drop_sma = SimpleMovingAverage(DEFAULT_SMA_WINDOW)
        self._sync_mode = sync_mode
        self._conn: Optional[ConnectionType] = None
        # The connection has to be locked since there exists a race between
        # the periodic thread of HTTPWriter and other threads that might
        # force a flush with `flush_queue()`.
        self._conn_lck: threading.RLock = threading.RLock()

        self._send_payload_with_backoff = fibonacci_backoff_with_jitter(  # type ignore[assignment]
            attempts=self.RETRY_ATTEMPTS,
            initial_wait=0.618 * self.interval / (1.618**self.RETRY_ATTEMPTS) / 2,
            until=lambda result: isinstance(result, Response),
        )(self._send_payload)

        self._reuse_connections = (
            config._trace_writer_connection_reuse if reuse_connections is None else reuse_connections
        )

    def _intake_endpoint(self, client=None):
        return "{}/{}".format(self._intake_url(client), client.ENDPOINT if client else self._endpoint)

    @property
    def _endpoint(self):
        return self._clients[0].ENDPOINT

    @property
    def _encoder(self):
        return self._clients[0].encoder

    def _intake_url(self, client=None):
        if client and hasattr(client, "_intake_url"):
            return client._intake_url
        return self.intake_url

    def _metrics_dist(self, name: str, count: int = 1, tags: Optional[List] = None) -> None:
        if not self._report_metrics:
            return
        if config._health_metrics_enabled and self.dogstatsd:
            self.dogstatsd.distribution("datadog.%s.%s" % (self.STATSD_NAMESPACE, name), count, tags=tags)

    def _set_drop_rate(self) -> None:
        accepted = self._metrics["accepted_traces"]
        sent = self._metrics["sent_traces"]
        encoded = sum([len(client.encoder) for client in self._clients])
        # The number of dropped traces is the number of accepted traces minus the number of traces in the encoder
        # This calculation is a best effort. Due to race conditions it may result in a slight underestimate.
        dropped = max(accepted - sent - encoded, 0)  # dropped spans should never be negative
        self._drop_sma.set(dropped, accepted)
        self._metrics["sent_traces"] = 0  # reset sent traces for the next interval
        self._metrics["accepted_traces"] = encoded  # sets accepted traces to number of spans in encoders

    def _set_keep_rate(self, trace):
        if trace:
            trace[0].set_metric(_KEEP_SPANS_RATE_KEY, 1.0 - self._drop_sma.get())

    def _reset_connection(self) -> None:
        with self._conn_lck:
            if self._conn:
                self._conn.close()
                self._conn = None

    def _put(self, data: bytes, headers: Dict[str, str], client: WriterClientBase, no_trace: bool) -> Response:
        sw = StopWatch()
        sw.start()
        with self._conn_lck:
            if self._conn is None:
                log.debug("creating new intake connection to %s with timeout %d", self.intake_url, self._timeout)
                self._conn = get_connection(self._intake_url(client), self._timeout)
                setattr(self._conn, _HTTPLIB_NO_TRACE_REQUEST, no_trace)
            try:
                log.debug("Sending request: %s %s %s", self.HTTP_METHOD, client.ENDPOINT, headers)
                self._conn.request(
                    self.HTTP_METHOD,
                    client.ENDPOINT,
                    data,
                    headers,
                )
                resp = self._conn.getresponse()
                log.debug("Got response: %s %s", resp.status, resp.reason)
                t = sw.elapsed()
                if t >= self.interval:
                    log_level = logging.WARNING
                else:
                    log_level = logging.DEBUG
                log.log(log_level, "sent %s in %.5fs to %s", _human_size(len(data)), t, self._intake_endpoint(client))
            except Exception:
                # Always reset the connection when an exception occurs
                self._reset_connection()
                raise
            else:
                return Response.from_http_response(resp)
            finally:
                # Reset the connection if reusing connections is disabled.
                if not self._reuse_connections:
                    self._reset_connection()

    def _get_finalized_headers(self, count: int, client: WriterClientBase) -> Dict[str, str]:
        headers = self._headers.copy()
        headers.update({"Content-Type": client.encoder.content_type})  # type: ignore[attr-defined]
        if hasattr(client, "_headers"):
            headers.update(client._headers)
        return headers

    def _send_payload(self, payload: bytes, count: int, client: WriterClientBase) -> Response:
        headers = self._get_finalized_headers(count, client)

        self._metrics_dist("http.requests")

        response = self._put(payload, headers, client, no_trace=True)

        if response.status >= 400:
            self._metrics_dist("http.errors", tags=["type:%s" % response.status])
        else:
            self._metrics_dist("http.sent.bytes", len(payload))
            self._metrics["sent_traces"] += count

        if response.status not in (404, 415) and response.status >= 400:
            msg = "failed to send traces to intake at %s: HTTP error status %s, reason %s"
            log_args = (
                self._intake_endpoint(client),
                response.status,
                response.reason,
            )  # type: Tuple[Any, Any, Any]
            # Append the payload if requested
            if config._trace_writer_log_err_payload:
                msg += ", payload %s"
                # If the payload is bytes then hex encode the value before logging
                if isinstance(payload, bytes):
                    log_args += (binascii.hexlify(payload).decode(),)  # type: ignore
                else:
                    log_args += (payload,)

            log.error(msg, *log_args)
            self._metrics_dist("http.dropped.bytes", len(payload))
            self._metrics_dist("http.dropped.traces", count)
        return response

    def write(self, spans=None):
        for client in self._clients:
            self._write_with_client(client, spans=spans)
        if self._sync_mode:
            self.flush_queue()

    def _write_with_client(self, client, spans=None):
        # type: (WriterClientBase, Optional[List[Span]]) -> None
        if spans is None:
            return

        if self._sync_mode is False:
            # Start the HTTPWriter on first write.
            try:
                if self.status != service.ServiceStatus.RUNNING:
                    self.start()

            except service.ServiceStatusError:
                pass

        self._metrics_dist("writer.accepted.traces")
        self._metrics["accepted_traces"] += 1
        self._set_keep_rate(spans)

        try:
            client.encoder.put(spans)
        except BufferItemTooLarge as e:
            payload_size = e.args[0]
            log.warning(
                "trace (%db) larger than payload buffer item limit (%db), dropping",
                payload_size,
                client.encoder.max_item_size,
            )
            self._metrics_dist("buffer.dropped.traces", 1, tags=["reason:t_too_big"])
            self._metrics_dist("buffer.dropped.bytes", payload_size, tags=["reason:t_too_big"])
        except BufferFull as e:
            payload_size = e.args[0]
            log.warning(
                "trace buffer (%s traces %db/%db) cannot fit trace of size %db, dropping (writer status: %s)",
                len(client.encoder),
                client.encoder.size,
                client.encoder.max_size,
                payload_size,
                self.status.value,
            )
            self._metrics_dist("buffer.dropped.traces", 1, tags=["reason:full"])
            self._metrics_dist("buffer.dropped.bytes", payload_size, tags=["reason:full"])
        except NoEncodableSpansError:
            self._metrics_dist("buffer.dropped.traces", 1, tags=["reason:incompatible"])
        else:
            self._metrics_dist("buffer.accepted.traces", 1)
            self._metrics_dist("buffer.accepted.spans", len(spans))

    def flush_queue(self, raise_exc: bool = False):
        try:
            for client in self._clients:
                self._flush_queue_with_client(client, raise_exc=raise_exc)
        finally:
            self._set_drop_rate()

    def _flush_queue_with_client(self, client: WriterClientBase, raise_exc: bool = False) -> None:
        n_traces = len(client.encoder)
        try:
            encoded, n_traces = client.encoder.encode()

            if encoded is None:
                return

            # Should gzip the payload if intake accepts it
            if self._intake_accepts_gzip:
                original_size = len(encoded)
                # Replace the value to send with the gzipped the value
                encoded = gzip.compress(encoded, compresslevel=6)
                log.debug("Original size in bytes: %s, Compressed size: %s", original_size, len(encoded))

                # And add the header
                self._headers["Content-Encoding"] = "gzip"

        except Exception:
            # FIXME(munir): if client.encoder raises an Exception n_traces may not be accurate due to race conditions
            log.error("failed to encode trace with encoder %r", client.encoder, exc_info=True)
            self._metrics_dist("encoder.dropped.traces", n_traces)
            return

        try:
            self._send_payload_with_backoff(encoded, n_traces, client)
        except Exception:
            self._metrics_dist("http.errors", tags=["type:err"])
            self._metrics_dist("http.dropped.bytes", len(encoded))
            self._metrics_dist("http.dropped.traces", n_traces)
            if raise_exc:
                raise
            else:
                log.error(
                    "failed to send, dropping %d traces to intake at %s after %d retries",
                    n_traces,
                    self._intake_endpoint(client),
                    self.RETRY_ATTEMPTS,
                )
        finally:
            self._metrics_dist("http.sent.bytes", len(encoded))
            self._metrics_dist("http.sent.traces", n_traces)

    def periodic(self):
        self.flush_queue(raise_exc=False)

    def _stop_service(
        self,
        timeout: Optional[float] = None,
    ) -> None:
        # FIXME: don't join() on stop(), let the caller handle this
        super(HTTPWriter, self)._stop_service()
        self.join(timeout=timeout)

    def on_shutdown(self):
        try:
            self.periodic()
        finally:
            self._reset_connection()


class AgentResponse(object):
    def __init__(self, rate_by_service: Dict[str, float]) -> None:
        self.rate_by_service = rate_by_service


class AgentWriterInterface(metaclass=abc.ABCMeta):
    intake_url: str
    _api_version: str
    _sync_mode: bool

    @abc.abstractmethod
    def set_test_session_token(self, token: Optional[str]) -> None:
        pass

    @abc.abstractmethod
    def before_fork(self) -> None:
        pass

    @abc.abstractmethod
    def flush_queue(self, raise_exc: bool = False) -> None:
        pass


class AgentWriter(HTTPWriter, AgentWriterInterface):
    """
    The Datadog Agent supports (at the time of writing this) receiving trace
    payloads up to 50MB. A trace payload is just a list of traces and the agent
    expects a trace to be complete. That is, all spans with the same trace_id
    should be in the same trace.
    """

    RETRY_ATTEMPTS = 3
    HTTP_METHOD = "PUT"
    STATSD_NAMESPACE = "tracer"

    def __init__(
        self,
        intake_url: str,
        processing_interval: Optional[float] = None,
        # Match the payload size since there is no functionality
        # to flush dynamically.
        buffer_size: Optional[int] = None,
        max_payload_size: Optional[int] = None,
        timeout: Optional[float] = None,
        dogstatsd: Optional["DogStatsd"] = None,
        report_metrics: bool = True,
        sync_mode: bool = False,
        api_version: Optional[str] = None,
        reuse_connections: Optional[bool] = None,
        headers: Optional[Dict[str, str]] = None,
        response_callback: Optional[Callable[[AgentResponse], None]] = None,
    ) -> None:
        if processing_interval is None:
            processing_interval = config._trace_writer_interval_seconds
        if timeout is None:
            timeout = agent_config.trace_agent_timeout_seconds
        if buffer_size is not None and buffer_size <= 0:
            raise ValueError("Writer buffer size must be positive")
        if max_payload_size is not None and max_payload_size <= 0:
            raise ValueError("Max payload size must be positive")
        # Default to v0.4 if we are on Windows since there is a known compatibility issue
        # https://github.com/DataDog/dd-trace-py/issues/4829
        # DEV: sys.platform on windows should be `win32` or `cygwin`, but using `startswith`
        #      as a safety precaution.
        #      https://docs.python.org/3/library/sys.html#sys.platform
        is_windows = sys.platform.startswith("win") or sys.platform.startswith("cygwin")

        default_api_version = "v0.5"
        if (
            is_windows
            or in_gcp_function()
            or in_azure_function()
            or asm_config._asm_enabled
            or asm_config._iast_enabled
        ):
            default_api_version = "v0.4"

        self._api_version = api_version or config._trace_api or default_api_version

        if agent_config.trace_native_span_events:
            log.warning("Setting api version to v0.4; DD_TRACE_NATIVE_SPAN_EVENTS is not compatible with v0.5")
            self._api_version = "v0.4"

        if is_windows and self._api_version == "v0.5":
            raise RuntimeError(
                "There is a known compatibility issue with v0.5 API and Windows, "
                "please see https://github.com/DataDog/dd-trace-py/issues/4829 for more details."
            )

        buffer_size = buffer_size or config._trace_writer_buffer_size
        max_payload_size = max_payload_size or config._trace_writer_payload_size
        if self._api_version not in WRITER_CLIENTS:
            log.warning(
                "Unsupported api version: '%s'. The supported versions are: %r",
                self._api_version,
                ", ".join(sorted(WRITER_CLIENTS.keys())),
            )
            self._api_version = sorted(WRITER_CLIENTS.keys())[-1]
        client = WRITER_CLIENTS[self._api_version](buffer_size, max_payload_size)

        _headers = {
            "Datadog-Meta-Lang": "python",
            "Datadog-Meta-Lang-Version": compat.PYTHON_VERSION,
            "Datadog-Meta-Lang-Interpreter": compat.PYTHON_INTERPRETER,
            "Datadog-Meta-Tracer-Version": ddtrace.__version__,
            "Datadog-Client-Computed-Top-Level": "yes",
        }
        if headers:
            _headers.update(headers)

        _headers.update({"Content-Type": client.encoder.content_type})  # type: ignore[attr-defined]
        additional_header_str = os.environ.get("_DD_TRACE_WRITER_ADDITIONAL_HEADERS")
        if additional_header_str is not None:
            _headers.update(parse_tags_str(additional_header_str))
        self._response_cb = response_callback
        self._report_metrics = report_metrics
        super(AgentWriter, self).__init__(
            intake_url=intake_url,
            clients=[client],
            processing_interval=processing_interval,
            buffer_size=buffer_size,
            max_payload_size=max_payload_size,
            timeout=timeout,
            dogstatsd=dogstatsd,
            sync_mode=sync_mode,
            reuse_connections=reuse_connections,
            headers=_headers,
            report_metrics=report_metrics,
        )

    def recreate(self) -> HTTPWriter:
        new_instance = self.__class__(
            intake_url=self.intake_url,
            processing_interval=self._interval,
            buffer_size=self._buffer_size,
            max_payload_size=self._max_payload_size,
            timeout=self._timeout,
            dogstatsd=self.dogstatsd,
            sync_mode=self._sync_mode,
            api_version=self._api_version,
            headers=self._headers,
            report_metrics=self._report_metrics,
            response_callback=self._response_cb,
        )
        return new_instance

    @property
    def _agent_endpoint(self):
        return self._intake_endpoint(client=None)

    def _downgrade(self, response, client):
        if client.ENDPOINT == "v0.5/traces":
            self._clients = [AgentWriterClientV4(self._buffer_size, self._max_payload_size)]
            # Since we have to change the encoding in this case, the payload
            # would need to be converted to the downgraded encoding before
            # sending it, but we chuck it away instead.
            log.warning(
                "Calling endpoint '%s' but received %s; downgrading API. "
                "Dropping trace payload due to the downgrade to an incompatible API version (from v0.5 to v0.4). To "
                "avoid this from happening in the future, either ensure that the Datadog agent has a v0.5/traces "
                "endpoint available, or explicitly set the trace API version to, e.g., v0.4.",
                client.ENDPOINT,
                response.status,
            )
        else:
            log.error(
                "unsupported endpoint '%s': received response %s from intake (%s)",
                client.ENDPOINT,
                response.status,
                self.intake_url,
            )

    def _send_payload(self, payload, count, client) -> Response:
        response = super(AgentWriter, self)._send_payload(payload, count, client)
        if response.status in [404, 415]:
            self._downgrade(response, client)
        elif response.status < 400:
            if self._response_cb:
                raw_resp = response.get_json()
                if raw_resp and "rate_by_service" in raw_resp:
                    self._response_cb(
                        AgentResponse(
                            rate_by_service=raw_resp["rate_by_service"],
                        )
                    )
        return response

    def start(self):
        super(AgentWriter, self).start()
        try:
            # appsec remote config should be enabled/started after the global tracer and configs
            # are initialized
            if asm_config._asm_rc_enabled:
                from ddtrace.appsec._remoteconfiguration import enable_appsec_rc

                enable_appsec_rc()
        except service.ServiceStatusError:
            pass

    def _get_finalized_headers(self, count: int, client: WriterClientBase) -> Dict[str, str]:
        headers = super(AgentWriter, self)._get_finalized_headers(count, client)
        headers["X-Datadog-Trace-Count"] = str(count)
        return headers

    def before_fork(self) -> None:
        pass

    def set_test_session_token(self, token: Optional[str]) -> None:
        self._headers["X-Datadog-Test-Session-Token"] = token or ""
