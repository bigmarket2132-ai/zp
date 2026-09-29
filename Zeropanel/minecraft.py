import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


MCJARS_TYPES = frozenset({
    "VANILLA", "PAPER", "PUFFERFISH", "SPIGOT", "FOLIA", "PURPUR", "WATERFALL",
    "VELOCITY", "FABRIC", "BUNGEECORD", "QUILT", "FORGE", "NEOFORGE", "MOHIST",
    "ARCLIGHT", "SPONGE", "LEAVES", "CANVAS", "ASPAPER", "LEGACY_FABRIC",
    "LOOHP_LIMBO", "NANOLIMBO", "DIVINEMC", "MAGMA", "LEAF", "VELOCITY_CTD",
    "YOUER", "PLUTO",
})
MAX_EDITABLE_FILE_BYTES = 1_000_000
MAX_SERVER_FILE_TRANSFER_BYTES = 64 * 1024 * 1024


class MinecraftError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


def _version_sort_key(value):
    return tuple((0, int(part)) if part.isdigit() else (1, part.lower())
                 for part in re.findall(r"\d+|[A-Za-z]+", value))


def get_versions(server_type):
    server_type = str(server_type or "").upper()
    if server_type not in MCJARS_TYPES:
        raise MinecraftError("unsupported_minecraft_type")
    request = urllib.request.Request(
        f"https://mcjars.app/api/v2/builds/{server_type}",
        headers={"Accept": "application/json", "User-Agent": "ZeroPanel/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MinecraftError("minecraft_version_catalog_unavailable", 502) from exc
    versions = payload.get("builds") if isinstance(payload, dict) else None
    if not isinstance(versions, dict):
        raise MinecraftError("invalid_minecraft_version_catalog", 502)
    return sorted((str(version) for version in versions), key=_version_sort_key, reverse=True)


def _settings(server):
    try:
        value = json.loads(server["settings_json"] or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _container_name(server):
    return server.get("container_name") or f"zeropanel-minecraft-{int(server['id'])}"


def _docker(args, timeout=120, allow_failure=False):
    try:
        result = subprocess.run(
            ["docker", *args], check=False, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise MinecraftError("docker_unavailable", 503) from exc
    except subprocess.TimeoutExpired as exc:
        raise MinecraftError("minecraft_runtime_timeout", 504) from exc
    if result.returncode and not allow_failure:
        detail = (result.stderr or result.stdout or "").strip()
        raise MinecraftError(f"minecraft_runtime_failed: {detail[:300]}", 503)
    return result


def _container_exists(name):
    return _docker(["inspect", name], timeout=10, allow_failure=True).returncode == 0


def _allocate_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return int(sock.getsockname()[1])


def _environment(settings):
    env = {}
    allowed = {
        "difficulty": ("DIFFICULTY", {"peaceful", "easy", "normal", "hard"}),
        "max_players": ("MAX_PLAYERS", None),
        "motd": ("MOTD", None),
        "gamemode": ("MODE", {"survival", "creative", "adventure", "spectator"}),
        "pvp": ("PVP", None),
        "view_distance": ("VIEW_DISTANCE", None),
        "simulation_distance": ("SIMULATION_DISTANCE", None),
        "online_mode": ("ONLINE_MODE", None),
        "white_list": ("WHITE_LIST", None),
        "allow_flight": ("ALLOW_FLIGHT", None),
        "hardcore": ("HARDCORE", None),
        "enable_command_block": ("ENABLE_COMMAND_BLOCK", None),
        "spawn_protection": ("SPAWN_PROTECTION", None),
    }
    for key, (env_key, values) in allowed.items():
        if key not in settings:
            continue
        value = settings[key]
        if values is not None and str(value).lower() not in values:
            continue
        if key == "max_players":
            value = max(1, min(500, int(value)))
        elif key in {"view_distance", "simulation_distance"}:
            value = max(4, min(32, int(value)))
        elif key in {"pvp", "online_mode", "white_list", "allow_flight", "hardcore", "enable_command_block"}:
            value = "true" if bool(value) else "false"
        elif key == "spawn_protection":
            value = max(0, min(1000, int(value)))
        else:
            value = str(value).replace("\r", " ").replace("\n", " ")[:80]
        env[env_key] = str(value)
    return env


def _run_container(server, port, settings=None):
    root = Path(server["root_path"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    ram_mb = max(512, int(server["ram_mb"]))
    args = [
        "run", "-d", "--name", _container_name(server), "--restart", "unless-stopped",
        "--memory", f"{ram_mb}m", "--memory-swap", f"{ram_mb}m",
        "-p", f"{port}:25565", "-e", "EULA=TRUE", "-e", f"TYPE={server['server_type']}",
        "-e", f"VERSION={server['version']}", "-e", f"MAX_MEMORY={ram_mb}M",
        "-v", f"{root}:/data",
    ]
    for key, value in _environment(settings if settings is not None else _settings(server)).items():
        args.extend(["-e", f"{key}={value}"])
    args.append("itzg/minecraft-server:latest")
    _docker(args, timeout=180)
    return port


def server_action(server, action):
    name = _container_name(server)
    if action == "stop":
        if _container_exists(name):
            _docker(["stop", name], timeout=60)
        return {"status": "stopped", "port": server["port"]}
    if action not in {"start", "restart"}:
        raise MinecraftError("invalid_minecraft_action")
    port = int(server["port"] or _allocate_port())
    if _container_exists(name):
        if action == "restart":
            _docker(["restart", name], timeout=60)
        else:
            _docker(["start", name], timeout=60)
    else:
        _run_container(server, port)
    return {"status": "running", "port": port, "container_name": name}


def _remove_container(server):
    name = _container_name(server)
    if _container_exists(name):
        _docker(["rm", "-f", name], timeout=60)


def update_server_version(server, version):
    version = str(version or "").strip()
    if len(version) > 48 or not re.fullmatch(r"[A-Za-z0-9._+-]+", version):
        raise MinecraftError("invalid_minecraft_version")
    was_running = server["status"] == "running"
    if was_running:
        _remove_container(server)
    updated = dict(server)
    updated["version"] = version
    port = int(server["port"] or _allocate_port())
    if was_running:
        _run_container(updated, port)
    return {"version": version, "status": "running" if was_running else "stopped", "port": port if was_running else server["port"]}


def update_server_settings(server, settings):
    current = _settings(server)
    allowed = {
        "difficulty", "max_players", "motd", "gamemode", "pvp", "view_distance", "simulation_distance",
        "online_mode", "white_list", "allow_flight", "hardcore", "enable_command_block", "spawn_protection",
    }
    for key, value in settings.items():
        if key in allowed:
            current[key] = value
    for key, values in (("difficulty", {"peaceful", "easy", "normal", "hard"}),
                        ("gamemode", {"survival", "creative", "adventure", "spectator"})):
        if key in current and str(current[key]).lower() not in values:
            raise MinecraftError(f"invalid_{key}")
    try:
        current["max_players"] = max(1, min(500, int(current.get("max_players", 20))))
        current["view_distance"] = max(4, min(32, int(current.get("view_distance", 10))))
        current["simulation_distance"] = max(4, min(32, int(current.get("simulation_distance", 10))))
        current["spawn_protection"] = max(0, min(1000, int(current.get("spawn_protection", 16))))
    except (TypeError, ValueError) as exc:
        raise MinecraftError("invalid_minecraft_numeric_setting") from exc
    for key in ("pvp", "online_mode", "white_list", "allow_flight", "hardcore", "enable_command_block"):
        if key in current and not isinstance(current[key], bool):
            if str(current[key]).lower() not in {"true", "false", "1", "0"}:
                raise MinecraftError(f"invalid_{key}")
            current[key] = str(current[key]).lower() in {"true", "1"}
    current["motd"] = str(current.get("motd", "A Minecraft Server"))[:80]
    was_running = server["status"] == "running"
    if was_running:
        _remove_container(server)
        updated = dict(server)
        updated["settings_json"] = json.dumps(current)
        port = int(server["port"] or _allocate_port())
        _run_container(updated, port, current)
    else:
        port = server["port"]
    return {"settings": current, "status": "running" if was_running else "stopped", "port": port}


def console_output(server):
    if server.get("status") != "running":
        return ""
    name = _container_name(server)
    if not _container_exists(name):
        return ""
    return _docker(["logs", "--tail", "200", name], timeout=15).stdout[-30_000:]


def _docker_bytes(value):
    match = re.fullmatch(r"\s*([\d.]+)\s*([kMGTPE]?i?B)\s*", str(value or ""), re.IGNORECASE)
    if not match:
        return 0
    unit = match.group(2).lower()
    powers = {"b": 0, "kb": 1, "kib": 1, "mb": 2, "mib": 2, "gb": 3, "gib": 3,
              "tb": 4, "tib": 4, "pb": 5, "pib": 5, "eb": 6, "eib": 6}
    return int(float(match.group(1)) * (1024 ** powers.get(unit, 0)))


def _docker_pair(value):
    left, _, right = str(value or "").partition("/")
    return _docker_bytes(left), _docker_bytes(right)


def _directory_size(root):
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _backup_directory(server):
    root = Path(server["root_path"]).resolve()
    return root.parent / f".zeropanel-backups-{int(server['id'])}"


def list_backups(server):
    directory = _backup_directory(server)
    if not directory.exists():
        return []
    backups = []
    for archive in sorted(directory.glob("*.zip"), key=lambda item: item.name, reverse=True):
        if archive.is_symlink() or not archive.is_file():
            continue
        stat = archive.stat()
        backups.append({"id": archive.stem, "name": archive.stem, "size": stat.st_size,
                        "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    return backups


def backup_path(server, backup_id):
    backup_id = str(backup_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", backup_id):
        raise MinecraftError("invalid_minecraft_backup_id")
    path = _backup_directory(server) / f"{backup_id}.zip"
    if path.is_symlink() or not path.is_file():
        raise MinecraftError("minecraft_backup_not_found", 404)
    return path


def create_backup(server):
    root = Path(server["root_path"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = _backup_directory(server)
    directory.mkdir(parents=True, exist_ok=True)
    backup_id = datetime.now(timezone.utc).strftime("backup-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    temporary = directory / f".{backup_id}.tmp"
    destination = directory / f"{backup_id}.zip"
    used_before = _directory_size(root) + _directory_size(directory)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            for item in root.rglob("*"):
                if item.is_symlink() or not item.is_file():
                    continue
                archive.write(item, item.relative_to(root).as_posix())
        archive_size = temporary.stat().st_size
        disk_limit = max(0, int(server["disk_mb"])) * 1024 * 1024
        if disk_limit and used_before + archive_size > disk_limit:
            raise MinecraftError("minecraft_disk_limit_reached", 403)
        temporary.replace(destination)
    except MinecraftError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MinecraftError("minecraft_backup_failed", 500) from exc
    return {"id": backup_id, "name": backup_id, "size": archive_size,
            "created_at": datetime.fromtimestamp(destination.stat().st_mtime, timezone.utc).isoformat()}


def delete_backup(server, backup_id):
    path = backup_path(server, backup_id)
    path.unlink()
    return {"deleted": True}


def restore_backup(server, backup_id):
    if server.get("status") == "running":
        raise MinecraftError("minecraft_server_must_be_stopped", 409)
    archive_path = backup_path(server, backup_id)
    root = Path(server["root_path"]).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    disk_limit = max(0, int(server["disk_mb"])) * 1024 * 1024
    backup_bytes = _directory_size(_backup_directory(server))
    staging = Path(tempfile.mkdtemp(prefix=f".mc-restore-{server['id']}-", dir=root.parent))
    old_root = root.with_name(f".{root.name}.restore-{uuid.uuid4().hex[:8]}")
    expanded_size = 0
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = archive.infolist()
            for member in members:
                relative = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts) or (mode & 0o170000) == 0o120000:
                    raise MinecraftError("invalid_minecraft_backup_archive")
                expanded_size += member.file_size
            if disk_limit and expanded_size + backup_bytes > disk_limit:
                raise MinecraftError("minecraft_disk_limit_reached", 403)
            for member in members:
                relative = PurePosixPath(member.filename)
                destination = staging.joinpath(*relative.parts)
                resolved = destination.resolve()
                if staging.resolve() not in resolved.parents and resolved != staging.resolve():
                    raise MinecraftError("invalid_minecraft_backup_archive")
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        if root.exists():
            root.replace(old_root)
        try:
            staging.replace(root)
        except OSError:
            if old_root.exists() and not root.exists():
                old_root.replace(root)
            raise
        if old_root.exists():
            shutil.rmtree(old_root)
    except MinecraftError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise MinecraftError("minecraft_backup_restore_failed", 500) from exc
    return {"restored": True, "files_restored": len(members), "size": expanded_size}


def runtime_metrics(server):
    root = Path(server["root_path"]).resolve()
    disk_used_bytes = _directory_size(root) + _directory_size(_backup_directory(server))
    ram_limit_bytes = max(0, int(server["ram_mb"])) * 1024 * 1024
    disk_limit_bytes = max(0, int(server["disk_mb"])) * 1024 * 1024
    result = {
        "running": server.get("status") == "running",
        "cpu_percent": None,
        "ram_used_bytes": 0,
        "ram_limit_bytes": ram_limit_bytes,
        "disk_used_bytes": disk_used_bytes,
        "disk_limit_bytes": disk_limit_bytes,
        "network_rx_bytes": 0,
        "network_tx_bytes": 0,
        "uptime_seconds": None,
    }
    if not result["running"]:
        return result

    name = _container_name(server)
    if not _container_exists(name):
        result["running"] = False
        return result
    stats = _docker(["stats", "--no-stream", "--format", "{{json .}}", name], timeout=15)
    try:
        row = json.loads(stats.stdout.splitlines()[0])
        result["cpu_percent"] = float(str(row.get("CPUPerc", "0%")).rstrip("%"))
        result["ram_used_bytes"] = _docker_pair(row.get("MemUsage"))[0]
        result["network_rx_bytes"], result["network_tx_bytes"] = _docker_pair(row.get("NetIO"))
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MinecraftError("invalid_minecraft_runtime_metrics", 502) from exc

    started = _docker(["inspect", "--format", "{{.State.StartedAt}}", name], timeout=10).stdout.strip()
    try:
        started_at = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        result["uptime_seconds"] = max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))
    except (TypeError, ValueError):
        pass
    return result


def send_command(server, command):
    command = str(command or "").strip()
    if not command or len(command) > 256 or any(char in command for char in "\r\n\x00"):
        raise MinecraftError("invalid_console_command")
    _docker(["exec", _container_name(server), "rcon-cli", command], timeout=15)


def _safe_path(server, relative_path):
    value = str(relative_path or "").replace("\\", "/")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise MinecraftError("invalid_server_file_path")
    root = Path(server["root_path"]).resolve()
    target = root.joinpath(*parts).resolve()
    if target != root and root not in target.parents:
        raise MinecraftError("invalid_server_file_path")
    return root, target


def list_files(server, relative_path=""):
    root, target = _safe_path(server, relative_path)
    root.mkdir(parents=True, exist_ok=True)
    if not target.exists() or not target.is_dir():
        raise MinecraftError("minecraft_directory_not_found", 404)
    files = []
    for item in sorted(target.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.casefold()))[:1000]:
        if item.is_symlink():
            continue
        stat = item.stat()
        files.append({"name": item.name, "directory": item.is_dir(), "size": stat.st_size if item.is_file() else 0,
                  "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    return files


def create_path(server, relative_path, kind):
    root, target = _safe_path(server, relative_path)
    kind = str(kind or "file").lower()
    if target == root or kind not in {"file", "directory"}:
        raise MinecraftError("invalid_server_file_path")
    if target.exists() or target.is_symlink():
        raise MinecraftError("minecraft_file_already_exists", 409)
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        target.mkdir()
    else:
        write_file(server, relative_path, "")
    return {"name": target.name, "directory": kind == "directory", "size": 0}


def rename_path(server, relative_path, new_name):
    root, source = _safe_path(server, relative_path)
    new_name = str(new_name or "").strip()
    if (source == root or source.is_symlink() or not source.exists() or not new_name
            or new_name in {".", ".."} or "/" in new_name or "\\" in new_name):
        raise MinecraftError("invalid_server_file_path")
    destination = (source.parent / new_name).resolve()
    if destination != root and root not in destination.parents:
        raise MinecraftError("invalid_server_file_path")
    if destination.exists() or destination.is_symlink():
        raise MinecraftError("minecraft_file_already_exists", 409)
    source.rename(destination)
    return {"name": destination.name, "directory": destination.is_dir(), "size": destination.stat().st_size if destination.is_file() else 0}


def delete_path(server, relative_path):
    root, target = _safe_path(server, relative_path)
    if target == root or target.is_symlink() or not target.exists():
        raise MinecraftError("minecraft_file_not_found", 404)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"deleted": True}


def upload_files(server, relative_path, files):
    root, directory = _safe_path(server, relative_path)
    if not directory.exists() or not directory.is_dir() or directory.is_symlink():
        raise MinecraftError("minecraft_directory_not_found", 404)
    if not isinstance(files, list) or not files or len(files) > 10:
        raise MinecraftError("invalid_minecraft_upload")
    prepared = []
    total_size = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise MinecraftError("invalid_minecraft_upload")
        name = str(entry.get("name") or "").strip()
        content = entry.get("content")
        if not name or name in {".", ".."} or "/" in name or "\\" in name or not isinstance(content, bytes):
            raise MinecraftError("invalid_minecraft_upload")
        if len(content) > MAX_SERVER_FILE_TRANSFER_BYTES:
            raise MinecraftError("minecraft_upload_too_large", 413)
        target = (directory / name).resolve()
        if target.parent != directory.resolve() or target.exists() or target.is_symlink():
            raise MinecraftError("minecraft_file_already_exists", 409)
        total_size += len(content)
        prepared.append((target, content))
    if total_size > MAX_SERVER_FILE_TRANSFER_BYTES:
        raise MinecraftError("minecraft_upload_too_large", 413)
    used = _directory_size(root)
    limit_bytes = max(0, int(server["disk_mb"])) * 1024 * 1024
    if limit_bytes and used + total_size > limit_bytes:
        raise MinecraftError("minecraft_disk_limit_reached", 403)
    created = []
    try:
        for target, content in prepared:
            target.write_bytes(content)
            created.append(target)
    except OSError as exc:
        for target in created:
            target.unlink(missing_ok=True)
        raise MinecraftError("minecraft_file_write_failed", 500) from exc
    return [{"name": target.name, "size": len(content)} for target, content in prepared]


def read_binary_file(server, relative_path):
    root, target = _safe_path(server, relative_path)
    if not target.is_file() or target.is_symlink():
        raise MinecraftError("minecraft_file_not_found", 404)
    if target.stat().st_size > MAX_SERVER_FILE_TRANSFER_BYTES:
        raise MinecraftError("minecraft_download_too_large", 413)
    try:
        return target.read_bytes()
    except OSError as exc:
        raise MinecraftError("minecraft_file_not_readable", 400) from exc


def read_file(server, relative_path):
    root, target = _safe_path(server, relative_path)
    if not target.is_file() or target.stat().st_size > MAX_EDITABLE_FILE_BYTES:
        raise MinecraftError("minecraft_file_not_editable", 413)
    try:
        return target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise MinecraftError("minecraft_file_not_text", 400) from exc


def write_file(server, relative_path, content):
    root, target = _safe_path(server, relative_path)
    content = str(content or "")
    size = len(content.encode("utf-8"))
    if size > MAX_EDITABLE_FILE_BYTES:
        raise MinecraftError("minecraft_file_too_large", 413)
    if target.is_symlink():
        raise MinecraftError("invalid_server_file_path")
    old_size = target.stat().st_size if target.is_file() else 0
    try:
        used = sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())
    except OSError:
        used = 0
    limit_bytes = max(0, int(server["disk_mb"])) * 1024 * 1024
    if limit_bytes and used - old_size + size > limit_bytes:
        raise MinecraftError("minecraft_disk_limit_reached", 403)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")