from dataclasses import dataclass
import json
from pathlib import Path
import re
import typing as t
import weakref

import pytest

from ddtrace.contrib.internal.pytest.constants import ATR_MIN_SUPPORTED_VERSION
from ddtrace.contrib.internal.pytest.constants import ATTEMPT_TO_FIX_MIN_SUPPORTED_VERSION
from ddtrace.contrib.internal.pytest.constants import EFD_MIN_SUPPORTED_VERSION
from ddtrace.contrib.internal.pytest.constants import ITR_MIN_SUPPORTED_VERSION
from ddtrace.contrib.internal.pytest.constants import RETRIES_MIN_SUPPORTED_VERSION
from ddtrace.ext.test_visibility.api import TestExcInfo
from ddtrace.ext.test_visibility.api import TestId
from ddtrace.ext.test_visibility.api import TestModuleId
from ddtrace.ext.test_visibility.api import TestSourceFileInfo
from ddtrace.ext.test_visibility.api import TestStatus
from ddtrace.ext.test_visibility.api import TestSuiteId
from ddtrace.internal.ci_visibility.constants import ITR_UNSKIPPABLE_REASON
from ddtrace.internal.ci_visibility.utils import get_source_lines_for_test_method
from ddtrace.internal.logger import get_logger
from ddtrace.internal.test_visibility.api import InternalTest
from ddtrace.internal.utils.cache import cached
from ddtrace.internal.utils.formats import asbool
from ddtrace.internal.utils.inspection import undecorated
from ddtrace.settings._config import _get_config


log = get_logger(__name__)

_NODEID_REGEX = re.compile("^(((?P<module>.*)/)?(?P<suite>[^/]*?))::(?P<name>.*?)$")

_USE_PLUGIN_V2 = not _get_config("_DD_PYTEST_USE_LEGACY_PLUGIN", False, asbool)


class _PYTEST_STATUS:
    ERROR = "error"
    FAILED = "failed"
    PASSED = "passed"
    SKIPPED = "skipped"


PYTEST_STATUS = _PYTEST_STATUS()


class TestPhase:
    SETUP = "setup"
    CALL = "call"
    TEARDOWN = "teardown"


@dataclass
class TestNames:
    module: str
    suite: str
    test: str


def _encode_test_parameter(parameter: t.Any) -> str:
    param_repr = repr(parameter)
    # if the representation includes an id() we'll remove it
    # because it isn't constant across executions
    return re.sub(r" at 0[xX][0-9a-fA-F]+", "", param_repr)


def _get_names_from_item(item: pytest.Item) -> TestNames:
    """Gets an item's module, suite, and test names by leveraging the plugin hooks"""

    matches = re.match(_NODEID_REGEX, item.nodeid)
    if not matches:
        return TestNames(module="unknown_module", suite="unknown_suite", test=item.name)

    module_name = (matches.group("module") or "").replace("/", ".")
    suite_name = matches.group("suite")
    test_name = matches.group("name")

    return TestNames(module=module_name, suite=suite_name, test=test_name)


@cached()
def _get_test_id_from_item(item: pytest.Item) -> TestId:
    """Converts an item to a CITestId, which recursively includes the parent IDs

    NOTE: it is mandatory that the session, module, suite, and test IDs for a given test and parameters combination
    be stable across test runs.
    """

    module_name = item.config.hook.pytest_ddtrace_get_item_module_name(item=item)
    suite_name = item.config.hook.pytest_ddtrace_get_item_suite_name(item=item)
    test_name = item.config.hook.pytest_ddtrace_get_item_test_name(item=item)

    module_id = TestModuleId(module_name)
    suite_id = TestSuiteId(module_id, suite_name)

    test_id = TestId(suite_id, test_name)

    return test_id


def _get_test_parameters_json(item) -> t.Optional[str]:
    # Test parameters are part of the test ID
    callspec: pytest.python.CallSpec2 = getattr(item, "callspec", None)

    if callspec is None:
        return None

    parameters: t.Dict[str, t.Dict[str, str]] = {"arguments": {}, "metadata": {}}
    for param_name, param_val in item.callspec.params.items():
        try:
            parameters["arguments"][param_name] = _encode_test_parameter(param_val)
        except Exception:  # noqa: E722
            parameters["arguments"][param_name] = "Could not encode"
            log.warning("Failed to encode %r", param_name, exc_info=True)

    try:
        return json.dumps(parameters, sort_keys=True)
    except TypeError:
        log.warning("Failed to serialize parameters for test %s", item, exc_info=True)
        return None


def _get_module_path_from_item(item: pytest.Item) -> Path:
    try:
        item_path = getattr(item, "path", None)
        if item_path is not None:
            return item.path.absolute().parent
        return Path(item.module.__file__).absolute().parent
    except Exception:  # noqa: E722
        return Path.cwd()


def _get_session_command(session: pytest.Session):
    """Extract and re-create pytest session command from pytest config."""
    command = "pytest"
    if getattr(session.config, "invocation_params", None):
        command += " {}".format(" ".join(session.config.invocation_params.args))
    if _get_config("PYTEST_ADDOPTS", False, asbool):
        command += " {}".format(_get_config("PYTEST_ADDOPTS", False, asbool))
    return command


def _get_source_file_info(item, item_path) -> t.Optional[TestSourceFileInfo]:
    try:
        # TODO: don't depend on internal for source file info
        if hasattr(item, "_obj"):
            test_method_object = undecorated(item._obj, item.name, item_path)
            source_lines = get_source_lines_for_test_method(test_method_object)
            source_file_info = TestSourceFileInfo(item_path, source_lines[0], source_lines[1])
        else:
            source_file_info = TestSourceFileInfo(item_path, item.reportinfo()[1])
        return source_file_info
    except Exception:
        log.debug("Unable to get source file info for item %s (path %s)", item, item_path, exc_info=True)
        return None


def _get_pytest_version_tuple() -> t.Tuple[int, ...]:
    if hasattr(pytest, "version_tuple"):
        return pytest.version_tuple
    return tuple(map(int, pytest.__version__.split(".")))


def _is_pytest_8_or_later() -> bool:
    return _get_pytest_version_tuple() >= (8, 0, 0)


def _pytest_version_supports_itr() -> bool:
    return _get_pytest_version_tuple() >= ITR_MIN_SUPPORTED_VERSION


def _pytest_version_supports_retries() -> bool:
    return _get_pytest_version_tuple() >= RETRIES_MIN_SUPPORTED_VERSION


def _pytest_version_supports_efd():
    return _get_pytest_version_tuple() >= EFD_MIN_SUPPORTED_VERSION


def _pytest_version_supports_atr():
    return _get_pytest_version_tuple() >= ATR_MIN_SUPPORTED_VERSION


def _pytest_version_supports_attempt_to_fix():
    return _get_pytest_version_tuple() >= ATTEMPT_TO_FIX_MIN_SUPPORTED_VERSION


def _pytest_marked_to_skip(item: pytest.Item) -> bool:
    """Checks whether Pytest will skip an item"""
    if item.get_closest_marker("skip") is not None:
        return True

    return any(marker.args[0] for marker in item.iter_markers(name="skipif"))


def _is_test_unskippable(item: pytest.Item) -> bool:
    """Returns True if a test has a skipif marker with value false and reason ITR_UNSKIPPABLE_REASON"""
    return any(
        (marker.args[0] is False and marker.kwargs.get("reason") == ITR_UNSKIPPABLE_REASON)
        for marker in item.iter_markers(name="skipif")
    )


def _extract_span(item):
    """Extract span from `pytest.Item` instance."""
    if _USE_PLUGIN_V2:
        test_id = _get_test_id_from_item(item)
        return InternalTest.get_span(test_id)

    return getattr(item, "_datadog_span", None)


def _is_enabled_early(early_config, args):
    """Checks if the ddtrace plugin is enabled before the config is fully populated.

    This is necessary because the module watchdog for coverage collection needs to be enabled as early as possible.

    Note: since coverage is used for ITR purposes, we only check if the plugin is enabled if the pytest version supports
    ITR
    """
    if not _pytest_version_supports_itr():
        return False

    if _is_option_true("no-ddtrace", early_config, args):
        return False

    return _is_option_true("ddtrace", early_config, args)


def _is_option_true(option, early_config, args):
    return early_config.getoption(option) or early_config.getini(option) or f"--{option}" in args


class _TestOutcome(t.NamedTuple):
    status: t.Optional[TestStatus] = None
    skip_reason: t.Optional[str] = None
    exc_info: t.Optional[TestExcInfo] = None


def get_user_property(report, key, default=None):
    # DEV: `CollectReport` does not have `user_properties`.
    user_properties = getattr(report, "user_properties", [])
    for k, v in user_properties:
        if k == key:
            return v
    return default


excinfo_by_report = weakref.WeakKeyDictionary()
reports_by_item = weakref.WeakKeyDictionary()
