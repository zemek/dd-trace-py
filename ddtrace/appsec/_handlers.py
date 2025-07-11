import asyncio
import io
import json
from typing import Any
from typing import Dict
from typing import Optional

import xmltodict

from ddtrace._trace.span import Span
from ddtrace.appsec._asm_request_context import _call_waf
from ddtrace.appsec._asm_request_context import _call_waf_first
from ddtrace.appsec._asm_request_context import get_blocked
from ddtrace.appsec._constants import SPAN_DATA_NAMES
from ddtrace.appsec._http_utils import extract_cookies_from_headers
from ddtrace.appsec._http_utils import normalize_headers
from ddtrace.appsec._http_utils import parse_http_body
from ddtrace.contrib import trace_utils
from ddtrace.contrib.internal.trace_utils_base import _get_request_header_user_agent
from ddtrace.contrib.internal.trace_utils_base import _set_url_tag
from ddtrace.ext import http
from ddtrace.internal import core
from ddtrace.internal import telemetry
from ddtrace.internal.constants import RESPONSE_HEADERS
from ddtrace.internal.logger import get_logger
from ddtrace.internal.telemetry import TELEMETRY_NAMESPACE
from ddtrace.internal.utils import http as http_utils
from ddtrace.internal.utils.http import parse_form_multipart
from ddtrace.settings.asm import config as asm_config


log = get_logger(__name__)
_BODY_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


def _get_content_length(environ):
    content_length = environ.get("CONTENT_LENGTH")
    transfer_encoding = environ.get("HTTP_TRANSFER_ENCODING")

    if transfer_encoding == "chunked" or content_length is None:
        return None

    try:
        return max(0, int(content_length))
    except Exception:
        return 0


# set_http_meta


def _on_set_http_meta(
    span,
    request_ip,
    raw_uri,
    route,
    method,
    request_headers,
    request_cookies,
    parsed_query,
    request_path_params,
    request_body,
    status_code,
    response_headers,
    response_cookies,
):
    if asm_config._asm_enabled and span.span_type in asm_config._asm_http_span_types:
        # avoid circular import
        from ddtrace.appsec._asm_request_context import set_waf_address

        status_code = str(status_code) if status_code is not None else None

        addresses = [
            (SPAN_DATA_NAMES.REQUEST_HTTP_IP, request_ip),
            (SPAN_DATA_NAMES.REQUEST_URI_RAW, raw_uri),
            (SPAN_DATA_NAMES.REQUEST_ROUTE, route),
            (SPAN_DATA_NAMES.REQUEST_METHOD, method),
            (SPAN_DATA_NAMES.REQUEST_HEADERS_NO_COOKIES, request_headers),
            (SPAN_DATA_NAMES.REQUEST_COOKIES, request_cookies),
            (SPAN_DATA_NAMES.REQUEST_QUERY, parsed_query),
            (SPAN_DATA_NAMES.REQUEST_PATH_PARAMS, request_path_params),
            (SPAN_DATA_NAMES.REQUEST_BODY, request_body),
            (SPAN_DATA_NAMES.RESPONSE_STATUS, status_code),
            (SPAN_DATA_NAMES.RESPONSE_HEADERS_NO_COOKIES, response_headers),
        ]
        for k, v in addresses:
            if v is not None:
                set_waf_address(k, v)


# AWS Lambda
def _on_lambda_start_request(
    span: Span,
    request_headers: Dict[str, str],
    request_ip: Optional[str],
    body: Optional[str],
    is_body_base64: bool,
    raw_uri: str,
    route: str,
    method: str,
    parsed_query: Dict[str, Any],
    request_path_parameters: Optional[Dict[str, Any]],
):
    if not (asm_config._asm_enabled and span.span_type in asm_config._asm_http_span_types):
        return

    headers = normalize_headers(request_headers)
    request_body = parse_http_body(headers, body, is_body_base64)
    request_cookies = extract_cookies_from_headers(headers)

    _on_set_http_meta(
        span,
        request_ip,
        raw_uri,
        route,
        method,
        headers,
        request_cookies,
        parsed_query,
        request_path_parameters,
        request_body,
        None,
        None,
        None,
    )

    _call_waf_first(("aws_lambda",))


def _on_lambda_start_response(
    span: Span,
    status_code: str,
    response_headers: Dict[str, str],
):
    if not (asm_config._asm_enabled and span.span_type in asm_config._asm_http_span_types):
        return

    waf_headers = normalize_headers(response_headers)
    response_cookies = extract_cookies_from_headers(waf_headers)

    _on_set_http_meta(
        span,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        status_code,
        waf_headers,
        response_cookies,
    )

    _call_waf(("aws_lambda",))


# ASGI


async def _on_asgi_request_parse_body(receive, headers):
    if asm_config._asm_enabled:
        more_body = True
        body_parts = []
        try:
            while more_body:
                data_received = await asyncio.wait_for(receive(), asm_config._fast_api_async_body_timeout)
                if data_received is None:
                    more_body = False
                if isinstance(data_received, dict):
                    more_body = data_received.get("more_body", False)
                    body_parts.append(data_received.get("body", b""))
        except asyncio.TimeoutError:
            pass
        except Exception:
            return receive, None
        body = b"".join(body_parts)

        async def receive_wrapped(once=[True]):
            if once[0]:
                once[0] = False
                return {"type": "http.request", "body": body, "more_body": more_body}
            return await receive()

        try:
            content_type = headers.get("content-type") or headers.get("Content-Type")
            if content_type in ("application/json", "text/json"):
                if body is None or body == b"":
                    req_body = None
                else:
                    req_body = json.loads(body.decode())
            elif content_type in ("application/xml", "text/xml"):
                req_body = xmltodict.parse(body)
            elif content_type == "text/plain":
                req_body = None
            else:
                req_body = parse_form_multipart(body.decode(), headers) or None
            return receive_wrapped, req_body
        except Exception:
            return receive_wrapped, None

    return receive, None


# FLASK


def _on_request_span_modifier(
    ctx, flask_config, request, environ, _HAS_JSON_MIXIN, flask_version, flask_version_str, exception_type
):
    req_body = None
    if asm_config._asm_enabled and request.method in _BODY_METHODS:
        content_type = request.content_type
        wsgi_input = environ.get("wsgi.input", "")

        # Copy wsgi input if not seekable
        if wsgi_input:
            try:
                seekable = wsgi_input.seekable()
            # expect AttributeError in normal error cases
            except Exception:
                seekable = False
            if not seekable:
                # https://gist.github.com/mitsuhiko/5721547
                # Provide wsgi.input as an end-of-file terminated stream.
                # In that case wsgi.input_terminated is set to True
                # and an app is required to read to the end of the file and disregard CONTENT_LENGTH for reading.
                if environ.get("wsgi.input_terminated"):
                    body = wsgi_input.read()
                else:
                    content_length = _get_content_length(environ)
                    body = wsgi_input.read(content_length) if content_length else b""
                environ["wsgi.input"] = io.BytesIO(body)

        try:
            if content_type in ("application/json", "text/json"):
                if _HAS_JSON_MIXIN and hasattr(request, "json") and request.json:
                    req_body = request.json
                elif request.data is None or request.data == b"":
                    req_body = None
                else:
                    req_body = json.loads(request.data.decode("UTF-8"))
            elif content_type in ("application/xml", "text/xml"):
                req_body = xmltodict.parse(request.get_data())
            elif hasattr(request, "form"):
                req_body = request.form.to_dict()
            else:
                # no raw body
                req_body = None
        except Exception:
            log.debug("Failed to parse request body", exc_info=True)
        finally:
            # Reset wsgi input to the beginning
            if wsgi_input:
                if seekable:
                    wsgi_input.seek(0)
                else:
                    environ["wsgi.input"] = io.BytesIO(body)
    return req_body


def _on_grpc_server_response(message):
    from ddtrace.appsec._asm_request_context import set_waf_address

    set_waf_address(SPAN_DATA_NAMES.GRPC_SERVER_RESPONSE_MESSAGE, message)


def _on_grpc_server_data(headers, request_message, method, metadata):
    from ddtrace.appsec._asm_request_context import set_headers
    from ddtrace.appsec._asm_request_context import set_waf_address

    set_headers(headers)
    if request_message is not None:
        set_waf_address(SPAN_DATA_NAMES.GRPC_SERVER_REQUEST_MESSAGE, request_message)

    set_waf_address(SPAN_DATA_NAMES.GRPC_SERVER_METHOD, method)

    if metadata:
        set_waf_address(SPAN_DATA_NAMES.GRPC_SERVER_REQUEST_METADATA, dict(metadata))


def _wsgi_make_block_content(ctx, construct_url):
    middleware = ctx.get_item("middleware")
    req_span = ctx.get_item("req_span")
    headers = ctx.get_item("headers")
    environ = ctx.get_item("environ")
    if req_span is None:
        raise ValueError("request span not found")
    block_config = get_blocked()
    desired_type = block_config.get("type", "auto")
    ctype = None
    if desired_type == "none":
        content = ""
        resp_headers = [("content-type", "text/plain; charset=utf-8"), ("location", block_config.get("location", ""))]
    else:
        ctype = block_config.get("content-type", "application/json")
        content = http_utils._get_blocked_template(ctype).encode("UTF-8")
        resp_headers = [("content-type", ctype)]
    status = block_config.get("status_code", 403)
    try:
        req_span.set_tag_str(RESPONSE_HEADERS + ".content-length", str(len(content)))
        if ctype is not None:
            req_span.set_tag_str(RESPONSE_HEADERS + ".content-type", ctype)
        req_span.set_tag_str(http.STATUS_CODE, str(status))
        url = construct_url(environ)
        query_string = environ.get("QUERY_STRING")
        _set_url_tag(middleware._config, req_span, url, query_string)
        if query_string and middleware._config.trace_query_string:
            req_span.set_tag_str(http.QUERY_STRING, query_string)
        method = environ.get("REQUEST_METHOD")
        if method:
            req_span.set_tag_str(http.METHOD, method)
        user_agent = _get_request_header_user_agent(headers, headers_are_case_sensitive=True)
        if user_agent:
            req_span.set_tag_str(http.USER_AGENT, user_agent)
    except Exception as e:
        log.warning("Could not set some span tags on blocked request: %s", str(e))  # noqa: G200
    resp_headers.append(("Content-Length", str(len(content))))
    return status, resp_headers, content


def _asgi_make_block_content(ctx, url):
    middleware = ctx.get_item("middleware")
    req_span = ctx.get_item("req_span")
    headers = ctx.get_item("headers")
    environ = ctx.get_item("environ")
    if req_span is None:
        raise ValueError("request span not found")
    block_config = get_blocked()
    desired_type = block_config.get("type", "auto")
    ctype = None
    if desired_type == "none":
        content = ""
        resp_headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"location", block_config.get("location", "").encode()),
        ]
    else:
        ctype = block_config.get("content-type", "application/json")
        content = http_utils._get_blocked_template(ctype).encode("UTF-8")
        # ctype = f"{ctype}; charset=utf-8" can be considered at some point
        resp_headers = [(b"content-type", ctype.encode())]
    status = block_config.get("status_code", 403)
    try:
        req_span.set_tag_str(RESPONSE_HEADERS + ".content-length", str(len(content)))
        if ctype is not None:
            req_span.set_tag_str(RESPONSE_HEADERS + ".content-type", ctype)
        req_span.set_tag_str(http.STATUS_CODE, str(status))
        query_string = environ.get("QUERY_STRING")
        _set_url_tag(middleware.integration_config, req_span, url, query_string)
        if query_string and middleware._config.trace_query_string:
            req_span.set_tag_str(http.QUERY_STRING, query_string)
        method = environ.get("REQUEST_METHOD")
        if method:
            req_span.set_tag_str(http.METHOD, method)
        user_agent = _get_request_header_user_agent(headers, headers_are_case_sensitive=True)
        if user_agent:
            req_span.set_tag_str(http.USER_AGENT, user_agent)
    except Exception as e:
        log.warning("Could not set some span tags on blocked request: %s", str(e))  # noqa: G200
    resp_headers.append((b"Content-Length", str(len(content)).encode()))
    return status, resp_headers, content


def _on_flask_blocked_request(span):
    span.set_tag_str(http.STATUS_CODE, "403")
    request = core.get_item("flask_request")
    try:
        base_url = getattr(request, "base_url", None)
        query_string = getattr(request, "query_string", None)
        if base_url and query_string:
            _set_url_tag(core.get_item("flask_config"), span, base_url, query_string)
        if query_string and core.get_item("flask_config").trace_query_string:
            span.set_tag_str(http.QUERY_STRING, query_string)
        if request.method is not None:
            span.set_tag_str(http.METHOD, request.method)
        user_agent = _get_request_header_user_agent(request.headers)
        if user_agent:
            span.set_tag_str(http.USER_AGENT, user_agent)
    except Exception as e:
        log.warning("Could not set some span tags on blocked request: %s", str(e))  # noqa: G200


def _on_start_response_blocked(ctx, flask_config, response_headers, status):
    trace_utils.set_http_meta(ctx["req_span"], flask_config, status_code=status, response_headers=response_headers)


def _on_telemetry_periodic():
    if asm_config._asm_enabled:
        telemetry.telemetry_writer.add_gauge_metric(
            TELEMETRY_NAMESPACE.APPSEC, "enabled", 2, (("origin", asm_config.asm_enabled_origin),)
        )


def listen():
    core.on("telemetry.periodic", _on_telemetry_periodic)

    core.on("set_http_meta_for_asm", _on_set_http_meta)
    core.on("flask.request_call_modifier", _on_request_span_modifier, "request_body")

    core.on("flask.blocked_request_callable", _on_flask_blocked_request)

    core.on("flask.start_response.blocked", _on_start_response_blocked)

    core.on("asgi.request.parse.body", _on_asgi_request_parse_body, "await_receive_and_body")

    core.on("aws_lambda.start_request", _on_lambda_start_request)
    core.on("aws_lambda.start_response", _on_lambda_start_response)

    core.on("grpc.server.response.message", _on_grpc_server_response)
    core.on("grpc.server.data", _on_grpc_server_data)

    core.on("wsgi.block.started", _wsgi_make_block_content, "status_headers_content")
    core.on("asgi.block.started", _asgi_make_block_content, "status_headers_content")
