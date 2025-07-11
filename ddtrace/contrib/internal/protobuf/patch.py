from typing import Dict

from google import protobuf
from google.protobuf.internal import builder
import wrapt

from ddtrace import config
from ddtrace.internal.utils.wrappers import unwrap
from ddtrace.trace import Pin

from .schema_iterator import SchemaExtractor


config._add(
    "protobuf",
    dict(),
)


_WRAPPED_MESSAGE_CLASSES = []


def get_version():
    # type: () -> str
    return getattr(protobuf, "__version__", "")


def _supported_versions() -> Dict[str, str]:
    return {"protobuf": "*"}


def patch():
    """Patch the instrumented methods"""
    if getattr(protobuf, "_datadog_patch", False):
        return
    protobuf._datadog_patch = True

    _w = wrapt.wrap_function_wrapper

    _w("google.protobuf.internal", "builder.BuildTopDescriptorsAndMessages", _traced_build)
    Pin().onto(builder)


def unpatch():
    if getattr(protobuf, "_datadog_patch", False):
        protobuf._datadog_patch = False

        unwrap(protobuf.internal.builder, "BuildTopDescriptorsAndMessages")

        global _WRAPPED_MESSAGE_CLASSES
        for wrapped_message_class in _WRAPPED_MESSAGE_CLASSES:
            _unwrap_message(wrapped_message_class)

        _WRAPPED_MESSAGE_CLASSES = []


def _unwrap_message(message_class):
    unwrap(message_class, "SerializeToString")
    unwrap(message_class, "ParseFromString")


def _wrap_message(message_descriptor, message_class):
    def serialize_wrapper(wrapped, instance, args, kwargs):
        return _traced_serialize_message(wrapped, instance, args, kwargs, msg_descriptor=message_descriptor)

    def deserialize_wrapper(wrapped, instance, args, kwargs):
        return _traced_deserialize_message(wrapped, instance, args, kwargs, msg_descriptor=message_descriptor)

    _w = wrapt.wrap_function_wrapper
    _w(message_class, "SerializeToString", serialize_wrapper)
    _w(message_class, "ParseFromString", deserialize_wrapper)

    global _WRAPPED_MESSAGE_CLASSES
    _WRAPPED_MESSAGE_CLASSES.append(message_class)
    Pin().onto(message_class)


#
# tracing functions
#
def _traced_build(func, instance, args, kwargs):
    file_des = args[0]

    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        func(*args, **kwargs)

    try:
        func(*args, **kwargs)
    finally:
        if config._data_streams_enabled:
            generated_message_classes = args[2]
            message_descriptors = file_des.message_types_by_name.items()
            for message_idx in range(len(message_descriptors)):
                message_class_name = message_descriptors[message_idx][0]
                message_descriptor = message_descriptors[message_idx][1]
                message_class = generated_message_classes[message_class_name]
                _wrap_message(message_descriptor=message_descriptor, message_class=message_class)


def _traced_deserialize_message(func, instance, args, kwargs, msg_descriptor):
    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        func(*args, **kwargs)

    active = pin.tracer.current_span()

    try:
        func(*args, **kwargs)
    finally:
        if config._data_streams_enabled and active:
            SchemaExtractor.attach_schema_on_span(msg_descriptor, active, SchemaExtractor.DESERIALIZATION)


def _traced_serialize_message(func, instance, args, kwargs, msg_descriptor):
    pin = Pin.get_from(instance)
    if not pin or not pin.enabled() or not msg_descriptor:
        return func(*args, **kwargs)

    active = pin.tracer.current_span()

    try:
        return func(*args, **kwargs)
    finally:
        if config._data_streams_enabled and active:
            SchemaExtractor.attach_schema_on_span(msg_descriptor, active, SchemaExtractor.SERIALIZATION)
