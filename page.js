"use strict";

const statusEl = document.getElementById("status");
const fileEl = document.getElementById("file");
const pagesEl = document.getElementById("pages");
const recordsEl = document.getElementById("records");

const PDFJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.8.69/pdf.min.mjs";
const PDFJS_WORKER = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.8.69/pdf.worker.min.mjs";

const NOT_EXTRACTED = "Nothing is extracted in the browser.";

let pdfjsPromise = null;
let requestSeq = 0;

function setStatus(message, isError) {
  statusEl.textContent = message;
  statusEl.className = isError ? "status error" : "status";
}

function clearPages() {
  pagesEl.replaceChildren();
}

function clearRecords() {
  recordsEl.replaceChildren();
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

function addText(parent, tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  parent.appendChild(node);
  return node;
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

function isPngDataUrl(value) {
  return typeof value === "string" && /^data:image\/png;base64,[a-z0-9+/=\s]+$/i.test(value);
}

function showServerImages(images) {
  clearPages();
  const list = Array.isArray(images) ? images : [];
  list.forEach((image) => {
    if (!image || !isPngDataUrl(image.data_url)) {
      return;
    }
    const pageNumber = image.page;
    const figure = document.createElement("figure");
    const caption = document.createElement("figcaption");
    caption.textContent = "Page " + pageNumber;
    const img = document.createElement("img");
    img.alt = "Page " + pageNumber + " from this extraction";
    img.src = image.data_url;
    figure.appendChild(caption);
    figure.appendChild(img);
    pagesEl.appendChild(figure);
  });
  return pagesEl.childElementCount;
}

function formatValue(value) {
  if (!value || typeof value !== "object") {
    return "(no value)";
  }
  const printed = typeof value.printed === "string" && value.printed ? value.printed : "";
  const hasNormalized = value.normalized !== null && value.normalized !== undefined;
  if (printed && hasNormalized) {
    return printed + " (normalized " + String(value.normalized) + ")";
  }
  if (printed) {
    return printed;
  }
  if (hasNormalized) {
    return "normalized " + String(value.normalized);
  }
  return "(no value)";
}

function appendEvidence(parent, item) {
  addText(parent, "p", "Value: " + formatValue(item && item.value)).className = "meta";
  const page = item && item.page !== null && item.page !== undefined ? String(item.page) : "(none)";
  addText(parent, "p", "Page: " + page).className = "meta";
  addText(parent, "p", "Excerpt:").className = "meta";
  const excerpt = document.createElement("pre");
  excerpt.className = "excerpt";
  excerpt.textContent = item && typeof item.excerpt === "string" ? item.excerpt : "";
  parent.appendChild(excerpt);
}

function walkLeaves(node, path, rows) {
  if (!node || typeof node !== "object") {
    return;
  }
  if (node.status === "found" || node.status === "not_found" || node.status === "conflict") {
    rows.push({ path: path, leaf: node });
    return;
  }
  Object.keys(node).forEach((key) => {
    walkLeaves(node[key], path ? path + "." + key : key, rows);
  });
}

function showRecords(records) {
  clearRecords();
  if (!Array.isArray(records) || records.length === 0) {
    addText(recordsEl, "p", "No plan records. This page does not guess benefits.");
    return;
  }
  records.forEach((record, index) => {
    const plan = document.createElement("article");
    plan.className = "plan";
    addText(plan, "h3", "Plan " + (index + 1));
    const usable = record && record.usable === true;
    addText(plan, "p", "usable: " + (usable ? "true" : "false")).className = "meta";
    if (record && typeof record.failure_reason === "string" && record.failure_reason) {
      addText(plan, "p", "failure_reason: " + record.failure_reason).className = "meta";
    }
    const rows = [];
    walkLeaves(record && record.fields, "", rows);
    if (rows.length === 0) {
      addText(plan, "p", "No fields on this record.");
    }
    rows.forEach((row) => {
      const leaf = row.leaf;
      const block = document.createElement("div");
      block.className = "leaf";
      if (leaf.status === "not_found") {
        block.className = "leaf not-found";
        addText(block, "p", row.path + " — not_found");
        plan.appendChild(block);
        return;
      }
      if (leaf.status === "conflict") {
        addText(block, "p", row.path + " — conflict");
        const sides = Array.isArray(leaf.sides) ? leaf.sides : [];
        sides.forEach((side, sideIndex) => {
          addText(block, "p", "Side " + (sideIndex + 1));
          appendEvidence(block, side);
        });
        plan.appendChild(block);
        return;
      }
      addText(block, "p", row.path + " — found");
      appendEvidence(block, leaf);
      plan.appendChild(block);
    });
    recordsEl.appendChild(plan);
  });
}

async function extractFile(file) {
  const seq = ++requestSeq;
  clearPages();
  clearRecords();
  fileEl.disabled = true;
  setStatus("Sending the PDF to the extractor on this origin…");
  try {
    const response = await fetch(new URL("extract", window.location.href), {
      method: "POST",
      headers: { "Content-Type": "application/pdf" },
      body: file,
    });
    if (seq !== requestSeq) {
      return;
    }
    const data = await response.json();
    if (!data || !Array.isArray(data.records)) {
      throw new Error("The extractor did not return plan records.");
    }
    const imageCount = showServerImages(data.images);
    showRecords(data.records);
    const pageNoun = imageCount === 1 ? "page image" : "page images";
    setStatus(
      "Showing " + imageCount + " " + pageNoun + " and " + data.records.length +
      " plan record" + (data.records.length === 1 ? "" : "s") + ". " + NOT_EXTRACTED
    );
  } catch (error) {
    if (seq !== requestSeq) {
      return;
    }
    clearRecords();
    addText(recordsEl, "p", "No plan records. This page does not guess benefits. " + NOT_EXTRACTED);
    const message = error && error.message ? error.message : String(error);
    try {
      const pageCount = await renderPdfPages(file);
      if (seq !== requestSeq) {
        return;
      }
      const noun = pageCount === 1 ? "page" : "pages";
      setStatus(
        "This origin did not return an extraction (" + message + "). Rendered " +
        pageCount + " " + noun + " locally. " + NOT_EXTRACTED,
        true
      );
    } catch (renderError) {
      const renderMessage = renderError && renderError.message ? renderError.message : String(renderError);
      setStatus(NOT_EXTRACTED + " Could not extract or render the PDF: " + renderMessage, true);
    }
  } finally {
    if (seq === requestSeq) {
      fileEl.disabled = false;
    }
  }
}

fileEl.addEventListener("change", () => {
  const file = fileEl.files && fileEl.files[0];
  if (!file) {
    clearPages();
    clearRecords();
    setStatus(NOT_EXTRACTED);
    return;
  }
  if (!isPdfFile(file)) {
    clearPages();
    clearRecords();
    setStatus(NOT_EXTRACTED + " Choose a PDF.");
    return;
  }
  extractFile(file);
});

document.getElementById("clear-file").addEventListener("click", () => {
  requestSeq += 1;
  fileEl.value = "";
  fileEl.disabled = false;
  clearPages();
  clearRecords();
  setStatus("File cleared. " + NOT_EXTRACTED);
});

setStatus(NOT_EXTRACTED);
