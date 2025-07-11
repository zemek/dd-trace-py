from collections import deque
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from threading import current_thread
from types import FrameType
from types import TracebackType
import typing as t
import uuid

from ddtrace.debugging._probe.model import LiteralTemplateSegment
from ddtrace.debugging._probe.model import LogLineProbe
from ddtrace.debugging._session import Session
from ddtrace.debugging._signal.snapshot import DEFAULT_CAPTURE_LIMITS
from ddtrace.debugging._signal.snapshot import Snapshot
from ddtrace.debugging._uploader import LogsIntakeUploaderV1
from ddtrace.debugging._uploader import UploaderProduct
from ddtrace.internal import core
from ddtrace.internal.logger import get_logger
from ddtrace.internal.packages import is_user_code
from ddtrace.internal.rate_limiter import BudgetRateLimiterWithJitter as RateLimiter
from ddtrace.internal.rate_limiter import RateLimitExceeded
from ddtrace.internal.utils.time import HourGlass
from ddtrace.settings.exception_replay import config
from ddtrace.trace import Span


log = get_logger(__name__)

GLOBAL_RATE_LIMITER = RateLimiter(
    limit_rate=1,  # one trace per second
    raise_on_exceed=False,
)

# used to store a snapshot on the frame locals
SNAPSHOT_KEY = "_dd_exception_replay_snapshot_id"

# used to mark that the span have debug info captured, visible to users
DEBUG_INFO_TAG = "error.debug_info_captured"

# used to rate limit decision on the entire local trace (stored at the root span)
CAPTURE_TRACE_TAG = "_dd.debug.error.trace_captured"
SNAPSHOT_COUNT_TAG = "_dd.debug.error.snapshot_count"

# unique exception id
EXCEPTION_HASH_TAG = "_dd.debug.error.exception_hash"
EXCEPTION_ID_TAG = "_dd.debug.error.exception_capture_id"

# link to matching snapshot for every frame in the traceback
FRAME_SNAPSHOT_ID_TAG = "_dd.debug.error.%d.snapshot_id"
FRAME_FUNCTION_TAG = "_dd.debug.error.%d.function"
FRAME_FILE_TAG = "_dd.debug.error.%d.file"
FRAME_LINE_TAG = "_dd.debug.error.%d.line"

EXCEPTION_IDENT_LIMIT = 3600.0  # 1 hour
EXCEPTION_IDENT_LIMITER: t.Dict[int, HourGlass] = {}


ExceptionChain = t.Deque[t.Tuple[BaseException, t.Optional[TracebackType]]]


def exception_ident(exc: BaseException, tb: t.Optional[TracebackType]) -> int:
    """Compute the identity of an exception.

    We use the exception type and the traceback to generate a unique identifier
    that we can use to identify the exception. This can be used to rate limit
    the number of times we capture information of the same exception.
    """
    h = 0
    _tb = tb
    while _tb is not None:
        frame = _tb.tb_frame
        h = (h << 1) ^ (id(frame.f_code) << 4 | frame.f_lasti)
        h &= 0xFFFFFFFFFFFFFFFF
        _tb = _tb.tb_next
    return (id(type(exc)) << 64) | h


def exception_chain_ident(chain: ExceptionChain) -> int:
    h = 0
    for exc, tb in chain:
        h = (h << 1) ^ exception_ident(exc, tb)
        h &= 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
    return h


def limit_exception(exc_ident: int) -> bool:
    try:
        hg = EXCEPTION_IDENT_LIMITER.get(exc_ident)
        if hg is None:
            # We haven't seen this exception yet, or it's been evicted
            hg = EXCEPTION_IDENT_LIMITER[exc_ident] = HourGlass(duration=EXCEPTION_IDENT_LIMIT)
            hg.turn()
            return False

        if hg.trickling():
            # We have seen this exception recently
            return True

        # We haven't seen this exception in a while
        hg.turn()

        return False
    finally:
        if len(EXCEPTION_IDENT_LIMITER) > 1024:
            # We limit the number of exception identities we track to avoid
            # memory leaks.
            sorted_keys = sorted(EXCEPTION_IDENT_LIMITER, key=lambda k: EXCEPTION_IDENT_LIMITER[k])
            for k in sorted_keys[:256]:
                del EXCEPTION_IDENT_LIMITER[k]


def unwind_exception_chain(
    exc: t.Optional[BaseException], tb: t.Optional[TracebackType]
) -> t.Tuple[ExceptionChain, t.Optional[uuid.UUID]]:
    """Unwind the exception chain and assign it an ID."""
    chain: ExceptionChain = deque()

    while exc is not None:
        chain.append((exc, tb))

        if exc.__cause__ is not None:
            exc = exc.__cause__
        elif exc.__context__ is not None and not exc.__suppress_context__:
            exc = exc.__context__
        else:
            exc = None

        tb = getattr(exc, "__traceback__", None)

    exc_id = None
    if chain:
        # If the chain is not trivial we generate an ID for the whole chain and
        # store it on the outermost exception, if not already generated.
        exc, _ = chain[-1]
        try:
            exc_id = exc._dd_exc_id  # type: ignore[attr-defined]
        except AttributeError:
            exc._dd_exc_id = exc_id = uuid.uuid4()  # type: ignore[attr-defined]

    return chain, exc_id


class SpanExceptionProbe(LogLineProbe):
    @classmethod
    def build(cls, exc_id: uuid.UUID, frame: FrameType) -> "SpanExceptionProbe":
        _exc_id = str(exc_id)
        filename = frame.f_code.co_filename
        line = frame.f_lineno
        name = frame.f_code.co_name
        message = f"exception info for {name}, in {filename}, line {line} (exception ID {_exc_id})"

        return cls(
            probe_id=_exc_id,
            version=0,
            tags={},
            source_file=filename,
            line=line,
            template=message,
            segments=[LiteralTemplateSegment(message)],
            take_snapshot=True,
            limits=DEFAULT_CAPTURE_LIMITS,
            condition=None,
            condition_error_rate=0.0,
            rate=float("inf"),
        )


@dataclass
class SpanExceptionSnapshot(Snapshot):
    exc_id: t.Optional[uuid.UUID] = None

    @property
    def data(self) -> t.Dict[str, t.Any]:
        data = super().data
        data.update({"exceptionId": str(self.exc_id)})
        return data


def can_capture(span: Span) -> bool:
    # We determine if we should capture the exception information from the span
    # by looking at its local root. If we have budget to capture, we mark the
    # root as "info captured" and return True. If we don't have budget, we mark
    # the root as "info not captured" and return False. If the root is already
    # marked, we return the mark.
    root = span._local_root
    if root is None:
        return False

    info_captured = root.get_tag(CAPTURE_TRACE_TAG)

    if info_captured == "false":
        return False

    if info_captured == "true":
        return True

    if info_captured is None:
        if Session.from_trace():
            # If we are in a debug session we always capture
            return True
        result = GLOBAL_RATE_LIMITER.limit() is not RateLimitExceeded
        root.set_tag_str(CAPTURE_TRACE_TAG, str(result).lower())
        return result

    msg = f"unexpected value for {CAPTURE_TRACE_TAG}: {info_captured}"
    raise ValueError(msg)


def get_snapshot_count(span: Span) -> int:
    root = span._local_root
    if root is None:
        return 0

    count = root.get_metric(SNAPSHOT_COUNT_TAG)
    if count is None:
        return 0

    return int(count)


class SpanExceptionHandler:
    __uploader__ = LogsIntakeUploaderV1

    _instance: t.Optional["SpanExceptionHandler"] = None

    def _capture_tb_frame_for_span(
        self, span: Span, tb: TracebackType, exc_id: uuid.UUID, seq_nr: int = 1, only_user_code: bool = True
    ) -> bool:
        frame = tb.tb_frame
        code = frame.f_code
        if only_user_code and not is_user_code(Path(code.co_filename)):
            return False

        snapshot = None
        snapshot_id = frame.f_locals.get(SNAPSHOT_KEY, None)
        if snapshot_id is None:
            # We don't have a snapshot for the frame so we create one
            snapshot = SpanExceptionSnapshot(
                probe=SpanExceptionProbe.build(exc_id, frame),
                frame=frame,
                thread=current_thread(),
                trace_context=span,
                exc_id=exc_id,
            )

            # Capture
            try:
                snapshot.do_line()
            except Exception:
                log.exception("Error capturing exception replay snapshot %r", snapshot)
                return False

            # Collect
            self.__uploader__.get_collector().push(snapshot)

            # Memoize
            frame.f_locals[SNAPSHOT_KEY] = snapshot_id = snapshot.uuid

        # Add correlation tags on the span
        span.set_tag_str(FRAME_SNAPSHOT_ID_TAG % seq_nr, snapshot_id)
        span.set_tag_str(FRAME_FUNCTION_TAG % seq_nr, code.co_name)
        span.set_tag_str(FRAME_FILE_TAG % seq_nr, code.co_filename)
        span.set_tag_str(FRAME_LINE_TAG % seq_nr, str(tb.tb_lineno))

        return snapshot is not None

    def on_span_exception(
        self, span: Span, _exc_type: t.Type[BaseException], exc: BaseException, tb: t.Optional[TracebackType]
    ) -> None:
        if span.get_tag(DEBUG_INFO_TAG) == "true" or not can_capture(span):
            # Debug info for span already captured or no budget to capture
            return

        chain, exc_id = unwind_exception_chain(exc, tb)
        if not chain or exc_id is None:
            # No exceptions to capture
            return

        exc_ident = exception_chain_ident(chain)
        if limit_exception(exc_ident):
            # We have seen this exception recently
            return

        seq = count(1)  # 1-based sequence number

        frames_captured = get_snapshot_count(span)

        while chain and frames_captured < config.max_frames:
            exc, _tb = chain.pop()  # LIFO: reverse the chain

            if _tb is None or _tb.tb_frame is None:
                # If we don't have a traceback there isn't much we can do
                continue

            # DEV: We go from the handler up to the root exception
            while _tb and frames_captured < config.max_frames:
                frames_captured += self._capture_tb_frame_for_span(span, _tb, exc_id, next(seq))

                # Move up the traceback
                _tb = _tb.tb_next

        if not frames_captured and tb is not None:
            # Ensure we capture at least one frame if we have a traceback,
            # the one potentially closer to user code.
            frames_captured += self._capture_tb_frame_for_span(span, tb, exc_id, only_user_code=False)

        if frames_captured:
            span.set_tag_str(DEBUG_INFO_TAG, "true")
            span.set_tag_str(EXCEPTION_HASH_TAG, str(exc_ident))
            span.set_tag_str(EXCEPTION_ID_TAG, str(exc_id))

            # Update the snapshot count
            root = span._local_root
            if root is not None:
                root.set_metric(SNAPSHOT_COUNT_TAG, frames_captured)

    @classmethod
    def enable(cls) -> None:
        if cls._instance is not None:
            log.debug("SpanExceptionHandler already enabled")
            return

        log.debug("Enabling SpanExceptionHandler")

        instance = cls()

        instance.__uploader__.register(UploaderProduct.EXCEPTION_REPLAY)
        core.on("span.exception", instance.on_span_exception, name=__name__)

        cls._instance = instance

    @classmethod
    def disable(cls) -> None:
        if cls._instance is None:
            log.debug("SpanExceptionHandler already disabled")
            return

        log.debug("Disabling SpanExceptionHandler")

        instance = cls._instance

        core.reset_listeners("span.exception", instance.on_span_exception)
        instance.__uploader__.unregister(UploaderProduct.EXCEPTION_REPLAY)

        cls._instance = None
