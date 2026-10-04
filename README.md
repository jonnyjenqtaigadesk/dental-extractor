# Dental benefit summary extractor

Local CLI for other agents. It reads a pasted text summary or a text-based PDF and returns one JSON object. Every benefit value is either quoted from the document or explicitly `not_found`. It does not invent numbers, percents, ages, waiting periods, class mappings, or yes/no answers.

No network. No API keys. No pip installs. Python 3 standard library plus the `pdftotext` binary from poppler.

## How to run

```bash
python3 /workspace/dental-extractor/extract.py /path/to/summary.pdf
python3 /workspace/dental-extractor/extract.py /path/to/summary.txt
python3 /workspace/dental-extractor/extract.py --text /path/to/summary.txt
python3 /workspace/dental-extractor/extract.py < /path/to/summary.txt
python3 /workspace/dental-extractor/extract.py -
python3 /workspace/dental-extractor/test_extract.py
```

Stdout is one JSON object (`ok`, `source`, `fields`, `notes`, `readable_summary`). `notes` is a list of short verbatim evidence snippets. There is no HTML page; the CLI is the interface.

PDF input is read only by running:

```bash
pdftotext -layout -enc UTF-8 <file.pdf> -
```

If `pdftotext` fails, the process exits non-zero and prints `{"ok": false, "error": "..."}`. It does not guess the plan.

## No-invent rule

Every leaf is:

```json
{"status": "found", "value": "<printed string or number>", "evidence": "<exact substring of the source>"}
```

or:

```json
{"status": "not_found", "value": null, "evidence": null}
```

`status: found` requires `evidence` to be an exact substring of the text after PDF extraction. If it cannot be quoted, the leaf is `not_found` and `value` is null. A number in `value` must appear in that quote. Words such as "eighty percent" are not rewritten as 80. An absent slot is empty. It is not filled from general dental knowledge, and it is not stored as `false`.

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

- Works on text-based PDFs. Scanned image PDFs are not OCR'd. If `pdftotext` returns no text, benefits stay `not_found` and a warning is included. Failure of `pdftotext` is an error, not a guess.
- Multi-column summaries depend on `pdftotext -layout`. Wrapped lines can split an age or a frequency away from its procedure; those leaves stay `not_found` instead of being inferred.
- Ambiguous rows (percent count does not match the network columns) are skipped.
- This tool does not import or depend on `/workspace/taigadesk/`.

## Hosted page

A browser page is published with GitHub Pages: <https://jonnyjenqtaigadesk.github.io/dental-extractor/>.

Paste a summary or upload a `.txt` file. The page loads this repo's `extract.py` from the same site and runs the unchanged `extract_text` function in the browser with Pyodide. It does not rewrite the parser. A PDF upload is read in the browser with PDF.js, not with `pdftotext`, so the words can differ from the CLI even though the parser file is the same. A `.txt` upload and pasted text do not go through PDF.js. There is no backend and no API key.
