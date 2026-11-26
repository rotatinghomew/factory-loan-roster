from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import (
    ConflictError,
    GatewayFailure,
    LaborError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from .gateway import Gateway, LocalGateway
from .timeutil import (
    PARK_TZ,
    format_minute,
    intersect_intervals,
    merge_intervals,
    overlap_minutes,
    parse_minute,
    round_cents_half_up,
    subtract_intervals,
)

SOURCE_GROUPS = (
    "people",
    "availability",
    "shifts",
    "terms",
    "consents",
    "recalls",
    "attendance",
    "pay_ack",
)


class Service:
    """In-memory shared-labor service with optional atomic JSON persistence."""

    def __init__(
        self,
        data: dict[str, list[dict[str, Any]]] | None = None,
        *,
        gateway: Gateway | None = None,
        clock: str | int | None = None,
        persist_path: str | Path | None = None,
    ):
        self.lock = threading.RLock()
        self.gateway = gateway or LocalGateway()
        self.persist_path = Path(persist_path) if persist_path else None
        self.db: dict[str, list[dict[str, Any]]] = {name: [] for name in SOURCE_GROUPS}
        self.db.update(
            {
                "factories": [],
                "consent_requests": [],
                "outbox": [],
                "disputes": [],
                "liabilities": [],
                "shortfalls": [],
                "audit": [],
            }
        )
        self._seq: dict[str, int] = defaultdict(int)
        self.clock = parse_minute(clock, "clock") if clock is not None else None
        if isinstance(data, (str, Path)):
            data = json.loads(Path(data).read_text(encoding="utf-8"))
        if data:
            self.load_exchange(data)
        self._ensure_factories()
        self.sync_disputes()
        self.reconcile_terminal_recalls(create_liabilities=True)

    # ------------------------------------------------------------------ core
    def load_exchange(self, data: dict[str, list[dict[str, Any]]]) -> None:
        for group in SOURCE_GROUPS:
            rows = data.get(group, []) or []
            for index, row in enumerate(rows):
                item = dict(row)
                item.setdefault("_seq", index)
                self.db[group].append(item)
                self._remember_id(item.get("id"))
        for group in ("factories", "consent_requests", "outbox", "disputes", "liabilities", "shortfalls", "audit"):
            for item in data.get(group, []) or []:
                self.db[group].append(dict(item))
                self._remember_id(item.get("id"))

    def _remember_id(self, value: Any) -> None:
        if not isinstance(value, str):
            return
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits:
            # IDs such as T2 and DIFF-W1 both contribute numbers; collisions
            # are harmless because next_id scans the target collection too.
            prefix = "".join(ch for ch in value if not ch.isdigit()).rstrip("-_")
            try:
                self._seq[prefix] = max(self._seq[prefix], int(digits))
            except ValueError:
                pass

    def next_id(self, prefix: str, collection: str | None = None) -> str:
        with self.lock:
            n = self._seq[prefix] + 1
            self._seq[prefix] = n
            if collection:
                existing = {row.get("id") for row in self.db.get(collection, [])}
                while f"{prefix}{n}" in existing:
                    n += 1
                    self._seq[prefix] = n
            return f"{prefix}{n}"

    def save(self) -> None:
        if not self.persist_path:
            return
        path = self.persist_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "Service":
        p = Path(path)
        seed_path = kwargs.pop("seed_path", None)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls(data, persist_path=p, **kwargs)
        seed_data = json.loads(Path(seed_path).read_text(encoding="utf-8")) if seed_path else None
        return cls(seed_data, persist_path=p, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.db, ensure_ascii=False, default=str))

    def _audit(self, action: str, **details) -> None:
        self.db["audit"].append(
            {"id": self.next_id("AUD", "audit"), "action": action, "at": self.now_label(), **details}
        )

    def now_minute(self, at: str | int | None = None) -> int:
        if at is not None:
            return parse_minute(at, "at")
        if self.clock is not None:
            return self.clock
        return datetime.now(PARK_TZ).hour * 60 + datetime.now(PARK_TZ).minute

    def now_label(self, at: str | int | None = None) -> str:
        return format_minute(self.now_minute(at))

    def set_clock(self, at: str | int) -> None:
        self.clock = parse_minute(at, "clock")

    @staticmethod
    def _public(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {k: v for k, v in row.items() if not str(k).startswith("_")}

    def _get(self, collection: str, entity_id: str) -> dict[str, Any]:
        for row in self.db[collection]:
            if row.get("id") == entity_id:
                return row
        raise NotFoundError(f"{collection[:-1].title()} {entity_id} was not found", id=entity_id)

    def person(self, person_id: str) -> dict[str, Any]:
        return self._get("people", person_id)

    def shift(self, shift_id: str) -> dict[str, Any]:
        return self._get("shifts", shift_id)

    def term(self, terms_id: str) -> dict[str, Any]:
        return self._get("terms", terms_id)

    # ------------------------------------------------------------------ auth
    @staticmethod
    def viewer(viewer: dict[str, Any] | str | None) -> dict[str, Any]:
        if viewer is None:
            return {"type": "admin", "id": None}
        if isinstance(viewer, str):
            return {"type": "factory", "id": viewer}
        return viewer

    def require_factory(self, factory_id: str, viewer: dict[str, Any] | str | None) -> dict[str, Any]:
        v = self.viewer(viewer)
        if v.get("type") in ("admin", "system"):
            return v
        if v.get("type") == "factory" and v.get("id") == factory_id:
            return v
        raise PermissionDeniedError("This factory may only query or change its own records", factory=factory_id)

    def require_person(self, person_id: str, viewer: dict[str, Any] | str | None) -> dict[str, Any]:
        v = self.viewer(viewer)
        if v.get("type") in ("admin", "system"):
            return v
        if v.get("type") == "person" and v.get("id") == person_id:
            return v
        raise PermissionDeniedError("A worker may only access their own record", person=person_id)

    # ------------------------------------------------------------- factories
    def _ensure_factories(self) -> None:
        known = {f["id"] for f in self.db["factories"]}
        ids: set[str] = set()
        for p in self.db["people"]:
            ids.add(p.get("origin"))
        for s in self.db["shifts"]:
            ids.add(s.get("host"))
        for a in self.db["availability"]:
            ids.add(a.get("factory"))
        for a in self.db["attendance"]:
            ids.add(a.get("reporter"))
        for m in self.db["pay_ack"]:
            ids.add(m.get("reporter"))
        ids.discard(None)
        for factory_id in sorted(ids):
            if factory_id not in known:
                kind = "origin" if factory_id == "OR" else "host"
                self.db["factories"].append(
                    {"id": factory_id, "name": f"{factory_id} 工厂", "role": kind}
                )

    def list_factories(self, viewer=None) -> list[dict[str, Any]]:
        v = self.viewer(viewer)
        if v.get("type") == "factory":
            return [f for f in self.db["factories"] if f["id"] == v["id"]]
        return [self._public(f) for f in self.db["factories"]]

    def add_factory(self, payload: dict[str, Any]) -> dict[str, Any]:
        factory_id = payload["id"]
        if any(f["id"] == factory_id for f in self.db["factories"]):
            raise ConflictError(f"Factory {factory_id} already exists")
        row = {"id": factory_id, "name": payload.get("name", factory_id), "role": payload.get("role", "both")}
        self.db["factories"].append(row)
        self.save()
        return row

    # ------------------------------------------------------------ people etc
    def add_person(self, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        origin = payload.get("origin") or payload.get("factory")
        if not origin:
            raise ValidationError("origin is required")
        if self.viewer(viewer).get("type") == "factory":
            self.require_factory(origin, viewer)
        person_id = payload.get("id") or self.next_id("W", "people")
        if any(p["id"] == person_id for p in self.db["people"]):
            raise ConflictError(f"Person {person_id} already exists")
        row = {
            "id": person_id,
            "origin": origin,
            "skills": sorted(set(payload.get("skills", []))),
            "name": payload.get("name"),
        }
        self.db["people"].append(row)
        self._ensure_factories()
        self.save()
        return self._public(row)

    def set_skills(self, person_id: str, skills: list[str], viewer=None) -> dict[str, Any]:
        person = self.person(person_id)
        v = self.viewer(viewer)
        if v.get("type") == "factory":
            self.require_factory(person["origin"], v)
        person["skills"] = sorted(set(skills or []))
        self.save()
        return self._public(person)

    def add_availability(self, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        person = self.person(payload["person"])
        factory_id = payload.get("factory") or person["origin"]
        self.require_factory(factory_id, viewer)
        try:
            start, end = self._interval(payload)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        kind = payload.get("kind", "lend")
        if kind not in ("lend", "own"):
            raise ValidationError("kind must be lend or own")
        row = {
            "id": payload.get("id") or self.next_id("A", "availability"),
            "person": person["id"],
            "factory": factory_id,
            "start": format_minute(start),
            "end": format_minute(end),
            "kind": kind,
            "_start": start,
            "_end": end,
        }
        # Persist only exchange fields; parsed minutes are derived.
        row.pop("_start")
        row.pop("_end")
        self.db["availability"].append(row)
        self._ensure_factories()
        conflicts = self.availability_conflicts(person["id"])
        commitment_conflicts = []
        if kind == "own":
            for commitment in self.list_commitments(person["id"]):
                if commitment.get("kind") != "shared_loan":
                    continue
                minutes = overlap_minutes((start, end), (commitment["start_minute"], commitment["end_minute"]))
                if minutes > 0:
                    commitment_conflicts.append(
                        {
                            "shift": commitment.get("shift"),
                            "factory": commitment.get("factory"),
                            "overlap_minutes": minutes,
                            "start": format_minute(max(start, commitment["start_minute"])),
                            "end": format_minute(min(end, commitment["end_minute"])),
                        }
                    )
        if payload.get("strict") and (conflicts or commitment_conflicts):
            raise ConflictError(
                "Availability conflicts with retained time or an existing commitment",
                availability_conflicts=conflicts,
                commitment_conflicts=commitment_conflicts,
            )
        self.save()
        return {
            "availability": self._public(row),
            "conflicts": conflicts,
            "commitment_conflicts": commitment_conflicts,
        }

    def revoke_availability(self, availability_id: str, payload: dict[str, Any] | None = None, viewer=None) -> dict[str, Any]:
        payload = payload or {}
        row = self._get("availability", availability_id)
        self.require_factory(row.get("factory") or self.person(row["person"])["origin"], viewer)
        row["status"] = "revoked"
        row["revoked_at"] = payload.get("at") or self.now_label()
        row["revoke_reason"] = payload.get("reason") or "origin_factory_took_back_uncommitted_offer"
        self._audit("availability_revoked", availability=availability_id, person=row["person"])
        self.save()
        return self._public(row)

    def revoke_uncommitted(self, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        """Take back an offer/request before the worker has made a commitment."""
        person_id = payload["person"]
        shift_id = payload["shift"]
        person = self.person(person_id)
        self.require_factory(payload.get("origin") or person["origin"], viewer)
        state = self.agreement_state(person_id, shift_id)
        if state["active"]:
            raise ConflictError(
                "Worker has already agreed; use the recall workflow and obtain return consent/replacement",
                agreement=state,
            )
        revoked = []
        for req in self.db["consent_requests"]:
            if req["person"] == person_id and req["shift"] == shift_id and req["status"] == "awaiting_response":
                req["status"] = "revoked_before_commitment"
                req["revoked_at"] = payload.get("at") or self.now_label()
                revoked.append(req["id"])
        self._audit("uncommitted_offer_revoked", person=person_id, shift=shift_id, requests=revoked)
        self.save()
        return {"person": person_id, "shift": shift_id, "revoked_requests": revoked, "committed": False}

    @staticmethod
    def _interval(payload: dict[str, Any], prefix: str = "") -> tuple[int, int]:
        start_key = prefix + "start"
        end_key = prefix + "end"
        return parse_minute(payload[start_key], start_key), parse_minute(payload[end_key], end_key)

    def _availability_rows(self, person_id: str, kind: str | None = None, include_revoked: bool = False):
        rows = [a for a in self.db["availability"] if a["person"] == person_id]
        if not include_revoked:
            rows = [a for a in rows if a.get("status") != "revoked"]
        if kind:
            rows = [a for a in rows if a["kind"] == kind]
        return rows

    def own_intervals(self, person_id: str) -> list[tuple[int, int]]:
        return merge_intervals(
            (parse_minute(a["start"]), parse_minute(a["end"])) for a in self._availability_rows(person_id, "own")
        )

    def lend_intervals(self, person_id: str) -> list[tuple[int, int]]:
        return merge_intervals(
            (parse_minute(a["start"]), parse_minute(a["end"])) for a in self._availability_rows(person_id, "lend")
        )

    def effective_lend_intervals(self, person_id: str) -> list[tuple[int, int]]:
        own = self.own_intervals(person_id)
        pieces: list[tuple[int, int]] = []
        for interval in self.lend_intervals(person_id):
            pieces.extend(subtract_intervals(interval, own))
        return merge_intervals(pieces)

    def availability_conflicts(self, person_id: str) -> list[dict[str, Any]]:
        conflicts = []
        owns = self._availability_rows(person_id, "own")
        lends = self._availability_rows(person_id, "lend")
        for lend in lends:
            li = (parse_minute(lend["start"]), parse_minute(lend["end"]))
            for own in owns:
                oi = (parse_minute(own["start"]), parse_minute(own["end"]))
                minutes = overlap_minutes(li, oi)
                if minutes > 0:
                    conflicts.append(
                        {
                            "lend_id": lend["id"],
                            "own_id": own["id"],
                            "start": format_minute(max(li[0], oi[0])),
                            "end": format_minute(min(li[1], oi[1])),
                            "overlap_minutes": minutes,
                            "rule": "own retained shift has priority",
                        }
                    )
        return conflicts

    def availability_blockers(self, person_id: str, start: int, end: int) -> list[dict[str, Any]]:
        effective = self.effective_lend_intervals(person_id)
        requested = [(start, end)]
        uncovered: list[tuple[int, int]] = []
        for interval in requested:
            covered_pieces = []
            for allowed in effective:
                hit = intersect_intervals(interval, allowed)
                if hit:
                    covered_pieces.append(hit)
            uncovered.extend(subtract_intervals(interval, covered_pieces))
        if not uncovered:
            return []
        return [
            {
                "code": "outside_lend_availability",
                "start": format_minute(s),
                "end": format_minute(e),
                "minutes": e - s,
            }
            for s, e in uncovered
        ]

    # --------------------------------------------------------------- shifts
    def create_shift(self, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        host = payload.get("host") or payload.get("factory")
        if not host:
            raise ValidationError("host is required")
        self.require_factory(host, viewer)
        try:
            start, end = self._interval(payload)
        except (KeyError, ValueError) as exc:
            raise ValidationError("valid start and end are required", reason=str(exc)) from exc
        min_people = int(payload.get("min_people", 1))
        if min_people < 1:
            raise ValidationError("min_people must be at least 1")
        shift_id = payload.get("id") or self.next_id("S", "shifts")
        if any(s["id"] == shift_id for s in self.db["shifts"]):
            raise ConflictError(f"Shift {shift_id} already exists")
        shift = {
            "id": shift_id,
            "host": host,
            "start": format_minute(start),
            "end": format_minute(end),
            "min_people": min_people,
            "required_skills": sorted(set(payload.get("required_skills", []))),
            "status": payload.get("status", "open"),
        }
        self.db["shifts"].append(shift)
        terms_payload = payload.get("terms") or {
            "place": payload.get("place"),
            "cents_hour": payload.get("cents_hour"),
            "recall_cutoff": payload.get("recall_cutoff"),
            "recall_compensation": payload.get("recall_compensation", 0),
        }
        if terms_payload.get("cents_hour") is not None:
            self._create_term_row(shift, 1, terms_payload)
        self._ensure_factories()
        self._audit("shift_created", shift=shift_id, host=host)
        self.save()
        return self.get_shift_report(shift_id)

    def _create_term_row(self, shift: dict[str, Any], revision: int, payload: dict[str, Any]) -> dict[str, Any]:
        if not payload.get("place"):
            raise ValidationError("terms.place is required")
        if payload.get("cents_hour") is None or int(payload["cents_hour"]) < 0:
            raise ValidationError("terms.cents_hour must be a non-negative integer")
        cutoff = payload.get("recall_cutoff")
        row = {
            "id": payload.get("id") or self.next_id("T", "terms"),
            "shift": shift["id"],
            "revision": revision,
            "place": payload["place"],
            "cents_hour": int(payload["cents_hour"]),
            "recall_cutoff": cutoff,
            "recall_compensation": int(payload.get("recall_compensation", 0) or 0),
        }
        if row["recall_cutoff"] is not None:
            parse_minute(row["recall_cutoff"], "recall_cutoff")
        self.db["terms"].append(row)
        return row

    def terms_for_shift(self, shift_id: str) -> list[dict[str, Any]]:
        return sorted((t for t in self.db["terms"] if t["shift"] == shift_id), key=lambda t: t["revision"])

    def current_terms(self, shift_id: str) -> dict[str, Any]:
        terms = self.terms_for_shift(shift_id)
        if not terms:
            raise NotFoundError(f"Shift {shift_id} has no terms")
        return terms[-1]

    def revise_terms(self, shift_id: str, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        shift = self.shift(shift_id)
        self.require_factory(shift["host"], viewer)
        old = self.current_terms(shift_id)
        next_revision = int(old["revision"]) + 1
        new_payload = dict(old)
        new_payload.pop("id", None)
        new_payload.pop("revision", None)
        material_change = False
        for key in ("place", "cents_hour", "recall_cutoff", "recall_compensation"):
            if key in payload and payload[key] != old.get(key):
                new_payload[key] = payload[key]
                if key in ("place", "cents_hour"):
                    material_change = True
        # Time changes are material and create a new consent-bounding version.
        if "start" in payload or "end" in payload or "start" in payload.get("time", {}) or "end" in payload.get("time", {}):
            time_payload = payload.get("time", payload)
            start = parse_minute(time_payload.get("start", shift["start"]), "start")
            end = parse_minute(time_payload.get("end", shift["end"]), "end")
            if end <= start:
                raise ValidationError("shift end must be after start")
            if format_minute(start) != shift["start"] or format_minute(end) != shift["end"]:
                material_change = True
                shift["start"] = format_minute(start)
                shift["end"] = format_minute(end)
        if payload.get("force_new_revision"):
            material_change = True
        if not material_change:
            return {"terms": self._public(old), "reasked": [], "changed": False}
        term = self._create_term_row(shift, next_revision, new_payload)
        reasked = self._reask_current_terms(shift, term["id"])
        self._audit("terms_revised", shift=shift_id, terms=term["id"], revision=next_revision)
        self.save()
        return {"terms": self._public(term), "reasked": reasked, "changed": True}

    def _reask_current_terms(self, shift: dict[str, Any], terms_id: str) -> list[str]:
        persons = set()
        for c in self.db["consents"]:
            t = next((x for x in self.db["terms"] if x["id"] == c["terms"]), None)
            if t and t["shift"] == shift["id"] and c["decision"] == "accept":
                persons.add(c["person"])
        # Old open requests are superseded by the new terms.
        for req in self.db["consent_requests"]:
            if req["shift"] == shift["id"] and req["status"] == "awaiting_response":
                req["status"] = "superseded"
        result = []
        for person_id in sorted(persons):
            req = {
                "id": self.next_id("Q", "consent_requests"),
                "person": person_id,
                "shift": shift["id"],
                "terms": terms_id,
                "status": "awaiting_response",
                "created_at": self.now_label(),
            }
            self.db["consent_requests"].append(req)
            result.append(person_id)
        return result

    def ask_consent(self, shift_id: str, payload: dict[str, Any] | str, viewer=None) -> dict[str, Any]:
        shift = self.shift(shift_id)
        self.require_factory(shift["host"], viewer)
        person_id = payload if isinstance(payload, str) else payload["person"]
        self.person(person_id)
        terms_id = (
            payload.get("terms") if isinstance(payload, dict) and payload.get("terms") else self.current_terms(shift_id)["id"]
        )
        self.term(terms_id)
        for req in self.db["consent_requests"]:
            if req["person"] == person_id and req["terms"] == terms_id and req["status"] == "awaiting_response":
                return self._public(req)
        req = {
            "id": self.next_id("Q", "consent_requests"),
            "person": person_id,
            "shift": shift_id,
            "terms": terms_id,
            "status": "awaiting_response",
            "created_at": self.now_label(payload.get("at") if isinstance(payload, dict) else None),
        }
        self.db["consent_requests"].append(req)
        self.save()
        return self._public(req)

    # ------------------------------------------------------------- consents
    def _consent_sort_key(self, c: dict[str, Any]) -> tuple[int, int]:
        try:
            minute = parse_minute(c.get("at", "00:00"), "at")
        except ValueError:
            minute = 0
        return minute, int(c.get("_seq", 0))

    def agreement_state(self, person_id: str, shift_id: str) -> dict[str, Any]:
        current = self.current_terms(shift_id)
        terms = {t["id"]: t for t in self.terms_for_shift(shift_id)}
        history = [
            c
            for c in self.db["consents"]
            if c["person"] == person_id and terms.get(c["terms"], {}).get("shift") == shift_id
        ]
        history.sort(key=self._consent_sort_key)
        active = False
        accepted_terms: str | None = None
        accepted_revision: int | None = None
        for c in history:
            rev = terms[c["terms"]]["revision"]
            if c["decision"] == "accept":
                active = True
                accepted_terms = c["terms"]
                accepted_revision = rev
            elif c["decision"] == "withdraw":
                active = False
                accepted_terms = None
                accepted_revision = None
            elif c["decision"] == "reject" and accepted_revision == rev:
                active = False
                accepted_terms = None
                accepted_revision = None
        current_history = [c for c in history if c["terms"] == current["id"]]
        current_decision = current_history[-1]["decision"] if current_history else None
        current_accepted = active and accepted_terms == current["id"] and current_decision == "accept"
        return {
            "active": active,
            "accepted_terms": accepted_terms,
            "accepted_revision": accepted_revision,
            "current_terms": current["id"],
            "current_revision": current["revision"],
            "current_decision": current_decision,
            "current_accepted": bool(current_accepted),
            "needs_reconfirmation": active and not current_accepted,
            "history": [self._public(c) for c in history],
        }

    def list_consents(self, viewer=None, person_id: str | None = None) -> list[dict[str, Any]]:
        v = self.viewer(viewer)
        rows = self.db["consents"]
        if person_id:
            if v.get("type") == "person":
                self.require_person(person_id, v)
            rows = [c for c in rows if c["person"] == person_id]
        if v.get("type") == "factory":
            factory = v["id"]
            visible_shifts = {s["id"] for s in self.db["shifts"] if s["host"] == factory}
            own_people = {p["id"] for p in self.db["people"] if p["origin"] == factory}
            visible_terms = {t["id"] for t in self.db["terms"] if t["shift"] in visible_shifts}
            rows = [c for c in rows if c["terms"] in visible_terms or c["person"] in own_people]
        elif v.get("type") == "person" and not person_id:
            rows = [c for c in rows if c["person"] == v["id"]]
        return [self._public(c) for c in rows]

    def record_consent(self, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        person_id = payload.get("person") or (viewer or {}).get("id")
        if not person_id:
            raise ValidationError("person is required")
        self.require_person(person_id, viewer)
        self.person(person_id)
        decision = payload.get("decision")
        if decision not in ("accept", "reject", "withdraw"):
            raise ValidationError("decision must be accept, reject or withdraw")
        terms_id = payload.get("terms") or payload.get("terms_id")
        shift_id = payload.get("shift") or payload.get("shift_id")
        if not terms_id and shift_id:
            terms_id = self.current_terms(shift_id)["id"]
        if not terms_id:
            raise ValidationError("terms or shift is required")
        term = self.term(terms_id)
        shift_id = term["shift"]
        shift = self.shift(shift_id)
        current = self.current_terms(shift_id)
        at = payload.get("at") or self.now_label()
        if decision == "accept" and terms_id != current["id"] and not payload.get("allow_old_terms"):
            raise ConflictError(
                "This terms version is no longer current; the worker must be asked and accept the new terms",
                code="terms_superseded",
                attempted_terms=terms_id,
                current_terms=current["id"],
                current_revision=current["revision"],
            )
        if decision == "accept" and not payload.get("allow_conflict"):
            start, end = parse_minute(shift["start"]), parse_minute(shift["end"])
            conflicts = self.find_commitment_conflicts(person_id, start, end, exclude_shift=shift_id)
            if conflicts:
                raise ConflictError("A worker can be promised to only one party for overlapping time", conflicts=conflicts)
        consent = {
            "id": payload.get("id") or self.next_id("C", "consents"),
            "person": person_id,
            "terms": terms_id,
            "decision": decision,
            "at": at,
            "_seq": len(self.db["consents"]),
        }
        if payload.get("effective"):
            consent["effective"] = payload["effective"]
        if payload.get("reason"):
            consent["reason"] = payload["reason"]
        self.db["consents"].append(consent)
        for req in self.db["consent_requests"]:
            if req.get("person") == person_id and req.get("terms") == terms_id and req.get("status") == "awaiting_response":
                req["status"] = decision + "ed" if decision != "withdraw" else "withdrawn"
                req["responded_at"] = at
                req["consent"] = consent["id"]
        self._audit("consent_recorded", person=person_id, shift=shift_id, decision=decision, terms=terms_id)
        self.save()
        auto = self._auto_complete_recalls(person_id, shift_id)
        return {"consent": self._public(consent), "agreement": self.agreement_state(person_id, shift_id), "recalls": auto}

    def find_commitment_conflicts(
        self, person_id: str, start: int, end: int, exclude_shift: str | None = None
    ) -> list[dict[str, Any]]:
        conflicts = []
        for c in self.list_commitments(person_id):
            if exclude_shift and c.get("shift") == exclude_shift:
                continue
            minutes = overlap_minutes((start, end), (c["start_minute"], c["end_minute"]))
            if minutes > 0:
                conflicts.append(
                    {
                        "commitment_id": c.get("id"),
                        "shift": c.get("shift"),
                        "factory": c.get("factory"),
                        "start": format_minute(max(start, c["start_minute"])),
                        "end": format_minute(min(end, c["end_minute"])),
                        "overlap_minutes": minutes,
                    }
                )
        return conflicts

    def assignment_interval(self, person_id: str, shift_id: str) -> tuple[int, int] | None:
        shift = self.shift(shift_id)
        start, end = parse_minute(shift["start"]), parse_minute(shift["end"])
        # A named replacement takes over from the recall effective time.
        for r in self.db["recalls"]:
            if r.get("shift") == shift_id and r.get("replacement") == person_id and r.get("effective"):
                start = max(start, parse_minute(r["effective"]))
        # A terminal recall truncates the recalled worker's promise.
        effect = self.recall_effect(person_id, shift_id)
        if effect and effect.get("terminal") and effect.get("stop_minute") is not None:
            end = min(end, effect["stop_minute"])
        if end <= start:
            return None
        return start, end

    def list_commitments(self, person_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        person = self.person(person_id)
        for a in self._availability_rows(person_id, "own"):
            s, e = parse_minute(a["start"]), parse_minute(a["end"])
            result.append(
                {
                    "id": a["id"],
                    "kind": "own_shift",
                    "person": person_id,
                    "factory": person["origin"],
                    "shift": None,
                    "start": a["start"],
                    "end": a["end"],
                    "start_minute": s,
                    "end_minute": e,
                    "responsible_units": {"employer": person["origin"], "workplace": person["origin"], "wage": person["origin"]},
                }
            )
        for shift in self.db["shifts"]:
            state = self.agreement_state(person_id, shift["id"])
            if not state["active"]:
                continue
            interval = self.assignment_interval(person_id, shift["id"])
            if not interval:
                continue
            s, e = interval
            terms_id = state["accepted_terms"] or state["current_terms"]
            terms = self.term(terms_id)
            result.append(
                {
                    "id": f"PROMISE-{person_id}-{shift['id']}",
                    "kind": "shared_loan",
                    "person": person_id,
                    "factory": shift["host"],
                    "party": shift["host"],
                    "origin": person["origin"],
                    "shift": shift["id"],
                    "terms": terms_id,
                    "terms_revision": terms["revision"],
                    "current_terms_accepted": state["current_accepted"],
                    "needs_reconfirmation": state["needs_reconfirmation"],
                    "start": format_minute(s),
                    "end": format_minute(e),
                    "start_minute": s,
                    "end_minute": e,
                    "location": terms["place"],
                    "place": terms["place"],
                    "pay_terms": {
                        "cents_hour": terms["cents_hour"],
                        "currency_unit": "cent",
                        "recall_compensation": terms["recall_compensation"],
                    },
                    "responsible_units": {
                        "employer": person["origin"],
                        "workplace_management": shift["host"],
                        "wage": shift["host"],
                    },
                }
            )
        result.sort(key=lambda x: (x["start_minute"], x["end_minute"]))
        return result

    # -------------------------------------------------------------- staffing
    def _current_accepted_people(self, shift_id: str) -> list[str]:
        people = []
        for p in self.db["people"]:
            state = self.agreement_state(p["id"], shift_id)
            if state["current_accepted"]:
                people.append(p["id"])
        return sorted(people)

    def _roster_for_interval(
        self,
        shift: dict[str, Any],
        terms: dict[str, Any],
        start: int,
        end: int,
        *,
        remove_person: str | None = None,
        add_person: str | None = None,
        use_assignments: bool = True,
    ) -> dict[str, Any]:
        candidate_ids = set(self._current_accepted_people(shift["id"]))
        if add_person:
            candidate_ids.add(add_person)
        if remove_person:
            candidate_ids.discard(remove_person)
        people_by_id = {p["id"]: p for p in self.db["people"]}
        eligible: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        for person_id in sorted(candidate_ids):
            person = people_by_id.get(person_id)
            if not person:
                invalid.append({"person": person_id, "blockers": [{"code": "unknown_person"}]})
                continue
            interval = (start, end)
            if use_assignments:
                assigned = self.assignment_interval(person_id, shift["id"])
                if not assigned:
                    invalid.append({"person": person_id, "skills": person.get("skills", []), "blockers": [{"code": "not_assigned_in_interval"}]})
                    continue
                hit = intersect_intervals(interval, assigned)
                if not hit or hit[1] - hit[0] < end - start:
                    invalid.append({"person": person_id, "skills": person.get("skills", []), "blockers": [{"code": "assignment_does_not_cover_interval"}]})
                    continue
            blockers = self.availability_blockers(person_id, start, end)
            conflicts = self.find_commitment_conflicts(person_id, start, end, exclude_shift=shift["id"])
            if conflicts:
                blockers.append({"code": "overlapping_commitment", "conflicts": conflicts})
            item = {"person": person_id, "skills": person.get("skills", []), "origin": person.get("origin")}
            if blockers:
                item["blockers"] = blockers
                invalid.append(item)
            else:
                eligible.append(item)
        covered = set()
        for item in eligible:
            covered.update(item.get("skills", []))
        required = shift.get("required_skills", [])
        missing = [skill for skill in required if skill not in covered]
        count = len(eligible)
        compliant = count >= int(shift["min_people"]) and not missing
        return {
            "start": format_minute(start),
            "end": format_minute(end),
            "eligible": eligible,
            "invalid": invalid,
            "eligible_count": count,
            "min_people": shift["min_people"],
            "covered_skills": sorted(covered),
            "missing_skills": missing,
            "skill_coverage_complete": not missing,
            "headcount_complete": count >= int(shift["min_people"]),
            "compliant": compliant,
            "can_open": compliant,
        }

    def staffing(self, shift_id: str, at: str | int | None = None) -> dict[str, Any]:
        shift = self.shift(shift_id)
        terms = self.current_terms(shift_id)
        start, end = parse_minute(shift["start"]), parse_minute(shift["end"])
        boundaries = {start, end}
        accepted = self._current_accepted_people(shift_id)
        for person_id in accepted:
            interval = self.assignment_interval(person_id, shift_id)
            if interval:
                boundaries.update(interval)
            for a in self._availability_rows(person_id):
                boundaries.add(parse_minute(a["start"]))
                boundaries.add(parse_minute(a["end"]))
            for c in self.list_commitments(person_id):
                if c.get("shift") != shift_id:
                    boundaries.add(c["start_minute"])
                    boundaries.add(c["end_minute"])
        cuts = sorted(b for b in boundaries if start <= b <= end)
        segments = []
        for bs, be in zip(cuts, cuts[1:]):
            if be > bs:
                segments.append(self._roster_for_interval(shift, terms, bs, be))
        candidates = []
        candidate_ids = set(accepted)
        candidate_ids.update(
            r.get("replacement")
            for r in self.db["recalls"]
            if r.get("shift") == shift_id and r.get("replacement")
        )
        for person_id in sorted(candidate_ids):
            if not person_id:
                continue
            state = self.agreement_state(person_id, shift_id)
            person = self.person(person_id)
            candidates.append(
                {
                    "person": person_id,
                    "skills": person.get("skills", []),
                    "current_accepted": state["current_accepted"],
                    "current_decision": state["current_decision"],
                    "needs_reconfirmation": state["needs_reconfirmation"],
                    "assignment_interval": self._interval_json(self.assignment_interval(person_id, shift_id)),
                }
            )
        at_minute = self.now_minute(at) if at is not None or self.clock is not None else None
        current_segment = None
        if at_minute is not None:
            current_segment = next((seg for seg in segments if parse_minute(seg["start"]) <= at_minute < parse_minute(seg["end"])), None)
        remaining = []
        if at_minute is not None:
            for seg in segments:
                ss, ee = parse_minute(seg["start"]), parse_minute(seg["end"])
                if ee <= at_minute:
                    continue
                rs = max(ss, at_minute)
                # Re-evaluate partial segment for exact remaining interval.
                remaining.append(self._roster_for_interval(shift, terms, rs, ee))
        result = {
            "shift": shift_id,
            "host": shift["host"],
            "start": shift["start"],
            "end": shift["end"],
            "current_terms": terms["id"],
            "terms_revision": terms["revision"],
            "min_people": shift["min_people"],
            "required_skills": shift["required_skills"],
            "candidates": candidates,
            "segments": segments,
            "can_open": bool(segments) and all(seg["compliant"] for seg in segments),
            "can_start": bool(segments) and segments[0]["compliant"],
            "at": format_minute(at_minute),
            "current_segment": current_segment,
            "remaining_segments": remaining,
            "can_open_remaining": bool(remaining) and all(seg["compliant"] for seg in remaining),
        }
        if current_segment:
            result["openable_now"] = current_segment["compliant"]
            result["openable_headcount"] = current_segment["eligible_count"]
        return result

    @staticmethod
    def _interval_json(interval: tuple[int, int] | None) -> dict[str, Any] | None:
        if not interval:
            return None
        return {"start": format_minute(interval[0]), "end": format_minute(interval[1])}

    def get_shift_report(self, shift_id: str, at=None, viewer=None) -> dict[str, Any]:
        shift = self._public(self.shift(shift_id))
        if viewer is not None:
            v = self.viewer(viewer)
            if v.get("type") == "factory" and v["id"] != shift["host"]:
                # Origin factories may see shifts involving their workers; the
                # board serializer redacts commercial fields there.
                pass
        return {"shift": shift, "terms": self._public(self.current_terms(shift_id)), "staffing": self.staffing(shift_id, at)}

    # --------------------------------------------------------------- recalls
    def list_recalls(self, viewer=None, at=None) -> list[dict[str, Any]]:
        v = self.viewer(viewer)
        rows = self.db["recalls"]
        if v.get("type") == "factory":
            factory = v["id"]
            rows = [
                r
                for r in rows
                if self.shift(r["shift"])["host"] == factory or self.person(r["person"])["origin"] == factory
            ]
        elif v.get("type") == "person":
            rows = [r for r in rows if r["person"] == v["id"]]
        views = [self.recall_view(r["id"], at) for r in rows]
        if v.get("type") == "factory":
            views = [self._redact_recall_for_factory(view, v["id"]) for view in views]
        return views

    @staticmethod
    def _redact_recall_for_factory(recall: dict[str, Any], factory_id: str) -> dict[str, Any]:
        comp = recall.get("compensation")
        if comp and comp.get("liable_factory") != factory_id:
            recall["compensation"] = {
                "id": comp.get("id"),
                "status": comp.get("status"),
                "liable_factory": comp.get("liable_factory"),
                "amount_redacted": True,
            }
        return recall

    def request_recall(self, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        person_id = payload["person"]
        shift_id = payload["shift"]
        person = self.person(person_id)
        shift = self.shift(shift_id)
        origin = payload.get("origin") or person["origin"]
        self.require_factory(origin, viewer)
        if origin != person["origin"]:
            raise ValidationError("Only the worker's origin factory may request an early recall")
        for r in self.db["recalls"]:
            if r["person"] == person_id and r["shift"] == shift_id and not r.get("force_stopped_at"):
                view = self.recall_view(r["id"])
                if not view["terminal"]:
                    raise ConflictError("An open recall already exists", recall=r["id"])
        requested = payload.get("requested") or self.now_label()
        effective = payload.get("effective")
        if effective:
            parse_minute(effective, "effective")
        r = {
            "id": payload.get("id") or self.next_id("R", "recalls"),
            "person": person_id,
            "shift": shift_id,
            "requested": requested,
            "effective": effective,
            "from_accepted": None,
            "worker_accepted": None,
            "replacement": None,
            "requested_by": origin,
            "replacement_status": "not_requested",
        }
        self.db["recalls"].append(r)
        self._audit("recall_requested", recall=r["id"], person=person_id, shift=shift_id)
        self.save()
        return self.recall_view(r["id"])

    def _auto_complete_recalls(self, person_id: str, shift_id: str) -> list[dict[str, Any]]:
        completed = []
        for r in self.db["recalls"]:
            if r["person"] != person_id or r["shift"] != shift_id:
                continue
            if r.get("worker_accepted") and r.get("from_accepted") and r.get("replacement"):
                check = self._check_replacement(r)
                if check["compliant"]:
                    r["status"] = "completed_return"
                    self._ensure_compensation(r, "agreed_return")
                    completed.append(self.recall_view(r["id"]))
        return completed

    def worker_respond_recall(self, recall_id: str, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        r = self._get("recalls", recall_id)
        self.require_person(r["person"], viewer)
        decision = payload.get("decision", "accept")
        at = payload.get("at") or self.now_label()
        if decision == "accept":
            r["worker_accepted"] = at
            r.pop("worker_declined", None)
            self._audit("recall_worker_accepted", recall=recall_id)
        elif decision in ("decline", "reject"):
            r["worker_declined"] = at
            r["worker_accepted"] = None
            self._audit("recall_worker_declined", recall=recall_id)
        else:
            raise ValidationError("decision must be accept or decline")
        self._auto_complete_recalls(r["person"], r["shift"])
        self.save()
        return self.recall_view(recall_id)

    def host_confirm_recall(self, recall_id: str, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        r = self._get("recalls", recall_id)
        shift = self.shift(r["shift"])
        self.require_factory(shift["host"], viewer)
        at = payload.get("at") or self.now_label()
        candidate = dict(r)
        if payload.get("replacement"):
            candidate["replacement"] = payload["replacement"]
        if payload.get("from_accepted", True):
            candidate["from_accepted"] = at
        check = self._check_replacement(candidate)
        if not check["compliant"]:
            raise ConflictError("Replacement does not compliantly cover the crew after recall", replacement=check)
        if payload.get("replacement"):
            r["replacement"] = payload["replacement"]
        r["from_accepted"] = at
        r["host_confirmed_by"] = shift["host"]
        r["replacement_status"] = "confirmed"
        self._auto_complete_recalls(r["person"], r["shift"])
        self._audit("recall_host_confirmed", recall=recall_id, replacement=r.get("replacement"))
        self.save()
        return self.recall_view(recall_id)

    def request_replacement(self, recall_id: str, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        r = self._get("recalls", recall_id)
        shift = self.shift(r["shift"])
        self.require_factory(shift["host"], viewer)
        person_id = payload["person"]
        self.person(person_id)
        r["replacement"] = person_id
        r["replacement_status"] = "awaiting_dispatch"
        terms = self.current_terms(shift["id"])
        req = self.ask_consent(shift["id"], {"person": person_id, "terms": terms["id"]}, viewer={"type": "admin"})
        message = {
            "id": self.next_id("MSG", "outbox"),
            "type": "replacement_request",
            "recall": recall_id,
            "person": person_id,
            "shift": shift["id"],
            "terms": terms["id"],
            "consent_request": req["id"],
            "created_at": self.now_label(payload.get("at")),
            "status": "queued",
        }
        self.db["outbox"].append(message)
        try:
            reply = self.gateway.send_replacement_request(message)
            message["status"] = "sent"
            message["reply"] = reply
            r["replacement_status"] = "awaiting_response"
        except GatewayFailure as exc:
            message["status"] = "failed"
            message["error"] = exc.message
            r["replacement_status"] = "interface_failed"
            self._audit("replacement_interface_failed", recall=recall_id, error=exc.message)
            self.save()
            # Crucially, no worker_accepted/withdrawn mutation occurs.
            raise
        self.save()
        return self.recall_view(recall_id)

    def retry_replacement_request(self, recall_id: str, viewer=None) -> dict[str, Any]:
        r = self._get("recalls", recall_id)
        shift = self.shift(r["shift"])
        self.require_factory(shift["host"], viewer)
        message = next((m for m in reversed(self.db["outbox"]) if m.get("recall") == recall_id), None)
        if not message:
            raise NotFoundError("No replacement interface message exists for this recall")
        try:
            reply = self.gateway.send_replacement_request(message)
            message["status"] = "sent"
            message["reply"] = reply
            message.pop("error", None)
            r["replacement_status"] = "awaiting_response"
        except GatewayFailure as exc:
            message["status"] = "failed"
            message["error"] = exc.message
            r["replacement_status"] = "interface_failed"
            self.save()
            raise
        self.save()
        return self.recall_view(recall_id)

    def _check_replacement(self, r: dict[str, Any]) -> dict[str, Any]:
        rid = r.get("replacement")
        if not rid:
            return {"compliant": False, "reason": "no_replacement"}
        try:
            self.person(rid)
        except NotFoundError:
            return {"compliant": False, "person": rid, "reason": "unknown_person"}
        shift = self.shift(r["shift"])
        terms = self.current_terms(shift["id"])
        state = self.agreement_state(rid, shift["id"])
        if not state["current_accepted"]:
            return {"person": rid, "compliant": False, "reason": "replacement_has_not_accepted_current_terms", "agreement": state}
        stop = parse_minute(r["effective"]) if r.get("effective") else parse_minute(shift["start"])
        end = parse_minute(shift["end"])
        if stop >= end:
            return {"person": rid, "compliant": False, "reason": "recall_effective_is_at_or_after_shift_end"}
        roster = self._roster_for_interval(
            shift, terms, stop, end, remove_person=r["person"], add_person=rid, use_assignments=False
        )
        return {"person": rid, "compliant": roster["compliant"], "post_recall_roster": roster}

    def recall_effect(self, person_id: str, shift_id: str) -> dict[str, Any] | None:
        effects = []
        for r in self.db["recalls"]:
            if r["person"] != person_id or r["shift"] != shift_id:
                continue
            view = self.recall_view(r["id"])
            if not view.get("terminal"):
                continue
            stop = r.get("stop_at") or r.get("effective")
            effects.append(
                {
                    "recall": r["id"],
                    "terminal": True,
                    "mode": "force_stop" if r.get("force_stopped_at") else "agreed_return",
                    "stop": stop,
                    "stop_minute": parse_minute(stop) if stop else None,
                }
            )
        if not effects:
            return None
        return min(effects, key=lambda x: x["stop_minute"] if x["stop_minute"] is not None else 1441)

    def recall_view(self, recall_id: str, at: str | int | None = None) -> dict[str, Any]:
        r = self._get("recalls", recall_id)
        shift = self.shift(r["shift"])
        terms = self.current_terms(r["shift"])
        action_minute = self.now_minute(at) if at is not None or self.clock is not None else None
        cutoff_minute = parse_minute(terms["recall_cutoff"]) if terms.get("recall_cutoff") else None
        replacement_check = self._check_replacement(r) if r.get("replacement") else {"compliant": False, "reason": "no_replacement"}
        worker_agreed = bool(r.get("worker_accepted"))
        host_confirmed = bool(r.get("from_accepted"))
        forced = bool(r.get("force_stopped_at"))
        normal_complete = worker_agreed and host_confirmed and replacement_check.get("compliant", False)
        terminal = normal_complete or forced
        cutoff_passed = cutoff_minute is not None and action_minute is not None and action_minute >= cutoff_minute
        can_force = bool(cutoff_passed and not terminal and not replacement_check.get("compliant", False))
        if forced:
            status = "force_stopped"
        elif normal_complete:
            status = "completed_return"
        elif r.get("replacement_status") == "interface_failed":
            status = "replacement_interface_failed"
        elif r.get("replacement_status") == "awaiting_response":
            status = "awaiting_replacement_response"
        elif worker_agreed:
            status = "awaiting_host_or_replacement"
        elif host_confirmed:
            status = "awaiting_worker_reconsent"
        else:
            status = "requested"
        worker_withdrawal_status = "agreed" if worker_agreed else "not_agreed"
        if r.get("worker_declined"):
            worker_withdrawal_status = "declined"
        if forced and not worker_agreed:
            worker_withdrawal_status = "not_agreed_but_contractually_stopped"
        compensation = next((x for x in self.db["liabilities"] if x.get("source_recall") == r["id"]), None)
        return {
            **self._public(r),
            "status": status,
            "terminal": terminal,
            "normal_return_complete": normal_complete,
            "worker_return_consent": worker_withdrawal_status,
            "worker_agreed": worker_agreed,
            "host_confirmed": host_confirmed,
            "replacement_check": replacement_check,
            "cutoff": terms.get("recall_cutoff"),
            "cutoff_passed": cutoff_passed,
            "can_force_stop": can_force,
            "compensation": self._public(compensation),
            "available_actions": self._recall_actions(
                status=status,
                can_force=can_force,
                worker_agreed=worker_agreed,
                host_confirmed=host_confirmed,
                replacement=r.get("replacement"),
            ),
        }

    @staticmethod
    def _recall_actions(**state) -> list[str]:
        actions = []
        if not state["worker_agreed"]:
            actions.append("worker_accept_or_decline_return")
        if not state["host_confirmed"]:
            actions.append("host_confirm_replacement")
        if not state["replacement"]:
            actions.append("request_replacement")
        if state["can_force"]:
            actions.append("force_stop_unperformed_remainder_at_cutoff")
        if state["status"] == "replacement_interface_failed":
            actions.append("retry_replacement_interface")
        return actions

    def force_stop_recall(self, recall_id: str, payload: dict[str, Any] | None = None, viewer=None, at=None) -> dict[str, Any]:
        payload = payload or {}
        r = self._get("recalls", recall_id)
        person = self.person(r["person"])
        self.require_factory(payload.get("origin") or person["origin"], viewer)
        view = self.recall_view(recall_id, at=payload.get("at") or at)
        if view["terminal"]:
            return view
        action_minute = self.now_minute(payload.get("at") or at)
        cutoff = parse_minute(view["cutoff"]) if view.get("cutoff") else None
        if cutoff is None:
            raise ConflictError("Terms do not define a recall cutoff")
        if action_minute < cutoff:
            raise ConflictError("Cannot force stop before the contractual recall cutoff", cutoff=view["cutoff"], now=format_minute(action_minute))
        if view["replacement_check"].get("compliant") and not payload.get("allow_compliant_replacement"):
            raise ConflictError(
                "A compliant replacement exists; complete the agreed return or explicitly allow force stop",
                replacement=view["replacement_check"],
            )
        shift = self.shift(r["shift"])
        stop_label = payload.get("stop_at") or r.get("effective") or view["cutoff"]
        stop_minute = parse_minute(stop_label, "stop_at")
        if stop_minute < cutoff:
            raise ConflictError("Force-stop time cannot be earlier than recall cutoff", cutoff=view["cutoff"], stop_at=stop_label)
        if stop_minute >= parse_minute(shift["end"]):
            raise ConflictError("No unperformed interval remains to stop")
        r["force_stopped_at"] = format_minute(action_minute)
        r["stop_at"] = format_minute(stop_minute)
        r["force_stop_reason"] = payload.get("reason") or "cutoff_reached_without_compliant_replacement"
        # Explicitly leave worker_accepted untouched.
        liability = self._ensure_compensation(r, "force_stop")
        shortfalls = self._create_shortfalls(r, stop_minute)
        self._audit("recall_force_stopped", recall=recall_id, stop_at=r["stop_at"], shortfalls=[x["id"] for x in shortfalls])
        self.save()
        result = self.recall_view(recall_id, at=action_minute)
        result["created_liability"] = liability
        result["shortfalls"] = shortfalls
        result["recheck"] = self.staffing(r["shift"], at=format_minute(action_minute))
        return result

    def process_due_recalls(self, at: str | int | None = None, viewer=None) -> dict[str, Any]:
        processed, skipped = [], []
        when = at or self.clock or self.now_label()
        for r in list(self.db["recalls"]):
            view = self.recall_view(r["id"], at=when)
            if view["terminal"]:
                self._ensure_compensation(r, view["status"])
                continue
            if view["can_force_stop"]:
                try:
                    result = self.force_stop_recall(r["id"], {"at": when}, viewer={"type": "admin"})
                    processed.append({"recall": r["id"], "status": result["status"]})
                except LaborError as exc:
                    skipped.append({"recall": r["id"], "reason": exc.code, "message": exc.message})
            else:
                skipped.append({"recall": r["id"], "status": view["status"], "cutoff_passed": view["cutoff_passed"]})
        self.save()
        return {"at": when, "processed": processed, "skipped": skipped}

    def reconcile_terminal_recalls(self, create_liabilities: bool = True) -> list[dict[str, Any]]:
        out = []
        for r in self.db["recalls"]:
            view = self.recall_view(r["id"])
            if view["terminal"] and create_liabilities:
                out.append(self._ensure_compensation(r, view["status"]))
        return out

    def _ensure_compensation(self, r: dict[str, Any], mode: str) -> dict[str, Any]:
        existing = next((x for x in self.db["liabilities"] if x.get("source_recall") == r["id"]), None)
        if existing:
            return existing
        terms = self.current_terms(r["shift"])
        person = self.person(r["person"])
        amount = int(terms.get("recall_compensation") or 0)
        row = {
            "id": self.next_id("L", "liabilities"),
            "type": "recall_compensation",
            "person": r["person"],
            "shift": r["shift"],
            "source_recall": r["id"],
            "terms": terms["id"],
            "amount": amount,
            "currency": "CNY_CENT",
            "liable_factory": r.get("compensation_factory") or terms.get("compensation_factory") or person["origin"],
            "basis": "origin_factory_early_recall" if person["origin"] == r.get("requested_by", person["origin"]) else "agreement",
            "status": "awaiting_pay_confirmation",
            "mode": mode,
        }
        self.db["liabilities"].append(row)
        return row

    def _create_shortfalls(self, r: dict[str, Any], stop_minute: int) -> list[dict[str, Any]]:
        existing = [x for x in self.db["shortfalls"] if x.get("source_recall") == r["id"]]
        if existing:
            return existing
        shift = self.shift(r["shift"])
        staffing = self.staffing(r["shift"])
        specs: list[dict[str, Any]] = []
        for seg in staffing["segments"]:
            ss, ee = parse_minute(seg["start"]), parse_minute(seg["end"])
            hit = intersect_intervals((ss, ee), (stop_minute, parse_minute(shift["end"])))
            if not hit or seg["compliant"]:
                continue
            hs, he = hit
            signature = (
                int(shift["min_people"]),
                int(seg["eligible_count"]),
                tuple(seg["missing_skills"]),
                tuple(x["person"] for x in seg["eligible"]),
            )
            if specs and specs[-1]["end_minute"] == hs and specs[-1]["signature"] == signature:
                specs[-1]["end_minute"] = he
                continue
            specs.append({"start_minute": hs, "end_minute": he, "signature": signature, "segment": seg})
        created = []
        for spec in specs:
            seg = spec["segment"]
            hs, he = spec["start_minute"], spec["end_minute"]
            row = {
                "id": self.next_id("SF", "shortfalls"),
                "kind": "understaffed_interval",
                "person_recalled": r["person"],
                "source_recall": r["id"],
                "shift": r["shift"],
                "host": shift["host"],
                "start": format_minute(hs),
                "end": format_minute(he),
                "minutes": he - hs,
                "required_people": shift["min_people"],
                "actual_people": seg["eligible_count"],
                "missing_skills": seg["missing_skills"],
                "eligible_people": [x["person"] for x in seg["eligible"]],
                "status": "open",
                "reason": "crew_count_or_certification_no_longer_covered",
            }
            self.db["shortfalls"].append(row)
            created.append(row)
        return created

    # ------------------------------------------------------------ attendance
    def add_attendance(self, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        reporter = payload.get("reporter") or (viewer or {}).get("id")
        if not reporter:
            raise ValidationError("reporter factory is required")
        self.require_factory(reporter, viewer)
        person_id = payload["person"]
        self.person(person_id)
        if payload.get("shift"):
            self.shift(payload["shift"])
        try:
            start, end = self._interval(payload)
        except (KeyError, ValueError) as exc:
            raise ValidationError("valid attendance start/end are required", reason=str(exc)) from exc
        row = {
            "id": payload.get("id") or self.next_id("E", "attendance"),
            "person": person_id,
            "shift": payload.get("shift"),
            "reporter": reporter,
            "start": format_minute(start),
            "end": format_minute(end),
            "received": payload.get("received") or self.now_label(),
            "_seq": len(self.db["attendance"]),
        }
        self.db["attendance"].append(row)
        self._ensure_factories()
        disputes = self.sync_disputes(person_id)
        self._audit("attendance_received", id=row["id"], person=person_id, late=bool(payload.get("late")))
        self.save()
        return {"attendance": self._public(row), "reconciliation": self.attendance_view(person_id), "new_disputes": disputes}

    def _attendance_segments_raw(self, person_id: str, shift_id: str | None = None) -> list[dict[str, Any]]:
        rows = [a for a in self.db["attendance"] if a["person"] == person_id]
        if shift_id:
            rows = [a for a in rows if a.get("shift") == shift_id]
        if not rows:
            return []
        points = sorted({x for a in rows for x in (parse_minute(a["start"]), parse_minute(a["end"]))})
        segments = []
        for bs, be in zip(points, points[1:]):
            if be <= bs:
                continue
            evidence = []
            for a in rows:
                as_, ae = parse_minute(a["start"]), parse_minute(a["end"])
                if as_ <= bs and ae >= be:
                    evidence.append(a)
            if not evidence:
                continue
            reporters = sorted({a["reporter"] for a in evidence})
            shifts = sorted({a.get("shift") for a in evidence if a.get("shift")})
            disputed = len(reporters) > 1
            segments.append(
                {
                    "person": person_id,
                    "start": format_minute(bs),
                    "end": format_minute(be),
                    "start_minute": bs,
                    "end_minute": be,
                    "minutes": be - bs,
                    "evidence_ids": [a["id"] for a in evidence],
                    "reporters": reporters,
                    "shifts": shifts,
                    "disputed": disputed,
                    "status": "attribution_disputed" if disputed else "confirmed",
                    "liable_factory": None if disputed else reporters[0],
                }
            )
        return self._merge_segments(segments)

    @staticmethod
    def _merge_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for seg in segments:
            key = (
                seg["status"],
                tuple(seg["reporters"]),
                tuple(seg["shifts"]),
                seg["liable_factory"],
            )
            if merged:
                prev = merged[-1]
                prev_key = (
                    prev["status"],
                    tuple(prev["reporters"]),
                    tuple(prev["shifts"]),
                    prev["liable_factory"],
                )
                if prev_key == key and prev["end"] == seg["start"]:
                    prev["end"] = seg["end"]
                    prev["end_minute"] = seg["end_minute"]
                    prev["minutes"] += seg["minutes"]
                    prev["evidence_ids"] = sorted(set(prev["evidence_ids"] + seg["evidence_ids"]))
                    continue
            merged.append(dict(seg))
        return merged

    def attendance_view(self, person_id: str | None = None, shift_id: str | None = None, viewer=None) -> Any:
        if person_id:
            self.require_person(person_id, viewer) if viewer and self.viewer(viewer).get("type") == "person" else None
            segments = self._attendance_segments_raw(person_id, shift_id)
            for seg in segments:
                dispute = self._find_dispute(person_id, seg["start"], seg["end"])
                seg["dispute_id"] = dispute["id"] if dispute else self._temp_dispute_id(person_id, seg)
                if dispute and dispute.get("status") == "resolved":
                    seg["status"] = "resolved"
                    seg["liable_factory"] = dispute.get("liable_factory")
                seg.pop("start_minute", None)
                seg.pop("end_minute", None)
            return {"person": person_id, "segments": segments}
        people = sorted({a["person"] for a in self.db["attendance"]})
        return [self.attendance_view(p, shift_id, viewer={"type": "admin"}) for p in people]

    @staticmethod
    def _temp_dispute_id(person_id: str, seg: dict[str, Any]) -> str:
        return f"D:{person_id}:{seg['start']}-{seg['end']}"

    def _find_dispute(self, person_id: str, start: str, end: str) -> dict[str, Any] | None:
        return next(
            (
                d
                for d in self.db["disputes"]
                if d.get("person") == person_id and d.get("start") == start and d.get("end") == end
            ),
            None,
        )

    def sync_disputes(self, person_id: str | None = None) -> list[dict[str, Any]]:
        created = []
        people = [person_id] if person_id else sorted({a["person"] for a in self.db["attendance"]})
        for pid in people:
            for seg in self._attendance_segments_raw(pid):
                if not seg["disputed"]:
                    continue
                start, end = seg["start"], seg["end"]
                if self._find_dispute(pid, start, end):
                    continue
                d = {
                    "id": self.next_id("D", "disputes"),
                    "kind": "attribution",
                    "person": pid,
                    "start": start,
                    "end": end,
                    "minutes": seg["minutes"],
                    "reporters": seg["reporters"],
                    "evidence_ids": seg["evidence_ids"],
                    "status": "open",
                    "liable_factory": None,
                    "confirmations": {},
                }
                self.db["disputes"].append(d)
                created.append(d)
        return created

    def list_disputes(self, viewer=None) -> list[dict[str, Any]]:
        v = self.viewer(viewer)
        rows = self.db["disputes"]
        if v.get("type") == "factory":
            factory = v["id"]
            rows = [d for d in rows if factory in d.get("reporters", []) or factory in self._dispute_factories(d)]
        return [self._public(d) for d in rows]

    def _dispute_factories(self, d: dict[str, Any]) -> list[str]:
        factories = set(d.get("reporters", []))
        for eid in d.get("evidence_ids", []):
            row = next((a for a in self.db["attendance"] if a["id"] == eid), None)
            if row:
                shift = next((s for s in self.db["shifts"] if s["id"] == row.get("shift")), None)
                if shift:
                    factories.add(shift["host"])
                person = next((p for p in self.db["people"] if p["id"] == row["person"]), None)
                if person:
                    factories.add(person["origin"])
        return sorted(factories)

    def check_dispute(self, dispute_id: str, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        d = self._get("disputes", dispute_id)
        factory = payload.get("factory") or (viewer or {}).get("id")
        if not factory:
            raise ValidationError("factory is required")
        involved = self._dispute_factories(d)
        self.require_factory(factory, viewer)
        if factory not in involved:
            raise PermissionDeniedError("Only the two checking factories may confirm this dispute")
        d.setdefault("confirmations", {})[factory] = {
            "agrees": bool(payload.get("agrees", True)),
            "liable_factory": payload.get("liable_factory"),
            "at": payload.get("at") or self.now_label(),
        }
        confirmations = d["confirmations"]
        positive = [c for c in confirmations.values() if c.get("agrees")]
        proposed = {c.get("liable_factory") for c in positive if c.get("liable_factory")}
        if len(positive) >= 2 and (len(proposed) == 1 or payload.get("liable_factory")):
            liable = payload.get("liable_factory") or next(iter(proposed))
            if len(proposed) <= 1:
                d["status"] = "resolved"
                d["liable_factory"] = liable
                d["resolved_at"] = self.now_label(payload.get("at"))
        self.save()
        return self._public(d)

    def resolve_dispute(self, dispute_id: str, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        v = self.viewer(viewer)
        if v.get("type") != "admin":
            raise PermissionDeniedError("Bilateral checks are required; an administrator may record a final resolution")
        d = self._get("disputes", dispute_id)
        d["status"] = "resolved"
        d["liable_factory"] = payload["liable_factory"]
        d["resolution_note"] = payload.get("note")
        d["resolved_at"] = payload.get("at") or self.now_label()
        self.save()
        return self._public(d)

    # --------------------------------------------------------------- pay
    def applicable_terms(self, person_id: str, shift_id: str) -> dict[str, Any]:
        state = self.agreement_state(person_id, shift_id)
        terms_id = state.get("accepted_terms") or state["current_terms"]
        return self.term(terms_id)

    def calculate_wage(self, person_id: str, shift_id: str, start: str | int, end: str | int, terms_id: str | None = None) -> dict[str, Any]:
        s, e = parse_minute(start, "start"), parse_minute(end, "end")
        if e <= s:
            raise ValidationError("coverage end must be after start")
        terms = self.term(terms_id) if terms_id else self.applicable_terms(person_id, shift_id)
        minutes = e - s
        amount = round_cents_half_up(minutes * int(terms["cents_hour"]), 60)
        return {
            "terms": terms["id"],
            "cents_hour": terms["cents_hour"],
            "minutes": minutes,
            "calculated_amount": amount,
            "currency": "CNY_CENT",
        }

    def list_pay_acks(self, viewer=None, person_id: str | None = None) -> list[dict[str, Any]]:
        v = self.viewer(viewer)
        rows = self.db["pay_ack"]
        if person_id:
            if v.get("type") == "person":
                self.require_person(person_id, v)
            rows = [m for m in rows if m["person"] == person_id]
        if v.get("type") == "factory":
            factory = v["id"]
            rows = [m for m in rows if m.get("liable_factory", m["reporter"]) == factory]
        elif v.get("type") == "person" and not person_id:
            rows = [m for m in rows if m["person"] == v["id"]]
        return [self._public(m) for m in rows]

    def add_pay_ack(self, payload: dict[str, Any], viewer=None) -> dict[str, Any]:
        person_id = payload["person"]
        shift_id = payload["shift"]
        reporter = payload.get("reporter") or (viewer or {}).get("id")
        if not reporter:
            raise ValidationError("reporter is required")
        self.person(person_id)
        self.shift(shift_id)
        self.require_factory(reporter, viewer)
        try:
            start, end = parse_minute(payload["coverage_start"], "coverage_start"), parse_minute(payload["coverage_end"], "coverage_end")
        except (KeyError, ValueError) as exc:
            raise ValidationError("valid coverage_start and coverage_end are required", reason=str(exc)) from exc
        amount = int(payload["amount"])
        ack = payload.get("ack") or self.next_id("ACK", "pay_ack")
        existing_ack = next((m for m in self.db["pay_ack"] if m.get("ack") == ack), None)
        if existing_ack:
            same = all(existing_ack.get(k) == payload.get(k) for k in ("person", "shift", "reporter", "amount", "coverage_start", "coverage_end"))
            if same:
                return {"pay_ack": self._public(existing_ack), "idempotent": True}
            raise ConflictError("ack business number already used with different content", ack=ack)
        calc = self.calculate_wage(person_id, shift_id, start, end, payload.get("terms"))
        prior = [
            m
            for m in self.db["pay_ack"]
            if m["person"] == person_id and m["shift"] == shift_id and m.get("liable_factory", m["reporter"]) == payload.get("liable_factory", reporter)
        ]
        prior_union = merge_intervals(
            (parse_minute(m["coverage_start"]), parse_minute(m["coverage_end"])) for m in prior
        )
        overlap = sum(overlap_minutes((start, end), interval) for interval in prior_union)
        prior_amount = sum(int(m["amount"]) for m in prior)
        if payload.get("settlement_mode") == "delta" or payload.get("delta_amount") is not None:
            incremental = int(payload.get("delta_amount", amount))
            reconciliation_mode = "explicit_delta"
        elif payload.get("cumulative"):
            incremental = amount - prior_amount
            reconciliation_mode = "cumulative_total_minus_confirmed"
        elif overlap == 0:
            incremental = amount
            reconciliation_mode = "new_interval"
        else:
            # Do not repost the overlapping whole interval.  Recognize newly
            # covered minutes plus any verified correction difference.
            uncovered_minutes = max(0, (end - start) - overlap)
            uncovered_calc = round_cents_half_up(uncovered_minutes * int(self.term(calc["terms"])["cents_hour"]), 60)
            incremental = uncovered_calc + (amount - calc["calculated_amount"])
            reconciliation_mode = "overlap_reconcile_difference_only"
        row = {
            "id": payload.get("id") or self.next_id("M", "pay_ack"),
            "person": person_id,
            "shift": shift_id,
            "reporter": reporter,
            "coverage_start": format_minute(start),
            "coverage_end": format_minute(end),
            "amount": amount,
            "ack": ack,
            "liable_factory": payload.get("liable_factory", reporter),
            "kind": payload.get("kind", "wage"),
            "settlement_mode": reconciliation_mode,
            "incremental_amount": incremental,
            "received": payload.get("received") or self.now_label(),
            "_seq": len(self.db["pay_ack"]),
        }
        if payload.get("liability_id"):
            row["liability_id"] = payload["liability_id"]
        self.db["pay_ack"].append(row)
        if row.get("liability_id"):
            liability = self._get("liabilities", row["liability_id"])
            liability["status"] = "confirmed"
            liability["pay_ack"] = ack
        elif row["kind"] == "compensation":
            liability = next(
                (x for x in self.db["liabilities"] if x.get("source_recall") == payload.get("recall") and x["person"] == person_id),
                None,
            )
            if liability:
                liability["status"] = "confirmed"
                liability["pay_ack"] = ack
        result = {
            "pay_ack": self._public(row),
            "verification": {
                **calc,
                "amount_difference": amount - calc["calculated_amount"],
                "status": "matched" if amount == calc["calculated_amount"] else "amount_mismatch",
            },
            "reconciliation": {
                "mode": reconciliation_mode,
                "prior_ack_count": len(prior),
                "prior_confirmed_amount": prior_amount,
                "overlap_minutes": overlap,
                "incremental_settlement_amount": incremental,
                "do_not_pay_full_shift_again": True,
            },
        }
        self._audit("pay_ack_received", id=row["id"], ack=ack, incremental=incremental)
        self.save()
        return result

    def settlement(
        self,
        person_id: str | None = None,
        shift_id: str | None = None,
        factory: str | None = None,
        viewer=None,
    ) -> dict[str, Any]:
        v = self.viewer(viewer)
        if v.get("type") == "factory":
            if factory and factory != v["id"]:
                raise PermissionDeniedError("Factory viewer cannot request another factory's settlement")
            factory = v["id"]
        if v.get("type") == "person":
            if person_id and person_id != v["id"]:
                raise PermissionDeniedError("Person viewer cannot request another worker's settlement")
            person_id = v["id"]
        acks = [m for m in self.db["pay_ack"]]
        if person_id:
            acks = [m for m in acks if m["person"] == person_id]
        if shift_id:
            acks = [m for m in acks if m["shift"] == shift_id]
        if factory:
            acks = [m for m in acks if m.get("liable_factory", m["reporter"]) == factory]
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for m in acks:
            groups[(m["person"], m["shift"], m.get("liable_factory", m["reporter"]))].append(m)
        wage_groups = []
        total_wages = 0
        for (pid, sid, liable), rows in sorted(groups.items()):
            rows.sort(key=lambda m: self._consent_sort_key(m) if "at" in m else (parse_minute(m.get("received", "00:00")), m.get("_seq", 0)))
            intervals = []
            recognized = 0
            vouchers = []
            for m in rows:
                s, e = parse_minute(m["coverage_start"]), parse_minute(m["coverage_end"])
                prior_union = merge_intervals(intervals)
                overlap = sum(overlap_minutes((s, e), iv) for iv in prior_union)
                if "incremental_amount" in m:
                    recognized += int(m["incremental_amount"])
                elif m.get("cumulative"):
                    recognized = int(m["amount"])
                elif overlap == 0 or m.get("settlement_mode") == "delta":
                    recognized += int(m["amount"])
                # An overlapping legacy ordinary voucher is reconciliation
                # evidence; settlement does not add its whole amount again.
                intervals.append((s, e))
                calc = self.calculate_wage(pid, sid, s, e, m.get("terms"))
                vouchers.append(
                    {
                        **self._public(m),
                        "overlap_minutes_with_prior_acks": overlap,
                        "calculated_amount": calc["calculated_amount"],
                        "verification_difference": int(m["amount"]) - calc["calculated_amount"],
                    }
                )
            union = merge_intervals(intervals)
            minutes = sum(e - s for s, e in union)
            # Rate is constant per accepted/current version in these records.
            try:
                rate_terms = self.applicable_terms(pid, sid)
                calculated = round_cents_half_up(minutes * int(rate_terms["cents_hour"]), 60)
            except LaborError:
                calculated = None
            total_wages += recognized
            wage_groups.append(
                {
                    "person": pid,
                    "shift": sid,
                    "liable_factory": liable,
                    "coverage": [{"start": format_minute(s), "end": format_minute(e), "minutes": e - s} for s, e in union],
                    "confirmed_minutes": minutes,
                    "confirmed_amount": recognized,
                    "rate_calculated_amount": calculated,
                    "difference": None if calculated is None else recognized - calculated,
                    "vouchers": vouchers,
                    "full_shift_repost_prevented": True,
                }
            )
        liabilities = [x for x in self.db["liabilities"]]
        if person_id:
            liabilities = [x for x in liabilities if x["person"] == person_id]
        if shift_id:
            liabilities = [x for x in liabilities if x["shift"] == shift_id]
        if factory:
            liabilities = [x for x in liabilities if x["liable_factory"] == factory]
        confirmed_comp = sum(int(x["amount"]) for x in liabilities if x.get("status") == "confirmed")
        pending_comp = sum(int(x["amount"]) for x in liabilities if x.get("status") != "confirmed")
        return {
            "factory": factory,
            "person": person_id,
            "shift": shift_id,
            "wage_groups": wage_groups,
            "total_confirmed_wages": total_wages,
            "compensation": [self._public(x) for x in liabilities],
            "confirmed_compensation": confirmed_comp,
            "pending_compensation": pending_comp,
            "confirmed_total": total_wages + confirmed_comp,
            "pending_total": pending_comp,
            "currency": "CNY_CENT",
        }

    def acknowledge_liability(self, liability_id: str, payload: dict[str, Any] | None = None, viewer=None) -> dict[str, Any]:
        payload = payload or {}
        liability = self._get("liabilities", liability_id)
        self.require_factory(liability["liable_factory"], viewer)
        liability["status"] = "confirmed"
        liability["pay_ack"] = payload.get("ack") or liability.get("pay_ack") or self.next_id("ACK", "pay_ack")
        liability["confirmed_at"] = payload.get("at") or self.now_label()
        if payload.get("note"):
            liability["confirmation_note"] = payload["note"]
        self._audit("liability_confirmed", liability=liability_id, pay_ack=liability["pay_ack"])
        self.save()
        return self._public(liability)

    def pay_difference(self, person_id: str, shift_id: str, revised_interval: dict[str, str] | None = None, revised_amount: int | None = None) -> dict[str, Any]:
        settlement = self.settlement(person_id=person_id, shift_id=shift_id, viewer={"type": "admin"})
        already = int(settlement["total_confirmed_wages"])
        if revised_interval:
            calc = self.calculate_wage(person_id, shift_id, revised_interval["start"], revised_interval["end"])
            total_due = revised_amount if revised_amount is not None else calc["calculated_amount"]
            interval = revised_interval
        else:
            attendance = self.attendance_view(person_id, shift_id, viewer={"type": "admin"})
            segments = attendance.get("segments", []) if isinstance(attendance, dict) else []
            if segments:
                start = min(parse_minute(seg["start"]) for seg in segments)
                end = max(parse_minute(seg["end"]) for seg in segments)
                interval = {"start": format_minute(start), "end": format_minute(end)}
            else:
                shift = self.shift(shift_id)
                interval = {"start": shift["start"], "end": shift["end"]}
            calc = self.calculate_wage(person_id, shift_id, interval["start"], interval["end"])
            total_due = revised_amount if revised_amount is not None else calc["calculated_amount"]
        return {
            "person": person_id,
            "shift": shift_id,
            "revised_interval": interval,
            "already_confirmed_amount": already,
            "revised_total_amount": total_due,
            "difference_only_amount": total_due - already,
            "instruction": "Settle difference_only_amount; do not initiate a second full-shift wage",
        }

    # --------------------------------------------------------------- queries
    def worker_record(self, person_id: str, viewer=None, at=None) -> dict[str, Any]:
        v = self.viewer(viewer)
        if v.get("type") == "person":
            self.require_person(person_id, v)
        elif v.get("type") == "factory":
            raise PermissionDeniedError(
                "Factories must use /api/factories/{id}; the worker record contains all parties' personal settlement information"
            )
        # admin/system/local callers with viewer=None retain the operational report.
        person = self.person(person_id)
        commitments = self.list_commitments(person_id)
        attendance = self.attendance_view(person_id)
        pay = self.settlement(person_id=person_id, viewer={"type": "admin"})
        conflicts = self.availability_conflicts(person_id)
        commitment_conflicts = []
        # Surface source conflicts without mutating snapshot facts.
        for i, a in enumerate(commitments):
            for b in commitments[i + 1 :]:
                minutes = overlap_minutes((a["start_minute"], a["end_minute"]), (b["start_minute"], b["end_minute"]))
                if minutes > 0:
                    commitment_conflicts.append(
                        {
                            "between": [a.get("shift") or a["id"], b.get("shift") or b["id"]],
                            "factories": [a["factory"], b["factory"]],
                            "start": format_minute(max(a["start_minute"], b["start_minute"])),
                            "end": format_minute(min(a["end_minute"], b["end_minute"])),
                            "overlap_minutes": minutes,
                        }
                    )
        next_work = [c for c in commitments if c["end_minute"] > self.now_minute(at)] if at is not None or self.clock is not None else commitments
        promises_by_factory: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in commitments:
            item = {k: v for k, v in c.items() if not k.endswith("_minute")}
            promises_by_factory[c["factory"]].append(item)
        pending = self._worker_pending(person_id, attendance, pay)
        recognized = []
        for seg in attendance["segments"]:
            recognized.append(
                {
                    "start": seg["start"],
                    "end": seg["end"],
                    "minutes": seg["minutes"],
                    "status": seg["status"],
                    "liable_factory": seg["liable_factory"],
                    "evidence_ids": seg["evidence_ids"],
                    "dispute_id": seg.get("dispute_id"),
                }
            )
        return {
            "person": self._public(person),
            "as_of": self.now_label(at),
            "next_work": [{k: v for k, v in c.items() if not k.endswith("_minute")} for c in next_work],
            "work_record": [
                {
                    "shift": c.get("shift"),
                    "location": c.get("location") or c["factory"],
                    "place": c.get("place") or c["factory"],
                    "start": c["start"],
                    "end": c["end"],
                    "pay": c.get("pay_terms"),
                    "cents_hour": (c.get("pay_terms") or {}).get("cents_hour"),
                    "responsible_unit": c["responsible_units"].get("workplace_management") or c["factory"],
                    "responsible_units": c["responsible_units"],
                    "status": self._commitment_status(c),
                }
                for c in commitments
            ],
            "promises_by_factory": dict(promises_by_factory),
            "recognized_hours": recognized,
            "attendance": attendance,
            "pay": pay,
            "pending_money_or_evidence": pending,
            "availability_conflicts": conflicts,
            "commitment_conflicts": commitment_conflicts,
        }

    @staticmethod
    def _commitment_status(c: dict[str, Any]) -> str:
        if c["kind"] == "own_shift":
            return "origin_own_shift"
        if not c.get("current_terms_accepted"):
            return "terms_change_needs_reconfirmation"
        return "confirmed_for_worker"

    def _worker_pending(self, person_id: str, attendance: dict[str, Any], pay: dict[str, Any]) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        ack_intervals = merge_intervals(
            (parse_minute(m["coverage_start"]), parse_minute(m["coverage_end"]))
            for m in self.db["pay_ack"]
            if m["person"] == person_id and m.get("kind", "wage") == "wage"
        )
        for seg in attendance["segments"]:
            s, e = parse_minute(seg["start"]), parse_minute(seg["end"])
            uncovered = subtract_intervals((s, e), ack_intervals)
            for us, ue in uncovered:
                pending.append(
                    {
                        "type": "wage_confirmation_missing",
                        "start": format_minute(us),
                        "end": format_minute(ue),
                        "minutes": ue - us,
                        "attendance_status": seg["status"],
                        "liable_factory": seg["liable_factory"],
                        "message": "Actual hours are evidenced but the factory has not sent a pay acknowledgement",
                    }
                )
            if seg["status"] == "attribution_disputed":
                pending.append(
                    {
                        "type": "factory_attribution_check",
                        "start": seg["start"],
                        "end": seg["end"],
                        "dispute_id": seg.get("dispute_id"),
                        "message": "Both factories must check which unit bears this interval",
                    }
                )
        for comp in pay["compensation"]:
            if comp.get("status") != "confirmed":
                pending.append(
                    {
                        "type": "compensation_awaiting_confirmation",
                        "source_recall": comp.get("source_recall"),
                        "amount": comp.get("amount"),
                        "liable_factory": comp.get("liable_factory"),
                        "message": "Fixed recall compensation is recorded but no pay acknowledgement exists",
                    }
                )
        for group in pay["wage_groups"]:
            for voucher in group["vouchers"]:
                if voucher.get("verification_difference") not in (0, None):
                    pending.append(
                        {
                            "type": "pay_amount_mismatch",
                            "ack": voucher.get("ack"),
                            "difference": voucher.get("verification_difference"),
                        }
                    )
        return pending

    def factory_board(self, factory_id: str, viewer=None, at=None) -> dict[str, Any]:
        self.require_factory(factory_id, viewer)
        factory = next((f for f in self.db["factories"] if f["id"] == factory_id), {"id": factory_id})
        host_shifts = [s for s in self.db["shifts"] if s["host"] == factory_id]
        staffings = [self.staffing(s["id"], at) for s in host_shifts]
        own_people = [p for p in self.db["people"] if p["origin"] == factory_id]
        origin_commitments = []
        for p in own_people:
            for c in self.list_commitments(p["id"]):
                if c["kind"] == "shared_loan":
                    item = {k: v for k, v in c.items() if not k.endswith("_minute")}
                    # Origin manages recall/cutoff but does not need the host's
                    # commercial hourly rate unless it is the cost bearer.
                    if c["responsible_units"].get("wage") != factory_id:
                        if item.get("pay_terms"):
                            item["pay_terms"] = {**item["pay_terms"], "cents_hour": None, "hourly_rate_redacted": True}
                    origin_commitments.append(item)
        recall_ids = {
            r["id"]
            for r in self.db["recalls"]
            if self.shift(r["shift"])["host"] == factory_id or self.person(r["person"])["origin"] == factory_id
        }
        recalls = [self._redact_recall_for_factory(self.recall_view(rid, at), factory_id) for rid in sorted(recall_ids)]
        wage_settlement = self.settlement(factory=factory_id, viewer={"type": "admin"})
        disputes = self.list_disputes(viewer={"type": "factory", "id": factory_id})
        shortfalls = [s for s in self.db["shortfalls"] if s.get("host") == factory_id]
        own_ids = {p["id"] for p in own_people}
        host_shift_ids = {s["id"] for s in host_shifts}
        attendance_rows = [
            self._public(a)
            for a in self.db["attendance"]
            if a.get("reporter") == factory_id
            or a.get("person") in own_ids
            or a.get("shift") in host_shift_ids
        ]
        attendance_reconciliation = [
            self.attendance_view(pid, viewer={"type": "admin"})
            for pid in sorted(own_ids | {a["person"] for a in self.db["attendance"] if a.get("shift") in host_shift_ids})
        ]
        return {
            "factory": self._public(factory),
            "as_of": self.now_label(at),
            "host_shifts": [self._public(s) for s in host_shifts],
            "staffing": staffings,
            "openable_shift_count": sum(1 for x in staffings if x["can_open"]),
            "current_openable_count": sum(1 for x in staffings if x.get("openable_now")),
            "own_workers": [self._public(p) for p in own_people],
            "availability": [self._public(a) for a in self.db["availability"] if a.get("factory") == factory_id],
            "origin_worker_commitments": origin_commitments,
            "recalls": recalls,
            "attendance": attendance_rows,
            "attendance_reconciliation": attendance_reconciliation,
            "wage_settlement": wage_settlement,
            "wage_items_only_own_factory": wage_settlement["wage_groups"],
            "compensation_liabilities": wage_settlement["compensation"],
            "shortfalls": [self._public(s) for s in shortfalls],
            "disputes": disputes,
        }


# Descriptive aliases for integrations and tests.
LaborService = Service
SharedLaborService = Service
