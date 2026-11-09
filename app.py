import hmac
import html
import io
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from PIL import Image, ImageOps
from supabase import create_client

from utils import (amount_in_words, build_receipt_pdf, check_password, clean_phone,
                   financial_year, hash_password, inr, temp_password, valid_email, valid_pan, valid_phone)

APP_NAME = "Donatehub"
st.set_page_config(page_title=APP_NAME, page_icon="🤝", layout="centered")
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap');
.stApp { font-family: 'Poppins', sans-serif; }
h1, h2, h3, h4 { font-family: 'Poppins', sans-serif !important; letter-spacing: -0.2px; }
.hero { background: linear-gradient(120deg,#4f46e5 0%,#7c3aed 45%,#ec4899 100%);
  padding: 22px 20px; border-radius: 20px; margin: 0 0 14px 0;
  box-shadow: 0 10px 24px rgba(124,58,237,.28); }
.hero-t { color:#fff !important; font-size: 26px; font-weight: 700; line-height: 1.2; }
.hero-s { color:#fff !important; opacity:.92; font-size: 14px; margin-top: 4px; }
.chip { display:inline-block; padding:3px 11px; border-radius:999px; font-size:12px; font-weight:600; margin:2px 6px 2px 0; }
.c-amt { background:#ede9fe; color:#5b21b6; } .c-date { background:#ffedd5; color:#9a3412; }
.c-reg { background:#cffafe; color:#155e75; } .c-ok { background:#dcfce7; color:#166534; }
.c-wait { background:#fef3c7; color:#92400e; } .c-bad { background:#fee2e2; color:#991b1b; }
.c-grey { background:#e5e7eb; color:#374151; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius:16px !important;
  border:1px solid rgba(124,58,237,.22) !important; border-left:5px solid #8b5cf6 !important;
  background: rgba(124,58,237,.04); box-shadow:0 4px 14px rgba(15,23,42,.06); }
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
  border-radius:999px; font-weight:600; border:1.5px solid rgba(124,58,237,.35); transition:all .15s ease; }
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
  transform:translateY(-1px); border-color:#7c3aed; box-shadow:0 6px 14px rgba(124,58,237,.25); }
button[kind="primary"], button[kind="primaryFormSubmit"],
button[data-testid="stBaseButton-primary"], button[data-testid="stBaseButton-primaryFormSubmit"] {
  background: linear-gradient(120deg,#6366f1,#8b5cf6 50%,#ec4899) !important; border:none !important; }
button[kind="primary"] *, button[kind="primaryFormSubmit"] *,
button[data-testid="stBaseButton-primary"] *, button[data-testid="stBaseButton-primaryFormSubmit"] * { color:#fff !important; }
.stTabs [data-baseweb="tab-list"] { gap:6px; }
.stTabs [data-baseweb="tab"] { border-radius:12px 12px 0 0; padding:8px 14px; font-weight:600; }
.stTabs [aria-selected="true"] { color:#7c3aed !important; }
.stTabs [data-baseweb="tab-highlight"] { background: linear-gradient(90deg,#6366f1,#ec4899) !important; height:3px; }
.stTextInput input, .stTextArea textarea, .stNumberInput input, .stDateInput input { border-radius:12px !important; }
[data-testid="stMetric"] { background: linear-gradient(120deg,#ecfeff,#f0fdf4); border-radius:14px; padding:10px 14px; }
[data-testid="stMetric"] * { color:#065f46 !important; }
section[data-testid="stSidebar"] { background: linear-gradient(180deg,#312e81 0%,#6d28d9 60%,#db2777 100%); }
section[data-testid="stSidebar"] * { color:#fff !important; }
.foot { text-align:center; font-size:13px; opacity:.85; margin:4px 0 12px; }
.foot span { background:linear-gradient(90deg,#6366f1,#ec4899); -webkit-background-clip:text;
  background-clip:text; color:transparent; font-weight:600; }
</style>
"""

st.markdown(CSS, unsafe_allow_html=True)

BUCKET = "qr-codes"
MAX_QR_BYTES = 2 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTOS = 3
DECLARATION = ("I confirm that I have made this payment myself and that the details entered are "
               "correct. I understand that I am responsible for the payment and its accuracy.")


# ------------------------------------------------------------------ DB / helpers
@st.cache_resource
def get_db():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_SERVICE_KEY"])


db = get_db()


def public_url(path: str) -> str:
    return db.storage.from_(BUCKET).get_public_url(path)


def is_dup_error(e: Exception) -> bool:
    s = str(e).lower()
    return "23505" in s or "duplicate" in s


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def chip(text, kind="amt") -> str:
    return f"<span class='chip c-{kind}'>{esc(text)}</span>"


def banner_html(title, sub="") -> str:
    return (f"<div class='hero'><div class='hero-t'>{esc(title)}</div>"
            f"<div class='hero-s'>{esc(sub)}</div></div>")


def banner(title, sub=""):
    st.markdown(banner_html(title, sub), unsafe_allow_html=True)


def reg_text(c) -> str:
    return (c.get("reg_no") or "").strip() or "Not available"


def event_label(req) -> str:
    return f"{req['purpose'][:60]} ({req['event_date']})"


def fetch_all(make_query, page=1000):
    out, start = [], 0
    while True:
        rows = make_query().range(start, start + page - 1).execute().data
        out += rows
        if len(rows) < page:
            return out
        start += page


def donation_rows(rows, club_name=None):
    out = []
    for d in rows:
        req = d.get("donation_requests") or {}
        out.append({
            "Club": club_name or (d.get("clubs") or {}).get("name", ""),
            "Event": req.get("purpose", ""), "Event date": req.get("event_date", ""),
            "Receipt No": d.get("receipt_no") or "", "Receipt date": d.get("receipt_date") or "",
            "Donor": d["donor_name"], "PAN": d.get("donor_pan") or "", "Phone": d["donor_phone"],
            "Email": d["donor_email"], "Amount": float(d["amount"]), "Txn ID": d["txn_id"],
            "Status": d["status"], "Submitted on": str(d.get("created_at", ""))[:10]})
    return out


def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(s: str):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def photo_strip(paths, h=90) -> str:
    if not paths:
        return ""
    imgs = "".join(f'<img src="{html.escape(public_url(p))}" '
                   f'style="height:{h}px;border-radius:8px;margin-right:6px">' for p in paths)
    return f'<div style="display:flex;overflow-x:auto;margin:6px 0">{imgs}</div>'


def check_qr(f):
    """Validate QR upload. Returns ((data, ext, mime), error)."""
    data = f.getvalue()
    if len(data) > MAX_QR_BYTES:
        return None, f"{f.name}: QR image is larger than 2 MB."
    try:
        img = Image.open(io.BytesIO(data)); img.verify(); fmt = img.format
    except Exception:
        fmt = None
    if fmt == "PNG":
        return (data, "png", "image/png"), None
    if fmt == "JPEG":
        return (data, "jpg", "image/jpeg"), None
    return None, f"{f.name}: not a valid PNG/JPEG image."


def prep_photo(f):
    """Validate, auto-rotate and shrink an event photo. Returns ((bytes, ext, mime), error)."""
    raw = f.getvalue()
    if len(raw) > MAX_PHOTO_BYTES:
        return None, f"{f.name}: photo is larger than 10 MB."
    try:
        img = Image.open(io.BytesIO(raw)); img.load()
        if img.format not in ("PNG", "JPEG"):
            raise ValueError
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((1280, 1280))
        out = io.BytesIO(); img.save(out, "JPEG", quality=82, optimize=True)
        return (out.getvalue(), "jpg", "image/jpeg"), None
    except Exception:
        return None, f"{f.name}: not a valid PNG/JPEG image."


def store_file(club_id, item, folder=""):
    data, ext, mime = item
    path = f"{club_id}/{folder}{uuid.uuid4().hex}.{ext}"
    db.storage.from_(BUCKET).upload(path, data, {"content-type": mime})
    return path


# ------------------------------------------------------------------ Google + email
def google_enabled() -> bool:
    try:
        return "auth" in st.secrets
    except Exception:
        return False


def google_email():
    try:
        u = st.user
        if getattr(u, "is_logged_in", False) and u.get("email") and u.get("email_verified", True):
            return str(u.get("email")).strip().lower()
    except Exception:
        pass
    return None


def google_name() -> str:
    try:
        return str(st.user.get("name") or "")
    except Exception:
        return ""


def google_button(key):
    if google_enabled() and not google_email():
        if st.button("Continue with Google", key=key):
            st.login()
        st.caption("or use email and password")


def do_logout(*keys):
    for k in keys:
        st.session_state.pop(k, None)
    if google_email():
        st.logout()
    st.rerun()


def find_account(table, ident):
    ident = (ident or "").strip().lower()
    if not ident:
        return None
    r = db.table(table).select("*").eq("email", ident).execute().data
    if not r and table == "clubs":
        r = db.table("clubs").select("*").eq("username", ident).execute().data
    return r[0] if r else None


def reset_request_ui(role):
    table = "clubs" if role == "club" else "donors"
    k = f"rr_{role}"
    st.caption("Enter your registered email (clubs registered earlier can use their username). "
               "The administrator will review your request and give you a temporary password. "
               "You'll then be asked to set a new password of your own.")
    ident = st.text_input("Registered email or username", key=k + "_i")
    phone = st.text_input("Mobile number to reach you", key=k + "_p", max_chars=15)
    if st.button("Request password reset", key=k + "_b"):
        if not ident.strip() or not valid_phone(phone):
            st.error("Enter your registered email/username and a valid 10-digit mobile number.")
            return
        acct = find_account(table, ident)
        if acct:
            dup = (db.table("password_requests").select("id").eq("role", role)
                   .eq("account_id", acct["id"]).eq("status", "pending").execute().data)
            if not dup:
                db.table("password_requests").insert({
                    "role": role, "account_id": acct["id"], "account_name": acct["name"],
                    "login_id": acct.get("email") or acct.get("username") or "",
                    "contact_phone": clean_phone(phone)}).execute()
        st.success("Request submitted. The administrator will review it and contact you on the "
                   "mobile number you gave.")


def force_change_ui():
    """Shown after login with a temporary password. Returns True while it is on screen."""
    pc = st.session_state.get("pw_change")
    if not pc:
        return False
    st.subheader("Set a new password")
    st.info("You signed in with a temporary password. Choose your own password to continue.")
    with st.form("force_pw"):
        new = st.text_input("New password (min 8 characters)", type="password")
        new2 = st.text_input("Confirm new password", type="password")
        go = st.form_submit_button("Save and continue", type="primary")
    if st.button("Cancel", key="pwc_cancel"):
        st.session_state.pop("pw_change", None)
        st.rerun()
    if go:
        rec = db.table(pc["table"]).select("password_hash").eq("id", pc["id"]).execute().data[0]
        if len(new) < 8:
            st.error("Password must be at least 8 characters.")
        elif new != new2:
            st.error("Passwords do not match.")
        elif check_password(new, rec["password_hash"]):
            st.error("Choose a password different from the temporary one.")
        else:
            db.table(pc["table"]).update({"password_hash": hash_password(new),
                                          "must_change_password": False}).eq("id", pc["id"]).execute()
            st.session_state["club_id" if pc["table"] == "clubs" else "donor_id"] = pc["id"]
            st.session_state.pop("pw_change", None)
            st.rerun()
    return True


def change_password_form(table, rec_id, key):
    with st.form(key):
        st.markdown("**Change password**")
        cur = st.text_input("Current password (leave blank if you signed up with Google)", type="password")
        new = st.text_input("New password (min 8 characters)", type="password")
        go = st.form_submit_button("Update password")
    if go:
        rec = db.table(table).select("password_hash").eq("id", rec_id).execute().data[0]
        if rec["password_hash"] and not check_password(cur, rec["password_hash"]):
            st.error("Current password is wrong.")
        elif len(new) < 8:
            st.error("New password must be at least 8 characters.")
        else:
            db.table(table).update({"password_hash": hash_password(new)}).eq("id", rec_id).execute()
            st.success("Password updated.")


# ------------------------------------------------------------------ PUBLIC: DONATE
def page_donate():
    banner(f"🤝 {APP_NAME}", "Give directly to causes you care about. Pay by UPI and get a receipt.")

    today = date.today().isoformat()
    rows = (db.table("donation_requests")
            .select("*, clubs(name,address,reg_no,status,contact_phone,email)")
            .eq("is_active", True).eq("approval", "approved").gte("event_date", today)
            .order("event_date").execute().data)
    rows = [r for r in rows if r.get("clubs") and r["clubs"]["status"] == "approved"]

    sel = st.session_state.get("selected_req")
    if sel:
        req = next((r for r in rows if r["id"] == sel), None)
        if req is None:
            st.session_state.pop("selected_req", None)
            st.warning("This request is no longer open.")
            st.rerun()
        donate_form(req)
        return

    if not rows:
        st.info("No open donation requests right now. Please check back soon.")
        return

    for r in rows:
        c = r["clubs"]
        with st.container(border=True):
            st.subheader(c["name"])
            if r.get("photo_paths"):
                st.markdown(photo_strip(r["photo_paths"]), unsafe_allow_html=True)
            st.write(f"**{r['purpose']}**")
            st.markdown(chip(f"Min Rs. {inr(r['min_amount'])}", "amt")
                        + chip("Event " + date.fromisoformat(r["event_date"]).strftime("%d-%b-%Y"), "date")
                        + chip("Reg. No.: " + reg_text(c), "reg"), unsafe_allow_html=True)
            st.caption(f"📍 {c['address']}")
            contact = "  |  ".join(x for x in [c.get("contact_phone"), c.get("email")] if x)
            if contact:
                st.caption(f"📞 Contact: {contact}")
            if st.button("Get QR code & donate", key=f"b_{r['id']}", type="primary"):
                st.session_state["selected_req"] = r["id"]
                st.rerun()


# ---------- donor accounts
def donor_register_form(g_email=None):
    with st.form("donor_reg"):
        name = st.text_input("Full name *", value=google_name() if g_email else "")
        phone = st.text_input("Mobile number *", max_chars=15)
        email = st.text_input("Email *", value=g_email or "", disabled=bool(g_email))
        pw = pw2 = ""
        if not g_email:
            pw = st.text_input("Password * (min 8 characters)", type="password")
            pw2 = st.text_input("Confirm password *", type="password")
        go = st.form_submit_button("Register", type="primary")
    if not go:
        return
    email = (g_email or email).strip().lower()
    errs = []
    if len(name.strip()) < 2: errs.append("Enter your name.")
    if not valid_phone(phone): errs.append("Enter a valid 10-digit mobile number.")
    if not valid_email(email): errs.append("Enter a valid email.")
    if not g_email:
        if len(pw) < 8: errs.append("Password must be at least 8 characters.")
        if pw != pw2: errs.append("Passwords do not match.")
    if errs:
        for e in errs: st.error(e)
        return
    try:
        db.table("donors").insert({"name": name.strip(), "phone": clean_phone(phone), "email": email,
                                   "password_hash": None if g_email else hash_password(pw)}).execute()
        st.success("Registered. You can donate once the administrator approves your account.")
    except Exception as e:
        st.error("This email is already registered." if is_dup_error(e) else "Could not register. Try again.")


def donor_auth_ui():
    if (st.session_state.get("pw_change") or {}).get("table") == "donors":
        force_change_ui()
        return
    st.markdown("#### Log in to donate")
    st.caption("Donors need a Donatehub account approved by the administrator.")
    ge = google_email()
    if ge:
        st.info(f"Signed in with Google as {ge}. Complete your details to register.")
        donor_register_form(ge)
        if st.button("Use a different account", key="d_diff"):
            st.logout()
        return
    google_button("d_google")
    t1, t2, t3 = st.tabs(["Login", "Register", "Forgot password?"])
    with t1:
        with st.form("donor_login"):
            u = st.text_input("Email")
            p = st.text_input("Password", type="password")
            ok = st.form_submit_button("Login", type="primary")
        if ok:
            d = find_account("donors", u)
            if not d or not d["password_hash"]:
                st.error("Invalid email or password. If you signed up with Google, use 'Continue with Google'.")
            elif not check_password(p, d["password_hash"]):
                st.error("Invalid email or password.")
            elif d["status"] == "approved" and d.get("must_change_password"):
                st.session_state["pw_change"] = {"table": "donors", "id": d["id"]}
                st.rerun()
            else:
                st.session_state["donor_id"] = d["id"]
                st.rerun()
    with t2:
        donor_register_form()
    with t3:
        reset_request_ui("donor")


def donor_gate():
    did = st.session_state.get("donor_id")
    if not did:
        ge = google_email()
        d = find_account("donors", ge) if ge else None
        if d:
            did = d["id"]
            st.session_state["donor_id"] = did
    if did:
        r = db.table("donors").select("*").eq("id", did).execute().data
        if r:
            d = r[0]
            if d["status"] == "approved":
                return d
            if d["status"] == "pending":
                st.info("Your donor account is awaiting approval by the administrator.")
            else:
                st.error(f"Your donor account is {d['status']}. Please contact the administrator.")
            if st.button("Logout", key="d_lo1"):
                do_logout("donor_id")
            return None
        st.session_state.pop("donor_id", None)
    donor_auth_ui()
    return None


def donate_form(req):
    c = req["clubs"]
    if st.button("← Back to all requests"):
        st.session_state.pop("selected_req", None)
        st.session_state.pop("donation_done", None)
        st.rerun()

    st.subheader(c["name"])
    st.write(f"**Purpose:** {req['purpose']}")
    st.write(f"**Minimum donation:** Rs. {inr(req['min_amount'])}")

    donor = donor_gate()
    if not donor:
        return

    if st.session_state.get("donation_done"):
        st.success("Thank you! Your payment details have been submitted. "
                   "The club will verify the payment and issue your receipt.")
        return

    cols = st.columns([3, 1])
    cols[0].caption(f"Logged in as {donor['name']}")
    if cols[1].button("Logout", key="d_lo2"):
        do_logout("donor_id")

    st.markdown("#### Step 1: Scan and pay")
    st.image(public_url(req["qr_path"]), width=280)
    st.caption("After paying, note the UPI transaction / reference ID.")

    st.markdown("#### Step 2: Submit your payment details")
    with st.form("donor_form"):
        name = st.text_input("Name of donor *", value=donor["name"])
        phone = st.text_input("Phone number *", value=donor["phone"], max_chars=15)
        email = st.text_input("Email *", value=donor["email"])
        amount = st.number_input("Amount paid (Rs.) *", min_value=0.0,
                                 value=float(req["min_amount"]), step=100.0, format="%.2f")
        txn = st.text_input("Transaction ID / UTR *")
        st.caption("For the receipt (optional)")
        pan = st.text_input("Donor PAN", max_chars=10)
        addr = st.text_area("Donor address", height=70)
        decl = st.checkbox(DECLARATION)
        go = st.form_submit_button("Submit", type="primary")

    if not go:
        return

    name, email, txn = name.strip(), email.strip(), txn.strip()
    pan = pan.strip().upper()
    errs = []
    if len(name) < 2: errs.append("Enter donor name.")
    if not valid_phone(phone): errs.append("Enter a valid 10-digit mobile number.")
    if not valid_email(email): errs.append("Enter a valid email.")
    if amount < float(req["min_amount"]):
        errs.append(f"Amount must be at least Rs. {inr(req['min_amount'])}.")
    if len(txn) < 6: errs.append("Enter the transaction ID shown in your payment app.")
    if pan and not valid_pan(pan): errs.append("PAN format is invalid (e.g. ABCDE1234F), or leave it blank.")
    if not decl: errs.append("Please tick the payment confirmation.")
    if errs:
        for e in errs: st.error(e)
        return

    fresh = (db.table("donation_requests").select("is_active,event_date,approval")
             .eq("id", req["id"]).execute().data)
    if (not fresh or not fresh[0]["is_active"] or fresh[0]["approval"] != "approved"
            or fresh[0]["event_date"] < date.today().isoformat()):
        st.error("Sorry, this request has just closed.")
        return
    try:
        db.table("donations").insert({
            "request_id": req["id"], "club_id": req["club_id"], "donor_id": donor["id"],
            "donor_name": name, "donor_phone": clean_phone(phone), "donor_email": email,
            "amount": amount, "txn_id": txn, "donor_pan": pan or None,
            "donor_address": addr.strip() or None, "payer_declaration": True}).execute()
    except Exception as e:
        st.error("This transaction ID has already been submitted for this club."
                 if is_dup_error(e) else "Could not save. Please try again.")
        return
    st.session_state["donation_done"] = True
    st.rerun()


# ------------------------------------------------------------------ ABOUT
def page_about():
    banner(f"About {APP_NAME}", "Simple, transparent giving for clubs and communities")
    st.markdown(f"""
**{APP_NAME}** connects donors with clubs and associations that are raising funds for a cause, and keeps the
paperwork simple: every confirmed donation gets a numbered receipt on the club's own letterhead.

#### How it works for donors
1. Register once and wait for approval from the {APP_NAME} administrator.
2. Browse open requests and see the purpose, event date, minimum donation and the club's contact details.
3. Tap **Get QR code & donate**, scan the club's UPI QR code and pay.
4. Enter your payment details and tick the payment confirmation.
5. The club verifies the payment against its bank statement and issues your receipt.

#### How it works for clubs
1. Register your club with contact information. A registration number is optional. The administrator approves your account.
2. Publish a donation request with the purpose, minimum amount, event date, your UPI QR code and event photos.
3. After the administrator accepts the listing it goes live for donors.
4. Verify each payment, confirm it and download the receipt. Donation registers (CSV) are available event-wise.

#### Please note
- Payments go directly from the donor to the club's UPI account. {APP_NAME} does not receive or hold funds.
- Donors are responsible for making the payment and for entering correct details.
- Clubs are responsible for verifying payments and for the accuracy of their details and receipts.
- Receipts show the club's registration number if it has one, otherwise "Not available".
""")


# ------------------------------------------------------------------ CLUB PORTAL
def page_club():
    cid = st.session_state.get("club_id")
    if not cid:
        club_auth()
        return
    club = db.table("clubs").select("*").eq("id", cid).execute().data
    if not club or club[0]["status"] != "approved":
        st.session_state.pop("club_id", None)
        st.rerun()
    club_dashboard(club[0])


def club_register_form(g_email=None):
    with st.form("club_reg"):
        name = st.text_input("Name of club *")
        code = st.text_input("Short code for receipts * (2-6 letters, e.g. RCA)", max_chars=6)
        addr = st.text_area("Address *", height=80)
        phone = st.text_input("Contact mobile number * (shown to donors)", max_chars=15)
        email = st.text_input("Email * (shown to donors, used for login)",
                              value=g_email or "", disabled=bool(g_email))
        reg = st.text_input("Registration number (optional)",
                            help="Leave blank if the club has none. Receipts will then say 'Not available'.")
        cpan = st.text_input("Club PAN (optional)", max_chars=10)
        sign = st.text_input("Authorised signatory name & designation *",
                             placeholder="R. Sharma, Hon. Secretary")
        pw = pw2 = ""
        if not g_email:
            pw = st.text_input("Password * (min 8 characters)", type="password")
            pw2 = st.text_input("Confirm password *", type="password")
        go = st.form_submit_button("Register", type="primary")
    if not go:
        return
    code, cpan = code.strip().upper(), cpan.strip().upper()
    email = (g_email or email).strip().lower()
    errs = []
    if len(name.strip()) < 3: errs.append("Enter club name.")
    if not re.fullmatch(r"[A-Z]{2,6}", code): errs.append("Short code must be 2-6 letters.")
    if len(addr.strip()) < 8: errs.append("Enter full address.")
    if not valid_phone(phone): errs.append("Enter a valid 10-digit contact mobile number.")
    if not valid_email(email): errs.append("Enter a valid email.")
    if cpan and not valid_pan(cpan): errs.append("Club PAN format is invalid.")
    if len(sign.strip()) < 3: errs.append("Enter signatory name.")
    if not g_email:
        if len(pw) < 8: errs.append("Password must be at least 8 characters.")
        if pw != pw2: errs.append("Passwords do not match.")
    if errs:
        for e in errs: st.error(e)
        return
    try:
        db.table("clubs").insert({
            "name": name.strip(), "short_code": code, "address": addr.strip(),
            "contact_phone": clean_phone(phone), "email": email, "reg_no": reg.strip() or None,
            "club_pan": cpan or None, "signatory": sign.strip(), "username": email,
            "password_hash": None if g_email else hash_password(pw)}).execute()
        st.success("Registered! You can log in once the administrator approves your club.")
    except Exception as e:
        st.error("That email or short code is already registered." if is_dup_error(e)
                 else "Could not register. Please try again.")


def club_auth():
    banner("Club Portal", "Register your club, publish requests and issue receipts")
    if (st.session_state.get("pw_change") or {}).get("table") == "clubs":
        force_change_ui()
        return
    ge = google_email()
    if ge:
        acct = find_account("clubs", ge)
        if acct:
            if acct["status"] == "approved":
                st.session_state["club_id"] = acct["id"]
                st.rerun()
            elif acct["status"] == "pending":
                st.warning("Your registration is awaiting approval.")
            else:
                st.error(f"Your account is {acct['status']}. Please contact the administrator.")
        else:
            st.info(f"Signed in with Google as {ge}. Complete your club details to register.")
            club_register_form(ge)
        if st.button("Use a different account", key="c_diff"):
            st.logout()
        return

    google_button("c_google")
    t_login, t_reg, t_fp = st.tabs(["Login", "Register your club", "Forgot password?"])
    with t_login:
        with st.form("club_login"):
            u = st.text_input("Email or username")
            p = st.text_input("Password", type="password")
            ok = st.form_submit_button("Login", type="primary")
        if ok:
            r = find_account("clubs", u)
            if not r or not r["password_hash"] or not check_password(p, r["password_hash"]):
                st.error("Invalid login. If you signed up with Google, use 'Continue with Google'.")
            elif r["status"] == "pending":
                st.warning("Your registration is awaiting approval.")
            elif r["status"] != "approved":
                st.error(f"Your account is {r['status']}. Please contact the administrator.")
            elif r.get("must_change_password"):
                st.session_state["pw_change"] = {"table": "clubs", "id": r["id"]}
                st.rerun()
            else:
                st.session_state["club_id"] = r["id"]
                st.rerun()
    with t_reg:
        club_register_form()
    with t_fp:
        reset_request_ui("club")


def next_receipt_no(club, rdate: date, attempt: int) -> str:
    fy = financial_year(rdate)
    prefix = f"{club['short_code']}/{fy}/"
    res = (db.table("donations").select("id", count="exact")
           .eq("club_id", club["id"]).like("receipt_no", prefix + "%").execute())
    return f"{prefix}{(res.count or 0) + 1 + attempt:04d}"


def confirm_donation(club, d, amount):
    today = date.today()
    for attempt in range(5):
        rno = next_receipt_no(club, today, attempt)
        try:
            r = (db.table("donations")
                 .update({"status": "confirmed", "amount": amount, "receipt_no": rno,
                          "receipt_date": today.isoformat()})
                 .eq("id", d["id"]).eq("status", "pending").execute())
            return rno if r.data else None
        except Exception as e:
            if not is_dup_error(e):
                raise
    return None


def club_dashboard(club):
    top = st.columns([4, 1])
    top[0].markdown(banner_html(club["name"], "Club dashboard"), unsafe_allow_html=True)
    if top[1].button("Logout"):
        do_logout("club_id")
    if not club.get("email") or not club.get("contact_phone"):
        st.warning("Add your contact mobile and email in the Profile tab. They are shown to donors "
                   "and used for login.")

    t_pend, t_done, t_req, t_prof = st.tabs(["Pending", "Confirmed & receipts", "My requests", "Profile"])

    with t_pend:
        pend = (db.table("donations").select("*, donation_requests(purpose)")
                .eq("club_id", club["id"]).eq("status", "pending")
                .order("created_at").execute().data)
        if not pend:
            st.info("No donations waiting for confirmation.")
        st.caption("Check each transaction ID against your bank/UPI statement before confirming.")
        for d in pend:
            with st.container(border=True):
                st.write(f"**{d['donor_name']}**  |  {d['donor_phone']}  |  {d['donor_email']}")
                st.write(f"Purpose: {d['donation_requests']['purpose']}")
                st.write(f"Txn ID: `{d['txn_id']}`  |  Claimed: Rs. {inr(d['amount'])}")
                if d.get("payer_declaration"):
                    st.markdown(chip("Donor has declared the payment", "ok"), unsafe_allow_html=True)
                if d.get("donor_pan") or d.get("donor_address"):
                    st.caption(f"PAN: {d.get('donor_pan') or '-'}  |  Address: {d.get('donor_address') or '-'}")
                amt = st.number_input("Amount actually received (Rs.)", min_value=1.0,
                                      value=float(d["amount"]), step=100.0, format="%.2f",
                                      key=f"amt_{d['id']}")
                c1, c2 = st.columns(2)
                if c1.button("✅ Confirm & generate receipt", key=f"ok_{d['id']}", type="primary"):
                    rno = confirm_donation(club, d, amt)
                    if rno:
                        st.success(f"Confirmed. Receipt {rno} is in 'Confirmed & receipts'.")
                        st.rerun()
                    else:
                        st.error("Could not confirm (already processed?). Refresh and retry.")
                if c2.button("❌ Reject", key=f"no_{d['id']}"):
                    db.table("donations").update({"status": "rejected"}) \
                        .eq("id", d["id"]).eq("status", "pending").execute()
                    st.rerun()

    with t_done:
        done = fetch_all(lambda: db.table("donations").select("*, donation_requests(purpose,event_date)")
                         .eq("club_id", club["id"]).eq("status", "confirmed")
                         .order("receipt_date", desc=True).order("receipt_no", desc=True))
        if not done:
            st.info("No confirmed donations yet.")
        else:
            labels = sorted({event_label(d["donation_requests"]) for d in done})
            pick = st.selectbox("Event", ["All events"] + labels, key="club_evt")
            sel = [d for d in done if pick == "All events" or event_label(d["donation_requests"]) == pick]
            df = pd.DataFrame(donation_rows(sel, club["name"]))
            st.metric(f"Total confirmed ({len(sel)} donations)", f"Rs. {inr(df['Amount'].sum())}")
            st.download_button("⬇ Download donations (CSV)", df.to_csv(index=False).encode(),
                               "donations_" + ("all" if pick == "All events" else "event") + ".csv",
                               "text/csv", key="club_csv")
            st.markdown("**Event-wise summary**")
            summ = (df.groupby(["Event", "Event date"], as_index=False)
                    .agg(Donations=("Amount", "count"), Total=("Amount", "sum")))
            st.dataframe(summ, hide_index=True)
            st.download_button("⬇ Download event-wise summary (CSV)", summ.to_csv(index=False).encode(),
                               "event_wise_summary.csv", "text/csv", key="club_csv2")
            st.markdown("**Receipts**")
            st.caption("Showing the latest 50. Use the CSV for the full register.")
            for d in sel[:50]:
                with st.container(border=True):
                    st.write(f"**{d['receipt_no']}**  |  {d['donor_name']}  |  Rs. {inr(d['amount'])}")
                    st.caption(f"{d['receipt_date']}  |  {d['donation_requests']['purpose']}  |  {d['txn_id']}")
                    pdf = build_receipt_pdf(club, d["donation_requests"], d)
                    st.download_button("⬇ Receipt PDF", pdf,
                                       f"Receipt_{d['receipt_no'].replace('/', '_')}.pdf",
                                       "application/pdf", key=f"dl_{d['id']}")

    with t_req:
        st.markdown("#### New donation request")
        with st.form("new_req", clear_on_submit=True):
            purpose = st.text_area("Purpose *", height=80)
            minamt = st.number_input("Minimum donation (Rs.) *", min_value=1.0, value=500.0,
                                     step=100.0, format="%.2f")
            ev = st.date_input("Date of event *", min_value=date.today())
            qr = st.file_uploader("UPI QR code (PNG/JPG, max 2 MB) *", type=["png", "jpg", "jpeg"])
            pics = st.file_uploader(f"Event pictures (optional, up to {MAX_PHOTOS})",
                                    type=["png", "jpg", "jpeg"], accept_multiple_files=True)
            go = st.form_submit_button("Submit for approval", type="primary")
        if go:
            if len(purpose.strip()) < 5:
                st.error("Enter the purpose.")
            elif qr is None:
                st.error("Upload the QR code image.")
            elif len(pics) > MAX_PHOTOS:
                st.error(f"Upload at most {MAX_PHOTOS} pictures.")
            else:
                qr_item, err = check_qr(qr)
                items, errs = [], [err] if err else []
                for p in pics:
                    it, e = prep_photo(p)
                    items.append(it) if it else errs.append(e)
                if errs:
                    for e in errs: st.error(e)
                else:
                    qpath = store_file(club["id"], qr_item)
                    ppaths = [store_file(club["id"], it, "photos/") for it in items]
                    db.table("donation_requests").insert({
                        "club_id": club["id"], "purpose": purpose.strip(), "min_amount": minamt,
                        "event_date": ev.isoformat(), "qr_path": qpath, "photo_paths": ppaths}).execute()
                    st.success("Submitted. It goes live once the administrator accepts it.")
        st.markdown("#### Your requests")
        mine = (db.table("donation_requests").select("*").eq("club_id", club["id"])
                .order("created_at", desc=True).execute().data)
        for r in mine:
            expired = r["event_date"] < date.today().isoformat()
            if r["approval"] == "deleted":
                state = chip("Removed by administrator", "bad")
            elif r["approval"] == "pending":
                state = chip("Awaiting approval", "wait")
            else:
                state = (chip("Expired", "grey") if expired else
                         chip("Live", "ok") if r["is_active"] else chip("Closed", "grey"))
            with st.container(border=True):
                st.write(f"**{r['purpose']}**")
                st.markdown(chip(f"Min Rs. {inr(r['min_amount'])}", "amt") + chip(f"Event {r['event_date']}", "date")
                            + state, unsafe_allow_html=True)
                paths = r.get("photo_paths") or []
                if r["approval"] == "deleted":
                    continue
                if paths:
                    pcols = st.columns(len(paths))
                    for i, (pc, p) in enumerate(zip(pcols, paths)):
                        pc.image(public_url(p), width=100)
                        if pc.button("🗑 Remove", key=f"rm_{r['id']}_{i}"):
                            db.table("donation_requests").update(
                                {"photo_paths": [x for x in paths if x != p]}).eq("id", r["id"]).execute()
                            try:
                                db.storage.from_(BUCKET).remove([p])
                            except Exception:
                                pass
                            st.rerun()
                if len(paths) < MAX_PHOTOS:
                    with st.form(f"ph_{r['id']}", clear_on_submit=True):
                        more = st.file_uploader(f"Add event pictures ({MAX_PHOTOS - len(paths)} more allowed)",
                                                type=["png", "jpg", "jpeg"], accept_multiple_files=True)
                        addp = st.form_submit_button("Upload pictures")
                    if addp:
                        if not more:
                            st.error("Choose at least one picture.")
                        elif len(more) > MAX_PHOTOS - len(paths):
                            st.error(f"You can add only {MAX_PHOTOS - len(paths)} more.")
                        else:
                            items, errs = [], []
                            for p in more:
                                it, e = prep_photo(p)
                                items.append(it) if it else errs.append(e)
                            if errs:
                                for e in errs: st.error(e)
                            else:
                                newp = [store_file(club["id"], it, "photos/") for it in items]
                                db.table("donation_requests").update({"photo_paths": paths + newp}) \
                                    .eq("id", r["id"]).execute()
                                st.rerun()
                with st.expander("✏️ Edit event details"):
                    with st.form(f"ed_{r['id']}"):
                        e_purpose = st.text_area("Purpose", value=r["purpose"], height=80)
                        e_min = st.number_input("Minimum donation (Rs.)", min_value=1.0,
                                                value=float(r["min_amount"]), step=100.0, format="%.2f")
                        e_date = st.date_input("Date of event", value=date.fromisoformat(r["event_date"]))
                        e_qr = st.file_uploader("Replace QR code (optional)", type=["png", "jpg", "jpeg"])
                        st.caption("A new QR code needs administrator re-approval before it goes live again.")
                        save = st.form_submit_button("Save changes", type="primary")
                    if save:
                        if len(e_purpose.strip()) < 5:
                            st.error("Enter the purpose.")
                        elif e_date.isoformat() != r["event_date"] and e_date < date.today():
                            st.error("Event date cannot be in the past.")
                        else:
                            upd = {"purpose": e_purpose.strip(), "min_amount": e_min,
                                   "event_date": e_date.isoformat()}
                            err = None
                            if e_qr is not None:
                                item, err = check_qr(e_qr)
                                if item:
                                    upd["qr_path"] = store_file(club["id"], item)
                                    if r["approval"] == "approved":
                                        upd["approval"] = "pending"
                            if err:
                                st.error(err)
                            else:
                                db.table("donation_requests").update(upd).eq("id", r["id"]).execute()
                                if "qr_path" in upd:
                                    try:
                                        db.storage.from_(BUCKET).remove([r["qr_path"]])
                                    except Exception:
                                        pass
                                st.rerun()
                if r["approval"] == "approved" and not expired:
                    lab = "Close request" if r["is_active"] else "Reopen request"
                    if st.button(lab, key=f"tg_{r['id']}"):
                        db.table("donation_requests").update({"is_active": not r["is_active"]}) \
                            .eq("id", r["id"]).execute()
                        st.rerun()

    with t_prof:
        st.markdown(chip("Reg. No.: " + reg_text(club), "reg") + chip(f"Receipt code: {club['short_code']}", "amt")
                    + (chip(f"PAN: {club['club_pan']}", "grey") if club.get("club_pan") else ""),
                    unsafe_allow_html=True)
        st.caption("Name, registration number and PAN can be changed by the administrator only.")
        with st.form("prof"):
            addr = st.text_area("Address", value=club["address"], height=80)
            phone = st.text_input("Contact mobile (shown to donors)", value=club.get("contact_phone") or "",
                                  max_chars=15)
            email = st.text_input("Email (shown to donors, used for login)", value=club.get("email") or "")
            sign = st.text_input("Authorised signatory", value=club.get("signatory") or "")
            go = st.form_submit_button("Save", type="primary")
        if go:
            email = email.strip().lower()
            if not valid_phone(phone):
                st.error("Enter a valid 10-digit contact mobile.")
            elif not valid_email(email):
                st.error("Enter a valid email.")
            elif len(addr.strip()) < 8 or len(sign.strip()) < 3:
                st.error("Address and signatory are required.")
            else:
                try:
                    db.table("clubs").update({"address": addr.strip(), "contact_phone": clean_phone(phone),
                                              "email": email, "signatory": sign.strip()}) \
                        .eq("id", club["id"]).execute()
                    st.success("Saved.")
                except Exception as e:
                    st.error("That email is already used by another club." if is_dup_error(e)
                             else "Could not save.")
        change_password_form("clubs", club["id"], "chg_club_pw")


# ------------------------------------------------------------------ SUPER ADMIN
def sa_set_status(table, rec_id, status):
    db.table(table).update({"status": status}).eq("id", rec_id).execute()
    st.rerun()


def sa_reset_password(table, rec_id, label):
    tmp = temp_password()
    db.table(table).update({"password_hash": hash_password(tmp), "must_change_password": True}) \
        .eq("id", rec_id).execute()
    st.success(f"Temporary password for {label} (shown once, share it securely). "
               f"They must set their own password at first login:")
    st.code(tmp)


def sa_status_buttons(table, rec, status_list, key):
    acts = [(("Restore" if rec["status"] == "removed" and new == "approved" else lab), new)
            for lab, new in status_list if rec["status"] != new]
    cols = st.columns(len(acts))
    for col, (lab, new) in zip(cols, acts):
        if col.button(lab, key=f"{key}_{new}_{rec['id']}"):
            sa_set_status(table, rec["id"], new)


def page_super():
    if not st.session_state.get("super_ok"):
        banner("Super Admin", "Sign in to manage Donatehub")
        with st.form("sa"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            ok = st.form_submit_button("Login", type="primary")
        if ok:
            good = (hmac.compare_digest(u, st.secrets["SUPER_ADMIN_USER"])
                    and hmac.compare_digest(p, st.secrets["SUPER_ADMIN_PASSWORD"]))
            if good:
                st.session_state["super_ok"] = True
                st.rerun()
            st.error("Invalid credentials.")
        return

    top = st.columns([4, 1])
    top[0].markdown(banner_html("Super Admin", "Control panel"), unsafe_allow_html=True)
    if top[1].button("Logout"):
        st.session_state.pop("super_ok", None)
        st.rerun()

    n_req = len(db.table("password_requests").select("id").eq("status", "pending").execute().data)
    t_clubs, t_list, t_donors, t_reset, t_rep = st.tabs(
        ["Clubs", "Listings", "Donors", f"Reset requests ({n_req})" if n_req else "Reset requests", "Reports"])
    STATUSES = [("Approve", "approved"), ("Reject", "rejected"), ("Suspend", "suspended"), ("Remove", "removed")]

    with t_clubs:
        clubs = db.table("clubs").select("*").order("created_at", desc=True).execute().data
        flt = st.radio("Show", ["pending", "approved", "rejected", "suspended", "removed"],
                       horizontal=True, key="sa_club_f")
        sub = [c for c in clubs if c["status"] == flt]
        if not sub:
            st.info("Nothing here.")
        for c in sub:
            with st.container(border=True):
                st.write(f"**{c['name']}** ({c['short_code']})")
                st.markdown(chip("Reg. No.: " + reg_text(c), "reg")
                            + chip("PAN: " + (c.get("club_pan") or "-"), "grey")
                            + chip("Google sign-in" if not c["password_hash"] else "Password set", "amt"),
                            unsafe_allow_html=True)
                st.caption(f"Login ID: {c.get('email') or c['username']}  |  "
                           f"Phone: {c.get('contact_phone') or '-'}  |  {c['address']}")
                sa_status_buttons("clubs", c, STATUSES, "c")
                if st.button("Reset password", key=f"c_pw_{c['id']}"):
                    sa_reset_password("clubs", c["id"], c["name"])
                with st.expander("✏️ Edit club details"):
                    with st.form(f"ec_{c['id']}"):
                        n_name = st.text_input("Name of club", value=c["name"])
                        n_reg = st.text_input("Registration number", value=c.get("reg_no") or "")
                        n_pan = st.text_input("Club PAN", value=c.get("club_pan") or "", max_chars=10)
                        n_addr = st.text_area("Address", value=c["address"], height=70)
                        n_sign = st.text_input("Authorised signatory", value=c.get("signatory") or "")
                        n_phone = st.text_input("Contact mobile", value=c.get("contact_phone") or "", max_chars=15)
                        save = st.form_submit_button("Save", type="primary")
                    if save:
                        pan = n_pan.strip().upper()
                        if len(n_name.strip()) < 3 or len(n_addr.strip()) < 8 or len(n_sign.strip()) < 3:
                            st.error("Name, address and signatory are required.")
                        elif pan and not valid_pan(pan):
                            st.error("PAN format is invalid.")
                        elif n_phone.strip() and not valid_phone(n_phone):
                            st.error("Enter a valid 10-digit mobile or leave blank.")
                        else:
                            db.table("clubs").update({
                                "name": n_name.strip(), "reg_no": n_reg.strip() or None, "club_pan": pan or None,
                                "address": n_addr.strip(), "signatory": n_sign.strip(),
                                "contact_phone": clean_phone(n_phone) if n_phone.strip() else None}) \
                                .eq("id", c["id"]).execute()
                            st.rerun()

    with t_list:
        flt = st.radio("Show", ["Pending approval", "Live", "Removed"], horizontal=True, key="sa_list_f")
        key = {"Pending approval": "pending", "Live": "approved", "Removed": "deleted"}[flt]
        reqs = (db.table("donation_requests").select("*, clubs(name)").eq("approval", key)
                .order("created_at", desc=True).execute().data)
        if not reqs:
            st.info("Nothing here.")
        for r in reqs:
            with st.container(border=True):
                st.write(f"**{r['clubs']['name']}**: {r['purpose']}")
                st.markdown(chip(f"Min Rs. {inr(r['min_amount'])}", "amt") + chip(f"Event {r['event_date']}", "date"),
                            unsafe_allow_html=True)
                st.image(public_url(r["qr_path"]), width=110, caption="QR code")
                if r.get("photo_paths"):
                    st.markdown(photo_strip(r["photo_paths"], 70), unsafe_allow_html=True)
                c1, c2 = st.columns(2)
                if key == "pending" and c1.button("Accept", key=f"l_ok_{r['id']}", type="primary"):
                    db.table("donation_requests").update({"approval": "approved"}).eq("id", r["id"]).execute()
                    st.rerun()
                if key == "deleted" and c1.button("Restore", key=f"l_re_{r['id']}"):
                    db.table("donation_requests").update({"approval": "approved"}).eq("id", r["id"]).execute()
                    st.rerun()
                if key != "deleted" and c2.button("Delete", key=f"l_del_{r['id']}"):
                    db.table("donation_requests").update({"approval": "deleted"}).eq("id", r["id"]).execute()
                    st.rerun()
        st.caption("Deleted listings are hidden but their donation records are kept for accounting.")

    with t_donors:
        donors = db.table("donors").select("*").order("created_at", desc=True).execute().data
        flt = st.radio("Show", ["pending", "approved", "rejected", "suspended", "removed"],
                       horizontal=True, key="sa_donor_f")
        sub = [d for d in donors if d["status"] == flt]
        if not sub:
            st.info("Nothing here.")
        for d in sub:
            with st.container(border=True):
                st.write(f"**{d['name']}**")
                st.caption(f"Login ID: {d['email']}  |  Phone: {d['phone']}  |  "
                           f"{'Google sign-in' if not d['password_hash'] else 'Password set'}")
                sa_status_buttons("donors", d, STATUSES, "d")
                if st.button("Reset password", key=f"d_pw_{d['id']}"):
                    sa_reset_password("donors", d["id"], d["name"])

    with t_reset:
        flt = st.radio("Show", ["Pending", "Handled"], horizontal=True, key="sa_rr_f")
        q = db.table("password_requests").select("*").order("created_at", desc=True)
        reqs = (q.eq("status", "pending").execute().data if flt == "Pending"
                else [x for x in q.execute().data if x["status"] != "pending"])
        if not reqs:
            st.info("Nothing here.")
        for x in reqs:
            with st.container(border=True):
                st.write(f"**{x['account_name']}** ({'Club' if x['role'] == 'club' else 'Donor'})")
                st.caption(f"Login ID: {x['login_id']}  |  Contact mobile given: {x['contact_phone']}  |  "
                           f"Requested: {str(x['created_at'])[:10]}")
                if x["status"] != "pending":
                    st.markdown(chip(x["status"].title(), "ok" if x["status"] == "approved" else "bad"),
                                unsafe_allow_html=True)
                    continue
                c1, c2 = st.columns(2)
                if c1.button("Approve & issue password", key=f"rr_ok_{x['id']}", type="primary"):
                    tbl = "clubs" if x["role"] == "club" else "donors"
                    tmp = temp_password()
                    db.table(tbl).update({"password_hash": hash_password(tmp), "must_change_password": True}) \
                        .eq("id", x["account_id"]).execute()
                    db.table("password_requests").update({"status": "approved", "handled_at": now_utc().isoformat()}) \
                        .eq("id", x["id"]).execute()
                    st.success(f"Temporary password for {x['account_name']} (shown once). "
                               f"Share it on {x['contact_phone']}. They must set a new password at first login:")
                    st.code(tmp)
                if c2.button("Reject", key=f"rr_no_{x['id']}"):
                    db.table("password_requests").update({"status": "rejected", "handled_at": now_utc().isoformat()}) \
                        .eq("id", x["id"]).execute()
                    st.rerun()

    with t_rep:
        rows = fetch_all(lambda: db.table("donations")
                         .select("*, clubs(name), donation_requests(purpose,event_date)")
                         .order("created_at", desc=True))
        df = pd.DataFrame(donation_rows(rows)) if rows else pd.DataFrame()
        if df.empty:
            st.info("No donations yet.")
        else:
            conf = df[df["Status"] == "confirmed"]
            st.metric(f"Total confirmed ({len(conf)} donations)", f"Rs. {inr(conf['Amount'].sum())}")
            st.markdown("**Club-wise (confirmed donations)**")
            cw = conf.groupby("Club", as_index=False).agg(Donations=("Amount", "count"), Total=("Amount", "sum"))
            st.dataframe(cw, hide_index=True)
            st.download_button("⬇ Club-wise summary (CSV)", cw.to_csv(index=False).encode(),
                               "club_wise_summary.csv", "text/csv", key="rep_cw")
            st.markdown("**Event-wise (confirmed donations)**")
            ew = (conf.groupby(["Club", "Event", "Event date"], as_index=False)
                  .agg(Donations=("Amount", "count"), Total=("Amount", "sum")))
            st.dataframe(ew, hide_index=True)
            st.download_button("⬇ Event-wise summary (CSV)", ew.to_csv(index=False).encode(),
                               "event_wise_summary.csv", "text/csv", key="rep_ew")
            st.markdown("**Detailed donations**")
            inc = st.radio("Include", ["Confirmed only", "All (incl. pending/rejected)"],
                           horizontal=True, key="rep_inc")
            det = conf if inc == "Confirmed only" else df
            club_pick = st.selectbox("Club", ["All clubs"] + sorted(det["Club"].unique()), key="rep_club")
            if club_pick != "All clubs":
                det = det[det["Club"] == club_pick]
            ev_labels = sorted((det["Event"] + " (" + det["Event date"].astype(str) + ")").unique())
            ev_pick = st.selectbox("Event", ["All events"] + ev_labels, key="rep_ev")
            if ev_pick != "All events":
                det = det[(det["Event"] + " (" + det["Event date"].astype(str) + ")") == ev_pick]
            st.caption(f"{len(det)} rows" + ("  (showing first 200)" if len(det) > 200 else ""))
            st.dataframe(det.head(200), hide_index=True)
            st.download_button("⬇ Download detailed donations (CSV)", det.to_csv(index=False).encode(),
                               "donations_detailed.csv", "text/csv", key="rep_det")


# ------------------------------------------------------------------ ROUTER


st.sidebar.title(f"🤝 {APP_NAME}")
page = st.sidebar.radio("Menu", ["Donate", "About", "Club Portal", "Super Admin"])
if page != "Donate":
    st.session_state.pop("selected_req", None)
    st.session_state.pop("donation_done", None)
{"Donate": page_donate, "About": page_about, "Club Portal": page_club,
 "Super Admin": page_super}[page]()

st.divider()
st.markdown("<div class='foot'>Developed by <span>CA Soumyadeep Pramanick</span></div>",
            unsafe_allow_html=True)
