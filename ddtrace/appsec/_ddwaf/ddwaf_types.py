import ctypes
import ctypes.util
from enum import IntEnum
from platform import machine
from platform import system
from typing import Any
from typing import Dict
from typing import List
from typing import Union

from ddtrace.appsec._ddwaf.waf_stubs import ddwaf_builder_capsule
from ddtrace.appsec._ddwaf.waf_stubs import ddwaf_context_capsule
from ddtrace.appsec._ddwaf.waf_stubs import ddwaf_handle_capsule
from ddtrace.appsec._utils import _observator
from ddtrace.appsec._utils import unpatching_popen
from ddtrace.internal.logger import get_logger
from ddtrace.settings.asm import config as asm_config


DDWafRulesType = Union[None, int, str, List[Any], Dict[str, Any]]

log = get_logger(__name__)

#
# Dynamic loading of libddwaf. For now it requires the file or a link to be in current directory
#

if system() == "Linux":
    try:
        with unpatching_popen():
            ctypes.CDLL(ctypes.util.find_library("rt"), mode=ctypes.RTLD_GLOBAL)
    except Exception:  # nosec
        pass

ARCHI = machine().lower()

# 32-bit-Python on 64-bit-Windows

with unpatching_popen():
    ddwaf = ctypes.CDLL(asm_config._asm_libddwaf)
#
# Constants
#

DDWAF_MAX_STRING_LENGTH = 4096
DDWAF_MAX_CONTAINER_DEPTH = 20
DDWAF_MAX_CONTAINER_SIZE = 256
DDWAF_NO_LIMIT = 1 << 31
DDWAF_DEPTH_NO_LIMIT = 1000


class DDWAF_OBJ_TYPE(IntEnum):
    DDWAF_OBJ_INVALID = 0
    # Value shall be decoded as a int64_t (or int32_t on 32bits platforms).
    DDWAF_OBJ_SIGNED = 1 << 0
    # Value shall be decoded as a uint64_t (or uint32_t on 32bits platforms).
    DDWAF_OBJ_UNSIGNED = 1 << 1
    # Value shall be decoded as a UTF-8 string of length nbEntries.
    DDWAF_OBJ_STRING = 1 << 2
    # Value shall be decoded as an array of ddwaf_object of length nbEntries, each item having no parameterName.
    DDWAF_OBJ_ARRAY = 1 << 3
    # Value shall be decoded as an array of ddwaf_object of length nbEntries, each item having a parameterName.
    DDWAF_OBJ_MAP = 1 << 4
    # Value shall be decode as bool
    DDWAF_OBJ_BOOL = 1 << 5
    # 64-bit float (or double) type
    DDWAF_OBJ_FLOAT = 1 << 6
    # Null type, only used for its semantical value
    DDWAF_OBJ_NULL = 1 << 7


class DDWAF_RET_CODE(IntEnum):
    DDWAF_ERR_INTERNAL = -3
    DDWAF_ERR_INVALID_OBJECT = -2
    DDWAF_ERR_INVALID_ARGUMENT = -1
    DDWAF_OK = 0
    DDWAF_MATCH = 1


class DDWAF_LOG_LEVEL(IntEnum):
    DDWAF_LOG_TRACE = 0
    DDWAF_LOG_DEBUG = 1
    DDWAF_LOG_INFO = 2
    DDWAF_LOG_WARN = 3
    DDWAF_LOG_ERROR = 4
    DDWAF_LOG_OFF = 5


#
# Objects Definitions
#


# to allow cyclic references, ddwaf_object fields are defined later
class ddwaf_object(ctypes.Structure):
    # "type" define how to read the "value" union field
    # defined in ddwaf.h
    #  1 is intValue
    #  2 is uintValue
    #  4 is stringValue as UTF-8 encoded
    #  8 is array of length "nbEntries" without parameterName
    # 16 is a map : array of length "nbEntries" with parameterName
    # 32 is boolean

    def __init__(
        self,
        struct: DDWafRulesType = None,
        observator: _observator = _observator(),  # noqa : B008
        max_objects: int = DDWAF_MAX_CONTAINER_SIZE,
        max_depth: int = DDWAF_MAX_CONTAINER_DEPTH,
        max_string_length: int = DDWAF_MAX_STRING_LENGTH,
    ) -> None:
        def truncate_string(string: bytes) -> bytes:
            if len(string) > max_string_length:
                observator.set_string_length(len(string))
                # difference of 1 to take null char at the end on the C side into account
                return string[:max_string_length]
            return string

        if isinstance(struct, bool):
            ddwaf_object_bool(self, struct)
        elif isinstance(struct, int):
            ddwaf_object_signed(self, struct)
        elif isinstance(struct, str):
            ddwaf_object_string(self, truncate_string(struct.encode("UTF-8", errors="ignore")))
        elif isinstance(struct, bytes):
            ddwaf_object_string(self, truncate_string(struct))
        elif isinstance(struct, float):
            ddwaf_object_float(self, struct)
        elif isinstance(struct, list):
            if max_depth <= 0:
                observator.set_container_depth(DDWAF_MAX_CONTAINER_DEPTH)
                max_objects = 0
            array = ddwaf_object_array(self)
            for counter_object, elt in enumerate(struct):
                if counter_object >= max_objects:
                    observator.set_container_size(len(struct))
                    break
                obj = ddwaf_object(
                    elt,
                    observator=observator,
                    max_objects=max_objects,
                    max_depth=max_depth - 1,
                    max_string_length=max_string_length,
                )
                ddwaf_object_array_add(array, obj)
        elif isinstance(struct, dict):
            if max_depth <= 0:
                observator.set_container_depth(DDWAF_MAX_CONTAINER_DEPTH)
                max_objects = 0
            map_o = ddwaf_object_map(self)
            # order is unspecified and could lead to problems if max_objects is reached
            for counter_object, (key, val) in enumerate(struct.items()):
                if not isinstance(key, (bytes, str)):  # discards non string keys
                    continue
                if counter_object >= max_objects:
                    observator.set_container_size(len(struct))
                    break
                res_key = truncate_string(key.encode("UTF-8", errors="ignore") if isinstance(key, str) else key)
                obj = ddwaf_object(
                    val,
                    observator=observator,
                    max_objects=max_objects,
                    max_depth=max_depth - 1,
                    max_string_length=max_string_length,
                )
                ddwaf_object_map_add(map_o, res_key, obj)
        elif struct is not None:
            ddwaf_object_string(self, truncate_string(str(struct).encode("UTF-8", errors="ignore")))
        else:
            ddwaf_object_null(self)

    @classmethod
    def create_without_limits(cls, struct: DDWafRulesType) -> "ddwaf_object":
        return cls(struct, max_objects=DDWAF_NO_LIMIT, max_depth=DDWAF_DEPTH_NO_LIMIT, max_string_length=DDWAF_NO_LIMIT)

    @property
    def struct(self) -> DDWafRulesType:
        """Generate a python structure from ddwaf_object"""
        if self.type == DDWAF_OBJ_TYPE.DDWAF_OBJ_STRING:
            return self.value.stringValue[: self.nbEntries].decode("UTF-8", errors="ignore")
        if self.type == DDWAF_OBJ_TYPE.DDWAF_OBJ_MAP:
            return {
                obj.parameterName[: obj.parameterNameLength].decode("UTF-8", errors="ignore"): obj.struct
                for obj in self.value.array[: self.nbEntries]
            }
        if self.type == DDWAF_OBJ_TYPE.DDWAF_OBJ_ARRAY:
            return [self.value.array[i].struct for i in range(self.nbEntries)]
        if self.type == DDWAF_OBJ_TYPE.DDWAF_OBJ_SIGNED:
            return self.value.intValue
        if self.type == DDWAF_OBJ_TYPE.DDWAF_OBJ_UNSIGNED:
            return self.value.uintValue
        if self.type == DDWAF_OBJ_TYPE.DDWAF_OBJ_BOOL:
            return self.value.boolean
        if self.type == DDWAF_OBJ_TYPE.DDWAF_OBJ_FLOAT:
            return self.value.f64
        if self.type == DDWAF_OBJ_TYPE.DDWAF_OBJ_NULL or self.type == DDWAF_OBJ_TYPE.DDWAF_OBJ_INVALID:
            return None
        log.debug("ddwaf_object struct: unknown object type: %s", repr(type(self.type)))
        return None

    def __repr__(self):
        return repr(self.struct)


ddwaf_object_p = ctypes.POINTER(ddwaf_object)


class ddwaf_value(ctypes.Union):
    _fields_ = [
        ("stringValue", ctypes.POINTER(ctypes.c_char)),
        ("uintValue", ctypes.c_ulonglong),
        ("intValue", ctypes.c_longlong),
        ("array", ddwaf_object_p),
        ("boolean", ctypes.c_bool),
        ("f64", ctypes.c_double),
    ]


ddwaf_object._fields_ = [
    ("parameterName", ctypes.POINTER(ctypes.c_char)),
    ("parameterNameLength", ctypes.c_uint64),
    ("value", ddwaf_value),
    ("nbEntries", ctypes.c_uint64),
    ("type", ctypes.c_int),
]


ddwaf_object_free_fn = ctypes.CFUNCTYPE(None, ddwaf_object_p)
ddwaf_object_free = ddwaf_object_free_fn(
    ("ddwaf_object_free", ddwaf),
    ((1, "object"),),
)


class ddwaf_config_limits(ctypes.Structure):
    _fields_ = [
        ("max_container_size", ctypes.c_uint32),
        ("max_container_depth", ctypes.c_uint32),
        ("max_string_length", ctypes.c_uint32),
    ]


class ddwaf_config_obfuscator(ctypes.Structure):
    _fields_ = [
        ("key_regex", ctypes.c_char_p),
        ("value_regex", ctypes.c_char_p),
    ]


class ddwaf_config(ctypes.Structure):
    _fields_ = [
        ("limits", ddwaf_config_limits),
        ("obfuscator", ddwaf_config_obfuscator),
        ("free_fn", ddwaf_object_free_fn),
    ]
    # TODO : initial value of free_fn

    def __init__(
        self,
        max_container_size: int = 0,
        max_container_depth: int = 0,
        max_string_length: int = 0,
        key_regex: bytes = b"",
        value_regex: bytes = b"",
        free_fn=ddwaf_object_free,
    ) -> None:
        self.limits.max_container_size = max_container_size
        self.limits.max_container_depth = max_container_depth
        self.limits.max_string_length = max_string_length
        self.obfuscator.key_regex = key_regex
        self.obfuscator.value_regex = value_regex
        self.free_fn = free_fn


ddwaf_config_p = ctypes.POINTER(ddwaf_config)

ddwaf_handle = ctypes.c_void_p  # may stay as this because it's mainly an abstract type in the interface
ddwaf_context = ctypes.c_void_p  # may stay as this because it's mainly an abstract type in the interface
ddwaf_builder = ctypes.c_void_p  # may stay as this because it's mainly an abstract type in the interface


ddwaf_log_cb = ctypes.POINTER(
    ctypes.CFUNCTYPE(
        None, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_char_p, ctypes.c_uint64
    )
)


#
# Functions Prototypes (creating python counterpart function from C function with )
#

ddwaf_init = ctypes.CFUNCTYPE(ddwaf_handle, ddwaf_object_p, ddwaf_config_p, ddwaf_object_p)(
    ("ddwaf_init", ddwaf),
    (
        (1, "ruleset_map"),
        (1, "config", None),
        (1, "diagnostics", None),
    ),
)


def py_ddwaf_init(ruleset_map: ddwaf_object, config, info) -> ddwaf_handle_capsule:
    return ddwaf_handle_capsule(ddwaf_init(ruleset_map, config, info), ddwaf_destroy)


ddwaf_destroy = ctypes.CFUNCTYPE(None, ddwaf_handle)(
    ("ddwaf_destroy", ddwaf),
    ((1, "handle"),),
)

ddwaf_known_addresses = ctypes.CFUNCTYPE(
    ctypes.POINTER(ctypes.c_char_p), ddwaf_handle, ctypes.POINTER(ctypes.c_uint32)
)(
    ("ddwaf_known_addresses", ddwaf),
    (
        (1, "handle"),
        (1, "size"),
    ),
)


def py_ddwaf_known_addresses(handle: ddwaf_handle_capsule) -> List[str]:
    size = ctypes.c_uint32()
    obj = ddwaf_known_addresses(handle.handle, size)
    return [obj[i].decode("UTF-8") for i in range(size.value)]


ddwaf_context_init = ctypes.CFUNCTYPE(ddwaf_context, ddwaf_handle)(
    ("ddwaf_context_init", ddwaf),
    ((1, "handle"),),
)


def py_ddwaf_context_init(handle: ddwaf_handle_capsule) -> ddwaf_context_capsule:
    return ddwaf_context_capsule(ddwaf_context_init(handle.handle), ddwaf_context_destroy)


ddwaf_run = ctypes.CFUNCTYPE(
    ctypes.c_int, ddwaf_context, ddwaf_object_p, ddwaf_object_p, ddwaf_object_p, ctypes.c_uint64
)(("ddwaf_run", ddwaf), ((1, "context"), (1, "persistent_data"), (1, "ephemeral_data"), (1, "result"), (1, "timeout")))

ddwaf_context_destroy = ctypes.CFUNCTYPE(None, ddwaf_context)(
    ("ddwaf_context_destroy", ddwaf),
    ((1, "context"),),
)


## ddwf_builder


ddwaf_builder_init = ctypes.CFUNCTYPE(ddwaf_builder, ddwaf_config_p)(
    ("ddwaf_builder_init", ddwaf),
    ((1, "config"),),
)


def py_ddwaf_builder_init(config: ddwaf_config) -> ddwaf_builder_capsule:
    return ddwaf_builder_capsule(ddwaf_builder_init(config), ddwaf_builder_destroy)


ddwaf_builder_add_or_update_config = ctypes.CFUNCTYPE(
    ctypes.c_bool, ddwaf_builder, ctypes.c_char_p, ctypes.c_uint32, ddwaf_object_p, ddwaf_object_p
)(
    ("ddwaf_builder_add_or_update_config", ddwaf),
    (
        (1, "builder"),
        (1, "path"),
        (1, "path_len"),
        (1, "config"),
        (1, "diagnostics"),
    ),
)


def py_add_or_update_config(
    builder: ddwaf_builder_capsule, path: str, config: ddwaf_object, diagnostics: ddwaf_object
) -> bool:
    bin_path = path.encode()
    return ddwaf_builder_add_or_update_config(builder.builder, bin_path, len(bin_path), config, diagnostics)


ddwaf_builder_remove_config = ctypes.CFUNCTYPE(ctypes.c_bool, ddwaf_builder, ctypes.c_char_p, ctypes.c_uint32)(
    ("ddwaf_builder_remove_config", ddwaf),
    (
        (1, "builder"),
        (1, "path"),
        (1, "path_len"),
    ),
)


def py_remove_config(builder: ddwaf_builder_capsule, path: str) -> bool:
    bin_path = path.encode()
    return ddwaf_builder_remove_config(builder.builder, bin_path, len(bin_path))


ddwaf_builder_build_instance = ctypes.CFUNCTYPE(ddwaf_handle, ddwaf_builder)(
    ("ddwaf_builder_build_instance", ddwaf),
    ((1, "builder"),),
)


def py_ddwaf_builder_build_instance(builder: ddwaf_builder_capsule) -> ddwaf_handle_capsule:
    return ddwaf_handle_capsule(ddwaf_builder_build_instance(builder.builder), ddwaf_destroy)


ddwaf_builder_get_config_paths = ctypes.CFUNCTYPE(
    ctypes.c_uint32, ddwaf_builder, ddwaf_object_p, ctypes.c_char_p, ctypes.c_uint32
)(
    ("ddwaf_builder_get_config_paths", ddwaf),
    (
        (1, "builder"),
        (1, "paths"),
        (1, "filter"),
        (1, "filter_len"),
    ),
)


def py_ddwaf_builder_get_config_paths(builder: ddwaf_builder_capsule, filter_str: str) -> int:
    return ddwaf_builder_get_config_paths(builder.builder, None, filter_str.encode(), len(filter_str))


ddwaf_builder_destroy = ctypes.CFUNCTYPE(None, ddwaf_builder)(
    ("ddwaf_builder_destroy", ddwaf),
    ((1, "builder"),),
)


## ddwaf_object

ddwaf_object_invalid = ctypes.CFUNCTYPE(ddwaf_object_p, ddwaf_object_p)(
    ("ddwaf_object_invalid", ddwaf),
    ((3, "object"),),
)


ddwaf_object_string = ctypes.CFUNCTYPE(ddwaf_object_p, ddwaf_object_p, ctypes.c_char_p)(
    ("ddwaf_object_string", ddwaf),
    (
        (3, "object"),
        (1, "string"),
    ),
)

# object_string variants not used

ddwaf_object_string_from_unsigned = ctypes.CFUNCTYPE(ddwaf_object_p, ddwaf_object_p, ctypes.c_uint64)(
    ("ddwaf_object_string_from_unsigned", ddwaf),
    (
        (3, "object"),
        (1, "value"),
    ),
)

ddwaf_object_string_from_signed = ctypes.CFUNCTYPE(ddwaf_object_p, ddwaf_object_p, ctypes.c_int64)(
    ("ddwaf_object_string_from_signed", ddwaf),
    (
        (3, "object"),
        (1, "value"),
    ),
)

ddwaf_object_unsigned = ctypes.CFUNCTYPE(ddwaf_object_p, ddwaf_object_p, ctypes.c_uint64)(
    ("ddwaf_object_unsigned", ddwaf),
    (
        (3, "object"),
        (1, "value"),
    ),
)

ddwaf_object_signed = ctypes.CFUNCTYPE(ddwaf_object_p, ddwaf_object_p, ctypes.c_int64)(
    ("ddwaf_object_signed", ddwaf),
    (
        (3, "object"),
        (1, "value"),
    ),
)

# object_(un)signed_forced : not used ?

ddwaf_object_bool = ctypes.CFUNCTYPE(ddwaf_object_p, ddwaf_object_p, ctypes.c_bool)(
    ("ddwaf_object_bool", ddwaf),
    (
        (3, "object"),
        (1, "value"),
    ),
)


ddwaf_object_float = ctypes.CFUNCTYPE(ddwaf_object_p, ddwaf_object_p, ctypes.c_double)(
    ("ddwaf_object_float", ddwaf),
    (
        (3, "object"),
        (1, "value"),
    ),
)

ddwaf_object_null = ctypes.CFUNCTYPE(ddwaf_object_p, ddwaf_object_p)(
    ("ddwaf_object_null", ddwaf),
    ((3, "object"),),
)

ddwaf_object_array = ctypes.CFUNCTYPE(ddwaf_object_p, ddwaf_object_p)(
    ("ddwaf_object_array", ddwaf),
    ((3, "object"),),
)

ddwaf_object_map = ctypes.CFUNCTYPE(ddwaf_object_p, ddwaf_object_p)(
    ("ddwaf_object_map", ddwaf),
    ((3, "object"),),
)

ddwaf_object_array_add = ctypes.CFUNCTYPE(ctypes.c_bool, ddwaf_object_p, ddwaf_object_p)(
    ("ddwaf_object_array_add", ddwaf),
    (
        (1, "array"),
        (1, "object"),
    ),
)

ddwaf_object_map_add = ctypes.CFUNCTYPE(ctypes.c_bool, ddwaf_object_p, ctypes.c_char_p, ddwaf_object_p)(
    ("ddwaf_object_map_add", ddwaf),
    (
        (1, "map"),
        (1, "key"),
        (1, "object"),
    ),
)

# unused because accessible from python part
# ddwaf_object_type
# ddwaf_object_size
# ddwaf_object_length
# ddwaf_object_get_key
# ddwaf_object_get_string
# ddwaf_object_get_unsigned
# ddwaf_object_get_signed
# ddwaf_object_get_index
# ddwaf_object_get_bool https://github.com/DataDog/libddwaf/commit/7dc68dacd972ae2e2a3c03a69116909c98dbd9cb
# ddwaf_object_get_float


ddwaf_get_version = ctypes.CFUNCTYPE(ctypes.c_char_p)(
    ("ddwaf_get_version", ddwaf),
    (),
)


ddwaf_set_log_cb = ctypes.CFUNCTYPE(ctypes.c_bool, ddwaf_log_cb, ctypes.c_int)(
    ("ddwaf_set_log_cb", ddwaf),
    (
        (1, "cb"),
        (1, "min_level"),
    ),
)
