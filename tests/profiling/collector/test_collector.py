import pytest

from ddtrace.profiling import collector


def _test_repr(collector_class, s):
    assert repr(collector_class()) == s


def _test_restart(collector, **kwargs):
    c = collector(**kwargs)
    c.start()
    c.stop()
    c.join()
    c.start()
    with pytest.raises(RuntimeError):
        c.start()
    c.stop()
    c.join()


def test_dynamic_interval():
    c = collector.PeriodicCollector(interval=1)
    c.start()
    assert c.interval == 1
    assert c._worker.interval == c.interval
    c.interval = 2
    assert c.interval == 2
    assert c._worker.interval == c.interval
    c.stop()


def test_thread_name():
    c = collector.PeriodicCollector(interval=1)
    c.start()
    assert c._worker.name == "ddtrace.profiling.collector:PeriodicCollector"
    c.stop()


def test_capture_sampler():
    cs = collector.CaptureSampler(15)
    assert cs.capture() is False  # 15
    assert cs.capture() is False  # 30
    assert cs.capture() is False  # 45
    assert cs.capture() is False  # 60
    assert cs.capture() is False  # 75
    assert cs.capture() is False  # 90
    assert cs.capture() is True  # 5
    assert cs.capture() is False  # 20
    assert cs.capture() is False  # 35
    assert cs.capture() is False  # 50
    assert cs.capture() is False  # 65
    assert cs.capture() is False  # 80
    assert cs.capture() is False  # 95
    assert cs.capture() is True  # 10
    assert cs.capture() is False  # 25
    assert cs.capture() is False  # 40
    assert cs.capture() is False  # 55
    assert cs.capture() is False  # 70
    assert cs.capture() is False  # 85
    assert cs.capture() is True  # 0
    assert cs.capture() is False  # 15


def test_capture_sampler_bad_value():
    with pytest.raises(ValueError):
        collector.CaptureSampler(-1)

    with pytest.raises(ValueError):
        collector.CaptureSampler(102)
