# Dental benefit summary extractor

CLI only. A PDF is rendered to page images and those images are sent to Gemini. The public page renders the same kind of page images and does not extract benefits.

The key is the environment variable `GEMINI_API_KEY`, read at call time. It is not stored in this repo and it is not on the public page. No pip installs. Python 3 standard library plus `pdftoppm` from poppler.

Model id: `gemini-3.8-flash`. `CALLS_ENABLED` is `True`.

## How to run

```bash
python3 /workspace/dental-extractor/extract.py /path/to/summary.pdf
python3 /workspace/dental-extractor/extract.py --text /path/to/summary.txt
python3 /workspace/dental-extractor/extract.py < /path/to/summary.txt
python3 /workspace/dental-extractor/test_extract.py
```

A PDF is not read as text. Each page is rendered first:

```bash
pdftoppm -png -r 200 <file.pdf> <prefix>
```

Those PNGs are the only PDF input to Gemini. `--text` and stdin do not call Gemini. They return one record, `usable` false, every leaf `not_found`, and `failure_reason` `only a PDF page-image read can fill fields`. Exit status is non-zero only for a real failure (missing file, render failure, API failure), not for a successful read that is not usable.

On a Gemini failure, a render failure, or zero pages, `ok` is false and the payload is still one record: `usable` false, every leaf `not_found`, `failure_reason` set to the error with the key redacted.

## Record

One record per named plan. In-network and out-of-network are fields, not separate plans. Unlabeled money columns are not a network split.

```text
usable                          bool
failure_reason                  string or null
fields.carrier
fields.plan_name
fields.plan_type
fields.annual_maximum.in_network / out_of_network / unlabeled
fields.deductible_individual.in_network / out_of_network / unlabeled
fields.deductible_family.in_network / out_of_network / unlabeled
fields.deductible_unlabeled.in_network / out_of_network / unlabeled
fields.preventive / basic / major / endodontics / periodontics / oral_surgery
    each .in_network / .out_of_network / .unlabeled
fields.preventive_deductible_waived
fields.ortho_lifetime_max
fields.ortho_coinsurance_or_copay
fields.ortho_age_limit
fields.waiting_period.basic / major / ortho
fields.out_of_network_basis
```

There is no preventive waiting field. Implants, missing tooth, frequencies, rollover, takeover, rates, contributions, participation, network size, procedure copay lists, carryover, and class-number remaps are omitted.

Every leaf is `found`, `not_found`, or `conflict`.

- `found`: `value` (printed text, plus `normalized` number or percent only when unambiguous), `page`, and a verbatim `excerpt` the model returned from a page image in that run. A missing excerpt or page forces `not_found`.
- `not_found`: `value`, `page`, and `excerpt` are null. Silence is `not_found`, not "not covered".
- `conflict`: `sides`, and each side has `value`, `page`, and `excerpt`. Neither side is dropped.

`usable` is true only when carrier is `found` (not `conflict`), at least one annual-maximum slot is `found`, none of those three slots is `conflict`, and preventive, basic, and major each have at least one `found` slot and no `conflict` slot.

The coercer, not a pixel guess, applies the benefit rules: class assignment only when the excerpt states the percent or copay or names a class whose percent is in that same excerpt; one unlabeled number stays unlabeled; an unlabeled 100/80/50 triple is preventive, basic, major on the unlabeled slots and a fourth number is ignored; two unlabeled triples conflict; member-pay percents are stored as plan-pay (`100` minus the member percent) with the member-pay wording left in the excerpt; a bare deductible is `deductible_unlabeled`; a family annual maximum does not fill the per-person annual maximum; lifetime non-ortho dollars fill neither; a class dollar is a copay; percent plus copay conflicts; waiting `none` or `waived` is `none`, not `0`; `100%` preventive does not set the waived flag; `child` with no age is not an age limit; `no age limit` stays a phrase.

## Hosted page

<https://jonnyjenqtaigadesk.github.io/dental-extractor/>

The page renders a PDF with PDF.js `page.render` onto a canvas and states that nothing is extracted in the browser. It does not call Gemini and it has no benefit JSON.

Repo: <https://github.com/jonnyjenqtaigadesk/dental-extractor>
