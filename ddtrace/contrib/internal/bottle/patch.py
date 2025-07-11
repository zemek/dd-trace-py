import os
from typing import Dict

import bottle
import wrapt

from ddtrace import config
from ddtrace.internal.utils.formats import asbool

from .trace import TracePlugin


# Configure default configuration
config._add(
    "bottle",
    dict(
        distributed_tracing=asbool(os.getenv("DD_BOTTLE_DISTRIBUTED_TRACING", default=True)),
    ),
)


def get_version():
    # type: () -> str
    return getattr(bottle, "__version__", "")


def _supported_versions() -> Dict[str, str]:
    return {"bottle": ">=0.12"}


def patch():
    """Patch the bottle.Bottle class"""
    if getattr(bottle, "_datadog_patch", False):
        return

    bottle._datadog_patch = True
    wrapt.wrap_function_wrapper("bottle", "Bottle.__init__", traced_init)


def traced_init(wrapped, instance, args, kwargs):
    wrapped(*args, **kwargs)

    service = config._get_service(default="bottle")

    plugin = TracePlugin(service=service)
    instance.install(plugin)
