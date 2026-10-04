#!/usr/bin/env python3
"""Extract dental plan-design fields from a summary.

Pure Python 3. A PDF is rendered to page images with pdftoppm, then those
images are read with the tesseract binary. extract_text sees that OCR text.
This is not a vision LLM. Every benefit leaf is found only when a
verbatim evidence quote is a substring of the source text. Nothing is
filled from general dental knowledge.

NADP class names (Class I / II / III) are NOT used as defaults.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

Leaf = dict[str, Any]

FREQUENCY_SLOTS = (
    "exams",
    "cleanings",
    "bitewings",
    "full_mouth_or_panoramic",
    "fluoride",
    "sealants",
    "space_maintainers",
    "fillings",
    "crowns",
    "dentures",
    "bridges",
    "implants",
    "root_canal",
    "periodontal_surgery",
    "scaling_and_root_planing",
    "periodontal_maintenance",
)

FREQUENCY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("exams", re.compile(r"(?i)\boral\s+evaluations?\b|\boral\s+exams?\b|\bperiodic\s+(?:oral\s+)?exams?\b|\bexams?\b")),
    ("cleanings", re.compile(r"(?i)\bcleanings?\b|\bprophylaxis\b|\bprophy\b")),
    ("bitewings", re.compile(r"(?i)\bbite[\s-]?wings?\b")),
    ("full_mouth_or_panoramic", re.compile(r"(?i)\bfull[\s-]?mouth\b|\bpanoramic\b|\bcomplete\s+series\b")),
    ("fluoride", re.compile(r"(?i)\bfluoride\b")),
    ("sealants", re.compile(r"(?i)\bsealants?\b")),
    ("space_maintainers", re.compile(r"(?i)\bspace\s+maintainers?\b")),
    ("fillings", re.compile(r"(?i)\bfillings?\b")),
    ("crowns", re.compile(r"(?i)\bcrowns?\b")),
    ("dentures", re.compile(r"(?i)\bdentures?\b")),
    ("bridges", re.compile(r"(?i)\bbridges?\b")),
    ("implants", re.compile(r"(?i)\bimplants?\b")),
    ("root_canal", re.compile(r"(?i)\broot\s+canals?\b")),
    ("periodontal_surgery", re.compile(r"(?i)\bperiodontal\s+surgery\b")),
    ("scaling_and_root_planing", re.compile(r"(?i)\bscaling\s+and\s+root\s+planing\b|\bscaling\s*&\s*root\s+planing\b|\broot\s+planing\b")),
    ("periodontal_maintenance", re.compile(r"(?i)\bperiodontal\s+maintenance\b|\bperio\s+maintenance\b")),
]

FREQ_SIGNAL = re.compile(
    r"(?i)("
    r"\d+\s+every\s+\d+\s*(?:months?|years?)"
    r"|\d+\s*(?:times\s*)?per\s+(?:calendar\s+|contract\s+|plan\s+)?year"
    r"|twice\s+per\s+(?:calendar\s+|contract\s+|plan\s+)?year"
    r"|two\s+per\s+(?:calendar\s+|contract\s+|plan\s+)?year"
    r"|once\s+every\s+\d+\s*(?:months?|years?)"
    r"|once\s+in\s+(?:a\s+|each\s+)?(?:calendar\s+|contract\s+|plan\s+)?year"
    r"|every\s+\d+\s*(?:months?|years?)"
    r"|once\s+per\s+\S(?:.*\S)?"
    r"|limited\s+to\s+\S(?:.*\S)?"
    r"|one\s+per\s+\S(?:.*\S)?"
    r")"
)

AGE_RE = re.compile(
    r"(?i)("
    r"(?:through|thru|under|up to)\s+age\s+\d{1,2}"
    r"|age\s+\d{1,2}\s+and\s+(?:under|younger)"
    r"|ages?\s+\d{1,2}\s*[-–]\s*\d{1,2}"
    r"|to\s+age\s+\d{1,2}"
    r")"
)

REPL_RE = re.compile(r"(?i)(replac(?:e|ed|ement|ing)\b[^\n]{0,60})")

MONEY_RE = re.compile(r"\$\d[\d,]*(?:\.\d{2})?")
PCT_RE = re.compile(r"\d{1,3}\s*%")
IN_RE = re.compile(r"(?i)in[-\s]?network")
OUT_RE = re.compile(r"(?i)out[-\s]of[-\s]?network")
PER_PERSON_RE = re.compile(
    r"(?i)\bper\s+(?:eligible\s+|insured\s+|covered\s+)?person\b|\bper\s+person\b|\beach\s+covered\s+person\b"
)
PERIOD_PATTERNS = (
    re.compile(r"(?i)contract\s+year"),
    re.compile(r"(?i)calendar\s+year"),
    re.compile(r"(?i)\bannual\b"),
)

PLAN_TYPE_RE = re.compile(
    r"(?i)("
    r"\bthis\s+is\s+an?\s+(?:PPO|DHMO|indemnity)\s+plan\b"
    r"|\b(?:PPO|DHMO|indemnity)\s+plan\b"
    r"|\bplan\s+type\s*[:\-]\s*(?:PPO|DHMO|indemnity)\b"
    r"|\b(?:PPO|DHMO|indemnity)\s+dental\s+plan\b"
    r")"
)
PLAN_TOKEN_RE = re.compile(r"(?i)\b(PPO|DHMO|indemnity)\b")


def empty_leaf() -> Leaf:
    return {"status": "not_found", "value": None, "evidence": None}


def is_leaf(obj: Any) -> bool:
    return isinstance(obj, dict) and set(obj.keys()) == {"status", "value", "evidence"}


def iter_leaves(obj: Any, path: str = ""):
    if is_leaf(obj):
        yield path, obj
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            yield from iter_leaves(value, child)


def number_tokens(value: Any) -> list[str]:
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [str(value)]
    return re.findall(r"\d[\d,]*", str(value))


def make_found(source: str, evidence: str | None, value: Any) -> Leaf:
    if not isinstance(evidence, str) or evidence == "" or evidence not in source:
        return empty_leaf()
    if value is None or isinstance(value, bool):
        return empty_leaf()
    if isinstance(value, str):
        if value == "" or value not in evidence:
            return empty_leaf()
    for tok in number_tokens(value):
        if tok not in evidence:
            return empty_leaf()
    return {"status": "found", "value": value, "evidence": evidence}


def set_if_empty(slot: Leaf, source: str, evidence: str | None, value: Any) -> None:
    if slot.get("status") == "found":
        return
    found = make_found(source, evidence, value)
    if found["status"] == "found":
        slot.clear()
        slot.update(found)


def validate_fields(fields: dict[str, Any], source: str) -> None:
    for _, leaf in iter_leaves(fields):
        if leaf.get("status") != "found":
            leaf["status"] = "not_found"
            leaf["value"] = None
            leaf["evidence"] = None
            continue
        checked = make_found(source, leaf.get("evidence"), leaf.get("value"))
        leaf.clear()
        leaf.update(checked)


def skeleton() -> dict[str, Any]:
    frequencies = {
        name: {
            "frequency": empty_leaf(),
            "age_limit": empty_leaf(),
            "replacement_period": empty_leaf(),
        }
        for name in FREQUENCY_SLOTS
    }
    return {
        "cost_share": {
            "in_network": empty_leaf(),
            "out_of_network": empty_leaf(),
            "second_network": empty_leaf(),
        },
        "annual_or_contract_maximum": {
            "amount_per_person": empty_leaf(),
            "period": empty_leaf(),
        },
        "annual_maximum_carryover": empty_leaf(),
        "deductible": {
            "per_person": empty_leaf(),
            "family_maximum": empty_leaf(),
            "waived_for_diagnostic_and_preventive": empty_leaf(),
            "waived_for_orthodontics": empty_leaf(),
        },
        "plan_pay_percent_by_class": {},
        "diagnostic_preventive_excluded_from_annual_maximum": empty_leaf(),
        "orthodontics": {
            "lifetime_maximum": empty_leaf(),
            "adult_eligibility": empty_leaf(),
            "child_eligibility": empty_leaf(),
            "one_course_per_lifetime": empty_leaf(),
        },
        "waiting_periods": {
            "collection": empty_leaf(),
            "by_class": {},
        },
        "frequencies": frequencies,
        "implants": empty_leaf(),
        "alternate_benefit_or_least_costly_treatment": empty_leaf(),
        "cosmetic_exclusion": empty_leaf(),
        "missing_tooth_clause": empty_leaf(),
        "tmj": empty_leaf(),
        "coordination_of_benefits": empty_leaf(),
        "allowed_amount_basis": empty_leaf(),
        "pretreatment_estimate": empty_leaf(),
        "dependent_age": empty_leaf(),
        "plan_type": empty_leaf(),
    }


def nonempty_line_starts(text: str, money_start: int, lookback: int = 4) -> int:
    line_start = text.rfind("\n", 0, money_start) + 1
    starts = [line_start]
    cursor = line_start
    found = 0
    while found < lookback and cursor > 0:
        prev = text.rfind("\n", 0, cursor - 1) + 1
        segment = text[prev: cursor - 1]
        cursor = prev
        if segment.strip():
            starts.append(prev)
            found += 1
        if prev == 0:
            break
    return min(starts)


def lines_between(text: str, start: int, end: int) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    cursor = start
    while cursor < end:
        newline = text.find("\n", cursor, end)
        if newline == -1:
            newline = end
        segment = text[cursor:newline]
        if segment.strip():
            out.append((cursor, segment))
        if newline == end:
            break
        cursor = newline + 1
    return out


def classify_money(lines: list[tuple[int, str]]) -> tuple[str | None, int | None]:
    if not lines:
        return None, None
    last = lines[-1][1]
    texts = [seg for _, seg in lines]
    for offset, seg in reversed(lines):
        if re.search(r"(?i)carry[-\s]?over|\brollover\b", seg):
            return "carryover", offset
        if re.search(r"(?i)orthodont", seg):
            return "ortho_lifetime", offset
        if re.search(r"(?i)\bdeductible\b", seg):
            if re.search(r"(?i)\bfamily\b", seg) or re.search(r"(?i)\bfamily\b", last):
                return "family_deductible", offset
            return "person_deductible", offset
        if re.search(r"(?i)\bmaximum\b", seg):
            familyish = re.search(r"(?i)\bfamily\b", seg) or re.search(r"(?i)\bfamily\b", last)
            if familyish:
                if any(re.search(r"(?i)\bdeductible\b", s) for s in texts):
                    return "family_deductible", offset
                return None, None
            if PER_PERSON_RE.search(last) or PER_PERSON_RE.search(seg):
                return "annual_max", offset
            return None, None
    return None, None


def period_phrase(scope: str) -> str | None:
    for pattern in PERIOD_PATTERNS:
        matches = list(pattern.finditer(scope))
        if matches:
            return matches[-1].group(0)
    return None


def parse_money(text: str, fields: dict[str, Any]) -> None:
    targets = {
        "annual_max": fields["annual_or_contract_maximum"]["amount_per_person"],
        "person_deductible": fields["deductible"]["per_person"],
        "family_deductible": fields["deductible"]["family_maximum"],
        "ortho_lifetime": fields["orthodontics"]["lifetime_maximum"],
        "carryover": fields["annual_maximum_carryover"],
    }
    for match in MONEY_RE.finditer(text):
        scope_start = nonempty_line_starts(text, match.start(), 4)
        lined = lines_between(text, scope_start, match.end())
        kind, trigger = classify_money(lined)
        if kind is None or trigger is None or kind not in targets:
            continue
        slot = targets[kind]
        if slot["status"] == "found":
            continue
        evidence = text[trigger: match.end()]
        value = match.group(0)
        set_if_empty(slot, text, evidence, value)
        if kind == "annual_max" and fields["annual_or_contract_maximum"]["period"]["status"] != "found":
            phrase = period_phrase(evidence)
            if phrase:
                set_if_empty(fields["annual_or_contract_maximum"]["period"], text, phrase, phrase)


def parse_carryover_phrase(text: str, fields: dict[str, Any]) -> None:
    slot = fields["annual_maximum_carryover"]
    if slot["status"] == "found":
        return
    match = re.search(
        r"(?i)((?:annual\s+)?maximum\s+carry[-\s]?over\s*(?:yes|no)?|\brollover\b\s*(?:yes|no)?)",
        text,
    )
    if not match:
        return
    evidence = match.group(0)
    yn = re.search(r"(?i)\b(yes|no)\b", evidence)
    if yn:
        value = yn.group(0)
    else:
        word = re.search(r"(?i)carry[-\s]?over|rollover", evidence)
        value = word.group(0) if word else evidence
    set_if_empty(slot, text, evidence, value)


def _waiver_value(snippet: str) -> str | None:
    column = re.search(r"(?i)(?:\s{2,}|\t)(yes|no)\s*$", snippet)
    if column:
        return column.group(1)
    waived = re.search(r"(?i)\bwaived\b", snippet)
    if waived:
        return waived.group(0)
    subject = re.search(r"(?i)not subject to(?:\s+the)?\s+deductible", snippet)
    if subject:
        return subject.group(0)
    return None


def parse_waivers(text: str, fields: dict[str, Any]) -> None:
    patterns = [
        re.compile(r"(?i)(?:\bdeductible\b[^\n]{0,60}\bwaived\b|\bwaived\b[^\n]{0,60}\bdeductible\b)[^\n]*"),
        re.compile(
            r"(?i)[^\n]*(?:diagnostic|preventive)[^\n]{0,100}not subject to[^\n]{0,40}deductible[^\n]*"
        ),
    ]
    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end()))
    for start, end in spans:
        snippet_end = end
        snippet = text[start:end]
        core = re.sub(r"(?i)(?:\s{2,}(?:yes|no))+\s*$", "", snippet).rstrip()
        if re.search(r"(?i)\band\s*(?:yes|no)?\s*$", core):
            nxt = re.match(r"\s*\n([^\n]*)", text[end:])
            if (
                nxt
                and re.search(r"(?i)orthodont", nxt.group(1))
                and not re.search(r"(?i)maximum|\$|%", nxt.group(1))
                and len(nxt.group(1).strip()) <= 80
            ):
                snippet_end = end + nxt.end()
                snippet = text[start:snippet_end]
        value = _waiver_value(snippet)
        if not value:
            continue
        if re.search(r"(?i)diagnostic|preventive", snippet):
            set_if_empty(fields["deductible"]["waived_for_diagnostic_and_preventive"], text, snippet, value)
        if re.search(r"(?i)orthodont", snippet):
            set_if_empty(fields["deductible"]["waived_for_orthodontics"], text, snippet, value)


def parse_dp_excluded(text: str, fields: dict[str, Any]) -> None:
    match = re.search(
        r"(?i)([^\n]*diagnostic[^\n]{0,50}preventive[^\n]{0,90}(?:do not|does not|don't|not)\s+apply[^\n]{0,70}maximum[^\n]*)",
        text,
    )
    if not match:
        return
    evidence = match.group(0)
    phrase = re.search(r"(?i)do not apply|does not apply|don't apply|not apply", evidence)
    if not phrase:
        return
    set_if_empty(fields["diagnostic_preventive_excluded_from_annual_maximum"], text, evidence, phrase.group(0))


def _plausible_network_name(name: str) -> bool:
    if not (2 <= len(name) <= 40):
        return False
    words = name.split()
    if not words or len(words) > 5:
        return False
    if not re.search(r"[A-Za-z]", name):
        return False
    if re.search(r"(?i)pays|coinsurance|glance|maximum|deductible|benefit|summary|services|person|%|\$|\bplan\b", name):
        return False
    if re.fullmatch(r"(?i)(yes|no)", name):
        return False
    return True


def best_network_header(text: str):
    best = None
    for line in text.splitlines():
        found = []
        for match in re.finditer(r"(?i)in[-\s]?network|out[-\s]of[-\s]?network", line):
            raw = match.group(0)
            kind = "in_network" if raw.lower().replace(" ", "").startswith("in") else "out_of_network"
            found.append((match.start(), match.end(), kind, raw))
        kinds = {item[2] for item in found}
        if "in_network" not in kinds or "out_of_network" not in kinds:
            continue
        found.sort()
        items = [(start, end, kind, raw) for start, end, kind, raw in found]
        extras = []
        for idx in range(len(found) - 1):
            gap = line[found[idx][1]: found[idx + 1][0]].strip()
            if _plausible_network_name(gap):
                extras.append((found[idx][1], found[idx + 1][0], "second_network", gap))
        if len(extras) > 1:
            extras = []
        merged = [(start, kind, raw) for start, _, kind, raw in items]
        for start, _, kind, raw in extras:
            merged.append((start, kind, raw))
        merged.sort()
        order = [kind for _, kind, _ in merged]
        second = next((raw for _, kind, raw in merged if kind == "second_network"), None)
        in_raw = next(raw for _, kind, raw in merged if kind == "in_network")
        out_raw = next(raw for _, kind, raw in merged if kind == "out_of_network")
        leftover = line
        for raw in (in_raw, out_raw):
            leftover = re.sub(re.escape(raw), " ", leftover, count=1)
        if second:
            leftover = leftover.replace(second, " ")
        score = (len(leftover.split()), len(line))
        cand = (score, order, second, in_raw, out_raw, line)
        if best is None or cand[0] < best[0]:
            best = cand
    return best


def clean_class_label(raw: str) -> str | None:
    label = re.sub(r"^[\s*•·\-\u2022\u2013\u2014|]+", "", raw)
    label = re.sub(r"[\s|]+$", "", label)
    label = re.sub(r"\s+", " ", label).strip(" :.-")
    if not label or len(label) < 3 or len(label) > 70:
        return None
    if len(label.split()) > 8:
        return None
    if re.search(r"(?i)\$|waiting period|limited to|per year|per tooth|deductible|\bmaximum\b|phone|example|customer service", label):
        return None
    if re.fullmatch(r"(?i)in[-\s]?network|out[-\s]of[-\s]?network", label):
        return None
    if re.search(r"(?i)\b(allows|balance billed|total cost|this example)\b", label):
        return None
    return label


def class_bucket(order: list[str], second_name: str | None) -> dict[str, Leaf]:
    bucket = {
        "in_network": empty_leaf(),
        "out_of_network": empty_leaf(),
    }
    if second_name or "second_network" in order:
        bucket["second_network"] = empty_leaf()
    return bucket


def parse_classes(text: str, fields: dict[str, Any]) -> None:
    header = best_network_header(text)
    order: list[str] = []
    second = None
    if header:
        _, order, second, in_raw, out_raw, _line = header
        set_if_empty(fields["cost_share"]["in_network"], text, in_raw, in_raw)
        set_if_empty(fields["cost_share"]["out_of_network"], text, out_raw, out_raw)
        if second:
            set_if_empty(fields["cost_share"]["second_network"], text, second, second)
    classes: dict[str, dict[str, Leaf]] = fields["plan_pay_percent_by_class"]
    explicit = re.compile(
        r"(?i)^(?P<label>.+?)\s+in[-\s]?network\s*(?P<inn>\d{1,3}\s*%)\s+"
        r"(?:(?P<second>[A-Za-z][^%\n]{1,30}?)\s+(?P<sp>\d{1,3}\s*%)\s+)?"
        r"out[-\s]of[-\s]?network\s*(?P<oon>\d{1,3}\s*%)\s*$"
    )
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        exp = explicit.match(stripped)
        if exp:
            label = clean_class_label(exp.group("label"))
            if not label or label in classes:
                continue
            bucket = class_bucket(order or ["in_network", "out_of_network"], exp.group("second"))
            inn = exp.group("inn")
            oon = exp.group("oon")
            bucket["in_network"] = make_found(text, line, inn)
            bucket["out_of_network"] = make_found(text, line, oon)
            if exp.group("second") and exp.group("sp") and _plausible_network_name(exp.group("second").strip()):
                sname = exp.group("second").strip()
                bucket["second_network"] = make_found(text, line, exp.group("sp"))
                set_if_empty(fields["cost_share"]["second_network"], text, sname, sname)
            in_match = IN_RE.search(line)
            out_match = OUT_RE.search(line)
            if in_match:
                set_if_empty(fields["cost_share"]["in_network"], text, in_match.group(0), in_match.group(0))
            if out_match:
                set_if_empty(fields["cost_share"]["out_of_network"], text, out_match.group(0), out_match.group(0))
            if bucket["in_network"]["status"] == "found" and bucket["out_of_network"]["status"] == "found":
                classes[label] = bucket
            continue
        pcts = list(PCT_RE.finditer(line))
        if not pcts or not order or len(pcts) != len(order):
            continue
        label = clean_class_label(line[: pcts[0].start()])
        if not label or label in classes:
            continue
        bucket = class_bucket(order, second)
        ok = True
        for kind, pct in zip(order, pcts):
            leaf = make_found(text, line, pct.group(0))
            if leaf["status"] != "found":
                ok = False
                break
            bucket[kind] = leaf
        if ok:
            classes[label] = bucket


def previous_nonempty(text: str, line_start: int) -> tuple[int | None, str]:
    cursor = line_start
    while cursor > 0:
        prev = text.rfind("\n", 0, cursor - 1) + 1
        segment = text[prev: cursor - 1]
        if segment.strip():
            return prev, segment
        if prev == 0:
            break
        cursor = prev
    return None, ""


def parse_ortho_eligibility(text: str, fields: dict[str, Any]) -> None:
    ortho = fields["orthodontics"]
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        start = offset
        offset += len(line)
        if not body.strip():
            continue
        own = bool(re.search(r"(?i)orthodont", body))
        prev_start, prev_seg = previous_nonempty(text, start)
        short_follow = (
            not own
            and prev_start is not None
            and len(body.strip()) <= 100
            and "$" not in body
            and "%" not in body
            and bool(re.search(r"(?i)orthodont", prev_seg))
            and bool(re.search(r"(?i)child|adult|one course", body))
        )
        if not own and not short_follow:
            continue
        evidence = body if own or prev_start is None else text[prev_start: start + len(body)]
        child = re.search(r"(?i)(children only|child only|dependent children|adults and dependent children)", body)
        if child and re.search(r"(?i)child", child.group(0)):
            set_if_empty(ortho["child_eligibility"], text, evidence, child.group(0))
        adult = re.search(r"(?i)(\badults?\b(?:\s+and\s+dependent\s+children)?|adults?\s+only)", body)
        if adult:
            set_if_empty(ortho["adult_eligibility"], text, evidence, adult.group(0))
        course = re.search(r"(?i)(one course\b[^\n]{0,70})", body)
        if course:
            set_if_empty(ortho["one_course_per_lifetime"], text, evidence, course.group(0).strip())


def parse_waiting(text: str, fields: dict[str, Any]) -> None:
    mention = re.search(r"(?i)waiting\s+periods?", text)
    if not mention:
        return
    set_if_empty(fields["waiting_periods"]["collection"], text, mention.group(0), mention.group(0))
    by_class = fields["waiting_periods"]["by_class"]
    labeled = re.compile(
        r"(?i)^(?P<label>[A-Za-z][^:\n]{1,60}?)\s*:\s*"
        r"(?P<dur>\d{1,3}\s*-\s*months?|\d{1,3}\s*-\s*years?|\d{1,3}\s+months?|\d{1,3}\s+years?)"
    )
    trailing = re.compile(
        r"(?i)(?P<dur>\d{1,3}\s*-\s*months?|\d{1,3}\s*-\s*years?|\d{1,3}\s+months?|\d{1,3}\s+years?)"
        r"\s+waiting\s+period\s+(?:on|for)\s+(?P<label>[A-Za-z][^.\n]{1,60})"
    )
    for line in text.splitlines():
        if not re.search(r"(?i)waiting", line):
            continue
        match = labeled.match(line.strip())
        if not match:
            match = trailing.search(line)
        if not match:
            continue
        label = clean_class_label(match.group("label"))
        if not label or label in by_class:
            continue
        if re.search(r"(?i)waiting", label):
            continue
        dur = match.group("dur")
        leaf = make_found(text, line, dur)
        if leaf["status"] == "found":
            by_class[label] = leaf


def parse_frequencies(text: str, fields: dict[str, Any]) -> None:
    for line in text.splitlines():
        if not line.strip():
            continue
        if PCT_RE.search(line) and not FREQ_SIGNAL.search(line):
            continue
        if not FREQ_SIGNAL.search(line):
            continue
        evidence = line.strip()
        if evidence not in text:
            evidence = line
        for slot, pattern in FREQUENCY_PATTERNS:
            bucket = fields["frequencies"][slot]
            if bucket["frequency"]["status"] == "found":
                continue
            if not pattern.search(line):
                continue
            set_if_empty(bucket["frequency"], text, evidence, evidence)
            age = AGE_RE.search(line)
            if age:
                set_if_empty(bucket["age_limit"], text, evidence, age.group(0))
            repl = REPL_RE.search(line)
            if repl:
                set_if_empty(bucket["replacement_period"], text, evidence, repl.group(0).strip())


def line_at(text: str, start: int, end: int) -> str:
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    if le == -1:
        le = len(text)
    return text[ls:le]


def parse_implants(text: str, fields: dict[str, Any]) -> None:
    for match in re.finditer(r"(?i)implant", text):
        line = line_at(text, match.start(), match.end())
        window = line
        status = re.search(
            r"(?i)(not covered|excluded|limited|covered)",
            window,
        )
        if not status:
            continue
        # Status word must sit near the implant word on this line.
        impl = re.search(r"(?i)implant\w*", window)
        if not impl:
            continue
        if abs(impl.start() - status.start()) > 50:
            continue
        if status.group(0).lower() == "covered" and re.search(r"(?i)not covered", window):
            status = re.search(r"(?i)not covered", window)
        value = status.group(0)
        set_if_empty(fields["implants"], text, window.strip() if window.strip() in text else window, value)
        if fields["implants"]["status"] == "found":
            return


def parse_named_provisions(text: str, fields: dict[str, Any]) -> None:
    rules = [
        (
            "alternate_benefit_or_least_costly_treatment",
            re.compile(r"(?i)(alternate benefit|alternative benefit|least costly(?:\s+\w+){0,4}|least expensive(?:\s+\w+){0,4})"),
        ),
        (
            "cosmetic_exclusion",
            re.compile(r"(?i)(cosmetic(?:\s+\w+){0,6})"),
        ),
        (
            "missing_tooth_clause",
            re.compile(r"(?i)(missing tooth(?:\s+\w+){0,6})"),
        ),
        (
            "tmj",
            re.compile(r"(?i)(\bTMJ\b|temporomandibular(?:\s+\w+){0,4})"),
        ),
        (
            "coordination_of_benefits",
            re.compile(r"(?i)(coordination of benefits|\bCOB\b)"),
        ),
        (
            "allowed_amount_basis",
            re.compile(
                r"(?i)(usual,?\s+and\s+customary|\bUCR\b|maximum allowable charge|\bMAC\b|"
                r"maximum allowed (?:cost|amount|charge)|allowed amount|fee schedule|"
                r"\d{1,3}(?:st|nd|rd|th)\s+percentile)"
            ),
        ),
        (
            "pretreatment_estimate",
            re.compile(r"(?i)(pre[-\s]?treatment estimate|predetermination|pre[-\s]?estimate)"),
        ),
        (
            "dependent_age",
            re.compile(
                r"(?i)((?:eligible\s+)?dependents?(?:\s+child(?:ren)?)?[^.\n]{0,40}(?:through age|to age|under age|age)\s*\d{1,2}"
                r"|limiting age[^.\n]{0,20}\d{1,2})"
            ),
        ),
    ]
    for key, pattern in rules:
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(1)
        line = line_at(text, match.start(1), match.end(1))
        evidence = line if value in line else value
        set_if_empty(fields[key], text, evidence, value)


def parse_plan_type(text: str, fields: dict[str, Any]) -> None:
    found: list[tuple[str, str]] = []
    seen = set()
    for match in PLAN_TYPE_RE.finditer(text):
        token_match = PLAN_TOKEN_RE.search(match.group(0))
        if not token_match:
            continue
        token = token_match.group(0)
        seen.add(token.upper())
        line = line_at(text, match.start(), match.end())
        found.append((token, line if token in line else match.group(0)))
    if len(seen) != 1 or not found:
        return
    token, evidence = found[0]
    set_if_empty(fields["plan_type"], text, evidence, token)


def build_summary(fields: dict[str, Any]) -> str:
    found_lines = []
    missing_lines = []
    for path, leaf in iter_leaves(fields):
        if leaf["status"] == "found":
            shown = leaf["value"]
            if isinstance(shown, str):
                shown = shown.replace("\n", " ")
            found_lines.append(f"- {path}: {shown}")
        else:
            missing_lines.append(f"- {path}")
    found_lines.sort()
    missing_lines.sort()
    parts = ["Found:"]
    parts.extend(found_lines or ["- (none)"])
    parts.append("Not found:")
    parts.extend(missing_lines or ["- (none)"])
    return "\n".join(parts)


def collect_notes(fields: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    seen = set()
    for _, leaf in iter_leaves(fields):
        if leaf["status"] != "found":
            continue
        evidence = leaf["evidence"]
        if evidence in seen:
            continue
        seen.add(evidence)
        notes.append(evidence)
    return notes


def extract_text(text: str) -> dict[str, Any]:
    fields = skeleton()
    parse_classes(text, fields)
    parse_money(text, fields)
    parse_carryover_phrase(text, fields)
    parse_waivers(text, fields)
    parse_dp_excluded(text, fields)
    parse_ortho_eligibility(text, fields)
    parse_waiting(text, fields)
    parse_frequencies(text, fields)
    parse_implants(text, fields)
    parse_named_provisions(text, fields)
    parse_plan_type(text, fields)
    validate_fields(fields, text)
    # Drop class buckets that failed validation entirely.
    cleaned = {}
    for name, bucket in fields["plan_pay_percent_by_class"].items():
        if any(leaf["status"] == "found" for leaf in bucket.values() if is_leaf(leaf)):
            cleaned[name] = bucket
    fields["plan_pay_percent_by_class"] = cleaned
    return {
        "ok": True,
        "fields": fields,
        "notes": collect_notes(fields),
        "readable_summary": build_summary(fields),
    }


def pdf_to_text(path: str) -> tuple[str | None, str | None, int | None]:
    """Render each page to a PNG, then OCR those images.

    Returns (text, error, page_count). Page texts are joined with a blank
    line. The PDF text layer is not read. Empty OCR text is not an error.
    """
    with tempfile.TemporaryDirectory(prefix="dental-ocr-") as tmp:
        prefix = str(Path(tmp) / "page")
        try:
            render = subprocess.run(
                ["pdftoppm", "-png", "-r", "200", path, prefix],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError:
            return None, "pdftoppm is not installed", None
        if render.returncode != 0:
            detail = (render.stderr or render.stdout or "pdftoppm failed").strip()
            return None, f"pdftoppm failed: {detail}", None
        pages = sorted(
            Path(tmp).glob("page-*.png"),
            key=lambda image: int(image.stem.rsplit("-", 1)[-1]),
        )
        if not pages:
            return None, "pdftoppm produced no page images", None
        texts: list[str] = []
        for image in pages:
            try:
                ocr = subprocess.run(
                    ["tesseract", str(image), "stdout", "-l", "eng"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            except FileNotFoundError:
                return None, "tesseract is not installed", None
            if ocr.returncode != 0:
                detail = (ocr.stderr or ocr.stdout or "tesseract failed").strip()
                return None, f"tesseract failed: {detail}", None
            texts.append(ocr.stdout.rstrip("\n"))
        return "\n\n".join(texts), None, len(pages)


def emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def error_payload(message: str, kind: str, name: str | None) -> dict[str, Any]:
    return {"ok": False, "error": message, "source": {"kind": kind, "name": name}}


def read_text_file(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract dental benefit fields from a summary.")
    parser.add_argument("file", nargs="?", help="PDF or text file. Use - or omit to read stdin.")
    parser.add_argument("--text", dest="text_path", help="Read this path as text, not as a PDF.")
    args = parser.parse_args(argv)

    try:
        if args.text_path:
            text = read_text_file(args.text_path)
            result = extract_text(text)
            result["source"] = {"kind": "text", "name": args.text_path}
        elif not args.file or args.file == "-":
            text = sys.stdin.read()
            result = extract_text(text)
            result["source"] = {"kind": "stdin", "name": None}
        elif args.file.lower().endswith(".pdf"):
            text, err, page_count = pdf_to_text(args.file)
            if err:
                emit(error_payload(err, "pdf-image-ocr", args.file))
                return 1
            result = extract_text(text or "")
            result["source"] = {
                "kind": "pdf-image-ocr",
                "name": args.file,
                "pages": page_count,
            }
            if not (text or "").strip():
                result["warnings"] = ["OCR returned no text"]
        else:
            text = read_text_file(args.file)
            result = extract_text(text)
            result["source"] = {"kind": "text", "name": args.file}
    except FileNotFoundError as exc:
        emit(error_payload(f"file not found: {exc.filename}", "file", str(exc.filename)))
        return 1
    except OSError as exc:
        emit(error_payload(str(exc), "file", args.text_path or args.file))
        return 1
    emit(result)
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
