import abc
import dataclasses
from enum import Enum
from pathlib import Path
from typing import Generic
from typing import Optional
from typing import TypeVar
from typing import Union

from ddtrace.internal.logger import get_logger


log = get_logger(__name__)


@dataclasses.dataclass(frozen=True)
class TestSessionId:
    __test__ = False
    """Placeholder ID without attributes

    Allows reusing the same _TestVisibilityIdBase methods for sessions which do not have individual session IDs
    """


@dataclasses.dataclass(frozen=True)
class _TestVisibilityRootItemIdBase:
    """This class exists for the ABC class below"""

    __test__ = False
    name: str

    def get_parent_id(self) -> "_TestVisibilityRootItemIdBase":
        return self


RT = TypeVar("RT", bound="_TestVisibilityRootItemIdBase")


@dataclasses.dataclass(frozen=True)
class _TestVisibilityIdBase(abc.ABC):
    __test__ = False

    @abc.abstractmethod
    def get_parent_id(self) -> Union["_TestVisibilityIdBase", _TestVisibilityRootItemIdBase]:
        raise NotImplementedError("This method must be implemented by the subclass")


PT = TypeVar("PT", bound=Union[_TestVisibilityIdBase, _TestVisibilityRootItemIdBase])


@dataclasses.dataclass(frozen=True)
class _TestVisibilityChildItemIdBase(_TestVisibilityIdBase, Generic[PT]):
    parent_id: PT
    name: str

    def get_parent_id(self) -> PT:
        return self.parent_id


@dataclasses.dataclass(frozen=True)
class TestModuleId(_TestVisibilityRootItemIdBase):
    name: str

    def __repr__(self):
        return "TestModuleId(module={})".format(
            self.name,
        )


@dataclasses.dataclass(frozen=True)
class TestSuiteId(_TestVisibilityChildItemIdBase[TestModuleId]):
    def __repr__(self):
        return "TestSuiteId(module={}, suite={})".format(self.parent_id.name, self.name)


@dataclasses.dataclass(frozen=True)
class TestId(_TestVisibilityChildItemIdBase[TestSuiteId]):
    parameters: Optional[str] = None  # For hashability, a JSON string of a dictionary of parameters

    def __repr__(self):
        return "TestId(module={}, suite={}, test={}, parameters={})".format(
            self.parent_id.parent_id.name,
            self.parent_id.name,
            self.name,
            self.parameters,
        )


TestVisibilityItemId = TypeVar(
    "TestVisibilityItemId",
    bound=Union[
        _TestVisibilityChildItemIdBase, _TestVisibilityRootItemIdBase, TestSessionId, TestModuleId, TestSuiteId, TestId
    ],
)


class _TestVisibilityAPIBase(abc.ABC):
    __test__ = False

    def __init__(self):
        raise NotImplementedError("This class is not meant to be instantiated")

    @staticmethod
    @abc.abstractmethod
    def discover(*args, **kwargs):
        pass

    @staticmethod
    @abc.abstractmethod
    def start(*args, **kwargs):
        pass

    @staticmethod
    @abc.abstractmethod
    def finish(
        item_id: _TestVisibilityRootItemIdBase,
        override_status: Optional[Enum],
        force_finish_children: bool = False,
        *args,
        **kwargs,
    ):
        pass


@dataclasses.dataclass(frozen=True)
class TestSourceFileInfoBase:
    """This supplies the __post_init__ method for the TestSourceFileInfo

    It is simply here for cosmetic reasons of keeping the original class definition short
    """

    __test__ = False

    path: Path
    start_line: Optional[int] = None
    end_line: Optional[int] = None

    def __post_init__(self):
        """Enforce that attributes make sense after initialization"""
        self._check_path()
        self._check_line_numbers()

    def _check_path(self):
        """Checks that path is of Path type and is absolute, converting it to absolute if not"""
        if not isinstance(self.path, Path):
            raise ValueError("path must be a Path object, but is of type %s", type(self.path))

        if not self.path.is_absolute():
            abs_path = self.path.absolute()
            log.debug("Converting path to absolute: %s -> %s", self.path, abs_path)
            object.__setattr__(self, "path", abs_path)

    def _check_line_numbers(self):
        self._check_line_number("start_line")
        self._check_line_number("end_line")

        # Lines must be non-zero positive ints after _check_line_number ran
        if self.start_line is not None and self.end_line is not None:
            if self.start_line > self.end_line:
                raise ValueError("start_line must be less than or equal to end_line")

        if self.start_line is None and self.end_line is not None:
            raise ValueError("start_line must be set if end_line is set")

    def _check_line_number(self, attr_name: str):
        """Checks that a line number is a positive integer, setting to None if not"""
        line_number = getattr(self, attr_name)

        if line_number is None:
            return

        if not isinstance(line_number, int):
            raise ValueError("%s must be an integer, but is of type %s", attr_name, type(line_number))

        if line_number < 1:
            raise ValueError("%s must be a positive integer, but is %s", attr_name, line_number)
