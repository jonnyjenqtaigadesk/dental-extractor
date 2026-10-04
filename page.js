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
const TESSERACT_URL = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
const TESSERACT_WORKER = "https://cdn.jsdelivr.net/npm/tesseract.js@v5.1.1/dist/worker.min.js";
const TESSERACT_CORE = "https://cdn.jsdelivr.net/npm/tesseract.js-core@v5.1.1";
const TESSERACT_LANG = "https://cdn.jsdelivr.net/npm/@tesseract.js-data/eng/4.0.0_best_int";

let pyodidePromise = null;
let pdfjsPromise = null;
let tesseractPromise = null;

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

function loadTesseract() {
  if (window.Tesseract && typeof window.Tesseract.createWorker === "function") {
    return Promise.resolve(window.Tesseract);
  }
  if (!tesseractPromise) {
    tesseractPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = TESSERACT_URL;
      script.async = true;
      script.onload = () => {
        if (!window.Tesseract || typeof window.Tesseract.createWorker !== "function") {
          reject(new Error("Tesseract.js loaded but createWorker is missing"));
          return;
        }
        resolve(window.Tesseract);
      };
      script.onerror = () => reject(new Error("Could not load Tesseract.js from " + TESSERACT_URL));
      document.head.appendChild(script);
    }).catch((error) => {
      tesseractPromise = null;
      throw error;
    });
  }
  return tesseractPromise;
}

function isPdfFile(file) {
  const name = (file.name || "").toLowerCase();
  return file.type === "application/pdf" || name.endsWith(".pdf");
}

async function pdfFileToOcr(file) {
  const pdfjs = await loadPdfJs();
  const TesseractLib = await loadTesseract();
  const data = new Uint8Array(await file.arrayBuffer());
  const doc = await pdfjs.getDocument({ data: data }).promise;
  clearPages();
  const worker = await TesseractLib.createWorker("eng", 1, {
    workerPath: TESSERACT_WORKER,
    corePath: TESSERACT_CORE,
    langPath: TESSERACT_LANG,
  });
  const pageTexts = [];
  try {
    for (let i = 1; i <= doc.numPages; i++) {
      setStatus("Rendering page " + i + " of " + doc.numPages + " to an image…");
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
      setStatus("Tesseract is reading the image of page " + i + " of " + doc.numPages + "…");
      const recognized = await worker.recognize(canvas);
      const text = recognized && recognized.data && recognized.data.text ? recognized.data.text : "";
      pageTexts.push(text.replace(/\s+$/, ""));
    }
  } finally {
    await worker.terminate();
  }
  return pageTexts.join("\n\n");
}

async function inputText() {
  const file = fileEl.files && fileEl.files[0];
  if (file) {
    if (isPdfFile(file)) {
      setStatus("Rendering the PDF to page images…");
      return {
        label: "pdf images then Tesseract OCR, not a vision LLM: " + (file.name || "upload"),
        text: await pdfFileToOcr(file),
        ocr: true,
      };
    }
    setStatus("Reading text file…");
    return {
      label: "text file: " + (file.name || "upload"),
      text: await file.text(),
      ocr: false,
    };
  }
  return { label: "pasted text", text: summaryEl.value, ocr: false };
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
    const source = await inputText();
    if (source.ocr && !String(source.text || "").trim()) {
      setStatus("OCR returned no text. Benefits stay not_found.");
    } else {
      setStatus("Running extract_text from " + extractPyUrl().href + " …");
    }
    const result = await extract(source.text);
    summaryOut.textContent = result.readable_summary || "";
    jsonOut.textContent = JSON.stringify(result, null, 2);
    textOut.textContent = source.text;
    if (source.ocr && !String(source.text || "").trim()) {
      setStatus("Done. OCR returned no text, so benefits stay not_found. Source: " + source.label + ".");
    } else {
      setStatus("Done. Source: " + source.label + ".");
    }
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
  .then(() => setStatus("Ready. Python loaded extract.py from this site. A PDF is rendered to images, then Tesseract reads those images. This is not a vision LLM."))
  .catch((error) => {
    const message = error && error.stack ? String(error.stack) : String(error);
    jsonOut.textContent = message;
    setStatus("Could not load extract.py: " + (error && error.message ? error.message : message), true);
  });
