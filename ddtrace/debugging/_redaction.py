from ddtrace.debugging._expressions import DDCompiler
from ddtrace.debugging._expressions import DDExpression
from ddtrace.internal.logger import get_logger
from ddtrace.internal.utils.cache import cached
from ddtrace.settings.dynamic_instrumentation import config
from ddtrace.settings.dynamic_instrumentation import normalize_ident


log = get_logger(__name__)

# The following identifier represent function argument/local variable/object
# attribute names that should be redacted from the payload.
REDACTED_IDENTIFIERS = (
    frozenset(
        {
            "2fa",
            "accesstoken",
            "aiohttpsession",
            "apikey",
            "apisecret",
            "apisignature",
            "appkey",
            "applicationkey",
            "auth",
            "authorization",
            "authtoken",
            "ccnumber",
            "certificatepin",
            "cipher",
            "clientid",
            "clientsecret",
            "connectionstring",
            "connectsid",
            "cookie",
            "credentials",
            "creditcard",
            "csrf",
            "csrftoken",
            "cvv",
            "databaseurl",
            "dburl",
            "encryptionkey",
            "encryptionkeyid",
            "geolocation",
            "gpgkey",
            "ipaddress",
            "jti",
            "jwt",
            "licensekey",
            "masterkey",
            "mysqlpwd",
            "nonce",
            "oauth",
            "oauthtoken",
            "otp",
            "passhash",
            "passwd",
            "password",
            "passwordb",
            "pemfile",
            "pgpkey",
            "phpsessid",
            "pin",
            "pincode",
            "pkcs8",
            "privatekey",
            "publickey",
            "pwd",
            "recaptchakey",
            "refreshtoken",
            "routingnumber",
            "salt",
            "secret",
            "secretkey",
            "secrettoken",
            "securityanswer",
            "securitycode",
            "securityquestion",
            "serviceaccountcredentials",
            "session",
            "sessionid",
            "sessionkey",
            "setcookie",
            "signature",
            "signaturekey",
            "sshkey",
            "ssn",
            "symfony",
            "token",
            "transactionid",
            "twiliotoken",
            "usersession",
            "voterid",
            "xapikey",
            "xauthtoken",
            "xcsrftoken",
            "xforwardedfor",
            "xrealip",
            "xsrf",
            "xsrftoken",
        }
    )
    | config.redacted_identifiers
)


REDACTED_PLACEHOLDER = r"{redacted}"


@cached()
def redact(ident: str) -> bool:
    normalized = normalize_ident(ident)
    return normalized in REDACTED_IDENTIFIERS and normalized not in config.redaction_excluded_identifiers


@cached()
def redact_type(_type: str) -> bool:
    _re = config.redacted_types_re
    if _re is None:
        return False
    return _re.search(_type) is not None


class DDRedactedExpressionError(Exception):
    pass


class DDRedactedCompiler(DDCompiler):
    @classmethod
    def __getmember__(cls, s, a):
        if redact(a):
            raise DDRedactedExpressionError(f"Access to attribute {a!r} is not allowed")

        return super().__getmember__(s, a)

    @classmethod
    def __index__(cls, o, i):
        if isinstance(i, str) and redact(i):
            raise DDRedactedExpressionError(f"Access to entry {i!r} is not allowed")

        return super().__index__(o, i)

    @classmethod
    def __ref__(cls, s):
        if redact(s):
            raise DDRedactedExpressionError(f"Access to local {s!r} is not allowed")

        return s


dd_compile_redacted = DDRedactedCompiler().compile


def _redacted_expr(exc):
    def _(_):
        raise exc

    return _


class DDRedactedExpression(DDExpression):
    __compiler__ = dd_compile_redacted

    @classmethod
    def on_compiler_error(cls, dsl, exc):
        if isinstance(exc, DDRedactedExpressionError):
            log.error("Cannot compile expression that references potential PII: %s", dsl, exc_info=True)
            return _redacted_expr(exc)
        return super().on_compiler_error(dsl, exc)
