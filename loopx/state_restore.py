"""Verify and restore LoopX state backup archives.

A backup archive contains the backed-up file tree plus an embedded
``manifest.json`` with per-file size and sha256 records (``file_index``).
Older archives without ``file_index`` remain supported with degraded
(existence and aggregate-size) verification.

Restore is journaled: every restore job stages archive content, writes a
rollback snapshot of the files it would replace, and only then applies
changes with per-file atomic replacement. An interrupted job is recognized
on the next run and can be continued or safely aborted; a partially
covered state is never left behind.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
from typing import Any
import zlib

from .state_backup import STATE_BACKUP_SCHEMA_VERSION, _target_category


RESTORE_SCHEMA_VERSION = "loopx_state_restore_v0"
JOURNAL_SCHEMA_VERSION = "loopx_restore_journal_v0"
MANIFEST_MEMBER = "manifest.json"
ALL_CATEGORIES: tuple[str, ...] = (
    "runtime",
    "project_state",
    "active_state_routes",
    "source_registries",
    "automations",
    "skills",
)
COPY_BUFFER = 1024 * 1024
_JOB_SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9._-]+")
_TERMINAL_PHASES = frozenset({"completed", "aborted", "rolled_back"})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(COPY_BUFFER), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_sha256(handle: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = handle.read(COPY_BUFFER)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _default_sidecar(archive: Path, manifest_path: Path | None) -> Path | None:
    if manifest_path is not None:
        return manifest_path
    if archive.name.endswith(".tar.gz"):
        candidate = archive.with_name(archive.name[:-7] + ".manifest.json")
        if candidate.exists():
            return candidate
    return None


def _read_embedded_manifest(tar: tarfile.TarFile) -> dict[str, Any]:
    try:
        member = tar.getmember(MANIFEST_MEMBER)
    except KeyError as exc:
        raise ValueError("archive does not contain manifest.json") from exc
    handle = tar.extractfile(member)
    if handle is None:
        raise ValueError("manifest.json is not a regular file")
    payload = json.loads(handle.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest.json must contain a JSON object")
    return payload


def _info_kind(info: tarfile.TarInfo) -> str:
    if info.isdir():
        return "dir"
    if info.issym():
        return "symlink"
    if info.isfile():
        return "file"
    return "other"


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _target_map(manifest: dict[str, Any]) -> dict[str, str]:
    included = manifest.get("included") if isinstance(manifest.get("included"), list) else []
    mapping: dict[str, str] = {}
    for item in included:
        if isinstance(item, dict) and item.get("key"):
            mapping[str(item["key"])] = str(item.get("archive_path") or "")
    return mapping


def _embedded_sidecar_consistency(
    embedded: dict[str, Any],
    sidecar: dict[str, Any],
) -> tuple[bool, str]:
    diffs: list[str] = []
    for field in ("schema_version", "backup_id", "project", "runtime_root"):
        if embedded.get(field) != sidecar.get(field):
            diffs.append(f"{field} differs")
    if embedded.get("file_index") != sidecar.get("file_index"):
        diffs.append("file_index differs")
    if _target_map(embedded) != _target_map(sidecar):
        diffs.append("included targets differ")
    if diffs:
        return False, "; ".join(diffs)
    return True, ""


def _problem(problems: list[dict[str, str]], *, path: str, reason: str, detail: str = "") -> None:
    problems.append({"path": path, "reason": reason, "detail": detail})


def _verify_file_index(
    tar: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    file_index: list[Any],
    problems: list[dict[str, str]],
) -> int:
    referenced: set[str] = {MANIFEST_MEMBER}
    checked_files = 0
    for record in file_index:
        if not isinstance(record, dict) or not record.get("name"):
            _problem(
                problems,
                path="manifest.json",
                reason="malformed_manifest_record",
                detail=str(record)[:200],
            )
            continue
        name = str(record["name"])
        referenced.add(name)
        info = members.get(name)
        if info is None:
            _problem(problems, path=name, reason="missing", detail="member absent from archive")
            continue
        actual_kind = _info_kind(info)
        expected_kind = str(record.get("kind") or "")
        if actual_kind != expected_kind:
            _problem(
                problems,
                path=name,
                reason="kind_changed",
                detail=f"expected {expected_kind}, archive holds {actual_kind}",
            )
        if actual_kind == "file":
            if int(record.get("size", -1)) != info.size:
                _problem(
                    problems,
                    path=name,
                    reason="truncated_or_resized",
                    detail=f"expected {record.get('size')} bytes, archive holds {info.size}",
                )
            checked_files += 1
            handle = tar.extractfile(info)
            actual_sha, streamed = _stream_sha256(handle if handle is not None else io.BytesIO(b""))
            if streamed != info.size:
                _problem(
                    problems,
                    path=name,
                    reason="truncated_or_resized",
                    detail=f"member header says {info.size} bytes, stream yielded {streamed}",
                )
            expected_sha = record.get("sha256")
            if expected_sha and expected_sha != actual_sha:
                _problem(
                    problems,
                    path=name,
                    reason="digest_mismatch",
                    detail="content does not match recorded sha256",
                )
        elif actual_kind == "symlink":
            if str(record.get("linktarget") or "") != info.linkname:
                _problem(
                    problems,
                    path=name,
                    reason="digest_mismatch",
                    detail=(
                        f"symlink target changed: recorded "
                        f"{record.get('linktarget')!r}, archive holds {info.linkname!r}"
                    ),
                )
    for name, info in members.items():
        if name not in referenced:
            _problem(
                problems,
                path=name,
                reason="unexpected_member",
                detail="member is not recorded in the manifest file index",
            )
    return checked_files


def _verify_legacy_targets(
    members: dict[str, tarfile.TarInfo],
    embedded: dict[str, Any],
    problems: list[dict[str, str]],
) -> int:
    included = embedded.get("included") if isinstance(embedded.get("included"), list) else []
    referenced: set[str] = {MANIFEST_MEMBER}
    checked_files = 0
    for item in included:
        if not isinstance(item, dict):
            continue
        prefix = str(item.get("archive_path") or "")
        if not prefix:
            continue
        owned = {
            name: info
            for name, info in members.items()
            if name == prefix or name.startswith(prefix + "/")
        }
        if not owned:
            _problem(
                problems,
                path=prefix,
                reason="missing",
                detail=f"target {item.get('key')} absent from archive",
            )
            continue
        referenced.update(owned)
        files = [info for info in owned.values() if info.isfile()]
        checked_files += len(files)
        dirs = [info for info in owned.values() if info.isdir()]
        symlinks = [info for info in owned.values() if info.issym()]
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        expected_counts = {
            "paths": len(owned),
            "files": len(files),
            "directories": len(dirs),
            "symlinks": len(symlinks),
        }
        for field, actual in expected_counts.items():
            expected = stats.get(field)
            if expected is not None and actual != int(expected):
                _problem(
                    problems,
                    path=prefix,
                    reason="truncated_or_resized",
                    detail=f"expected {expected} {field}, archive holds {actual}",
                )
        # Aggregate byte counts cannot be reconstructed: tar stores zero size
        # for directories, which contributed st_size to the old stats.
    for name in members:
        if name not in referenced:
            _problem(
                problems,
                path=name,
                reason="unexpected_member",
                detail="member is outside every manifest target prefix",
            )
    return checked_files


_VERIFY_REASON_GROUPS: dict[str, tuple[str, ...]] = {
    "missing": ("missing",),
    "truncated": ("truncated_or_resized",),
    "modified": ("digest_mismatch", "kind_changed"),
    "unexpected": ("unexpected_member", "malformed_manifest_record"),
}


def _verify_problem_counts(problems: list[dict[str, str]]) -> dict[str, int]:
    counts = {group: 0 for group in _VERIFY_REASON_GROUPS}
    counts["other"] = 0
    for problem in problems:
        reason = str(problem.get("reason") or "")
        for group, reasons in _VERIFY_REASON_GROUPS.items():
            if reason in reasons:
                counts[group] += 1
                break
        else:
            counts["other"] += 1
    return counts


def verify_state_archive(
    archive_path: Path | str,
    *,
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    archive = Path(str(archive_path)).expanduser()
    sidecar_arg = (
        Path(str(manifest_path)).expanduser() if manifest_path is not None else None
    )
    problems: list[dict[str, str]] = []
    checks: dict[str, Any] = {
        "archive_readable": False,
        "embedded_manifest_present": False,
        "sidecar_manifest": "absent",
        "archive_sha256_match": None,
        "embedded_sidecar_consistent": None,
        "per_file_digests": None,
        "integrity_level": None,
    }
    member_count = 0
    checked_file_count = 0
    backup_id = None

    if not archive.exists():
        _problem(
            problems,
            path=str(archive),
            reason="archive_missing",
            detail="archive file does not exist",
        )
    else:
        sidecar: dict[str, Any] | None = None
        sidecar_path = _default_sidecar(archive, sidecar_arg)
        if sidecar_path is not None:
            try:
                loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                _problem(
                    problems,
                    path=str(sidecar_path),
                    reason="sidecar_manifest_unreadable",
                    detail=str(exc),
                )
            else:
                if isinstance(loaded, dict):
                    sidecar = loaded
                    checks["sidecar_manifest"] = "present"
                else:
                    _problem(
                        problems,
                        path=str(sidecar_path),
                        reason="sidecar_manifest_unreadable",
                        detail="sidecar manifest must contain a JSON object",
                    )
        try:
            actual_archive_sha = _file_sha256(archive)
            checks["archive_readable"] = True
        except OSError as exc:
            _problem(
                problems,
                path=str(archive),
                reason="archive_unreadable",
                detail=str(exc),
            )
        else:
            if sidecar is not None:
                execution = (
                    sidecar.get("execution")
                    if isinstance(sidecar.get("execution"), dict)
                    else {}
                )
                expected_sha = execution.get("archive_sha256")
                if expected_sha:
                    checks["archive_sha256_match"] = expected_sha == actual_archive_sha
                    if not checks["archive_sha256_match"]:
                        _problem(
                            problems,
                            path=str(archive),
                            reason="archive_sha256_mismatch",
                            detail="archive bytes do not match the sidecar manifest sha256",
                        )
            try:
                with tarfile.open(archive, "r:gz") as tar:
                    members = {info.name: info for info in tar.getmembers()}
                    member_count = len(members)
                    embedded: dict[str, Any] | None = None
                    try:
                        embedded = _read_embedded_manifest(tar)
                        checks["embedded_manifest_present"] = True
                        backup_id = embedded.get("backup_id")
                    except ValueError as exc:
                        _problem(
                            problems,
                            path=MANIFEST_MEMBER,
                            reason="embedded_manifest_missing",
                            detail=str(exc),
                        )
                    if embedded is not None and sidecar is not None:
                        consistent, detail = _embedded_sidecar_consistency(
                            embedded, sidecar
                        )
                        checks["embedded_sidecar_consistent"] = consistent
                        if not consistent:
                            _problem(
                                problems,
                                path=MANIFEST_MEMBER,
                                reason="embedded_sidecar_manifest_mismatch",
                                detail=detail,
                            )
                    if embedded is not None:
                        file_index = embedded.get("file_index")
                        if isinstance(file_index, list) and file_index:
                            checks["per_file_digests"] = True
                            checks["integrity_level"] = "existence_size_digest"
                            checked_file_count = _verify_file_index(
                                tar, members, file_index, problems
                            )
                        else:
                            checks["per_file_digests"] = False
                            checks["integrity_level"] = "existence_size"
                            checked_file_count = _verify_legacy_targets(
                                members, embedded, problems
                            )
            except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
                _problem(
                    problems,
                    path=str(archive),
                    reason="archive_unreadable",
                    detail=str(exc),
                )

    counts = _verify_problem_counts(problems)
    corrupt = bool(problems)
    payload: dict[str, Any] = {
        "ok": not corrupt,
        "schema_version": RESTORE_SCHEMA_VERSION,
        "backup_schema_version": STATE_BACKUP_SCHEMA_VERSION,
        "mode": "state_verify",
        "dry_run": True,
        "created_at": _utcnow_iso(),
        "archive_path": str(archive),
        "backup_id": backup_id,
        "checks": checks,
        "problems": problems,
        "summary": {
            "member_count": member_count,
            "checked_file_count": checked_file_count,
            "problem_count": len(problems),
            "integrity_level": checks["integrity_level"],
            **counts,
        },
        "recommended_action": (
            "do not restore from this archive; re-create a backup after review"
            if corrupt
            else "archive integrity verified; safe to plan a restore"
        ),
    }
    return payload


# ---------------------------------------------------------------------------
# Restore plan (read-only dry run)
# ---------------------------------------------------------------------------


def _normalize_categories(categories: Any) -> list[str]:
    if not categories:
        return list(ALL_CATEGORIES)
    values: list[str] = []
    for raw in categories:
        for item in str(raw).split(","):
            item = item.strip()
            if item:
                values.append(item)
    invalid = sorted(set(values) - set(ALL_CATEGORIES))
    if invalid:
        raise ValueError(f"unknown categories: {', '.join(invalid)}")
    return sorted(set(values))


def _validate_member_name(name: str) -> None:
    if not name or name.startswith("/") or "\\" in name:
        raise ValueError(f"unsafe archive member name: {name!r}")
    parts = name.split("/")
    if any(part in ("", "..") for part in parts):
        raise ValueError(f"unsafe archive member name: {name!r}")


def _targets_by_key(manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    included = manifest.get("included") if isinstance(manifest.get("included"), list) else []
    result: dict[str, dict[str, str]] = {}
    for item in included:
        if isinstance(item, dict) and item.get("key"):
            result[str(item["key"])] = {
                "archive_path": str(item.get("archive_path") or ""),
                "source_path": str(item.get("source_path") or ""),
            }
    return result


def _synthesize_legacy_records(
    manifest: dict[str, Any],
    members: list[tarfile.TarInfo],
) -> list[dict[str, Any]]:
    targets = [
        (str(item.get("archive_path") or ""), str(item.get("key") or ""))
        for item in manifest.get("included", [])
        if isinstance(item, dict) and item.get("archive_path")
    ]
    targets.sort(key=lambda entry: len(entry[0]), reverse=True)
    records: list[dict[str, Any]] = []
    for info in members:
        if info.name == MANIFEST_MEMBER:
            continue
        owner: tuple[str, str] | None = None
        for prefix, key in targets:
            if info.name == prefix or info.name.startswith(prefix + "/"):
                owner = (prefix, key)
                break
        if owner is None:
            continue
        kind = _info_kind(info)
        if kind == "other":
            continue
        records.append(
            {
                "name": info.name,
                "kind": kind,
                "size": int(info.size),
                "mode": int(info.mode & 0o7777),
                "sha256": None,
                "linktarget": info.linkname if kind == "symlink" else None,
                "target_key": owner[1],
                "category": _target_category(owner[1]),
            }
        )
    return records


def _destination_for(
    record: dict[str, Any],
    *,
    project: Path,
    runtime_root: Path,
    codex_home: Path,
    targets: dict[str, dict[str, str]],
) -> Path:
    key = str(record.get("target_key") or "")
    name = str(record.get("name") or "")
    if key == "runtime_root":
        if name == "runtime-root":
            return runtime_root
        return runtime_root / name.removeprefix("runtime-root/")
    if key.startswith("project_"):
        return project / name.removeprefix("project/")
    if key.startswith("codex_"):
        return codex_home / name.removeprefix("codex/")
    target = targets.get(key)
    if target is None:
        raise ValueError(f"manifest has no source target for {key}")
    prefix = target["archive_path"]
    source_path = Path(target["source_path"]).expanduser()
    if name == prefix:
        return source_path
    return source_path / name.removeprefix(prefix + "/")


def _classify_destination(
    record: dict[str, Any],
    dest: Path,
    *,
    legacy: bool,
) -> tuple[str, str]:
    kind = str(record.get("kind") or "")
    is_link = dest.is_symlink()
    exists = is_link or dest.exists()
    if kind == "dir":
        if not exists:
            return "add", "missing"
        if is_link or not dest.is_dir():
            return "conflict", "path_type_mismatch"
        return "unchanged", ""
    if kind == "symlink":
        if not exists:
            return "add", "missing"
        if not is_link:
            return "conflict", "path_type_mismatch"
        if os.readlink(dest) == str(record.get("linktarget") or ""):
            return "unchanged", ""
        return "conflict", "symlink_target_changed"
    if not exists:
        return "add", "missing"
    if is_link or dest.is_dir():
        return "conflict", "path_type_mismatch"
    if legacy:
        return "overwrite", "unverified_legacy"
    if dest.stat().st_size != int(record.get("size", -1)):
        return "overwrite", "content_changed"
    if _file_sha256(dest) == str(record.get("sha256") or ""):
        return "unchanged", ""
    return "overwrite", "content_changed"


def _goal_id_from_route(route: str) -> str | None:
    parts = route.split("/")
    if "goals" in parts:
        index = parts.index("goals")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def _affected_sessions_and_config(
    entries: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    project_codex: set[str] = set()
    project_claude: set[str] = set()
    project_local: set[str] = set()
    registry_goals: set[str] = set()
    registry_project_paths: set[str] = set()
    registries: set[str] = set()
    global_registry: str | None = None
    automation_ids: set[str] = set()
    skill_names: set[str] = set()

    for entry in entries:
        key = str(entry.get("target_key") or "")
        dest = str(entry.get("dest") or "")
        remainder = ""
        if key == "runtime_root":
            remainder = str(entry.get("name")).removeprefix("runtime-root/")
        elif key.startswith("project_"):
            remainder = str(entry.get("name")).removeprefix("project/")
        goal_id = _goal_id_from_route(remainder)
        if goal_id:
            if "/.codex/" in f"/{remainder}":
                project_codex.add(goal_id)
            elif "/.claude/" in f"/{remainder}":
                project_claude.add(goal_id)
            elif "/.local/" in f"/{remainder}":
                project_local.add(goal_id)
        if key.startswith("registry_project_"):
            registry_goals.add(key.split(":", 1)[1])
            for marker in ("/.loopx/", "/.codex/", "/.claude/", "/.local/"):
                if marker in dest:
                    registry_project_paths.add(dest.split(marker, 1)[0])
                    break
        elif key.startswith("registry_") and ":" in key:
            registry_goals.add(key.split(":", 1)[1])
        if dest.endswith("registry.json"):
            registries.add(dest)
        if dest.endswith("registry.global.json"):
            global_registry = dest
        if key == "codex_automations" and dest:
            automation_ids.add(Path(dest).stem)
        if key.startswith("codex_skill:"):
            skill_names.add(key.split(":", 1)[1])

    sessions = {
        "project_codex_goal_ids": sorted(project_codex),
        "project_claude_goal_ids": sorted(project_claude),
        "project_local_goal_ids": sorted(project_local),
        "registry_goal_ids": sorted(registry_goals),
        "registry_project_paths": sorted(p for p in registry_project_paths if p and p != "."),
    }
    configuration = {
        "registries": sorted(registries),
        "global_registry": global_registry,
        "automation_ids": sorted(automation_ids),
        "skill_names": sorted(skill_names),
    }
    return sessions, configuration


def build_state_restore_plan(
    archive_path: Path | str,
    *,
    project: Path | str | None = None,
    runtime_root: Path | str | None = None,
    categories: Any = None,
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    archive = Path(str(archive_path)).expanduser()
    selected_categories = _normalize_categories(categories)
    if not archive.exists():
        raise ValueError(f"archive does not exist: {archive}")

    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        embedded = _read_embedded_manifest(tar)
        raw_index = embedded.get("file_index")
        legacy = not (isinstance(raw_index, list) and raw_index)
        records = (
            [dict(item) for item in raw_index if isinstance(item, dict)]
            if not legacy
            else _synthesize_legacy_records(embedded, members)
        )

    resolved_project = (
        Path(str(project)).expanduser().resolve()
        if project is not None
        else Path(str(embedded.get("project") or ".")).expanduser().resolve()
    )
    resolved_runtime = (
        Path(str(runtime_root)).expanduser().resolve()
        if runtime_root is not None
        else Path(str(embedded.get("runtime_root") or "")).expanduser().resolve()
    )
    codex_home = Path(
        os.environ.get("CODEX_HOME", Path.home() / ".codex")
    ).expanduser().resolve()
    targets = _targets_by_key(embedded)

    to_add: list[dict[str, Any]] = []
    to_overwrite: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for record in records:
        name = str(record.get("name") or "")
        _validate_member_name(name)
        category = str(record.get("category") or _target_category(str(record.get("target_key") or "")))
        if category not in selected_categories:
            continue
        dest = _destination_for(
            record,
            project=resolved_project,
            runtime_root=resolved_runtime,
            codex_home=codex_home,
            targets=targets,
        )
        change, reason = _classify_destination(record, dest, legacy=legacy)
        entry = {
            "name": name,
            "dest": str(dest),
            "kind": str(record.get("kind") or ""),
            "size": int(record.get("size") or 0),
            "mode": int(record.get("mode") if record.get("mode") is not None else 0o644),
            "sha256": record.get("sha256"),
            "linktarget": record.get("linktarget"),
            "target_key": str(record.get("target_key") or ""),
            "category": category,
            "change": change,
            "reason": reason,
        }
        if change == "add":
            to_add.append(entry)
        elif change == "overwrite":
            to_overwrite.append(entry)
        elif change == "conflict":
            conflicts.append(entry)
        else:
            unchanged.append(entry)

    changed_entries = to_add + to_overwrite
    affected_sessions, affected_configuration = _affected_sessions_and_config(
        changed_entries + conflicts
    )

    def change_bytes(items: list[dict[str, Any]]) -> int:
        return sum(int(item.get("size") or 0) for item in items if item.get("kind") == "file")

    output_dir = Path(str(embedded.get("output_dir") or resolved_runtime / "backups")).expanduser()
    integrity_level = "existence_size" if legacy else "existence_size_digest"
    summary: dict[str, Any] = {
        "selected_categories": selected_categories,
        "integrity_level": integrity_level,
        "add_count": len(to_add),
        "overwrite_count": len(to_overwrite),
        "conflict_count": len(conflicts),
        "unchanged_count": len(unchanged),
        "add_bytes": change_bytes(to_add),
        "overwrite_bytes": change_bytes(to_overwrite),
    }
    category_counts: dict[str, dict[str, int]] = {}
    for bucket in (to_add, to_overwrite, conflicts):
        for entry in bucket:
            stats = category_counts.setdefault(entry["category"], {"paths": 0, "bytes": 0})
            stats["paths"] += 1
            if entry["kind"] == "file":
                stats["bytes"] += int(entry["size"])
    summary["category_counts"] = category_counts

    return {
        "ok": True,
        "schema_version": RESTORE_SCHEMA_VERSION,
        "mode": "state_restore",
        "dry_run": True,
        "execute_requested": False,
        "created_at": _utcnow_iso(),
        "archive_path": str(archive),
        "backup_id": embedded.get("backup_id"),
        "project": str(resolved_project),
        "runtime_root": str(resolved_runtime),
        "codex_home": str(codex_home),
        "categories": selected_categories,
        "legacy_archive": legacy,
        "integrity_level": integrity_level,
        "to_add": to_add,
        "to_overwrite": to_overwrite,
        "conflicts": conflicts,
        "unchanged": unchanged,
        "affected_sessions": affected_sessions,
        "affected_configuration": affected_configuration,
        "jobs_dir": str(output_dir / "restore-jobs"),
        "summary": summary,
        "recommended_action": (
            "review the changes; run with --execute to restore, or repeat with "
            "--category to narrow scope"
        ),
    }


# ---------------------------------------------------------------------------
# Journaled restore execution
# ---------------------------------------------------------------------------


def _job_slug(value: str, *, fallback: str) -> str:
    compact = _JOB_SLUG_PATTERN.sub("-", str(value)).strip("-._") or fallback
    return compact[:64]


def _selection_token(categories: list[str]) -> str:
    return hashlib.sha1(",".join(categories).encode("utf-8")).hexdigest()[:10]


@contextmanager
def _jobs_lock(jobs_dir: Path):
    jobs_dir.mkdir(parents=True, exist_ok=True)
    handle = open(jobs_dir / ".lock", "a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except (ImportError, AttributeError):
        pass
    try:
        yield
    finally:
        try:
            handle.close()
        except OSError:
            pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def _load_journal(job_dir: Path) -> dict[str, Any] | None:
    path = job_dir / "journal.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _find_other_active_job(jobs_dir: Path, backup_id: str, current_job: Path) -> Path | None:
    if not jobs_dir.exists():
        return None
    for entry in jobs_dir.iterdir():
        if not entry.is_dir() or entry == current_job:
            continue
        journal = _load_journal(entry)
        if journal is None:
            continue
        if journal.get("phase") not in _TERMINAL_PHASES and journal.get("backup_id") == backup_id:
            return entry
    return None


def _stage_files(
    plan: dict[str, Any],
    job_dir: Path,
    entries: list[dict[str, Any]],
) -> None:
    archive = Path(str(plan["archive_path"])).expanduser()
    staging = job_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        member_map = {info.name: info for info in tar.getmembers()}
        for entry in entries:
            name = str(entry["name"])
            info = member_map.get(name)
            if info is None:
                raise ValueError(f"archive member vanished before staging: {name}")
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if info.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif info.issym():
                if target.exists() or target.is_symlink():
                    target.unlink()
                os.symlink(info.linkname, target)
            elif info.isfile():
                handle = tar.extractfile(info)
                if handle is None:
                    raise ValueError(f"cannot extract {name}")
                hasher = hashlib.sha256()
                with target.open("wb") as out:
                    while True:
                        chunk = handle.read(COPY_BUFFER)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        out.write(chunk)
                if target.stat().st_size != info.size:
                    raise ValueError(f"staged file size mismatch: {name}")
                if entry.get("sha256") and hasher.hexdigest() != str(entry["sha256"]):
                    raise ValueError(f"staged file digest mismatch: {name}")
            else:
                raise ValueError(f"unsupported archive member type: {name}")


def _write_rollback_snapshot(
    job_dir: Path,
    ops: list[dict[str, Any]],
) -> None:
    rollback_path = job_dir / "rollback.tar"
    with tarfile.open(rollback_path, "w", format=tarfile.PAX_FORMAT) as rollback:
        for op in ops:
            if op["action"] != "overwrite":
                continue
            dest = Path(str(op["dest"]))
            if not (dest.exists() or dest.is_symlink()):
                raise ValueError(f"file to replace vanished before snapshot: {dest}")
            original = f"originals/{op['id']}"
            rollback.add(dest, arcname=original, recursive=False)
            op["original_member"] = original


def _ordered_ops(plan: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [*plan["to_add"], *plan["to_overwrite"]]
    dir_entries = sorted(
        (entry for entry in entries if entry["kind"] == "dir"),
        key=lambda item: str(item["dest"]),
    )
    file_entries = sorted(
        (entry for entry in entries if entry["kind"] != "dir"),
        key=lambda item: str(item["dest"]),
    )
    ops: list[dict[str, Any]] = []
    for index, entry in enumerate([*dir_entries, *file_entries]):
        ops.append(
            {
                "id": f"{index:04d}",
                "action": entry["change"],
                "kind": entry["kind"],
                "dest": entry["dest"],
                "member": entry["name"],
                "size": entry["size"],
                "mode": entry["mode"],
                "sha256": entry["sha256"],
                "linktarget": entry["linktarget"],
                "reason": entry["reason"],
                "original_member": None,
                "state": "pending",
            }
        )
    return ops


def prepare_restore(plan: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    job_dir.mkdir(parents=True, exist_ok=True)
    entries = [*plan["to_add"], *plan["to_overwrite"]]
    _stage_files(plan, job_dir, entries)
    ops = _ordered_ops(plan)
    _write_rollback_snapshot(job_dir, ops)
    journal = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "backup_id": plan["backup_id"],
        "archive_path": plan["archive_path"],
        "categories": list(plan["categories"]),
        "created_at": _utcnow_iso(),
        "phase": "prepared",
        "ops": ops,
        "created_dirs": [],
        "applied_count": 0,
        "warnings": [],
    }
    _atomic_write_json(job_dir / "journal.json", journal)
    return journal


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _ensure_parent_dirs(op: dict[str, Any], journal: dict[str, Any]) -> None:
    dest = Path(str(op["dest"]))
    parent = dest.parent
    created: list[Path] = []
    while not parent.exists():
        created.append(parent)
        parent = parent.parent
    for directory in reversed(created):
        directory.mkdir(parents=False, exist_ok=True)
        journal["created_dirs"].append(str(directory))


def _apply_one(op: dict[str, Any], job_dir: Path, journal: dict[str, Any]) -> None:
    dest = Path(str(op["dest"]))
    staging = job_dir / "staging" / str(op["member"])
    if op["kind"] == "dir":
        if not dest.exists():
            dest.mkdir(parents=True, exist_ok=True)
            journal["created_dirs"].append(str(dest))
        return
    _ensure_parent_dirs(op, journal)
    tmp = dest.with_name(
        f".{dest.name}.loopx-restore.tmp-{os.getpid()}-{op['id']}"
    )
    if op["kind"] == "symlink":
        os.symlink(str(op["linktarget"]), tmp)
    else:
        shutil.copyfile(staging, tmp)
        os.chmod(tmp, int(op["mode"]))
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
    os.replace(tmp, dest)
    _fsync_directory(dest.parent)


def apply_restore(
    plan: dict[str, Any],
    job_dir: Path,
    journal: dict[str, Any],
    *,
    max_ops: int = 0,
) -> dict[str, Any]:
    journal["phase"] = "applying"
    _atomic_write_json(job_dir / "journal.json", journal)
    applied_now = 0
    failure: BaseException | None = None
    try:
        for op in journal["ops"]:
            if op["state"] == "done":
                continue
            _apply_one(op, job_dir, journal)
            op["state"] = "done"
            journal["applied_count"] = int(journal.get("applied_count") or 0) + 1
            _atomic_write_json(job_dir / "journal.json", journal)
            applied_now += 1
            if max_ops and applied_now >= max_ops:
                return journal
    except BaseException as exc:
        failure = exc
        try:
            rollback_to_snapshot(job_dir, journal, user_abort=False)
        except Exception as rollback_exc:
            journal["phase"] = "applying"
            journal.setdefault("warnings", []).append(
                f"automatic rollback failed: {rollback_exc}; manual intervention required"
            )
            _atomic_write_json(job_dir / "journal.json", journal)
            raise
        _atomic_write_json(job_dir / "journal.json", journal)
    if failure is not None:
        # Changes were rolled back; report a stable failure state instead of
        # propagating the exception so callers never see a half-covered tree.
        return journal
    journal["phase"] = "completed"
    journal["completed_at"] = _utcnow_iso()
    _atomic_write_json(job_dir / "journal.json", journal)
    return journal


def _restore_original(
    info: tarfile.TarInfo,
    rollback: tarfile.TarFile,
    dest: Path,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(
        f".{dest.name}.loopx-rollback.tmp-{os.getpid()}"
    )
    if info.issym():
        os.symlink(info.linkname, tmp)
    else:
        handle = rollback.extractfile(info)
        if handle is None:
            raise ValueError(f"cannot read original for {dest}")
        with tmp.open("wb") as out:
            shutil.copyfileobj(handle, out, COPY_BUFFER)
        os.chmod(tmp, int(info.mode & 0o7777))
    os.replace(tmp, dest)


def _remove_added(op: dict[str, Any], dest: Path, warnings: list[str]) -> None:
    if op["kind"] == "symlink":
        if dest.is_symlink() and os.readlink(dest) == str(op.get("linktarget") or ""):
            dest.unlink()
        else:
            warnings.append(f"left added symlink in place (externally changed): {dest}")
        return
    if op["kind"] == "dir":
        try:
            dest.rmdir()
        except OSError:
            pass
        return
    if dest.is_file() and not dest.is_symlink():
        digest = _file_sha256(dest)
        if digest == str(op.get("sha256") or ""):
            dest.unlink()
        else:
            warnings.append(f"left added file in place (externally changed): {dest}")
    else:
        warnings.append(f"added path no longer matches restore record: {dest}")


def rollback_to_snapshot(
    job_dir: Path,
    journal: dict[str, Any],
    *,
    user_abort: bool,
) -> dict[str, Any]:
    warnings: list[str] = []
    rollback_path = job_dir / "rollback.tar"
    if rollback_path.exists():
        rollback = tarfile.open(rollback_path, "r")
        try:
            originals = {info.name: info for info in rollback.getmembers()}
            for op in reversed(journal["ops"]):
                if op["state"] != "done":
                    continue
                dest = Path(str(op["dest"]))
                if op["action"] == "overwrite":
                    original_name = op.get("original_member")
                    info = originals.get(str(original_name)) if original_name else None
                    if info is None:
                        warnings.append(f"original missing from snapshot: {dest}")
                        continue
                    _restore_original(info, rollback, dest)
                elif op["action"] == "add":
                    _remove_added(op, dest, warnings)
        finally:
            rollback.close()
    else:
        warnings.append("rollback snapshot absent; could not restore originals")
    for directory_text in reversed(journal.get("created_dirs", [])):
        directory = Path(directory_text)
        try:
            directory.rmdir()
        except OSError:
            pass
    for parent in {
        str(Path(str(op["dest"])).parent) for op in journal["ops"]
    }:
        try:
            for leftover in Path(parent).glob(".*.loopx-*.tmp-*"):
                if leftover.is_file() and not leftover.is_symlink():
                    leftover.unlink()
        except OSError:
            pass
    journal["warnings"] = [*journal.get("warnings", []), *warnings]
    journal["phase"] = "aborted" if user_abort else "rolled_back"
    journal["finished_at"] = _utcnow_iso()
    _atomic_write_json(job_dir / "journal.json", journal)
    return journal


def _resume_validate(journal: dict[str, Any]) -> None:
    for op in journal["ops"]:
        if op["state"] != "done" or op["kind"] != "file":
            continue
        dest = Path(str(op["dest"]))
        if not dest.is_file() or dest.is_symlink():
            raise ValueError(
                f"restored file changed or vanished; use --abort to give up: {dest}"
            )
        if op.get("sha256") and _file_sha256(dest) != str(op["sha256"]):
            raise ValueError(
                f"restored file changed externally; use --abort to give up: {dest}"
            )


def _ensure_resume_assets(
    plan: dict[str, Any], job_dir: Path, journal: dict[str, Any]
) -> None:
    needs_snapshot = any(
        op["action"] == "overwrite" for op in journal["ops"] if op["state"] != "done"
    )
    if needs_snapshot and not (job_dir / "rollback.tar").exists():
        raise ValueError(
            "rollback snapshot is missing; cannot guarantee a safe continuation"
        )
    pending_entries = [
        entry
        for entry in [*plan["to_add"], *plan["to_overwrite"]]
        if any(
            op["member"] == entry["name"] and op["state"] != "done"
            for op in journal["ops"]
        )
    ]
    missing_stage = [
        entry
        for entry in pending_entries
        if not (job_dir / "staging" / str(entry["name"])).exists()
    ]
    if missing_stage:
        _stage_files(plan, job_dir, missing_stage)


def _result_payload(
    plan: dict[str, Any],
    job_dir: Path,
    journal: dict[str, Any],
) -> dict[str, Any]:
    states = {
        "completed": ("restore completed; rollback snapshot retained for safety", True),
        "aborted": ("restore safely abandoned; original state preserved", True),
        "rolled_back": ("restore failed; changes rolled back to the pre-restore state", False),
        "prepared": ("restore staged but not applied; rerun to continue or --abort", True),
        "applying": (
            "restore partially applied; rerun to continue or use --abort",
            False,
        ),
    }
    message, ok = states.get(str(journal["phase"]), ("unknown journal state", False))
    done_count = sum(1 for op in journal["ops"] if op["state"] == "done")
    return {
        **plan,
        "ok": ok,
        "dry_run": False,
        "execute_requested": True,
        "restore_state": journal["phase"],
        "restored_count": done_count,
        "total_op_count": len(journal["ops"]),
        "unchanged_count": len(plan.get("unchanged") or []),
        "conflict_count": len(plan.get("conflicts") or []),
        "rollback_snapshot": str(job_dir / "rollback.tar"),
        "journal_path": str(job_dir / "journal.json"),
        "warnings": journal.get("warnings", []),
        "recommended_action": message,
    }


def execute_state_restore(
    plan: dict[str, Any],
    *,
    abort: bool = False,
) -> dict[str, Any]:
    if not isinstance(plan, dict) or not plan.get("ok"):
        raise ValueError("a successful restore plan is required before execution")
    backup_id = str(plan.get("backup_id") or "")
    categories = list(plan.get("categories") or [])
    jobs_dir = Path(str(plan["jobs_dir"])).expanduser()
    job_name = (
        f"{_job_slug(backup_id, fallback='backup')}-{_selection_token(categories)}"
    )
    job_dir = jobs_dir / job_name

    with _jobs_lock(jobs_dir):
        other = _find_other_active_job(jobs_dir, backup_id, job_dir)
        if other is not None:
            raise ValueError(
                f"another restore job for this backup is active: {other}; "
                "abort it before starting a new selection"
            )
        journal = _load_journal(job_dir)
        if journal is not None:
            phase = str(journal.get("phase") or "")
            if phase in _TERMINAL_PHASES and phase != "completed":
                archived = job_dir / f"journal-{_job_slug(phase, fallback='past')}.json"
                shutil.copyfile(job_dir / "journal.json", archived)
                journal = None
            elif phase == "completed":
                return _result_payload(plan, job_dir, journal)
        if journal is not None:
            if abort:
                journal = rollback_to_snapshot(job_dir, journal, user_abort=True)
                return _result_payload(plan, job_dir, journal)
            if str(journal.get("archive_path") or "") != str(plan["archive_path"]):
                raise ValueError(
                    "existing restore job is bound to a different archive path"
                )
            _resume_validate(journal)
            _ensure_resume_assets(plan, job_dir, journal)
            journal = apply_restore(plan, job_dir, journal)
            return _result_payload(plan, job_dir, journal)

        if abort:
            raise ValueError("no active restore job found for this archive and selection")
        if plan.get("conflicts"):
            raise ValueError(
                f"{len(plan['conflicts'])} conflicting paths must be resolved "
                "before restore can run"
            )
        if not plan["to_add"] and not plan["to_overwrite"]:
            return {
                **plan,
                "dry_run": False,
                "execute_requested": True,
                "restore_state": "completed",
                "restored_count": 0,
                "recommended_action": "current state already matches the archive; nothing restored",
            }
        journal = prepare_restore(plan, job_dir)
        journal = apply_restore(plan, job_dir, journal)
        return _result_payload(plan, job_dir, journal)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_state_verify_markdown(payload: dict[str, Any]) -> str:
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# LoopX State Archive Verification",
        "",
        f"- OK: `{payload.get('ok')}`",
        f"- Archive: `{payload.get('archive_path')}`",
        f"- Backup id: `{payload.get('backup_id')}`",
        f"- Integrity level: `{checks.get('integrity_level')}`",
        f"- Archive readable: `{checks.get('archive_readable')}`",
        f"- Embedded manifest: `{checks.get('embedded_manifest_present')}`",
        f"- Sidecar manifest: `{checks.get('sidecar_manifest')}`",
        f"- Archive sha256 match: `{checks.get('archive_sha256_match')}`",
        f"- Per-file digests: `{checks.get('per_file_digests')}`",
        f"- Members: `{summary.get('member_count')}`",
        f"- Files checked: `{summary.get('checked_file_count')}`",
        f"- Missing: `{summary.get('missing')}`",
        f"- Truncated/resized: `{summary.get('truncated')}`",
        f"- Modified: `{summary.get('modified')}`",
        f"- Unexpected members: `{summary.get('unexpected')}`",
        f"- Recommended action: {payload.get('recommended_action')}",
    ]
    problems = payload.get("problems") if isinstance(payload.get("problems"), list) else []
    if problems:
        lines.extend(["", "## Problems", ""])
        for problem in problems:
            if isinstance(problem, dict):
                detail = f": {problem.get('detail')}" if problem.get("detail") else ""
                lines.append(
                    f"- `{problem.get('path')}` — {problem.get('reason')}{detail}"
                )
    return "\n".join(lines) + "\n"


def _render_path_section(title: str, entries: list[dict[str, Any]]) -> list[str]:
    if not entries:
        return []
    lines = ["", f"## {title}", ""]
    for entry in entries:
        if isinstance(entry, dict):
            reason = f" ({entry.get('reason')})" if entry.get("reason") else ""
            lines.append(f"- `{entry.get('dest')}`{reason}")
    return lines


def render_state_restore_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    sessions = (
        payload.get("affected_sessions")
        if isinstance(payload.get("affected_sessions"), dict)
        else {}
    )
    configuration = (
        payload.get("affected_configuration")
        if isinstance(payload.get("affected_configuration"), dict)
        else {}
    )
    lines = [
        "# LoopX State Restore",
        "",
        f"- OK: `{payload.get('ok')}`",
        f"- Dry run: `{payload.get('dry_run')}`",
        f"- Archive: `{payload.get('archive_path')}`",
        f"- Backup id: `{payload.get('backup_id')}`",
        f"- Project: `{payload.get('project')}`",
        f"- Runtime root: `{payload.get('runtime_root')}`",
        f"- Codex home: `{payload.get('codex_home')}`",
        f"- Categories: `{', '.join(payload.get('categories') or [])}`",
        f"- Integrity level: `{payload.get('integrity_level')}`",
        f"- Paths to add: `{summary.get('add_count')}`",
        f"- Paths to overwrite: `{summary.get('overwrite_count')}`",
        f"- Conflicts: `{summary.get('conflict_count')}`",
        f"- Unchanged: `{summary.get('unchanged_count')}`",
        f"- Recommended action: {payload.get('recommended_action')}",
    ]
    lines.extend(_render_path_section("Would Add", payload.get("to_add") or []))
    lines.extend(_render_path_section("Would Overwrite", payload.get("to_overwrite") or []))
    lines.extend(_render_path_section("Conflicts", payload.get("conflicts") or []))
    if any(sessions.values()):
        lines.extend(["", "## Affected Sessions", ""])
        for label, values in sessions.items():
            if values:
                lines.append(f"- {label}: {', '.join(f'`{v}`' for v in values)}")
    if any(value for value in configuration.values()):
        lines.extend(["", "## Affected Configuration", ""])
        for label, values in configuration.items():
            if values:
                rendered = values if isinstance(values, str) else ", ".join(
                    f"`{v}`" for v in values
                )
                lines.append(f"- {label}: {rendered}")
    restore_state = payload.get("restore_state")
    if restore_state is not None:
        lines.extend(
            [
                "",
                "## Execution",
                "",
                f"- Restore state: `{restore_state}`",
                f"- Restored paths: `{payload.get('restored_count')}`",
                f"- Rollback snapshot: `{payload.get('rollback_snapshot')}`",
                f"- Journal: `{payload.get('journal_path')}`",
            ]
        )
    return "\n".join(lines) + "\n"
