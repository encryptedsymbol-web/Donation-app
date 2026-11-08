"""Pure helper functions (no Streamlit / Supabase) so they can be tested alone."""
import hashlib
import hmac
import io
import os
import re
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

# ---------- validation ----------
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[6-9][0-9]{9}$")


def clean_phone(p: str) -> str:
    p = re.sub(r"[\s\-]", "", p or "")
    if p.startswith("+91"):
        p = p[3:]
    elif p.startswith("0") and len(p) == 11:
        p = p[1:]
    return p


def valid_phone(p: str) -> bool:
    return bool(PHONE_RE.match(clean_phone(p)))


def valid_email(e: str) -> bool:
    return bool(EMAIL_RE.match((e or "").strip()))


def valid_pan(p: str) -> bool:
    return bool(PAN_RE.match((p or "").strip().upper()))


# ---------- passwords ----------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + h.hex()


def check_password(password: str, stored: str) -> bool:
    try:
        salt_hex, h_hex = stored.split("$")
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


# ---------- Indian financial year ----------
def financial_year(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


# ---------- amount in words (Indian system) ----------
_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
         "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
         "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_100(n: int) -> str:
    return _ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))


def _below_1000(n: int) -> str:
    out = []
    if n >= 100:
        out.append(_ONES[n // 100] + " Hundred")
        n %= 100
    if n:
        out.append(_below_100(n))
    return " ".join(out)


def _int_words(n: int) -> str:
    if n == 0:
        return "Zero"
    parts = []
    for div, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        if n >= div:
            parts.append(_below_1000(n // div) if div == 1000 else _int_words(n // div))
            parts[-1] += " " + name
            n %= div
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)


def amount_in_words(amount) -> str:
    paise_total = int(round(float(amount) * 100))
    rupees, paise = divmod(paise_total, 100)
    s = f"Rupees {_int_words(rupees)}"
    if paise:
        s += f" and {_below_100(paise)} Paise"
    return s + " Only"


def inr(amount) -> str:
    """Indian digit grouping, e.g. 1234567.5 -> 12,34,567.50"""
    s = f"{float(amount):.2f}"
    whole, dec = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return f"{whole}.{dec}"


# ---------- receipt PDF ----------
def _fmt_date(d) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return d.strftime("%d-%b-%Y")


def build_receipt_pdf(club: dict, req: dict, don: dict) -> bytes:
    """Generate a receipt on a generated club letterhead. Returns PDF bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"Receipt {don['receipt_no']}")
    navy = colors.HexColor("#1f3a5f")
    s_name = ParagraphStyle("n", fontName="Helvetica-Bold", fontSize=18, alignment=1,
                            textColor=navy, leading=22)
    s_sub = ParagraphStyle("s", fontName="Helvetica", fontSize=9.5, alignment=1, leading=13)
    s_title = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=13, alignment=1,
                             spaceBefore=10, spaceAfter=10)
    s_body = ParagraphStyle("b", fontName="Helvetica", fontSize=10.5, leading=15)
    s_small = ParagraphStyle("sm", fontName="Helvetica-Oblique", fontSize=8.5, leading=12,
                             textColor=colors.HexColor("#444444"))
    s_right = ParagraphStyle("r", parent=s_body, alignment=2)

    E = lambda x: escape(str(x or ""))
    story = [Paragraph(E(club["name"]).upper(), s_name),
             Paragraph(E(club["address"]), s_sub)]
    reg = f"80G Reg. No.: <b>{E(club['reg_no_80g'])}</b>"
    if club.get("approval_validity"):
        reg += f" &nbsp;|&nbsp; Valid: {E(club['approval_validity'])}"
    if club.get("club_pan"):
        reg += f" &nbsp;|&nbsp; PAN: {E(club['club_pan'])}"
    story.append(Paragraph(reg, s_sub))

    rule = Table([[""]], colWidths=[174 * mm], rowHeights=[2])
    rule.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1.5, navy)]))
    story += [Spacer(1, 4), rule, Paragraph("DONATION RECEIPT", s_title)]

    meta = Table([[Paragraph(f"Receipt No.: <b>{E(don['receipt_no'])}</b>", s_body),
                   Paragraph(f"Date: <b>{_fmt_date(don['receipt_date'])}</b>", s_right)]],
                 colWidths=[100 * mm, 74 * mm])
    story += [meta, Spacer(1, 8)]

    story.append(Paragraph(
        f"Received with thanks from <b>{E(don['donor_name'])}</b>"
        + (f", {E(don['donor_address'])}" if don.get("donor_address") else "")
        + (f" (PAN: {E(don['donor_pan'])})" if don.get("donor_pan") else "")
        + f" a sum of <b>Rs. {inr(don['amount'])}</b> ({E(amount_in_words(don['amount']))}) "
          f"towards <b>{E(req['purpose'])}</b>.", s_body))
    story.append(Spacer(1, 10))

    rows = [["Mode of payment", "UPI / QR"],
            ["Transaction ID", don["txn_id"]],
            ["Purpose", req["purpose"]],
            ["Event date", _fmt_date(req["event_date"])]]
    t = Table([[Paragraph(f"<b>{E(a)}</b>", s_body), Paragraph(E(b), s_body)] for a, b in rows],
              colWidths=[45 * mm, 129 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
                           ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f7")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story += [t, Spacer(1, 14)]

    story.append(Paragraph(
        f"Donation is eligible for deduction under Section 80G of the Income-tax Act, 1961, "
        f"subject to the applicable conditions and limits, under 80G registration no. "
        f"{E(club['reg_no_80g'])}.", s_small))
    story.append(Spacer(1, 40))
    story.append(Paragraph(f"For <b>{E(club['name'])}</b>", s_right))
    story.append(Spacer(1, 28))
    sig = club.get("signatory") or "Authorised Signatory"
    story.append(Paragraph(f"{E(sig)}<br/>Authorised Signatory", s_right))
    story.append(Spacer(1, 18))
    story.append(Paragraph("This is a computer-generated receipt.", s_small))
    doc.build(story)
    return buf.getvalue()
