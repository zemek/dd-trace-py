import os
from typing import Dict

import celery

from ddtrace import config
from ddtrace.contrib.internal.celery.app import patch_app
from ddtrace.contrib.internal.celery.app import unpatch_app
from ddtrace.contrib.internal.celery.constants import PRODUCER_SERVICE
from ddtrace.contrib.internal.celery.constants import WORKER_SERVICE
from ddtrace.internal.utils.formats import asbool


# Celery default settings
config._add(
    "celery",
    {
        "distributed_tracing": asbool(os.getenv("DD_CELERY_DISTRIBUTED_TRACING", default=False)),
        "producer_service_name": os.getenv("DD_CELERY_PRODUCER_SERVICE_NAME", default=PRODUCER_SERVICE),
        "worker_service_name": os.getenv("DD_CELERY_WORKER_SERVICE_NAME", default=WORKER_SERVICE),
    },
)


def get_version():
    # type: () -> str
    return str(celery.__version__)


def _supported_versions() -> Dict[str, str]:
    return {"celery": ">=4.4"}


def patch():
    """Instrument Celery base application and the `TaskRegistry` so
    that any new registered task is automatically instrumented. In the
    case of Django-Celery integration, also the `@shared_task` decorator
    must be instrumented because Django doesn't use the Celery registry.
    """
    patch_app(celery.Celery)


def unpatch():
    """Disconnect all signals and remove Tracing capabilities"""
    unpatch_app(celery.Celery)
