"use strict";

const statusEl = document.getElementById("status");
const summaryEl = document.getElementById("summary");
const fileEl = document.getElementById("file");
const summaryOut = document.getElementById("summary-out");
const jsonOut = document.getElementById("json-out");
const textOut = document.getElementById("text-out");
const pagesEl = document.getElementById("pages");

const PDFJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.8.69/pdf.min.mjs";
const PDFJS_WORKER = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.8.69/pdf.worker.min.mjs";

const NOT_CALLED = "Pages rendered. Gemini runs in the CLI, not in the browser, so no plan fields were extracted.";

let pyodidePromise = null;
let pdfjsPromise = null;

function setStatus(message, isError) {
  statusEl.textContent = message;
  statusEl.className = isError ? "status error" : "status";
}

function extractPyUrl() {
  return new URL("extract.py", document.baseURI);
}

function clearPages() {
  pagesEl.replaceChildren();
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

function isPdfFile(file) {
  const name = (file.name || "").toLowerCase();
  return file.type === "application/pdf" || name.endsWith(".pdf");
}

async function renderPdfPages(file) {
  const pdfjs = await loadPdfJs();
  const data = new Uint8Array(await file.arrayBuffer());
  const doc = await pdfjs.getDocument({ data: data }).promise;
  clearPages();
  for (let i = 1; i <= doc.numPages; i++) {
    setStatus("Rendering page " + i + " of " + doc.numPages + "…");
    const page = await doc.getPage(i);
    const viewport = page.getViewport({ scale: 2 });
    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);
    const context = canvas.getContext("2d", { alpha: false });
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    await page.render({ canvasContext: context, viewport: viewport }).promise;
    const figure = document.createElement("figure");
    const caption = document.createElement("figcaption");
    caption.textContent = "Page " + i;
    figure.appendChild(caption);
    figure.appendChild(canvas);
    pagesEl.appendChild(figure);
  }
  return doc.numPages;
}

async function inputText() {
  const file = fileEl.files && fileEl.files[0];
  if (file) {
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
  clearPages();
  const button = document.getElementById("run");
  button.disabled = true;
  try {
    const file = fileEl.files && fileEl.files[0];
    if (file && isPdfFile(file)) {
      const pageCount = await renderPdfPages(file);
      const payload = {
        ok: false,
        error: "Gemini runs in the CLI, not in the browser",
        source: {
          kind: "pdf-images",
          page_count: pageCount,
          provider: "gemini",
        },
      };
      summaryOut.textContent = NOT_CALLED;
      jsonOut.textContent = JSON.stringify(payload, null, 2);
      textOut.textContent = "";
      setStatus(NOT_CALLED);
      return;
    }
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
  .then(() => setStatus("Ready. Paste or a .txt file runs extract_text in the browser. A PDF is rendered to images only. Gemini runs in the CLI, not in the browser."))
  .catch((error) => {
    const message = error && error.stack ? String(error.stack) : String(error);
    jsonOut.textContent = message;
    setStatus("Could not load extract.py: " + (error && error.message ? error.message : message), true);
  });
