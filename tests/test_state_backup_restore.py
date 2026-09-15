from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

import loopx
from loopx.state_backup import (
    build_state_backup_plan,
    execute_state_backup_plan,
)
from loopx.state_restore import (
    _load_journal,
    _selection_token,
    apply_restore,
    build_state_restore_plan,
    execute_state_restore,
    prepare_restore,
    verify_state_archive,
)

REPO_ROOT = Path(loopx.__file__).resolve().parents[1]


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False) + "\n")


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    home = tmp_path / "home"
    codex = home / ".codex"
    runtime = codex / "loopx"
    project = tmp_path / "project"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex))

    _write_json(
        project / ".loopx" / "registry.json",
        {"schema_version": "0.1", "goals": [{"id": "goal-a"}]},
    )
    _write_text(project / ".codex/goals/goal-a/ACTIVE_GOAL_STATE.md", "# codex goal v1\n")
    _write_text(project / ".claude/goals/goal-a/ACTIVE_GOAL_STATE.md", "# claude goal v1\n")
    _write_text(project / ".local/goals/goal-a/ACTIVE_GOAL_STATE.md", "# local goal v1\n")
    _write_json(codex / "automations" / "auto-a.json", {"automation_id": "auto-a"})
    _write_text(codex / "skills/loopx-fixture/SKILL.md", "# fixture skill\n")
    _write_json(runtime / "registry.global.json", {"schema_version": "0.1", "goals": []})
    _write_text(runtime / "state.txt", "runtime state v1\n")
    return SimpleNamespace(
        tmp=tmp_path,
        home=home,
        codex=codex,
        runtime=runtime,
        project=project,
        goal_state=project / ".codex/goals/goal-a/ACTIVE_GOAL_STATE.md",
        registry=project / ".loopx/registry.json",
        automation=codex / "automations/auto-a.json",
        skill=codex / "skills/loopx-fixture/SKILL.md",
        runtime_state=runtime / "state.txt",
    )


def _backup(ws: SimpleNamespace, backup_id: str = "fixture") -> dict[str, object]:
    plan = build_state_backup_plan(
        project=ws.project,
        runtime_root=ws.runtime,
        backup_id=backup_id,
        include_registry_projects=False,
    )
    assert plan["ok"] is True
    return execute_state_backup_plan(plan)


def _job_dir(plan: dict[str, object]) -> Path:
    token = _selection_token(list(plan["categories"]))  # type: ignore[arg-type]
    return Path(str(plan["jobs_dir"])) / f"{plan['backup_id']}-{token}"


def _rewrite_archive(
    source: Path,
    destination: Path,
    *,
    drop: tuple[str, ...] = (),
    replace: dict[str, bytes] | None = None,
    extra: tuple[tuple[str, bytes], ...] = (),
    truncate: tuple[str, ...] = (),
) -> Path:
    replace = replace or {}
    with tarfile.open(source, "r:gz") as src:
        items: list[tuple[tarfile.TarInfo, bytes | None]] = []
        for member in src.getmembers():
            if member.name in drop:
                continue
            handle = src.extractfile(member)
            content = handle.read() if handle is not None else None
            if member.name in replace and content is not None:
                content = replace[member.name]
            if member.name in truncate and content is not None:
                content = content[:-2]
            items.append((member, content))
    with tarfile.open(destination, "w:gz") as dst:
        for member, content in items:
            if content is not None and member.isfile():
                member.size = len(content)
                dst.addfile(member, io.BytesIO(content))
            else:
                dst.addfile(member)
        for name, content in extra:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            dst.addfile(info, io.BytesIO(content))
    return destination


def _legacy_archive(payload: dict[str, object]) -> tuple[Path, Path]:
    archive = Path(str(payload["archive_path"]))
    legacy_archive = archive.with_name("loopx-state-legacy.tar.gz")
    legacy_sidecar = archive.with_name("loopx-state-legacy.manifest.json")
    embedded: dict[str, object] = {}
    with tarfile.open(archive, "r:gz") as src:
        items: list[tuple[tarfile.TarInfo, bytes | None]] = []
        for member in src.getmembers():
            handle = src.extractfile(member)
            content = handle.read() if handle is not None else None
            if member.name == "manifest.json" and content is not None:
                embedded = json.loads(content.decode("utf-8"))
                embedded.pop("file_index", None)
                content = json.dumps(embedded, ensure_ascii=False, indent=2).encode("utf-8")
                member.size = len(content)
            items.append((member, content))
    with tarfile.open(legacy_archive, "w:gz") as dst:
        for member, content in items:
            if content is not None and member.isfile():
                member.size = len(content)
                dst.addfile(member, io.BytesIO(content))
            else:
                dst.addfile(member)
    sidecar = json.loads(Path(str(payload["manifest_path"])).read_text(encoding="utf-8"))
    sidecar.pop("file_index", None)
    sidecar["execution"]["archive_sha256"] = hashlib.sha256(
        legacy_archive.read_bytes()
    ).hexdigest()
    sidecar["execution"]["archive_size_bytes"] = legacy_archive.stat().st_size
    legacy_sidecar.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return legacy_archive, legacy_sidecar


def _plan(
    archive: Path,
    ws: SimpleNamespace,
    *,
    categories: list[str] | None = None,
) -> dict[str, object]:
    return build_state_restore_plan(
        archive,
        project=ws.project,
        runtime_root=ws.runtime,
        categories=categories,
    )


# ---------------------------------------------------------------------------
# Backup index and intact verification
# ---------------------------------------------------------------------------


def test_backup_file_index_is_additive(workspace: SimpleNamespace) -> None:
    dry_run = build_state_backup_plan(
        project=workspace.project,
        runtime_root=workspace.runtime,
        backup_id="plan-only",
        include_registry_projects=False,
    )
    assert "file_index" not in dry_run

    payload = _backup(workspace)
    file_index = payload["file_index"]
    assert isinstance(file_index, list) and file_index
    records = {item["name"]: item for item in file_index}
    goal = records["project/.codex/goals/goal-a/ACTIVE_GOAL_STATE.md"]
    assert goal["kind"] == "file"
    assert goal["size"] == len("# codex goal v1\n")
    assert goal["category"] == "project_state"
    assert goal["sha256"] and len(goal["sha256"]) == 64
    dir_record = records["project/.codex/goals"]
    assert dir_record["kind"] == "dir"
    assert dir_record["size"] == 0

    archive = Path(str(payload["archive_path"]))
    with tarfile.open(archive, "r:gz") as tar:
        embedded = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))
        goal_member = tar.extractfile(goal["name"])
        goal_sha = hashlib.sha256(goal_member.read()).hexdigest()
    assert embedded["file_index"] == file_index
    assert goal_sha == goal["sha256"]
    sidecar = json.loads(Path(str(payload["manifest_path"])).read_text("utf-8"))
    assert sidecar["file_index"] == file_index


def test_verify_intact_archive(workspace: SimpleNamespace) -> None:
    payload = _backup(workspace)
    result = verify_state_archive(payload["archive_path"])
    assert result["ok"] is True
    assert result["checks"]["archive_readable"] is True
    assert result["checks"]["embedded_manifest_present"] is True
    assert result["checks"]["sidecar_manifest"] == "present"
    assert result["checks"]["archive_sha256_match"] is True
    assert result["checks"]["embedded_sidecar_consistent"] is True
    assert result["checks"]["per_file_digests"] is True
    assert result["summary"]["integrity_level"] == "existence_size_digest"
    assert result["summary"]["problem_count"] == 0


def test_verify_archive_missing_file(tmp_path: Path) -> None:
    result = verify_state_archive(tmp_path / "does-not-exist.tar.gz")
    assert result["ok"] is False
    assert any(p["reason"] == "archive_missing" for p in result["problems"])


@pytest.mark.parametrize(
    "tamper, expected_reason",
    [
        ("modify", "digest_mismatch"),
        ("truncate", "truncated_or_resized"),
        ("drop", "missing"),
        ("extra", "unexpected_member"),
        ("gzip", "archive_unreadable"),
    ],
)
def test_verify_detects_corruption(
    workspace: SimpleNamespace, tmp_path: Path, tamper: str, expected_reason: str
) -> None:
    payload = _backup(workspace, backup_id=f"corrupt-{tamper}")
    source = Path(str(payload["archive_path"]))
    target = tmp_path / f"{tamper}.tar.gz"
    if tamper == "modify":
        _rewrite_archive(
            source,
            target,
            replace={
                "project/.codex/goals/goal-a/ACTIVE_GOAL_STATE.md": b"# codex goal X1\n"
            },
        )
    elif tamper == "truncate":
        _rewrite_archive(source, target, truncate=("runtime-root/state.txt",))
    elif tamper == "drop":
        _rewrite_archive(
            source,
            target,
            drop=("project/.codex/goals/goal-a/ACTIVE_GOAL_STATE.md",),
        )
    elif tamper == "extra":
        _rewrite_archive(source, target, extra=(("rogue.txt", b"not expected"),))
    else:
        target.write_bytes(source.read_bytes()[:-40])
    result = verify_state_archive(target)
    assert result["ok"] is False
    assert any(p["reason"] == expected_reason for p in result["problems"]), result["problems"]


def test_verify_cli_exit_codes(
    workspace: SimpleNamespace, tmp_path: Path
) -> None:
    payload = _backup(workspace, backup_id="cli-verify")
    intact = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "--format",
            "json",
            "verify-state",
            "--archive",
            str(payload["archive_path"]),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    assert intact.returncode == 0, intact.stderr
    assert json.loads(intact.stdout)["ok"] is True

    bad = tmp_path / "bad.tar.gz"
    _rewrite_archive(
        Path(str(payload["archive_path"])),
        bad,
        replace={"runtime-root/state.txt": b"runtime state hacked\n"},
    )
    corrupted = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "--format",
            "json",
            "verify-state",
            "--archive",
            str(bad),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    assert corrupted.returncode == 1, corrupted.stdout
    assert json.loads(corrupted.stdout)["ok"] is False


def test_restore_cli_category_selection(workspace: SimpleNamespace) -> None:
    payload = _backup(workspace, backup_id="cli-category")
    workspace.goal_state.write_text("# codex goal MUTATED\n")
    _write_json(workspace.automation, {"automation_id": "MUTATED"})
    archive = str(payload["archive_path"])

    selected = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "--format",
            "json",
            "--runtime-root",
            str(workspace.runtime),
            "restore-state",
            "--archive",
            archive,
            "--project",
            str(workspace.project),
            "--category",
            "automations",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    assert selected.returncode == 0, selected.stderr
    plan = json.loads(selected.stdout)
    assert plan["categories"] == ["automations"]
    assert plan["execute_requested"] is False
    assert not Path(str(plan["jobs_dir"])).exists()

    invalid = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "--format",
            "json",
            "--runtime-root",
            str(workspace.runtime),
            "restore-state",
            "--archive",
            archive,
            "--category",
            "nope,automations",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    assert invalid.returncode == 1
    assert "unknown categories" in json.loads(invalid.stdout)["error"]


# ---------------------------------------------------------------------------
# Restore dry run
# ---------------------------------------------------------------------------


def test_restore_dry_run_diff_is_read_only(workspace: SimpleNamespace) -> None:
    payload = _backup(workspace, backup_id="dryrun")
    archive = Path(str(payload["archive_path"]))

    workspace.goal_state.write_text("# codex goal CORRUPTED\n")
    workspace.automation.unlink()
    workspace.skill.unlink()
    workspace.skill.mkdir(parents=True)
    (workspace.skill / "unrelated.txt").write_text("skill replaced by directory\n")
    (workspace.project / ".codex/goals/stranger.txt").write_text("outside archive\n")

    plan = _plan(archive, workspace)
    add_dests = {Path(e["dest"]).name for e in plan["to_add"]}
    overwrite_dests = {Path(e["dest"]).name for e in plan["to_overwrite"]}
    conflict_dests = {Path(e["dest"]).name for e in plan["conflicts"]}
    assert "auto-a.json" in add_dests
    assert "ACTIVE_GOAL_STATE.md" in overwrite_dests
    assert "SKILL.md" in conflict_dests
    assert all(
        e["reason"] in ("missing", "content_changed")
        for e in [*plan["to_add"], *plan["to_overwrite"]]
    )

    sessions = plan["affected_sessions"]
    assert sessions["project_codex_goal_ids"] == ["goal-a"]
    configuration = plan["affected_configuration"]
    assert "auto-a" in configuration["automation_ids"]
    assert "loopx-fixture" in configuration["skill_names"]

    # Dry run writes nothing: no job state, outside-archive file stays, current
    # mutated state stays byte-identical.
    assert not Path(str(plan["jobs_dir"])).exists()
    assert (workspace.project / ".codex/goals/stranger.txt").read_text() == "outside archive\n"
    assert workspace.goal_state.read_text() == "# codex goal CORRUPTED\n"
    assert not workspace.automation.exists()


def test_restore_conflicts_block_execution(workspace: SimpleNamespace) -> None:
    payload = _backup(workspace, backup_id="conflict")
    workspace.skill.unlink()
    workspace.skill.mkdir()
    plan = _plan(Path(str(payload["archive_path"])), workspace)
    assert plan["conflicts"]
    with pytest.raises(ValueError, match="conflicting paths"):
        execute_state_restore(plan)


def test_restore_rejects_unknown_category(workspace: SimpleNamespace) -> None:
    payload = _backup(workspace, backup_id="badcat")
    with pytest.raises(ValueError, match="unknown categories"):
        _plan(Path(str(payload["archive_path"])), workspace, categories=["nope"])


# ---------------------------------------------------------------------------
# Atomic restore
# ---------------------------------------------------------------------------


def _mutate_state(workspace: SimpleNamespace) -> None:
    workspace.goal_state.write_text("# codex goal MUTATED\n")
    workspace.automation.unlink()
    workspace.runtime_state.write_text("runtime state MUTATED\n")
    _write_json(workspace.registry, {"schema_version": "0.1", "goals": []})


def _assert_backed_up_state(workspace: SimpleNamespace) -> None:
    assert workspace.goal_state.read_text() == "# codex goal v1\n"
    assert json.loads(workspace.automation.read_text()) == {"automation_id": "auto-a"}
    assert workspace.runtime_state.read_text() == "runtime state v1\n"
    assert json.loads(workspace.registry.read_text())["goals"] == [{"id": "goal-a"}]


def test_atomic_restore_success_is_idempotent(workspace: SimpleNamespace) -> None:
    payload = _backup(workspace, backup_id="restore")
    archive = Path(str(payload["archive_path"]))
    _mutate_state(workspace)

    plan = _plan(archive, workspace)
    assert plan["to_add"] and plan["to_overwrite"]
    result = execute_state_restore(plan)
    assert result["ok"] is True
    assert result["restore_state"] == "completed"
    _assert_backed_up_state(workspace)
    job_dir = _job_dir(plan)
    assert (job_dir / "rollback.tar").exists()
    assert _load_journal(job_dir)["phase"] == "completed"

    # Re-running completes without changing anything and replans to a no-op.
    again = execute_state_restore(_plan(archive, workspace))
    assert again["restore_state"] == "completed"
    replan = _plan(archive, workspace)
    assert replan["summary"]["add_count"] == 0
    assert replan["summary"]["overwrite_count"] == 0
    assert replan["summary"]["conflict_count"] == 0
    _assert_backed_up_state(workspace)


def test_interrupted_restore_can_continue(workspace: SimpleNamespace) -> None:
    payload = _backup(workspace, backup_id="resume")
    archive = Path(str(payload["archive_path"]))
    _mutate_state(workspace)

    plan = _plan(archive, workspace)
    job_dir = _job_dir(plan)
    journal = prepare_restore(plan, job_dir)
    journal = apply_restore(plan, job_dir, journal, max_ops=1)
    assert journal["phase"] == "applying"
    assert journal["applied_count"] == 1

    resumed = execute_state_restore(_plan(archive, workspace))
    assert resumed["ok"] is True
    assert resumed["restore_state"] == "completed"
    _assert_backed_up_state(workspace)
    assert _load_journal(job_dir)["phase"] == "completed"


def test_interrupted_restore_can_abort(workspace: SimpleNamespace) -> None:
    payload = _backup(workspace, backup_id="abort")
    archive = Path(str(payload["archive_path"]))
    _mutate_state(workspace)

    plan = _plan(archive, workspace)
    job_dir = _job_dir(plan)
    journal = prepare_restore(plan, job_dir)
    journal = apply_restore(plan, job_dir, journal, max_ops=2)
    assert journal["phase"] == "applying"

    aborted = execute_state_restore(_plan(archive, workspace), abort=True)
    assert aborted["ok"] is True
    assert aborted["restore_state"] == "aborted"
    # Original (mutated) state is fully preserved: overwritten files were
    # rolled back from the snapshot, added files were removed.
    assert workspace.goal_state.read_text() == "# codex goal MUTATED\n"
    assert not workspace.automation.exists()
    assert workspace.runtime_state.read_text() == "runtime state MUTATED\n"
    assert not list(workspace.runtime.rglob(".*.loopx-*.tmp-*"))

    # After giving up, a fresh execute can start over and succeed.
    result = execute_state_restore(_plan(archive, workspace))
    assert result["restore_state"] == "completed"
    _assert_backed_up_state(workspace)


def test_apply_failure_rolls_back_automatically(
    workspace: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import loopx.state_restore as state_restore

    payload = _backup(workspace, backup_id="autofail")
    archive = Path(str(payload["archive_path"]))
    _mutate_state(workspace)

    plan = _plan(archive, workspace)
    real_apply_one = state_restore._apply_one
    file_calls = {"count": 0}

    def flaky_apply_one(op: dict[str, object], job_dir: Path, journal: object) -> None:
        if op["kind"] == "file":
            file_calls["count"] += 1
            if file_calls["count"] == 2:
                raise RuntimeError("simulated write failure")
        real_apply_one(op, job_dir, journal)

    monkeypatch.setattr(state_restore, "_apply_one", flaky_apply_one)
    result = execute_state_restore(plan)
    assert result["ok"] is False
    assert result["restore_state"] == "rolled_back"
    assert _load_journal(_job_dir(plan))["phase"] == "rolled_back"

    # The tree must be exactly as it was before execution started.
    assert workspace.goal_state.read_text() == "# codex goal MUTATED\n"
    assert not workspace.automation.exists()
    assert workspace.runtime_state.read_text() == "runtime state MUTATED\n"
    assert json.loads(workspace.registry.read_text())["goals"] == []
    assert not list(workspace.tmp.rglob(".*.loopx-*.tmp-*"))


# ---------------------------------------------------------------------------
# Category selection and legacy archives
# ---------------------------------------------------------------------------


def test_category_selection_leaves_other_categories_untouched(
    workspace: SimpleNamespace,
) -> None:
    payload = _backup(workspace, backup_id="category")
    archive = Path(str(payload["archive_path"]))
    workspace.goal_state.write_text("# codex goal MUTATED\n")
    _write_json(workspace.automation, {"automation_id": "MUTATED"})
    workspace.skill.write_text("# skill MUTATED\n")

    plan = _plan(archive, workspace, categories=["automations"])
    assert {e["category"] for e in plan["to_overwrite"]} == {"automations"}
    executed = execute_state_restore(plan)
    assert executed["restore_state"] == "completed"
    assert json.loads(workspace.automation.read_text()) == {"automation_id": "auto-a"}
    assert workspace.goal_state.read_text() == "# codex goal MUTATED\n"
    assert workspace.skill.read_text() == "# skill MUTATED\n"

    skills_plan = _plan(archive, workspace, categories=["skills"])
    execute_state_restore(skills_plan)
    assert workspace.skill.read_text() == "# fixture skill\n"
    assert workspace.goal_state.read_text() == "# codex goal MUTATED\n"


def test_restore_plan_with_registry_projects(workspace: SimpleNamespace) -> None:
    remote = workspace.tmp / "remote-project"
    _write_json(
        remote / ".loopx/registry.json",
        {"schema_version": "0.1", "goals": [{"id": "goal-r"}]},
    )
    _write_text(remote / ".codex/goals/goal-r/ACTIVE_GOAL_STATE.md", "# remote v1\n")
    _write_json(
        workspace.runtime / "registry.global.json",
        {
            "schema_version": "0.1",
            "goals": [
                {
                    "id": "goal-r",
                    "repo": str(remote),
                    "state_file": ".codex/goals/goal-r/ACTIVE_GOAL_STATE.md",
                    "source_registry": str(remote / ".loopx/registry.json"),
                }
            ],
        },
    )
    plan0 = build_state_backup_plan(
        project=workspace.project,
        runtime_root=workspace.runtime,
        backup_id="with-registry",
    )
    payload = execute_state_backup_plan(plan0)
    archive = Path(str(payload["archive_path"]))

    remote_state = remote / ".codex/goals/goal-r/ACTIVE_GOAL_STATE.md"
    remote_state.write_text("# remote CORRUPTED\n")
    restore_plan = _plan(archive, workspace)
    registry_changes = [
        e
        for e in restore_plan["to_overwrite"]
        if str(e["target_key"]).startswith("registry_")
    ]
    assert registry_changes
    sessions = restore_plan["affected_sessions"]
    assert "goal-r" in sessions["registry_goal_ids"]
    assert str(remote) in sessions["registry_project_paths"]

    result = execute_state_restore(restore_plan)
    assert result["ok"] is True
    assert remote_state.read_text() == "# remote v1\n"


def test_legacy_archive_without_file_index(workspace: SimpleNamespace) -> None:
    payload = _backup(workspace, backup_id="old-format")
    legacy_archive, _ = _legacy_archive(payload)

    verified = verify_state_archive(legacy_archive)
    assert verified["ok"] is True
    assert verified["checks"]["per_file_digests"] is False
    assert verified["summary"]["integrity_level"] == "existence_size"

    _mutate_state(workspace)
    plan = _plan(legacy_archive, workspace)
    assert plan["legacy_archive"] is True
    assert plan["integrity_level"] == "existence_size"
    assert plan["to_overwrite"]
    assert all(e["reason"] == "unverified_legacy" for e in plan["to_overwrite"])
    assert plan["to_add"]

    result = execute_state_restore(plan)
    assert result["ok"] is True
    _assert_backed_up_state(workspace)

    assert verify_state_archive(legacy_archive)["ok"] is True
