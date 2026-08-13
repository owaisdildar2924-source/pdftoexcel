# PDF to Excel — offline statement converter

Converts PDF bank statements and settlement reports to Excel, offline, on
Windows, with every figure proven by the statement's own arithmetic or
clearly flagged when it cannot be.

## For the end user

1. Unzip the `PDF-to-Excel-Windows.zip` folder anywhere.
2. Double-click **PDF to Excel.exe**.
3. The first time, choose a working folder. The app creates `input`,
   `output`, `processed` and `failed` inside it and remembers the choice.
4. Drop PDF statements into `input`, press **Convert PDFs now**.
5. Spreadsheets appear in `output`. Each source PDF moves to `processed`
   (converted) or `failed` (not converted, with the reason shown in the
   app and in `output/_logs`).

Every file ends in exactly one of three states, and the app says which:

- **PROVEN** — converted, and the figures reconcile exactly against the
  statement's own printed balances and totals.
- **NOT PROVEN** — converted, but the arithmetic does not check out
  everywhere; the affected rows are highlighted in the spreadsheet with
  the specific reason, in figures.
- **FAILED** — not converted; the reason is stated in plain language and
  the source PDF is untouched in `failed`.

The single most valuable property of this tool: **you can trust the
figures it does not flag.** It never invents or "corrects" a number, never
phones home (no network code exists in the app, enforced by a test), and
never overwrites or deletes a source document.

## What it reads

One geometric engine handles every issuer; small per-issuer profiles add
vocabulary only. Verified against real statements from: Chase (business
checking, personal checking, credit card), Capital One, Chime (checking,
Credit Builder), Wells Fargo (business checking, credit card), Navy
Federal (checking/savings, credit card), Truist, Delta Community, PNC,
plus Colonial Van Lines driver settlements and Jordan Carriers deduction
reports. Unknown issuers are read generically and proven where the
document prints balances.

Scanned PDFs go through bundled OCR (Tesseract). Native-text PDFs are the
accuracy tier: on the verification set, every native statement reconciled
to the cent. Scans are extracted best-effort and anything unproven is
flagged — a wrong number that is flagged is a manageable problem; a wrong
number that looks confident is not.

## Building the Windows app

Builds happen on GitHub Actions (`.github/workflows/build-windows.yml`):
push to `main`, then download the `PDF-to-Excel-Windows` artifact. The
build job packages with PyInstaller (onedir, no console), bundles
Tesseract, runs the packaged exe end-to-end on a deterministic fixture,
verifies the output figure by figure, and deliberately breaks a fixture
to prove the reconciliation safety net actually fires.

To build locally on a Windows machine:

    pip install -r requirements.txt pyinstaller
    pyinstaller pdf2excel.spec --noconfirm
    # result: dist/PDF to Excel/

## Development

    pip install -r requirements.txt pytest
    python -m pytest tests/          # includes the no-network-imports test
    python app.py                    # run the GUI from source
    python app.py --cli <workdir>    # headless run (used by CI)

Licence note: dependencies are MIT/BSD/Apache only. Do not add PyMuPDF
(AGPL) — this software is redistributed.
