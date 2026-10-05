"use strict";

const statusEl = document.getElementById("status");
const fileEl = document.getElementById("file");
const pagesEl = document.getElementById("pages");

const PDFJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.8.69/pdf.min.mjs";
const PDFJS_WORKER = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.8.69/pdf.worker.min.mjs";

const NOT_EXTRACTED = "Nothing is extracted in the browser.";

let pdfjsPromise = null;

function setStatus(message, isError) {
  statusEl.textContent = message;
  statusEl.className = isError ? "status error" : "status";
}

function clearPages() {
  pagesEl.replaceChildren();
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
    setStatus("Rendering page " + i + " of " + doc.numPages + "… " + NOT_EXTRACTED);
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

async function onRender() {
  clearPages();
  const button = document.getElementById("render");
  button.disabled = true;
  try {
    const file = fileEl.files && fileEl.files[0];
    if (!file) {
      setStatus(NOT_EXTRACTED);
      return;
    }
    if (!isPdfFile(file)) {
      setStatus(NOT_EXTRACTED + " Choose a PDF to render page images.");
      return;
    }
    const pageCount = await renderPdfPages(file);
    const noun = pageCount === 1 ? "page" : "pages";
    setStatus("Rendered " + pageCount + " " + noun + ". " + NOT_EXTRACTED);
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    setStatus(NOT_EXTRACTED + " Could not render the PDF: " + message, true);
  } finally {
    button.disabled = false;
  }
}

document.getElementById("render").addEventListener("click", onRender);
document.getElementById("clear-file").addEventListener("click", () => {
  fileEl.value = "";
  clearPages();
  setStatus("File cleared. " + NOT_EXTRACTED);
});

setStatus(NOT_EXTRACTED);
