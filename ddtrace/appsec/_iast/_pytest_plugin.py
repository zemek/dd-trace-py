#!/usr/bin/env python3
import dataclasses
import json
from typing import List

from ddtrace.appsec._constants import IAST
from ddtrace.appsec._iast.reporter import Vulnerability
from ddtrace.internal.logger import get_logger
from ddtrace.settings.asm import config as asm_config


log = get_logger(__name__)


@dataclasses.dataclass(unsafe_hash=True)
class VulnerabilityFoundInTest(Vulnerability):
    test: str


try:
    import pytest

    @pytest.fixture(autouse=asm_config._iast_enabled)
    def ddtrace_iast(request, ddspan):
        """
        Extract the vulnerabilities discovered in tests.
        Optionally output the test as failed if vulnerabilities are found.
        """
        yield
        if ddspan is None:
            return

        # looking for IAST data in the span
        dict_data = ddspan.get_struct_tag(IAST.STRUCT)
        if dict_data is None:
            data = ddspan.get_tag(IAST.JSON)
            if data is None:
                return
            else:
                dict_data = json.loads(data)

        if dict_data["vulnerabilities"]:
            for vuln in dict_data["vulnerabilities"]:
                vuln_data.append(
                    VulnerabilityFoundInTest(
                        test=request.node.nodeid,
                        type=vuln["type"],
                        evidence=vuln["evidence"],
                        location=vuln["location"],
                    )
                )

            if request.config.getoption("ddtrace-iast-fail-tests"):
                vulns = ", ".join([vuln["type"] for vuln in dict_data["vulnerabilities"]])
                pytest.fail(f"There are vulnerabilities in the code: {vulns}")

except ImportError:
    log.debug("pytest not imported")


vuln_data: List[VulnerabilityFoundInTest] = []


def extract_code_snippet(filepath, line_number, context=3):
    """Extracts code snippet around the given line number."""
    try:
        with open(filepath, "r") as file:
            lines = file.readlines()
            start = max(0, line_number - context - 1)
            end = min(len(lines), line_number + context)
            code = lines[start:end]
            return code, start  # Return lines and starting line number
    except Exception:
        log.debug("Error reading file %s", filepath, exc_info=True)
        return "", 0


def print_iast_report(terminalreporter):
    if not asm_config._iast_enabled:
        return

    if not vuln_data:
        terminalreporter.write_sep("=", "Datadog Code Security Report", purple=True, bold=True)
        terminalreporter.write_line("No vulnerabilities found.")
        return

    terminalreporter.write_sep("=", "Datadog Code Security Report", purple=True, bold=True)

    for entry in vuln_data:
        terminalreporter.write_line(f"Test: {entry.test}", bold=True)
        high_severity = entry.type.endswith("INJECTION")
        terminalreporter.write_line(
            f"Vulnerability: {entry.type}",
            # TODO(@gnufede): Add remediation links, where remediation is a dict with the vulnerability as key
            # f" - \033]8;;{remediation[entry.type]}\033\\Remediation\033]8;;\033\\ \n",
            bold=True,
            red=high_severity,
            yellow=not high_severity,
        )
        terminalreporter.write_line(f"Location: {entry.location['path']}:{entry.location['line']}")
        code_snippet, start_line = extract_code_snippet(entry.location["path"], entry.location["line"])

        if code_snippet:
            terminalreporter.write_line("Code:")

            if start_line is not None:
                for i, line in enumerate(code_snippet, start=start_line + 1):
                    if i == entry.location["line"]:
                        terminalreporter.write(f"{i:4d}: {line}", bold=True, purple=True)
                    else:
                        terminalreporter.write(f"{i:4d}: {line}")
            else:
                # If there's an error extracting the code snippet
                terminalreporter.write_line(code_snippet[0], bold=True)

        terminalreporter.write_sep("=")
