"""Dev harness: run the engine over the sample PDFs and print a summary."""
import sys, glob, traceback
sys.path.insert(0, ".")
from bankconv.pdftext import extract_native
from bankconv.parse import parse_statement
from bankconv.reconcile import reconcile_block, check_section_totals

files = sys.argv[1:] or sorted(glob.glob("/sessions/adoring-modest-turing/mnt/uploads/*.pdf"))
VERBOSE = "-v" in files
files = [f for f in files if f != "-v"]

for f in files:
    name = f.split("/")[-1]
    try:
        pages, text, is_native, page_texts = extract_native(f)
        if not is_native:
            print(f"\n### {name}: SCANNED (OCR path, later)")
            continue
        profile, conf, blocks, sp = parse_statement(pages, text)
        for b in blocks:
            reconcile_block(b)
        tot_notes = check_section_totals(blocks, sp.section_totals)
        ntxn = sum(len(b.txns) for b in blocks)
        nflag = sum(1 for b in blocks for t in b.txns if t.status != "OK")
        print(f"\n### {name}")
        print(f"  profile={profile.key} conf={conf} period={sp.period} "
              f"txns={ntxn} flagged={nflag}")
        for b in blocks:
            print(f"  [block] '{b.name[:40]}' begin={b.beginning} end={b.ending} "
                  f"n={len(b.txns)} recon={b.recon_passed}")
            print(f"     {b.recon_detail[:180]}")
        for note in tot_notes:
            print(f"  [total-mismatch] {note}")
        if VERBOSE:
            for b in blocks:
                for t in b.txns[:60]:
                    print(f"    {t.date} {str(t.amount):>12} bal={t.balance} "
                          f"[{t.sign_source}] {t.status[:1]} {t.description[:60]}")
    except Exception:
        print(f"\n### {name}: EXCEPTION")
        traceback.print_exc()
