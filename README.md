# Dental benefit summary extractor

Local CLI for other agents. It reads a pasted text summary or a PDF and returns one JSON object. For pasted text, stdin, and `--text`, every benefit value is either quoted from that text or explicitly `not_found`. It does not invent numbers, percents, ages, waiting periods, class mappings, or yes/no answers.

OCR is removed. A PDF is rendered to page images and those images are sent to Gemini. The CLI reads `GEMINI_API_KEY` from the environment at call time. The public page does not contain the key and does not call Gemini. No pip installs. Python 3 standard library plus the `pdftoppm` binary from poppler.

## How to run

```bash
python3 /workspace/dental-extractor/extract.py /path/to/summary.pdf
python3 /workspace/dental-extractor/extract.py /path/to/summary.txt
python3 /workspace/dental-extractor/extract.py --text /path/to/summary.txt
python3 /workspace/dental-extractor/extract.py < /path/to/summary.txt
python3 /workspace/dental-extractor/extract.py -
python3 /workspace/dental-extractor/test_extract.py
```

Stdout for pasted text, stdin, and `--text` is one JSON object (`ok`, `source`, `fields`, `notes`, `readable_summary`). `notes` is a list of short verbatim evidence snippets. `extract_text` is only for those text inputs. It is not run on PDF contents.

A PDF is not read as text. Each page is rendered to a PNG:

```bash
pdftoppm -png -r 200 <file.pdf> <prefix>
```

Those images are sent to Gemini (`GEMINI_MODEL`, currently `gemini-3.8-flash`) as inline `image/png` parts on `generateContent`. `CALLS_ENABLED` is `True`. Temp PNGs are deleted before the request returns. A field is found only when the model returns a non-empty evidence string; otherwise it is forced to `not_found` with `value` null. `extract_text` is not run on the PDF or on the model prose. If rendering fails, the process prints `{"ok": false, "error": "<render error>"}` and no benefits. If `GEMINI_API_KEY` is missing, the result is `{"ok": false, "error": "GEMINI_API_KEY is not set"}` and there is no network call. The key is read only from the environment at call time, never from a file.

Model id: `gemini-3.8-flash`. Doc: <https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash>. That page lists it as the current stable Flash model and says its inputs include images and its output is text. Image-generation Flash models are a different task, so they are not used. The same id is the image-understanding example on <https://ai.google.dev/gemini-api/docs/generate-content/image-understanding>.

## No-invent rule

Every leaf is:

```json
{"status": "found", "value": "<printed string or number>", "evidence": "<exact substring of the source>"}
```

or:

```json
{"status": "not_found", "value": null, "evidence": null}
```

For text inputs, `status: found` requires `evidence` to be an exact substring of the text passed to `extract_text`. If it cannot be quoted, the leaf is `not_found` and `value` is null. A number in `value` must appear in that quote. Words such as "eighty percent" are not rewritten as 80. An absent slot is empty. It is not filled from general dental knowledge, and it is not stored as `false`.

For a PDF, the regex parser does not run. A Gemini field is `found` only when the model returned a non-empty evidence string. Missing or blank evidence is forced to `not_found` with `value` null.

If the document waives the deductible for preventive only, the orthodontic waiver stays `not_found`. If it says orthodontics is for children only, adult eligibility stays `not_found` unless adult eligibility is also printed. If it never mentions waiting periods, the waiting-period collection is `not_found`. It is not a grid of zeros.

When a dollar amount is printed for more than one network, the first amount that the classifier can tie to that slot is kept. Class coinsurance is stored per printed network column instead.

## Field list

Only these slots are emitted. Printed class names are added under plan-pay and waiting periods when the document prints them. They are not a fixed carrier list.

- `cost_share.in_network`
- `cost_share.out_of_network`
- `cost_share.second_network` (printed name of a third network column only; otherwise `not_found`)
- `annual_or_contract_maximum.amount_per_person`
- `annual_or_contract_maximum.period` (`annual`, `calendar year`, or `contract year` only when those words are printed next to the maximum; otherwise `not_found` even if the dollar amount was found)
- `annual_maximum_carryover`
- `deductible.per_person`
- `deductible.family_maximum`
- `deductible.waived_for_diagnostic_and_preventive`
- `deductible.waived_for_orthodontics`
- `plan_pay_percent_by_class.<printed class name>.in_network`
- `plan_pay_percent_by_class.<printed class name>.out_of_network`
- `plan_pay_percent_by_class.<printed class name>.second_network` only when a third network column was printed
- `diagnostic_preventive_excluded_from_annual_maximum`
- `orthodontics.lifetime_maximum`
- `orthodontics.adult_eligibility`
- `orthodontics.child_eligibility`
- `orthodontics.one_course_per_lifetime`
- `waiting_periods.collection`
- `waiting_periods.by_class.<printed class name>`
- `frequencies.<slot>.frequency`
- `frequencies.<slot>.age_limit`
- `frequencies.<slot>.replacement_period`

Frequency slots: exams, cleanings, bitewings, full_mouth_or_panoramic, fluoride, sealants, space_maintainers, fillings, crowns, dentures, bridges, implants, root_canal, periodontal_surgery, scaling_and_root_planing, periodontal_maintenance. Age limit and replacement period are separate leaves and stay `not_found` unless printed on that frequency line. A replacement period is recorded only when the line uses replace/replacement wording.

- `implants` (covered, excluded, or limited only when those words are printed near implant)
- `alternate_benefit_or_least_costly_treatment`
- `cosmetic_exclusion`
- `missing_tooth_clause`
- `tmj`
- `coordination_of_benefits`
- `allowed_amount_basis` (UCR, MAC, maximum allowed cost, fee schedule, and similar printed bases)
- `pretreatment_estimate`
- `dependent_age`
- `plan_type` (only `PPO`, `DHMO`, or `indemnity`, and only in a plan-type phrase). UCR or MAC is the allowed-amount slot, not a plan type. If the document names more than one of PPO, DHMO, and indemnity as the plan, plan type stays `not_found` rather than picking one.

`readable_summary` lists found leaves with their values, then the slot names that are `not_found`. It does not state a value that is absent from `fields`.

## NADP class reference (NOT a default)

This mapping is documentation only. The extractor never applies it and never remaps a printed name onto it.

- Diagnostic and Preventive = Class I
- Basic = Class II
- Major = Class III

A crown is not Major, and a procedure is not moved into a class, unless the document prints that class name on a plan-pay row. Example row names that may appear, only if printed: Diagnostic and Preventive, Basic, Endodontics, Periodontics, Oral Surgery, Major Restorative, Prosthodontics, Prosthetic Repairs, Orthodontics.

## Limitations

- OCR is removed. A PDF is rendered to page images with `pdftoppm`, then each image is sent to Gemini. `CALLS_ENABLED` is `True`. The public page does not make that call.
- If rendering fails, the process exits non-zero with an error JSON and no guessed benefits.
- Text extraction still skips ambiguous rows (percent count does not match the network columns) instead of inferring them.
- This tool does not import or depend on `/workspace/taigadesk/`.

## Hosted page

A browser page is published with GitHub Pages: <https://jonnyjenqtaigadesk.github.io/dental-extractor/>.

Paste a summary or upload a `.txt` or `.pdf` file. Pasted text and a `.txt` upload load this repo's `extract.py` from the same site and run `extract_text` in the browser with Pyodide. They do not use PDF.js.

A PDF is rendered with PDF.js `page.render`, and the page images stay on the page. OCR is removed. Gemini is not called from the browser. A public page cannot hold `GEMINI_API_KEY`. After the images render, the page says: "Pages rendered. Gemini runs in the CLI, not in the browser, so no plan fields were extracted." The PDF JSON is `{"ok": false, "error": "Gemini runs in the CLI, not in the browser", "source": {"kind": "pdf-images", "page_count": N, "provider": "gemini"}}`.

On-page note: OCR removed. Images stay on the page. Gemini runs in the CLI, not in the browser.
