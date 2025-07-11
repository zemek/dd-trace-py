from io import BytesIO
from io import StringIO

from ddtrace.appsec._iast._taint_tracking._taint_objects_base import is_pyobject_tainted
from ddtrace.appsec._iast._utils import _is_iast_propagation_debug_enabled
from ddtrace.internal._unpatched import _threading as threading
from ddtrace.internal.logger import get_logger


log = get_logger(__name__)

if _is_iast_propagation_debug_enabled():
    TAINTED_FRAMES = []

    def trace_calls_and_returns(frame, event, arg):
        co = frame.f_code
        func_name = co.co_name
        if func_name == "write":
            # Ignore write() calls from print statements
            return
        if func_name in ("is_pyobject_tainted", "__repr__"):
            return
        line_no = frame.f_lineno
        filename = co.co_filename
        if "ddtrace" in filename:
            return
        if event == "call":
            f_locals = frame.f_locals
            try:
                if any([is_pyobject_tainted(f_locals[arg]) for arg in f_locals]):
                    TAINTED_FRAMES.append(frame)
                    log.debug("Call to %s on line %s of %s, args: %s", func_name, line_no, filename, frame.f_locals)
                    log.debug("Tainted arguments:")
                    for arg in f_locals:
                        if is_pyobject_tainted(f_locals[arg]):
                            log.debug("\t%s: %s", arg, f_locals[arg])
                    log.debug("-----")
                return trace_calls_and_returns
            except AttributeError:
                pass
        elif event == "return":
            if frame in TAINTED_FRAMES:
                TAINTED_FRAMES.remove(frame)
                log.debug("Return from %s on line %d of %s, return value: %s", func_name, line_no, filename, arg)
                if isinstance(arg, (str, bytes, bytearray, BytesIO, StringIO, list, tuple, dict)):
                    if (
                        (isinstance(arg, (str, bytes, bytearray, BytesIO, StringIO)) and is_pyobject_tainted(arg))
                        or (isinstance(arg, (list, tuple)) and any([is_pyobject_tainted(x) for x in arg]))
                        or (isinstance(arg, dict) and any([is_pyobject_tainted(x) for x in arg.values()]))
                    ):
                        log.debug("Return value is tainted")
                    else:
                        log.debug("Return value is NOT tainted")
                log.debug("-----")
        return

    threading.settrace(trace_calls_and_returns)
