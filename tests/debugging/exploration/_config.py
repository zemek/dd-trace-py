from io import TextIOWrapper
from pathlib import Path
import sys
import typing as t
from warnings import warn

from ddtrace.debugging._probe.model import CaptureLimits
from ddtrace.settings._core import DDConfig


def parse_venv(value: str) -> t.Optional[Path]:
    try:
        return Path(value).resolve() if value is not None else None
    except TypeError:
        warn(
            "No virtual environment detected. Running without a virtual environment active might "
            "cause exploration tests to instrument more than intended."
        )


class ExplorationConfig(DDConfig):
    venv = DDConfig.v(
        t.Optional[Path],
        "virtual_env",
        parser=parse_venv,
        default=None,
    )

    encode = DDConfig.v(
        bool,
        "dd.debugger.expl.encode",
        default=False,
        help="Whether to encode the snapshots",
    )

    status_messages = DDConfig.v(
        bool,
        "dd.debugger.expl.status_messages",
        default=False,
        help="Whether to print exploration debugger status messages",
    )

    includes = DDConfig.v(
        list,
        "dd.debugger.expl.include",
        parser=lambda v: [path.split(".") for path in v.split(",")],
        default=[],
        help="List of module paths to include in the exploration",
    )

    elusive = DDConfig.v(
        bool,
        "dd.debugger.expl.elusive",
        default=False,
        help="Whether to include elusive modules in the exploration",
    )

    conservative = DDConfig.v(
        bool,
        "dd.debugger.expl.conservative",
        default=True,
        help="Use extremely low capture limits to reduce overhead",
    )

    output_file = DDConfig.v(
        t.Optional[Path],
        "dd.debugger.expl.output_file",
        default=None,
        help="Path to the output file. The standard output is used otherwise",
    )

    output_stream = DDConfig.d(
        TextIOWrapper,
        lambda c: c.output_file.open("a") if c.output_file is not None else sys.__stdout__,
    )

    limits = DDConfig.d(
        CaptureLimits,
        lambda c: CaptureLimits(
            max_level=0 if c.conservative else 1,
            max_size=1,
            max_len=1 if c.conservative else 8,
            max_fields=1,
        ),
    )

    class ProfilerConfig(DDConfig):
        __item__ = "profiler"
        __prefix__ = "dd.debugger.expl.profiler"

        enabled = DDConfig.v(
            bool,
            "enabled",
            default=True,
            help="Whether to enable the exploration deterministic profiler",
        )
        delete_probes = DDConfig.v(
            bool,
            "delete_function_probes",
            default=False,
            help="Whether to delete function probes after they are triggered",
        )

        instrumentation_rate = DDConfig.v(
            float,
            "instrumentation_rate",
            default=1.0,
            help="Rate at which to instrument functions for profiling",
        )

    class CoverageConfig(DDConfig):
        __item__ = "coverage"
        __prefix__ = "dd.debugger.expl.coverage"

        enabled = DDConfig.v(
            bool,
            "enabled",
            default=True,
            help="Whether to enable the exploration line coverage",
        )

        delete_probes = DDConfig.v(
            bool,
            "delete_line_probes",
            default=False,
            help="Whether to delete line probes after they are triggered",
        )

        instrumentation_rate = DDConfig.v(
            float,
            "instrumentation_rate",
            default=1.0,
            help="Rate at which to instrument lines for coverage",
        )


config = ExplorationConfig()
