from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from aria.errors import WorkflowError
from aria.events_time import utc_now
from aria.io import atomic_write_bytes, exclusive_lock


LESSON_KINDS = {"error", "correction", "successful_pattern"}
LESSON_SCOPES = {"global", "project"}


def _canonical_sha(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zа-яё0-9_]{4,}", value.lower())
        if token not in {"этот", "этого", "задача", "project", "task"}
    }


class LessonStore:
    """Append-only useful behavior memory without scores or milestone events."""

    def __init__(self, framework_root: Path, runtime_root: Path) -> None:
        del framework_root  # Kept in the signature for source compatibility.
        machine_runtime = (
            runtime_root.parent.parent
            if runtime_root.parent.name == "projects"
            else runtime_root
        )
        self.path = machine_runtime / "behavior" / "lessons.jsonl"
        self.lock_path = machine_runtime / "locks" / "lessons.lock"

    def verify(self) -> dict[str, object]:
        if not self.path.is_file():
            return {
                "ok": True,
                "path": str(self.path),
                "events": 0,
                "head_sha256": None,
            }
        previous: str | None = None
        events = 0
        try:
            for line_number, raw in enumerate(
                self.path.read_text(encoding="utf-8-sig").splitlines(), start=1
            ):
                if not raw.strip():
                    continue
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise WorkflowError(f"Lesson line {line_number} is not an object")
                if event.get("schema_version") != 1:
                    raise WorkflowError(f"Lesson line {line_number} schema mismatch")
                if event.get("sequence") != events + 1:
                    raise WorkflowError(f"Lesson line {line_number} sequence mismatch")
                if event.get("previous_event_sha256") != previous:
                    raise WorkflowError(f"Lesson line {line_number} chain mismatch")
                actual = event.get("event_sha256")
                unsigned = {
                    key: value for key, value in event.items() if key != "event_sha256"
                }
                expected = _canonical_sha(unsigned)
                if actual != expected:
                    raise WorkflowError(f"Lesson line {line_number} SHA mismatch")
                previous = str(actual)
                events += 1
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            WorkflowError,
        ) as error:
            return {
                "ok": False,
                "path": str(self.path),
                "events": events,
                "head_sha256": previous,
                "error": str(error),
            }
        return {
            "ok": True,
            "path": str(self.path),
            "events": events,
            "head_sha256": previous,
        }

    def events(self) -> list[dict[str, object]]:
        verified = self.verify()
        if verified.get("ok") is not True:
            raise WorkflowError(f"Lesson chain is invalid: {verified.get('error')}")
        if not self.path.is_file():
            return []
        return [
            json.loads(raw)
            for raw in self.path.read_text(encoding="utf-8-sig").splitlines()
            if raw.strip()
        ]

    def snapshot(
        self, *, project_id: str, task: str, limit: int = 12
    ) -> dict[str, object]:
        events = self.events()
        resolved = {
            str(event.get("resolves_lesson_id"))
            for event in events
            if event.get("type") == "lesson_resolved"
        }
        task_tokens = _tokens(task)
        active: list[tuple[int, int, dict[str, object]]] = []
        for event in events:
            if event.get("type") != "lesson_recorded":
                continue
            lesson_id = str(event.get("lesson_id"))
            if lesson_id in resolved:
                continue
            scope = event.get("scope")
            if scope == "project" and event.get("project_id") != project_id:
                continue
            relevance_text = " ".join(
                str(event.get(key, ""))
                for key in ("trigger", "finding", "countermeasure")
            )
            overlap = len(task_tokens & _tokens(relevance_text))
            active.append((overlap, int(event.get("sequence", 0)), event))
        active.sort(key=lambda row: (row[0], row[1]), reverse=True)
        selected = [
            {
                key: event.get(key)
                for key in (
                    "lesson_id",
                    "kind",
                    "scope",
                    "project_id",
                    "trigger",
                    "finding",
                    "countermeasure",
                    "evidence",
                )
            }
            for _overlap, _sequence, event in active[:limit]
        ]
        payload: dict[str, object] = {
            "event_count": len(events),
            "active_count": len(active),
            "selected": selected,
            "head_sha256": self.verify().get("head_sha256"),
        }
        payload["sha256"] = _canonical_sha(payload)
        return payload

    def append(
        self,
        *,
        project_id: str,
        run_id: str,
        lessons: list[dict[str, object]],
        resolutions: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        if not lessons and not resolutions:
            return []
        with exclusive_lock(self.lock_path):
            events = self.events()
            existing_keys = {
                (event.get("run_id"), event.get("lesson_key"))
                for event in events
                if event.get("type") == "lesson_recorded"
            }
            existing_ids = {
                str(event.get("lesson_id"))
                for event in events
                if event.get("type") == "lesson_recorded"
            }
            resolved_ids = {
                str(event.get("resolves_lesson_id"))
                for event in events
                if event.get("type") == "lesson_resolved"
            }
            appended: list[dict[str, object]] = []
            for lesson in lessons:
                kind = lesson.get("kind")
                scope = lesson.get("scope", "project")
                trigger = lesson.get("trigger")
                finding = lesson.get("finding")
                countermeasure = lesson.get("countermeasure")
                evidence = lesson.get("evidence")
                if kind not in LESSON_KINDS:
                    raise WorkflowError(
                        f"Lesson kind must be one of {sorted(LESSON_KINDS)}"
                    )
                if scope not in LESSON_SCOPES:
                    raise WorkflowError(
                        f"Lesson scope must be one of {sorted(LESSON_SCOPES)}"
                    )
                if not all(
                    isinstance(value, str) and value.strip()
                    for value in (trigger, finding, countermeasure, evidence)
                ):
                    raise WorkflowError(
                        "Lesson requires trigger, finding, countermeasure and evidence"
                    )
                lesson_key = _canonical_sha(
                    [kind, scope, project_id if scope == "project" else None, finding]
                )
                if (run_id, lesson_key) in existing_keys:
                    continue
                event = {
                    "schema_version": 1,
                    "sequence": len(events) + len(appended) + 1,
                    "timestamp": utc_now(),
                    "type": "lesson_recorded",
                    "lesson_id": uuid.uuid4().hex,
                    "lesson_key": lesson_key,
                    "kind": kind,
                    "scope": scope,
                    "project_id": project_id if scope == "project" else None,
                    "run_id": run_id,
                    "trigger": trigger.strip(),
                    "finding": finding.strip(),
                    "countermeasure": countermeasure.strip(),
                    "evidence": evidence.strip(),
                    "previous_event_sha256": (
                        appended[-1]["event_sha256"]
                        if appended
                        else events[-1]["event_sha256"]
                        if events
                        else None
                    ),
                }
                event["event_sha256"] = _canonical_sha(event)
                appended.append(event)
                existing_keys.add((run_id, lesson_key))
                existing_ids.add(str(event["lesson_id"]))
            for resolution in resolutions:
                lesson_id = resolution.get("lesson_id")
                evidence = resolution.get("evidence")
                if not isinstance(lesson_id, str) or lesson_id not in existing_ids:
                    raise WorkflowError(
                        f"Lesson resolution target does not exist: {lesson_id}"
                    )
                if lesson_id in resolved_ids:
                    continue
                if not isinstance(evidence, str) or not evidence.strip():
                    raise WorkflowError("Lesson resolution requires evidence")
                event = {
                    "schema_version": 1,
                    "sequence": len(events) + len(appended) + 1,
                    "timestamp": utc_now(),
                    "type": "lesson_resolved",
                    "lesson_id": uuid.uuid4().hex,
                    "resolves_lesson_id": lesson_id,
                    "project_id": project_id,
                    "run_id": run_id,
                    "evidence": evidence.strip(),
                    "previous_event_sha256": (
                        appended[-1]["event_sha256"]
                        if appended
                        else events[-1]["event_sha256"]
                        if events
                        else None
                    ),
                }
                event["event_sha256"] = _canonical_sha(event)
                appended.append(event)
                resolved_ids.add(lesson_id)
            if not appended:
                return []
            all_events = [*events, *appended]
            content = "".join(
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                for event in all_events
            ).encode("utf-8")
            atomic_write_bytes(self.path, content)
            verified = self.verify()
            if verified.get("ok") is not True or verified.get("events") != len(
                all_events
            ):
                raise WorkflowError("Lesson memory read-back failed")
            return appended
