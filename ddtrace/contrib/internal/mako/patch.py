from typing import Dict

import mako
from mako.template import DefTemplate
from mako.template import Template

from ddtrace import config
from ddtrace.constants import _SPAN_MEASURED_KEY
from ddtrace.contrib.internal.trace_utils import int_service
from ddtrace.contrib.internal.trace_utils import unwrap as _u
from ddtrace.contrib.internal.trace_utils import wrap as _w
from ddtrace.ext import SpanTypes
from ddtrace.internal.constants import COMPONENT
from ddtrace.internal.schema import schematize_service_name
from ddtrace.internal.utils.importlib import func_name
from ddtrace.trace import Pin

from .constants import DEFAULT_TEMPLATE_NAME


def get_version():
    # type: () -> str
    return getattr(mako, "__version__", "")


def _supported_versions() -> Dict[str, str]:
    return {"mako": ">=1.0.0"}


def patch():
    if getattr(mako, "__datadog_patch", False):
        # already patched
        return
    mako.__datadog_patch = True

    Pin().onto(Template)

    _w(mako, "template.Template.render", _wrap_render)
    _w(mako, "template.Template.render_unicode", _wrap_render)
    _w(mako, "template.Template.render_context", _wrap_render)


def unpatch():
    if not getattr(mako, "__datadog_patch", False):
        return
    mako.__datadog_patch = False

    _u(mako.template.Template, "render")
    _u(mako.template.Template, "render_unicode")
    _u(mako.template.Template, "render_context")


def _wrap_render(wrapped, instance, args, kwargs):
    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        return wrapped(*args, **kwargs)

    # Determine the resource and `mako.template_name` tag value
    # DefTemplate is a wrapper around a callable from another template, it does not have a filename
    # https://github.com/sqlalchemy/mako/blob/c2c690ac9add584f2216dc655cdf8215b24ef03c/mako/template.py#L603-L622
    if isinstance(instance, DefTemplate) and hasattr(instance, "callable_"):
        template_name = func_name(instance.callable_)
    else:
        template_name = getattr(instance, "filename", None)
    template_name = template_name or DEFAULT_TEMPLATE_NAME

    with pin.tracer.trace(
        func_name(wrapped), int_service(pin, config.mako, schematize_service_name("mako")), span_type=SpanTypes.TEMPLATE
    ) as span:
        span.set_tag_str(COMPONENT, "mako")

        span.set_tag(_SPAN_MEASURED_KEY)
        try:
            return wrapped(*args, **kwargs)
        finally:
            span.resource = template_name
            span.set_tag("mako.template_name", template_name)
