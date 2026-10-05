#!/usr/bin/env python3
"""Extract dental plan-design fields from a benefit summary PDF.

A PDF is rendered to PNG page images with pdftoppm. Those images are
sent to Gemini. serve.py on localhost uses this same path for the
page. The coercer below applies the benefit rules to the model JSON.
It does not read pixels, and it does not invent excerpts.

Pasted text, stdin, and --text do not call Gemini and cannot fill
fields: there is no page image.

Model id: gemini-3.8-flash. The key is read from the environment at
call time under the name GEMINI_API_KEY. It is never written here.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CALLS_ENABLED = True
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_MODEL
    + ":generateContent"
)

TEXT_ONLY_REASON = "only a PDF page-image read can fill fields"

NETWORK_GROUPS = (
    "annual_maximum",
    "deductible_individual",
    "deductible_family",
    "deductible_unlabeled",
    "preventive",
    "basic",
    "major",
    "endodontics",
    "periodontics",
    "oral_surgery",
)
SINGLE_FIELDS = (
    "carrier",
    "plan_name",
    "plan_type",
    "preventive_deductible_waived",
    "ortho_lifetime_maximum",
    "ortho_coinsurance_or_copay",
    "ortho_age_limit",
    "out_of_network_basis",
    "waiting_period_basic",
    "waiting_period_major",
    "waiting_period_ortho",
)
WAITING_SLOTS = {
    "basic": "waiting_period_basic",
    "major": "waiting_period_major",
    "ortho": "waiting_period_ortho",
}
CLASS_FIELDS = (
    "preventive",
    "basic",
    "major",
    "endodontics",
    "periodontics",
    "oral_surgery",
)
ASSIGNABLE = ("endodontics", "periodontics", "oral_surgery")

IN_RE = re.compile(r"(?i)\bin[-\s]?network\b")
OUT_RE = re.compile(r"(?i)\bout[-\s]of[-\s]?network\b")
PCT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%")
SLASH_RE = re.compile(
    r"(?<![\d./])(\d{1,3})\s*/\s*(\d{1,3})\s*/\s*(\d{1,3})(?:\s*/\s*(\d{1,3}))?(?!\s*/)"
)
DOLLAR_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?")
MEMBER_PHRASE_RE = re.compile(
    r"(?i)\b(?:you|member|patient)\s+pays?\s+\d{1,3}(?:\.\d+)?\s*%"
)
PLAN_PHRASE_RE = re.compile(
    r"(?i)\b(?:plan|insurance|we)\s+pays?\s+\d{1,3}(?:\.\d+)?\s*%"
)
ASSIGN_RE = re.compile(
    r"(?i)\b(?:paid|payable|covered|pay)\s+as\s+(preventive|basic|major)\b"
    r"|\bsame\s+as\s+(preventive|basic|major)\b"
)
PLAN_TYPE_RE = re.compile(
    r"(?i)\b(DHMO|DPPO|DMO|PPO|indemnity|EPO|POS|HMO|FFS)\b|fee-for-service"
)
AGE_RE = re.compile(
    r"(?i)(?:through|thru|until|to|under|up to)\s+age\s*(\d{1,2})"
    r"|age\s*(\d{1,2})"
    r"|(\d{1,2})\s*(?:years?\s+of\s+age|years?\s+old)"
)
NO_AGE_RE = re.compile(r"(?i)\bno\s+age\s+limit\b")
ADULT_RE = re.compile(r"(?i)\badults?\b")
MONTH_RE = re.compile(r"(?i)\b(\d{1,3})\s*-?\s*months?\b")
YEAR_RE = re.compile(r"(?i)\b(\d{1,3})\s*-?\s*years?\b")
WAITING_NONE_RE = re.compile(r"(?i)\b(?:none|waived|no\s+waiting(?:\s+period)?)\b")
ANNUAL_RE = re.compile(r"(?i)\bannual\b|\bcalendar\s+year\b|\bper\s+year\b|\byearly\b")
LIFETIME_RE = re.compile(r"(?i)\blifetime\b")
MAXIMUM_RE = re.compile(r"(?i)\bmax(?:imum)?\b")
ORTHO_RE = re.compile(r"(?i)\borthodont")
FAMILY_RE = re.compile(r"(?i)\bfamily\b")
PERSON_RE = re.compile(
    r"(?i)\bper\s+person\b|\bindividual\b|\bper\s+member\b|\beach\s+(?:covered\s+)?person\b"
)
DEDUCTIBLE_RE = re.compile(r"(?i)\bdeductible\b")
WAIVED_RE = re.compile(r"(?i)\bwaiv\w*\b")
APPLIES_RE = re.compile(r"(?i)\bapplies\b")
PREVENTIVE_WORD_RE = re.compile(r"(?i)\bpreventiv\w*\b|\bdiagnostic\b")
PROCEDURE_RE = re.compile(r"\bD\d{4}\b")
BASIS_RE = re.compile(
    r"(?i)\bbasis\b|\bucr\b|\bmac\b|\bpercentile\b|\bfee\s+schedule\b|"
    r"\breasonable\s+and\s+customary\b|\busual\s+and\s+customary\b|"
    r"\ballowed\s+amount\b|\ballowable\s+charge\b"
)

CLASS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("oral_surgery", re.compile(r"(?i)\boral\s+surgery\b")),
    ("endodontics", re.compile(r"(?i)\bendodont(?:ic|ics|ia)?\b|\bendo\b")),
    ("periodontics", re.compile(r"(?i)\bperiodont(?:ic|ics|ia)?\b|\bperio\b")),
    ("preventive", re.compile(r"(?i)\bpreventive\b|\bpreventative\b")),
    ("basic", re.compile(r"(?i)\bbasic\b")),
    ("major", re.compile(r"(?i)\bmajor\b")),
)

GEMINI_PROMPT = """You read page images of dental benefit summaries. Return one JSON object and nothing else.

The object is {"plans":[...]} with one plan object per named column or named option. A named plan has its own printed name (for example Low, High, Option 1, or Plan A). In-network and out-of-network are fields, not separate plans. Unlabeled money columns are not plans: do not return a separate plan object for an unlabeled column of dollars or percents, and do not write in-network or out-of-network unless those words are printed. If the document prints a carrier once, copy that same carrier onto each named plan. If the document prints a plan type once, copy that same plan type onto each named plan. Do not copy a carrier or plan type when the document prints two different ones.

Each quote is {"page": <1-based page number of that image>, "excerpt": "<verbatim text from that page image>"}.
The excerpt must be copied from the image. Do not invent, paraphrase, or calculate. Include the label words on the line (for example Preventive, Basic, deductible, annual, lifetime), not a bare number. If a line assigns endodontics, periodontics, or oral surgery to another class, the excerpt for that line is only the printed line; do not merge a different line into it. Omit procedure-level copay lists, implants, missing-tooth rules, how often a service is allowed, rollover, takeover, rates, contributions, participation, and network size.

Schema, using placeholders you must replace with verbatim page text or omit when not printed:
{"plans":[{"carrier":{"page":1,"excerpt":"VERBATIM CARRIER"},"plan_name":{"page":1,"excerpt":"VERBATIM PLAN NAME LINE"},"plan_type":{"page":1,"excerpt":"VERBATIM PLAN TYPE LINE"},"excerpts":[{"page":1,"excerpt":"VERBATIM BENEFIT LINE"}]}]}

Quote, when printed: carrier name, plan name, plan type (only if a type word such as PPO, DHMO, or indemnity is printed), annual maximum, deductibles, preventive/basic/major coinsurance or copay, endodontics, periodontics, oral surgery, whether the preventive deductible is waived or applies, orthodontia lifetime maximum, orthodontia copay or patient cost, orthodontia age limit, waiting periods, and out-of-network basis. Do not copy the placeholders above.
"""


def not_found() -> dict[str, Any]:
    return {"status": "not_found", "value": None, "page": None, "excerpt": None}


def skeleton_fields() -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name in SINGLE_FIELDS:
        fields[name] = not_found()
    for name in NETWORK_GROUPS:
        fields[name] = {
            "in_network": not_found(),
            "out_of_network": not_found(),
            "unlabeled": not_found(),
        }
    return fields


def blank_record(failure_reason: str | None) -> dict[str, Any]:
    if isinstance(failure_reason, str):
        failure_reason = redact_secret(failure_reason)
    return {
        "usable": "no",
        "failure_reason": failure_reason,
        "fields": skeleton_fields(),
    }


def iter_leaves(obj: Any, path: str = ""):
    if isinstance(obj, dict) and obj.get("status") in {"found", "not_found", "conflict"}:
        yield path, obj
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            yield from iter_leaves(value, child)


def parse_page(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, float) and value.is_integer():
        number = int(value)
        return number if number >= 1 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number >= 1 else None
    return None


def quote_ok(excerpt: Any, page: Any, pages_read: set[int] | None) -> int | None:
    if not isinstance(excerpt, str) or excerpt.strip() == "":
        return None
    number = parse_page(page)
    if number is None:
        return None
    if pages_read is not None and number not in pages_read:
        return None
    return number


def value_obj(printed: str, normalized: Any, unit: str | None = None) -> dict[str, Any]:
    obj: dict[str, Any] = {"printed": printed, "normalized": normalized}
    # Unit belongs only on class coinsurance/copay and orthodontia copay.
    if unit in {"percent", "dollars"}:
        obj["unit"] = unit
    return obj


def same_value(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("printed") == right.get("printed")
        and left.get("normalized") == right.get("normalized")
        and left.get("unit") == right.get("unit")
    )


def write_found(
    slot: dict[str, Any],
    printed: str,
    normalized: Any,
    page: int,
    excerpt: str,
    unit: str | None = None,
) -> None:
    side_value = value_obj(printed, normalized, unit)
    side = {"value": side_value, "page": page, "excerpt": excerpt}
    status = slot.get("status")
    if status == "not_found":
        slot.clear()
        slot.update(
            {
                "status": "found",
                "value": side_value,
                "page": page,
                "excerpt": excerpt,
            }
        )
        return
    if status == "found":
        if same_value(slot["value"], side_value):
            return
        previous = {
            "value": slot["value"],
            "page": slot["page"],
            "excerpt": slot["excerpt"],
        }
        slot.clear()
        slot.update({"status": "conflict", "sides": [previous, side]})
        return
    if status == "conflict":
        if any(same_value(item["value"], side_value) for item in slot["sides"]):
            return
        slot["sides"].append(side)


def as_number(text: str) -> int | float:
    if "." in text:
        number = float(text)
        if number.is_integer():
            return int(number)
        return number
    return int(text)


def whole(number: int | float) -> int | float:
    if isinstance(number, float) and number.is_integer():
        return int(number)
    return number


def find_dollars(excerpt: str) -> list[dict[str, Any]]:
    found = []
    for match in DOLLAR_RE.finditer(excerpt):
        raw_number = match.group(1).replace(",", "")
        cents = match.group(2)
        if cents and cents != "00":
            number: int | float = float(f"{raw_number}.{cents}")
        else:
            number = int(raw_number)
        found.append(
            {
                "raw": match.group(0).replace(" ", ""),
                "value": number,
                "start": match.start(),
                "end": match.end(),
            }
        )
    return found


def find_percents(excerpt: str) -> list[dict[str, Any]]:
    spans: list[tuple[int, int]] = []
    found: list[dict[str, Any]] = []
    for match in SLASH_RE.finditer(excerpt):
        spans.append((match.start(), match.end()))
        groups = [match.group(i) for i in range(1, 5) if match.group(i) is not None]
        # A fourth slash number is not part of the triple. Keep it so the
        # caller can ignore anything past the first three, but do not add
        # further slash groups.
        cursor = match.start()
        for piece in groups:
            at = excerpt.find(piece, cursor, match.end())
            number = as_number(piece)
            if 0 <= number <= 100:
                found.append(
                    {
                        "raw": piece,
                        "value": number,
                        "start": at,
                        "end": at + len(piece),
                        "slash": True,
                    }
                )
            cursor = at + len(piece)
    for match in PCT_RE.finditer(excerpt):
        if any(start <= match.start() < end for start, end in spans):
            continue
        number = as_number(match.group(1))
        if 0 <= number <= 100:
            found.append(
                {
                    "raw": match.group(0).strip(),
                    "value": number,
                    "start": match.start(),
                    "end": match.end(),
                    "slash": False,
                }
            )
    found.sort(key=lambda item: item["start"])
    return found


def find_categories(excerpt: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for name, pattern in CLASS_PATTERNS:
        for match in pattern.finditer(excerpt):
            found.append({"name": name, "start": match.start(), "end": match.end(), "raw": match.group(0)})
    found.sort(key=lambda item: (item["start"], -(item["end"] - item["start"])))
    kept: list[dict[str, Any]] = []
    cursor = -1
    for item in found:
        if item["start"] < cursor:
            continue
        kept.append(item)
        cursor = item["end"]
    return kept


def network_matches(excerpt: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for match in IN_RE.finditer(excerpt):
        found.append((match.start(), match.end(), "in_network"))
    for match in OUT_RE.finditer(excerpt):
        found.append((match.start(), match.end(), "out_of_network"))
    found.sort()
    return found


def shared_network_phrase(excerpt: str, matches: list[tuple[int, int, str]]) -> bool:
    kinds = {kind for _, _, kind in matches}
    if kinds != {"in_network", "out_of_network"}:
        return False
    if len(matches) < 2:
        return False
    first_end = matches[0][1]
    second_start = matches[1][0]
    if second_start - first_end > 60:
        return False
    between = excerpt[first_end:second_start]
    return re.search(r"\d", between) is None


def networks_for(excerpt: str, value_count: int, pos: int) -> list[str]:
    matches = network_matches(excerpt)
    kinds = {kind for _, _, kind in matches}
    if "in_network" in kinds and "out_of_network" in kinds:
        if value_count == 1 or shared_network_phrase(excerpt, matches):
            return ["in_network", "out_of_network"]
        nearest = min(matches, key=lambda item: abs(pos - item[0]))
        return [nearest[2]]
    if "in_network" in kinds:
        return ["in_network"]
    if "out_of_network" in kinds:
        return ["out_of_network"]
    return ["unlabeled"]


def put_network(
    group: dict[str, Any],
    excerpt: str,
    pos: int,
    value_count: int,
    printed: str,
    normalized: Any,
    page: int,
    unit: str | None = None,
) -> None:
    for name in networks_for(excerpt, value_count, pos):
        write_found(group[name], printed, normalized, page, excerpt, unit)


def percent_role(excerpt: str, start: int, end: int) -> str:
    window = excerpt[max(0, start - 60) : end]
    if MEMBER_PHRASE_RE.search(window) or re.search(r"(?i)\b(?:you|member|patient)\s+pays?\b", window):
        return "member"
    if PLAN_PHRASE_RE.search(window) or re.search(r"(?i)\b(?:plan|insurance|we)\s+pays?\b", window):
        return "plan"
    return "bare"


def member_phrase(excerpt: str) -> str | None:
    match = MEMBER_PHRASE_RE.search(excerpt)
    if match:
        return match.group(0)
    return None


def plan_phrase(excerpt: str) -> str | None:
    match = PLAN_PHRASE_RE.search(excerpt)
    if match:
        return match.group(0)
    return None


def apply_percent_decision(
    group: dict[str, Any],
    excerpt: str,
    percents: list[dict[str, Any]],
    page: int,
    value_count: int,
) -> None:
    if not percents:
        return
    members = [item for item in percents if percent_role(excerpt, item["start"], item["end"]) == "member"]
    plans = [item for item in percents if percent_role(excerpt, item["start"], item["end"]) != "member"]
    member_values = {item["value"] for item in members}
    plan_values = {item["value"] for item in plans}
    pos = percents[0]["start"]
    if len(member_values) > 1 or len(plan_values) > 1:
        for item in percents:
            write_found(
                group[networks_for(excerpt, value_count, item["start"])[0]],
                item["raw"],
                item["value"],
                page,
                excerpt,
                "percent",
            )
        return
    if members and plans:
        member_n = members[0]["value"]
        plan_n = plans[0]["value"]
        plan_printed = plan_phrase(excerpt) or plans[0]["raw"]
        member_printed = member_phrase(excerpt) or members[0]["raw"]
        if abs((plan_n + member_n) - 100) < 0.001:
            put_network(group, excerpt, pos, value_count, plan_printed, whole(plan_n), page, "percent")
            return
        # Keep both stated sides. Do not pick one and do not convert.
        put_network(group, excerpt, pos, value_count, plan_printed, whole(plan_n), page, "percent")
        put_network(group, excerpt, members[0]["start"], value_count, member_printed, whole(member_n), page, "percent")
        return
    if members:
        member_n = members[0]["value"]
        printed = member_phrase(excerpt) or members[0]["raw"]
        put_network(group, excerpt, pos, value_count, printed, whole(100 - member_n), page, "percent")
        return
    item = plans[0]
    printed = plan_phrase(excerpt) or item["raw"]
    put_network(group, excerpt, item["start"], value_count, printed, whole(item["value"]), page, "percent")


def apply_dollars(
    group: dict[str, Any],
    excerpt: str,
    dollars: list[dict[str, Any]],
    page: int,
    value_count: int,
) -> None:
    # A class dollar is a copay. The unit is dollars, not percent.
    for item in dollars:
        put_network(group, excerpt, item["start"], value_count, item["raw"], item["value"], page, "dollars")


def class_percent_from_results(
    results: dict[str, dict[str, Any]],
    class_name: str,
) -> dict[str, Any] | None:
    found = results.get(class_name)
    if not found:
        return None
    if found.get("kind") == "percent":
        return found
    return None


PBM_ORDER = ("preventive", "basic", "major")


def apply_class_assignment(
    fields: dict[str, Any],
    excerpt: str,
    page: int,
    categories: list[dict[str, Any]],
    direct: dict[str, dict[str, Any]],
    value_count: int,
) -> None:
    assign = ASSIGN_RE.search(excerpt)
    if not assign:
        return
    class_name = next(group for group in assign.groups() if group)
    class_name = class_name.lower()
    parent = class_percent_from_results(direct, class_name)
    if parent is None:
        return
    if str(parent["value"]) not in excerpt and parent["printed"] not in excerpt:
        return
    for category in categories:
        name = category["name"]
        if name not in ASSIGNABLE:
            continue
        if name in direct:
            continue
        if category["raw"].lower() not in excerpt.lower():
            continue
        printed = parent["printed"] if "%" in parent["printed"] else f"{parent['value']}%"
        if "%" not in printed:
            printed = f"{whole(parent['value'])}%"
        put_network(
            fields[name],
            excerpt,
            parent["pos"],
            value_count,
            printed if printed in excerpt or parent["printed"] in excerpt else parent["printed"],
            whole(parent["value"]),
            page,
            "percent",
        )


def parse_pbm_label_order(
    fields: dict[str, Any],
    excerpt: str,
    page: int,
    categories: list[dict[str, Any]],
    percents: list[dict[str, Any]],
) -> None:
    """Pair class labels in printed order with percents in printed order.

    The unlabeled triple is always preventive, basic, major. When the
    labels use that same order, one found value is kept. When they do
    not, both interpretations stay and the slots conflict.
    """
    value_count = max(len(percents), 1)
    direct: dict[str, dict[str, Any]] = {}
    assigned: dict[str, dict[str, Any]] = {}
    for category, item in zip(categories, percents):
        name = category["name"]
        apply_percent_decision(fields[name], excerpt, [item], page, value_count)
        if name not in assigned:
            assigned[name] = item
            direct[name] = {
                "kind": "percent",
                "value": item["value"],
                "printed": item["raw"],
                "pos": item["start"],
            }
    apply_class_assignment(fields, excerpt, page, categories, direct, value_count)
    disagree = False
    for index, name in enumerate(PBM_ORDER):
        got = assigned.get(name)
        if got is None or got["start"] != percents[index]["start"]:
            disagree = True
            break
    if disagree:
        # Positional 100/80/50 reading, kept beside the labeled reading.
        parse_unlabeled_triple(fields, excerpt, page, percents)


def parse_classes(fields: dict[str, Any], excerpt: str, page: int) -> None:
    categories = find_categories(excerpt)
    percents = find_percents(excerpt)
    dollars = find_dollars(excerpt)
    pbm = [item for item in categories if item["name"] in PBM_ORDER]
    if (
        len(pbm) == 3
        and len({item["name"] for item in pbm}) == 3
        and len(percents) >= 3
        and not dollars
    ):
        parse_pbm_label_order(fields, excerpt, page, categories, percents)
        return
    if categories:
        parse_labeled(fields, excerpt, page, categories, percents, dollars)
        return
    if len(percents) >= 3:
        parse_unlabeled_triple(fields, excerpt, page, percents)
    # One or two percents with no class label are not a triple and stay not_found.


def parse_unlabeled_triple(
    fields: dict[str, Any],
    excerpt: str,
    page: int,
    percents: list[dict[str, Any]],
) -> None:
    matches = network_matches(excerpt)
    kinds = {kind for _, _, kind in matches}
    if kinds == {"in_network", "out_of_network"} and len(percents) >= 6 and not shared_network_phrase(excerpt, matches):
        grouped: dict[str, list[dict[str, Any]]] = {"in_network": [], "out_of_network": []}
        for item in percents:
            network = networks_for(excerpt, len(percents), item["start"])[0]
            if network in grouped:
                grouped[network].append(item)
        for network, items in grouped.items():
            for category, item in zip(("preventive", "basic", "major"), items[:3]):
                write_found(fields[category][network], item["raw"], item["value"], page, excerpt, "percent")
        return
    first_three = percents[:3]
    if kinds == {"in_network", "out_of_network"}:
        targets = ["in_network", "out_of_network"]
    elif "in_network" in kinds:
        targets = ["in_network"]
    elif "out_of_network" in kinds:
        targets = ["out_of_network"]
    else:
        targets = ["unlabeled"]
    for category, item in zip(("preventive", "basic", "major"), first_three):
        for network in targets:
            write_found(fields[category][network], item["raw"], item["value"], page, excerpt, "percent")


def parse_labeled(
    fields: dict[str, Any],
    excerpt: str,
    page: int,
    categories: list[dict[str, Any]],
    percents: list[dict[str, Any]],
    dollars: list[dict[str, Any]],
) -> None:
    value_count = max(len(percents), len(dollars), 1)
    local: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    for index, category in enumerate(categories):
        right = categories[index + 1]["start"] if index + 1 < len(categories) else len(excerpt)
        own_percents = [item for item in percents if category["end"] <= item["start"] < right]
        own_dollars = [item for item in dollars if category["end"] <= item["start"] < right]
        if not own_percents and not own_dollars:
            left = categories[index - 1]["end"] if index else 0
            own_percents = [item for item in percents if left <= item["start"] < category["start"]]
            own_dollars = [item for item in dollars if left <= item["start"] < category["start"]]
        local.append((category, own_percents, own_dollars))

    direct: dict[str, dict[str, Any]] = {}
    any_local = any(own_percents or own_dollars for _, own_percents, own_dollars in local)
    if not any_local and percents:
        for category, item in zip(categories, percents[: len(categories)]):
            apply_percent_decision(fields[category["name"]], excerpt, [item], page, len(percents))
            direct[category["name"]] = {
                "kind": "percent",
                "value": item["value"],
                "printed": item["raw"],
                "pos": item["start"],
            }
    else:
        for category, own_percents, own_dollars in local:
            name = category["name"]
            if own_percents and own_dollars:
                apply_percent_decision(fields[name], excerpt, own_percents, page, value_count)
                apply_dollars(fields[name], excerpt, own_dollars, page, value_count)
                direct[name] = {
                    "kind": "percent",
                    "value": own_percents[0]["value"],
                    "printed": own_percents[0]["raw"],
                    "pos": own_percents[0]["start"],
                }
                continue
            if own_percents:
                apply_percent_decision(fields[name], excerpt, own_percents, page, value_count)
                direct[name] = {
                    "kind": "percent",
                    "value": own_percents[0]["value"],
                    "printed": own_percents[0]["raw"],
                    "pos": own_percents[0]["start"],
                }
                continue
            if own_dollars:
                apply_dollars(fields[name], excerpt, own_dollars, page, value_count)
                direct[name] = {"kind": "copay", "value": own_dollars[0]["value"], "pos": own_dollars[0]["start"]}

    apply_class_assignment(fields, excerpt, page, categories, direct, value_count)


def deductible_bucket(excerpt: str, pos: int) -> str:
    window = excerpt[max(0, pos - 80) : pos + 40]
    family = FAMILY_RE.search(window) is not None
    person = PERSON_RE.search(window) is not None
    if person and not family:
        return "deductible_individual"
    if family and not person:
        return "deductible_family"
    return "deductible_unlabeled"


def apply_waiting(fields: dict[str, Any], excerpt: str, page: int) -> bool:
    if not re.search(r"(?i)\bwaiting\b", excerpt):
        return False
    named = []
    if ORTHO_RE.search(excerpt):
        named.append("ortho")
    if re.search(r"(?i)\bmajor\b", excerpt):
        named.append("major")
    if re.search(r"(?i)\bbasic\b", excerpt):
        named.append("basic")
    # Preventive waiting is not a field. Do not copy one class onto another.
    if len(named) != 1:
        return True
    target = fields[WAITING_SLOTS[named[0]]]
    if WAITING_NONE_RE.search(excerpt):
        write_found(target, "none", None, page, excerpt)
        return True
    months = MONTH_RE.search(excerpt)
    if months:
        write_found(target, months.group(0), int(months.group(1)), page, excerpt)
        return True
    years = YEAR_RE.search(excerpt)
    if years:
        write_found(target, years.group(0), None, page, excerpt)
        return True
    write_found(target, re.sub(r"\s+", " ", excerpt).strip(), None, page, excerpt)
    return True


def apply_waiver(fields: dict[str, Any], excerpt: str, page: int) -> bool:
    if DEDUCTIBLE_RE.search(excerpt) is None:
        return False
    waived = WAIVED_RE.search(excerpt) is not None
    applies = APPLIES_RE.search(excerpt) is not None
    if not waived and not applies:
        return False
    if PREVENTIVE_WORD_RE.search(excerpt) is None:
        return False
    other = re.search(r"(?i)\b(?:orthodont|basic|major|endodont|periodont|oral\s+surgery)\b", excerpt)
    preventive = PREVENTIVE_WORD_RE.search(excerpt)
    if other and preventive is None:
        return False
    slot = fields["preventive_deductible_waived"]
    if waived and applies:
        write_found(slot, "waived", None, page, excerpt)
        write_found(slot, "not waived", None, page, excerpt)
    elif waived:
        write_found(slot, "waived", None, page, excerpt)
    else:
        write_found(slot, "not waived", None, page, excerpt)
    return find_dollars(excerpt) == []


def apply_deductible(fields: dict[str, Any], excerpt: str, page: int) -> bool:
    if DEDUCTIBLE_RE.search(excerpt) is None:
        return False
    dollars = find_dollars(excerpt)
    if not dollars:
        return False
    for item in dollars:
        bucket = deductible_bucket(excerpt, item["start"])
        put_network(
            fields[bucket],
            excerpt,
            item["start"],
            len(dollars),
            item["raw"],
            item["value"],
            page,
        )
    return True


def family_only_maximum(excerpt: str) -> bool:
    return FAMILY_RE.search(excerpt) is not None and PERSON_RE.search(excerpt) is None


def per_person_dollars(excerpt: str, dollars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if FAMILY_RE.search(excerpt) is None or PERSON_RE.search(excerpt) is None or len(dollars) < 2:
        return dollars
    person = PERSON_RE.search(excerpt)
    if not person:
        return dollars
    chosen = min(dollars, key=lambda item: abs(item["start"] - person.start()))
    return [chosen]


def apply_maximum(fields: dict[str, Any], excerpt: str, page: int) -> bool:
    if MAXIMUM_RE.search(excerpt) is None and not (ORTHO_RE.search(excerpt) and LIFETIME_RE.search(excerpt)):
        return False
    lifetime = LIFETIME_RE.search(excerpt) is not None
    annual = ANNUAL_RE.search(excerpt) is not None
    ortho = ORTHO_RE.search(excerpt) is not None
    dollars = find_dollars(excerpt)
    if lifetime and ortho and dollars:
        for item in dollars:
            write_found(fields["ortho_lifetime_maximum"], item["raw"], item["value"], page, excerpt)
        return True
    if lifetime and not ortho:
        # Lifetime dollars that are not orthodontia fill neither annual nor ortho.
        return True
    if not annual and not lifetime:
        # A maximum with neither annual nor lifetime stays not_found.
        return True
    if not annual:
        return True
    if family_only_maximum(excerpt):
        # A family annual maximum is not the per-person annual maximum.
        return True
    chosen = per_person_dollars(excerpt, dollars)
    for item in chosen:
        put_network(
            fields["annual_maximum"],
            excerpt,
            item["start"],
            len(chosen),
            item["raw"],
            item["value"],
            page,
        )
    return True


def apply_ortho_copay(fields: dict[str, Any], excerpt: str, page: int) -> bool:
    if ORTHO_RE.search(excerpt) is None:
        return False
    if LIFETIME_RE.search(excerpt):
        return False
    if re.search(r"(?i)\bcopay\b|\bpatient\s+cost\b", excerpt) is None:
        return False
    dollars = find_dollars(excerpt)
    percents = find_percents(excerpt)
    if not dollars and not percents:
        return False
    for item in dollars:
        write_found(fields["ortho_coinsurance_or_copay"], item["raw"], item["value"], page, excerpt, "dollars")
    for item in percents:
        if re.search(r"(?i)\bcoinsurance\b|\bcopay\b|\bpatient\s+cost\b", excerpt):
            write_found(fields["ortho_coinsurance_or_copay"], item["raw"], item["value"], page, excerpt, "percent")
    return True


def apply_ortho_age(fields: dict[str, Any], excerpt: str, page: int) -> bool:
    if ORTHO_RE.search(excerpt) is None:
        return False
    if re.search(r"(?i)\bwaiting\b|\bdeductible\b|\bcopay\b", excerpt):
        return False
    no_age = NO_AGE_RE.search(excerpt)
    adult = ADULT_RE.search(excerpt)
    if no_age or adult:
        # Adult orthodontia and "no age limit" are the printed phrase, not a number.
        phrase = no_age.group(0) if no_age else adult.group(0)
        write_found(fields["ortho_age_limit"], phrase, None, page, excerpt)
        return True
    age = AGE_RE.search(excerpt)
    if not age:
        # "Child" or "children" with no age is not_found. Do not invent 19.
        return True
    number = next(group for group in age.groups() if group)
    write_found(fields["ortho_age_limit"], age.group(0), int(number), page, excerpt)
    return True


def apply_basis(fields: dict[str, Any], excerpt: str, page: int) -> bool:
    if BASIS_RE.search(excerpt) is None:
        return False
    if find_categories(excerpt) and find_percents(excerpt):
        return False
    printed = re.sub(r"\s+", " ", excerpt).strip()
    write_found(fields["out_of_network_basis"], printed, None, page, excerpt)
    return True


def apply_excerpt(fields: dict[str, Any], excerpt: str, page: int) -> None:
    if len(PROCEDURE_RE.findall(excerpt)) >= 2:
        return
    if apply_waiting(fields, excerpt, page):
        return
    waiver_done = apply_waiver(fields, excerpt, page)
    if waiver_done:
        return
    if apply_deductible(fields, excerpt, page):
        return
    if apply_maximum(fields, excerpt, page):
        return
    if apply_ortho_copay(fields, excerpt, page):
        return
    if apply_ortho_age(fields, excerpt, page):
        return
    if find_categories(excerpt) or len(find_percents(excerpt)) >= 3:
        parse_classes(fields, excerpt, page)
        return
    apply_basis(fields, excerpt, page)


def clean_label(excerpt: str, label: re.Pattern[str]) -> str:
    text = re.sub(r"\s+", " ", excerpt).strip()
    match = label.match(text)
    if match:
        return text[match.end() :].strip(" :-")
    return text


def apply_carrier(fields: dict[str, Any], raw: Any, pages_read: set[int] | None) -> None:
    for item in as_quote_list(raw):
        excerpt, page = pull_quote(item)
        number = quote_ok(excerpt, page, pages_read)
        if number is None or excerpt is None:
            continue
        printed = clean_label(excerpt, re.compile(r"(?i)^(?:carrier|insurer|insurance company|underwritten by)\s*[:\-]\s*"))
        write_found(fields["carrier"], printed, None, number, excerpt)


def apply_plan_name(fields: dict[str, Any], raw: Any, pages_read: set[int] | None) -> None:
    for item in as_quote_list(raw):
        excerpt, page = pull_quote(item)
        number = quote_ok(excerpt, page, pages_read)
        if number is None or excerpt is None:
            continue
        if printed_plan_name(excerpt) is None:
            continue
        printed = clean_label(excerpt, re.compile(r"(?i)^plan\s+name\s*[:\-]\s*"))
        write_found(fields["plan_name"], printed, None, number, excerpt)


def apply_plan_type(fields: dict[str, Any], raw: Any, pages_read: set[int] | None) -> None:
    for item in as_quote_list(raw):
        excerpt, page = pull_quote(item)
        number = quote_ok(excerpt, page, pages_read)
        if number is None or excerpt is None:
            continue
        seen: list[tuple[str, str]] = []
        for match in PLAN_TYPE_RE.finditer(excerpt):
            token = match.group(0)
            key = token.upper()
            if key == "FEE-FOR-SERVICE":
                key = "FFS"
            if any(existing == key for existing, _ in seen):
                continue
            seen.append((key, token))
        if len(seen) == 1:
            write_found(fields["plan_type"], seen[0][1], None, number, excerpt)
        elif len(seen) > 1:
            for _, token in seen:
                write_found(fields["plan_type"], token, None, number, excerpt)


def pull_quote(item: Any) -> tuple[str | None, Any]:
    if isinstance(item, str):
        return item, None
    if not isinstance(item, dict):
        return None, None
    excerpt = item.get("excerpt")
    if not isinstance(excerpt, str):
        excerpt = item.get("quote")
    if not isinstance(excerpt, str):
        excerpt = item.get("evidence")
    page = item.get("page", item.get("page_number"))
    return excerpt if isinstance(excerpt, str) else None, page


def as_quote_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if any(key in raw for key in ("excerpt", "quote", "evidence")):
            return [raw]
        return []
    if isinstance(raw, str):
        return [raw]
    return []


def collect_excerpts(plan: dict[str, Any]) -> list[Any]:
    lines: list[Any] = []
    for key in ("excerpts", "lines", "statements", "quotes"):
        value = plan.get(key)
        if isinstance(value, list):
            lines.extend(value)
        elif isinstance(value, dict):
            lines.append(value)
        elif isinstance(value, str):
            lines.append(value)
    skip = {
        "carrier",
        "plan_name",
        "plan_type",
        "excerpts",
        "lines",
        "statements",
        "quotes",
        "usable",
        "failure_reason",
    }
    for key, value in plan.items():
        if key in skip:
            continue
        walk_quotes(value, lines)
    return lines


def walk_quotes(value: Any, lines: list[Any]) -> None:
    if isinstance(value, dict):
        if any(isinstance(value.get(key), str) for key in ("excerpt", "quote", "evidence")):
            lines.append(value)
        for child in value.values():
            walk_quotes(child, lines)
        return
    if isinstance(value, list):
        for child in value:
            walk_quotes(child, lines)


def compute_usable(fields: dict[str, Any]) -> str:
    """yes only for a found carrier, per-person annual maximum, and P/B/M with no conflict."""

    def net_ok(group: dict[str, Any]) -> bool:
        slots = (group["in_network"], group["out_of_network"], group["unlabeled"])
        if any(slot.get("status") == "conflict" for slot in slots):
            return False
        return any(slot.get("status") == "found" for slot in slots)

    if fields["carrier"].get("status") != "found":
        return "no"
    # Family-only maximums never fill annual_maximum, so a found slot is per person.
    if not net_ok(fields["annual_maximum"]):
        return "no"
    for name in ("preventive", "basic", "major"):
        if not net_ok(fields[name]):
            return "no"
    return "yes"


def restrict_pages(fields: dict[str, Any], pages_read: set[int] | None) -> None:
    if pages_read is None:
        return

    def fix(slot: dict[str, Any]) -> None:
        status = slot.get("status")
        if status == "found":
            if quote_ok(slot.get("excerpt"), slot.get("page"), pages_read) is None:
                slot.clear()
                slot.update(not_found())
            return
        if status != "conflict":
            return
        kept = []
        for side in slot.get("sides", []):
            if quote_ok(side.get("excerpt"), side.get("page"), pages_read) is not None:
                kept.append(side)
        slot.clear()
        if len(kept) >= 2:
            slot.update({"status": "conflict", "sides": kept})
        elif len(kept) == 1:
            side = kept[0]
            slot.update(
                {
                    "status": "found",
                    "value": side["value"],
                    "page": side["page"],
                    "excerpt": side["excerpt"],
                }
            )
        else:
            slot.update(not_found())

    for _, slot in list(iter_leaves(fields)):
        fix(slot)



CARRIER_LABEL_RE = re.compile(
    r"(?i)^(?:carrier|insurer|insurance company|underwritten by)\s*[:\-]\s*"
)
PLAN_NAME_LABEL_RE = re.compile(r"(?i)^plan\s+name\s*[:\-]\s*")


def printed_plan_name(excerpt: str) -> str | None:
    """A plan name is not an in-network or out-of-network column label."""
    printed = clean_label(excerpt, PLAN_NAME_LABEL_RE)
    residual = IN_RE.sub(" ", printed)
    residual = OUT_RE.sub(" ", residual)
    residual = re.sub(r"(?i)\bnetwork\b", " ", residual)
    residual = re.sub(r"\s+", " ", residual).strip(" :-/,")
    if not residual:
        return None
    return residual


def iter_valid_quotes(raw: Any, pages_read: set[int] | None):
    for item in as_quote_list(raw):
        excerpt, page = pull_quote(item)
        number = quote_ok(excerpt, page, pages_read)
        if number is None or excerpt is None:
            continue
        yield {"page": number, "excerpt": excerpt}


def has_valid_quote(raw: Any, pages_read: set[int] | None) -> bool:
    for _quote in iter_valid_quotes(raw, pages_read):
        return True
    return False


def note_unread(raw: Any, pages_read: set[int] | None, unread: set[int]) -> None:
    if pages_read is None:
        return
    for item in as_quote_list(raw):
        excerpt, page = pull_quote(item)
        number = parse_page(page)
        if number is None or not isinstance(excerpt, str) or excerpt.strip() == "":
            continue
        if number not in pages_read:
            unread.add(number)


def unwrap_plan(plan: Any) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    if isinstance(plan.get("fields"), dict) and "carrier" not in plan and "excerpts" not in plan:
        merged = dict(plan["fields"])
        for key, value in plan.items():
            if key != "fields":
                merged[key] = value
        return merged
    return dict(plan)


def as_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def plan_is_named(plan: Any, pages_read: set[int] | None) -> bool:
    body = unwrap_plan(plan)
    if body is None:
        return False
    for quote in iter_valid_quotes(body.get("plan_name"), pages_read):
        if printed_plan_name(quote["excerpt"]):
            return True
    return False


def plan_type_keys(excerpt: str) -> list[str]:
    seen: list[str] = []
    for match in PLAN_TYPE_RE.finditer(excerpt):
        token = match.group(0)
        key = token.upper()
        if key == "FEE-FOR-SERVICE":
            key = "FFS"
        if key not in seen:
            seen.append(key)
    return seen


def one_shared_quote(raws: list[Any], pages_read: set[int] | None, kind: str) -> dict[str, Any] | None:
    quotes: list[dict[str, Any]] = []
    for raw in raws:
        quotes.extend(iter_valid_quotes(raw, pages_read))
    if kind == "carrier":
        unique: list[tuple[str, dict[str, Any]]] = []
        for quote in quotes:
            printed = clean_label(quote["excerpt"], CARRIER_LABEL_RE)
            if not any(printed == existing for existing, _quote in unique):
                unique.append((printed, quote))
        if len(unique) == 1:
            return unique[0][1]
        return None
    first: dict[str, dict[str, Any]] = {}
    for quote in quotes:
        for key in plan_type_keys(quote["excerpt"]):
            first.setdefault(key, quote)
    if len(first) == 1:
        return next(iter(first.values()))
    return None


def merge_plan_objects(
    plans: list[Any],
    extra_carrier: Any = None,
    extra_type: Any = None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {"excerpts": [], "carrier": [], "plan_name": [], "plan_type": []}
    merged["carrier"].extend(as_items(extra_carrier))
    merged["plan_type"].extend(as_items(extra_type))
    for plan in plans:
        body = unwrap_plan(plan)
        if body is None:
            continue
        merged["carrier"].extend(as_items(body.get("carrier")))
        merged["plan_name"].extend(as_items(body.get("plan_name")))
        merged["plan_type"].extend(as_items(body.get("plan_type")))
        merged["excerpts"].extend(collect_excerpts(body))
    return merged


def unread_reason(unread: set[int]) -> str | None:
    if not unread:
        return None
    numbers = ", ".join(str(number) for number in sorted(unread))
    return f"cited unread page numbers: {numbers}"


def coerce_plan(plan: Any, pages_read: set[int] | None) -> dict[str, Any]:
    fields = skeleton_fields()
    if not isinstance(plan, dict):
        return {"usable": "no", "failure_reason": None, "fields": fields}
    if isinstance(plan.get("fields"), dict) and "carrier" not in plan and "excerpts" not in plan:
        merged = dict(plan["fields"])
        for key, value in plan.items():
            if key != "fields":
                merged[key] = value
        plan = merged
    unread: set[int] = set()
    note_unread(plan.get("carrier"), pages_read, unread)
    note_unread(plan.get("plan_name"), pages_read, unread)
    note_unread(plan.get("plan_type"), pages_read, unread)
    apply_carrier(fields, plan.get("carrier"), pages_read)
    apply_plan_name(fields, plan.get("plan_name"), pages_read)
    apply_plan_type(fields, plan.get("plan_type"), pages_read)
    seen: set[tuple[str, int]] = set()
    for item in collect_excerpts(plan):
        excerpt, page = pull_quote(item)
        note_unread(item, pages_read, unread)
        number = quote_ok(excerpt, page, pages_read)
        if number is None or excerpt is None:
            continue
        key = (excerpt, number)
        if key in seen:
            continue
        seen.add(key)
        lines = [part.strip() for part in excerpt.splitlines() if part.strip()]
        if not lines:
            continue
        for line in lines:
            apply_excerpt(fields, line, number)
    restrict_pages(fields, pages_read)
    return {
        "usable": compute_usable(fields),
        "failure_reason": unread_reason(unread),
        "fields": fields,
    }


def plan_list(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    plans = payload.get("plans")
    if isinstance(plans, list):
        return plans
    if isinstance(payload.get("fields"), dict) or any(
        key in payload for key in ("carrier", "excerpts", "plan_name", "plan_type")
    ):
        return [payload]
    records = payload.get("records")
    if isinstance(records, list):
        return records
    return []


def coerce_model_json(payload: Any, pages_read: set[int] | None = None) -> list[dict[str, Any]]:
    """Coerce model JSON into benefit records. Rules read excerpts, not pixels.

    Fewer than two printed plan names collapse to one record. Unlabeled
    money columns are not plans. A carrier or plan type printed once is
    copied onto each named plan; two different values are not copied.
    """
    plans = plan_list(payload)
    if plans is None:
        return [blank_record("model JSON was not an object")]
    if not plans:
        return [blank_record(None)]
    doc_carrier = None
    doc_type = None
    if isinstance(payload, dict) and isinstance(payload.get("plans"), list):
        doc_carrier = payload.get("carrier")
        doc_type = payload.get("plan_type")
    named = [plan for plan in plans if plan_is_named(plan, pages_read)]
    if len(named) < 2:
        merged = merge_plan_objects(plans, doc_carrier, doc_type)
        return [coerce_plan(merged, pages_read)]
    carrier_raws: list[Any] = [doc_carrier]
    type_raws: list[Any] = [doc_type]
    for plan in plans:
        body = unwrap_plan(plan)
        if body is None:
            continue
        carrier_raws.append(body.get("carrier"))
        type_raws.append(body.get("plan_type"))
    shared_carrier = one_shared_quote(carrier_raws, pages_read, "carrier")
    shared_type = one_shared_quote(type_raws, pages_read, "plan_type")
    records: list[dict[str, Any]] = []
    for plan in named:
        body = unwrap_plan(plan)
        if body is None:
            continue
        if shared_carrier is not None and not has_valid_quote(body.get("carrier"), pages_read):
            body["carrier"] = shared_carrier
        if shared_type is not None and not has_valid_quote(body.get("plan_type"), pages_read):
            body["plan_type"] = shared_type
        records.append(coerce_plan(body, pages_read))
    if not records:
        return [blank_record(None)]
    return records


def redact_secret(message: str) -> str:
    """Remove the API key from a message. Never log the key itself."""
    try:
        secret = os.environ["GEMINI_API_KEY"]
    except KeyError:
        return message
    if secret:
        return message.replace(secret, "[redacted]")
    return message


def render_pdf_pages(path: str) -> tuple[list[bytes] | None, str | None]:
    """Render each PDF page to a PNG at 200 DPI. Temp files are removed."""
    with tempfile.TemporaryDirectory(prefix="dental-pdf-") as tmp:
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
            return None, "pdftoppm is not installed"
        if render.returncode != 0:
            detail = (render.stderr or render.stdout or "pdftoppm failed").strip()
            return None, f"pdftoppm failed: {detail}"
        pages = sorted(
            Path(tmp).glob("page-*.png"),
            key=lambda image: int(image.stem.rsplit("-", 1)[-1]),
        )
        if not pages:
            return None, "pdftoppm produced no page images"
        images: list[bytes] = []
        for image in pages:
            images.append(image.read_bytes())
            image.unlink()
        return images, None


def build_gemini_request(page_pngs: list[bytes]) -> dict[str, Any]:
    """Build the generateContent request. The key is read here, from the environment."""
    key = os.environ["GEMINI_API_KEY"]
    parts: list[dict[str, Any]] = [{"text": GEMINI_PROMPT}]
    for index, png in enumerate(page_pngs, start=1):
        parts.append({"text": f"Page {index} image follows."})
        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(png).decode("ascii"),
                }
            }
        )
    return {
        "url": GEMINI_URL,
        "headers": {
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        },
        "body": {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
    }


def prepare_gemini_request(page_pngs: list[bytes]) -> dict[str, Any]:
    if not CALLS_ENABLED:
        return {"ok": False, "error": "CALLS_ENABLED is false"}
    try:
        if os.environ["GEMINI_API_KEY"] == "":
            return {"ok": False, "error": "GEMINI_API_KEY is not set"}
    except KeyError:
        return {"ok": False, "error": "GEMINI_API_KEY is not set"}
    return {"ok": True, "request": build_gemini_request(page_pngs)}


def execute_gemini_request(request: dict[str, Any]) -> tuple[Any, str | None]:
    data = json.dumps(request["body"]).encode("utf-8")
    outgoing = urllib.request.Request(
        request["url"],
        data=data,
        headers=dict(request["headers"]),
        method="POST",
    )
    try:
        with urllib.request.urlopen(outgoing, timeout=180) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return None, redact_secret(f"gemini http {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        return None, redact_secret(f"gemini request failed: {exc.reason}")
    except TimeoutError as exc:
        return None, redact_secret(f"gemini request failed: {exc}")
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        return None, "gemini response was not JSON"


def model_text_from_response(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        texts = [part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
        if texts:
            return "\n".join(texts)
    return None


def decode_model_json(payload: Any) -> tuple[Any, str | None]:
    if isinstance(payload, dict) and (
        "plans" in payload or "fields" in payload or "carrier" in payload or "records" in payload
    ):
        return payload, None
    text = model_text_from_response(payload)
    if text is None:
        if isinstance(payload, dict):
            return payload, None
        return None, "gemini response had no JSON"
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        return None, "gemini response was not JSON"


def text_result(kind: str, name: str | None) -> dict[str, Any]:
    return {
        "ok": True,
        "records": [blank_record(TEXT_ONLY_REASON)],
        "source": {"kind": kind, "name": name},
    }


def failure_payload(message: str, source: dict[str, Any]) -> dict[str, Any]:
    reason = redact_secret(message)
    return {
        "ok": False,
        "error": reason,
        "records": [blank_record(reason)],
        "source": source,
    }


def emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def extract_text(text: str) -> dict[str, Any]:
    """Text has no page image, so every leaf stays not_found."""
    del text
    return text_result("text", None)


def run_pdf(path: str) -> tuple[dict[str, Any], int]:
    source: dict[str, Any] = {"kind": "pdf-images", "name": path, "provider": "gemini", "model": GEMINI_MODEL}
    if not os.path.isfile(path):
        return failure_payload(f"file not found: {path}", source), 1
    images, err = render_pdf_pages(path)
    if err or not images:
        return failure_payload(err or "pdftoppm produced no page images", source), 1
    pages_read = set(range(1, len(images) + 1))
    source["page_count"] = len(images)
    source["pages_read"] = list(range(1, len(images) + 1))
    prepared = prepare_gemini_request(images)
    if not prepared["ok"]:
        return failure_payload(prepared["error"], source), 1
    payload, call_err = execute_gemini_request(prepared["request"])
    if call_err:
        return failure_payload(call_err, source), 1
    decoded, decode_err = decode_model_json(payload)
    if decode_err or decoded is None:
        return failure_payload(decode_err or "gemini response had no JSON", source), 1
    records = coerce_model_json(decoded, pages_read)
    return {"ok": True, "records": records, "source": source}, 0


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract dental benefit fields from a summary PDF.")
    parser.add_argument("file", nargs="?", help="PDF file. Use - or omit to read stdin as text.")
    parser.add_argument("--text", dest="text_path", help="Read this path as text. Does not call Gemini.")
    args = parser.parse_args(argv)
    try:
        if args.text_path:
            if not os.path.isfile(args.text_path):
                emit(failure_payload(f"file not found: {args.text_path}", {"kind": "text", "name": args.text_path}))
                return 1
            with open(args.text_path, encoding="utf-8", errors="replace") as handle:
                handle.read()
            result = text_result("text", args.text_path)
            emit(result)
            return 0
        if not args.file or args.file == "-":
            sys.stdin.read()
            emit(text_result("stdin", None))
            return 0
        if args.file.lower().endswith(".pdf"):
            result, code = run_pdf(args.file)
            emit(result)
            return code
        if not os.path.isfile(args.file):
            emit(failure_payload(f"file not found: {args.file}", {"kind": "text", "name": args.file}))
            return 1
        with open(args.file, encoding="utf-8", errors="replace") as handle:
            handle.read()
        emit(text_result("text", args.file))
        return 0
    except OSError as exc:
        emit(failure_payload(redact_secret(str(exc)), {"kind": "file", "name": args.text_path or args.file}))
        return 1


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
