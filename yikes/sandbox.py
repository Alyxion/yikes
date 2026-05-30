from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


CONTAINER_PREFIX = "yksb"
VOLUME_PREFIX = "ykvol"
DEFAULT_IMAGE = "yikes-sandbox:latest"
SANDBOX_IMAGE_VERSION = "2026-05-17-py314-bwrap"
DEFAULT_SANDBOX_STORE = Path.home() / ".yikes" / "sandboxes"
DEFAULT_DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "yikes-sandbox.Dockerfile"
DEFAULT_SERVER_COMMAND = (
    "yikes",
    "server",
    "--host",
    "0.0.0.0",
    "--port",
    "8989",
    "--token-store",
    "/workspace/home/.yikes/tokens.json",
    "--event-store",
    "/workspace/home/.yikes/events",
    "--bootstrap-token-env",
    "YIKES_SERVER_TOKEN",
)


@dataclass(frozen=True)
class SecurityProfile:
    enabled: bool = True
    cap_drop_all: bool = True
    cap_add: tuple[str, ...] = ("NET_BIND_SERVICE",)
    no_new_privileges: bool = True
    read_only_root: bool = True
    pids_limit: int = 256
    tmpfs_mounts: tuple[tuple[str, str], ...] = (
        ("/tmp", "size=100m,noexec,nosuid"),
        ("/run", "size=10m,noexec,nosuid"),
        ("/workspace/home", "size=200m,nosuid"),
        ("/workspace/npm-cache", "size=200m,nosuid"),
    )
    block_metadata: bool = True


@dataclass(frozen=True)
class SandboxConfig:
    image: str = DEFAULT_IMAGE
    command: tuple[str, ...] = DEFAULT_SERVER_COMMAND
    mounts: tuple[tuple[str, str, str], ...] = ()
    ports: tuple[tuple[str, str], ...] = ()
    memory: str | None = None
    cpus: float | None = None
    disk: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    secret_env: dict[str, str] = field(default_factory=dict, repr=False)
    timeout_minutes: int = 60
    security: SecurityProfile = field(default_factory=SecurityProfile)


@dataclass
class SandboxMeta:
    id: str
    container_name: str
    volume_name: str
    config: SandboxConfig
    created_at: str
    last_active: str
    user_data: dict[str, str] = field(default_factory=dict)


class SandboxSession:
    """Reusable isolated Docker execution session.

    The container can be stopped and resumed. Durable workspace state lives in
    the named Docker volume mounted at `/workspace`.
    """

    def __init__(self, meta: SandboxMeta, store_dir: Path) -> None:
        self.meta = meta
        self._store_dir = store_dir
        self._meta_path = store_dir / f"{meta.id}.json"

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def container_name(self) -> str:
        return self.meta.container_name

    @property
    def volume_name(self) -> str:
        return self.meta.volume_name

    def is_running(self) -> bool:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def container_exists(self) -> bool:
        result = subprocess.run(
            ["docker", "inspect", self.container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def start(self) -> None:
        if self.is_running():
            self._touch()
            return
        if self.container_exists():
            _run_docker(["docker", "start", self.container_name])
        else:
            ensure_image(self.meta.config.image)
            _run_docker(self._build_run_cmd())
        self._touch()

    def stop(self) -> None:
        if self.is_running():
            subprocess.run(
                ["docker", "stop", "-t", "5", self.container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        self._touch()

    def destroy(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "volume", "rm", "-f", self.volume_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._meta_path.unlink(missing_ok=True)

    def exec(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self._ensure_running()
        result = subprocess.run([*self._exec_prefix(), *cmd], **kwargs)
        self._touch()
        return result

    def exec_streaming(self, cmd: list[str]) -> subprocess.Popen[bytes]:
        self._ensure_running()
        proc = subprocess.Popen(
            [*self._exec_prefix(), *cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        self._touch()
        return proc

    def write_file(self, path: str, content: str | bytes) -> None:
        self._ensure_running()
        data = content.encode() if isinstance(content, str) else content
        target = shlex.quote(path)
        parent = shlex.quote(str(Path(path).parent))
        result = subprocess.run(
            ["docker", "exec", "-i", self.container_name, "sh", "-c", f"mkdir -p {parent} && cat > {target}"],
            input=data,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or b"").decode(errors="replace").strip()
            raise RuntimeError(f"Failed to write {path} in Docker container {self.container_name}: {detail}".strip())
        self._touch()

    def _ensure_running(self) -> None:
        if not self.is_running():
            self.start()

    def _exec_prefix(self) -> list[str]:
        prefix = ["docker", "exec"]
        for key, value in self.meta.config.secret_env.items():
            prefix.extend(["-e", f"{key}={value}"])
        prefix.extend(["-i", self.container_name])
        return prefix

    def _build_run_cmd(self) -> list[str]:
        cfg = self.meta.config
        sec = cfg.security
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self.container_name,
            "-v",
            f"{self.volume_name}:/workspace",
            "--add-host=host.docker.internal:host-gateway",
        ]
        for host_path, container_path, mode in cfg.mounts:
            cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])
        if sec.enabled and sec.cap_drop_all:
            cmd.append("--cap-drop=ALL")
            for cap in sec.cap_add:
                cmd.append(f"--cap-add={cap}")
        if sec.enabled and sec.no_new_privileges:
            cmd.append("--security-opt=no-new-privileges")
        if sec.enabled and sec.block_metadata:
            cmd.extend([
                "--add-host=metadata.google.internal:127.0.0.1",
                "--add-host=169.254.169.254:127.0.0.1",
            ])
        if sec.enabled and sec.pids_limit:
            cmd.extend(["--pids-limit", str(sec.pids_limit)])
        if sec.enabled and sec.read_only_root:
            cmd.append("--read-only")
            for mount_path, opts in sec.tmpfs_mounts:
                cmd.extend(["--tmpfs", f"{mount_path}:{opts}"])
        for key, value in cfg.env.items():
            cmd.extend(["-e", f"{key}={value}"])
        if cfg.memory:
            cmd.extend(["--memory", cfg.memory])
        if cfg.cpus is not None:
            cmd.extend(["--cpus", str(cfg.cpus)])
        if cfg.disk:
            cmd.extend(["--storage-opt", f"size={cfg.disk}"])
        for host_port, container_port in cfg.ports:
            cmd.extend(["-p", f"127.0.0.1:{host_port}:{container_port}"])
        cmd.append(cfg.image)
        cmd.extend(cfg.command)
        return cmd

    def _touch(self) -> None:
        self.meta.last_active = _now()
        self._save()

    def _save(self) -> None:
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._meta_path.write_text(json.dumps(_meta_to_json(self.meta), indent=2))


class SandboxManager:
    def __init__(self, store_dir: Path | None = None) -> None:
        configured = store_dir or Path(os.environ.get("YIKES_SANDBOX_STORE", str(DEFAULT_SANDBOX_STORE)))
        self.store_dir = configured.expanduser()
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def create(self, config: SandboxConfig | None = None, *, user_data: dict[str, str] | None = None) -> SandboxSession:
        sid = uuid4().hex[:12]
        now = _now()
        meta = SandboxMeta(
            id=sid,
            container_name=f"{CONTAINER_PREFIX}-{sid}",
            volume_name=f"{VOLUME_PREFIX}-{sid}",
            config=config or SandboxConfig(),
            created_at=now,
            last_active=now,
            user_data=user_data or {},
        )
        session = SandboxSession(meta, self.store_dir)
        session._save()
        return session

    def get(self, session_id: str) -> SandboxSession | None:
        path = self.store_dir / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            return SandboxSession(_meta_from_json(json.loads(path.read_text())), self.store_dir)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def list_sessions(self) -> list[SandboxSession]:
        sessions: list[SandboxSession] = []
        for path in self.store_dir.glob("*.json"):
            session = self.get(path.stem)
            if session is not None:
                sessions.append(session)
        sessions.sort(key=lambda item: item.meta.last_active, reverse=True)
        return sessions

    def find_running(self, *, image: str | None = None, label: str | None = None) -> SandboxSession | None:
        for session in self.list_sessions():
            if not session.is_running():
                continue
            if image and session.meta.config.image != image:
                continue
            if label and session.meta.user_data.get("label") != label:
                continue
            return session
        return None

    def destroy(self, session_id: str) -> bool:
        session = self.get(session_id)
        if session is None:
            return False
        session.destroy()
        return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_image(image: str) -> None:
    inspect = subprocess.run(
        ["docker", "image", "inspect", "-f", '{{ index .Config.Labels "yikes.sandbox.version" }}', image],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode == 0 and (image != DEFAULT_IMAGE or inspect.stdout.strip() == SANDBOX_IMAGE_VERSION):
        return
    if image != DEFAULT_IMAGE:
        raise RuntimeError(
            f"Docker image {image!r} is not available. Build it first or set YIKES_DOCKER_IMAGE to an existing image."
        )
    if not DEFAULT_DOCKERFILE.exists():
        raise RuntimeError(f"Default Dockerfile is missing: {DEFAULT_DOCKERFILE}")
    _run_docker(
        [
            "docker",
            "build",
            "-f",
            str(DEFAULT_DOCKERFILE),
            "-t",
            DEFAULT_IMAGE,
            str(DEFAULT_DOCKERFILE.parent.parent),
        ],
        action=f"build default Docker image {DEFAULT_IMAGE}",
        timeout=None,
    )


def _run_docker(cmd: list[str], *, action: str | None = None, timeout: int | None = 300) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        label = action or "run Docker command"
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Failed to {label}: {shlex.join(cmd)}\n{detail}".strip())
    return result


def _meta_to_json(meta: SandboxMeta) -> dict[str, object]:
    data = asdict(meta)
    data["config"] = _config_to_json(meta.config)
    return data


def _config_to_json(config: SandboxConfig) -> dict[str, object]:
    data = asdict(config)
    data["command"] = list(config.command)
    data["mounts"] = [list(item) for item in config.mounts]
    data["ports"] = [list(item) for item in config.ports]
    data["security"]["cap_add"] = list(config.security.cap_add)
    data["security"]["tmpfs_mounts"] = [list(item) for item in config.security.tmpfs_mounts]
    return data


def _meta_from_json(data: dict[str, object]) -> SandboxMeta:
    config_data = data.get("config")
    if not isinstance(config_data, dict):
        config_data = {}
    return SandboxMeta(
        id=str(data["id"]),
        container_name=str(data["container_name"]),
        volume_name=str(data["volume_name"]),
        config=_config_from_json(config_data),
        created_at=str(data["created_at"]),
        last_active=str(data["last_active"]),
        user_data={str(key): str(value) for key, value in (data.get("user_data") or {}).items()}
        if isinstance(data.get("user_data"), dict)
        else {},
    )


def _config_from_json(data: dict[str, object]) -> SandboxConfig:
    security_data = data.get("security")
    if not isinstance(security_data, dict):
        security_data = {}
    return SandboxConfig(
        image=str(data.get("image", DEFAULT_IMAGE)),
        command=tuple(str(item) for item in data.get("command", ("sleep", "infinity")))
        if isinstance(data.get("command"), list)
        else ("sleep", "infinity"),
        mounts=tuple(
            (str(item[0]), str(item[1]), str(item[2]))
            for item in data.get("mounts", [])
            if isinstance(item, (list, tuple)) and len(item) == 3
        ),
        ports=tuple(
            (str(item[0]), str(item[1]))
            for item in data.get("ports", [])
            if isinstance(item, (list, tuple)) and len(item) == 2
        ),
        memory=_optional_str(data.get("memory")),
        cpus=float(data["cpus"]) if data.get("cpus") is not None else None,
        disk=_optional_str(data.get("disk")),
        env={str(key): str(value) for key, value in (data.get("env") or {}).items()}
        if isinstance(data.get("env"), dict)
        else {},
        secret_env={str(key): str(value) for key, value in (data.get("secret_env") or {}).items()}
        if isinstance(data.get("secret_env"), dict)
        else {},
        timeout_minutes=int(data.get("timeout_minutes", 60)),
        security=SecurityProfile(
            enabled=bool(security_data.get("enabled", True)),
            cap_drop_all=bool(security_data.get("cap_drop_all", True)),
            cap_add=tuple(str(cap) for cap in security_data.get("cap_add", ("NET_BIND_SERVICE",))),
            no_new_privileges=bool(security_data.get("no_new_privileges", True)),
            read_only_root=bool(security_data.get("read_only_root", True)),
            pids_limit=int(security_data.get("pids_limit", 256)),
            tmpfs_mounts=tuple(
                (str(item[0]), str(item[1]))
                for item in security_data.get("tmpfs_mounts", (("/tmp", "size=100m,noexec,nosuid"),))
                if isinstance(item, (list, tuple)) and len(item) == 2
            ),
            block_metadata=bool(security_data.get("block_metadata", True)),
        ),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
