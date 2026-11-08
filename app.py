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
                   financial_year, hash_password, inr, make_code, send_email,
                   temp_password, valid_email, valid_pan, valid_phone)

APP_NAME = "Donatehub"
st.set_page_config(page_title=APP_NAME, page_icon="🤝", layout="centered")

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


def email_cfg():
    try:
        s = st.secrets
        if all(k in s for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")):
            return {"host": s["SMTP_HOST"], "port": int(s.get("SMTP_PORT", 465)),
                    "user": s["SMTP_USER"], "password": s["SMTP_PASSWORD"],
                    "from": s.get("SMTP_FROM", s["SMTP_USER"])}
    except Exception:
        pass
    return None


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


def forgot_password_ui(role):
    table = "clubs" if role == "club" else "donors"
    cfg = email_cfg()
    if not cfg:
        st.info("Password reset by email isn't set up yet. Please contact the Donatehub "
                "administrator to reset your password.")
        return
    k = f"fp_{role}"
    st.caption("Enter your registered email. We'll send a 6-digit code that works for 15 minutes.")
    email = st.text_input("Registered email", key=k + "_e").strip().lower()
    if st.button("Send code", key=k + "_s"):
        if not valid_email(email):
            st.error("Enter a valid email.")
        else:
            prev = (db.table("password_resets").select("created_at")
                    .eq("role", role).eq("email", email).execute().data)
            if prev and now_utc() - parse_ts(prev[0]["created_at"]) < timedelta(seconds=60):
                st.warning("Please wait a minute before requesting another code.")
            else:
                if db.table(table).select("id").eq("email", email).execute().data:
                    code = make_code()
                    db.table("password_resets").delete().eq("role", role).eq("email", email).execute()
                    db.table("password_resets").insert({
                        "role": role, "email": email, "code_hash": hash_password(code),
                        "expires_at": (now_utc() + timedelta(minutes=15)).isoformat()}).execute()
                    try:
                        send_email(cfg, email, f"{APP_NAME} password reset code",
                                   f"Your {APP_NAME} password reset code is {code}.\n"
                                   f"It is valid for 15 minutes. If you didn't ask for this, ignore this email.")
                    except Exception:
                        db.table("password_resets").delete().eq("role", role).eq("email", email).execute()
                        st.error("Couldn't send the email. Try again later or contact the administrator.")
                        return
                st.success("If this email is registered, a code has been sent.")
    st.markdown("**Have a code?**")
    code = st.text_input("6-digit code", key=k + "_c", max_chars=6)
    new = st.text_input("New password (min 8 characters)", type="password", key=k + "_p")
    if st.button("Reset password", key=k + "_r"):
        row = (db.table("password_resets").select("*").eq("role", role).eq("email", email)
               .execute().data)
        row = row[0] if row else None
        if (not row or parse_ts(row["expires_at"]) < now_utc() or row["attempts"] >= 5):
            st.error("Invalid or expired code. Request a new one.")
        elif not check_password(code.strip(), row["code_hash"]):
            db.table("password_resets").update({"attempts": row["attempts"] + 1}).eq("id", row["id"]).execute()
            st.error("Invalid or expired code.")
        elif len(new) < 8:
            st.error("Password must be at least 8 characters.")
        else:
            db.table(table).update({"password_hash": hash_password(new)}).eq("email", email).execute()
            db.table("password_resets").delete().eq("id", row["id"]).execute()
            st.success("Password changed. You can log in now.")


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
    st.title(f"🤝 {APP_NAME}")
    st.caption("Support clubs and causes. Pay the club directly by UPI, then submit your payment details "
               "so the club can confirm and issue your 80G receipt.")

    today = date.today().isoformat()
    rows = (db.table("donation_requests")
            .select("*, clubs(name,address,reg_no_80g,approval_validity,status,contact_phone,email)")
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
            st.write(f"**Purpose:** {r['purpose']}")
            st.write(f"**Minimum donation:** Rs. {inr(r['min_amount'])}  \n"
                     f"**Event date:** {date.fromisoformat(r['event_date']).strftime('%d-%b-%Y')}")
            st.caption(f"📍 {c['address']}  |  80G Reg. No.: {c['reg_no_80g']}")
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
    t1, t2, t3 = st.tabs(["Login", "Register", "Forgot password"])
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
            else:
                st.session_state["donor_id"] = d["id"]
                st.rerun()
    with t2:
        donor_register_form()
    with t3:
        forgot_password_ui("donor")


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
                   "The club will verify the payment and issue your 80G receipt.")
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
        st.caption("For the 80G receipt (optional)")
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
    st.title(f"About {APP_NAME}")
    st.markdown(f"""
**{APP_NAME}** connects donors with clubs and associations that are raising funds for a cause, and makes the
paperwork simple: every confirmed donation gets a numbered 80G receipt on the club's own letterhead.

#### How it works for donors
1. Register once and wait for approval from the {APP_NAME} administrator.
2. Browse open requests, see the purpose, event date, minimum donation and the club's contact details.
3. Tap **Get QR code & donate**, scan the club's UPI QR code and pay.
4. Enter your payment details and tick the payment confirmation.
5. The club verifies the payment against its bank statement and issues your receipt.

#### How it works for clubs
1. Register your club with 80G details and contact information. The administrator approves your account.
2. Publish a donation request with the purpose, minimum amount, event date, your UPI QR code and event photos.
3. After the administrator accepts the listing it goes live for donors.
4. Verify each payment, confirm it and download the 80G receipt. A donation register (CSV) is available.

#### Please note
- Payments go directly from the donor to the club's UPI account. {APP_NAME} does not receive or hold funds.
- Donors are responsible for making the payment and for entering correct details.
- Clubs are responsible for verifying payments and for the accuracy of their 80G details and receipts.
- Eligibility for a deduction under Section 80G depends on the club's approval and on the donor's tax
  regime and limits. Donors should consult their tax advisor.
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
        reg = st.text_input("80G registration no. *")
        val = st.text_input("80G approval validity (optional)", placeholder="AY 2022-23 to 2026-27")
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
    if len(reg.strip()) < 4: errs.append("Enter 80G registration no.")
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
            "contact_phone": clean_phone(phone), "email": email, "reg_no_80g": reg.strip(),
            "approval_validity": val.strip() or None, "club_pan": cpan or None,
            "signatory": sign.strip(), "username": email,
            "password_hash": None if g_email else hash_password(pw)}).execute()
        st.success("Registered! You can log in once the administrator approves your club.")
    except Exception as e:
        st.error("That email or short code is already registered." if is_dup_error(e)
                 else "Could not register. Please try again.")


def club_auth():
    st.title("Club Portal")
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
    t_login, t_reg, t_fp = st.tabs(["Login", "Register your club", "Forgot password"])
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
            else:
                st.session_state["club_id"] = r["id"]
                st.rerun()
    with t_reg:
        club_register_form()
    with t_fp:
        forgot_password_ui("club")


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
    top[0].title(club["name"])
    if top[1].button("Logout"):
        do_logout("club_id")
    if not club.get("email") or not club.get("contact_phone"):
        st.warning("Add your contact mobile and email in the Profile tab. They are shown to donors "
                   "and used for password reset.")

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
                    st.caption("✔ Donor has declared the payment and its accuracy.")
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
        done = (db.table("donations").select("*, donation_requests(purpose,event_date)")
                .eq("club_id", club["id"]).eq("status", "confirmed")
                .order("receipt_date", desc=True).order("receipt_no", desc=True)
                .limit(300).execute().data)
        if not done:
            st.info("No confirmed donations yet.")
        else:
            df = pd.DataFrame([{
                "Receipt No": d["receipt_no"], "Date": d["receipt_date"], "Donor": d["donor_name"],
                "PAN": d.get("donor_pan") or "", "Phone": d["donor_phone"], "Email": d["donor_email"],
                "Amount": d["amount"], "Txn ID": d["txn_id"],
                "Purpose": d["donation_requests"]["purpose"]} for d in done])
            st.metric("Total confirmed (shown)", f"Rs. {inr(df['Amount'].astype(float).sum())}")
            st.download_button("⬇ Download register (CSV)", df.to_csv(index=False).encode(),
                               "donation_register.csv", "text/csv")
            for d in done:
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
                state = "Removed by administrator"
            elif r["approval"] == "pending":
                state = "Awaiting administrator approval"
            else:
                state = "Expired" if expired else ("Live" if r["is_active"] else "Closed")
            with st.container(border=True):
                st.write(f"**{r['purpose']}**")
                st.caption(f"Min Rs. {inr(r['min_amount'])}  |  Event {r['event_date']}  |  {state}")
                paths = r.get("photo_paths") or []
                if paths:
                    st.markdown(photo_strip(paths, 70), unsafe_allow_html=True)
                if r["approval"] != "deleted" and len(paths) < MAX_PHOTOS:
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
                if r["approval"] == "approved" and not expired:
                    lab = "Close request" if r["is_active"] else "Reopen request"
                    if st.button(lab, key=f"tg_{r['id']}"):
                        db.table("donation_requests").update({"is_active": not r["is_active"]}) \
                            .eq("id", r["id"]).execute()
                        st.rerun()

    with t_prof:
        st.write(f"**80G Reg. No.:** {club['reg_no_80g']}  |  **Receipt code:** {club['short_code']}")
        with st.form("prof"):
            addr = st.text_area("Address", value=club["address"], height=80)
            phone = st.text_input("Contact mobile (shown to donors)", value=club.get("contact_phone") or "",
                                  max_chars=15)
            email = st.text_input("Email (shown to donors, used for login)", value=club.get("email") or "")
            val = st.text_input("80G approval validity", value=club.get("approval_validity") or "")
            cpan = st.text_input("Club PAN", value=club.get("club_pan") or "", max_chars=10)
            sign = st.text_input("Authorised signatory", value=club.get("signatory") or "")
            go = st.form_submit_button("Save", type="primary")
        if go:
            email = email.strip().lower()
            if cpan.strip() and not valid_pan(cpan.strip().upper()):
                st.error("Club PAN format is invalid.")
            elif not valid_phone(phone):
                st.error("Enter a valid 10-digit contact mobile.")
            elif not valid_email(email):
                st.error("Enter a valid email.")
            elif len(addr.strip()) < 8 or len(sign.strip()) < 3:
                st.error("Address and signatory are required.")
            else:
                try:
                    db.table("clubs").update({"address": addr.strip(), "contact_phone": clean_phone(phone),
                                              "email": email, "approval_validity": val.strip() or None,
                                              "club_pan": cpan.strip().upper() or None,
                                              "signatory": sign.strip()}).eq("id", club["id"]).execute()
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
    db.table(table).update({"password_hash": hash_password(tmp)}).eq("id", rec_id).execute()
    st.success(f"Temporary password for {label} (shown once, share it securely):")
    st.code(tmp)


def page_super():
    if not st.session_state.get("super_ok"):
        st.title("Super Admin")
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
    top[0].title("Super Admin")
    if top[1].button("Logout"):
        st.session_state.pop("super_ok", None)
        st.rerun()

    t_clubs, t_list, t_donors = st.tabs(["Clubs", "Listings", "Donors"])

    with t_clubs:
        clubs = db.table("clubs").select("*").order("created_at", desc=True).execute().data
        flt = st.radio("Show", ["pending", "approved", "rejected", "suspended"],
                       horizontal=True, key="sa_club_f")
        sub = [c for c in clubs if c["status"] == flt]
        if not sub:
            st.info("Nothing here.")
        for c in sub:
            with st.container(border=True):
                st.write(f"**{c['name']}** ({c['short_code']})")
                st.caption(f"Login ID: {c.get('email') or c['username']}  |  "
                           f"Phone: {c.get('contact_phone') or '-'}  |  "
                           f"{'Google sign-in' if not c['password_hash'] else 'Password set'}")
                st.caption(f"{c['address']}  |  80G: {c['reg_no_80g']}  |  "
                           f"Validity: {c.get('approval_validity') or '-'}  |  PAN: {c.get('club_pan') or '-'}")
                cols = st.columns(4)
                for col, (lab, new) in zip(cols, [("Approve", "approved"), ("Reject", "rejected"),
                                                  ("Suspend", "suspended")]):
                    if c["status"] != new and col.button(lab, key=f"c_{new}_{c['id']}"):
                        sa_set_status("clubs", c["id"], new)
                if cols[3].button("Reset password", key=f"c_pw_{c['id']}"):
                    sa_reset_password("clubs", c["id"], c["name"])

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
                st.caption(f"Min Rs. {inr(r['min_amount'])}  |  Event {r['event_date']}")
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
        flt = st.radio("Show", ["pending", "approved", "rejected", "suspended"],
                       horizontal=True, key="sa_donor_f")
        sub = [d for d in donors if d["status"] == flt]
        if not sub:
            st.info("Nothing here.")
        for d in sub:
            with st.container(border=True):
                st.write(f"**{d['name']}**")
                st.caption(f"Login ID: {d['email']}  |  Phone: {d['phone']}  |  "
                           f"{'Google sign-in' if not d['password_hash'] else 'Password set'}")
                cols = st.columns(4)
                for col, (lab, new) in zip(cols, [("Approve", "approved"), ("Reject", "rejected"),
                                                  ("Suspend", "suspended")]):
                    if d["status"] != new and col.button(lab, key=f"d_{new}_{d['id']}"):
                        sa_set_status("donors", d["id"], new)
                if cols[3].button("Reset password", key=f"d_pw_{d['id']}"):
                    sa_reset_password("donors", d["id"], d["name"])


# ------------------------------------------------------------------ ROUTER
st.sidebar.title(f"🤝 {APP_NAME}")
page = st.sidebar.radio("Menu", ["Donate", "About", "Club Portal", "Super Admin"])
if page != "Donate":
    st.session_state.pop("selected_req", None)
    st.session_state.pop("donation_done", None)
{"Donate": page_donate, "About": page_about, "Club Portal": page_club,
 "Super Admin": page_super}[page]()

st.divider()
st.markdown("<div style='text-align:center;font-size:13px;opacity:0.7'>"
            "Developed by CA Soumyadeep Pramanick</div>", unsafe_allow_html=True)
