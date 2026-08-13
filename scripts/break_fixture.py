"""Rebuild the fixture with one withdrawal row REMOVED but the printed
totals left untouched. The tool must notice the missing 150.25 and refuse
to call the file proven."""
import sys

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

OUT = sys.argv[1]

LINES = [
    "JPMorgan Chase Bank, N.A.",
    "Chase Business Complete Checking",
    "January 01, 2022 through January 31, 2022",
    "Account Number: 000000105263997",
    "CHECKING SUMMARY",
    "Beginning Balance $1,000.00",
    "Deposits and Additions 2 700.50",
    "Electronic Withdrawals 2 -200.25",
    "Ending Balance 4 $1,500.25",
    "DEPOSITS AND ADDITIONS",
    "DATE DESCRIPTION AMOUNT",
    "01/07 Customer Payment Received Invoice 1001 $500.25",
    "01/14 Customer Payment Received Invoice 1002 200.25",
    "Total Deposits and Additions $700.50",
    "ELECTRONIC WITHDRAWALS",
    # the 150.25 rent row has been deliberately removed
    "01/21 Vendor ACH Payment Software Subscription 50.00",
    "Total Electronic Withdrawals $200.25",
]

c = canvas.Canvas(OUT, pagesize=LETTER)
c.setFont("Courier", 10)
y = 750
for ln in LINES:
    c.drawString(60, y, ln)
    y -= 16
c.showPage()
c.save()
print(f"wrote broken fixture {OUT}")
