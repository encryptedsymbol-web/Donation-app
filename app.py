import hmac
import io
import re
import uuid
from datetime import date

import pandas as pd
import streamlit as st
from PIL import Image
from supabase import create_client

from utils import (amount_in_words, build_receipt_pdf, check_password, clean_phone,
                   financial_year, hash_password, inr, valid_email, valid_pan, valid_phone)

st.set_page_config(page_title="Club Donations", page_icon="🤝", layout="centered")

BUCKET = "qr-codes"
MAX_QR_BYTES = 2 * 1024 * 1024


# ------------------------------------------------------------------ DB
@st.cache_resource
def get_db():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_SERVICE_KEY"])


db = get_db()


def qr_url(path: str) -> str:
    return db.storage.from_(BUCKET).get_public_url(path)


def is_dup_error(e: Exception) -> bool:
    s = str(e).lower()
    return "23505" in s or "duplicate" in s


# ------------------------------------------------------------------ PUBLIC: DONATE
def page_donate():
    st.title("🤝 Support a Club")
    st.caption("Choose a cause, pay using the club's QR code, then submit your payment details "
               "so the club can confirm and issue your 80G receipt.")

    today = date.today().isoformat()
    rows = (db.table("donation_requests")
            .select("*, clubs(name,address,reg_no_80g,approval_validity,status)")
            .eq("is_active", True).gte("event_date", today)
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
            st.write(f"**Purpose:** {r['purpose']}")
            st.write(f"**Minimum donation:** Rs. {inr(r['min_amount'])}  \n"
                     f"**Event date:** {date.fromisoformat(r['event_date']).strftime('%d-%b-%Y')}")
            st.caption(f"📍 {c['address']}  |  80G Reg. No.: {c['reg_no_80g']}")
            if st.button("Get QR code & donate", key=f"b_{r['id']}", type="primary"):
                st.session_state["selected_req"] = r["id"]
                st.rerun()


def donate_form(req):
    c = req["clubs"]
    if st.button("← Back to all requests"):
        st.session_state.pop("selected_req", None)
        st.session_state.pop("donation_done", None)
        st.rerun()

    st.subheader(c["name"])
    st.write(f"**Purpose:** {req['purpose']}")
    st.write(f"**Minimum donation:** Rs. {inr(req['min_amount'])}")

    if st.session_state.get("donation_done"):
        st.success("Thank you! Your payment details have been submitted. "
                   "The club will verify the payment and issue your 80G receipt.")
        return

    st.markdown("#### Step 1: Scan and pay")
    st.image(qr_url(req["qr_path"]), width=280)
    st.caption("After paying, note the UPI transaction / reference ID.")

    st.markdown("#### Step 2: Submit your payment details")
    with st.form("donor_form"):
        name = st.text_input("Name of donor *")
        phone = st.text_input("Phone number *", max_chars=15)
        email = st.text_input("Email *")
        amount = st.number_input("Amount paid (Rs.) *", min_value=0.0,
                                 value=float(req["min_amount"]), step=100.0, format="%.2f")
        txn = st.text_input("Transaction ID / UTR *")
        st.caption("For the 80G receipt (optional)")
        pan = st.text_input("Donor PAN", max_chars=10)
        addr = st.text_area("Donor address", height=70)
        go = st.form_submit_button("Submit", type="primary")

    if not go:
        return

    name, email, txn = name.strip(), email.strip(), txn.strip()
    pan = pan.strip().upper()
    errs = []
    if len(name) < 2:
        errs.append("Enter donor name.")
    if not valid_phone(phone):
        errs.append("Enter a valid 10-digit mobile number.")
    if not valid_email(email):
        errs.append("Enter a valid email.")
    if amount < float(req["min_amount"]):
        errs.append(f"Amount must be at least Rs. {inr(req['min_amount'])}.")
    if len(txn) < 6:
        errs.append("Enter the transaction ID shown in your payment app.")
    if pan and not valid_pan(pan):
        errs.append("PAN format is invalid (e.g. ABCDE1234F), or leave it blank.")
    if errs:
        for e in errs:
            st.error(e)
        return

    # re-check that request is still open
    fresh = (db.table("donation_requests").select("is_active,event_date")
             .eq("id", req["id"]).execute().data)
    if not fresh or not fresh[0]["is_active"] or fresh[0]["event_date"] < date.today().isoformat():
        st.error("Sorry, this request has just closed.")
        return
    try:
        db.table("donations").insert({
            "request_id": req["id"], "club_id": req["club_id"], "donor_name": name,
            "donor_phone": clean_phone(phone), "donor_email": email, "amount": amount,
            "txn_id": txn, "donor_pan": pan or None, "donor_address": addr.strip() or None,
        }).execute()
    except Exception as e:
        if is_dup_error(e):
            st.error("This transaction ID has already been submitted for this club.")
        else:
            st.error("Could not save. Please try again.")
        return
    st.session_state["donation_done"] = True
    st.rerun()


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


def club_auth():
    st.title("Club Portal")
    t_login, t_reg = st.tabs(["Login", "Register your club"])
    with t_login:
        with st.form("club_login"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            ok = st.form_submit_button("Login", type="primary")
        if ok:
            r = db.table("clubs").select("*").eq("username", u.strip().lower()).execute().data
            if not r or not check_password(p, r[0]["password_hash"]):
                st.error("Invalid username or password.")
            elif r[0]["status"] == "pending":
                st.warning("Your registration is awaiting approval.")
            elif r[0]["status"] != "approved":
                st.error(f"Your account is {r[0]['status']}. Please contact the administrator.")
            else:
                st.session_state["club_id"] = r[0]["id"]
                st.rerun()
    with t_reg:
        with st.form("club_reg"):
            name = st.text_input("Name of club *")
            code = st.text_input("Short code for receipts * (2-6 letters, e.g. RCA)", max_chars=6)
            addr = st.text_area("Address *", height=80)
            reg = st.text_input("80G registration no. *")
            val = st.text_input("80G approval validity (optional)", placeholder="AY 2022-23 to 2026-27")
            cpan = st.text_input("Club PAN (optional)", max_chars=10)
            sign = st.text_input("Authorised signatory name & designation *",
                                 placeholder="R. Sharma, Hon. Secretary")
            user = st.text_input("Choose a username *")
            pw = st.text_input("Password * (min 8 characters)", type="password")
            pw2 = st.text_input("Confirm password *", type="password")
            go = st.form_submit_button("Register", type="primary")
        if go:
            code, cpan, user = code.strip().upper(), cpan.strip().upper(), user.strip().lower()
            errs = []
            if len(name.strip()) < 3: errs.append("Enter club name.")
            if not re.fullmatch(r"[A-Z]{2,6}", code): errs.append("Short code must be 2-6 letters.")
            if len(addr.strip()) < 8: errs.append("Enter full address.")
            if len(reg.strip()) < 4: errs.append("Enter 80G registration no.")
            if cpan and not valid_pan(cpan): errs.append("Club PAN format is invalid.")
            if len(sign.strip()) < 3: errs.append("Enter signatory name.")
            if not re.fullmatch(r"[a-z0-9_.]{4,30}", user):
                errs.append("Username: 4-30 chars, letters/numbers/._ only.")
            if len(pw) < 8: errs.append("Password must be at least 8 characters.")
            if pw != pw2: errs.append("Passwords do not match.")
            if errs:
                for e in errs: st.error(e)
            else:
                try:
                    db.table("clubs").insert({
                        "name": name.strip(), "short_code": code, "address": addr.strip(),
                        "reg_no_80g": reg.strip(), "approval_validity": val.strip() or None,
                        "club_pan": cpan or None, "signatory": sign.strip(),
                        "username": user, "password_hash": hash_password(pw)}).execute()
                    st.success("Registered! You can log in once the administrator approves your club.")
                except Exception as e:
                    if is_dup_error(e):
                        st.error("That username or short code is already taken.")
                    else:
                        st.error("Could not register. Please try again.")


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
        st.session_state.pop("club_id", None)
        st.rerun()

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
            go = st.form_submit_button("Publish request", type="primary")
        if go:
            if len(purpose.strip()) < 5:
                st.error("Enter the purpose.")
            elif qr is None:
                st.error("Upload the QR code image.")
            else:
                data = qr.getvalue()
                try:
                    img = Image.open(io.BytesIO(data)); img.verify()
                    fmt = img.format
                except Exception:
                    fmt = None
                if fmt not in ("PNG", "JPEG"):
                    st.error("File is not a valid PNG/JPEG image.")
                elif len(data) > MAX_QR_BYTES:
                    st.error("Image is larger than 2 MB.")
                else:
                    ext, mime = ("png", "image/png") if fmt == "PNG" else ("jpg", "image/jpeg")
                    path = f"{club['id']}/{uuid.uuid4().hex}.{ext}"
                    db.storage.from_(BUCKET).upload(path, data, {"content-type": mime})
                    db.table("donation_requests").insert({
                        "club_id": club["id"], "purpose": purpose.strip(), "min_amount": minamt,
                        "event_date": ev.isoformat(), "qr_path": path}).execute()
                    st.success("Request published.")
        st.markdown("#### Your requests")
        mine = (db.table("donation_requests").select("*").eq("club_id", club["id"])
                .order("created_at", desc=True).execute().data)
        for r in mine:
            expired = r["event_date"] < date.today().isoformat()
            state = "Expired" if expired else ("Open" if r["is_active"] else "Closed")
            with st.container(border=True):
                st.write(f"**{r['purpose']}**")
                st.caption(f"Min Rs. {inr(r['min_amount'])}  |  Event {r['event_date']}  |  {state}")
                if not expired:
                    lab = "Close request" if r["is_active"] else "Reopen request"
                    if st.button(lab, key=f"tg_{r['id']}"):
                        db.table("donation_requests").update({"is_active": not r["is_active"]}) \
                            .eq("id", r["id"]).execute()
                        st.rerun()

    with t_prof:
        st.write(f"**80G Reg. No.:** {club['reg_no_80g']}  |  **Receipt code:** {club['short_code']}")
        with st.form("prof"):
            addr = st.text_area("Address", value=club["address"], height=80)
            val = st.text_input("80G approval validity", value=club.get("approval_validity") or "")
            cpan = st.text_input("Club PAN", value=club.get("club_pan") or "", max_chars=10)
            sign = st.text_input("Authorised signatory", value=club.get("signatory") or "")
            go = st.form_submit_button("Save", type="primary")
        if go:
            if cpan.strip() and not valid_pan(cpan.strip().upper()):
                st.error("Club PAN format is invalid.")
            elif len(addr.strip()) < 8 or len(sign.strip()) < 3:
                st.error("Address and signatory are required.")
            else:
                db.table("clubs").update({"address": addr.strip(),
                                          "approval_validity": val.strip() or None,
                                          "club_pan": cpan.strip().upper() or None,
                                          "signatory": sign.strip()}).eq("id", club["id"]).execute()
                st.success("Saved.")


# ------------------------------------------------------------------ SUPER ADMIN
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

    clubs = db.table("clubs").select("*").order("created_at", desc=True).execute().data
    tabs = st.tabs(["Pending", "Approved", "Rejected / Suspended"])
    groups = [["pending"], ["approved"], ["rejected", "suspended"]]
    for tab, g in zip(tabs, groups):
        with tab:
            sub = [c for c in clubs if c["status"] in g]
            if not sub:
                st.info("Nothing here.")
            for c in sub:
                with st.container(border=True):
                    st.write(f"**{c['name']}** ({c['short_code']}) — {c['status']}")
                    st.caption(f"{c['address']}  |  80G: {c['reg_no_80g']}  |  "
                               f"Validity: {c.get('approval_validity') or '-'}  |  "
                               f"PAN: {c.get('club_pan') or '-'}  |  user: {c['username']}")
                    cols = st.columns(3)
                    acts = [("Approve", "approved"), ("Reject", "rejected"), ("Suspend", "suspended")]
                    for col, (lab, new) in zip(cols, acts):
                        if c["status"] != new and col.button(lab, key=f"{new}_{c['id']}"):
                            db.table("clubs").update({"status": new}).eq("id", c["id"]).execute()
                            st.rerun()


# ------------------------------------------------------------------ ROUTER
page = st.sidebar.radio("Menu", ["Donate", "Club Portal", "Super Admin"])
if page != "Donate":
    st.session_state.pop("selected_req", None)
    st.session_state.pop("donation_done", None)
{"Donate": page_donate, "Club Portal": page_club, "Super Admin": page_super}[page]()
