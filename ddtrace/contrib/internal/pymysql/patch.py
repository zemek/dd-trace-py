import os
from typing import Dict

import pymysql
import wrapt

from ddtrace import config
from ddtrace.contrib.dbapi import TracedConnection
from ddtrace.contrib.internal.trace_utils import _convert_to_string
from ddtrace.ext import db
from ddtrace.ext import net
from ddtrace.internal.schema import schematize_database_operation
from ddtrace.internal.schema import schematize_service_name
from ddtrace.internal.utils.formats import asbool
from ddtrace.propagation._database_monitoring import _DBM_Propagator
from ddtrace.trace import Pin


config._add(
    "pymysql",
    dict(
        _default_service=schematize_service_name("pymysql"),
        _dbapi_span_name_prefix="pymysql",
        _dbapi_span_operation_name=schematize_database_operation("pymysql.query", database_provider="mysql"),
        trace_fetch_methods=asbool(os.getenv("DD_PYMYSQL_TRACE_FETCH_METHODS", default=False)),
        _dbm_propagator=_DBM_Propagator(0, "query"),
    ),
)


def get_version():
    # type: () -> str
    return getattr(pymysql, "__version__", "")


CONN_ATTR_BY_TAG = {
    net.TARGET_HOST: "host",
    net.TARGET_PORT: "port",
    net.SERVER_ADDRESS: "host",
    db.USER: "user",
    db.NAME: "db",
}


def _supported_versions() -> Dict[str, str]:
    return {"pymysql": ">=0.10"}


def patch():
    wrapt.wrap_function_wrapper("pymysql", "connect", _connect)
    pymysql._datadog_patch = True


def unpatch():
    if isinstance(pymysql.connect, wrapt.ObjectProxy):
        pymysql.connect = pymysql.connect.__wrapped__
    pymysql._datadog_patch = False


def _connect(func, instance, args, kwargs):
    conn = func(*args, **kwargs)
    return patch_conn(conn)


def patch_conn(conn):
    tags = {t: _convert_to_string(getattr(conn, a)) for t, a in CONN_ATTR_BY_TAG.items() if getattr(conn, a, "") != ""}
    tags[db.SYSTEM] = "mysql"
    pin = Pin(tags=tags)

    # grab the metadata from the conn
    wrapped = TracedConnection(conn, pin=pin, cfg=config.pymysql)
    pin.onto(wrapped)
    return wrapped
