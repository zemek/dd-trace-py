from typing import Dict

import structlog

import ddtrace
from ddtrace import config
from ddtrace._logger import LogInjectionState
from ddtrace.contrib.internal.trace_utils import unwrap as _u
from ddtrace.contrib.internal.trace_utils import wrap as _w
from ddtrace.internal.utils import get_argument_value
from ddtrace.internal.utils import set_argument_value


config._add(
    "structlog",
    dict(),
)


def get_version():
    # type: () -> str
    return getattr(structlog, "__version__", "")


def _supported_versions() -> Dict[str, str]:
    return {"structlog": ">=20.2.0"}


def _tracer_injection(_, __, event_dict):
    if config._logs_injection == LogInjectionState.DISABLED:
        return event_dict
    event_dict.update(ddtrace.tracer.get_log_correlation_context())
    return event_dict


def _w_get_logger(func, instance, args, kwargs):
    """
    Append the tracer injection processor to the ``default_processors`` list used by the logger
    Ensures that the tracer injection processor is the first processor in the chain and only injected once
    The ``default_processors`` list has built in defaults which protects against a user configured ``None`` value.
    The argument to configure ``default_processors`` accepts an iterable type:
        - List: default use case which has been accounted for
        - Tuple: patched via list conversion
        - Set: ignored because structlog processors care about order notably the last value to be a Renderer
        - Dict: because keys are ignored, this essentially becomes a List
    """

    dd_processor = [_tracer_injection]
    if (
        _tracer_injection not in list(structlog._config._CONFIG.default_processors)
        and structlog._config._CONFIG.default_processors
    ):
        structlog._config._CONFIG.default_processors = dd_processor + list(structlog._config._CONFIG.default_processors)

    return func(*args, **kwargs)


def _w_configure(func, instance, args, kwargs):
    """
    Injects the tracer injection processor to the ``processors`` list parameter when configuring a logger
    Ensures that the tracer injection processor is the first processor in the chain and only injected once
    In addition, the tracer injection processor is only injected if there is a renderer processor in the chain
    """

    dd_processor = [_tracer_injection]
    arg_processors = get_argument_value(args, kwargs, 0, "processors", True)
    if arg_processors and len(arg_processors) != 0:
        set_argument_value(args, kwargs, 0, "processors", dd_processor + list(arg_processors))

    return func(*args, **kwargs)


def _w_reset_defaults(func, instance, args, kwargs):
    """
    Reset the default_processors list to the original defaults
    Ensures that the tracer injection processor is injected after to the default_processors list
    """
    func(*args, **kwargs)

    dd_processor = [_tracer_injection]
    if (
        _tracer_injection not in list(structlog._config._CONFIG.default_processors)
        and structlog._config._CONFIG.default_processors
    ):
        structlog._config._CONFIG.default_processors = dd_processor + list(structlog._config._CONFIG.default_processors)

    return


def patch():
    """
    Patch ``structlog`` module for injection of tracer information
    by appending a processor before creating a logger via ``structlog.get_logger``
    """
    if getattr(structlog, "_datadog_patch", False):
        return
    structlog._datadog_patch = True

    if hasattr(structlog, "get_logger"):
        _w(structlog, "get_logger", _w_get_logger)

    # getLogger is an alias for get_logger
    if hasattr(structlog, "getLogger"):
        _w(structlog, "getLogger", _w_get_logger)

    if hasattr(structlog, "configure"):
        _w(structlog, "configure", _w_configure)

    if hasattr(structlog, "reset_defaults"):
        _w(structlog, "reset_defaults", _w_reset_defaults)


def unpatch():
    if getattr(structlog, "_datadog_patch", False):
        structlog._datadog_patch = False

        if hasattr(structlog, "get_logger"):
            _u(structlog, "get_logger")
        if hasattr(structlog, "getLogger"):
            _u(structlog, "getLogger")
        if hasattr(structlog, "configure"):
            _u(structlog, "configure")
        if hasattr(structlog, "reset_defaults"):
            _u(structlog, "reset_defaults")
