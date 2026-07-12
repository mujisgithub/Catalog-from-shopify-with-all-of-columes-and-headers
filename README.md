# CatalogPress — Shopify CSV → PDF catalog website

This is a complete, ready-to-host website. A customer uploads the CSV that
Shopify's **Products → Export** button gives them, types their discount
levels (e.g. `0, 10, 20`), and a couple of minutes later downloads a ZIP
containing one designed, clickable PDF catalog **per discount level** —
old prices crossed out, new prices in the badge.

It is your original catalog script, wrapped in a small web server.

---

## 1. What each file does

| File | In plain English |
|---|---|
| `app.py` | **The backend.** Receives the upload, starts the job, reports progress, serves the download. ~200 lines, heavily commented. |
| `catalog_maker.py` | **The PDF engine** — your original code, reorganised, with discounts, parallel image downloads and progress reporting added. All design settings (colours, fonts, grid size) live at the top of this file. |
| `templates/index.html` | **The page customers see.** Upload form, live progress bar, download button. |
| `requirements.txt` | The list of Python packages the server needs. Hosting providers read this and install them for you. |
| `render.yaml` | Optional one-click setup file for Render.com. |

**How a request flows:** browser sends the CSV to `/generate` → server
answers instantly with a job number and keeps working in the background →
browser asks `/status/<job>` every 1.5 s and fills the progress bar →
when done, `/download/<job>` sends the PDF or ZIP. Files self-delete
after 2 hours.

---

## 2. Run it on your own computer first (5 minutes)

You already have Python (you ran the original script). In Terminal:

```bash
cd catalog-app                # the folder this README is in
pip3 install -r requirements.txt
python3 app.py
```

Open **http://127.0.0.1:5000** in your browser. Upload your real
`products_export.csv`, set discounts to `0, 10, 20`, click Generate.
You should get a ZIP with three PDFs. If that works locally, hosting it
is just "run the same thing on someone else's computer".

---

## 3. Put it on the internet (Render.com, free)

Render is the easiest host for beginners: you connect a GitHub folder,
it runs your app, you get a public link like `catalogpress.onrender.com`.

**A. Put the code on GitHub**
1. Create a free account at github.com.
2. Click **New repository**, name it `catalogpress`, keep it Private, create.
3. On the repository page click **uploading an existing file** and drag in
   everything from this folder (`app.py`, `catalog_maker.py`,
   `requirements.txt`, `render.yaml`, and the `templates` folder with
   `index.html` inside — GitHub keeps the folder if you drag the folder).
4. Click **Commit changes**.

**B. Deploy on Render**
1. Create a free account at render.com (sign in with GitHub — easiest).
2. Click **New → Web Service** and pick your `catalogpress` repository.
3. Render usually detects everything from `render.yaml`. If it asks:
   - Runtime: **Python 3**
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --workers 1 --threads 8 --timeout 300`
   - Instance type: **Free**
4. Click **Create Web Service** and wait ~3 minutes for the first build.
5. Your site is live at the `.onrender.com` link at the top of the page.

**Important:** keep `--workers 1` in the start command. Progress tracking
lives in the app's memory; one worker with 8 threads handles plenty of
simultaneous users and keeps everything simple.

**Free-plan honesty:** free Render services fall asleep after 15 minutes
of no visitors, so the *first* visit of the day takes ~50 seconds to wake
up. Once you have paying customers, the $7/month "Starter" plan removes
that and is the only running cost you need.

**C. Your own domain (optional, looks professional)**
Buy a domain (~£10/year at Namecheap or Cloudflare), then in Render open
your service → **Settings → Custom Domains → Add**, and follow the two
DNS records it shows you. Done in 10 minutes.

---

## 4. Charge for it (no payment code needed)

The app has a built-in freemium switch that needs **zero programming**:

1. In Render, open your service → **Environment** → add two variables:
   - `FREE_PRODUCT_LIMIT` = `25`
   - `LICENSE_KEYS` = `CP-7F3K9Q, CP-2M8XZL` (invent your own codes,
     comma-separated — add a new one for every customer)
2. Click **Save** (Render restarts the app automatically).

Now anyone can make small catalogs free, but a file with more than 25
products shows: *"Enter a license key to unlock unlimited catalogs."*

To actually take the money, create a **Stripe Payment Link**
(stripe.com → Payment Links → New): set your price — e.g. **£15/month**
or **£9 one-off per key** — and put the payment link on your page.
When someone pays, Stripe emails you; you invent a new key, add it to
`LICENSE_KEYS` in Render, and email it to the customer. Manual, yes —
but you can take your first payment today, and automate later once it's
proven. (Gumroad works the same way and even generates license keys
for you.)

**Pricing ideas people actually pay for:** trade/wholesale suppliers who
send price lists to retailers are the perfect customer — the multi-discount
feature is built exactly for them.

---

## 5. Changing the design of the PDFs

Everything visual sits at the top of `catalog_maker.py`:

- `COLUMNS` / `ROWS` — cards per page (3 × 4 = 12 by default)
- `ACCENT`, `HEADER_BAR`, `PRICE_BADGE_BG` — the colour scheme
- `TITLE_MAX_LINES`, font sizes, paddings

Change a number, refresh, regenerate. The website's own look lives in
`templates/index.html` (colours are the `--paper / --ink / --brass`
tokens at the top of the `<style>` block). Rename "CatalogPress" there
to whatever you call your product.

---

## 6. Troubleshooting

- **"Missing columns" error** — the customer exported the wrong thing.
  It must be Shopify's *product* export (Products → Export → Plain CSV),
  not an order or customer export.
- **Some cards say "Image unavailable"** — that product's image URL was
  broken or private. The catalog still builds; those cards get a neat
  placeholder instead of failing the whole job.
- **A product is missing from the PDF** — products need a Title, a
  Variant SKU and a Variant Price to be included, and only the first
  variant of each product gets a card (same rule as your original script).
- **Site sleeps / first load slow** — that's the free plan waking up;
  upgrade to Starter when you're ready.
- **Changed the code?** Upload the changed file to GitHub (open the file
  there → pencil icon → paste → Commit). Render redeploys automatically.

---

## 7. Sensible next upgrades (when you outgrow v1)

In rough order of value: automatic license keys (Stripe webhooks or
Gumroad), category grouping in the PDF (Shopify's "Type" column),
"Compare At Price" support, a per-customer cover note field, and swapping
the in-memory job list for Redis if you ever need multiple workers.
Each is a small, separate job — the code is structured so none of them
require a rewrite.
