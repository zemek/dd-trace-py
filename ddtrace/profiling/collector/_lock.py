from __future__ import absolute_import

import _thread
import abc
import os.path
import sys
import time
import types
import typing

import wrapt

from ddtrace.internal.datadog.profiling import ddup
from ddtrace.profiling import _threading
from ddtrace.profiling import collector
from ddtrace.profiling.collector import _task
from ddtrace.profiling.collector import _traceback
from ddtrace.settings.profiling import config
from ddtrace.trace import Tracer


def _current_thread():
    # type: (...) -> typing.Tuple[int, str]
    thread_id = _thread.get_ident()
    return thread_id, _threading.get_thread_name(thread_id)


# We need to know if wrapt is compiled in C or not. If it's not using the C module, then the wrappers function will
# appear in the stack trace and we need to hide it.
if os.environ.get("WRAPT_DISABLE_EXTENSIONS"):
    WRAPT_C_EXT = False
else:
    try:
        import wrapt._wrappers as _w  # noqa: F401
    except ImportError:
        WRAPT_C_EXT = False
    else:
        WRAPT_C_EXT = True
        del _w


class _ProfiledLock(wrapt.ObjectProxy):
    def __init__(
        self,
        wrapped: typing.Any,
        tracer: typing.Optional[Tracer],
        max_nframes: int,
        capture_sampler: collector.CaptureSampler,
        endpoint_collection_enabled: bool,
    ) -> None:
        wrapt.ObjectProxy.__init__(self, wrapped)
        self._self_tracer = tracer
        self._self_max_nframes = max_nframes
        self._self_capture_sampler = capture_sampler
        self._self_endpoint_collection_enabled = endpoint_collection_enabled
        frame = sys._getframe(2 if WRAPT_C_EXT else 3)
        code = frame.f_code
        self._self_init_loc = "%s:%d" % (os.path.basename(code.co_filename), frame.f_lineno)
        self._self_name: typing.Optional[str] = None

    def __aenter__(self, *args, **kwargs):
        return self._acquire(self.__wrapped__.__aenter__, *args, **kwargs)

    def __aexit__(self, *args, **kwargs):
        return self._release(self.__wrapped__.__aexit__, *args, **kwargs)

    def _acquire(self, inner_func, *args, **kwargs):
        if not self._self_capture_sampler.capture():
            return inner_func(*args, **kwargs)

        start = time.monotonic_ns()
        try:
            return inner_func(*args, **kwargs)
        finally:
            try:
                end = self._self_acquired_at = time.monotonic_ns()
                thread_id, thread_name = _current_thread()
                task_id, task_name, task_frame = _task.get_task(thread_id)
                self._maybe_update_self_name()
                lock_name = "%s:%s" % (self._self_init_loc, self._self_name) if self._self_name else self._self_init_loc

                if task_frame is None:
                    # If we can't get the task frame, we use the caller frame. We expect acquire/release or
                    # __enter__/__exit__ to be on the stack, so we go back 2 frames.
                    frame = sys._getframe(2)
                else:
                    frame = task_frame

                frames, _ = _traceback.pyframe_to_frames(frame, self._self_max_nframes)

                thread_native_id = _threading.get_thread_native_id(thread_id)

                handle = ddup.SampleHandle()
                handle.push_monotonic_ns(end)
                handle.push_lock_name(lock_name)
                handle.push_acquire(end - start, 1)  # AFAICT, capture_pct does not adjust anything here
                handle.push_threadinfo(thread_id, thread_native_id, thread_name)
                handle.push_task_id(task_id)
                handle.push_task_name(task_name)

                if self._self_tracer is not None:
                    handle.push_span(self._self_tracer.current_span())
                for frame in frames:
                    handle.push_frame(frame.function_name, frame.file_name, 0, frame.lineno)
                handle.flush_sample()
            except Exception:
                pass  # nosec

    def acquire(self, *args, **kwargs):
        return self._acquire(self.__wrapped__.acquire, *args, **kwargs)

    def _release(self, inner_func, *args, **kwargs):
        # type (typing.Any, typing.Any) -> None

        # The underlying threading.Lock class is implemented using C code, and
        # it doesn't have the __dict__ attribute. So we can't do
        # self.__dict__.pop("_self_acquired_at", None) to remove the attribute.
        # Instead, we need to use the following workaround to retrieve and
        # remove the attribute.
        start = getattr(self, "_self_acquired_at", None)
        try:
            # Though it should generally be avoided to call release() from
            # multiple threads, it is possible to do so. In that scenario, the
            # following statement code will raise an AttributeError. This should
            # not be propagated to the caller and to the users. The inner_func
            # will raise an RuntimeError as the threads are trying to release()
            # and unlocked lock, and the expected behavior is to propagate that.
            del self._self_acquired_at
        except AttributeError:
            # We just ignore the error, if the attribute is not found.
            pass
        try:
            return inner_func(*args, **kwargs)
        finally:
            if start is not None:
                end = time.monotonic_ns()
                thread_id, thread_name = _current_thread()
                task_id, task_name, task_frame = _task.get_task(thread_id)
                lock_name = "%s:%s" % (self._self_init_loc, self._self_name) if self._self_name else self._self_init_loc

                if task_frame is None:
                    # See the comments in _acquire
                    frame = sys._getframe(2)
                else:
                    frame = task_frame

                frames, _ = _traceback.pyframe_to_frames(frame, self._self_max_nframes)

                thread_native_id = _threading.get_thread_native_id(thread_id)

                handle = ddup.SampleHandle()
                handle.push_monotonic_ns(end)
                handle.push_lock_name(lock_name)
                handle.push_release(end - start, 1)  # AFAICT, capture_pct does not adjust anything here
                handle.push_threadinfo(thread_id, thread_native_id, thread_name)
                handle.push_task_id(task_id)
                handle.push_task_name(task_name)

                if self._self_tracer is not None:
                    handle.push_span(self._self_tracer.current_span())
                for frame in frames:
                    handle.push_frame(frame.function_name, frame.file_name, 0, frame.lineno)
                handle.flush_sample()

    def release(self, *args, **kwargs):
        return self._release(self.__wrapped__.release, *args, **kwargs)

    acquire_lock = acquire

    def __enter__(self, *args, **kwargs):
        return self._acquire(self.__wrapped__.__enter__, *args, **kwargs)

    def __exit__(self, *args, **kwargs):
        self._release(self.__wrapped__.__exit__, *args, **kwargs)

    def _find_self_name(self, var_dict: typing.Dict):
        for name, value in var_dict.items():
            if name.startswith("__") or isinstance(value, types.ModuleType):
                continue
            if value is self:
                return name
            if config.lock.name_inspect_dir:
                for attribute in dir(value):
                    if not attribute.startswith("__") and getattr(value, attribute) is self:
                        self._self_name = attribute
                        return attribute
        return None

    # Get lock acquire/release call location and variable name the lock is assigned to
    # This function propagates ValueError if the frame depth is <= 3.
    def _maybe_update_self_name(self):
        if self._self_name is not None:
            return
        # We expect the call stack to be like this:
        # 0: this
        # 1: _acquire/_release
        # 2: acquire/release (or __enter__/__exit__)
        # 3: caller frame
        if config.enable_asserts:
            frame = sys._getframe(1)
            if frame.f_code.co_name not in {"_acquire", "_release"}:
                raise AssertionError("Unexpected frame %s" % frame.f_code.co_name)
            frame = sys._getframe(2)
            if frame.f_code.co_name not in {
                "acquire",
                "release",
                "__enter__",
                "__exit__",
                "__aenter__",
                "__aexit__",
            }:
                raise AssertionError("Unexpected frame %s" % frame.f_code.co_name)
        frame = sys._getframe(3)

        # First, look at the local variables of the caller frame, and then the global variables
        self._self_name = self._find_self_name(frame.f_locals) or self._find_self_name(frame.f_globals)

        if not self._self_name:
            self._self_name = ""


class FunctionWrapper(wrapt.FunctionWrapper):
    # Override the __get__ method: whatever happens, _allocate_lock is always considered by Python like a "static"
    # method, even when used as a class attribute. Python never tried to "bind" it to a method, because it sees it is a
    # builtin function. Override default wrapt behavior here that tries to detect bound method.
    def __get__(self, instance, owner=None):
        return self


class LockCollector(collector.CaptureSamplerCollector):
    """Record lock usage."""

    def __init__(
        self,
        nframes=config.max_frames,
        endpoint_collection_enabled=config.endpoint_collection,
        tracer=None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.nframes = nframes
        self.endpoint_collection_enabled = endpoint_collection_enabled
        self.tracer = tracer
        self._original = None

    @abc.abstractmethod
    def _get_patch_target(self):
        # type: (...) -> typing.Any
        pass

    @abc.abstractmethod
    def _set_patch_target(
        self,
        value,  # type: typing.Any
    ):
        # type: (...) -> None
        pass

    def _start_service(self):
        # type: (...) -> None
        """Start collecting lock usage."""
        self.patch()
        super(LockCollector, self)._start_service()  # type: ignore[safe-super]

    def _stop_service(self):
        # type: (...) -> None
        """Stop collecting lock usage."""
        super(LockCollector, self)._stop_service()  # type: ignore[safe-super]
        self.unpatch()

    def patch(self):
        # type: (...) -> None
        """Patch the module for tracking lock allocation."""
        # We only patch the lock from the `threading` module.
        # Nobody should use locks from `_thread`; if they do so, then it's deliberate and we don't profile.
        self._original = self._get_patch_target()

        def _allocate_lock(wrapped, instance, args, kwargs):
            lock = wrapped(*args, **kwargs)
            return self.PROFILED_LOCK_CLASS(
                lock,
                self.tracer,
                self.nframes,
                self._capture_sampler,
                self.endpoint_collection_enabled,
            )

        self._set_patch_target(FunctionWrapper(self._original, _allocate_lock))

    def unpatch(self):
        # type: (...) -> None
        """Unpatch the threading module for tracking lock allocation."""
        self._set_patch_target(self._original)
