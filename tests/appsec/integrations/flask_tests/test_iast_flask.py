import json
import traceback

from flask import request
import pytest

from ddtrace.appsec._constants import IAST
from ddtrace.appsec._iast._iast_request_context_base import _iast_start_request
from ddtrace.appsec._iast._overhead_control_engine import oce
from ddtrace.appsec._iast._patches.json_tainting import patch as patch_json
from ddtrace.appsec._iast._taint_tracking._taint_objects_base import is_pyobject_tainted
from ddtrace.appsec._iast.constants import VULN_INSECURE_COOKIE
from ddtrace.appsec._iast.constants import VULN_NO_HTTPONLY_COOKIE
from ddtrace.appsec._iast.constants import VULN_NO_SAMESITE_COOKIE
from ddtrace.appsec._iast.constants import VULN_SQL_INJECTION
from ddtrace.appsec._iast.constants import VULN_STACKTRACE_LEAK
from ddtrace.appsec._iast.constants import VULN_UNVALIDATED_REDIRECT
from ddtrace.appsec._iast.constants import VULN_XSS
from ddtrace.appsec._iast.taint_sinks.header_injection import patch as patch_header_injection
from ddtrace.appsec._iast.taint_sinks.insecure_cookie import patch as patch_insecure_cookie
from ddtrace.appsec._iast.taint_sinks.unvalidated_redirect import patch as patch_unvalidated_redirect
from ddtrace.appsec._iast.taint_sinks.xss import patch as patch_xss_injection
from ddtrace.contrib.internal.sqlite3.patch import patch as patch_sqlite_sqli
from ddtrace.settings.asm import config as asm_config
from tests.appsec.iast.iast_utils import get_line_and_hash
from tests.appsec.iast.iast_utils import load_iast_report
from tests.appsec.integrations.flask_tests.utils import flask_version
from tests.appsec.integrations.flask_tests.utils import werkzeug_version
from tests.contrib.flask import BaseFlaskTestCase
from tests.utils import override_global_config


TEST_FILE_PATH = "tests/appsec/integrations/flask_tests/test_iast_flask.py"


class FlaskAppSecIASTEnabledTestCase(BaseFlaskTestCase):
    @pytest.fixture(autouse=True)
    def inject_fixtures(self, caplog, telemetry_writer):  # noqa: F811
        self._telemetry_writer = telemetry_writer
        self._caplog = caplog

    def setUp(self):
        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            patch_sqlite_sqli()
            patch_insecure_cookie()
            patch_header_injection()
            patch_xss_injection()
            patch_unvalidated_redirect()
            patch_json()
            super(FlaskAppSecIASTEnabledTestCase, self).setUp()
            self.tracer.configure(iast_enabled=True)
            oce.reconfigure()

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_http_request_path_parameter(self):
        @self.app.route("/sqli/<string:param_str>/", methods=["GET", "POST"])
        def sqli_1(param_str):
            import sqlite3

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            assert is_pyobject_tainted(param_str)
            con = sqlite3.connect(":memory:")
            cur = con.cursor()
            # label test_flask_full_sqli_iast_http_request_path_parameter
            cur.execute(add_aspect("SELECT 1 FROM ", param_str))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.post("/sqli/sqlite_master/", data={"name": "test"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.path.parameter", "name": "param_str", "value": "sqlite_master"}
            ]

            line, hash_value = get_line_and_hash(
                "test_flask_full_sqli_iast_http_request_path_parameter", VULN_SQL_INJECTION, filename=TEST_FILE_PATH
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT "},
                    {"redacted": True},
                    {"value": " FROM "},
                    {"value": "sqlite_master", "source": 0},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"]
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_enabled_http_request_header_getitem(self):
        @self.app.route("/sqli/<string:param_str>/", methods=["GET", "POST"])
        def sqli_2(param_str):
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()
            # label test_flask_full_sqli_iast_enabled_http_request_header_getitem
            cur.execute(add_aspect("SELECT 1 FROM ", request.headers["User-Agent"]))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.post(
                "/sqli/sqlite_master/", data={"name": "test"}, headers={"User-Agent": "sqlite_master"}
            )
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.header", "name": "User-Agent", "value": "sqlite_master"}
            ]

            line, hash_value = get_line_and_hash(
                "test_flask_full_sqli_iast_enabled_http_request_header_getitem",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]

            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT "},
                    {"redacted": True},
                    {"value": " FROM "},
                    {"value": "sqlite_master", "source": 0},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_2"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_iast_enabled_http_request_header_get(self):
        @self.app.route("/sqli/<string:param_str>/", methods=["GET", "POST"])
        def sqli_2(param_str):
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()
            # label test_flask_iast_enabled_http_request_header_get
            cur.execute(add_aspect("SELECT 1 FROM ", request.headers.get("User-Agent")))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.post(
                "/sqli/sqlite_master/", data={"name": "test"}, headers={"User-Agent": "sqlite_master"}
            )
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.header", "name": "User-Agent", "value": "sqlite_master"}
            ]

            line, hash_value = get_line_and_hash(
                "test_flask_iast_enabled_http_request_header_get",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]

            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT "},
                    {"redacted": True},
                    {"value": " FROM "},
                    {"value": "sqlite_master", "source": 0},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_2"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_enabled_http_request_header_name_keys(self):
        @self.app.route("/sqli/<string:param_str>/", methods=["GET", "POST"])
        def sqli_3(param_str):
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            # Test to consume request.header.keys twice
            _ = [k for k in request.headers.keys() if k == "Master"][0]
            header_name = [k for k in request.headers.keys() if k == "Master"][0]
            # label test_flask_full_sqli_iast_enabled_http_request_header_name_keys
            cur.execute(add_aspect("SELECT 1 FROM sqlite_", header_name))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
            )
        ):
            resp = self.client.post("/sqli/sqlite_master/", data={"name": "test"}, headers={"master": "not_user_agent"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [{"origin": "http.request.header.name", "name": "Master", "value": "Master"}]

            line, hash_value = get_line_and_hash(
                "test_flask_full_sqli_iast_enabled_http_request_header_name_keys",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT "},
                    {"redacted": True},
                    {"value": " FROM sqlite_"},
                    {"value": "Master", "source": 0},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_3"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_enabled_http_request_header_values(self):
        @self.app.route("/sqli/<string:param_str>/", methods=["GET", "POST"])
        def sqli_4(param_str):
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            header = [k for k in request.headers.values() if k == "master"][0]
            # label test_flask_full_sqli_iast_enabled_http_request_header_values
            cur.execute(add_aspect("SELECT 1 FROM sqlite_", header))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
            )
        ):
            resp = self.client.post("/sqli/sqlite_master/", data={"name": "test"}, headers={"user-agent": "master"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [{"origin": "http.request.header", "name": "User-Agent", "value": "master"}]

            line, hash_value = get_line_and_hash(
                "test_flask_full_sqli_iast_enabled_http_request_header_values",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT "},
                    {"redacted": True},
                    {"value": " FROM sqlite_"},
                    {"value": "master", "source": 0},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_4"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    def test_flask_simple_iast_path_header_and_querystring_tainted(self):
        @self.app.route("/sqli/<string:param_str>/<int:param_int>/", methods=["GET", "POST"])
        def sqli_5(param_str, param_int):
            from flask import request

            from ddtrace.appsec._iast._taint_tracking import OriginType
            from ddtrace.appsec._iast._taint_tracking._taint_objects_base import get_tainted_ranges

            header_ranges = get_tainted_ranges(request.headers["User-Agent"])
            assert header_ranges
            assert header_ranges[0].source.name.lower() == "user-agent"
            assert header_ranges[0].has_origin(OriginType.HEADER)

            if flask_version > (2, 0):
                query_string_ranges = get_tainted_ranges(request.query_string)
                assert query_string_ranges
                assert query_string_ranges[0].source.name == "http.request.query"
                assert query_string_ranges[0].has_origin(OriginType.QUERY)

                request_path_ranges = get_tainted_ranges(request.path)
                assert request_path_ranges
                assert request_path_ranges[0].source.name == "http.request.path"
                assert request_path_ranges[0].has_origin(OriginType.PATH)

            _ = get_tainted_ranges(param_str)
            assert not is_pyobject_tainted(param_int)

            request_form_name_ranges = get_tainted_ranges(request.form.get("name"))
            assert request_form_name_ranges
            assert request_form_name_ranges[0].source.name == "name"
            assert request_form_name_ranges[0].source.origin == OriginType.PARAMETER

            return request.query_string, 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.post("/sqli/hello/1000/?select%20from%20table", data={"name": "test"})
            assert resp.status_code == 200
            if hasattr(resp, "text"):
                # not all flask versions have r.text
                assert resp.text == "select%20from%20table"

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_simple_iast_path_header_and_querystring_tainted_request_sampling_0(self):
        @self.app.route("/sqli/<string:param_str>/", methods=["GET", "POST"])
        def sqli_6(param_str):
            from flask import request

            # Note: these are not tainted because of request sampling at 0%
            assert not is_pyobject_tainted(request.headers["User-Agent"])
            assert not is_pyobject_tainted(request.query_string)
            assert not is_pyobject_tainted(param_str)
            assert not is_pyobject_tainted(request.path)
            assert not is_pyobject_tainted(request.form.get("name"))

            return request.query_string, 200

        class MockSpan:
            _trace_id_64bits = 17577308072598193742

        with override_global_config(
            dict(_iast_enabled=True, _iast_deduplication_enabled=False, _iast_request_sampling=0.0)
        ):
            oce.reconfigure()
            _iast_start_request(MockSpan())
            resp = self.client.post("/sqli/hello/?select%20from%20table", data={"name": "test"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]

            assert root_span.get_metric(IAST.ENABLED) == 0.0

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_enabled_http_request_cookies_value(self):
        @self.app.route("/sqli/cookies/", methods=["GET", "POST"])
        def sqli_7():
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()
            # label test_flask_full_sqli_iast_enabled_http_request_cookies_value
            cur.execute(add_aspect("SELECT 1 FROM ", request.cookies.get("test-cookie1")))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            oce.reconfigure()

            if werkzeug_version >= (2, 3):
                self.client.set_cookie(domain="localhost", key="test-cookie1", value="sqlite_master")
            else:
                self.client.set_cookie(server_name="localhost", key="test-cookie1", value="sqlite_master")

            resp = self.client.post("/sqli/cookies/")
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.cookie.value", "name": "test-cookie1", "value": "sqlite_master"}
            ]

            line, hash_value = get_line_and_hash(
                "test_flask_full_sqli_iast_enabled_http_request_cookies_value",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = False
            for vuln in loaded["vulnerabilities"]:
                if vuln["type"] == VULN_SQL_INJECTION:
                    vulnerability = vuln

            assert vulnerability, "No {} reported".format(VULN_SQL_INJECTION)
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT "},
                    {"redacted": True},
                    {"value": " FROM "},
                    {"value": "sqlite_master", "source": 0},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_7"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_enabled_http_request_cookies_name(self):
        @self.app.route("/sqli/cookies/", methods=["GET", "POST"])
        def sqli_8():
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()
            key = [x for x in request.cookies.keys() if x == "sqlite_master"][0]
            # label test_flask_full_sqli_iast_enabled_http_request_cookies_name
            cur.execute(add_aspect("SELECT 1 FROM ", key))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            if werkzeug_version >= (2, 3):
                self.client.set_cookie(domain="localhost", key="sqlite_master", value="sqlite_master2")
            else:
                self.client.set_cookie(server_name="localhost", key="sqlite_master", value="sqlite_master2")

            resp = self.client.post("/sqli/cookies/")
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.cookie.name", "name": "sqlite_master", "value": "sqlite_master"}
            ]
            vulnerabilities = set()
            line, hash_value = get_line_and_hash(
                "test_flask_full_sqli_iast_enabled_http_request_cookies_name",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )

            for vulnerability in loaded["vulnerabilities"]:
                vulnerabilities.add(vulnerability["type"])
                if vulnerability["type"] == VULN_SQL_INJECTION:
                    assert vulnerability["type"] == VULN_SQL_INJECTION
                    assert vulnerability["evidence"] == {
                        "valueParts": [
                            {"value": "SELECT "},
                            {"redacted": True},
                            {"value": " FROM "},
                            {"value": "sqlite_master", "source": 0},
                        ]
                    }
                    assert vulnerability["location"]["line"] == line
                    assert vulnerability["location"]["path"] == TEST_FILE_PATH
                    assert vulnerability["location"]["method"] == "sqli_8"
                    assert "class" not in vulnerability["location"]
                    assert vulnerability["hash"] == hash_value

            assert {VULN_SQL_INJECTION} == vulnerabilities

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_http_request_parameter(self):
        @self.app.route("/sqli/parameter/", methods=["GET"])
        def sqli_9():
            import sqlite3

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()
            # label test_flask_full_sqli_iast_http_request_parameter
            cur.execute(add_aspect("SELECT 1 FROM ", request.args.get("table")))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.get("/sqli/parameter/?table=sqlite_master")
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.parameter", "name": "table", "value": "sqlite_master"}
            ]

            line, hash_value = get_line_and_hash(
                "test_flask_full_sqli_iast_http_request_parameter", VULN_SQL_INJECTION, filename=TEST_FILE_PATH
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT "},
                    {"redacted": True},
                    {"value": " FROM "},
                    {"value": "sqlite_master", "source": 0},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_9"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_http_request_parameter_name_post(self):
        @self.app.route("/sqli/", methods=["POST"])
        def sqli_13():
            import sqlite3

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            for i in request.form.keys():
                assert is_pyobject_tainted(i)

            first_param = list(request.form.keys())[0]

            con = sqlite3.connect(":memory:")
            cur = con.cursor()
            # label test_flask_full_sqli_iast_http_request_parameter_name_post
            cur.execute(add_aspect("SELECT 1 FROM ", first_param))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.post("/sqli/", data={"sqlite_master": "unused"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.parameter.name", "name": "sqlite_master", "value": "sqlite_master"}
            ]

            line, hash_value = get_line_and_hash(
                "test_flask_full_sqli_iast_http_request_parameter_name_post",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT "},
                    {"redacted": True},
                    {"value": " FROM "},
                    {"value": "sqlite_master", "source": 0},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_13"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_http_request_parameter_name_get(self):
        @self.app.route("/sqli/", methods=["GET"])
        def sqli_14():
            import sqlite3

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            for i in request.args.keys():
                assert is_pyobject_tainted(i)

            first_param = list(request.args.keys())[0]

            con = sqlite3.connect(":memory:")
            cur = con.cursor()
            # label test_flask_full_sqli_iast_http_request_parameter_name_get
            cur.execute(add_aspect("SELECT 1 FROM ", first_param))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.get("/sqli/", query_string={"sqlite_master": "unused"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.parameter.name", "name": "sqlite_master", "value": "sqlite_master"}
            ]

            line, hash_value = get_line_and_hash(
                "test_flask_full_sqli_iast_http_request_parameter_name_get",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT "},
                    {"redacted": True},
                    {"value": " FROM "},
                    {"value": "sqlite_master", "source": 0},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_14"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_request_body(self):
        @self.app.route("/sqli/body/", methods=("POST",))
        def sqli_10():
            import json
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()
            if flask_version > (2, 0):
                json_data = request.json
            else:
                json_data = json.loads(request.data)
            value = json_data.get("json_body")
            assert value == "master"

            assert is_pyobject_tainted(value)
            query = add_aspect(add_aspect("SELECT tbl_name FROM sqlite_", value), " WHERE tbl_name LIKE 'password'")
            # label test_flask_request_body
            cur.execute(query)

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.post(
                "/sqli/body/", data=json.dumps(dict(json_body="master")), content_type="application/json"
            )
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [{"name": "json_body", "origin": "http.request.body", "value": "master"}]

            line, hash_value = get_line_and_hash(
                "test_flask_request_body",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT tbl_name FROM sqlite_"},
                    {"value": "master", "source": 0},
                    {"value": " WHERE tbl_name LIKE '"},
                    {"redacted": True},
                    {"value": "'"},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_10"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_request_body_complex_3_lvls(self):
        @self.app.route("/sqli/body/", methods=("POST",))
        def sqli_11():
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            if flask_version > (2, 0):
                json_data = request.json
            else:
                json_data = json.loads(request.data)
            value = json_data.get("body").get("body2").get("body3")
            assert value == "master"
            assert is_pyobject_tainted(value)
            query = add_aspect(add_aspect("SELECT tbl_name FROM sqlite_", value), " WHERE tbl_name LIKE 'password'")
            # label test_flask_request_body_complex_3_lvls
            cur.execute(query)

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
            )
        ):
            resp = self.client.post(
                "/sqli/body/",
                data=json.dumps(dict(body=dict(body2=dict(body3="master")))),
                content_type="application/json",
            )
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [{"name": "body3", "origin": "http.request.body", "value": "master"}]

            line, hash_value = get_line_and_hash(
                "test_flask_request_body_complex_3_lvls",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT tbl_name FROM sqlite_"},
                    {"value": "master", "source": 0},
                    {"value": " WHERE tbl_name LIKE '"},
                    {"redacted": True},
                    {"value": "'"},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_11"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_request_body_complex_3_lvls_and_list(self):
        @self.app.route("/sqli/body/", methods=("POST",))
        def sqli_11():
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            if flask_version > (2, 0):
                json_data = request.json
            else:
                json_data = json.loads(request.data)
            value = json_data.get("body").get("body2").get("body3")[3]
            assert value == "master"
            assert is_pyobject_tainted(value)
            query = add_aspect(add_aspect("SELECT tbl_name FROM sqlite_", value), " WHERE tbl_name LIKE 'password'")
            # label test_flask_request_body_complex_3_lvls_and_list
            cur.execute(query)

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
            )
        ):
            resp = self.client.post(
                "/sqli/body/",
                data=json.dumps(dict(body=dict(body2=dict(body3=["master3", "master2", "master1", "master"])))),
                content_type="application/json",
            )
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [{"name": "body3", "origin": "http.request.body", "value": "master"}]

            line, hash_value = get_line_and_hash(
                "test_flask_request_body_complex_3_lvls_and_list",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT tbl_name FROM sqlite_"},
                    {"value": "master", "source": 0},
                    {"value": " WHERE tbl_name LIKE '"},
                    {"redacted": True},
                    {"value": "'"},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_11"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_request_body_complex_3_lvls_list_dict(self):
        @self.app.route("/sqli/body/", methods=("POST",))
        def sqli_11():
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            if flask_version > (2, 0):
                json_data = request.json
            else:
                json_data = json.loads(request.data)
            value = json_data.get("body").get("body2").get("body3")[3].get("body4")
            assert value == "master"
            assert is_pyobject_tainted(value)
            query = add_aspect(add_aspect("SELECT tbl_name FROM sqlite_", value), " WHERE tbl_name LIKE 'password'")
            # label test_flask_request_body_complex_3_lvls_list_dict
            cur.execute(query)

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
            )
        ):
            resp = self.client.post(
                "/sqli/body/",
                data=json.dumps(
                    dict(body=dict(body2=dict(body3=["master3", "master2", "master1", {"body4": "master"}])))
                ),
                content_type="application/json",
            )
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [{"name": "body4", "origin": "http.request.body", "value": "master"}]

            line, hash_value = get_line_and_hash(
                "test_flask_request_body_complex_3_lvls_list_dict",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT tbl_name FROM sqlite_"},
                    {"value": "master", "source": 0},
                    {"value": " WHERE tbl_name LIKE '"},
                    {"redacted": True},
                    {"value": "'"},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_11"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_request_body_complex_json_all_types_of_values(self):
        @self.app.route("/sqli/body/", methods=("POST",))
        def sqli_11():
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            def iterate_json(data, parent_key=""):
                if isinstance(data, dict):
                    for key, value in data.items():
                        iterate_json(value, key)
                elif isinstance(data, list):
                    for index, item in enumerate(data):
                        iterate_json(item, parent_key)
                else:
                    assert is_pyobject_tainted(parent_key), f"{parent_key} taint error"
                    if isinstance(data, str):
                        assert is_pyobject_tainted(data), f"{parent_key}.{data} taint error"
                    else:
                        assert not is_pyobject_tainted(data), f"{parent_key}.{data} taint error"

            if flask_version > (2, 0):
                request_json = request.json
            else:
                request_json = json.loads(request.data)

            iterate_json(request_json)

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            value = request_json.get("user").get("profile").get("preferences").get("extra")
            assert value == "master"
            assert is_pyobject_tainted(value)
            query = add_aspect(add_aspect("SELECT tbl_name FROM sqlite_", value), " WHERE tbl_name LIKE 'password'")
            # label test_flask_request_body_complex_json_all_types_of_values
            cur.execute(query)

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
            )
        ):
            # random json with all kind of types
            json_data = {
                "user": {
                    "id": 12345,
                    "name": "John Doe",
                    "email": "johndoe@example.com",
                    "profile": {
                        "age": 30,
                        "gender": "male",
                        "preferences": {
                            "language": "English",
                            "timezone": "GMT+0",
                            "notifications": True,
                            "theme": "dark",
                            "extra": "master",
                        },
                        "social_links": ["https://twitter.com/johndoe", "https://github.com/johndoe"],
                    },
                },
                "settings": {
                    "volume": 80,
                    "brightness": 50,
                    "wifi": {
                        "enabled": True,
                        "networks": [
                            {"ssid": "HomeNetwork", "signal_strength": -40, "secured": True},
                            {"ssid": "WorkNetwork", "signal_strength": -60, "secured": False},
                        ],
                    },
                },
                "tasks": [
                    {"task_id": 1, "title": "Finish project report", "due_date": "2024-08-25", "completed": False},
                    {
                        "task_id": 2,
                        "title": "Buy groceries",
                        "due_date": "2024-08-23",
                        "completed": True,
                        "items": ["milk", "bread", "eggs"],
                    },
                ],
                "random_values": [
                    42,
                    "randomString",
                    True,
                    None,
                    [3.14, 2.71, 1.618],
                    {"nested_key": "nestedValue", "nested_number": 999, "nested_array": [1, "two", None]},
                ],
                "system": {
                    "os": "Linux",
                    "version": "5.10",
                    "uptime": 1234567,
                    "processes": {"running": 345, "sleeping": 56, "stopped": 2},
                },
            }

            resp = self.client.post(
                "/sqli/body/",
                data=json.dumps(json_data),
                content_type="application/json",
            )
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [{"name": "extra", "origin": "http.request.body", "value": "master"}]

            line, hash_value = get_line_and_hash(
                "test_flask_request_body_complex_json_all_types_of_values",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT tbl_name FROM sqlite_"},
                    {"value": "master", "source": 0},
                    {"value": " WHERE tbl_name LIKE '"},
                    {"redacted": True},
                    {"value": "'"},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_11"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_request_body_iast_and_appsec(self):
        """Verify IAST, Appsec and API security work correctly running at the same time"""

        @self.app.route("/sqli/body/", methods=("POST",))
        def sqli_10():
            import json
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()
            if flask_version > (2, 0):
                json_data = request.json
            else:
                json_data = json.loads(request.data)
            value = json_data.get("json_body")
            assert value == "master"

            assert is_pyobject_tainted(value)
            query = add_aspect(add_aspect("SELECT tbl_name FROM sqlite_", value), " WHERE tbl_name LIKE 'password'")
            # label test_flask_request_body
            cur.execute(query)

            return {"Response": value}, 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _asm_enabled=True,
                _api_security_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.post(
                "/sqli/body/", data=json.dumps(dict(json_body="master")), content_type="application/json"
            )
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [{"name": "json_body", "origin": "http.request.body", "value": "master"}]

            list_metrics_logs = list(self._telemetry_writer._logs)
            assert len(list_metrics_logs) == 0

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_enabled_http_request_header_values_scrubbed(self):
        @self.app.route("/sqli/<string:param_str>/", methods=["GET", "POST"])
        def sqli_12(param_str):
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            header = [k for k in request.headers.values() if k == "master"][0]
            query = add_aspect(add_aspect("SELECT tbl_name FROM sqlite_", header), " WHERE tbl_name LIKE 'password'")
            # label test_flask_full_sqli_iast_enabled_http_request_header_values_scrubbed
            cur.execute(query)

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
            )
        ):
            resp = self.client.post("/sqli/sqlite_master/", data={"name": "test"}, headers={"user-agent": "master"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [{"origin": "http.request.header", "name": "User-Agent", "value": "master"}]

            line, hash_value = get_line_and_hash(
                "test_flask_full_sqli_iast_enabled_http_request_header_values_scrubbed",
                VULN_SQL_INJECTION,
                filename=TEST_FILE_PATH,
            )
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_SQL_INJECTION
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "SELECT tbl_name FROM sqlite_"},
                    {"value": "master", "source": 0},
                    {"value": " WHERE tbl_name LIKE '"},
                    {"redacted": True},
                    {"value": "'"},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "sqli_12"
            assert "class" not in vulnerability["location"]
            assert vulnerability["hash"] == hash_value

    def test_flask_header_injection(self):
        """Test header injection vulnerability detection in Flask test client.

        This test works specifically because we're using Flask's test client, which has a different
        header handling mechanism than a real Flask application. In the test client, setting
        resp.headers["Header-Injection"] directly manipulates the headers without calling
        werkzeug.datastructures.headers._str_header_value. In a real application, that method
        would be called and it would sanitize or raise an exception for invalid header values.

        This test is valuable for verifying the header injection vulnerability detection logic,
        but it's important to note that exploiting this in a real Flask application would be more
        difficult due to Werkzeug's header value sanitization.
        """

        @self.app.route("/header_injection/", methods=["GET", "POST"])
        def header_injection():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")
            resp.headers["Vary"] = tainted_string

            resp.headers["Header-Injection"] = tainted_string
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.post("/header_injection/", data={"name": "test"})
            assert resp.status_code == 200
            assert resp.headers["Header-Injection"] == "test"

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0
            assert load_iast_report(root_span) is None

    def test_flask_header_injection_direct_access_to_header(self):
        @self.app.route("/header_injection_insecure/", methods=["GET", "POST"])
        def header_injection():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")
            resp.headers._list.append(("Header-Injection", tainted_string))
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.post("/header_injection_insecure/", data={"name": "test"})
            assert resp.status_code == 200
            assert resp.headers["Header-Injection"] == "test"

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            assert load_iast_report(root_span) is None

    def test_flask_header_injection_direct_access_to_header_exception(self):
        @self.app.route("/header_injection_insecure/", methods=["GET", "POST"])
        def header_injection():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")
            # resp.headers["Vary"] = tainted_string

            # label test_flask_header_injection_label
            resp.headers._list.append(("Header-Injection", tainted_string))
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            if werkzeug_version <= (2, 0, 3):
                self.client.post("/header_injection_insecure/", data={"name": "test\r\nInjected-Header: 1234"})
            else:
                with pytest.raises(ValueError):
                    self.client.post("/header_injection_insecure/", data={"name": "test\r\nInjected-Header: 1234"})

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0
            assert load_iast_report(root_span) is None

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_header_injection_exclusions_transfer_encoding(self):
        @self.app.route("/header_injection/", methods=["GET", "POST"])
        def header_injection():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")
            resp.headers["Transfer-Encoding"] = tainted_string
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.post("/header_injection/", data={"name": "test"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            assert load_iast_report(root_span) is None

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_header_injection_exclusions_access_control(self):
        @self.app.route("/header_injection/", methods=["GET", "POST"])
        def header_injection():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")
            resp.headers["Access-Control-Allow-Example1"] = tainted_string
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.post("/header_injection/", data={"name": "test"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            assert load_iast_report(root_span) is None

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_insecure_cookie(self):
        @self.app.route("/insecure_cookie/", methods=["GET", "POST"])
        def insecure_cookie():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")

            # label test_flask_insecure_cookie
            resp.set_cookie("insecure", "cookie", secure=False, httponly=True, samesite="Strict")
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.post("/insecure_cookie/", data={"name": "test"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == []
            assert len(loaded["vulnerabilities"]) == 1
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_INSECURE_COOKIE
            assert vulnerability["evidence"] == {"valueParts": [{"value": "insecure"}]}
            assert "method" in vulnerability["location"].keys()
            assert "class" not in vulnerability["location"].keys()
            assert vulnerability["location"]["spanId"]
            assert vulnerability["hash"]
            line, hash_value = get_line_and_hash(
                "test_flask_insecure_cookie", VULN_INSECURE_COOKIE, filename=TEST_FILE_PATH
            )
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_insecure_cookie_empty(self):
        @self.app.route("/insecure_cookie_empty/", methods=["GET", "POST"])
        def insecure_cookie_empty():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")
            resp.set_cookie("insecure", "", secure=False, httponly=True, samesite="Strict")
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.post("/insecure_cookie_empty/", data={"name": "test"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded is None

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_no_http_only_cookie(self):
        @self.app.route("/no_http_only_cookie/", methods=["GET", "POST"])
        def no_http_only_cookie():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")

            # label test_flask_no_http_only_cookie
            resp.set_cookie("insecure", "cookie", secure=True, httponly=False, samesite="Strict")
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.post("/no_http_only_cookie/", data={"name": "test"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == []
            assert len(loaded["vulnerabilities"]) == 1
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_NO_HTTPONLY_COOKIE
            assert vulnerability["evidence"] == {"valueParts": [{"value": "insecure"}]}
            assert vulnerability["location"]["spanId"]
            assert vulnerability["hash"]
            line, hash_value = get_line_and_hash(
                "test_flask_no_http_only_cookie", VULN_NO_HTTPONLY_COOKIE, filename=TEST_FILE_PATH
            )
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_no_http_only_cookie_empty(self):
        @self.app.route("/no_http_only_cookie_empty/", methods=["GET", "POST"])
        def no_http_only_cookie_empty():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")
            resp.set_cookie("insecure", "", secure=True, httponly=False, samesite="Strict")
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.post("/no_http_only_cookie_empty/", data={"name": "test"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded is None

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_no_samesite_cookie(self):
        @self.app.route("/no_samesite_cookie/", methods=["GET", "POST"])
        def no_samesite_cookie():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")

            # label test_flask_no_samesite_cookie
            resp.set_cookie("insecure", "cookie", secure=True, httponly=True, samesite="None")
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.post("/no_samesite_cookie/", data={"name": "test"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == []
            assert len(loaded["vulnerabilities"]) == 1
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_NO_SAMESITE_COOKIE
            assert vulnerability["evidence"] == {"valueParts": [{"value": "insecure"}]}
            assert "method" in vulnerability["location"].keys()
            assert vulnerability["location"]["spanId"]
            assert vulnerability["hash"]
            line, hash_value = get_line_and_hash(
                "test_flask_no_samesite_cookie", VULN_NO_SAMESITE_COOKIE, filename=TEST_FILE_PATH
            )
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_no_samesite_cookie_empty(self):
        @self.app.route("/no_samesite_cookie_empty/", methods=["GET", "POST"])
        def no_samesite_cookie_empty():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")
            resp.set_cookie("insecure", "", secure=True, httponly=True, samesite="None")
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
            )
        ):
            resp = self.client.post("/no_samesite_cookie_empty/", data={"name": "test"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            loaded = load_iast_report(root_span)
            assert loaded is None

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_cookie_secure(self):
        @self.app.route("/cookie_secure/", methods=["GET", "POST"])
        def cookie_secure():
            from flask import Response
            from flask import request

            tainted_string = request.form.get("name")
            assert is_pyobject_tainted(tainted_string)
            resp = Response("OK")
            resp.set_cookie("insecure", "cookie", secure=True, httponly=True, samesite="Strict")
            return resp

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.post("/cookie_secure/", data={"name": "test"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded is None

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_stacktrace_leak(self):
        @self.app.route("/stacktrace_leak/")
        def stacktrace_leak():
            from flask import Response

            return Response(
                """Traceback (most recent call last):
  File "/usr/local/lib/python3.9/site-packages/some_module.py", line 42, in process_data
    result = complex_calculation(data)
  File "/usr/local/lib/python3.9/site-packages/another_module.py", line 158, in complex_calculation
    intermediate = perform_subtask(data_slice)
  File "/usr/local/lib/python3.9/site-packages/subtask_module.py", line 27, in perform_subtask
    processed = handle_special_case(data_slice)
  File "/usr/local/lib/python3.9/site-packages/special_cases.py", line 84, in handle_special_case
    return apply_algorithm(data_slice, params)
  File "/usr/local/lib/python3.9/site-packages/algorithm_module.py", line 112, in apply_algorithm
    step_result = execute_step(data, params)
  File "/usr/local/lib/python3.9/site-packages/step_execution.py", line 55, in execute_step
    temp = pre_process(data)
  File "/usr/local/lib/python3.9/site-packages/pre_processing.py", line 33, in pre_process
    validated_data = validate_input(data)
  File "/usr/local/lib/python3.9/site-packages/validation.py", line 66, in validate_input
    check_constraints(data)
  File "/usr/local/lib/python3.9/site-packages/constraints.py", line 19, in check_constraints
    raise ValueError("Constraint violation at step 9")
ValueError: Constraint violation at step 9

Lorem Ipsum Foobar
"""
            )

        with override_global_config(
            dict(
                _iast_enabled=True,
                _deduplication_enabled=False,
            )
        ):
            resp = self.client.get("/stacktrace_leak/")
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == []
            assert len(loaded["vulnerabilities"]) == 1
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_STACKTRACE_LEAK
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": 'Module: ".usr.local.lib.python3.9.site-packages.constraints.py"\nException: ValueError'}
                ]
            }

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_stacktrace_leak_from_debug_page(self):
        try:
            from werkzeug.debug.tbtools import DebugTraceback
        except ImportError:
            return  # this version of werkzeug does not have the DebugTraceback

        @self.app.route("/stacktrace_leak_debug/")
        def stacktrace_leak():
            from flask import Response

            try:
                raise ValueError()
            except ValueError as exc:
                dt = DebugTraceback(
                    exc,
                    traceback.TracebackException.from_exception(exc),
                )

                # Render the debugger HTML
                html = dt.render_debugger_html(evalex=False, secret="test_secret", evalex_trusted=False)
                return Response(html, mimetype="text/html")

        with override_global_config(
            dict(
                _iast_enabled=True,
                _deduplication_enabled=False,
            )
        ):
            resp = self.client.get("/stacktrace_leak_debug/")
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == []
            assert len(loaded["vulnerabilities"]) == 1
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_STACKTRACE_LEAK
            assert "valueParts" in vulnerability["evidence"]
            assert (
                "tests.appsec.integrations.flask_tests.test_iast_flask"
                in vulnerability["evidence"]["valueParts"][0]["value"]
            )
            assert "Exception: ValueError" in vulnerability["evidence"]["valueParts"][0]["value"]

    def test_flask_xss(self):
        @self.app.route("/xss/", methods=["GET"])
        def xss_view():
            from flask import render_template_string
            from flask import request

            user_input = request.args.get("input", "")

            # label test_flask_xss
            return render_template_string("<p>XSS: {{ user_input|safe }}</p>", user_input=user_input)

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.get("/xss/?input=<script>alert('XSS')</script>")
            assert resp.status_code == 200
            assert resp.data == b"<p>XSS: <script>alert('XSS')</script></p>"

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.parameter", "name": "input", "value": "<script>alert('XSS')</script>"}
            ]

            line, hash_value = get_line_and_hash("test_flask_xss", VULN_XSS, filename=TEST_FILE_PATH)
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_XSS
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "<script>alert('XSS')</script>", "source": 0},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "xss_view"
            assert "class" not in vulnerability["location"]

    def test_flask_unvalidated_redirect(self):
        @self.app.route("/unvalidated_redirect/", methods=["GET"])
        def unvalidated_redirect_view():
            from flask import redirect
            from flask import request

            url = request.args.get("url", "")

            # label test_flask_unvalidated_redirect
            return redirect(location=url)

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.get("/unvalidated_redirect/?url=http://localhost:8080/malicious")
            assert resp.status_code == 302
            assert b"Redirecting..." in resp.data

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.parameter", "name": "url", "value": "http://localhost:8080/malicious"}
            ]

            get_line_and_hash("test_flask_unvalidated_redirect", VULN_UNVALIDATED_REDIRECT, filename=TEST_FILE_PATH)
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_UNVALIDATED_REDIRECT
            assert vulnerability["evidence"] == {
                "valueParts": [{"source": 0, "value": "http://localhost:8080/malicious"}]
            }
            # TODO: This test fails in the CI in some scenarios with with this location:
            #  {'spanId': 2149503346182698386, 'path': 'tests/contrib/flask/__init__.py', 'line': 21, 'method': 'open'}
            # assert vulnerability["location"]["path"] == TEST_FILE_PATH
            # assert vulnerability["location"]["line"] == line
            # assert vulnerability["location"]["method"] == "unvalidated_redirect_view"
            # assert vulnerability["location"].get("stackId") == "1", f"Wrong Vulnerability stackId {vulnerability}"
            # assert "class" not in vulnerability["location"]

    def test_flask_unvalidated_redirect_headers(self):
        @self.app.route("/unvalidated_redirect_headers/", methods=["GET"])
        def unvalidated_redirect_headers_view():
            from flask import Response

            url = request.args.get("url", "")

            response = Response("OK")
            response.headers["Location"] = url
            return response

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.get("/unvalidated_redirect_headers/?url=http://localhost:8080/malicious")
            assert resp.status_code == 200
            assert b"OK" in resp.data

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.parameter", "name": "url", "value": "http://localhost:8080/malicious"}
            ]

            get_line_and_hash("test_flask_unvalidated_redirect", VULN_UNVALIDATED_REDIRECT, filename=TEST_FILE_PATH)
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_UNVALIDATED_REDIRECT
            assert vulnerability["evidence"] == {
                "valueParts": [{"source": 0, "value": "http://localhost:8080/malicious"}]
            }

    def test_flask_xss_concat(self):
        @self.app.route("/xss/concat/", methods=["GET"])
        def xss_view():
            from flask import render_template_string
            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            user_input = request.args.get("input", "")

            # label test_flask_xss_concat
            return render_template_string(add_aspect(add_aspect("<p>XSS: ", user_input), "</p>"))

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.get("/xss/concat/?input=<script>alert('XSS')</script>")
            assert resp.status_code == 200
            assert resp.data == b"<p>XSS: <script>alert('XSS')</script></p>"

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.parameter", "name": "input", "value": "<script>alert('XSS')</script>"}
            ]

            line, hash_value = get_line_and_hash("test_flask_xss_concat", VULN_SQL_INJECTION, filename=TEST_FILE_PATH)
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_XSS
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "<p>XSS: "},
                    {"source": 0, "value": "<script>alert('XSS')</script>"},
                    {"value": "</p>"},
                ]
            }
            assert vulnerability["location"]["line"] == line
            assert vulnerability["location"]["path"] == TEST_FILE_PATH
            assert vulnerability["location"]["method"] == "xss_view"
            assert "class" not in vulnerability["location"]

    def test_flask_xss_template_secure(self):
        @self.app.route("/xss/template/secure/", methods=["GET"])
        def xss_view_template():
            from flask import render_template
            from flask import request

            user_input = request.args.get("input", "")

            # label test_flask_xss_template
            return render_template("test.html", world=user_input)

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.get("/xss/template/secure/?input=<script>alert('XSS')</script>")
            assert resp.status_code == 200
            assert resp.data == b"hello &lt;script&gt;alert(&#39;XSS&#39;)&lt;/script&gt;"

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            assert load_iast_report(root_span) is None

    def test_flask_xss_template(self):
        @self.app.route("/xss/template/", methods=["GET"])
        def xss_view_template():
            from flask import render_template
            from flask import request

            user_input = request.args.get("input", "")

            # label test_flask_xss_template
            return render_template("test_insecure.html", world=user_input)

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            resp = self.client.get("/xss/template/?input=<script>alert('XSS')</script>")
            assert resp.status_code == 200
            assert resp.data == b"hello <script>alert('XSS')</script>"

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) == 1.0

            loaded = load_iast_report(root_span)
            assert loaded["sources"] == [
                {"origin": "http.request.parameter", "name": "input", "value": "<script>alert('XSS')</script>"}
            ]

            line, hash_value = get_line_and_hash("test_flask_xss", VULN_SQL_INJECTION, filename=TEST_FILE_PATH)
            vulnerability = loaded["vulnerabilities"][0]
            assert vulnerability["type"] == VULN_XSS
            assert vulnerability["evidence"] == {
                "valueParts": [
                    {"value": "<script>alert('XSS')</script>", "source": 0},
                ]
            }
            assert vulnerability["location"]["path"] == "tests/contrib/flask/test_templates/test_insecure.html"

    def test_flask_iast_sampling(self):
        @self.app.route("/appsec/iast_sampling/", methods=["GET"])
        def test_sqli():
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            param_tainted = request.args.get("param", "")
            con = sqlite3.connect(":memory:")
            cursor = con.cursor()
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '1'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '2'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '3'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '4'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '5'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '6'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '7'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '8'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '9'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '10'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '11'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '12'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '13'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '14'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '15'  FROM sqlite_master"))
            cursor.execute(add_aspect(add_aspect("SELECT '", param_tainted), "', '16'  FROM sqlite_master"))

            return f"OK:{param_tainted}", 200

        with override_global_config(
            dict(
                _iast_enabled=True,
                _iast_deduplication_enabled=False,
                _iast_max_vulnerabilities_per_requests=2,
                _iast_request_sampling=100.0,
            )
        ):
            list_vulnerabilities = []
            for i in range(10):
                resp = self.client.get(f"/appsec/iast_sampling/?param=value{i}")
                assert resp.status_code == 200

                root_span = self.pop_spans()[0]
                assert str(resp.data, encoding="utf-8") == f"OK:value{i}", resp.data
                loaded = load_iast_report(root_span)
                if i < 8:
                    assert loaded, f"No data({i}): {loaded}"
                    assert len(loaded["vulnerabilities"]) == 2
                    assert loaded["sources"] == [
                        {"origin": "http.request.parameter", "name": "param", "redacted": True, "pattern": "abcdef"}
                    ]
                    for vuln in loaded["vulnerabilities"]:
                        assert vuln["type"] == VULN_SQL_INJECTION
                        list_vulnerabilities.append(vuln["location"]["line"])
                else:
                    assert loaded is None
            assert (
                len(list_vulnerabilities) == 16
            ), f"Num vulnerabilities: ({len(list_vulnerabilities)}): {list_vulnerabilities}"


class FlaskAppSecIASTDisabledTestCase(BaseFlaskTestCase):
    @pytest.fixture(autouse=True)
    def inject_fixtures(self, caplog):
        self._caplog = caplog

    def setUp(self):
        with override_global_config(
            dict(
                _iast_enabled=False,
                _iast_request_sampling=100.0,
            )
        ):
            super(FlaskAppSecIASTDisabledTestCase, self).setUp()
            # Hack: need to pass an argument to configure so that the processors are recreated
            self.tracer._recreate()

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_disabled_http_request_cookies_name(self):
        @self.app.route("/sqli/cookies/", methods=["GET", "POST"])
        def test_sqli():
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            key = [x for x in request.cookies.keys() if x == "sqlite_master"][0]
            cur.execute(add_aspect("SELECT 1 FROM ", key))

            return "OK", 200

        with override_global_config(dict(_iast_enabled=False)):
            if werkzeug_version >= (2, 3):
                self.client.set_cookie(domain="localhost", key="sqlite_master", value="sqlite_master3")
            else:
                self.client.set_cookie(server_name="localhost", key="sqlite_master", value="sqlite_master3")

            resp = self.client.post("/sqli/cookies/")
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) is None

            assert load_iast_report(root_span) is None

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_disabled_http_request_header_getitem(self):
        @self.app.route("/sqli/<string:param_str>/", methods=["GET", "POST"])
        def test_sqli(param_str):
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            cur.execute(add_aspect("SELECT 1 FROM ", request.headers["User-Agent"]))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=False,
            )
        ):
            resp = self.client.post(
                "/sqli/sqlite_master/", data={"name": "test"}, headers={"User-Agent": "sqlite_master"}
            )
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) is None

            assert load_iast_report(root_span) is None

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_disabled_http_request_header_name_keys(self):
        @self.app.route("/sqli/<string:param_str>/", methods=["GET", "POST"])
        def test_sqli(param_str):
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            header_name = [k for k in request.headers.keys() if k == "Master"][0]

            cur.execute(add_aspect("SELECT 1 FROM sqlite_", header_name))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=False,
            )
        ):
            resp = self.client.post("/sqli/sqlite_master/", data={"name": "test"}, headers={"master": "not_user_agent"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) is None

            assert load_iast_report(root_span) is None

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_disabled_http_request_header_values(self):
        @self.app.route("/sqli/<string:param_str>/", methods=["GET", "POST"])
        def test_sqli(param_str):
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            header = [k for k in request.headers.values() if k == "master"][0]

            cur.execute(add_aspect("SELECT 1 FROM sqlite_", header))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=False,
            )
        ):
            resp = self.client.post("/sqli/sqlite_master/", data={"name": "test"}, headers={"user-agent": "master"})
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) is None

            assert load_iast_report(root_span) is None

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_simple_iast_path_header_and_querystring_not_tainted_if_iast_disabled(self):
        @self.app.route("/sqli/<string:param_str>/", methods=["GET", "POST"])
        def test_sqli(param_str):
            from flask import request

            assert not is_pyobject_tainted(request.headers["User-Agent"])
            assert not is_pyobject_tainted(request.query_string)
            assert not is_pyobject_tainted(param_str)
            assert not is_pyobject_tainted(request.path)
            assert not is_pyobject_tainted(request.form.get("name"))
            return request.query_string, 200

        with override_global_config(
            dict(
                _iast_enabled=False,
            )
        ):
            resp = self.client.post("/sqli/hello/?select%20from%20table", data={"name": "test"})
            assert resp.status_code == 200
            if hasattr(resp, "text"):
                # not all flask versions have r.text
                assert resp.text == "select%20from%20table"

    @pytest.mark.skipif(not asm_config._iast_supported, reason="Python version not supported by IAST")
    def test_flask_full_sqli_iast_disabled_http_request_cookies_value(self):
        @self.app.route("/sqli/cookies/", methods=["GET", "POST"])
        def test_sqli():
            import sqlite3

            from flask import request

            from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect

            con = sqlite3.connect(":memory:")
            cur = con.cursor()

            cur.execute(add_aspect("SELECT 1 FROM ", request.cookies.get("test-cookie1")))

            return "OK", 200

        with override_global_config(
            dict(
                _iast_enabled=False,
            )
        ):
            if werkzeug_version >= (2, 3):
                self.client.set_cookie(domain="localhost", key="test-cookie1", value="sqlite_master")
            else:
                self.client.set_cookie(server_name="localhost", key="test-cookie1", value="sqlite_master")

            resp = self.client.post("/sqli/cookies/")
            assert resp.status_code == 200

            root_span = self.pop_spans()[0]
            assert root_span.get_metric(IAST.ENABLED) is None

            assert load_iast_report(root_span) is None
