import datetime
import logging
import os
import platform
import sys
from typing import TYPE_CHECKING  # noqa:F401
from typing import Any  # noqa:F401
from typing import Dict  # noqa:F401
from typing import Union  # noqa:F401

import ddtrace
from ddtrace.internal.packages import get_distributions
from ddtrace.internal.utils.cache import callonce
from ddtrace.internal.writer import AgentWriterInterface
from ddtrace.internal.writer import LogWriter
from ddtrace.settings._agent import config as agent_config
from ddtrace.settings.asm import config as asm_config

from .logger import get_logger


if TYPE_CHECKING:  # pragma: no cover
    from ddtrace.trace import Tracer  # noqa:F401


logger = get_logger(__name__)

# The architecture function spawns the file subprocess on the interpreter
# executable. We make sure we call this once and cache the result.
architecture = callonce(lambda: platform.architecture())


def in_venv():
    # type: () -> bool
    # Works with both venv and virtualenv
    # https://stackoverflow.com/a/42580137
    return (
        "VIRTUAL_ENV" in os.environ
        or hasattr(sys, "real_prefix")
        or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
    )


def tags_to_str(tags):
    # type: (Dict[str, Any]) -> str
    # Turn a dict of tags to a string "k1:v1,k2:v2,..."
    return ",".join(["%s:%s" % (k, v) for k, v in tags.items()])


def collect(tracer):
    # type: (Tracer) -> Dict[str, Any]
    """Collect system and library information into a serializable dict."""

    from ddtrace.internal.runtime.runtime_metrics import RuntimeWorker

    if isinstance(tracer._span_aggregator.writer, LogWriter):
        agent_url = "AGENTLESS"
        agent_error = None
    elif isinstance(tracer._span_aggregator.writer, AgentWriterInterface):
        writer = tracer._span_aggregator.writer
        agent_url = writer.intake_url
        try:
            writer.write([])
            writer.flush_queue(raise_exc=True)
        except Exception as e:
            agent_error = "Agent not reachable at %s. Exception raised: %s" % (agent_url, str(e))
        else:
            agent_error = None
    else:
        agent_url = "CUSTOM"
        agent_error = None

    sampler_rules = [str(rule) for rule in tracer._sampler.rules]

    is_venv = in_venv()

    packages_available = {name: version for (name, version) in get_distributions().items()}
    integration_configs = {}  # type: Dict[str, Union[Dict[str, Any], str]]
    for module, enabled in ddtrace._monkey.PATCH_MODULES.items():
        # TODO: this check doesn't work in all cases... we need a mapping
        #       between the module and the library name.
        module_available = module in packages_available
        module_instrumented = module in ddtrace._monkey._PATCHED_MODULES
        module_imported = module in sys.modules

        if enabled:
            # Note that integration configs aren't added until the integration
            # module is imported. This typically occurs as a side-effect of
            # patch().
            # This also doesn't load work in all cases since we don't always
            # name the configuration entry the same as the integration module
            # name :/
            config = ddtrace.config._config.get(module, "N/A")
        else:
            config = None

        if module_available:
            integration_configs[module] = dict(
                enabled=enabled,
                instrumented=module_instrumented,
                module_available=module_available,
                module_version=packages_available[module],
                module_imported=module_imported,
                config=config,
            )
        else:
            # Use N/A here to avoid the additional clutter of an entire
            # config dictionary for a module that isn't available.
            integration_configs[module] = "N/A"

    pip_version = packages_available.get("pip", "N/A")

    from ddtrace._trace.tracer import log

    return dict(
        # Timestamp UTC ISO 8601 with the trailing +00:00 removed
        date=datetime.datetime.now(datetime.timezone.utc).isoformat()[0:-6],
        # eg. "Linux", "Darwin"
        os_name=platform.system(),
        # eg. 12.5.0
        os_version=platform.release(),
        is_64_bit=sys.maxsize > 2**32,
        architecture=architecture()[0],
        vm=platform.python_implementation(),
        version=ddtrace.__version__,
        lang="python",
        lang_version=platform.python_version(),
        pip_version=pip_version,
        in_virtual_env=is_venv,
        agent_url=agent_url,
        agent_error=agent_error,
        statsd_url=agent_config.dogstatsd_url,
        env=ddtrace.config.env or "",
        is_global_tracer=tracer == ddtrace.tracer,
        enabled_env_setting=os.getenv("DATADOG_TRACE_ENABLED"),
        tracer_enabled=tracer.enabled,
        sampler_type=type(tracer._sampler).__name__ if tracer._sampler else "N/A",
        priority_sampler_type="N/A",
        sampler_rules=sampler_rules,
        service=ddtrace.config.service or "",
        debug=log.isEnabledFor(logging.DEBUG),
        enabled_cli="ddtrace" in os.getenv("PYTHONPATH", ""),
        log_injection_enabled=ddtrace.config._logs_injection,
        health_metrics_enabled=ddtrace.config._health_metrics_enabled,
        runtime_metrics_enabled=RuntimeWorker.enabled,
        dd_version=ddtrace.config.version or "",
        global_tags=tags_to_str(ddtrace.config.tags),
        tracer_tags=tags_to_str(tracer._tags),
        integrations=integration_configs,
        partial_flush_enabled=tracer._span_aggregator.partial_flush_enabled,
        partial_flush_min_spans=tracer._span_aggregator.partial_flush_min_spans,
        asm_enabled=asm_config._asm_enabled,
        iast_enabled=asm_config._iast_enabled,
        waf_timeout=asm_config._waf_timeout,
        remote_config_enabled=ddtrace.config._remote_config_enabled,
    )


def pretty_collect(tracer, color=True):
    class bcolors:
        HEADER = "\033[95m"
        OKBLUE = "\033[94m"
        OKCYAN = "\033[96m"
        OKGREEN = "\033[92m"
        WARNING = "\033[93m"
        FAIL = "\033[91m"
        ENDC = "\033[0m"
        BOLD = "\033[1m"

    if not color:
        bcolors.HEADER = ""
        bcolors.OKBLUE = ""
        bcolors.OKCYAN = ""
        bcolors.OKGREEN = ""
        bcolors.WARNING = ""
        bcolors.FAIL = ""
        bcolors.ENDC = ""
        bcolors.BOLD = ""

    info = collect(tracer)

    info_pretty = """{blue}{bold}Tracer Configurations:{end}
    Tracer enabled: {tracer_enabled}
    Application Security enabled: {appsec_enabled}
    Remote Configuration enabled: {remote_config_enabled}
    IAST enabled (experimental): {iast_enabled}
    Debug logging: {debug}
    Writing traces to: {agent_url}
    Agent error: {agent_error}
    Log injection enabled: {log_injection_enabled}
    Health metrics enabled: {health_metrics_enabled}
    Priority sampling enabled: {priority_sampling_enabled}
    Partial flushing enabled: {partial_flush_enabled}
    Partial flush minimum number of spans: {partial_flush_min_spans}
    WAF timeout: {waf_timeout} msecs
    {green}{bold}Tagging:{end}
    DD Service: {service}
    DD Env: {env}
    DD Version: {dd_version}
    Global Tags: {global_tags}
    Tracer Tags: {tracer_tags}""".format(
        tracer_enabled=info.get("tracer_enabled"),
        appsec_enabled=info.get("asm_enabled"),
        remote_config_enabled=info.get("remote_config_enabled"),
        iast_enabled=info.get("iast_enabled"),
        debug=info.get("debug"),
        agent_url=info.get("agent_url") or "Not writing at the moment, is your tracer running?",
        agent_error=info.get("agent_error") or "None",
        log_injection_enabled=info.get("log_injection_enabled"),
        health_metrics_enabled=info.get("health_metrics_enabled"),
        priority_sampling_enabled=info.get("priority_sampling_enabled"),
        partial_flush_enabled=info.get("partial_flush_enabled"),
        partial_flush_min_spans=info.get("partial_flush_min_spans") or "Not set",
        service=info.get("service") or "None",
        env=info.get("env") or "None",
        dd_version=info.get("dd_version") or "None",
        global_tags=info.get("global_tags") or "None",
        waf_timeout=info.get("waf_timeout"),
        tracer_tags=info.get("tracer_tags") or "None",
        blue=bcolors.OKBLUE,
        green=bcolors.OKGREEN,
        bold=bcolors.BOLD,
        end=bcolors.ENDC,
    )

    summary = "{0}{1}Summary{2}".format(bcolors.OKCYAN, bcolors.BOLD, bcolors.ENDC)

    if info.get("agent_error"):
        summary += (
            "\n\n{fail}ERROR: It looks like you have an agent error: '{agent_error}'\n If you're experiencing"
            " a connection error, please make sure you've followed the setup for your particular environment so that "
            "the tracer and Datadog agent are configured properly to connect, and that the Datadog agent is running: "
            "https://ddtrace.readthedocs.io/en/stable/troubleshooting.html#failed-to-send-traces-connectionrefused"
            "error"
            "\nIf your issue is not a connection error then please reach out to support for further assistance:"
            " https://docs.datadoghq.com/help/{end}"
        ).format(fail=bcolors.FAIL, agent_error=info.get("agent_error"), end=bcolors.ENDC)

    if not info.get("service"):
        summary += (
            "\n\n{warning}WARNING SERVICE NOT SET: It is recommended that a service tag be set for all traced"
            " applications. For more information please see"
            " https://ddtrace.readthedocs.io/en/stable/troubleshooting.html{end}"
        ).format(warning=bcolors.WARNING, end=bcolors.ENDC)

    if not info.get("env"):
        summary += (
            "\n\n{warning}WARNING ENV NOT SET: It is recommended that an env tag be set for all traced"
            " applications. For more information please see "
            "https://ddtrace.readthedocs.io/en/stable/troubleshooting.html{end}"
        ).format(warning=bcolors.WARNING, end=bcolors.ENDC)

    if not info.get("dd_version"):
        summary += (
            "\n\n{warning}WARNING VERSION NOT SET: It is recommended that a version tag be set for all traced"
            " applications. For more information please see"
            " https://ddtrace.readthedocs.io/en/stable/troubleshooting.html{end}"
        ).format(warning=bcolors.WARNING, end=bcolors.ENDC)

    info_pretty += "\n\n" + summary

    return info_pretty
