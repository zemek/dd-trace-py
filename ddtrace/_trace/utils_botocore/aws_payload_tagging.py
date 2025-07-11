import copy
from decimal import Decimal
import json
from typing import Any
from typing import Dict
from typing import Optional

from ddtrace import config
from ddtrace._trace.span import Span
from ddtrace.vendor.jsonpath_ng import parse


_MAX_TAG_VALUE_LENGTH = 5000


class AWSPayloadTagging:
    _INCOMPLETE_TAG = "_dd.payload_tags_incomplete"  # Set to True if MAX_TAGS is reached

    _REDACTION_PATHS_DEFAULTS = [
        # SNS
        "$..Attributes.KmsMasterKeyId",
        "$..Attributes.Token",
        # EventBridge
        "$..AuthParameters.OAuthParameters.OAuthHttpParameters.HeaderParameters[*].Value",
        "$..AuthParameters.OAuthParameters.OAuthHttpParameters.QueryStringParameters[*].Value",
        "$..AuthParameters.OAuthParameters.OAuthHttpParameters.BodyParameters[*].Value",
        "$..AuthParameters.InvocationHttpParameters.HeaderParameters[*].Value",
        "$..AuthParameters.InvocationHttpParameters.QueryStringParameters[*].Value",
        "$..AuthParameters.InvocationHttpParameters.BodyParameters[*].Value",
        "$..Targets[*].RedshiftDataParameters.Sql",
        "$..Targets[*].RedshiftDataParameters.Sqls",
        "$..Targets[*].AppSyncParameters.GraphQLOperation",
        # // S3
        "$..SSEKMSKeyId",
        "$..SSEKMSEncryptionContext",
    ]
    _REQUEST_REDACTION_PATHS_DEFAULTS = [
        # Sns
        "$..Attributes.PlatformCredential",
        "$..Attributes.PlatformPrincipal",
        "$..AWSAccountId",
        "$..Endpoint",
        "$..Token",
        "$..OneTimePassword",
        "$..phoneNumber",
        "$..PhoneNumber",
        # EventBridge
        "$..AuthParameters.BasicAuthParameters.Password",
        "$..AuthParameters.OAuthParameters.ClientParameters.ClientSecret",
        "$..AuthParameters.ApiKeyAuthParameters.ApiKeyValue",
        # S3
        "$..SSECustomerKey",
        "$..CopySourceSSECustomerKey",
        "$..RestoreRequest.OutputLocation.S3.Encryption.KMSKeyId",
    ]

    _RESPONSE_REDACTION_PATHS_DEFAULTS = [
        # // Sns
        "$..Endpoints.*.Token",
        "$..PlatformApplication.*.PlatformCredential",
        "$..PlatformApplication.*.PlatformPrincipal",
        "$..Subscriptions.*.Endpoint",
        "$..PhoneNumbers[*].PhoneNumber",
        "$..phoneNumbers[*]",
        # // S3
        "$..Credentials.SecretAccessKey",
        "$..Credentials.SessionToken",
    ]

    def __init__(self):
        self.current_tag_count = 0
        self.validated = False
        self.request_redaction_paths = None
        self.response_redaction_paths = None

    def expand_payload_as_tags(self, span: Span, result: Dict[str, Any], key: str) -> None:
        """
        Expands the JSON payload from various AWS services into tags and sets them on the Span.
        """
        if not self.validated:
            self.request_redaction_paths = self._get_redaction_paths_request()
            self.response_redaction_paths = self._get_redaction_paths_response()
            self.validated = True

        if not self.request_redaction_paths and not self.response_redaction_paths:
            return

        if not result:
            return

        # we will be redacting at least one of request/response
        redacted_dict = copy.deepcopy(result)
        self.current_tag_count = 0
        if self.request_redaction_paths:
            self._redact_json(redacted_dict, span, self.request_redaction_paths)
        if self.response_redaction_paths:
            self._redact_json(redacted_dict, span, self.response_redaction_paths)

        # flatten the payload into span tags
        for key2, value in redacted_dict.items():
            escaped_sub_key = key2.replace(".", "\\.")
            self._tag_object(span, f"{key}.{escaped_sub_key}", value)
            if self.current_tag_count >= config.botocore.get("payload_tagging_max_tags"):
                return

    def _should_json_parse(self, obj: Any) -> bool:
        if isinstance(obj, (str, bytes)):
            return True
        return False

    def _validate_json_paths(self, paths: Optional[str]) -> bool:
        """
        Checks whether paths is "all" or all valid JSONPaths
        """
        if not paths:
            return False  # not enabled

        if paths == "all":
            return True  # enabled, use the defaults

        # otherwise validate that we have valid JSONPaths
        for path in paths.split(","):
            if path:
                # Require JSONPath to start with "$"
                if not path.startswith("$"):
                    return False
                try:
                    parse(path)
                except Exception:
                    return False
            else:
                return False

        return True

    def _redact_json(self, data: Dict[str, Any], span: Span, paths: list) -> None:
        """
        Redact sensitive data in the JSON payload based on default and user-provided JSONPath expressions
        """
        for path in paths:
            expression = parse(path)
            for match in expression.find(data):
                match.context.value[match.path.fields[0]] = "redacted"

    def _get_redaction_paths_response(self) -> list:
        """
        Get the list of redaction paths, combining defaults with any user-provided JSONPaths.
        """
        if not config.botocore.get("payload_tagging_response"):
            return []

        response_redaction = config.botocore.get("payload_tagging_response")
        if self._validate_json_paths(response_redaction):
            if response_redaction == "all":
                return self._RESPONSE_REDACTION_PATHS_DEFAULTS + self._REDACTION_PATHS_DEFAULTS
            return (
                self._RESPONSE_REDACTION_PATHS_DEFAULTS + self._REDACTION_PATHS_DEFAULTS + response_redaction.split(",")
            )

        return []

    def _get_redaction_paths_request(self) -> list:
        """
        Get the list of redaction paths, combining defaults with any user-provided JSONPaths.
        """
        if not config.botocore.get("payload_tagging_request"):
            return []

        request_redaction = config.botocore.get("payload_tagging_request")
        if self._validate_json_paths(request_redaction):
            if request_redaction == "all":
                return self._REQUEST_REDACTION_PATHS_DEFAULTS + self._REDACTION_PATHS_DEFAULTS
            return (
                self._REQUEST_REDACTION_PATHS_DEFAULTS + self._REDACTION_PATHS_DEFAULTS + request_redaction.split(",")
            )

        return []

    def _tag_object(self, span: Span, key: str, obj: Any, depth: int = 0) -> None:
        """
        Recursively expands the given AWS payload object and adds the values as flattened Span tags.
        It is not expected that AWS Payloads will be deeply nested so the number of recursive calls should be low.
        For example, the following (shortened payload object) becomes:
        {
            "ResponseMetadata": {
                "RequestId": "SOMEID",
                "HTTPHeaders": {
                    "x-amz-request-id": "SOMEID",
                    "content-length": "5",
                }
        }

        =>

        "aws.response.body.RequestId": "SOMEID"
        "aws.response.body.HTTPHeaders.x-amz-request-id": "SOMEID"
        "aws.response.body.HTTPHeaders.content-length": "5"
        """
        # if we've hit the maximum allowed tags, mark the expansion as incomplete
        if self.current_tag_count >= config.botocore.get("payload_tagging_max_tags"):
            span.set_tag(self._INCOMPLETE_TAG, True)
            return
        if obj is None:
            self.current_tag_count += 1
            span.set_tag(key, obj)
            return
        if depth >= config.botocore.get("payload_tagging_max_depth"):
            self.current_tag_count += 1
            span.set_tag(
                key, str(obj)[:_MAX_TAG_VALUE_LENGTH]
            )  # at the maximum depth - set the tag without further expansion
            return
        depth += 1
        if self._should_json_parse(obj):
            try:
                parsed = json.loads(obj)
                self._tag_object(span, key, parsed, depth)
            except ValueError:
                self.current_tag_count += 1
                span.set_tag(key, str(obj)[:_MAX_TAG_VALUE_LENGTH])
            return
        if isinstance(obj, (int, float, Decimal)):
            self.current_tag_count += 1
            span.set_tag(key, str(obj))
            return
        if isinstance(obj, list):
            for k, v in enumerate(obj):
                self._tag_object(span, f"{key}.{k}", v, depth)
            return
        if hasattr(obj, "items"):
            for k, v in obj.items():
                escaped_key = str(k).replace(".", "\\.")
                self._tag_object(span, f"{key}.{escaped_key}", v, depth)
            return
        if hasattr(obj, "to_dict"):
            for k, v in obj.to_dict().items():
                escaped_key = str(k).replace(".", "\\.")
                self._tag_object(span, f"{key}.{escaped_key}", v, depth)
            return
        try:
            value_as_str = str(obj)
        except Exception:
            value_as_str = "UNKNOWN"
        self.current_tag_count += 1
        span.set_tag(key, value_as_str)
