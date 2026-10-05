#!/usr/bin/env python3
"""Tests for the dental benefit summary extractor. Fixtures are fake."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import extract  # noqa: E402

FIX1 = ROOT / "fixtures" / "fixture1_rich.txt"
FIX2 = ROOT / "fixtures" / "fixture2_sparse.txt"
TEXT1 = FIX1.read_text(encoding="utf-8")
TEXT2 = FIX2.read_text(encoding="utf-8")


def leaves(fields):
    return list(extract.iter_leaves(fields))


def assert_leaf_rules(testcase: unittest.TestCase, source: str, fields: dict) -> None:
    for path, leaf in leaves(fields):
        if leaf["status"] == "found":
            testcase.assertIsInstance(leaf["evidence"], str, path)
            testcase.assertIn(leaf["evidence"], source, path)
            testcase.assertNotIn(leaf["value"], (None, False, True), path)
            if isinstance(leaf["value"], str):
                testcase.assertIn(leaf["value"], leaf["evidence"], path)
            for tok in extract.number_tokens(leaf["value"]):
                testcase.assertIn(tok, leaf["evidence"], path)
        else:
            testcase.assertEqual(leaf["status"], "not_found", path)
            testcase.assertIsNone(leaf["value"], path)
            testcase.assertIsNone(leaf["evidence"], path)
            testcase.assertIsNot(leaf["value"], False, path)


def build_pdf(lines: list[str]) -> bytes:
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


class Fixture1Tests(unittest.TestCase):
    def setUp(self):
        self.data = extract.extract_text(TEXT1)
        self.fields = self.data["fields"]

    def test_found_slots_and_evidence(self):
        classes = self.fields["plan_pay_percent_by_class"]
        self.assertEqual(
            set(classes),
            {"Diagnostic and Preventive", "Basic", "Major Restorative"},
        )
        expected = {
            "Diagnostic and Preventive": ("100%", "80%"),
            "Basic": ("80%", "60%"),
            "Major Restorative": ("50%", "40%"),
        }
        for name, (inn, oon) in expected.items():
            self.assertEqual(classes[name]["in_network"]["status"], "found")
            self.assertEqual(classes[name]["out_of_network"]["status"], "found")
            self.assertEqual(classes[name]["in_network"]["value"], inn)
            self.assertEqual(classes[name]["out_of_network"]["value"], oon)
            self.assertIn(classes[name]["in_network"]["evidence"], TEXT1)
            self.assertIn(inn, classes[name]["in_network"]["evidence"])
            self.assertIn(oon, classes[name]["out_of_network"]["evidence"])

        maximum = self.fields["annual_or_contract_maximum"]
        self.assertEqual(maximum["amount_per_person"]["value"], "$1,500")
        self.assertIn("annual", maximum["period"]["value"].lower())
        self.assertNotIn("contract", maximum["period"]["value"].lower())
        self.assertEqual(self.fields["deductible"]["per_person"]["value"], "$50")
        self.assertEqual(self.fields["deductible"]["family_maximum"]["value"], "$150")
        self.assertEqual(self.fields["deductible"]["waived_for_diagnostic_and_preventive"]["status"], "found")
        self.assertIn("waived", self.fields["deductible"]["waived_for_diagnostic_and_preventive"]["value"].lower())

        ortho = self.fields["orthodontics"]
        self.assertEqual(ortho["lifetime_maximum"]["value"], "$1,000")
        self.assertEqual(ortho["child_eligibility"]["status"], "found")
        self.assertIn("children only", ortho["child_eligibility"]["value"])

        cleanings = self.fields["frequencies"]["cleanings"]["frequency"]
        self.assertEqual(cleanings["status"], "found")
        self.assertIn("2 per year", cleanings["evidence"])
        self.assertIn("2 per year", cleanings["value"])

        self.assertEqual(self.fields["plan_type"]["value"], "PPO")
        self.assertIn("PPO", self.fields["plan_type"]["evidence"])
        waiting = self.fields["waiting_periods"]
        self.assertEqual(waiting["collection"]["status"], "found")
        self.assertEqual(set(waiting["by_class"]), {"Major Restorative"})
        self.assertEqual(waiting["by_class"]["Major Restorative"]["value"], "12-month")
        self.assertIn("12-month", waiting["by_class"]["Major Restorative"]["evidence"])

        self.assertEqual(self.fields["cost_share"]["in_network"]["status"], "found")
        self.assertEqual(self.fields["cost_share"]["out_of_network"]["status"], "found")

    def test_not_found_slots_and_crown_is_not_major(self):
        fields = self.fields
        for path in (
            "annual_maximum_carryover",
            "deductible.waived_for_orthodontics",
            "orthodontics.adult_eligibility",
            "implants",
            "missing_tooth_clause",
            "tmj",
            "dependent_age",
            "cost_share.second_network",
        ):
            leaf = fields
            for part in path.split("."):
                leaf = leaf[part]
            self.assertEqual(leaf["status"], "not_found", path)
            self.assertIsNone(leaf["value"], path)
            self.assertIsNot(leaf["value"], False, path)

        classes = fields["plan_pay_percent_by_class"]
        for absent in (
            "Endodontics",
            "Periodontics",
            "Oral Surgery",
            "Prosthodontics",
            "Major",
            "Crowns",
            "Class I",
            "Class II",
            "Class III",
        ):
            self.assertNotIn(absent, classes)
        for name in classes:
            self.assertNotEqual(name.strip().lower(), "major")
            self.assertNotIn("class iii", name.lower())
            self.assertNotIn("class i", name.lower())

        crown = fields["frequencies"]["crowns"]["frequency"]
        self.assertEqual(crown["status"], "found")
        self.assertNotIn("Major", crown["value"])
        self.assertNotIn("Major", crown["evidence"])
        self.assertEqual(fields["frequencies"]["crowns"]["replacement_period"]["status"], "not_found")
        self.assertEqual(fields["frequencies"]["cleanings"]["age_limit"]["status"], "not_found")
        # Waiting period exists only for the printed class, not a zero grid.
        self.assertNotIn("Basic", fields["waiting_periods"]["by_class"])
        self.assertNotIn("Diagnostic and Preventive", fields["waiting_periods"]["by_class"])

    def test_summary_mentions_only_found_values(self):
        summary = self.data["readable_summary"]
        found_part, missing_part = summary.split("Not found:", 1)
        self.assertIn("$1,500", found_part)
        self.assertIn("PPO", found_part)
        self.assertIn("2 per year", found_part)
        self.assertIn("12-month", found_part)
        self.assertNotIn("$1,500", missing_part)
        self.assertNotIn("%", missing_part)
        self.assertIn("annual_maximum_carryover", missing_part)
        self.assertIn("orthodontics.adult_eligibility", missing_part)
        self.assertIn("cost_share.second_network", missing_part)
        for _, leaf in leaves(self.fields):
            if leaf["status"] == "found" and isinstance(leaf["value"], str):
                shown = leaf["value"].replace("\n", " ")
                self.assertIn(shown, summary)


class Fixture2Tests(unittest.TestCase):
    def test_every_benefit_slot_not_found(self):
        data = extract.extract_text(TEXT2)
        found = [path for path, leaf in leaves(data["fields"]) if leaf["status"] == "found"]
        self.assertEqual(found, [])
        assert_leaf_rules(self, TEXT2, data["fields"])
        blob = json.dumps(data["fields"])
        summary = data["readable_summary"]
        for banned in ("100%", "$", "PPO", "555", "80%"):
            self.assertNotIn(banned, blob)
            self.assertNotIn(banned, summary)
        # Slot names such as waived_for_diagnostic_and_preventive may appear
        # in the not-found list. They must not be stated as values.
        self.assertNotIn(": waived", summary.lower())
        self.assertNotIn(": 100", summary)
        self.assertIn("Found:\n- (none)", summary)
        self.assertEqual(data["notes"], [])


class EvidenceTests(unittest.TestCase):
    def test_every_found_evidence_is_a_source_substring(self):
        for source in (TEXT1, TEXT2):
            data = extract.extract_text(source)
            assert_leaf_rules(self, source, data["fields"])
            for note in data["notes"]:
                self.assertIn(note, source)
                self.assertIsInstance(note, str)

    def test_period_not_invented_when_unprinted(self):
        data = extract.extract_text("Benefit maximum per person: $500\n")
        maximum = data["fields"]["annual_or_contract_maximum"]
        self.assertEqual(maximum["amount_per_person"]["status"], "found")
        self.assertEqual(maximum["amount_per_person"]["value"], "$500")
        self.assertEqual(maximum["period"]["status"], "not_found")
        self.assertIsNone(maximum["period"]["value"])

    def test_preventive_waiver_does_not_set_ortho_false(self):
        fields = extract.extract_text("Deductible waived for preventive services.\n")["fields"]
        self.assertEqual(fields["deductible"]["waived_for_diagnostic_and_preventive"]["status"], "found")
        ortho = fields["deductible"]["waived_for_orthodontics"]
        self.assertEqual(ortho["status"], "not_found")
        self.assertIsNone(ortho["value"])

    def test_word_percents_are_not_rewritten_as_digits(self):
        data = extract.extract_text("Plan pays eighty percent for Basic services in-network.\n")
        blob = json.dumps(data["fields"])
        self.assertNotIn("80", blob)
        self.assertEqual(data["fields"]["plan_pay_percent_by_class"], {})

    def test_second_network_only_when_printed(self):
        text = (
            "In-Network    Premier Dental    Out-of-Network\n"
            "Diagnostic and Preventive    100%    90%    80%\n"
        )
        fields = extract.extract_text(text)["fields"]
        self.assertEqual(fields["cost_share"]["second_network"]["value"], "Premier Dental")
        self.assertIn("Premier Dental", text)
        row = fields["plan_pay_percent_by_class"]["Diagnostic and Preventive"]
        self.assertEqual(row["second_network"]["value"], "90%")
        self.assertEqual(row["in_network"]["value"], "100%")
        self.assertEqual(row["out_of_network"]["value"], "80%")

    def test_no_network_imports(self):
        source = (ROOT / "extract.py").read_text(encoding="utf-8")
        for banned in ("urllib", "requests", "http.client", "socket", "pip ", "tesseract", "pdftotext"):
            self.assertNotIn(banned, source)
        self.assertIn("pdftoppm", source)
        self.assertIn("GEMINI_MODEL", source)
        self.assertIn("CALLS_ENABLED = False", source)
        self.assertIn("subprocess", source)
        self.assertIn(
            "quote the page verbatim in evidence, and if it is not printed, status not_found",
            source,
        )


class CliTests(unittest.TestCase):
    def test_cli_text_fixtures(self):
        for path, raw in ((FIX1, TEXT1), (FIX2, TEXT2)):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "extract.py"), "--text", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["fields"], extract.extract_text(raw)["fields"])
            direct = subprocess.run(
                [sys.executable, str(ROOT / "extract.py"), str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(direct.returncode, 0, direct.stderr)
            self.assertEqual(json.loads(direct.stdout)["fields"], payload["fields"])

    def test_cli_stdin(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "extract.py")],
            input=TEXT2,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["source"]["kind"], "stdin")
        self.assertEqual(
            [path for path, leaf in leaves(payload["fields"]) if leaf["status"] == "found"],
            [],
        )

    def test_pdf_renders_images_and_does_not_call_ocr_or_network(self):
        lines = [
            "This is a PPO plan.",
            "Per person annual maximum: $1,500",
            "In-Network    Out-of-Network",
            "Diagnostic and Preventive    100%    80%",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "sample.pdf"
            pdf_path.write_bytes(build_pdf(lines))
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            marker = tmp_path / "banned-calls"
            script = "#!/bin/sh\nprintf '%s\\n' \"$0\" >> " + str(marker) + "\nexit 99\n"
            for name in ("tesseract", "pdftotext", "curl", "wget"):
                fake = bin_dir / name
                fake.write_text(script, encoding="utf-8")
                fake.chmod(0o755)
            env = os.environ.copy()
            env.pop("GEMINI_API_KEY", None)
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            before = set(Path(tempfile.gettempdir()).glob("dental-pdf-*"))
            proc = subprocess.run(
                [sys.executable, str(ROOT / "extract.py"), str(pdf_path)],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            after = set(Path(tempfile.gettempdir()).glob("dental-pdf-*"))
            self.assertEqual(proc.returncode, 2, proc.stderr + proc.stdout)
            self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")
            self.assertEqual(after, before)
            payload = json.loads(proc.stdout)
            self.assertEqual(
                payload,
                {
                    "ok": False,
                    "error": "vision model not confirmed",
                    "source": {
                        "kind": "pdf-images",
                        "name": str(pdf_path),
                        "page_count": 1,
                        "provider": "gemini",
                    },
                },
            )
            self.assertNotIn("fields", payload)

    def test_bad_pdf_is_json_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "broken.pdf"
            pdf_path.write_bytes(b"this is not a pdf")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "extract.py"), str(pdf_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("pdftoppm", payload["error"])
            self.assertNotIn("pdftotext", payload["error"])

    def test_missing_file_is_json_error(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "extract.py"), "/workspace/dental-extractor/fixtures/missing.txt"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("not found", payload["error"].lower())


class GeminiPrepTests(unittest.TestCase):
    def test_quote_filter_blank_evidence_is_not_found(self):
        for raw in (
            {"status": "found", "value": "100%", "evidence": ""},
            {"status": "found", "value": "100%", "evidence": "   "},
            {"status": "found", "value": "100%"},
            {"status": "found", "value": "PPO", "evidence": None},
        ):
            leaf = extract.coerce_model_leaf(raw)
            self.assertEqual(leaf["status"], "not_found")
            self.assertIsNone(leaf["value"])
            self.assertIsNone(leaf["evidence"])
        fields = extract.fields_from_gemini(
            {
                "fields": {
                    "plan_type": {"status": "found", "value": "PPO", "evidence": ""},
                    "implants": {"status": "found", "value": "covered"},
                    "plan_pay_percent_by_class": {
                        "Major Restorative": {
                            "in_network": {
                                "status": "found",
                                "value": "50%",
                                "evidence": "Major Restorative 50%",
                            },
                            "out_of_network": {
                                "status": "found",
                                "value": "40%",
                                "evidence": "",
                            },
                        }
                    },
                }
            }
        )
        self.assertEqual(fields["plan_type"]["status"], "not_found")
        self.assertIsNone(fields["plan_type"]["value"])
        self.assertEqual(fields["implants"]["status"], "not_found")
        row = fields["plan_pay_percent_by_class"]["Major Restorative"]
        self.assertEqual(row["in_network"]["status"], "found")
        self.assertEqual(row["in_network"]["evidence"], "Major Restorative 50%")
        self.assertEqual(row["out_of_network"]["status"], "not_found")
        self.assertIsNone(row["out_of_network"]["value"])
        self.assertNotIn("Class III", fields["plan_pay_percent_by_class"])
        self.assertNotIn("Major", fields["plan_pay_percent_by_class"])

    def test_model_prose_is_not_regex_parsed(self):
        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": "This is a PPO plan.\nPer person annual maximum: $1,500\n",
                            }
                        ]
                    }
                }
            ]
        }
        fields = extract.parse_gemini_response(payload)
        found = [path for path, leaf in leaves(fields) if leaf["status"] == "found"]
        self.assertEqual(found, [])
        quoted = extract.parse_gemini_response(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "fields": {
                                                "plan_type": {
                                                    "status": "found",
                                                    "value": "PPO",
                                                    "evidence": "This is a PPO plan.",
                                                }
                                            }
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            }
        )
        self.assertEqual(quoted["plan_type"]["status"], "found")
        self.assertEqual(quoted["plan_type"]["value"], "PPO")
        self.assertEqual(quoted["plan_type"]["evidence"], "This is a PPO plan.")
        self.assertEqual(quoted["annual_or_contract_maximum"]["amount_per_person"]["status"], "not_found")

    def test_missing_key_builds_no_request_and_does_not_use_network(self):
        import socket

        previous = os.environ.pop("GEMINI_API_KEY", None)
        try:
            blocked = extract.prepare_gemini_request([b"png"])
            self.assertEqual(blocked, {"ok": False, "error": "GEMINI_API_KEY is not set"})
            lines = ["This is a PPO plan."]
            with tempfile.TemporaryDirectory() as tmp:
                pdf_path = Path(tmp) / "sample.pdf"
                pdf_path.write_bytes(build_pdf(lines))
                commands = []
                real_run = extract.subprocess.run

                def wrapped(cmd, *args, **kwargs):
                    commands.append(list(cmd))
                    if cmd[0] != "pdftoppm":
                        raise AssertionError("unexpected command " + str(cmd[0]))
                    return real_run(cmd, *args, **kwargs)

                def no_network(*args, **kwargs):
                    raise AssertionError("network")

                with mock.patch.object(extract.subprocess, "run", wrapped):
                    with mock.patch.object(extract, "extract_text", side_effect=AssertionError("extract_text")):
                        with mock.patch.object(socket, "socket", no_network):
                            with mock.patch.object(extract, "CALLS_ENABLED", True):
                                from io import StringIO

                                buf = StringIO()
                                with mock.patch.object(sys, "stdout", buf):
                                    code = extract.run([str(pdf_path)])
                self.assertEqual(code, 1, buf.getvalue())
                self.assertEqual(commands[0][0], "pdftoppm")
                self.assertIn("200", commands[0])
                self.assertEqual(json.loads(buf.getvalue())["error"], "GEMINI_API_KEY is not set")
                self.assertNotIn("fields", json.loads(buf.getvalue()))
        finally:
            if previous is not None:
                os.environ["GEMINI_API_KEY"] = previous

    def test_request_shape_is_unsent(self):
        import socket

        previous = os.environ.pop("GEMINI_API_KEY", None)
        os.environ["GEMINI_API_KEY"] = "x"
        try:
            def no_network(*args, **kwargs):
                raise AssertionError("network")

            with mock.patch.object(socket, "socket", no_network):
                request = extract.build_gemini_request([b"png-bytes"])
            self.assertEqual(extract.GEMINI_MODEL, "gemini-3.8-flash")
            self.assertFalse(extract.CALLS_ENABLED)
            self.assertIn("/models/gemini-3.8-flash:generateContent", request["url"])
            self.assertEqual(request["headers"]["x-goog-api-key"], "x")
            self.assertEqual(request["headers"]["Content-Type"], "application/json")
            parts = request["body"]["contents"][0]["parts"]
            self.assertEqual(parts[0]["text"][:1], "Y")
            image = parts[1]["inline_data"]
            self.assertEqual(image["mime_type"], "image/png")
            self.assertEqual(image["data"], "cG5nLWJ5dGVz")
            self.assertNotIn("fields", request)
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
            if previous is not None:
                os.environ["GEMINI_API_KEY"] = previous



if __name__ == "__main__":
    unittest.main(verbosity=2)
