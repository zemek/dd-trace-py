import inspect
import os
from typing import Dict

from boto import __version__
import boto.connection
import wrapt

from ddtrace import config
from ddtrace.constants import _SPAN_MEASURED_KEY
from ddtrace.constants import SPAN_KIND
from ddtrace.ext import SpanKind
from ddtrace.ext import SpanTypes
from ddtrace.ext import aws
from ddtrace.ext import http
from ddtrace.internal.constants import COMPONENT
from ddtrace.internal.schema import schematize_cloud_api_operation
from ddtrace.internal.schema import schematize_service_name
from ddtrace.internal.utils import get_argument_value
from ddtrace.internal.utils.formats import asbool
from ddtrace.internal.utils.wrappers import unwrap
from ddtrace.trace import Pin


# Original boto client class
_Boto_client = boto.connection.AWSQueryConnection

AWS_QUERY_ARGS_NAME = ("operation_name", "params", "path", "verb")
AWS_AUTH_ARGS_NAME = (
    "method",
    "path",
    "headers",
    "data",
    "host",
    "auth_path",
    "sender",
)
AWS_QUERY_TRACED_ARGS = {"operation_name", "params", "path"}
AWS_AUTH_TRACED_ARGS = {"path", "data", "host"}


config._add(
    "boto",
    {
        "tag_no_params": asbool(os.getenv("DD_AWS_TAG_NO_PARAMS", default=False)),
    },
)


def get_version():
    # type: () -> str
    return __version__


def _supported_versions() -> Dict[str, str]:
    return {"boto": "*"}


def patch():
    if getattr(boto.connection, "_datadog_patch", False):
        return
    boto.connection._datadog_patch = True

    # AWSQueryConnection and AWSAuthConnection are two different classes called by
    # different services for connection.
    # For example EC2 uses AWSQueryConnection and S3 uses AWSAuthConnection
    wrapt.wrap_function_wrapper("boto.connection", "AWSQueryConnection.make_request", patched_query_request)
    wrapt.wrap_function_wrapper("boto.connection", "AWSAuthConnection.make_request", patched_auth_request)
    Pin(service="aws").onto(boto.connection.AWSQueryConnection)
    Pin(service="aws").onto(boto.connection.AWSAuthConnection)


def unpatch():
    if getattr(boto.connection, "_datadog_patch", False):
        boto.connection._datadog_patch = False
        unwrap(boto.connection.AWSQueryConnection, "make_request")
        unwrap(boto.connection.AWSAuthConnection, "make_request")


# ec2, sqs, kinesis
def patched_query_request(original_func, instance, args, kwargs):
    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        return original_func(*args, **kwargs)

    endpoint_name = instance.host.split(".")[0]

    with pin.tracer.trace(
        schematize_cloud_api_operation(
            "{}.command".format(endpoint_name), cloud_provider="aws", cloud_service=endpoint_name
        ),
        service=schematize_service_name("{}.{}".format(pin.service, endpoint_name)),
        span_type=SpanTypes.HTTP,
    ) as span:
        span.set_tag_str(COMPONENT, config.boto.integration_name)

        # set span.kind to the type of request being performed
        span.set_tag_str(SPAN_KIND, SpanKind.CLIENT)

        span.set_tag(_SPAN_MEASURED_KEY)

        operation_name = None
        if args:
            operation_name = get_argument_value(args, kwargs, 0, "action")
            params = get_argument_value(args, kwargs, 1, "params")

            span.resource = "%s.%s" % (endpoint_name, operation_name.lower())

            if params and not config.boto["tag_no_params"]:
                aws._add_api_param_span_tags(span, endpoint_name, params)
        else:
            span.resource = endpoint_name

        # Obtaining region name
        region_name = _get_instance_region_name(instance)

        meta = {
            aws.AGENT: "boto",
            aws.OPERATION: operation_name,
        }
        if region_name:
            meta[aws.REGION] = region_name
            meta[aws.AWSREGION] = region_name

        span.set_tags(meta)

        # Original func returns a boto.connection.HTTPResponse object
        result = original_func(*args, **kwargs)
        span.set_tag(http.STATUS_CODE, result.status)
        span.set_tag_str(http.METHOD, result._method)

        return result


# s3, lambda
def patched_auth_request(original_func, instance, args, kwargs):
    # Catching the name of the operation that called make_request()
    operation_name = None

    # Go up the stack until we get the first non-ddtrace module
    # DEV: For `lambda.list_functions()` this should be:
    #        - ddtrace.contrib.internal.boto.patch
    #        - wrapt.wrappers
    #        - boto.awslambda.layer1 (make_request)
    #        - boto.awslambda.layer1 (list_functions)
    # But can vary depending on Python versions; that's why we use an heuristic
    frame = inspect.currentframe().f_back
    operation_name = None
    while frame:
        if frame.f_code.co_name == "make_request":
            operation_name = frame.f_back.f_code.co_name
            break
        frame = frame.f_back

    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        return original_func(*args, **kwargs)

    endpoint_name = instance.host.split(".")[0]

    with pin.tracer.trace(
        schematize_cloud_api_operation(
            "{}.command".format(endpoint_name), cloud_provider="aws", cloud_service=endpoint_name
        ),
        service=schematize_service_name("{}.{}".format(pin.service, endpoint_name)),
        span_type=SpanTypes.HTTP,
    ) as span:
        span.set_tag(_SPAN_MEASURED_KEY)
        if args:
            http_method = get_argument_value(args, kwargs, 0, "method")
            span.resource = "%s.%s" % (endpoint_name, http_method.lower())
        else:
            span.resource = endpoint_name

        # Obtaining region name
        region_name = _get_instance_region_name(instance)

        meta = {
            aws.AGENT: "boto",
            aws.OPERATION: operation_name,
        }
        if region_name:
            meta[aws.REGION] = region_name
            meta[aws.AWSREGION] = region_name

        span.set_tags(meta)

        # Original func returns a boto.connection.HTTPResponse object
        result = original_func(*args, **kwargs)
        span.set_tag(http.STATUS_CODE, result.status)
        span.set_tag_str(http.METHOD, result._method)

        span.set_tag_str(COMPONENT, config.boto.integration_name)

        # set span.kind to the type of request being performed
        span.set_tag_str(SPAN_KIND, SpanKind.CLIENT)

        return result


def _get_instance_region_name(instance):
    region = getattr(instance, "region", None)

    if not region:
        return None
    if isinstance(region, str):
        return region.split(":")[1]
    else:
        return region.name
