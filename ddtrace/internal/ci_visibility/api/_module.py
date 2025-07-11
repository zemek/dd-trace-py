from typing import Dict
from typing import Optional

from ddtrace.ext import test
from ddtrace.ext.test_visibility import ITR_SKIPPING_LEVEL
from ddtrace.ext.test_visibility._test_visibility_base import TestModuleId
from ddtrace.ext.test_visibility._test_visibility_base import TestSuiteId
from ddtrace.internal.ci_visibility.api._base import TestVisibilityChildItem
from ddtrace.internal.ci_visibility.api._base import TestVisibilityParentItem
from ddtrace.internal.ci_visibility.api._base import TestVisibilitySessionSettings
from ddtrace.internal.ci_visibility.api._suite import TestVisibilitySuite
from ddtrace.internal.ci_visibility.constants import MODULE_ID
from ddtrace.internal.ci_visibility.constants import MODULE_TYPE
from ddtrace.internal.ci_visibility.constants import SUITE
from ddtrace.internal.ci_visibility.constants import TEST
from ddtrace.internal.ci_visibility.telemetry.constants import EVENT_TYPES
from ddtrace.internal.ci_visibility.telemetry.events import record_event_created
from ddtrace.internal.ci_visibility.telemetry.events import record_event_finished
from ddtrace.internal.compat import Path
from ddtrace.internal.logger import get_logger


log = get_logger(__name__)


class TestVisibilityModule(
    TestVisibilityParentItem[TestSuiteId, TestVisibilitySuite], TestVisibilityChildItem[TestModuleId]
):
    _event_type = MODULE_TYPE
    _event_type_metric_name = EVENT_TYPES.MODULE

    def __init__(
        self,
        name: str,
        session_settings: TestVisibilitySessionSettings,
        module_path: Optional[Path] = None,
        initial_tags: Optional[Dict[str, str]] = None,
    ):
        super().__init__(name, session_settings, session_settings.module_operation_name, initial_tags)

        self._module_path = module_path.absolute() if module_path else None
        self.set_tag(test.ITR_TEST_CODE_COVERAGE_ENABLED, session_settings.coverage_enabled)

    def _get_hierarchy_tags(self) -> Dict[str, str]:
        # Module path is set for module and below
        module_path: str
        if self._module_path:
            if self._module_path == self._session_settings.workspace_path:
                # '.' is not the desired relative path when the workspace and module path are the same
                module_path = ""
            elif self._module_path.is_relative_to(self._session_settings.workspace_path):
                module_path = str(self._module_path.relative_to(self._session_settings.workspace_path))
            else:
                module_path = str(self._module_path)
        else:
            module_path = ""

        return {
            MODULE_ID: str(self.get_span_id()),
            test.MODULE_PATH: module_path,
            test.MODULE: self.name,
        }

    def _set_itr_tags(self, itr_enabled: bool):
        """Set module-level tags based in ITR enablement status"""
        super()._set_itr_tags(itr_enabled)

        self.set_tag(test.ITR_TEST_SKIPPING_ENABLED, self._session_settings.itr_test_skipping_enabled)
        if itr_enabled:
            self.set_tag(
                test.ITR_TEST_SKIPPING_TYPE,
                TEST if self._session_settings.itr_test_skipping_level == ITR_SKIPPING_LEVEL.TEST else SUITE,
            )
            self.set_tag(test.ITR_DD_CI_ITR_TESTS_SKIPPED, self._itr_skipped_count > 0)

    def _telemetry_record_event_created(self):
        record_event_created(
            event_type=self._event_type_metric_name,
            test_framework=self._session_settings.test_framework_metric_name,
        )

    def _telemetry_record_event_finished(self):
        record_event_finished(
            event_type=self._event_type_metric_name,
            test_framework=self._session_settings.test_framework_metric_name,
        )

    def add_coverage_data(self, *args, **kwargs):
        raise NotImplementedError("Coverage data cannot be added to modules.")
