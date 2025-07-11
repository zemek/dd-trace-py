import base64
import dataclasses
import enum
import hashlib
import json
import os
import re
from typing import TYPE_CHECKING  # noqa:F401
from typing import Any
from typing import Callable
from typing import Dict
from typing import Iterable
from typing import List
from typing import Mapping
from typing import MutableMapping
from typing import Optional
from typing import Set
from typing import Tuple
import uuid

import ddtrace
from ddtrace.internal import agent
from ddtrace.internal import gitmetadata
from ddtrace.internal import runtime
from ddtrace.internal.hostname import get_hostname
from ddtrace.internal.logger import get_logger
from ddtrace.internal.packages import is_distribution_available
from ddtrace.internal.remoteconfig import ConfigMetadata
from ddtrace.internal.remoteconfig import PayloadType
from ddtrace.internal.remoteconfig._pubsub import PubSub
from ddtrace.internal.remoteconfig.constants import REMOTE_CONFIG_AGENT_ENDPOINT
from ddtrace.internal.service import ServiceStatus
from ddtrace.internal.utils.formats import parse_tags_str
from ddtrace.internal.utils.version import _pep440_to_semver
from ddtrace.settings._agent import config as agent_config
from ddtrace.settings._core import DDConfig


log = get_logger(__name__)

TARGET_FORMAT = re.compile(r"^(datadog/\d+|employee)/([^/]+)/([^/]+)/([^/]+)$")


REQUIRE_SKIP_SHUTDOWN = frozenset({"django-q"})


def derive_skip_shutdown(c: "RemoteConfigClientConfig") -> bool:
    return (
        c._skip_shutdown
        if c._skip_shutdown is not None
        else any(is_distribution_available(_) for _ in REQUIRE_SKIP_SHUTDOWN)
    )


class RemoteConfigClientConfig(DDConfig):
    __prefix__ = "_dd.remote_configuration"

    log_payloads = DDConfig.v(bool, "log_payloads", default=False)

    _skip_shutdown = DDConfig.v(Optional[bool], "skip_shutdown", default=None)
    skip_shutdown = DDConfig.d(bool, derive_skip_shutdown)


config = RemoteConfigClientConfig()


class RemoteConfigError(Exception):
    """
    An error occurred during the configuration update procedure.
    The error is reported to the agent.
    """


@dataclasses.dataclass
class Signature:
    keyid: str
    sig: str


@dataclasses.dataclass
class Key:
    keytype: str
    keyid_hash_algorithms: List[str]
    keyval: Mapping
    scheme: str


@dataclasses.dataclass
class Role:
    keyids: List[str]
    threshold: int


@dataclasses.dataclass
class Root:
    _type: str
    spec_version: str
    consistent_snapshot: bool
    expires: str
    keys: Mapping[str, Key]
    roles: Mapping[str, Role]
    version: int = 0

    def __post_init__(self):
        if self._type != "root":
            raise ValueError("Root: invalid root type")
        for k, v in self.keys.items():
            if isinstance(v, dict):
                self.keys[k] = Key(**v)
        for k, v in self.roles.items():
            if isinstance(v, dict):
                self.roles[k] = Role(**v)


@dataclasses.dataclass
class SignedRoot:
    signatures: List[Signature]
    signed: Root

    def __post_init__(self):
        for i in range(len(self.signatures)):
            if isinstance(self.signatures[i], dict):
                self.signatures[i] = Signature(**self.signatures[i])
        if isinstance(self.signed, dict):
            self.signed = Root(**self.signed)


@dataclasses.dataclass
class TargetDesc:
    length: int
    hashes: Mapping[str, str]
    custom: Mapping[str, Any]


@dataclasses.dataclass
class Targets:
    _type: str
    custom: Mapping[str, Any]
    expires: str
    spec_version: str
    targets: Mapping[str, TargetDesc]
    version: int = 0

    def __post_init__(self):
        if self._type != "targets":
            raise ValueError("Targets: invalid targets type")
        if self.spec_version not in ("1.0", "1.0.0"):
            raise ValueError("Targets: invalid spec version")
        for k, v in self.targets.items():
            if isinstance(v, dict):
                self.targets[k] = TargetDesc(**v)


@dataclasses.dataclass
class SignedTargets:
    signatures: List[Signature]
    signed: Targets
    version: int = 0

    def __post_init__(self):
        for i in range(len(self.signatures)):
            if isinstance(self.signatures[i], dict):
                self.signatures[i] = Signature(**self.signatures[i])
        if isinstance(self.signed, dict):
            self.signed = Targets(**self.signed)


@dataclasses.dataclass
class TargetFile:
    path: str
    raw: str


@dataclasses.dataclass
class AgentPayload:
    roots: Optional[List[SignedRoot]] = None
    targets: Optional[SignedTargets] = None
    target_files: List[TargetFile] = dataclasses.field(default_factory=list)
    client_configs: Set[str] = dataclasses.field(default_factory=set)

    def __post_init__(self):
        if self.roots is not None:
            for i in range(len(self.roots)):
                if isinstance(self.roots[i], str):
                    self.roots[i] = SignedRoot(**json.loads(base64.b64decode(self.roots[i])))
        if isinstance(self.targets, str):
            self.targets = SignedTargets(**json.loads(base64.b64decode(self.targets)))
        for i in range(len(self.target_files)):
            if isinstance(self.target_files[i], dict):
                self.target_files[i] = TargetFile(**self.target_files[i])


AppliedConfigType = Dict[str, ConfigMetadata]
TargetsType = Dict[str, ConfigMetadata]


class RemoteConfigClient:
    """
    The Remote Configuration client regularly checks for updates on the agent
    and dispatches configurations to registered products.
    """

    def __init__(self) -> None:
        tracer_version = _pep440_to_semver()

        self.id = str(uuid.uuid4())
        self.agent_url = agent_config.trace_agent_url

        self._headers = {"content-type": "application/json"}
        additional_header_str = os.environ.get("_DD_REMOTE_CONFIGURATION_ADDITIONAL_HEADERS")
        if additional_header_str is not None:
            self._headers.update(parse_tags_str(additional_header_str))

        tags = ddtrace.config.tags.copy()

        # Add git metadata tags, if available
        gitmetadata.add_tags(tags)

        if ddtrace.config.env:
            tags["env"] = ddtrace.config.env
        if ddtrace.config.version:
            tags["version"] = ddtrace.config.version
        tags["tracer_version"] = tracer_version
        tags["host_name"] = get_hostname()

        self._client_tracer = dict(
            runtime_id=runtime.get_runtime_id(),
            language="python",
            tracer_version=tracer_version,
            service=ddtrace.config.service,
            extra_services=list(ddtrace.config._get_extra_services()),
            env=ddtrace.config.env,
            app_version=ddtrace.config.version,
            tags=[":".join(_) for _ in tags.items()],
        )
        self.cached_target_files: List[AppliedConfigType] = []

        self._products: MutableMapping[str, PubSub] = dict()
        self._applied_configs: AppliedConfigType = dict()
        self._last_targets_version = 0
        self._last_error: Optional[str] = None
        self._backend_state: Optional[str] = None
        self._capabilities: int = 0

    def _encode_capabilities(self, capabilities: int) -> str:
        return base64.b64encode(capabilities.to_bytes((capabilities.bit_length() + 7) // 8, "big")).decode()

    def renew_id(self):
        # called after the process is forked to declare a new id
        self.id = str(uuid.uuid4())
        self._client_tracer["runtime_id"] = runtime.get_runtime_id()

    def register_product(self, product_name: str, pubsub_instance: Optional[PubSub] = None) -> None:
        if pubsub_instance is not None:
            self._products[product_name] = pubsub_instance
        else:
            self._products.pop(product_name, None)

    def add_capabilities(self, capabilities: Iterable[enum.IntFlag]) -> None:
        for capability in capabilities:
            self._capabilities |= capability

    def update_product_callback(self, product_name: str, callback: Callable) -> bool:
        pubsub_instance = self._products.get(product_name)
        if pubsub_instance:
            pubsub_instance._subscriber._callback = callback
            if not self.is_subscriber_running(pubsub_instance):
                pubsub_instance.start_subscriber()
            return True
        return False

    def start_products(self, products: Set[str]) -> None:
        for product_name in products:
            pubsub_instance = self._products.get(product_name)
            if pubsub_instance:
                pubsub_instance.restart_subscriber()

    def unregister_product(self, product_name: str) -> None:
        self._products.pop(product_name, None)

    def get_pubsubs(self):
        for pubsub in set(self._products.values()):
            yield pubsub

    def is_subscriber_running(self, pubsub_to_check: PubSub) -> bool:
        for pubsub in self.get_pubsubs():
            if pubsub_to_check._subscriber is pubsub._subscriber and pubsub._subscriber.status == ServiceStatus.RUNNING:
                return True
        return False

    def reset_products(self):
        self._products = dict()

    def _send_request(self, payload: str) -> Optional[Mapping[str, Any]]:
        conn = None
        try:
            log.debug(
                "[%s][P: %s] Requesting RC data from products: %s", os.getpid(), os.getppid(), str(self._products)
            )  # noqa: G200

            if config.log_payloads:
                log.debug("[%s][P: %s] RC request payload: %s", os.getpid(), os.getppid(), payload)  # noqa: G200

            conn = agent.get_connection(self.agent_url, timeout=agent_config.trace_agent_timeout_seconds)
            conn.request("POST", REMOTE_CONFIG_AGENT_ENDPOINT, payload, self._headers)
            resp = conn.getresponse()
            data_length = resp.headers.get("Content-Length")
            if data_length is not None and int(data_length) == 0:
                log.debug("[%s][P: %s] RC response payload empty", os.getpid(), os.getppid())
                return None
            data = resp.read()

            if config.log_payloads:
                log.debug(
                    "[%s][P: %s] RC response payload: %s", os.getpid(), os.getppid(), data.decode("utf-8")
                )  # noqa: G200
        except OSError as e:
            log.debug("Unexpected connection error in remote config client request: %s", str(e))  # noqa: G200
            return None
        finally:
            if conn is not None:
                conn.close()

        if resp.status == 404:
            # Remote configuration is not enabled or unsupported by the agent
            return None

        if resp.status < 200 or resp.status >= 300:
            log.debug("Unexpected error: HTTP error status %s, reason %s", resp.status, resp.reason)
            return None

        return json.loads(data)

    @staticmethod
    def _extract_target_file(payload: AgentPayload, target: str, config: ConfigMetadata) -> Optional[Dict[str, Any]]:
        candidates = [item.raw for item in payload.target_files if item.path == target]
        if len(candidates) != 1 or candidates[0] is None:
            log.debug(
                "invalid target_files for %r. target files: %s", target, [item.path for item in payload.target_files]
            )
            return None

        try:
            raw = base64.b64decode(candidates[0])
        except Exception:
            raise RemoteConfigError("invalid base64 target_files for {!r}".format(target))

        computed_hash = hashlib.sha256(raw).hexdigest()
        if computed_hash != config.sha256_hash:
            raise RemoteConfigError(
                "mismatch between target {!r} hashes {!r} != {!r}".format(target, computed_hash, config.sha256_hash)
            )

        try:
            return json.loads(raw)
        except Exception:
            raise RemoteConfigError("invalid JSON content for target {!r}".format(target))

    def _build_payload(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        self._client_tracer["extra_services"] = list(ddtrace.config._get_extra_services())
        return dict(
            client=dict(
                id=self.id,
                products=list(self._products.keys()),
                is_tracer=True,
                client_tracer=self._client_tracer,
                state=state,
                capabilities=self._encode_capabilities(self._capabilities),
            ),
            cached_target_files=self.cached_target_files,
        )

    def _build_state(self) -> Mapping[str, Any]:
        has_error = self._last_error is not None
        state = dict(
            root_version=1,
            targets_version=self._last_targets_version,
            config_states=[
                (
                    dict(
                        id=config.id,
                        version=config.tuf_version,
                        product=config.product_name,
                        apply_state=config.apply_state,
                        apply_error=config.apply_error,
                    )
                    if config.apply_error
                    else dict(
                        id=config.id,
                        version=config.tuf_version,
                        product=config.product_name,
                        apply_state=config.apply_state,
                    )
                )
                for config in self._applied_configs.values()
            ],
            has_error=has_error,
        )
        if self._backend_state is not None:
            state["backend_client_state"] = self._backend_state
        if has_error:
            state["error"] = self._last_error
        return state

    @staticmethod
    def _apply_callback(
        list_callbacks: List[PubSub],
        callback: PubSub,
        config_content: PayloadType,
        target: str,
        config_metadata: ConfigMetadata,
    ) -> None:
        callback.append(config_content, target, config_metadata)
        if callback not in list_callbacks and not any(filter(lambda x: x is callback, list_callbacks)):
            list_callbacks.append(callback)

    def _remove_previously_applied_configurations(
        self,
        list_callbacks: List[PubSub],
        applied_configs: AppliedConfigType,
        client_configs: TargetsType,
        targets: TargetsType,
    ) -> None:
        witness = object()
        for target, config in self._applied_configs.items():
            if client_configs.get(target, witness) == config:
                # The configuration has not changed.
                applied_configs[target] = config
                continue
            elif target not in targets:
                callback_action = None
            else:
                continue

            callback = self._products.get(config.product_name)
            if callback:
                try:
                    log.debug("[%s][P: %s] Disabling configuration: %s", os.getpid(), os.getppid(), target)
                    self._apply_callback(list_callbacks, callback, callback_action, target, config)
                except Exception:
                    log.debug("error while removing product %s config %r", config.product_name, config)
                    continue

    def _load_new_configurations(
        self,
        list_callbacks: List[PubSub],
        applied_configs: AppliedConfigType,
        client_configs: TargetsType,
        payload: AgentPayload,
    ) -> None:
        for target, config in client_configs.items():
            callback = self._products.get(config.product_name)
            if callback:
                applied_config = self._applied_configs.get(target)
                if applied_config == config:
                    continue
                config_content = self._extract_target_file(payload, target, config)
                if config_content is None:
                    continue

                try:
                    log.debug("[%s][P: %s] Load new configuration: %s. content", os.getpid(), os.getppid(), target)
                    self._apply_callback(list_callbacks, callback, config_content, target, config)
                except Exception:
                    error_message = "Failed to apply configuration %s for product %r" % (config, config.product_name)
                    log.debug(error_message, exc_info=True)
                    config.apply_state = 3  # Error state
                    config.apply_error = error_message
                    applied_configs[target] = config
                    continue
                else:
                    config.apply_state = 2  # Acknowledged (applied)
                    applied_configs[target] = config

    def _add_apply_config_to_cache(self):
        if self._applied_configs:
            cached_data = []
            for target, config in self._applied_configs.items():
                cached_data.append(
                    {
                        "path": target,
                        "length": config.length,
                        "hashes": [{"algorithm": "sha256", "hash": config.sha256_hash}],
                    }
                )
            self.cached_target_files = cached_data
        else:
            self.cached_target_files = []

    def _validate_config_exists_in_target_paths(
        self, payload_client_configs: Set[str], payload_target_files: List[TargetFile]
    ) -> None:
        paths = {_.path for _ in payload_target_files}
        paths = paths.union({_["path"] for _ in self.cached_target_files})

        # !(payload.client_configs is a subset of paths or payload.client_configs is equal to paths)
        if not set(payload_client_configs) <= paths:
            raise RemoteConfigError("Not all client configurations have target files")

    @staticmethod
    def _validate_signed_target_files(
        payload_target_files: List[TargetFile], payload_targets_signed: Targets, client_configs: TargetsType
    ) -> None:
        for target in payload_target_files:
            if (payload_targets_signed.targets and not payload_targets_signed.targets.get(target.path)) and (
                client_configs and not client_configs.get(target.path)
            ):
                raise RemoteConfigError(
                    "target file %s not exists in client_config and signed targets" % (target.path,)
                )

    def _publish_configuration(self, list_callbacks: List[PubSub]) -> None:
        for callback_to_dispach in list_callbacks:
            callback_to_dispach.publish()

    def _process_targets(self, payload: AgentPayload) -> Tuple[Optional[int], Optional[str], Optional[TargetsType]]:
        if payload.targets is None:
            # no targets received
            return None, None, None
        signed = payload.targets.signed
        targets = dict()
        for target, metadata in signed.targets.items():
            m = TARGET_FORMAT.match(target)
            if m is None:
                raise RemoteConfigError("unexpected target format {!r}".format(target))
            _, product_name, config_id, _ = m.groups()
            targets[target] = ConfigMetadata(
                id=config_id,
                product_name=product_name,
                sha256_hash=metadata.hashes.get("sha256"),
                length=metadata.length,
                tuf_version=metadata.custom.get("v"),
            )
        backend_state = signed.custom.get("opaque_backend_state")
        return signed.version, backend_state, targets

    def _process_response(self, data: Mapping[str, Any]) -> None:
        try:
            payload = AgentPayload(**data)
        except Exception as e:
            log.debug("invalid agent payload received: %r", data, exc_info=True)
            msg = f"invalid agent payload received: {e}"
            raise RemoteConfigError(msg)

        self._validate_config_exists_in_target_paths(payload.client_configs, payload.target_files)

        # 1. Deserialize targets
        if payload.targets is None:
            return
        last_targets_version, backend_state, targets = self._process_targets(payload)
        if last_targets_version is None or targets is None:
            return

        client_configs = {k: v for k, v in targets.items() if k in payload.client_configs}
        log.debug(
            "[%s][P: %s] Retrieved client configs last version %s: %s",
            os.getpid(),
            os.getppid(),
            last_targets_version,
            client_configs,
        )

        self._validate_signed_target_files(payload.target_files, payload.targets.signed, client_configs)

        # 2. Remove previously applied configurations
        applied_configs: AppliedConfigType = dict()
        list_callbacks: List[PubSub] = []
        self._remove_previously_applied_configurations(list_callbacks, applied_configs, client_configs, targets)

        # 3. Load new configurations
        self._load_new_configurations(list_callbacks, applied_configs, client_configs, payload)

        self._publish_configuration(list_callbacks)

        self._last_targets_version = last_targets_version
        self._applied_configs = applied_configs
        self._backend_state = backend_state

        self._add_apply_config_to_cache()

    def request(self) -> bool:
        try:
            state = self._build_state()
            payload = json.dumps(self._build_payload(state))
            response = self._send_request(payload)
            if response is None:
                return False
            self._process_response(response)
            self._last_error = None
            return True

        except RemoteConfigError as e:
            self._last_error = str(e)
            log.debug("remote configuration client reported an error", exc_info=True)
        except ValueError:
            log.debug("Unexpected response data", exc_info=True)
        except Exception:
            log.debug("Unexpected error", exc_info=True)

        return False
