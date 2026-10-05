#!/usr/bin/env python3
"""Serve the page and run extraction in this process.

The browser posts a PDF to POST /extract on this origin. This process
renders page images and calls the existing extract path. The JSON
response is page images plus coercer records. The API key stays in
this process. It is not written into the response.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import extract

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
MAX_UPLOAD = 32 * 1024 * 1024

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/page.js": ("page.js", "text/javascript; charset=utf-8"),
}

# Keys that must never leave this process, even if a model payload
# included them. Benefit leaves are copied by status, not by this list.
DROP_KEY_NAMES = {
    "confidence",
    "confidence_score",
    "score",
    "scores",
    "rate",
    "rates",
    "premium",
    "premiums",
    "contribution",
    "contributions",
    "token",
    "tokens",
    "token_count",
    "totaltokencount",
    "usage",
    "usagemetadata",
    "raw",
    "raw_text",
    "raw_model",
    "raw_model_text",
    "model_text",
    "model_response",
    "prose",
    "summary",
    "candidates",
    "parts",
    "prompt",
    "api_key",
    "apikey",
    "x-goog-api-key",
}


def _drop_key(name: str) -> bool:
    lowered = name.lower().replace("-", "_")
    if lowered in DROP_KEY_NAMES:
        return True
    if "confidence" in lowered or "api_key" in lowered or "token" in lowered:
        return True
    if lowered in {"rate", "rates"} or lowered.endswith("_rate") or lowered.endswith("_rates"):
        return True
    return False


def page_images(pngs: list[bytes] | None) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for index, png in enumerate(pngs or [], start=1):
        if not isinstance(png, (bytes, bytearray)) or not png:
            continue
        encoded = base64.b64encode(bytes(png)).decode("ascii")
        images.append(
            {
                "page": index,
                "data_url": "data:image/png;base64," + encoded,
            }
        )
    return images


def _clean_reason(reason: Any) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str):
        return None
    text = extract.redact_secret(reason)
    lowered = text.lower()
    if any(
        token in lowered
        for token in (
            "x-goog-api-key",
            "inline_data",
            "candidates",
            "generativelanguage",
            "usagemetadata",
        )
    ):
        text = "gemini request failed"
    if len(text) > 500:
        text = text[:500]
    return extract.redact_secret(text)


def _sanitize_value(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    printed = value.get("printed")
    normalized = value.get("normalized")
    if not isinstance(printed, str):
        printed = None
    if isinstance(normalized, bool) or not isinstance(normalized, (int, float, type(None))):
        normalized = None
    return {"printed": printed, "normalized": normalized}


def _sanitize_side(side: Any) -> dict[str, Any] | None:
    if not isinstance(side, dict):
        return None
    page = side.get("page")
    excerpt = side.get("excerpt")
    return {
        "value": _sanitize_value(side.get("value")),
        "page": page if isinstance(page, int) and not isinstance(page, bool) else None,
        "excerpt": excerpt if isinstance(excerpt, str) else None,
    }


def _sanitize_leaf(leaf: Any) -> dict[str, Any]:
    if not isinstance(leaf, dict):
        return extract.not_found()
    status = leaf.get("status")
    if status == "found":
        page = leaf.get("page")
        excerpt = leaf.get("excerpt")
        return {
            "status": "found",
            "value": _sanitize_value(leaf.get("value")),
            "page": page if isinstance(page, int) and not isinstance(page, bool) else None,
            "excerpt": excerpt if isinstance(excerpt, str) else None,
        }
    if status == "conflict":
        sides = []
        for side in leaf.get("sides") or []:
            cleaned = _sanitize_side(side)
            if cleaned is not None:
                sides.append(cleaned)
        return {"status": "conflict", "sides": sides}
    return extract.not_found()


def _lookup(obj: Any, path: str) -> Any:
    current = obj
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def sanitize_fields(fields: Any) -> dict[str, Any]:
    clean = extract.skeleton_fields()
    source = fields if isinstance(fields, dict) else {}
    for path, slot in list(extract.iter_leaves(clean)):
        found = _lookup(source, path)
        slot.clear()
        slot.update(_sanitize_leaf(found))
    return clean


def sanitize_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return extract.blank_record("model JSON was not an object")
    fields = sanitize_fields(record.get("fields"))
    return {
        "usable": extract.compute_usable(fields),
        "failure_reason": _clean_reason(record.get("failure_reason")),
        "fields": fields,
    }


def _redact_tree(value: Any) -> Any:
    if isinstance(value, str):
        return extract.redact_secret(value)
    if isinstance(value, list):
        return [_redact_tree(item) for item in value]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _drop_key(key):
                continue
            cleaned[key] = _redact_tree(item)
        return cleaned
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if value is None or isinstance(value, bool):
        return value
    return None


def build_browser_response(records: Any, pngs: list[bytes] | None) -> dict[str, Any]:
    """JSON object the browser may see: images and coercer records only."""
    if not isinstance(records, list) or not records:
        records = [extract.blank_record(None)]
    payload = {
        "images": page_images(pngs),
        "records": [sanitize_record(record) for record in records],
    }
    cleaned = _redact_tree(payload)
    if not isinstance(cleaned, dict):
        cleaned = {
            "images": [],
            "records": [extract.blank_record(None)],
        }
    cleaned["images"] = cleaned.get("images") if isinstance(cleaned.get("images"), list) else []
    recs = cleaned.get("records")
    if not isinstance(recs, list) or not recs:
        cleaned["records"] = [extract.blank_record(None)]
    # Drop anything a later edit might have attached.
    return {"images": cleaned["images"], "records": cleaned["records"]}


def public_json(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    text = extract.redact_secret(text)
    if "x-goog-api-key" in text.lower() or "generativelanguage" in text.lower():
        text = extract.redact_secret(text)
        text = re.sub(r"(?i)x-goog-api-key", "[redacted]", text)
        text = re.sub(r"(?i)generativelanguage\.googleapis\.com\S*", "[redacted]", text)
    return text


def pdf_bytes_from_body(body: bytes, content_type: str) -> bytes | None:
    if not body:
        return None
    ctype = content_type or ""
    if body.startswith(b"%PDF"):
        return body
    if "multipart/form-data" not in ctype.lower():
        if "application/pdf" in ctype.lower():
            return body
        return None
    match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;\s]+))", ctype, flags=re.IGNORECASE)
    if not match:
        return None
    boundary = (match.group(1) or match.group(2) or "").strip().encode("utf-8", errors="replace")
    if not boundary:
        return None
    for part in body.split(b"--" + boundary):
        if not part or part in (b"--", b"--\r\n"):
            continue
        chunk = part
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        if chunk.endswith(b"--"):
            chunk = chunk[:-2]
            if chunk.endswith(b"\r\n"):
                chunk = chunk[:-2]
        if b"\r\n\r\n" not in chunk:
            continue
        header, data = chunk.split(b"\r\n\r\n", 1)
        header_text = header.decode("utf-8", errors="replace").lower()
        if (
            "filename=" in header_text
            or "application/pdf" in header_text
            or data.startswith(b"%PDF")
        ):
            return data
    return None


def extract_pdf_bytes(pdf_bytes: bytes) -> dict[str, Any]:
    """Render pages, call the existing extract path, and return a browser payload."""
    images: list[bytes] = []
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return build_browser_response([extract.blank_record("upload is not a PDF")], [])
    with tempfile.TemporaryDirectory(prefix="dental-upload-") as tmp:
        path = str(Path(tmp) / "upload.pdf")
        Path(path).write_bytes(pdf_bytes)
        rendered, err = extract.render_pdf_pages(path)
        if rendered:
            images = rendered
        if err or not images:
            return build_browser_response(
                [extract.blank_record(err or "pdftoppm produced no page images")],
                images,
            )
        pages_read = set(range(1, len(images) + 1))
        prepared = extract.prepare_gemini_request(images)
        if not prepared.get("ok"):
            return build_browser_response(
                [extract.blank_record(str(prepared.get("error") or "GEMINI_API_KEY is not set"))],
                images,
            )
        payload, call_err = extract.execute_gemini_request(prepared["request"])
        if call_err or payload is None:
            return build_browser_response(
                [extract.blank_record(call_err or "gemini response had no JSON")],
                images,
            )
        decoded, decode_err = extract.decode_model_json(payload)
        if decode_err or decoded is None:
            return build_browser_response(
                [extract.blank_record(decode_err or "gemini response had no JSON")],
                images,
            )
        records = extract.coerce_model_json(decoded, pages_read)
        return build_browser_response(records, images)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Request line and status only. Never the body or the environment.
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_bytes(self, status: int, content_type: str, raw: bytes, cache: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, payload: dict[str, Any]) -> None:
        raw = public_json(payload).encode("utf-8")
        self._send_bytes(200, "application/json; charset=utf-8", raw, "no-store")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        item = STATIC.get(path)
        if item is None:
            self.send_error(404)
            return
        name, content_type = item
        raw = (ROOT / name).read_bytes()
        self._send_bytes(200, content_type, raw, "no-cache")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/extract":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_UPLOAD:
            self._send_json(build_browser_response([extract.blank_record("upload is too large")], []))
            return
        body = self.rfile.read(length) if length else b""
        pdf = pdf_bytes_from_body(body, self.headers.get("Content-Type", ""))
        if pdf is None:
            self._send_json(build_browser_response([extract.blank_record("upload is not a PDF")], []))
            return
        try:
            payload = extract_pdf_bytes(pdf)
        except Exception as exc:
            payload = build_browser_response(
                [extract.blank_record(extract.redact_secret(str(exc)))],
                [],
            )
        self._send_json(payload)


def make_server(host: str = HOST, port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the dental extractor on localhost.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        httpd = make_server(HOST, args.port)
    except OSError as exc:
        sys.stderr.write(f"server failed to start: {exc}\n")
        return 1
    bound = httpd.server_address[1]
    sys.stderr.write(f"listening on http://{HOST}:{bound}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
