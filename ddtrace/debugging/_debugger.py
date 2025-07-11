from collections import defaultdict
from collections import deque
from itertools import chain
import json
import linecache
import os
from pathlib import Path
import sys
import threading
import time
from types import FunctionType
from types import ModuleType
from types import TracebackType
from typing import Deque
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from typing import Set
from typing import Type
from typing import TypeVar
from typing import cast

import ddtrace
from ddtrace import config as ddconfig
from ddtrace.debugging._config import di_config
from ddtrace.debugging._function.discovery import FunctionDiscovery
from ddtrace.debugging._function.store import FullyNamedContextWrappedFunction
from ddtrace.debugging._function.store import FunctionStore
from ddtrace.debugging._import import DebuggerModuleWatchdog
from ddtrace.debugging._metrics import metrics
from ddtrace.debugging._probe.model import FunctionLocationMixin
from ddtrace.debugging._probe.model import FunctionProbe
from ddtrace.debugging._probe.model import LineLocationMixin
from ddtrace.debugging._probe.model import LineProbe
from ddtrace.debugging._probe.model import Probe
from ddtrace.debugging._probe.registry import ProbeRegistry
from ddtrace.debugging._probe.remoteconfig import ProbePollerEvent
from ddtrace.debugging._probe.remoteconfig import ProbePollerEventType
from ddtrace.debugging._probe.remoteconfig import ProbeRCAdapter
from ddtrace.debugging._probe.remoteconfig import build_probe
from ddtrace.debugging._probe.status import ProbeStatusLogger
from ddtrace.debugging._signal.collector import SignalCollector
from ddtrace.debugging._signal.model import Signal
from ddtrace.debugging._signal.model import SignalState
from ddtrace.debugging._uploader import LogsIntakeUploaderV1
from ddtrace.debugging._uploader import UploaderProduct
from ddtrace.internal import core
from ddtrace.internal.logger import get_logger
from ddtrace.internal.metrics import Metrics
from ddtrace.internal.module import origin
from ddtrace.internal.module import register_post_run_module_hook
from ddtrace.internal.module import unregister_post_run_module_hook
from ddtrace.internal.rate_limiter import BudgetRateLimiterWithJitter as RateLimiter
from ddtrace.internal.remoteconfig.worker import remoteconfig_poller
from ddtrace.internal.service import Service
from ddtrace.internal.wrapping.context import WrappingContext
from ddtrace.trace import Tracer


log = get_logger(__name__)

_probe_metrics = Metrics(namespace="dynamic.instrumentation.metric")
_probe_metrics.enable()

T = TypeVar("T")


class DebuggerError(Exception):
    """Generic debugger error."""

    pass


class DebuggerWrappingContext(WrappingContext):
    __priority__ = 99  # Execute after all other contexts

    def __init__(
        self, f, collector: SignalCollector, registry: ProbeRegistry, tracer: Tracer, probe_meter: Metrics.Meter
    ) -> None:
        super().__init__(f)

        self._collector = collector
        self._probe_registry = registry
        self._tracer = tracer
        self._probe_meter = probe_meter

        self.probes: Dict[str, Probe] = {}

    def add_probe(self, probe: Probe) -> None:
        self.probes[probe.probe_id] = probe

    def remove_probe(self, probe: Probe) -> None:
        del self.probes[probe.probe_id]

    def has_probes(self) -> bool:
        return bool(self.probes)

    def _open_signals(self) -> None:
        # Group probes on the basis of whether they create new context.
        context_creators: List[Probe] = []
        context_consumers: List[Probe] = []
        for p in self.probes.values():
            (context_creators if p.__context_creator__ else context_consumers).append(p)

        signals: Deque[Signal] = deque()

        try:
            frame = self.__frame__
            thread = threading.current_thread()

            # Trigger the context creators first, so that the new context can be
            # consumed by the consumers.
            for probe in chain(context_creators, context_consumers):
                try:
                    signal = Signal.from_probe(
                        probe,
                        frame=frame,
                        thread=thread,
                        # Because new context might be created, we need to
                        # recompute it for each probe.
                        trace_context=self._tracer.current_trace_context(),
                        meter=self._probe_meter,
                    )
                except TypeError:
                    log.error("Unsupported probe type: %s", type(probe))
                    continue

                try:
                    signal.do_enter()
                except Exception:
                    log.exception("Failed to enter signal %r", signal)
                    continue
                signals.append(signal)
        finally:
            # Save state on the wrapping context
            self.set("start_time", time.monotonic_ns())
            self.set("signals", signals)

    def _close_signals(self, retval=None, exc_info=(None, None, None)) -> None:
        end_time = time.monotonic_ns()

        try:
            signals = cast(Deque[Signal], self.get("signals"))
        except KeyError:
            log.error("Signal contexts were not opened for function probe over %s", self.__wrapped__)
            return

        while signals:
            # Open probe signals are ordered, with those that have created new
            # tracing context first. We need to finalize them in reverse order,
            # so we pop them from the end of the queue (LIFO).
            signal = signals.pop()
            try:
                signal.do_exit(retval, exc_info, end_time - self.get("start_time"))
            except Exception:
                log.exception("Failed to exit signal %r", signal)
                continue

            self._collector.push(signal)
            if signal.state is SignalState.DONE:
                self._probe_registry.set_emitting(signal.probe)

    def __enter__(self) -> "DebuggerWrappingContext":
        super().__enter__()

        try:
            self._open_signals()
        except Exception:
            log.exception("Failed to open debugging contexts")

        return self

    def __return__(self, value: T) -> T:
        try:
            self._close_signals(retval=value)
        except Exception:
            log.exception("Failed to close debugging contexts from return")
        return super().__return__(value)

    def __exit__(
        self, exc_type: Optional[Type[BaseException]], exc_val: Optional[BaseException], exc_tb: Optional[TracebackType]
    ) -> None:
        try:
            self._close_signals(exc_info=(exc_type, exc_val, exc_tb))
        except Exception:
            log.exception("Failed to close debugging contexts from exception block")
        super().__exit__(exc_type, exc_val, exc_tb)


class Debugger(Service):
    _instance: Optional["Debugger"] = None
    _probe_meter = _probe_metrics.get_meter("probe")

    __rc_adapter__ = ProbeRCAdapter
    __uploader__ = LogsIntakeUploaderV1
    __watchdog__ = DebuggerModuleWatchdog
    __logger__ = ProbeStatusLogger

    @classmethod
    def enable(cls) -> None:
        """Enable dynamic instrumentation

        This class method is idempotent. Dynamic instrumentation will be
        disabled automatically at exit.
        """
        if cls._instance is not None:
            log.debug("%s already enabled", cls.__name__)
            return

        log.debug("Enabling %s", cls.__name__)

        di_config.enabled = True

        if di_config.metrics:
            metrics.enable()

        cls._instance = debugger = cls()

        debugger.start()

        register_post_run_module_hook(cls._on_run_module)

        log.debug("%s enabled", cls.__name__)

        core.dispatch("dynamic-instrumentation.enabled")

    @classmethod
    def disable(cls, join: bool = True) -> None:
        """Disable dynamic instrumentation.

        This class method is idempotent. Called automatically at exit, if
        dynamic instrumentation was enabled.
        """
        if cls._instance is None:
            log.debug("%s not enabled", cls.__name__)
            return

        log.debug("Disabling %s", cls.__name__)

        remoteconfig_poller.unregister("LIVE_DEBUGGING")

        unregister_post_run_module_hook(cls._on_run_module)

        cls._instance.stop(join=join)
        cls._instance = None

        if di_config.metrics:
            metrics.disable()

        di_config.enabled = False

        log.debug("%s disabled", cls.__name__)

    def __init__(self, tracer: Optional[Tracer] = None) -> None:
        super().__init__()

        self._tracer = tracer or ddtrace.tracer
        service_name = di_config.service_name

        self._status_logger = status_logger = self.__logger__(service_name)

        self._probe_registry = ProbeRegistry(status_logger=status_logger)

        self._function_store = FunctionStore(extra_attrs=["__dd_wrappers__"])

        log_limiter = RateLimiter(limit_rate=1.0, raise_on_exceed=False)
        self._global_rate_limiter = RateLimiter(
            limit_rate=di_config.global_rate_limit,  # TODO: Make it configurable. Note that this is per-process!
            on_exceed=lambda: log_limiter.limit(log.warning, "Global rate limit exceeded"),
            call_once=True,
            raise_on_exceed=False,
        )

        self.probe_file = di_config.probe_file

        if di_config.enabled:
            # TODO: this is only temporary and will be reverted once the DD_REMOTE_CONFIGURATION_ENABLED variable
            #  has been removed
            if ddconfig._remote_config_enabled is False:
                ddconfig._remote_config_enabled = True
                log.info("Disabled Remote Configuration enabled by Dynamic Instrumentation.")

            # Register the debugger with the RCM client.
            di_callback = self.__rc_adapter__(None, self._on_configuration, status_logger=status_logger)
            remoteconfig_poller.register("LIVE_DEBUGGING", di_callback, restart_on_fork=True)

            # Load local probes from the probe file.
            self._load_local_config()

        log.debug("%s initialized (service name: %s)", self.__class__.__name__, service_name)

    def _load_local_config(self) -> None:
        if self.probe_file is None:
            return

        # This is intentionally an all or nothing approach. If one probe is malformed, none of the
        # local probes will be installed, that way waiting for the success log guarantees installation.
        try:
            raw_probes = json.loads(self.probe_file.read_text())

            probes = [build_probe(p) for p in raw_probes]

            self._on_configuration(ProbePollerEvent.NEW_PROBES, probes)
            log.info("Successfully loaded probes from file %s: %s", self.probe_file, [p.probe_id for p in probes])

        except Exception as e:
            log.error("Failed to load probes from file %s: %s", self.probe_file, e)

    def _dd_debugger_hook(self, probe: Probe) -> None:
        """Debugger probe hook.

        This gets called with a reference to the probe. We only check whether
        the probe is active. If so, we push the collected data to the collector
        for bulk processing. This way we avoid adding delay while the
        instrumented code is running.
        """
        try:
            try:
                signal = Signal.from_probe(
                    probe,
                    frame=sys._getframe(1),
                    thread=threading.current_thread(),
                    trace_context=self._tracer.current_trace_context(),
                    meter=self._probe_meter,
                )
            except TypeError:
                log.error("Unsupported probe type: %r", type(probe), exc_info=True)
                return

            signal.do_line(self._global_rate_limiter if probe.is_global_rate_limited() else None)

            if signal.state is SignalState.DONE:
                self._probe_registry.set_emitting(probe)

            log.debug("[%s][P: %s] Debugger. Report signal %s", os.getpid(), os.getppid(), signal)
            self.__uploader__.get_collector().push(signal)

        except Exception:
            log.error("Failed to execute probe hook", exc_info=True)

    def _probe_injection_hook(self, module: ModuleType) -> None:
        # This hook is invoked by the ModuleWatchdog or the post run module hook
        # to inject probes.

        # Group probes by function so that we decompile each function once and
        # bulk-inject the probes.
        probes_for_function: Dict[FullyNamedContextWrappedFunction, List[Probe]] = defaultdict(list)
        for probe in self._probe_registry.get_pending(str(origin(module))):
            if not isinstance(probe, LineLocationMixin):
                continue
            line = probe.line
            assert line is not None  # nosec
            functions = FunctionDiscovery.from_module(module).at_line(line)
            if not functions:
                module_origin = str(origin(module))
                if linecache.getline(module_origin, line):
                    # The source actually has a line at the given line number
                    message = (
                        f"Cannot install probe {probe.probe_id}: "
                        f"function at line {line} within source file {module_origin} "
                        "is likely decorated with an unsupported decorator."
                    )
                else:
                    message = (
                        f"Cannot install probe {probe.probe_id}: "
                        f"no functions at line {line} within source file {module_origin} found"
                    )
                log.error(message)
                self._probe_registry.set_error(probe, "NoFunctionsAtLine", message)
                continue
            for function in (cast(FullyNamedContextWrappedFunction, _) for _ in functions):
                probes_for_function[function].append(cast(LineProbe, probe))

        for function, probes in probes_for_function.items():
            failed = self._function_store.inject_hooks(
                function, [(self._dd_debugger_hook, cast(LineProbe, probe).line, probe) for probe in probes]
            )

            for probe in probes:
                if probe.probe_id in failed:
                    self._probe_registry.set_error(probe, "InjectionFailure", "Failed to inject")
                else:
                    self._probe_registry.set_installed(probe)

            if failed:
                log.error("[%s][P: %s] Failed to inject probes %r", os.getpid(), os.getppid(), failed)

            log.debug(
                "[%s][P: %s] Injected probes %r in %r",
                os.getpid(),
                os.getppid(),
                [probe.probe_id for probe in probes if probe.probe_id not in failed],
                function,
            )

    def _inject_probes(self, probes: List[LineProbe]) -> None:
        for probe in probes:
            if probe not in self._probe_registry:
                if len(self._probe_registry) >= di_config.max_probes:
                    log.warning("Too many active probes. Ignoring new ones.")
                    return
                log.debug("[%s][P: %s] Received new %s.", os.getpid(), os.getppid(), probe)
                self._probe_registry.register(probe)

            resolved_source = probe.resolved_source_file
            if resolved_source is None:
                log.error(
                    "Cannot inject probe %s: source file %s cannot be resolved", probe.probe_id, probe.source_file
                )
                self._probe_registry.set_error(probe, "NoSourceFile", "Source file location cannot be resolved")
                continue

        for source in {probe.resolved_source_file for probe in probes if probe.resolved_source_file is not None}:
            try:
                self.__watchdog__.register_origin_hook(source, self._probe_injection_hook)
            except Exception as exc:
                for probe in probes:
                    if probe.resolved_source_file != source:
                        continue
                    exc_type = type(exc)
                    self._probe_registry.set_error(probe, exc_type.__name__, str(exc))
                log.error("Cannot register probe injection hook on source '%s'", source, exc_info=True)

    def _eject_probes(self, probes_to_eject: List[LineProbe]) -> None:
        # TODO[perf]: Bulk-collect probes as for injection. This is lower
        # priority as probes are normally removed manually by users.
        unregistered_probes: List[LineProbe] = []
        for probe in probes_to_eject:
            if probe not in self._probe_registry:
                log.error("Attempted to eject unregistered probe %r", probe)
                continue

            (registered_probe,) = self._probe_registry.unregister(probe)
            unregistered_probes.append(cast(LineProbe, registered_probe))

        probes_for_source: Dict[Path, List[LineProbe]] = defaultdict(list)
        for probe in unregistered_probes:
            if probe.resolved_source_file is None:
                continue
            probes_for_source[probe.resolved_source_file].append(probe)

        for resolved_source, probes in probes_for_source.items():
            module = self.__watchdog__.get_by_origin(resolved_source)
            if module is not None:
                # The module is still loaded, so we can try to eject the hooks
                probes_for_function: Dict[FullyNamedContextWrappedFunction, List[LineProbe]] = defaultdict(list)
                for probe in probes:
                    if not isinstance(probe, LineLocationMixin):
                        continue
                    line = probe.line
                    assert line is not None, probe  # nosec
                    functions = FunctionDiscovery.from_module(module).at_line(line)
                    for function in (cast(FullyNamedContextWrappedFunction, _) for _ in functions):
                        probes_for_function[function].append(probe)

                for function, ps in probes_for_function.items():
                    failed = self._function_store.eject_hooks(
                        cast(FunctionType, function),
                        [(self._dd_debugger_hook, probe.line, probe) for probe in ps if probe.line is not None],
                    )
                    for probe in ps:
                        if probe.probe_id in failed:
                            log.error("Failed to eject %r from %r", probe, function)
                        else:
                            log.debug("Ejected %r from %r", probe, function)

            if not self._probe_registry.has_probes(str(resolved_source)):
                try:
                    self.__watchdog__.unregister_origin_hook(resolved_source, self._probe_injection_hook)
                    log.debug("Unregistered injection hook on source '%s'", resolved_source)
                except ValueError:
                    log.error("Cannot unregister injection hook on %r", resolved_source, exc_info=True)

    def _probe_wrapping_hook(self, module: ModuleType) -> None:
        probes = self._probe_registry.get_pending(module.__name__)
        for probe in probes:
            if not isinstance(probe, FunctionLocationMixin):
                continue

            try:
                assert probe.module is not None and probe.func_qname is not None  # nosec
                function = cast(FunctionType, FunctionDiscovery.from_module(module).by_name(probe.func_qname))
            except ValueError:
                message = (
                    f"Cannot install probe {probe.probe_id}: no function '{probe.func_qname}' in module {probe.module}"
                    "found (note: if the function exists, it might be decorated with an unsupported decorator)"
                )
                self._probe_registry.set_error(probe, "NoFunctionInModule", message)
                log.error(message)
                continue

            if DebuggerWrappingContext.is_wrapped(function):
                context = cast(DebuggerWrappingContext, DebuggerWrappingContext.extract(function))
                log.debug(
                    "[%s][P: %s] Function probe %r added to already wrapped %r",
                    os.getpid(),
                    os.getppid(),
                    probe.probe_id,
                    function,
                )
            else:
                context = DebuggerWrappingContext(
                    function,
                    collector=self.__uploader__.get_collector(),
                    registry=self._probe_registry,
                    tracer=self._tracer,
                    probe_meter=self._probe_meter,
                )
                self._function_store.wrap(cast(FunctionType, function), context)
                log.debug(
                    "[%s][P: %s] Function probe %r wrapped around %r",
                    os.getpid(),
                    os.getppid(),
                    probe.probe_id,
                    function,
                )

            context.add_probe(probe)
            self._probe_registry.set_installed(probe)

    def _wrap_functions(self, probes: List[FunctionProbe]) -> None:
        for probe in probes:
            if len(self._probe_registry) >= di_config.max_probes:
                log.warning("Too many active probes. Ignoring new ones.")
                return

            self._probe_registry.register(probe)
            try:
                assert probe.module is not None  # nosec
                self.__watchdog__.register_module_hook(probe.module, self._probe_wrapping_hook)
            except Exception as exc:
                exc_type = type(exc)
                self._probe_registry.set_error(probe, exc_type.__name__, str(exc))
                log.error("Cannot register probe wrapping hook on module '%s'", probe.module, exc_info=True)

    def _unwrap_functions(self, probes: List[FunctionProbe]) -> None:
        # Keep track of all the modules involved to see if there are any import
        # hooks that we can clean up at the end.
        touched_modules: Set[str] = set()

        for probe in probes:
            registered_probes = self._probe_registry.unregister(probe)
            if not registered_probes:
                log.error("Attempted to eject unregistered probe %r", probe)
                continue

            (registered_probe,) = registered_probes

            assert probe.module is not None  # nosec
            module = sys.modules.get(probe.module, None)
            if module is not None:
                # The module is still loaded, so we can try to unwrap the function
                touched_modules.add(probe.module)
                assert probe.func_qname is not None  # nosec
                function = cast(FunctionType, FunctionDiscovery.from_module(module).by_name(probe.func_qname))
                if DebuggerWrappingContext.is_wrapped(function):
                    context = cast(DebuggerWrappingContext, DebuggerWrappingContext.extract(function))
                    context.remove_probe(probe)
                    if not context.has_probes():
                        self._function_store.unwrap(cast(FullyNamedContextWrappedFunction, function))
                    log.debug("Unwrapped %r", registered_probe)
                else:
                    log.error("Attempted to unwrap %r, but no wrapper found", registered_probe)

        # Clean up import hooks.
        for module_name in touched_modules:
            if not self._probe_registry.has_probes(module_name):
                try:
                    self.__watchdog__.unregister_module_hook(module_name, self._probe_wrapping_hook)
                    log.debug("Unregistered wrapping import hook on module %s", module_name)
                except ValueError:
                    log.error("Cannot unregister wrapping import hook for module %r", module_name, exc_info=True)

    def _on_configuration(self, event: ProbePollerEventType, probes: Iterable[Probe]) -> None:
        log.debug("[%s][P: %s] Received poller event %r with probes %r", os.getpid(), os.getppid(), event, probes)

        if event == ProbePollerEvent.STATUS_UPDATE:
            self._probe_registry.log_probes_status()
            return

        if event == ProbePollerEvent.MODIFIED_PROBES:
            for probe in probes:
                if probe in self._probe_registry:
                    registered_probe = self._probe_registry.get(probe.probe_id)
                    if registered_probe is None:
                        # We didn't have the probe. This shouldn't have happened!
                        log.error("Modified probe %r was not found in registry.", probe)
                        continue
                    self._probe_registry.update(probe)

            return

        line_probes: List[LineProbe] = []
        function_probes: List[FunctionProbe] = []
        for probe in probes:
            if isinstance(probe, LineLocationMixin):
                line_probes.append(cast(LineProbe, probe))
            elif isinstance(probe, FunctionLocationMixin):
                function_probes.append(cast(FunctionProbe, probe))
            else:
                log.warning("Skipping probe '%r': not supported.", probe)

        if event == ProbePollerEvent.NEW_PROBES:
            self._inject_probes(line_probes)
            self._wrap_functions(function_probes)
        elif event == ProbePollerEvent.DELETED_PROBES:
            self._eject_probes(line_probes)
            self._unwrap_functions(function_probes)
        else:
            raise ValueError("Unknown probe poller event %r" % event)

    def _stop_service(self, join: bool = True) -> None:
        self._function_store.restore_all()
        self.__uploader__.unregister(UploaderProduct.DEBUGGER)

    def _start_service(self) -> None:
        self.__uploader__.register(UploaderProduct.DEBUGGER)

    @classmethod
    def _on_run_module(cls, module: ModuleType) -> None:
        debugger = cls._instance
        if debugger is not None:
            debugger.__watchdog__.on_run_module(module)
