#!/usr/bin/env python3
"""Coercer tests. These do not call Google."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import extract  # noqa: E402


def dig(fields, path):
    current = fields
    for part in path.split("."):
        current = current[part]
    return current


def assert_all_not_found(testcase, record):
    for path, leaf in extract.iter_leaves(record["fields"]):
        testcase.assertEqual(leaf["status"], "not_found", path)
        testcase.assertIsNone(leaf["value"], path)
        testcase.assertIsNone(leaf["page"], path)
        testcase.assertIsNone(leaf["excerpt"], path)


def assert_schema(testcase, record):
    fields = record["fields"]
    testcase.assertEqual(set(fields), set(extract.SINGLE_FIELDS) | set(extract.NETWORK_GROUPS))
    for name in extract.NETWORK_GROUPS:
        testcase.assertEqual(set(fields[name]), {"in_network", "out_of_network", "unlabeled"}, name)
    testcase.assertNotIn("waiting_period", fields)
    testcase.assertNotIn("ortho_lifetime_max", fields)
    for name in ("waiting_period_basic", "waiting_period_major", "waiting_period_ortho"):
        testcase.assertIn(name, fields)
    testcase.assertNotIn("waiting_period_preventive", fields)
    testcase.assertIn(record["usable"], ("yes", "no"))
    testcase.assertTrue(record["failure_reason"] is None or isinstance(record["failure_reason"], str))


def plan_payload(excerpts, **header):
    body = {
        "carrier": header.get("carrier", {"page": 1, "excerpt": "Northwind Dental"}),
        "plan_name": header.get("plan_name", {"page": 1, "excerpt": "Plan name: Example Dental PPO"}),
        "plan_type": header.get("plan_type", {"page": 1, "excerpt": "Plan type: PPO"}),
        "excerpts": [{"page": 1, "excerpt": line} if isinstance(line, str) else line for line in excerpts],
    }
    return {"plans": [body]}


def one(payload, pages_read=None):
    records = extract.coerce_model_json(payload, pages_read)
    return records


def build_pdf(lines):
    content = ["BT", "/F1 10 Tf", "50 750 Td"]
    for index, line in enumerate(lines):
        esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            content.append("0 -14 Td")
        content.append(f"({esc}) Tj")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        (b"<< /Length %d >>\nstream\n" % len(stream)) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii")
        out += obj
        out += b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


class CoercerRulesTests(unittest.TestCase):
    def test_silence_is_not_found(self):
        for payload in ({}, {"plans": []}, {"plans": [{}]}, {"plans": [{"carrier": {"page": 1, "excerpt": "   "}}]}):
            records = one(payload)
            self.assertEqual(len(records), 1)
            assert_schema(self, records[0])
            assert_all_not_found(self, records[0])
            self.assertEqual(records[0]["usable"], "no")
        missing_page = one({"plans": [{"carrier": {"excerpt": "Acme Dental"}}]})
        self.assertEqual(dig(missing_page[0]["fields"], "carrier")["status"], "not_found")
        self.assertIsNone(dig(missing_page[0]["fields"], "carrier")["excerpt"])

    def test_in_network_is_not_copied_out(self):
        records = one(
            plan_payload(["In-network preventive 100% / basic 80% / major 50%", "Annual maximum $1,500"])
        )
        fields = records[0]["fields"]
        for name, expected in (("preventive", 100), ("basic", 80), ("major", 50)):
            inn = fields[name]["in_network"]
            self.assertEqual(inn["status"], "found", name)
            self.assertEqual(inn["value"]["normalized"], expected, name)
            self.assertEqual(fields[name]["out_of_network"]["status"], "not_found", name)
            self.assertEqual(fields[name]["unlabeled"]["status"], "not_found", name)
            self.assertNotEqual(fields[name]["out_of_network"].get("value"), inn["value"])

    def test_unlabeled_triple_order_two_triples_and_fourth(self):
        single = one(plan_payload(["100/80/50"]))[0]["fields"]
        self.assertEqual(single["preventive"]["unlabeled"]["value"]["normalized"], 100)
        self.assertEqual(single["basic"]["unlabeled"]["value"]["normalized"], 80)
        self.assertEqual(single["major"]["unlabeled"]["value"]["normalized"], 50)
        self.assertEqual(single["endodontics"]["unlabeled"]["status"], "not_found")
        self.assertEqual(single["preventive"]["in_network"]["status"], "not_found")

        fourth = one(plan_payload(["100/80/50/40"]))[0]
        fields = fourth["fields"]
        self.assertEqual(fields["preventive"]["unlabeled"]["value"]["normalized"], 100)
        self.assertEqual(fields["basic"]["unlabeled"]["value"]["normalized"], 80)
        self.assertEqual(fields["major"]["unlabeled"]["value"]["normalized"], 50)
        self.assertEqual(fields["endodontics"]["unlabeled"]["status"], "not_found")
        blob = json.dumps(fourth)
        self.assertNotIn('"normalized": 40', blob)

        conflicted = one(plan_payload(["100/80/50", "90/70/40"]))[0]["fields"]
        for name, expected in (
            ("preventive", {100, 90}),
            ("basic", {80, 70}),
            ("major", {50, 40}),
        ):
            slot = conflicted[name]["unlabeled"]
            self.assertEqual(slot["status"], "conflict", name)
            self.assertGreaterEqual(len(slot["sides"]), 2, name)
            norms = {side["value"]["normalized"] for side in slot["sides"]}
            self.assertEqual(norms, expected, name)
            for side in slot["sides"]:
                self.assertIn("value", side)
                self.assertIn("page", side)
                self.assertIn("excerpt", side)
                self.assertIsInstance(side["excerpt"], str)
                self.assertTrue(side["excerpt"])

        labeled = one(plan_payload(["Major 100% / Basic 80% / Preventive 50%"]))[0]["fields"]
        basic = labeled["basic"]["unlabeled"]
        self.assertEqual(basic["status"], "found")
        self.assertEqual(basic["value"]["normalized"], 80)
        self.assertEqual(basic["value"]["unit"], "percent")
        for name, expected in (("preventive", {50, 100}), ("major", {100, 50})):
            slot = labeled[name]["unlabeled"]
            self.assertEqual(slot["status"], "conflict", name)
            norms = {side["value"]["normalized"] for side in slot["sides"]}
            self.assertEqual(norms, expected, name)
            self.assertTrue(all(side["value"]["unit"] == "percent" for side in slot["sides"]))
            self.assertGreaterEqual(len(slot["sides"]), 2)
        agreed = one(plan_payload(["Preventive 100% / Basic 80% / Major 50%"]))[0]["fields"]
        self.assertEqual(agreed["preventive"]["unlabeled"]["status"], "found")
        self.assertEqual(agreed["preventive"]["unlabeled"]["value"]["normalized"], 100)
        self.assertEqual(agreed["basic"]["unlabeled"]["value"]["normalized"], 80)
        self.assertEqual(agreed["major"]["unlabeled"]["value"]["normalized"], 50)
        self.assertEqual(agreed["major"]["unlabeled"]["value"]["unit"], "percent")

    def test_member_pay_and_mismatch_conflict(self):
        leaf = one(plan_payload(["Preventive: you pay 20%"]))[0]["fields"]["preventive"]["unlabeled"]
        self.assertEqual(leaf["status"], "found")
        self.assertEqual(leaf["value"]["normalized"], 80)
        self.assertIn("you pay 20%", leaf["excerpt"])
        self.assertIn("you pay 20%", leaf["value"]["printed"])
        self.assertNotEqual(leaf["value"]["normalized"], 20)

        both_ok = one(plan_payload(["Preventive plan pays 80%, you pay 20%"]))[0]["fields"]["preventive"]["unlabeled"]
        self.assertEqual(both_ok["status"], "found")
        self.assertEqual(both_ok["value"]["normalized"], 80)
        self.assertIn("you pay 20%", both_ok["excerpt"])

        bad = one(plan_payload(["Preventive plan pays 70%, you pay 20%"]))[0]["fields"]["preventive"]["unlabeled"]
        self.assertEqual(bad["status"], "conflict")
        norms = {side["value"]["normalized"] for side in bad["sides"]}
        self.assertEqual(norms, {70, 20})
        excerpts = " ".join(side["excerpt"] for side in bad["sides"])
        self.assertIn("you pay 20%", excerpts)
        self.assertIn("plan pays 70%", excerpts)

        bare = one(plan_payload(["80%"]))[0]["fields"]
        for name in ("preventive", "basic", "major", "endodontics"):
            self.assertEqual(bare[name]["unlabeled"]["status"], "not_found", name)

    def test_deductible_maximum_lifetime_and_copay(self):
        fields = one(plan_payload(["Deductible: $50"]))[0]["fields"]
        self.assertEqual(fields["deductible_unlabeled"]["unlabeled"]["status"], "found")
        self.assertEqual(fields["deductible_unlabeled"]["unlabeled"]["value"]["normalized"], 50)
        self.assertEqual(fields["deductible_individual"]["unlabeled"]["status"], "not_found")
        self.assertEqual(fields["deductible_individual"]["in_network"]["status"], "not_found")
        self.assertEqual(fields["deductible_family"]["unlabeled"]["status"], "not_found")

        family_max = one(plan_payload(["Family annual maximum $3,000"]))[0]["fields"]
        for slot in family_max["annual_maximum"].values():
            self.assertEqual(slot["status"], "not_found")
        self.assertEqual(family_max["ortho_lifetime_maximum"]["status"], "not_found")

        lifetime = one(plan_payload(["Lifetime maximum $5,000"]))[0]["fields"]
        for slot in lifetime["annual_maximum"].values():
            self.assertEqual(slot["status"], "not_found")
        self.assertEqual(lifetime["ortho_lifetime_maximum"]["status"], "not_found")

        neither = one(plan_payload(["Maximum $1,500"]))[0]["fields"]
        for slot in neither["annual_maximum"].values():
            self.assertEqual(slot["status"], "not_found")

        copay = one(plan_payload(["Basic $40"]))[0]["fields"]["basic"]["unlabeled"]
        self.assertEqual(copay["status"], "found")
        self.assertEqual(copay["value"]["normalized"], 40)
        self.assertIn("$40", copay["value"]["printed"])
        self.assertNotIn("%", copay["value"]["printed"])
        self.assertEqual(copay["value"]["unit"], "dollars")

        mixed = one(plan_payload(["Basic 80% copay $25"]))[0]["fields"]["basic"]["unlabeled"]
        self.assertEqual(mixed["status"], "conflict")
        norms = {side["value"]["normalized"] for side in mixed["sides"]}
        self.assertEqual(norms, {80, 25})
        self.assertEqual({side["value"]["unit"] for side in mixed["sides"]}, {"percent", "dollars"})

    def test_endo_not_inferred_and_assignment_requires_percent(self):
        major = one(plan_payload(["Major 50%"]))[0]["fields"]
        self.assertEqual(major["major"]["unlabeled"]["value"]["normalized"], 50)
        self.assertEqual(major["endodontics"]["unlabeled"]["status"], "not_found")
        self.assertEqual(major["basic"]["unlabeled"]["status"], "not_found")

        bare_assign = one(plan_payload(["Endodontics, periodontics, and oral surgery paid as basic"]))[0]["fields"]
        for name in ("endodontics", "periodontics", "oral_surgery", "basic"):
            self.assertEqual(bare_assign[name]["unlabeled"]["status"], "not_found", name)

        assigned = one(plan_payload(["Endodontics paid as basic. Basic 80%."]))[0]["fields"]
        self.assertEqual(assigned["basic"]["unlabeled"]["status"], "found")
        self.assertEqual(assigned["basic"]["unlabeled"]["value"]["normalized"], 80)
        endo = assigned["endodontics"]["unlabeled"]
        self.assertEqual(endo["status"], "found")
        self.assertEqual(endo["value"]["normalized"], 80)
        self.assertIn("Endodontics", endo["excerpt"])
        self.assertIn("80", endo["excerpt"])
        self.assertIn("basic", endo["excerpt"].lower())

    def test_waiting_waiver_age_and_basis(self):
        fields = one(
            plan_payload(
                [
                    "Basic waiting period: 6 months",
                    "Major waiting period: 12 months",
                    "Orthodontia waiting period: none",
                    "Preventive waiting period: 0 months",
                    "Preventive 100%",
                    "Orthodontia for children",
                    "Out-of-network basis: UCR",
                ]
            )
        )[0]["fields"]
        self.assertNotIn("waiting_period", fields)
        self.assertNotIn("waiting_period_preventive", fields)
        basic = fields["waiting_period_basic"]
        self.assertEqual(basic["status"], "found")
        self.assertEqual(basic["value"]["normalized"], 6)
        self.assertEqual(fields["waiting_period_major"]["value"]["normalized"], 12)
        ortho = fields["waiting_period_ortho"]
        self.assertEqual(ortho["status"], "found")
        self.assertEqual(ortho["value"]["printed"], "none")
        self.assertIsNone(ortho["value"]["normalized"])
        self.assertNotEqual(ortho["value"]["normalized"], 0)
        self.assertNotEqual(ortho["value"]["printed"], 0)
        self.assertEqual(fields["preventive_deductible_waived"]["status"], "not_found")
        self.assertEqual(fields["ortho_age_limit"]["status"], "not_found")
        basis = fields["out_of_network_basis"]
        self.assertEqual(basis["status"], "found")
        self.assertEqual(basis["value"]["printed"], "Out-of-network basis: UCR")
        self.assertIsNone(basis["value"]["normalized"])
        self.assertNotIn("90", json.dumps(basis))

        waived = one(plan_payload(["Preventive deductible waived"]))[0]["fields"]["preventive_deductible_waived"]
        self.assertEqual(waived["status"], "found")
        self.assertEqual(waived["value"]["printed"], "waived")
        self.assertIsNone(waived["value"]["normalized"])

        applies = one(plan_payload(["Deductible applies to preventive"]))[0]["fields"]["preventive_deductible_waived"]
        self.assertEqual(applies["value"]["printed"], "not waived")

        no_limit = one(plan_payload(["Orthodontia: No age limit"]))[0]["fields"]["ortho_age_limit"]
        self.assertEqual(no_limit["status"], "found")
        self.assertIsNone(no_limit["value"]["normalized"])
        self.assertIn("no age limit", no_limit["value"]["printed"].lower())
        self.assertNotIsInstance(no_limit["value"]["normalized"], (int, float))
        self.assertNotEqual(no_limit["value"]["printed"], "19")

        age = one(plan_payload(["Orthodontia to age 19"]))[0]["fields"]["ortho_age_limit"]
        self.assertEqual(age["status"], "found")
        self.assertEqual(age["value"]["normalized"], 19)

    def test_usable_flag(self):
        good = one(
            plan_payload(
                [
                    "Annual maximum: $1,500 per person",
                    "Preventive 100% / Basic 80% / Major 50%",
                ]
            )
        )[0]
        self.assertEqual(good["usable"], "yes")
        self.assertIsNone(good["failure_reason"])
        annual = good["fields"]["annual_maximum"]["unlabeled"]
        self.assertEqual(annual["status"], "found")
        self.assertEqual(annual["value"]["normalized"], 1500)
        self.assertEqual(annual["page"], 1)
        self.assertIn("$1,500", annual["excerpt"])

        missing_carrier = plan_payload(["Annual maximum $1,500", "Preventive 100% / Basic 80% / Major 50%"])
        missing_carrier["plans"][0]["carrier"] = {"page": 1, "excerpt": ""}
        self.assertEqual(one(missing_carrier)[0]["usable"], "no")

        carrier_conflict = plan_payload(["Annual maximum $1,500", "Preventive 100% / Basic 80% / Major 50%"])
        carrier_conflict["plans"][0]["carrier"] = [
            {"page": 1, "excerpt": "Northwind Dental"},
            {"page": 1, "excerpt": "Southwind Dental"},
        ]
        conflicted = one(carrier_conflict)[0]
        self.assertEqual(conflicted["fields"]["carrier"]["status"], "conflict")
        self.assertEqual(conflicted["usable"], "no")

        missing_annual = one(plan_payload(["Preventive 100% / Basic 80% / Major 50%"]))[0]
        self.assertEqual(missing_annual["usable"], "no")

        annual_conflict = one(
            plan_payload(
                [
                    "Annual maximum $1,500",
                    "Annual maximum $2,000",
                    "Preventive 100% / Basic 80% / Major 50%",
                ]
            )
        )[0]
        self.assertEqual(annual_conflict["fields"]["annual_maximum"]["unlabeled"]["status"], "conflict")
        self.assertEqual(annual_conflict["usable"], "no")

        missing_preventive = one(plan_payload(["Annual maximum $1,500", "Basic 80% / Major 50%"]))[0]
        self.assertEqual(missing_preventive["fields"]["preventive"]["unlabeled"]["status"], "not_found")
        self.assertEqual(missing_preventive["usable"], "no")

        class_conflict = one(
            plan_payload(
                [
                    "Annual maximum $1,500",
                    "Preventive 100%",
                    "Preventive 90%",
                    "Basic 80%",
                    "Major 50%",
                ]
            )
        )[0]
        self.assertEqual(class_conflict["fields"]["preventive"]["unlabeled"]["status"], "conflict")
        self.assertEqual(class_conflict["usable"], "no")

    def test_failure_record_and_unread_page_dropped(self):
        record = extract.blank_record("gemini http 500: boom")
        assert_schema(self, record)
        assert_all_not_found(self, record)
        self.assertEqual(record["usable"], "no")
        self.assertIn("gemini http 500", record["failure_reason"])

        payload = plan_payload(
            [
                {"page": 2, "excerpt": "Annual maximum $1,500"},
                {"page": 1, "excerpt": "Preventive 100% / Basic 80% / Major 50%"},
            ],
            carrier={"page": 2, "excerpt": "Northwind Dental"},
        )
        record = one(payload, pages_read={1})[0]
        self.assertEqual(record["fields"]["carrier"]["status"], "not_found")
        self.assertIsNone(record["fields"]["carrier"]["excerpt"])
        for slot in record["fields"]["annual_maximum"].values():
            self.assertEqual(slot["status"], "not_found")
        self.assertEqual(record["fields"]["preventive"]["unlabeled"]["status"], "found")
        self.assertEqual(record["fields"]["preventive"]["unlabeled"]["page"], 1)
        self.assertEqual(record["usable"], "no")
        self.assertEqual(record["failure_reason"], "cited unread page numbers: 2")

    def test_contract_lines_if_quoted_verbatim(self):
        lines = [
            "Annual maximum: $1,500 per person",
            "In-network individual deductible: $50",
            "Out-of-network individual deductible: $75",
            "Preventive 100% / Basic 80% / Major 50% in-network",
            "Out-of-network preventive 80% / basic 60% / major 40%",
            "Endodontics, periodontics, and oral surgery paid as basic",
            "Preventive deductible waived",
            "Orthodontia lifetime maximum $1,500",
            "Orthodontia copay $0",
            "Orthodontia to age 19",
            "Basic waiting period: 6 months",
            "Major waiting period: 12 months",
            "Orthodontia waiting period: none",
            "Out-of-network basis: 90th percentile of UCR",
        ]
        record = one(plan_payload(lines), pages_read={1})[0]
        assert_schema(self, record)
        self.assertEqual(record["usable"], "yes")
        fields = record["fields"]
        self.assertEqual(fields["carrier"]["value"]["printed"], "Northwind Dental")
        self.assertEqual(fields["plan_name"]["value"]["printed"], "Example Dental PPO")
        self.assertEqual(fields["plan_type"]["value"]["printed"], "PPO")
        self.assertEqual(fields["annual_maximum"]["unlabeled"]["value"]["normalized"], 1500)
        self.assertEqual(fields["annual_maximum"]["in_network"]["status"], "not_found")
        self.assertEqual(fields["deductible_individual"]["in_network"]["value"]["normalized"], 50)
        self.assertEqual(fields["deductible_individual"]["out_of_network"]["value"]["normalized"], 75)
        self.assertEqual(fields["deductible_unlabeled"]["unlabeled"]["status"], "not_found")
        self.assertEqual(fields["preventive"]["in_network"]["value"]["normalized"], 100)
        self.assertEqual(fields["preventive"]["in_network"]["value"]["unit"], "percent")
        self.assertEqual(fields["basic"]["in_network"]["value"]["normalized"], 80)
        self.assertEqual(fields["major"]["in_network"]["value"]["normalized"], 50)
        self.assertEqual(fields["preventive"]["out_of_network"]["value"]["normalized"], 80)
        self.assertEqual(fields["basic"]["out_of_network"]["value"]["normalized"], 60)
        self.assertEqual(fields["major"]["out_of_network"]["value"]["normalized"], 40)
        for name in ("endodontics", "periodontics", "oral_surgery"):
            self.assertEqual(fields[name]["in_network"]["status"], "not_found", name)
            self.assertEqual(fields[name]["unlabeled"]["status"], "not_found", name)
        self.assertEqual(fields["preventive_deductible_waived"]["value"]["printed"], "waived")
        self.assertEqual(fields["ortho_lifetime_maximum"]["value"]["normalized"], 1500)
        self.assertEqual(fields["ortho_coinsurance_or_copay"]["value"]["normalized"], 0)
        self.assertEqual(fields["ortho_coinsurance_or_copay"]["value"]["unit"], "dollars")
        self.assertEqual(fields["ortho_coinsurance_or_copay"]["status"], "found")
        self.assertNotEqual(fields["ortho_lifetime_maximum"]["value"]["normalized"], 0)
        self.assertNotIn("unit", fields["ortho_lifetime_maximum"]["value"])
        self.assertNotIn("unit", fields["carrier"]["value"])
        self.assertNotIn("unit", fields["annual_maximum"]["unlabeled"]["value"])
        self.assertNotIn("unit", fields["deductible_individual"]["in_network"]["value"])
        self.assertNotIn("unit", fields["waiting_period_basic"]["value"])
        self.assertNotIn("unit", fields["ortho_age_limit"]["value"])
        self.assertNotIn("unit", fields["out_of_network_basis"]["value"])
        self.assertNotIn("unit", fields["preventive_deductible_waived"]["value"])
        self.assertEqual(fields["ortho_age_limit"]["value"]["normalized"], 19)
        self.assertEqual(fields["waiting_period_basic"]["value"]["normalized"], 6)
        self.assertEqual(fields["waiting_period_major"]["value"]["normalized"], 12)
        self.assertEqual(fields["waiting_period_ortho"]["value"]["printed"], "none")
        self.assertIsNone(fields["out_of_network_basis"]["value"]["normalized"])
        self.assertIn("90th percentile of UCR", fields["out_of_network_basis"]["value"]["printed"])
        for path, leaf in extract.iter_leaves(fields):
            if leaf["status"] == "conflict":
                self.fail(path)

    def test_one_record_per_plan_and_procedure_list_omitted(self):
        records = one(
            {
                "plans": [
                    {"carrier": {"page": 1, "excerpt": "Alpha Dental"}},
                    {"carrier": {"page": 1, "excerpt": "Beta Dental"}},
                ]
            }
        )
        self.assertEqual(len(records), 1)
        carrier = records[0]["fields"]["carrier"]
        self.assertEqual(carrier["status"], "conflict")
        self.assertEqual(
            {side["value"]["printed"] for side in carrier["sides"]},
            {"Alpha Dental", "Beta Dental"},
        )

        record = one(
            plan_payload(
                [
                    "D0120 $20 D1110 $30 D2140 $40",
                    "Annual maximum $1,000",
                    "Preventive 100% / Basic 80% / Major 50%",
                ]
            )
        )[0]
        assert_schema(self, record)
        self.assertNotIn("implants", record["fields"])
        self.assertNotIn("frequencies", json.dumps(record))
        self.assertEqual(record["usable"], "yes")


    def test_label_order_adult_ortho_units_and_named_plans(self):
        header = one(plan_payload(["Preventive, Basic, and Major: 100/80/50"]))[0]["fields"]
        self.assertEqual(header["preventive"]["unlabeled"]["status"], "found")
        self.assertEqual(header["preventive"]["unlabeled"]["value"]["normalized"], 100)
        self.assertEqual(header["basic"]["unlabeled"]["value"]["normalized"], 80)
        self.assertEqual(header["major"]["unlabeled"]["value"]["normalized"], 50)
        self.assertEqual(header["endodontics"]["unlabeled"]["status"], "not_found")

        adult = one(plan_payload(["Adult orthodontia"]))[0]["fields"]["ortho_age_limit"]
        self.assertEqual(adult["status"], "found")
        self.assertIsNone(adult["value"]["normalized"])
        self.assertIn("adult", adult["value"]["printed"].lower())
        self.assertNotEqual(adult["value"]["normalized"], 19)
        self.assertNotIn("unit", adult["value"])

        children = one(plan_payload(["Orthodontia for children"]))[0]["fields"]["ortho_age_limit"]
        self.assertEqual(children["status"], "not_found")

        ages = one(plan_payload(["Orthodontia to age 19", "Orthodontia through age 26"]))[0]["fields"]["ortho_age_limit"]
        self.assertEqual(ages["status"], "conflict")
        self.assertEqual({side["value"]["normalized"] for side in ages["sides"]}, {19, 26})

        both = one(
            plan_payload(["Preventive deductible waived", "Deductible applies to preventive"])
        )[0]["fields"]["preventive_deductible_waived"]
        self.assertEqual(both["status"], "conflict")
        self.assertEqual({side["value"]["printed"] for side in both["sides"]}, {"waived", "not waived"})

        kept = one(plan_payload(["Deductible $50 applies to preventive"]))[0]["fields"]
        self.assertEqual(kept["preventive_deductible_waived"]["value"]["printed"], "not waived")
        self.assertEqual(kept["deductible_unlabeled"]["unlabeled"]["value"]["normalized"], 50)
        self.assertNotEqual(kept["deductible_unlabeled"]["unlabeled"]["value"]["normalized"], 0)

        self.assertEqual(extract.FAMILY_RE.pattern, r"(?i)\bfamily\b")
        self.assertIsNone(extract.FAMILY_RE.search("per calendar year"))
        self.assertIsNotNone(extract.FAMILY_RE.search("Family annual maximum"))
        calendar = one(plan_payload(["Per calendar year maximum $1,500"]))[0]["fields"]
        self.assertEqual(calendar["annual_maximum"]["unlabeled"]["status"], "found")
        self.assertEqual(calendar["annual_maximum"]["unlabeled"]["value"]["normalized"], 1500)
        self.assertEqual(calendar["annual_maximum"]["in_network"]["status"], "not_found")
        self.assertNotIn("unit", calendar["annual_maximum"]["unlabeled"]["value"])
        family = one(plan_payload(["Family annual maximum $3,000"]))[0]["fields"]
        for slot in family["annual_maximum"].values():
            self.assertEqual(slot["status"], "not_found")

        annuals = one(plan_payload(["Annual maximum $1,500", "Annual maximum $2,000"]))[0]["fields"]
        self.assertEqual(annuals["annual_maximum"]["unlabeled"]["status"], "conflict")
        self.assertEqual(
            {side["value"]["normalized"] for side in annuals["annual_maximum"]["unlabeled"]["sides"]},
            {1500, 2000},
        )
        deductibles = one(plan_payload(["Deductible $50", "Deductible $75"]))[0]["fields"]
        slot = deductibles["deductible_unlabeled"]["unlabeled"]
        self.assertEqual(slot["status"], "conflict")
        self.assertEqual({side["value"]["normalized"] for side in slot["sides"]}, {50, 75})
        self.assertNotIn("unit", slot["sides"][0]["value"])

        columns = one(
            {
                "plans": [
                    {
                        "excerpts": [
                            {"page": 1, "excerpt": "Annual maximum $1,500"},
                            {"page": 1, "excerpt": "Deductible $50"},
                            {"page": 1, "excerpt": "Preventive 100% / Basic 80% / Major 50%"},
                        ]
                    },
                    {
                        "excerpts": [
                            {"page": 1, "excerpt": "Annual maximum $2,000"},
                            {"page": 1, "excerpt": "Deductible $75"},
                        ]
                    },
                ]
            }
        )
        self.assertEqual(len(columns), 1)
        self.assertEqual(columns[0]["fields"]["annual_maximum"]["unlabeled"]["status"], "conflict")
        self.assertEqual(columns[0]["fields"]["deductible_unlabeled"]["unlabeled"]["status"], "conflict")
        self.assertEqual(columns[0]["fields"]["plan_name"]["status"], "not_found")

        networks = one(
            {
                "plans": [
                    {
                        "plan_name": {"page": 1, "excerpt": "In-network"},
                        "excerpts": [{"page": 1, "excerpt": "In-network preventive 100% / basic 80% / major 50%"}],
                    },
                    {
                        "plan_name": {"page": 1, "excerpt": "Out-of-network"},
                        "excerpts": [{"page": 1, "excerpt": "Out-of-network preventive 80% / basic 60% / major 40%"}],
                    },
                ]
            }
        )
        self.assertEqual(len(networks), 1)
        self.assertEqual(networks[0]["fields"]["plan_name"]["status"], "not_found")
        self.assertEqual(networks[0]["fields"]["preventive"]["in_network"]["value"]["normalized"], 100)
        self.assertEqual(networks[0]["fields"]["preventive"]["out_of_network"]["value"]["normalized"], 80)

        benefits = [
            "Annual maximum $1,000",
            "Preventive 100% / Basic 80% / Major 50%",
        ]
        high_benefits = [
            "Annual maximum $2,000",
            "Preventive 100% / Basic 80% / Major 50%",
        ]
        named = one(
            {
                "plans": [
                    {
                        "plan_name": {"page": 1, "excerpt": "Low"},
                        "excerpts": [{"page": 1, "excerpt": line} for line in benefits],
                    },
                    {
                        "plan_name": {"page": 1, "excerpt": "High"},
                        "carrier": {"page": 1, "excerpt": "Carrier: Northwind Dental"},
                        "plan_type": {"page": 1, "excerpt": "Plan type: PPO"},
                        "excerpts": [{"page": 1, "excerpt": line} for line in high_benefits],
                    },
                ]
            }
        )
        self.assertEqual(len(named), 2)
        self.assertEqual(named[0]["usable"], "yes")
        self.assertEqual(named[1]["usable"], "yes")
        self.assertEqual(named[0]["fields"]["plan_name"]["value"]["printed"], "Low")
        self.assertEqual(named[1]["fields"]["plan_name"]["value"]["printed"], "High")
        self.assertEqual(named[0]["fields"]["carrier"]["value"]["printed"], "Northwind Dental")
        self.assertEqual(named[1]["fields"]["carrier"]["value"]["printed"], "Northwind Dental")
        self.assertEqual(named[0]["fields"]["plan_type"]["value"]["printed"], "PPO")
        self.assertEqual(named[1]["fields"]["plan_type"]["value"]["printed"], "PPO")
        self.assertEqual(named[0]["fields"]["annual_maximum"]["unlabeled"]["value"]["normalized"], 1000)
        self.assertEqual(named[1]["fields"]["annual_maximum"]["unlabeled"]["value"]["normalized"], 2000)

        split = one(
            {
                "carrier": {"page": 1, "excerpt": "Northwind Dental"},
                "plan_type": {"page": 1, "excerpt": "PPO"},
                "plans": [
                    {
                        "plan_name": {"page": 1, "excerpt": "Option 1"},
                        "carrier": {"page": 1, "excerpt": "Alpha Dental"},
                        "excerpts": [{"page": 1, "excerpt": line} for line in benefits],
                    },
                    {
                        "plan_name": {"page": 1, "excerpt": "Plan A"},
                        "carrier": {"page": 1, "excerpt": "Beta Dental"},
                        "plan_type": {"page": 1, "excerpt": "DHMO"},
                        "excerpts": [{"page": 1, "excerpt": line} for line in high_benefits],
                    },
                ],
            }
        )
        self.assertEqual(len(split), 2)
        self.assertEqual(split[0]["fields"]["carrier"]["value"]["printed"], "Alpha Dental")
        self.assertEqual(split[1]["fields"]["carrier"]["value"]["printed"], "Beta Dental")
        self.assertEqual(split[0]["fields"]["plan_type"]["status"], "not_found")
        self.assertEqual(split[1]["fields"]["plan_type"]["value"]["printed"], "DHMO")

        copied = one(
            {
                "carrier": {"page": 1, "excerpt": "Northwind Dental"},
                "plan_type": {"page": 1, "excerpt": "PPO"},
                "plans": [
                    {
                        "plan_name": {"page": 1, "excerpt": "Option 1"},
                        "excerpts": [{"page": 1, "excerpt": line} for line in benefits],
                    },
                    {
                        "plan_name": {"page": 1, "excerpt": "Option 2"},
                        "excerpts": [{"page": 1, "excerpt": line} for line in high_benefits],
                    },
                ],
            }
        )
        self.assertEqual(len(copied), 2)
        self.assertEqual(copied[0]["usable"], "yes")
        self.assertEqual(copied[1]["usable"], "yes")
        self.assertEqual(copied[0]["fields"]["carrier"]["value"]["printed"], "Northwind Dental")
        self.assertEqual(copied[1]["fields"]["carrier"]["value"]["printed"], "Northwind Dental")
        self.assertEqual(copied[0]["fields"]["plan_type"]["value"]["printed"], "PPO")
        self.assertEqual(copied[1]["fields"]["plan_type"]["value"]["printed"], "PPO")
        self.assertEqual(copied[0]["fields"]["annual_maximum"]["unlabeled"]["value"]["normalized"], 1000)
        self.assertEqual(copied[1]["fields"]["annual_maximum"]["unlabeled"]["value"]["normalized"], 2000)

    def test_model_failure_payload_shape(self):
        payload = extract.failure_payload("gemini http 400: bad", {"kind": "pdf-images", "name": "x.pdf"})
        self.assertFalse(payload["ok"])
        self.assertEqual(len(payload["records"]), 1)
        assert_all_not_found(self, payload["records"][0])
        self.assertEqual(payload["records"][0]["usable"], "no")
        self.assertIn("gemini http 400", payload["records"][0]["failure_reason"])


class SourceAndCliTests(unittest.TestCase):
    def assert_no_secret(self, text):
        secret = os.environ.get("GEMINI_API_KEY")
        if secret and secret in text:
            self.fail("secret appeared in output")

    def test_source_bans_and_key_name_only(self):
        source = (ROOT / "extract.py").read_text(encoding="utf-8")
        for banned in ("tesseract", "pdftotext", "getTextContent"):
            self.assertNotIn(banned, source)
        self.assertIn("gemini-3.8-flash", source)
        self.assertIn("CALLS_ENABLED = True", source)
        self.assertIn("one plan object per named column or named option", source)
        self.assertIn("Unlabeled money columns are not plans", source)
        self.assertIn("copy that same carrier onto each named plan", source)
        self.assertIn("copy that same plan type onto each named plan", source)
        self.assertIn("pdftoppm", source)
        self.assertIn('"-r"', source)
        self.assertIn('"200"', source)
        self.assertIn("os.environ[\"GEMINI_API_KEY\"]", source)
        self.assertIsNone(__import__("re").search(r"GEMINI_API_KEY\s*=\s*['\"]", source))
        self.assertNotIn("AIza", source)
        for banned in (
            "plan_pay_percent_by_class",
            "annual_maximum_carryover",
            "missing_tooth",
            "frequencies",
        ):
            self.assertNotIn(banned, source)
        page = (ROOT / "page.js").read_text(encoding="utf-8")
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        public = page + html
        self.assertNotIn("generativelanguage", public)
        self.assertNotIn("GEMINI_API_KEY", public)
        self.assertNotIn("tesseract", public.lower())
        self.assertNotIn("getTextContent", public)
        self.assertNotIn("AIza", public)
        self.assertIn("page.render", page)
        self.assertIn('record.usable === "yes"', page)
        self.assertIn('record.usable === "no"', page)
        self.assertNotIn('usable ? "true"', page)
        self.assertIn("Nothing is extracted in the browser", public)

    def test_text_and_stdin_do_not_call_or_find(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.txt"
            path.write_text("Annual maximum $1,500\nPreventive 100%\n", encoding="utf-8")
            with mock.patch.object(extract.urllib.request, "urlopen", side_effect=AssertionError("network")):
                buf = StringIO()
                with mock.patch.object(sys, "stdout", buf):
                    code = extract.run(["--text", str(path)])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(len(payload["records"]), 1)
            self.assertEqual(payload["records"][0]["usable"], "no")
            self.assertEqual(payload["records"][0]["failure_reason"], extract.TEXT_ONLY_REASON)
            assert_all_not_found(self, payload["records"][0])
            self.assert_no_secret(buf.getvalue())

            buf = StringIO()
            with mock.patch.object(sys, "stdin", StringIO("PPO annual maximum $1,500")):
                with mock.patch.object(extract.urllib.request, "urlopen", side_effect=AssertionError("network")):
                    with mock.patch.object(sys, "stdout", buf):
                        code = extract.run(["-"])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["source"]["kind"], "stdin")
            assert_all_not_found(self, payload["records"][0])
            self.assertIn("only a PDF page-image read can fill fields", payload["records"][0]["failure_reason"])

    def test_cli_subprocess_text_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.txt"
            path.write_text("not a pdf\n", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "extract.py"), "--text", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_no_secret(proc.stdout)
        self.assert_no_secret(proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        assert_all_not_found(self, payload["records"][0])

    def test_missing_file_and_bad_pdf_and_missing_key(self):
        buf = StringIO()
        with mock.patch.object(sys, "stdout", buf):
            code = extract.run(["/workspace/dental-extractor/fixtures/missing.pdf"])
        self.assertNotEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("not found", payload["error"].lower())
        assert_all_not_found(self, payload["records"][0])

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "broken.pdf"
            pdf_path.write_bytes(b"this is not a pdf")
            with mock.patch.object(extract.urllib.request, "urlopen", side_effect=AssertionError("network")):
                buf = StringIO()
                with mock.patch.object(sys, "stdout", buf):
                    code = extract.run([str(pdf_path)])
            self.assertNotEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("pdftoppm", payload["error"])
            self.assertNotIn("pdftotext", payload["error"])
            assert_all_not_found(self, payload["records"][0])

            good = Path(tmp) / "one.pdf"
            good.write_bytes(build_pdf(["Annual maximum $1,500"]))
            previous = os.environ.pop("GEMINI_API_KEY", None)
            try:
                with mock.patch.object(extract.urllib.request, "urlopen", side_effect=AssertionError("network")):
                    buf = StringIO()
                    with mock.patch.object(sys, "stdout", buf):
                        code = extract.run([str(good)])
            finally:
                if previous is not None:
                    os.environ["GEMINI_API_KEY"] = previous
            self.assertEqual(code, 1)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["error"], "GEMINI_API_KEY is not set")
            assert_all_not_found(self, payload["records"][0])
            self.assertEqual(payload["records"][0]["usable"], "no")
            self.assertEqual(payload["source"]["page_count"], 1)

    def test_redact_and_unsent_request_shape(self):
        previous = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "unit-test-not-a-real-key"
        try:
            hidden = extract.redact_secret("failure unit-test-not-a-real-key tail")
            self.assertNotIn("unit-test-not-a-real-key", hidden)
            self.assertIn("[redacted]", hidden)
            request = extract.build_gemini_request([b"png-bytes"])
            self.assertIn("/models/gemini-3.8-flash:generateContent", request["url"])
            self.assertEqual(request["headers"]["x-goog-api-key"], "unit-test-not-a-real-key")
            parts = request["body"]["contents"][0]["parts"]
            self.assertEqual(parts[1]["text"], "Page 1 image follows.")
            self.assertEqual(parts[2]["inline_data"]["mime_type"], "image/png")
            self.assertNotIn("tesseract", json.dumps(request["body"]))
        finally:
            if previous is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = previous

    def test_fake_response_is_coerced_without_google(self):
        import urllib.request

        previous = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "unit-test-not-a-real-key"
        body = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        plan_payload(
                                            [
                                                "Annual maximum: $1,500 per person",
                                                "Preventive 100% / Basic 80% / Major 50%",
                                            ]
                                        )
                                    )
                                }
                            ]
                        }
                    }
                ]
            }
        ).encode("utf-8")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return body

        try:
            with tempfile.TemporaryDirectory() as tmp:
                pdf_path = Path(tmp) / "sample.pdf"
                pdf_path.write_bytes(build_pdf(["Annual maximum: $1,500 per person"]))
                with mock.patch.object(urllib.request, "urlopen", return_value=FakeResponse()):
                    buf = StringIO()
                    with mock.patch.object(sys, "stdout", buf):
                        code = extract.run([str(pdf_path)])
            self.assertEqual(code, 0, buf.getvalue())
            self.assert_no_secret(buf.getvalue())
            self.assertNotIn("unit-test-not-a-real-key", buf.getvalue())
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["records"][0]["usable"], "yes")
            self.assertEqual(payload["source"]["provider"], "gemini")
            self.assertEqual(payload["source"]["pages_read"], [1])
            annual = payload["records"][0]["fields"]["annual_maximum"]["unlabeled"]
            self.assertEqual(annual["status"], "found")
            self.assertEqual(annual["page"], 1)
            self.assertIn("Annual maximum", annual["excerpt"])
        finally:
            if previous is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = previous



class BrowserResponseTests(unittest.TestCase):
    """The HTTP payload is images plus coercer records. These tests do not call Google."""

    def setUp(self):
        self._previous = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "unit-test-not-a-real-key"

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = self._previous

    def test_fake_success_response_has_records_and_images_without_secrets(self):
        import serve

        records = extract.coerce_model_json(
            plan_payload(
                [
                    "Annual maximum: $1,500 per person",
                    "Preventive 100% / Basic 80% / Major 50%",
                ]
            ),
            {1},
        )
        dirty = json.loads(json.dumps(records))
        dirty[0]["confidence"] = 0.91
        dirty[0]["rates"] = {"employee": 12.5}
        dirty[0]["raw_model_text"] = (
            "candidates parts prose summary unit-test-not-a-real-key"
        )
        dirty[0]["model_response"] = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "PROSE SUMMARY SHOULD NOT LEAK"}],
                    }
                }
            ],
            "usageMetadata": {"totalTokenCount": 99},
        }
        dirty[0]["fields"]["carrier"]["confidence_score"] = 0.2
        png = b"\x89PNG\r\n\x1a\nnot-a-real-image"
        body = serve.build_browser_response(dirty, [png])
        text = serve.public_json(body)
        self.assertNotIn("unit-test-not-a-real-key", text)
        self.assertNotIn("confidence", text)
        self.assertNotIn("rates", text)
        self.assertNotIn("candidates", text)
        self.assertNotIn("PROSE SUMMARY SHOULD NOT LEAK", text)
        self.assertNotIn("totalTokenCount", text)
        self.assertNotIn("usageMetadata", text)
        self.assertNotIn("generativelanguage", text)
        self.assertNotIn("x-goog-api-key", text)
        self.assertEqual(set(body), {"images", "records"})
        self.assertEqual(body["images"][0]["page"], 1)
        self.assertTrue(body["images"][0]["data_url"].startswith("data:image/png;base64,"))
        self.assertGreaterEqual(len(body["records"]), 1)
        self.assertEqual(body["records"][0]["usable"], "yes")
        carrier = body["records"][0]["fields"]["carrier"]
        self.assertEqual(carrier["status"], "found")
        self.assertEqual(carrier["page"], 1)
        self.assertIn("Northwind Dental", carrier["excerpt"])
        annual = body["records"][0]["fields"]["annual_maximum"]["unlabeled"]
        self.assertEqual(annual["status"], "found")
        self.assertEqual(annual["value"]["normalized"], 1500)

    def test_failure_keeps_rendered_images_and_one_blank_record(self):
        import serve

        with mock.patch.object(extract.urllib.request, "urlopen", side_effect=AssertionError("network")):
            with mock.patch.object(
                extract,
                "execute_gemini_request",
                return_value=(None, "gemini http 500: unit-test-not-a-real-key"),
            ):
                with tempfile.TemporaryDirectory() as tmp:
                    pdf_path = Path(tmp) / "one.pdf"
                    pdf_path.write_bytes(build_pdf(["Annual maximum $1,500"]))
                    result = serve.extract_pdf_bytes(pdf_path.read_bytes())
        text = serve.public_json(result)
        self.assertNotIn("unit-test-not-a-real-key", text)
        self.assertNotIn("generativelanguage", text)
        self.assertNotIn("candidates", text)
        self.assertEqual(set(result), {"images", "records"})
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["images"][0]["page"], 1)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["usable"], "no")
        self.assertIn("[redacted]", result["records"][0]["failure_reason"])
        assert_all_not_found(self, result["records"][0])

    def test_http_post_returns_images_and_records_without_calling_google(self):
        import threading
        from http.client import HTTPConnection

        import serve

        fake_payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    plan_payload(
                                        [
                                            "Annual maximum: $1,500 per person",
                                            "Preventive 100% / Basic 80% / Major 50%",
                                        ]
                                    )
                                )
                            }
                        ]
                    }
                }
            ],
            "usageMetadata": {"totalTokenCount": 12},
        }
        httpd = serve.make_server("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            pdf = build_pdf(["Annual maximum: $1,500 per person"])
            with mock.patch.object(extract.urllib.request, "urlopen", side_effect=AssertionError("network")):
                with mock.patch.object(
                    extract,
                    "execute_gemini_request",
                    return_value=(fake_payload, None),
                ) as call:
                    conn = HTTPConnection("127.0.0.1", port, timeout=30)
                    conn.request(
                        "POST",
                        "/extract",
                        body=pdf,
                        headers={"Content-Type": "application/pdf", "Content-Length": str(len(pdf))},
                    )
                    response = conn.getresponse()
                    raw = response.read().decode("utf-8")
                    conn.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(call.call_count, 1)
            self.assertNotIn("unit-test-not-a-real-key", raw)
            self.assertNotIn("candidates", raw)
            self.assertNotIn("confidence", raw)
            self.assertNotIn("totalTokenCount", raw)
            self.assertNotIn("generativelanguage", raw)
            payload = json.loads(raw)
            self.assertEqual(set(payload), {"images", "records"})
            self.assertEqual(len(payload["images"]), 1)
            self.assertEqual(payload["records"][0]["usable"], "yes")
            self.assertEqual(
                payload["records"][0]["fields"]["annual_maximum"]["unlabeled"]["status"],
                "found",
            )
            second = json.dumps(
                plan_payload(
                    ["Annual maximum $2,000", "Preventive 90% / Basic 70% / Major 40%"],
                    carrier={"page": 1, "excerpt": "Second Carrier"},
                )
            )
            fake_second = {
                "candidates": [
                    {"content": {"parts": [{"text": second}]}},
                ]
            }
            with mock.patch.object(extract.urllib.request, "urlopen", side_effect=AssertionError("network")):
                with mock.patch.object(
                    extract,
                    "execute_gemini_request",
                    return_value=(fake_second, None),
                ):
                    conn = HTTPConnection("127.0.0.1", port, timeout=30)
                    conn.request(
                        "POST",
                        "/extract",
                        body=pdf,
                        headers={"Content-Type": "application/pdf", "Content-Length": str(len(pdf))},
                    )
                    response = conn.getresponse()
                    raw_second = response.read().decode("utf-8")
                    conn.close()
            self.assertEqual(response.status, 200)
            payload_second = json.loads(raw_second)
            self.assertEqual(
                payload_second["records"][0]["fields"]["carrier"]["value"]["printed"],
                "Second Carrier",
            )
            self.assertEqual(
                payload_second["records"][0]["fields"]["annual_maximum"]["unlabeled"]["value"]["normalized"],
                2000,
            )
            self.assertNotIn("Northwind Dental", raw_second)
            self.assertNotEqual(
                payload["records"][0]["fields"]["annual_maximum"]["unlabeled"]["value"]["normalized"],
                2000,
            )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)




if __name__ == "__main__":
    unittest.main(verbosity=2)
