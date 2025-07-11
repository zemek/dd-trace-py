import os
from typing import Dict

import jinja2
from wrapt import wrap_function_wrapper as _w

from ddtrace import config
from ddtrace.constants import _SPAN_MEASURED_KEY
from ddtrace.contrib.internal.trace_utils import unwrap as _u
from ddtrace.ext import SpanTypes
from ddtrace.internal.constants import COMPONENT
from ddtrace.internal.utils import ArgumentError
from ddtrace.internal.utils import get_argument_value
from ddtrace.trace import Pin

from .constants import DEFAULT_TEMPLATE_NAME


# default settings
config._add(
    "jinja2",
    {
        "service_name": os.getenv("DD_JINJA2_SERVICE_NAME"),
    },
)


def get_version():
    # type: () -> str
    return getattr(jinja2, "__version__", "")


def _supported_versions() -> Dict[str, str]:
    return {"jinja2": ">=2.10"}


def patch():
    if getattr(jinja2, "__datadog_patch", False):
        # already patched
        return
    jinja2.__datadog_patch = True
    Pin(
        service=config.jinja2["service_name"],
        _config=config.jinja2,
    ).onto(jinja2.environment.Environment)
    _w(jinja2, "environment.Template.render", _wrap_render)
    _w(jinja2, "environment.Template.generate", _wrap_render)
    _w(jinja2, "environment.Environment.compile", _wrap_compile)
    _w(jinja2, "environment.Environment._load_template", _wrap_load_template)


def unpatch():
    if not getattr(jinja2, "__datadog_patch", False):
        return
    jinja2.__datadog_patch = False
    _u(jinja2.Template, "render")
    _u(jinja2.Template, "generate")
    _u(jinja2.Environment, "compile")
    _u(jinja2.Environment, "_load_template")


def _wrap_render(wrapped, instance, args, kwargs):
    """Wrap `Template.render()` or `Template.generate()`"""
    pin = Pin.get_from(instance.environment)
    if not pin or not pin.enabled():
        return wrapped(*args, **kwargs)

    template_name = str(instance.name or DEFAULT_TEMPLATE_NAME)
    with pin.tracer.trace("jinja2.render", pin.service, span_type=SpanTypes.TEMPLATE) as span:
        span.set_tag_str(COMPONENT, config.jinja2.integration_name)

        span.set_tag(_SPAN_MEASURED_KEY)
        try:
            return wrapped(*args, **kwargs)
        finally:
            span.resource = template_name
            span.set_tag_str("jinja2.template_name", template_name)


def _wrap_compile(wrapped, instance, args, kwargs):
    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        return wrapped(*args, **kwargs)

    try:
        template_name = get_argument_value(args, kwargs, 1, "name")
    except ArgumentError:
        template_name = DEFAULT_TEMPLATE_NAME

    with pin.tracer.trace("jinja2.compile", pin.service, span_type=SpanTypes.TEMPLATE) as span:
        try:
            return wrapped(*args, **kwargs)
        finally:
            span.set_tag_str(COMPONENT, config.jinja2.integration_name)

            span.resource = template_name
            span.set_tag_str("jinja2.template_name", template_name)


def _wrap_load_template(wrapped, instance, args, kwargs):
    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        return wrapped(*args, **kwargs)

    template_name = get_argument_value(args, kwargs, 0, "name")
    with pin.tracer.trace("jinja2.load", pin.service, span_type=SpanTypes.TEMPLATE) as span:
        template = None
        try:
            template = wrapped(*args, **kwargs)
            return template
        finally:
            span.resource = template_name
            span.set_tag_str("jinja2.template_name", template_name)
            if template:
                span.set_tag_str("jinja2.template_path", template.filename)
