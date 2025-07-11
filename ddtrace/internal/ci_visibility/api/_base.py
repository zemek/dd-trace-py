import abc
import dataclasses
from enum import Enum
import functools
import json
from pathlib import Path
import typing
from typing import Any
from typing import Dict
from typing import Generic
from typing import List
from typing import Optional
from typing import TypeVar
from typing import Union

from ddtrace._trace.context import Context
from ddtrace.constants import SPAN_KIND
from ddtrace.ext import SpanTypes
from ddtrace.ext import test
from ddtrace.ext.test_visibility import ITR_SKIPPING_LEVEL
from ddtrace.ext.test_visibility._test_visibility_base import TestId
from ddtrace.ext.test_visibility._test_visibility_base import TestModuleId
from ddtrace.ext.test_visibility._test_visibility_base import TestSuiteId
from ddtrace.ext.test_visibility.status import TestSourceFileInfo
from ddtrace.ext.test_visibility.status import TestStatus
from ddtrace.internal.ci_visibility._api_client import EarlyFlakeDetectionSettings
from ddtrace.internal.ci_visibility._api_client import TestManagementSettings
from ddtrace.internal.ci_visibility.api._coverage_data import TestVisibilityCoverageData
from ddtrace.internal.ci_visibility.constants import COVERAGE_TAG_NAME
from ddtrace.internal.ci_visibility.constants import EVENT_TYPE
from ddtrace.internal.ci_visibility.constants import SKIPPED_BY_ITR_REASON
from ddtrace.internal.ci_visibility.errors import CIVisibilityDataError
from ddtrace.internal.ci_visibility.telemetry.constants import EVENT_TYPES
from ddtrace.internal.ci_visibility.telemetry.constants import TEST_FRAMEWORKS
from ddtrace.internal.ci_visibility.telemetry.itr import record_itr_forced_run
from ddtrace.internal.ci_visibility.telemetry.itr import record_itr_skipped
from ddtrace.internal.ci_visibility.telemetry.itr import record_itr_unskippable
from ddtrace.internal.constants import COMPONENT
from ddtrace.internal.logger import get_logger
from ddtrace.internal.test_visibility._atr_mixins import AutoTestRetriesSettings
from ddtrace.internal.test_visibility.coverage_lines import CoverageLines
from ddtrace.trace import Span
from ddtrace.trace import Tracer


if typing.TYPE_CHECKING:
    from ddtrace.internal.ci_visibility.api._session import TestVisibilitySession


log = get_logger(__name__)


@dataclasses.dataclass(frozen=True)
class TestVisibilitySessionSettings:
    __test__ = False
    tracer: Tracer
    test_service: str
    test_command: str
    test_framework: str
    test_framework_metric_name: TEST_FRAMEWORKS
    test_framework_version: str
    session_operation_name: str
    module_operation_name: str
    suite_operation_name: str
    test_operation_name: str
    workspace_path: Path
    is_unsupported_ci: bool = False
    reject_duplicates: bool = True
    itr_enabled: bool = False
    itr_test_skipping_enabled: bool = False
    itr_test_skipping_level: Optional[ITR_SKIPPING_LEVEL] = None
    itr_correlation_id: str = ""
    coverage_enabled: bool = False
    known_tests_enabled: bool = False
    efd_settings: EarlyFlakeDetectionSettings = dataclasses.field(default_factory=EarlyFlakeDetectionSettings)
    atr_settings: AutoTestRetriesSettings = dataclasses.field(default_factory=AutoTestRetriesSettings)
    test_management_settings: TestManagementSettings = dataclasses.field(default_factory=TestManagementSettings)
    ci_provider_name: Optional[str] = None
    is_auto_injected: bool = False

    def __post_init__(self):
        if not isinstance(self.tracer, Tracer):
            raise TypeError("tracer must be a ddtrace.trace.Tracer")
        if not isinstance(self.workspace_path, Path):
            raise TypeError("root_dir must be a pathlib.Path")
        if not self.workspace_path.is_absolute():
            raise ValueError("root_dir must be an absolute pathlib.Path")
        if not isinstance(self.test_framework_metric_name, TEST_FRAMEWORKS):
            raise TypeError("test_framework_metric must be a TEST_FRAMEWORKS enum")


class SPECIAL_STATUS(Enum):
    UNFINISHED = 1
    NONSTARTED = 2


CIDT = TypeVar("CIDT", TestModuleId, TestSuiteId, TestId)  # Child item ID types
ITEMT = TypeVar("ITEMT", bound="TestVisibilityItemBase")  # All item types


def _require_not_finished(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if self.is_finished():
            log.warning("Method %s called on item %s, but it is already finished", func, self)
            return
        return func(self, *args, **kwargs)

    return wrapper


def _require_span(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if self._span is None:
            log.warning("Method %s called on item %s, but self._span is None", func, self)
            return
        return func(self, *args, **kwargs)

    return wrapper


class TestVisibilityItemBase(abc.ABC):
    __test__ = False
    _event_type = "unset_event_type"
    _event_type_metric_name = EVENT_TYPES.UNSET

    def __init__(
        self,
        name: str,
        session_settings: TestVisibilitySessionSettings,
        operation_name: str,
        initial_tags: Optional[Dict[str, Any]] = None,
        parent: Optional["TestVisibilityParentItem"] = None,
        resource: Optional[str] = None,
    ) -> None:
        self.name: str = name
        self.parent: Optional["TestVisibilityParentItem"] = parent
        self._status: TestStatus = TestStatus.FAIL
        self._session_settings: TestVisibilitySessionSettings = session_settings
        self._tracer: Tracer = session_settings.tracer
        self._service: str = session_settings.test_service
        self._operation_name: str = operation_name
        self._resource: Optional[str] = resource if resource is not None else operation_name

        self._span: Optional[Span] = None
        self._tags: Dict[str, Any] = initial_tags if initial_tags else {}

        self._stash: Dict[str, Any] = {}

        # ITR-related attributes
        self._is_itr_skipped: bool = False
        self._itr_skipped_count: int = 0
        self._is_itr_unskippable: bool = False
        self._is_itr_forced_run: bool = False

        # General purpose attributes not used by all item types
        self._codeowners: Optional[List[str]] = []
        self._source_file_info: Optional[TestSourceFileInfo] = None
        self._coverage_data: Optional[TestVisibilityCoverageData] = None

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name})"

    @_require_span
    def _add_all_tags_to_span(self) -> None:
        if self._span is None:
            return

        for tag, tag_value in self._tags.items():
            try:
                if isinstance(tag_value, str):
                    self._span.set_tag_str(tag, tag_value)
                elif isinstance(tag_value, bool):
                    self._span.set_tag_str(tag, "true" if tag_value else "false")
                else:
                    self._span.set_tag(tag, tag_value)
            except Exception as e:
                log.debug("Error setting tag %s: %s", tag, e)

    def _start_span(self, context: Optional[Context] = None) -> None:
        # Test items do not use a parent, and are instead their own trace's root span
        # except if context is passed (for xdist support)
        parent_span: Optional[Union[Span, Context]] = None
        if context is not None:
            parent_span = context
        elif isinstance(self, TestVisibilityParentItem):
            parent_span = self.get_parent_span()

        self._span = self._tracer._start_span(
            self._operation_name,
            resource=self._resource if self._resource else self._operation_name,
            child_of=parent_span,
            service=self._service,
            span_type=SpanTypes.TEST,
            activate=True,
        )
        # Setting initial tags is necessary for integrations that might look at the span before it is finished
        self._span.set_tag(EVENT_TYPE, self._event_type)
        self._span.set_tag(SPAN_KIND, "test")
        log.debug("Started span %s for item %s", self._span, self)

    @_require_span
    def _finish_span(self, override_finish_time: Optional[float] = None) -> None:
        if self._span is None:
            return

        self._set_default_tags()
        self._set_test_hierarchy_tags()
        self._add_coverage_data_tag()

        # ITR-related tags should only be set if ITR is enabled in the first place
        self._set_itr_tags(self._session_settings.itr_enabled)

        # Add efd-related tags if EFD is enabled
        if self._session_settings.efd_settings is not None and self._session_settings.efd_settings.enabled:
            self._set_efd_tags()

        if self._session_settings.known_tests_enabled:
            self._set_known_tests_tags()

        if self._session_settings.atr_settings is not None and self._session_settings.atr_settings.enabled:
            self._set_atr_tags()

        if (
            self._session_settings.test_management_settings is not None
            and self._session_settings.test_management_settings.enabled
        ):
            self._set_test_management_tags()

        # Allow items to potentially overwrite default and hierarchy tags.
        self._set_item_tags()
        self._set_span_tags()

        self._add_all_tags_to_span()
        self._span.finish(finish_time=override_finish_time)

    def _set_default_tags(self) -> None:
        """Applies the tags that should be on every span regardless of the item type

        All spans start with test.STATUS set to FAIL, in order to ensure that no span is accidentally
        reported as successful.
        """

        self.set_tags(
            {
                COMPONENT: self._session_settings.test_framework,
                test.FRAMEWORK: self._session_settings.test_framework,
                test.FRAMEWORK_VERSION: self._session_settings.test_framework_version,
                test.COMMAND: self._session_settings.test_command,
                test.STATUS: self._status.value,  # Convert to a string at the last moment
            }
        )

        if self._codeowners:
            self.set_tag(test.CODEOWNERS, json.dumps(self._codeowners))

        if self._source_file_info is not None:
            if self._source_file_info.path:
                # Set source file path to be relative to the root directory
                try:
                    relative_path = self._source_file_info.path.relative_to(self._session_settings.workspace_path)
                except ValueError:
                    log.debug("Source file path is not within the root directory, replacing with absolute path")
                    relative_path = self._source_file_info.path
                self.set_tag(test.SOURCE_FILE, str(relative_path))
            if self._source_file_info.start_line is not None:
                self.set_tag(test.SOURCE_START, self._source_file_info.start_line)
            if self._source_file_info.end_line is not None:
                self.set_tag(test.SOURCE_END, self._source_file_info.end_line)

    def _set_item_tags(self) -> None:
        """Overridable by subclasses to set tags specific to the item type"""
        pass

    def _set_itr_tags(self, itr_enabled: bool) -> None:
        """Note: some tags are also added in the parent class as well as some individual item classes"""
        if not itr_enabled:
            return

        if self._is_itr_skipped:
            self.set_tag(test.SKIP_REASON, SKIPPED_BY_ITR_REASON)
        self.set_tag(test.ITR_SKIPPED, self._is_itr_skipped)

        self.set_tag(test.ITR_UNSKIPPABLE, self._is_itr_unskippable)
        self.set_tag(test.ITR_FORCED_RUN, self._is_itr_forced_run)

    def _set_efd_tags(self) -> None:
        """EFD tags are only set at the test or session level"""
        pass

    def _set_known_tests_tags(self) -> None:
        """Known test tags are only set at the test level"""
        pass

    def _set_atr_tags(self) -> None:
        """ATR tags are only set at the test level"""
        pass

    def _set_test_management_tags(self) -> None:
        """Quarantine tags are only set at the test or session level"""
        pass

    def _set_span_tags(self):
        """This is effectively a callback method for exceptional cases where the item span
        needs to be modified directly by the class

        Only use if absolutely necessary.

        Classes that need to specifically modify the span directly should override this method.
        """
        pass

    @property
    def _source_file_info(self) -> Optional[TestSourceFileInfo]:
        return self.__source_file_info

    @_source_file_info.setter
    def _source_file_info(self, source_file_info_value: Optional[TestSourceFileInfo] = None):
        """This checks that filepaths are absolute when setting source file info"""
        self.__source_file_info = None  # Default value until source_file_info is validated

        if source_file_info_value is None:
            return
        if source_file_info_value.path is None:
            # Source file info is invalid if path is None
            return
        if not isinstance(source_file_info_value, TestSourceFileInfo):
            log.warning("Source file info must be of type TestSourceFileInfo")
            return
        if not source_file_info_value.path.is_absolute():
            # Note: this should effectively be unreachable code because the TestSourceFileInfoBase class enforces
            # that paths be absolute at creation time
            log.warning("Source file path must be absolute, removing source file info")
            return

        self.__source_file_info = source_file_info_value

    @property
    def _session_settings(self) -> TestVisibilitySessionSettings:
        return self.__session_settings

    @_session_settings.setter
    def _session_settings(self, session_settings_value: TestVisibilitySessionSettings) -> None:
        if not isinstance(session_settings_value, TestVisibilitySessionSettings):
            raise TypeError("Session settings must be of type TestVisibilitySessionSettings")
        self.__session_settings = session_settings_value

    @abc.abstractmethod
    def _get_hierarchy_tags(self) -> Dict[str, str]:
        raise NotImplementedError("This method must be implemented by the subclass")

    def _collect_hierarchy_tags(self) -> Dict[str, str]:
        """Collects all tags from the item's hierarchy and returns them as a single dict"""
        tags = self._get_hierarchy_tags()
        parent = self.parent
        while parent is not None:
            tags.update(parent._get_hierarchy_tags())
            parent = parent.parent
        return tags

    def _set_test_hierarchy_tags(self) -> None:
        """Add module, suite, and test name and id tags"""
        self.set_tags(self._collect_hierarchy_tags())

    @abc.abstractmethod
    def _telemetry_record_event_created(self):
        # Telemetry for events created has specific tags for item types
        raise NotImplementedError("This method must be implemented by the subclass")

    @abc.abstractmethod
    def _telemetry_record_event_finished(self):
        # Telemetry for events created has specific tags for item types
        raise NotImplementedError("This method must be implemented by the subclass")

    def start(self, context: Optional[Context] = None) -> None:
        log.debug("Test Visibility: starting %s", self)

        if self.is_started():
            if self._session_settings.reject_duplicates:
                error_msg = f"Item {self} has already been started"
                log.warning(error_msg)
                raise CIVisibilityDataError(error_msg)
            return

        self._telemetry_record_event_created()
        self._start_span(context)

    def is_started(self) -> bool:
        return self._span is not None

    def finish(
        self,
        force: bool = False,
        override_status: Optional[TestStatus] = None,
        override_finish_time: Optional[float] = None,
    ) -> None:
        """Finish the span and set the _is_finished flag to True.

        Nothing should be called after this method is called.
        """
        log.debug("Test Visibility: finishing %s", self)

        if override_status:
            self.set_status(override_status)

        self._telemetry_record_event_finished()
        self._finish_span(override_finish_time=override_finish_time)

    def is_finished(self) -> bool:
        return self._span is not None and self._span.finished

    def get_session(self) -> Optional["TestVisibilitySession"]:
        if self.parent is None:
            return None
        return self.parent.get_session()

    def get_span_id(self) -> Optional[int]:
        if self._span is None:
            return None
        return self._span.span_id

    def get_status(self) -> Union[TestStatus, SPECIAL_STATUS]:
        if self.is_finished():
            return self._status
        if not self.is_started():
            return SPECIAL_STATUS.NONSTARTED
        return SPECIAL_STATUS.UNFINISHED

    def get_raw_status(self) -> TestStatus:
        return self._status

    def set_status(self, status: TestStatus) -> None:
        if self.is_finished():
            error_msg = f"Status {self._status} already set for item {self}, not setting to {status}"
            log.warning(error_msg)
            return
        self._status = status

    def count_itr_skipped(self) -> None:
        self._itr_skipped_count += 1
        if self.parent is not None:
            self.parent.count_itr_skipped()

    def mark_itr_skipped(self) -> None:
        record_itr_skipped(self._event_type_metric_name)
        self._is_itr_skipped = True

    def is_itr_skipped(self) -> bool:
        return self._is_itr_skipped

    def mark_itr_unskippable(self) -> None:
        record_itr_unskippable(self._event_type_metric_name)
        self._is_itr_unskippable = True
        if self.parent is not None:
            self.parent.mark_itr_unskippable()

    def is_itr_unskippable(self) -> bool:
        return self._is_itr_unskippable

    def mark_itr_forced_run(self) -> None:
        """If any item is forced to run, all ancestors are forced to run and increment by one"""
        record_itr_forced_run(self._event_type_metric_name)
        self._is_itr_forced_run = True
        if self.parent is not None:
            self.parent.mark_itr_forced_run()

    def was_itr_forced_run(self) -> bool:
        return self._is_itr_forced_run

    @_require_not_finished
    def set_tag(self, tag_name: str, tag_value: Any) -> None:
        self._tags[tag_name] = tag_value

    @_require_not_finished
    def set_tags(self, tags: Dict[str, Any]) -> None:
        for tag in tags:
            self._tags[tag] = tags[tag]

    def get_tag(self, tag_name: str) -> Any:
        return self._tags.get(tag_name)

    def get_tags(self, tag_names: List[str]) -> Dict[str, Any]:
        tags = {}
        for tag_name in tag_names:
            tags[tag_name] = self._tags.get(tag_name)

        return tags

    @_require_not_finished
    def delete_tag(self, tag_name: str) -> None:
        del self._tags[tag_name]

    # @_require_not_finished
    def delete_tags(self, tag_names: List[str]) -> None:
        for tag_name in tag_names:
            del self._tags[tag_name]

    def get_span(self) -> Optional[Span]:
        return self._span

    def get_parent_span(self) -> Optional[Span]:
        if self.parent is not None:
            return self.parent.get_span()
        return None

    @abc.abstractmethod
    def add_coverage_data(self, coverage_data: Dict[Path, CoverageLines]) -> None:
        pass

    @_require_span
    def _add_coverage_data_tag(self) -> None:
        if self._span is None:
            return
        if self._coverage_data:
            self._span.set_struct_tag(
                COVERAGE_TAG_NAME, self._coverage_data.build_payload(self._session_settings.workspace_path)
            )

    def get_coverage_data(self) -> Optional[Dict[Path, CoverageLines]]:
        if self._coverage_data is None:
            return None
        return self._coverage_data.get_data()

    def stash_set(self, key: str, value: object) -> None:
        self._stash[key] = value

    def stash_get(self, key: str) -> object:
        return self._stash.get(key)

    def stash_delete(self, key: str) -> object:
        return self._stash.pop(key, None)


class TestVisibilityChildItem(TestVisibilityItemBase, Generic[CIDT]):
    pass


CITEMT = TypeVar("CITEMT", bound="TestVisibilityChildItem")


class TestVisibilityParentItem(TestVisibilityItemBase, Generic[CIDT, CITEMT]):
    def __init__(
        self,
        name: str,
        session_settings: TestVisibilitySessionSettings,
        operation_name: str,
        initial_tags: Optional[Dict[str, Any]],
    ) -> None:
        super().__init__(name, session_settings, operation_name, initial_tags)
        self._children: Dict[CIDT, CITEMT] = {}
        self._distributed_children = False

    def get_status(self) -> Union[TestStatus, SPECIAL_STATUS]:
        """Recursively computes status based on all children's status

        - FAIL: if any children have a status of FAIL
        - SKIP: if all children have a status of SKIP
        - PASS: if all children have a status of PASS
        - UNFINISHED: if any children are not finished

        The caller of get_status() must decide what to do if the result is UNFINISHED
        """
        if self._children is None:
            return self.get_raw_status()

        # We use values because enum entries do not hash stably
        children_status_counts = {
            TestStatus.FAIL.value: 0,
            TestStatus.SKIP.value: 0,
            TestStatus.PASS.value: 0,
        }

        for child in self._children.values():
            child_status = child.get_status()
            if child_status == SPECIAL_STATUS.NONSTARTED:
                # This means that the child was never started, so we don't count it
                continue
            elif child_status == SPECIAL_STATUS.UNFINISHED:
                # There's no point in continuing to count if we care about unfinished children
                log.debug("Item %s has unfinished children", self)
                return SPECIAL_STATUS.UNFINISHED
            children_status_counts[child_status.value] += 1

        log.debug("Children status counts for %s: %s", self, children_status_counts)

        if children_status_counts[TestStatus.FAIL.value] > 0:
            return TestStatus.FAIL
        len_children = len(self._children)
        if len_children > 0 and children_status_counts[TestStatus.SKIP.value] == len_children:
            return TestStatus.SKIP
        # We can assume the current item passes if not all children are skipped, and there were no failures
        if children_status_counts[TestStatus.FAIL.value] == 0:
            return TestStatus.PASS

        # If we somehow got here, something odd happened and we set the status as FAIL out of caution
        return TestStatus.FAIL

    def finish(
        self,
        force: bool = False,
        override_status: Optional[TestStatus] = None,
        override_finish_time: Optional[float] = None,
    ) -> None:
        """Recursively finish all children and then finish self

        An unfinished status is not considered an error condition (eg: some order-randomization plugins may cause
        non-linear ordering of children items).

        force results in all children being finished regardless of their status

        override_status only applies to the current item. Any unfinished children that are forced to finish will be
        finished with whatever status they had at finish time (in reality, this should mean that any unfinished
        children are marked as failed, since that is the default status set upon start)
        """
        if override_status:
            # Respect override status no matter what
            self.set_status(override_status)

        item_status = self.get_status()

        if item_status == SPECIAL_STATUS.UNFINISHED:
            if force:
                # Finish all children regardless of their status
                for child in self._children.values():
                    if not child.is_finished():
                        child.finish(force=force)
                self.set_status(self.get_raw_status())
            else:
                return
        elif not isinstance(item_status, SPECIAL_STATUS):
            self.set_status(item_status)

        super().finish(force=force, override_status=override_status, override_finish_time=override_finish_time)

    def add_child(self, child_item_id: CIDT, child: CITEMT) -> None:
        child.parent = self
        if child_item_id in self._children:
            if self._session_settings.reject_duplicates:
                error_msg = f"{child_item_id} already exists in {self}'s children"
                log.warning(error_msg)
                raise CIVisibilityDataError(error_msg)
            # If duplicates are allowed, we don't need to do anything
            return
        self._children[child_item_id] = child

    def get_child_by_id(self, child_id: CIDT) -> CITEMT:
        if child_id in self._children:
            return self._children[child_id]
        error_msg = f"{child_id} not found in {self}'s children"
        raise CIVisibilityDataError(error_msg)

    def _set_itr_tags(self, itr_enabled: bool) -> None:
        """Set tags on parent items based on ITR enablement status"""
        super()._set_itr_tags(itr_enabled)

        if not itr_enabled:
            return

        self.set_tag(test.ITR_TEST_SKIPPING_TESTS_SKIPPED, self._itr_skipped_count > 0)

        # Only parent items set skipped counts because tests would always be 1 or 0
        if self._children or self._distributed_children:
            self.set_tag(test.ITR_TEST_SKIPPING_COUNT, self._itr_skipped_count)

    def set_distributed_children(self) -> None:
        self._distributed_children = True
