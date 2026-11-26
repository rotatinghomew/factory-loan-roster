from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared_labor import ConflictError, GatewayFailure, PermissionDeniedError, Service  # noqa: E402
from shared_labor.gateway import FailingGateway, LocalGateway  # noqa: E402
from shared_labor.server import create_server  # noqa: E402

DATA = ROOT / "data" / "labor_20261203.json"


def seed_service(**kwargs):
    return Service(json.loads(DATA.read_text(encoding="utf-8")), **kwargs)


def basic_service(gateway=None):
    s = Service(gateway=gateway or LocalGateway(), clock="09:00")
    for person_id, skills in {
        "W1": ["machine", "lead"],
        "W2": ["machine"],
        "W3": ["lead"],
        "W4": ["machine", "lead"],
    }.items():
        s.add_person({"id": person_id, "origin": "OR", "skills": skills})
        s.add_availability({"person": person_id, "start": "08:00", "end": "18:00", "kind": "lend"})
    return s


class DomainRuleTests(unittest.TestCase):
    def test_team_requires_headcount_and_skill_coverage(self):
        s = basic_service()
        s.create_shift(
            {
                "id": "S1",
                "host": "FB",
                "start": "09:00",
                "end": "13:00",
                "min_people": 2,
                "required_skills": ["lead", "machine"],
                "terms": {"place": "west", "cents_hour": 3000, "recall_cutoff": "10:10", "recall_compensation": 1000},
            }
        )
        # The multi-skilled worker covers both skills but not the headcount.
        s.record_consent({"person": "W1", "terms": "T1", "decision": "accept", "at": "08:00"})
        report = s.staffing("S1")
        self.assertFalse(report["can_open"])
        self.assertEqual(report["segments"][0]["missing_skills"], [])
        self.assertFalse(report["segments"][0]["headcount_complete"])

        # With W1 withdrawn, two machine-only workers still lack lead.
        s.record_consent({"person": "W1", "terms": "T1", "decision": "withdraw", "at": "08:01"})
        s.record_consent({"person": "W2", "terms": "T1", "decision": "accept", "at": "08:02"})
        s.add_person({"id": "W5", "origin": "OR", "skills": ["machine"]})
        s.add_availability({"person": "W5", "start": "08:00", "end": "18:00"})
        s.record_consent({"person": "W5", "terms": "T1", "decision": "accept", "at": "08:03"})
        segment = s.staffing("S1")["segments"][0]
        self.assertFalse(segment["can_open"])
        self.assertIn("lead", segment["missing_skills"])

        # A lead-only worker completes the certified combination.
        s.record_consent({"person": "W3", "terms": "T1", "decision": "accept", "at": "08:04"})
        self.assertTrue(s.staffing("S1")["can_open"])

    def test_overlapping_promise_to_another_party_is_rejected(self):
        s = basic_service()
        for sid, host, start, end in (
            ("S1", "FB", "09:00", "12:00"),
            ("S2", "FC", "10:00", "11:00"),
        ):
            s.create_shift(
                {
                    "id": sid,
                    "host": host,
                    "start": start,
                    "end": end,
                    "min_people": 1,
                    "required_skills": [],
                    "terms": {"place": "site-" + sid, "cents_hour": 3000, "recall_cutoff": "09:30", "recall_compensation": 500},
                }
            )
        s.record_consent({"person": "W1", "terms": "T1", "decision": "accept"})
        with self.assertRaises(ConflictError) as ctx:
            s.record_consent({"person": "W1", "shift": "S2", "decision": "accept"})
        self.assertIn("one party", ctx.exception.message)

        # An own retained shift is also an existing promise.
        s.add_availability({"person": "W2", "start": "11:30", "end": "12:30", "kind": "own"})
        with self.assertRaises(ConflictError):
            s.record_consent({"person": "W2", "terms": "T1", "decision": "accept"})

    def test_changed_pay_place_or_time_requires_new_consent(self):
        s = basic_service()
        s.create_shift(
            {
                "id": "S1",
                "host": "FB",
                "start": "09:00",
                "end": "13:00",
                "min_people": 1,
                "required_skills": [],
                "terms": {"place": "west", "cents_hour": 2900, "recall_cutoff": "10:10", "recall_compensation": 1200},
            }
        )
        s.record_consent({"person": "W1", "terms": "T1", "decision": "accept", "at": "07:15"})
        self.assertTrue(s.staffing("S1")["can_open"])

        revised = s.revise_terms("S1", {"cents_hour": 3300, "place": "east"})
        self.assertEqual(revised["terms"]["revision"], 2)
        self.assertIn("W1", revised["reasked"])
        self.assertTrue(s.agreement_state("W1", "S1")["needs_reconfirmation"])
        self.assertFalse(s.staffing("S1")["can_open"])

        with self.assertRaises(ConflictError):
            s.record_consent({"person": "W1", "terms": "T1", "decision": "accept"})
        s.record_consent({"person": "W1", "terms": "T2", "decision": "accept", "at": "08:05"})
        self.assertTrue(s.staffing("S1")["can_open"])

        # A time change creates another version and again invalidates current staffing.
        s.revise_terms("S1", {"start": "09:30", "end": "13:30"})
        self.assertEqual(s.current_terms("S1")["revision"], 3)
        self.assertFalse(s.staffing("S1")["can_open"])

    def test_uncommitted_offer_can_be_revoked_but_accepted_worker_uses_recall(self):
        s = basic_service()
        s.create_shift(
            {
                "id": "S1",
                "host": "FB",
                "start": "09:00",
                "end": "13:00",
                "min_people": 1,
                "required_skills": [],
                "terms": {"place": "west", "cents_hour": 3000, "recall_cutoff": "10:10", "recall_compensation": 500},
            }
        )
        asked = s.ask_consent("S1", {"person": "W1"})
        revoked = s.revoke_uncommitted({"person": "W1", "shift": "S1"})
        self.assertEqual(revoked["revoked_requests"], [asked["id"]])
        self.assertEqual(s.db["consent_requests"][-1]["status"], "revoked_before_commitment")

        s.ask_consent("S1", {"person": "W2"})
        s.record_consent({"person": "W2", "terms": "T1", "decision": "accept"})
        with self.assertRaises(ConflictError):
            s.revoke_uncommitted({"person": "W2", "shift": "S1"})

    def test_pending_replacement_or_gateway_failure_does_not_fake_agreement(self):
        s = basic_service(gateway=FailingGateway())
        s.create_shift(
            {
                "id": "S1",
                "host": "FB",
                "start": "09:00",
                "end": "13:00",
                "min_people": 2,
                "required_skills": ["machine", "lead"],
                "terms": {"place": "west", "cents_hour": 3300, "recall_cutoff": "10:10", "recall_compensation": 1200},
            }
        )
        s.record_consent({"person": "W1", "terms": "T1", "decision": "accept", "at": "08:00"})
        s.record_consent({"person": "W2", "terms": "T1", "decision": "accept", "at": "08:01"})
        recall = s.request_recall({"person": "W1", "shift": "S1", "requested": "09:50", "effective": "10:15"})

        with self.assertRaises(GatewayFailure):
            s.request_replacement(recall["id"], {"person": "W4"}, viewer="FB")
        view = s.recall_view(recall["id"])
        self.assertEqual(view["replacement_status"], "interface_failed")
        self.assertFalse(view["worker_agreed"])
        self.assertEqual(view["worker_return_consent"], "not_agreed")
        self.assertTrue(all(c["decision"] != "withdraw" for c in s.db["consents"] if c["person"] == "W1"))

        s.gateway = LocalGateway()
        s.retry_replacement_request(recall["id"], viewer="FB")
        self.assertEqual(s.recall_view(recall["id"])["replacement_status"], "awaiting_response")

        s.record_consent({"person": "W4", "terms": "T1", "decision": "accept", "at": "10:01"})
        s.host_confirm_recall(recall["id"], {"replacement": "W4", "at": "10:04"}, viewer="FB")
        s.worker_respond_recall(recall["id"], {"decision": "accept", "at": "10:14"}, viewer={"type": "person", "id": "W1"})
        view = s.recall_view(recall["id"])
        self.assertEqual(view["status"], "completed_return")
        self.assertTrue(view["normal_return_complete"])
        self.assertEqual(s.assignment_interval("W1", "S1"), (9 * 60, 10 * 60 + 15))
        self.assertEqual(s.assignment_interval("W4", "S1"), (10 * 60 + 15, 13 * 60))
        self.assertTrue(all(seg["compliant"] for seg in s.staffing("S1")["segments"]))

    def test_cutoff_force_stop_records_shortfall_and_compensation_without_fake_consent(self):
        s = seed_service(clock="13:10")
        with self.assertRaises(ConflictError):
            s.force_stop_recall("R2", {"at": "10:05"})
        result = s.force_stop_recall("R2", {"at": "10:15"})
        self.assertEqual(result["status"], "force_stopped")
        self.assertFalse(result["worker_agreed"])
        self.assertEqual(result["worker_return_consent"], "not_agreed_but_contractually_stopped")
        c3 = next(c for c in s.db["consents"] if c["id"] == "C3")
        self.assertEqual(c3["decision"], "accept")
        self.assertEqual(result["created_liability"]["amount"], 1200)
        self.assertEqual(result["created_liability"]["status"], "awaiting_pay_confirmation")
        self.assertTrue(result["shortfalls"])
        self.assertEqual(result["shortfalls"][0]["actual_people"], 1)
        self.assertEqual(result["shortfalls"][0]["required_people"], 2)
        self.assertFalse(all(seg["compliant"] for seg in s.staffing("S1")["segments"]))


class ExchangeReconciliationTests(unittest.TestCase):
    def test_seed_staffing_versions_and_recall_state(self):
        s = seed_service(clock="13:10")
        staffing = s.staffing("S1")
        self.assertEqual(staffing["current_terms"], "T2")
        self.assertTrue(staffing["can_open"])
        roster = [
            [(p["person"]) for p in seg["eligible"]]
            for seg in staffing["segments"]
            if seg["start"] == "10:15"
        ][0]
        self.assertEqual(roster, ["W2", "W4"])
        r1 = s.recall_view("R1")
        self.assertEqual(r1["status"], "completed_return")
        self.assertTrue(r1["replacement_check"]["compliant"])

    def test_attendance_overlap_is_disputed_but_clean_intervals_confirmed(self):
        s = seed_service()
        segments = s.attendance_view("W1")["segments"]
        compact = [(x["start"], x["end"], x["status"]) for x in segments]
        self.assertEqual(
            compact,
            [
                ("09:00", "10:15", "confirmed"),
                ("10:15", "10:17", "attribution_disputed"),
                ("10:17", "10:50", "confirmed"),
            ],
        )
        dispute = s.list_disputes()[0]
        self.assertEqual((dispute["start"], dispute["end"]), ("10:15", "10:17"))
        resolved = s.resolve_dispute(dispute["id"], {"liable_factory": "FB"}, viewer={"type": "admin"})
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(s.attendance_view("W1")["segments"][1]["status"], "resolved")

    def test_pay_acks_verify_difference_and_prevent_full_shift_repost(self):
        s = seed_service()
        settlement = s.settlement(person_id="W1", shift_id="S1")
        group = settlement["wage_groups"][0]
        self.assertEqual(group["confirmed_amount"], 4235)
        self.assertEqual(group["confirmed_minutes"], 77)
        amounts = {v["ack"]: v["calculated_amount"] for v in group["vouchers"]}
        self.assertEqual(amounts["BASE-W1"], 4125)
        self.assertEqual(amounts["DIFF-W1"], 110)

        diff = s.pay_difference("W1", "S1", {"start": "09:00", "end": "10:17"}, 4300)
        self.assertEqual(diff["already_confirmed_amount"], 4235)
        self.assertEqual(diff["difference_only_amount"], 65)

        result = s.add_pay_ack(
            {
                "id": "M3",
                "ack": "CUM-W1",
                "person": "W1",
                "shift": "S1",
                "reporter": "FB",
                "coverage_start": "09:00",
                "coverage_end": "10:17",
                "amount": 4235,
                "cumulative": True,
            },
            viewer="FB",
        )
        self.assertEqual(result["reconciliation"]["incremental_settlement_amount"], 0)
        self.assertEqual(s.settlement(person_id="W1", shift_id="S1")["total_confirmed_wages"], 4235)

    def test_factory_privacy_scopes_wage_records(self):
        s = seed_service()
        s.add_pay_ack(
            {
                "id": "M9",
                "ack": "OR-W1-OWN",
                "person": "W1",
                "shift": "S1",
                "reporter": "OR",
                "coverage_start": "10:17",
                "coverage_end": "10:50",
                "amount": 1815,
            },
            viewer="OR",
        )
        fb = s.factory_board("FB")
        or_board = s.factory_board("OR")
        fb_r1 = next(r for r in fb["recalls"] if r["id"] == "R1")
        self.assertTrue(fb_r1["compensation"]["amount_redacted"])
        or_r1 = next(r for r in or_board["recalls"] if r["id"] == "R1")
        self.assertEqual(or_r1["compensation"]["amount"], 1200)
        self.assertEqual({row["ack"] for row in fb["wage_items_only_own_factory"][0]["vouchers"]}, {"BASE-W1", "DIFF-W1"})
        or_acks = {v["ack"] for g in or_board["wage_items_only_own_factory"] for v in g["vouchers"]}
        self.assertEqual(or_acks, {"OR-W1-OWN"})
        with self.assertRaises(PermissionDeniedError):
            s.factory_board("OR", viewer="FB")
        worker = s.worker_record("W1", viewer={"type": "person", "id": "W1"})
        self.assertEqual(worker["pay"]["total_confirmed_wages"], 6050)
        with self.assertRaises(PermissionDeniedError):
            s.worker_record("W2", viewer={"type": "person", "id": "W1"})

    def test_worker_record_has_location_pay_units_and_pending_evidence(self):
        s = seed_service(clock="13:10")
        record = s.worker_record("W1", viewer={"type": "person", "id": "W1"})
        s1 = next(x for x in record["work_record"] if x["shift"] == "S1")
        self.assertEqual(s1["location"], "east")
        self.assertEqual(s1["cents_hour"], 3300)
        self.assertEqual(s1["responsible_units"]["employer"], "OR")
        self.assertEqual(s1["responsible_units"]["workplace_management"], "FB")
        types = {item["type"] for item in record["pending_money_or_evidence"]}
        self.assertIn("factory_attribution_check", types)
        self.assertIn("wage_confirmation_missing", types)
        self.assertIn("compensation_awaiting_confirmation", types)


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.httpd = create_server("127.0.0.1", 0, data=json.loads(DATA.read_text(encoding="utf-8")), clock="13:10")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=3)
        self.httpd.server_close()

    def request(self, path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as reply:
                return reply.status, json.loads(reply.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_health_worker_factory_and_consent_flow(self):
        status, body = self.request("/health")
        self.assertEqual(status, 200)
        status, body = self.request("/api/workers/W2/record", headers={"X-Actor-Person": "W2"})
        self.assertEqual(status, 200)
        self.assertTrue(any(x["shift"] == "S1" for x in body["work_record"]))

        status, body = self.request("/api/factories/FC", headers={"X-Actor-Factory": "FC"})
        self.assertEqual(status, 200)
        self.assertEqual(body["factory"]["id"], "FC")

        status, body = self.request(
            "/api/shifts/S2/ask",
            method="POST",
            body={"person": "W4"},
            headers={"X-Actor-Factory": "FC"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "awaiting_response")

        status, body = self.request(
            "/api/consents",
            method="POST",
            body={"person": "W4", "shift": "S2", "decision": "accept", "at": "13:20"},
            headers={"X-Actor-Person": "W4"},
        )
        # W4 already has an overlapping S1 commitment in the seed reconciliation.
        self.assertEqual(status, 409)

        status, body = self.request("/api/factories/FB", headers={"X-Actor-Factory": "OR"})
        self.assertEqual(status, 403)

    def test_force_stop_endpoint(self):
        status, body = self.request(
            "/api/recalls/R2/force-stop",
            method="POST",
            body={"at": "10:15"},
            headers={"X-Actor-Factory": "OR"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "force_stopped")
        self.assertFalse(body["worker_agreed"])


if __name__ == "__main__":
    unittest.main()
