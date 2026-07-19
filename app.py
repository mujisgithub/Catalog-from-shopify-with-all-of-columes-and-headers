"""
app.py
======
The web server. This is what "the backend" means: a small program that
sits on a computer in the cloud, receives the customer's CSV file,
runs catalog_maker.py, and hands back the finished PDF.

How one visit flows through this file:

  Browser                       This server
  -------                       -----------
  1. opens the site      ->     "/"          sends templates/index.html
  2. submits the form    ->     "/generate"  saves the CSV, starts a
                                             background job, replies
                                             instantly with a job id
  3. asks every 1.5 sec  ->     "/status/id" replies {percent, message}
  4. job finishes        ->     "/download/id" sends the PDF or ZIP

MAKING MONEY (automatic, single tier):
  Set STRIPE_SECRET_KEY plus PAY_LINK_PRO (or PAY_LINK_BASIC / PAY_LINK)
  in your host's environment and the paywall switches on. One payment,
  any price, unlocks unlimited products and every premium feature.
  Customers pay through your Stripe Payment Link, come straight back,
  and are unlocked automatically - their Stripe receipt is verified
  server-side and becomes their license.
"""

import os
import re
import time
import uuid
import shutil
import threading

import requests
from flask import Flask, request, jsonify, send_file, redirect
from werkzeug.utils import secure_filename

from catalog_maker import load_products, build_catalogs

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024   # refuse uploads over 25 MB

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

MAX_PRODUCTS = 3000          # hard ceiling so one giant file can't jam the server
JOB_MAX_AGE_SECONDS = 2 * 60 * 60   # delete job files after 2 hours

# All running/finished jobs live in this dictionary while the server runs.
# job_id -> {"status", "percent", "message", "result", "download_name", "created"}
JOBS = {}
JOBS_LOCK = threading.Lock()


# ----------------------------------------------------------
# Housekeeping: delete old job folders so the disk stays clean
# ----------------------------------------------------------
def cleanup_old_jobs():
    now = time.time()
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items()
                 if now - j["created"] > JOB_MAX_AGE_SECONDS]
        for jid in stale:
            JOBS.pop(jid, None)
    for name in os.listdir(JOBS_DIR):
        path = os.path.join(JOBS_DIR, name)
        try:
            if now - os.path.getmtime(path) > JOB_MAX_AGE_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


# ----------------------------------------------------------
# Reading the form safely
# ----------------------------------------------------------
def parse_discounts(raw):
    """'0, 10, 20' -> [0.0, 10.0, 20.0]. Empty -> [0]. Max 5 levels."""
    values = []
    for part in re.split(r"[,\s]+", (raw or "").strip()):
        if not part:
            continue
        try:
            d = float(part.replace("%", ""))
        except ValueError:
            raise ValueError(f"'{part}' isn't a number. Use e.g. 0, 10, 20")
        if not 0 <= d <= 90:
            raise ValueError("Discounts must be between 0 and 90.")
        if d not in values:
            values.append(d)
    if not values:
        values = [0.0]
    if len(values) > 5:
        raise ValueError("Maximum 5 discount levels per run.")
    return sorted(values)


def parse_sections(raw):
    """'Seerah, Quran, Kids' -> ['Seerah', 'Quran', 'Kids']. Premium."""
    names = []
    for part in (raw or "").split(","):
        p = part.strip()[:40]
        if p and p.lower() not in [n.lower() for n in names]:
            names.append(p)
    if len(names) > 12:
        raise ValueError("Maximum 12 category sections per catalog.")
    return names


def clean_store_url(raw):
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not re.match(r"^https?://[A-Za-z0-9.\-]+(\.[A-Za-z]{2,})(:\d+)?$", url):
        raise ValueError("Store URL should look like: https://yourstore.com")
    return url


def _keys(env_name):
    return {k.strip() for k in os.environ.get(env_name, "").split(",") if k.strip()}


def key_tier(key):
    """One tier now: is this key valid or not?
      'pro'  - listed in LICENSE_KEYS_PRO/LICENSE_KEYS_BASIC/LICENSE_KEYS,
               or a verified Stripe payment (any amount) / active subscription
      None   - no valid key (free tier)
    Keys starting with cs_ are Stripe checkout receipts and get verified
    with Stripe automatically - no manual work for you at all."""
    key = (key or "").strip()
    if not key or key in _keys("BLOCKED_KEYS"):    # refunds: block a key here
        return None
    if key in (_keys("LICENSE_KEYS_PRO") | _keys("LICENSE_KEYS_BASIC") | _keys("LICENSE_KEYS")):
        return "pro"
    if key.startswith("cs_"):
        return stripe_session_tier(key)
    return None


# ----------------------------------------------------------
# AUTOMATIC payments: verifying Stripe receipts
# ----------------------------------------------------------
# When a customer pays through your Stripe Payment Link, Stripe sends them
# back to /thanks with a "checkout session id" (cs_...). That id IS their
# license key: we ask Stripe's servers directly whether it was really paid
# (nobody can fake that - it needs your SECRET key, which lives only in
# Render's Environment as STRIPE_SECRET_KEY, never in this code).
#
# Verified keys are remembered in memory so we don't ask Stripe on every
# click. Subscriptions are re-checked once a day, so cancelled subscribers
# lose access automatically.
_VERIFIED = {}          # session_id -> (tier, checked_at, is_subscription)
_SUB_RECHECK_SECONDS = 24 * 60 * 60


def stripe_session_tier(session_id):
    if not re.fullmatch(r"cs_[A-Za-z0-9_]+", session_id or ""):
        return None
    now = time.time()
    cached = _VERIFIED.get(session_id)
    if cached:
        tier, checked_at, is_sub = cached
        if not is_sub or now - checked_at < _SUB_RECHECK_SECONDS:
            return tier

    secret = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        return None
    try:
        r = requests.get(
            "https://api.stripe.com/v1/checkout/sessions/" + session_id,
            auth=(secret, ""), timeout=15)
        if r.status_code != 200:
            return None
        session = r.json()
        if session.get("payment_status") != "paid":
            return None

        is_sub = session.get("mode") == "subscription"
        if is_sub:
            # Subscribers keep access only while the subscription is alive
            sub_id = session.get("subscription")
            r2 = requests.get("https://api.stripe.com/v1/subscriptions/" + str(sub_id),
                              auth=(secret, ""), timeout=15)
            if r2.status_code != 200 or r2.json().get("status") not in ("active", "trialing"):
                _VERIFIED.pop(session_id, None)
                return None
        # One tier now: any successful payment (any price) unlocks everything.
        tier = "pro"

        _VERIFIED[session_id] = (tier, now, is_sub)
        return tier
    except Exception:                              # noqa: BLE001
        # Network hiccup talking to Stripe: honour a previous verification
        return cached[0] if cached else None


def paywall_on():
    """The paywall switches on when key lists are configured OR Stripe
    automatic payments are connected (STRIPE_SECRET_KEY is set)."""
    return bool(_keys("LICENSE_KEYS") or _keys("LICENSE_KEYS_BASIC")
                or _keys("LICENSE_KEYS_PRO")
                or os.environ.get("STRIPE_SECRET_KEY", "").strip())


def free_limit():
    try:
        return int(os.environ.get("FREE_PRODUCT_LIMIT", "25"))
    except ValueError:
        return 25


# ----------------------------------------------------------
# The background worker
# ----------------------------------------------------------
def run_job(job_id, items, job_dir, options):
    def set_progress(pct, msg):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["percent"] = int(round(pct * 100))
                if msg:
                    job["message"] = msg

    try:
        result_path, _pdfs = build_catalogs(
            items, job_dir,
            business_name=options["business_name"],
            discounts=options["discounts"],
            currency=options["currency"],
            logo_path=options["logo_path"],
            layout=options.get("layout", "12"),
            section_names=options.get("section_names"),
            use_compare_at=options.get("use_compare_at", False),
            show_stock=options.get("show_stock", False),
            branding_text=options.get("branding_text", ""),
            branding_url=options.get("branding_url", ""),
            progress=set_progress,
        )
        with JOBS_LOCK:
            JOBS[job_id].update(
                status="done", percent=100, message="Your catalog is ready",
                result=result_path,
                download_name=os.path.basename(result_path),
            )
    except Exception as exc:                      # noqa: BLE001
        app.logger.exception("Job %s failed", job_id)
        with JOBS_LOCK:
            JOBS[job_id].update(
                status="error", message=f"Something went wrong: {exc}")


# ----------------------------------------------------------
# Routes (the URLs the browser talks to)
# ----------------------------------------------------------
@app.route("/")
def index():
    # The page is plain HTML (no template variables), so we serve the file
    # directly - and we accept it living either in templates/ or right next
    # to app.py. That makes the site immune to the file landing in the
    # "wrong" folder during a GitHub upload.
    for candidate in (os.path.join(BASE_DIR, "templates", "index.html"),
                      os.path.join(BASE_DIR, "index.html")):
        if os.path.exists(candidate):
            with open(candidate, encoding="utf-8") as f:
                return f.read()
    return ("<h1>Almost there</h1><p>index.html is missing from the "
            "deployment. Upload it to your GitHub repository (ideally "
            "inside the templates folder) and Render will redeploy.</p>"), 500


@app.route("/thanks")
def thanks():
    """Stripe sends customers here after a successful payment, with their
    checkout receipt in the address. We verify it really was paid, then
    send them back to the homepage where the page auto-fills their key
    and unlocks everything - no manual steps for anyone."""
    session_id = request.args.get("session_id", "").strip()
    tier = stripe_session_tier(session_id) if session_id else None
    if tier:
        return redirect(f"/?key={session_id}&tier={tier}")
    return ("""<!DOCTYPE html><html><head><meta charset="utf-8">
        <meta http-equiv="refresh" content="4">
        <title>Confirming your payment…</title>
        <style>body{font-family:sans-serif;background:#F4F4F1;color:#1A1F29;
        display:grid;place-items:center;min-height:90vh;text-align:center}</style>
        </head><body><div><h2>Confirming your payment…</h2>
        <p>This page checks again every few seconds.<br>
        If it doesn't confirm within a minute, go back to the site and
        try generating - or contact us with your Stripe receipt email.</p>
        <p><a href="/">Back to CatalogPress</a></p></div></body></html>"""), 202


@app.route("/pricing")
def pricing():
    """Tells the page which payment link to show under the license box."""
    link = (os.environ.get("PAY_LINK_PRO", "").strip()
           or os.environ.get("PAY_LINK_BASIC", "").strip()
           or os.environ.get("PAY_LINK", "").strip())
    return jsonify(buy=link)


@app.route("/generate", methods=["POST"])
def generate():
    cleanup_old_jobs()

    # ---- 1. validate everything the user typed -----------------------
    csv_file = request.files.get("csv")
    if not csv_file or not csv_file.filename.lower().endswith(".csv"):
        return jsonify(error="Please choose your Shopify CSV export (a .csv file)."), 400

    business_name = (request.form.get("business_name") or "").strip()[:60]
    if not business_name:
        return jsonify(error="Please enter your business or catalog name."), 400

    try:
        store_url = clean_store_url(request.form.get("store_url"))
        discounts = parse_discounts(request.form.get("discounts"))
        section_names = parse_sections(request.form.get("sections"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    # PDF fonts can only draw Western characters, so currencies whose symbol
    # needs special glyphs (like the rupee or riyal signs) use their standard
    # letter codes instead - that's normal on trade price lists anyway.
    currency = request.form.get("currency", "£")
    if currency not in ("£", "$", "€", "SAR ", "AED ", "Rs ", "Rp ",
                        "RM ", "BDT ", "TL "):
        currency = "£"

    layout = request.form.get("layout", "12")
    if layout not in ("12", "20", "30"):
        layout = "12"
    use_compare_at = request.form.get("compare_at") == "on"
    show_stock = request.form.get("show_stock") == "on"

    # ---- 2. save the uploads into this job's private folder ----------
    job_id = uuid.uuid4().hex
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    csv_path = os.path.join(job_dir, secure_filename(csv_file.filename) or "products.csv")
    csv_file.save(csv_path)

    logo_path = ""
    logo_file = request.files.get("logo")
    if logo_file and logo_file.filename:
        if not logo_file.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify(error="Logo must be a PNG, JPG or WEBP image."), 400
        logo_path = os.path.join(job_dir, "logo" + os.path.splitext(logo_file.filename)[1].lower())
        logo_file.save(logo_path)

    # ---- 3. read the CSV now, so bad files fail instantly ------------
    try:
        items = load_products(csv_path, store_url)
    except Exception as e:                        # noqa: BLE001
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=f"Couldn't read that CSV: {e}"), 400

    if not items:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error="No sellable products found. Each product row "
                             "needs at least a title and a price."), 400
    if len(items) > MAX_PRODUCTS:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=f"That's {len(items)} products — the limit is "
                             f"{MAX_PRODUCTS} per catalog."), 400

    # ---- 4. the paywall (single tier: any valid key/payment = full access)
    tier = key_tier(request.form.get("license_key", ""))
    wall = paywall_on()
    limit = free_limit()

    premium_used = []
    if section_names:
        premium_used.append("category sections")
    if layout != "12":
        premium_used.append(f"{layout}-per-page layout")
    if use_compare_at:
        premium_used.append("compare-at prices")
    if show_stock:
        premium_used.append("stock counts")

    # One purchase link now (checked in this order so old BASIC/PRO setups
    # keep working without changes): PAY_LINK_PRO, then PAY_LINK_BASIC,
    # then a plain PAY_LINK if you ever rename it.
    pay_link = (os.environ.get("PAY_LINK_PRO", "").strip()
               or os.environ.get("PAY_LINK_BASIC", "").strip()
               or os.environ.get("PAY_LINK", "").strip())

    if wall and limit and len(items) > limit and tier is None:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=f"The free plan covers up to {limit} products and "
                             f"your file has {len(items)}. Buy a license key for "
                             "full access — payment takes under a minute and "
                             "you'll be brought straight back here, unlocked "
                             "automatically.",
                       upgrade=True, pay_url=pay_link), 402

    if wall and premium_used and tier is None:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error="These are premium features: "
                             + ", ".join(premium_used)
                             + ". Buy a license key for full access — payment "
                               "takes under a minute and you'll be brought "
                               "straight back here, unlocked automatically.",
                       upgrade=True, pay_url=pay_link), 402

    # Free catalogs carry a small clickable footer back to this site -
    # every shared PDF advertises you. Any paying customer gets clean PDFs.
    if tier is None:
        branding_text = os.environ.get(
            "BRAND_TEXT", "Made with CatalogPress · catalog-from-shopify.onrender.com")
        branding_url = os.environ.get(
            "BRAND_URL", "https://catalog-from-shopify.onrender.com")
    else:
        branding_text = branding_url = ""

    # ---- 5. start the background job and reply straight away ---------
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "working", "percent": 1,
                        "message": "Starting…", "result": None,
                        "download_name": None, "created": time.time()}

    options = {"business_name": business_name, "discounts": discounts,
               "currency": currency, "logo_path": logo_path,
               "layout": layout, "section_names": section_names,
               "use_compare_at": use_compare_at, "show_stock": show_stock,
               "branding_text": branding_text, "branding_url": branding_url}
    threading.Thread(target=run_job, args=(job_id, items, job_dir, options),
                     daemon=True).start()

    return jsonify(job_id=job_id, products=len(items), variants=len(discounts))


@app.route("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(status="unknown",
                           message="Job not found (it may have expired)."), 404
        return jsonify(status=job["status"], percent=job["percent"],
                       message=job["message"],
                       download_name=job["download_name"])


@app.route("/download/<job_id>")
def download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["status"] != "done" or not job["result"]:
        return "This download isn't ready or has expired.", 404
    return send_file(job["result"], as_attachment=True,
                     download_name=job["download_name"])


if __name__ == "__main__":
    # Local testing only. In the cloud, gunicorn runs the app instead
    # (see the Start Command in README.md).
    app.run(debug=True, port=5000)
