"use strict";

const statusEl = document.getElementById("status");
const summaryEl = document.getElementById("summary");
const fileEl = document.getElementById("file");
const summaryOut = document.getElementById("summary-out");
const jsonOut = document.getElementById("json-out");
const textOut = document.getElementById("text-out");

const PDFJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.8.69/pdf.min.mjs";
const PDFJS_WORKER = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.8.69/pdf.worker.min.mjs";

let pyodidePromise = null;
let pdfjsPromise = null;

function setStatus(message, isError) {
  statusEl.textContent = message;
  statusEl.className = isError ? "status error" : "status";
}

function extractPyUrl() {
  return new URL("extract.py", document.baseURI);
}

function loadRuntime() {
  if (!pyodidePromise) {
    pyodidePromise = (async () => {
      if (typeof loadPyodide !== "function") {
        throw new Error("loadPyodide is not available. The Pyodide script did not load.");
      }
      setStatus("Loading Python in the browser…");
      const pyodide = await loadPyodide({
        indexURL: "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/",
      });
      const url = extractPyUrl();
      const response = await fetch(url.href, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(
          "Could not fetch extract.py from " + url.href + " (" + response.status + " " + response.statusText + ")"
        );
      }
      const source = await response.text();
      if (!source.includes("def extract_text(text: str)")) {
        throw new Error("Fetched extract.py from " + url.href + " does not contain def extract_text(text: str)");
      }
      pyodide.FS.writeFile("/home/pyodide/extract.py", source);
      pyodide.runPython(`
import sys
sys.path.insert(0, "/home/pyodide")
if "extract" in sys.modules:
    del sys.modules["extract"]
import extract
`);
      return pyodide;
    })().catch((error) => {
      pyodidePromise = null;
      throw error;
    });
  }
  return pyodidePromise;
}

function loadPdfJs() {
  if (!pdfjsPromise) {
    pdfjsPromise = import(PDFJS_URL).then((pdfjs) => {
      pdfjs.GlobalWorkerOptions.workerSrc = PDFJS_WORKER;
      return pdfjs;
    });
  }
  return pdfjsPromise;
}

function itemsToText(items) {
  const rows = [];
  for (const item of items) {
    if (!item || !item.str) continue;
    const x = item.transform[4];
    const y = item.transform[5];
    let row = null;
    for (const candidate of rows) {
      if (Math.abs(candidate.y - y) < 2) {
        row = candidate;
        break;
      }
    }
    if (!row) {
      row = { y: y, parts: [] };
      rows.push(row);
    }
    row.parts.push({ x: x, str: item.str });
  }
  rows.sort((a, b) => b.y - a.y);
  return rows
    .map((row) => {
      row.parts.sort((a, b) => a.x - b.x);
      return row.parts.map((part) => part.str).join("");
    })
    .join("\n");
}

async function pdfFileToText(file) {
  const pdfjs = await loadPdfJs();
  const data = new Uint8Array(await file.arrayBuffer());
  const doc = await pdfjs.getDocument({ data: data }).promise;
  const pages = [];
  for (let i = 1; i <= doc.numPages; i++) {
    const page = await doc.getPage(i);
    const content = await page.getTextContent();
    pages.push(itemsToText(content.items));
  }
  return pages.join("\n\n");
}

function isPdfFile(file) {
  const name = (file.name || "").toLowerCase();
  return file.type === "application/pdf" || name.endsWith(".pdf");
}

async function inputText() {
  const file = fileEl.files && fileEl.files[0];
  if (file) {
    if (isPdfFile(file)) {
      setStatus("Reading PDF in the browser with PDF.js…");
      return {
        label: "pdf (browser PDF.js, not pdftotext): " + (file.name || "upload"),
        text: await pdfFileToText(file),
      };
    }
    setStatus("Reading text file…");
    return {
      label: "text file: " + (file.name || "upload"),
      text: await file.text(),
    };
  }
  return { label: "pasted text", text: summaryEl.value };
}

async function extract(text) {
  const pyodide = await loadRuntime();
  pyodide.FS.writeFile("/tmp/input.txt", text);
  const raw = pyodide.runPython(`
import json
with open("/tmp/input.txt", encoding="utf-8") as handle:
    text = handle.read()
json.dumps(extract.extract_text(text), ensure_ascii=False, indent=2)
`);
  return JSON.parse(raw);
}

async function onRun() {
  summaryOut.textContent = "";
  jsonOut.textContent = "";
  textOut.textContent = "";
  const button = document.getElementById("run");
  button.disabled = true;
  try {
    const source = await inputText();
    setStatus("Running extract_text from " + extractPyUrl().href + " …");
    const result = await extract(source.text);
    summaryOut.textContent = result.readable_summary || "";
    jsonOut.textContent = JSON.stringify(result, null, 2);
    textOut.textContent = source.text;
    setStatus("Done. Source: " + source.label + ".");
  } catch (error) {
    const message = error && error.stack ? String(error.stack) : String(error);
    jsonOut.textContent = message;
    setStatus("Failed: " + (error && error.message ? error.message : message), true);
  } finally {
    button.disabled = false;
  }
}

document.getElementById("run").addEventListener("click", onRun);
document.getElementById("clear-file").addEventListener("click", () => {
  fileEl.value = "";
  setStatus("File cleared. Extract will use the paste box.");
});

loadRuntime()
  .then(() => setStatus("Ready. Python loaded extract.py from this site."))
  .catch((error) => {
    const message = error && error.stack ? String(error.stack) : String(error);
    jsonOut.textContent = message;
    setStatus("Could not load extract.py: " + (error && error.message ? error.message : message), true);
  });
