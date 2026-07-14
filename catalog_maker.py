"""
catalog_maker.py
================
The "engine" of the website: takes a Shopify product CSV and turns it
into one or more PDF catalogs (one per discount level).

FREE features:
  - designed product cards (photo, title, SKU, price badge), all clickable
  - discount versions with the old price crossed out
  - accepts every Shopify export flavour (old/new headers, no header,
    semicolons, odd encodings - only a title and a price are required)

PREMIUM features (switched on per-request by app.py):
  - category SECTIONS: merchant types section names, products are grouped
    by their Shopify tags under divider pages, in the merchant's order
  - compact LAYOUTS: 20 or 30 products per page instead of 12
  - COMPARE-AT prices: the store's own "was" price shown crossed out
  - STOCK counts on each card

All the visual design knobs live in the SETTINGS block below.
"""

import os
import re
import socket
import ipaddress
import zipfile
from io import BytesIO
from datetime import date
from urllib.parse import quote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# =========================================================
# SETTINGS - tweak the look of every catalog here
# =========================================================
# Page layouts. "scale" shrinks fonts/paddings so smaller cards stay tidy.
LAYOUTS = {
    "12": {"cols": 3, "rows": 4, "scale": 1.00, "gutter_mm": 5, "img_ratio": 0.50},
    "20": {"cols": 4, "rows": 5, "scale": 0.82, "gutter_mm": 4, "img_ratio": 0.48},
    "30": {"cols": 5, "rows": 6, "scale": 0.70, "gutter_mm": 3, "img_ratio": 0.44},
}

IMAGE_PADDING_MM = 2.5
PAGE_MARGIN_MM = 10

TITLE_FONT = "Helvetica-Bold"
TITLE_FONT_SIZE = 8.5
TITLE_MAX_LINES = 2
TITLE_LINE_GAP = 9

META_FONT = "Helvetica"
META_FONT_SIZE = 7.5

PRICE_FONT = "Helvetica-Bold"
PRICE_FONT_SIZE = 10

STRIKE_FONT = "Helvetica"
STRIKE_FONT_SIZE = 8

# Colours (red, green, blue - each 0.0 to 1.0)
ZINC_BG = (0.97, 0.97, 0.98)
CARD_BG = (1, 1, 1)
CARD_BORDER = (0.82, 0.82, 0.86)
SHADOW = (0.86, 0.86, 0.90)
HEADER_BAR = (0.10, 0.12, 0.16)
HEADER_TEXT = (1, 1, 1)
ACCENT = (0.85, 0.65, 0.15)

PRICE_BADGE_BG = (0.10, 0.12, 0.16)
PRICE_BADGE_TEXT = (1, 1, 1)

META_PILL_BG = (0.93, 0.93, 0.95)
META_PILL_TEXT = (0.22, 0.24, 0.28)
META_PILL_BORDER = (0.86, 0.86, 0.90)

STRIKE_COLOR = (0.45, 0.47, 0.52)

CARD_RADIUS = 10
CARD_BORDER_WIDTH = 1.1
SHADOW_OFFSET = 2.0

DOWNLOAD_THREADS = 12       # how many images to download at the same time
DOWNLOAD_TIMEOUT = 20       # seconds before giving up on one image

# Shopify has exported products under several different column names over
# the years (and some files arrive with no header row at all). We accept
# them all. Only a TITLE and a PRICE are truly required - SKU, stock,
# images, tags and compare-at prices are used when present.
HEADER_SYNONYMS = {
    "title":    ["title", "product title", "name", "product name"],
    "handle":   ["handle", "url handle", "product handle"],
    "sku":      ["variant sku", "sku"],
    "qty":      ["variant inventory qty", "inventory quantity", "inventory qty",
                 "variant inventory quantity", "available", "on hand",
                 "stock", "quantity"],
    "price":    ["variant price", "price"],
    "image":    ["image src", "product image url", "image url", "featured image",
                 "image link", "image", "variant image url", "variant image"],
    "compare":  ["variant compare at price", "compare-at price",
                 "compare at price", "compare at"],
    "tags":     ["tags", "product tags"],
    "category": ["product category", "category"],
    "type":     ["type", "product type"],
}

# Values that only ever appear in specific Shopify columns - we use these
# as "anchors" to find our way around files that have no header row.
_POLICY_VALUES = {"deny", "continue"}
_TRACKER_VALUES = {"shopify", "not tracked", "fulfillment-service-handles", ""}


# =========================================================
# Small helpers
# =========================================================
def clean_str(x):
    s = str(x).strip()
    return "" if s.lower() in ("nan", "none") else s


def parse_price(v):
    """'£12.50', '12.5', '1,299.00' or European '12,50' -> a number.
    Returns None if unreadable."""
    s = clean_str(v).replace("£", "").replace("$", "").replace("€", "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{1,6},\d{1,2}", s):      # '12,50' means 12.50
        s = s.replace(",", ".")
    else:                                        # '1,299.00' -> 1299.00
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def format_money(amount, symbol):
    return f"{symbol}{amount:,.2f}"


def to_int_qty(v):
    s = clean_str(v)
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def safe_filename(s):
    s = clean_str(s)
    s = re.sub(r"[^\w\-\. ]+", "", s)
    s = re.sub(r"\s+", "_", s)
    return s[:120] if s else "item"


def product_url(store_domain, handle):
    handle = clean_str(handle)
    if not handle or not store_domain:
        return ""
    return f"{store_domain.rstrip('/')}/products/{quote(handle)}"


# =========================================================
# Image safety + downloading
# =========================================================
_host_check_cache = {}

def is_safe_image_url(url):
    """Only allow normal public http/https image links. Blocks links that
    point at private/internal network addresses (a standard safety measure
    for any website that downloads URLs supplied inside an uploaded file)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname
        if host in _host_check_cache:
            return _host_check_cache[host]
        ok = True
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                ok = False
                break
        _host_check_cache[host] = ok
        return ok
    except Exception:
        return False


def flatten_to_white(img):
    """Transparent PNGs turn black with a plain convert('RGB').
    This pastes them onto white first, which looks right in the catalog."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        return background
    return img.convert("RGB")


def download_image(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CatalogMaker/1.0)",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    r = requests.get(url, timeout=DOWNLOAD_TIMEOUT, headers=headers)
    r.raise_for_status()
    return flatten_to_white(Image.open(BytesIO(r.content)))


def predownload_images(items, cache_dir, progress=None):
    """Download every product photo BEFORE drawing any PDF.
    Runs 12 downloads at once, so 200 photos take seconds not minutes.
    Each item gets item["img_path"] = local file (or None if it failed)."""
    os.makedirs(cache_dir, exist_ok=True)

    def fetch_one(item):
        url = item.get("image", "")
        if not url:
            return None
        cache_path = os.path.join(
            cache_dir, safe_filename(item["sku"] or item["title"]) + ".jpg"
        )
        if os.path.exists(cache_path):
            return cache_path
        if not is_safe_image_url(url):
            return None
        try:
            img = download_image(url)
            img.save(cache_path, "JPEG", quality=88)
            return cache_path
        except Exception:
            return None   # one bad image never kills the whole catalog

    total = len(items)
    done = 0
    with ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS) as pool:
        futures = {pool.submit(fetch_one, it): it for it in items}
        for fut in as_completed(futures):
            futures[fut]["img_path"] = fut.result()
            done += 1
            if progress:
                progress(done / total, f"Downloading product photos ({done}/{total})")


# =========================================================
# Reading the Shopify CSV - accepts every export variation
# =========================================================
def _norm(name):
    """'  Variant_Price ' -> 'variant price' for header comparison."""
    s = str(name).replace("\ufeff", "").replace("_", " ").strip().lower()
    return re.sub(r"\s+", " ", s)


def _guess_sep(line):
    """Pick the delimiter by counting candidates OUTSIDE quoted text."""
    counts = {}
    for sep in [",", ";", "\t", "|"]:
        in_quotes, n = False, 0
        for ch in line:
            if ch == '"':
                in_quotes = not in_quotes
            elif ch == sep and not in_quotes:
                n += 1
        counts[sep] = n
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def _read_dataframe(csv_path):
    """Load the file with EVERY row as data (no header assumption yet),
    trying sensible encodings and sniffing the delimiter."""
    with open(csv_path, "rb") as f:
        head = f.read(8192)
    if head[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in head[:200]:
        encodings = ["utf-16", "utf-8-sig", "latin-1"]
    else:
        encodings = ["utf-8-sig", "latin-1"]

    last_err = None
    for enc in encodings:
        try:
            with open(csv_path, "r", encoding=enc, errors="strict") as f:
                first_line = f.readline()
            sep = _guess_sep(first_line)
            df = pd.read_csv(csv_path, header=None, dtype=str, sep=sep,
                             encoding=enc, engine="python",
                             keep_default_na=False, on_bad_lines="skip")
            if len(df.columns) >= 2:
                return df
        except Exception as e:                    # noqa: BLE001
            last_err = e
    raise ValueError(f"Couldn't open this file as a CSV ({last_err}).")


def _looks_like_image_url(v):
    v = str(v)
    return v.startswith("http") and (
        "cdn.shopify" in v
        or "/files/" in v
        or re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", v, re.I)
    )


def _map_from_header(header_cells):
    """If row 0 is a header, map field -> column index via known names."""
    normed = [_norm(c) for c in header_cells]
    if not any(c in HEADER_SYNONYMS["title"] for c in normed):
        return None                                # row 0 isn't a header

    colmap = {}
    for field, names in HEADER_SYNONYMS.items():
        for name in names:                         # first synonym wins
            if name in normed:
                colmap[field] = normed.index(name)
                break

    # Fallbacks for renamed/localised columns like "Price / United Kingdom"
    if "price" not in colmap:
        for i, c in enumerate(normed):
            if c.startswith("price") and "compare" not in c:
                colmap["price"] = i
                break
    if "qty" not in colmap:
        for i, c in enumerate(normed):
            if (c.startswith("inventory quantity") or c.startswith("available")
                    or c.startswith("variant inventory")):
                colmap["qty"] = i
                break

    return colmap if "price" in colmap else None


def _map_from_anchors(df):
    """No header row: locate columns by their tell-tale CONTENT.
    Shopify's inventory-policy column only ever says deny/continue -
    from there, fulfillment comes next, then price, then compare-at.
    SKU sits two or three columns to the left; images are the first
    http column; tags/category sit at Shopify's fixed early positions."""
    ncols = len(df.columns)
    sample = df.head(200)

    def nonempty(col):
        if col < 0 or col >= ncols:
            return []
        return [v.strip() for v in sample[col].tolist() if str(v).strip()]

    policy_col = None
    for c in range(ncols):
        vals = nonempty(c)
        if vals and {v.lower() for v in vals} <= _POLICY_VALUES:
            policy_col = c
            break
    if policy_col is None:
        return None

    colmap = {"handle": 0, "title": 1}
    # Shopify's fixed early columns in every product export:
    if ncols > 4:
        colmap["category"] = 4
    if ncols > 5:
        colmap["type"] = 5
    if ncols > 6:
        colmap["tags"] = 6

    # Column just left of policy: either the qty numbers, or the tracker
    left = {v.lower() for v in nonempty(policy_col - 1)}
    if left and left <= _TRACKER_VALUES:
        tracker_col = policy_col - 1               # store exports no qty
    else:
        colmap["qty"] = policy_col - 1
        tracker_col = policy_col - 2

    # SKU sits left of the grams column, which sits left of the tracker
    grams_vals = nonempty(tracker_col - 1)
    grams_numeric = grams_vals and all(
        re.fullmatch(r"-?\d+(\.\d+)?", v) for v in grams_vals[:20])
    colmap["sku"] = tracker_col - 2 if grams_numeric else tracker_col - 1

    # Price: policy -> fulfillment -> price (both layouts). Verify, and
    # scan a little to the right if this store's file is unusual.
    for candidate in (policy_col + 2, policy_col + 1, policy_col + 3):
        vals = nonempty(candidate)
        if vals and sum(parse_price(v) is not None for v in vals) >= 0.7 * len(vals):
            colmap["price"] = candidate
            break
    if "price" not in colmap:
        return None

    # Compare-at price sits immediately after the price column
    cmp_vals = nonempty(colmap["price"] + 1)
    if not cmp_vals or sum(parse_price(v) is not None
                           for v in cmp_vals) >= 0.7 * len(cmp_vals):
        colmap["compare"] = colmap["price"] + 1

    # Image: first column that mostly holds picture links
    for c in range(ncols):
        vals = nonempty(c)
        if vals and sum(_looks_like_image_url(v) for v in vals) >= 0.5 * len(vals):
            colmap["image"] = c
            break

    # Sanity-check: handle should look like Shopify slugs
    handles = nonempty(0)
    if not handles or not re.fullmatch(r"[a-z0-9][a-z0-9\-_.]*", handles[0]):
        colmap.pop("handle", None)

    return colmap


def load_products(csv_path, store_domain):
    """Reads ANY Shopify product export and returns one entry per product.
    Only a title and a price are required; everything else is optional."""
    df = _read_dataframe(csv_path)

    colmap = _map_from_header(df.iloc[0].tolist())
    if colmap is not None:
        df = df.iloc[1:].reset_index(drop=True)    # drop the header row
    else:
        colmap = _map_from_anchors(df)

    if not colmap or "title" not in colmap or "price" not in colmap:
        raise ValueError(
            "I couldn't find the product title and price in this file. "
            "Please export from Shopify admin via Products \u2192 Export \u2192 "
            "'Plain CSV file' and upload that file unchanged."
        )

    def cell(row, field):
        idx = colmap.get(field)
        if idx is None or idx < 0 or idx >= len(row):
            return ""
        return clean_str(row.iloc[idx])

    # Shopify often puts a product's photo on a *different* row than its
    # price (extra image rows). Lookup: handle -> first image URL found.
    image_by_handle = {}
    if "handle" in colmap and "image" in colmap:
        for _, r in df.iterrows():
            h, img = cell(r, "handle"), cell(r, "image").split(",")[0].strip()
            if h and img and h not in image_by_handle:
                image_by_handle[h] = img

    items, seen = [], set()
    for _, r in df.iterrows():
        title = cell(r, "title")
        price = parse_price(cell(r, "price"))
        if not title or price is None:
            continue                               # variant/image/blank rows

        handle = cell(r, "handle")
        sku = cell(r, "sku")
        # one card per product, never duplicates
        key = ("s", sku) if sku else ("h", handle) if handle else ("t", title)
        if key in seen:
            continue
        seen.add(key)

        img = cell(r, "image").split(",")[0].strip() or image_by_handle.get(handle, "")
        tag_set = {t.strip().lower() for t in cell(r, "tags").split(",") if t.strip()}
        cat_text = (cell(r, "category") + " " + cell(r, "type")).lower()

        compare_at = parse_price(cell(r, "compare"))
        if compare_at is not None and compare_at <= 0:
            compare_at = None

        items.append({
            "url": product_url(store_domain, handle),
            "title": title,
            "sku": sku,
            "qty": to_int_qty(cell(r, "qty")),
            "price": price,
            "compare_at": compare_at,
            "tag_set": tag_set,
            "cat_text": cat_text,
            "image": img,
            "img_path": None,
        })
    return items


# =========================================================
# PREMIUM: grouping products into merchant-named sections
# =========================================================
def assign_sections(items, section_names):
    """The merchant types e.g. "Seerah, Quran, Kids". Each product joins
    the FIRST section whose name matches one of its Shopify tags (exact
    tag, part of a tag like 'seerah books', or part of the product
    category). Anything unmatched goes into a final "More Products"
    section. Returns a list of (section_name, items); with no names it
    returns one nameless section containing everything (= no dividers)."""
    if not section_names:
        return [(None, items)]

    buckets = {name: [] for name in section_names}
    rest = []
    for it in items:
        placed = False
        for name in section_names:
            n = name.strip().lower()
            if not n:
                continue
            if (n in it["tag_set"]
                    or any(n in tag for tag in it["tag_set"])
                    or (it["cat_text"].strip() and n in it["cat_text"])):
                buckets[name].append(it)
                placed = True
                break
        if not placed:
            rest.append(it)

    sections = [(name, buckets[name]) for name in section_names if buckets[name]]
    if rest:
        sections.append(("More Products" if sections else None, rest))
    return sections


# =========================================================
# Drawing helpers
# =========================================================
def resize_contain(img, target_w, target_h, bg=(255, 255, 255)):
    iw, ih = img.size
    scale = min(target_w / iw, target_h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    canvas_img = Image.new("RGB", (target_w, target_h), bg)
    canvas_img.paste(resized, ((target_w - nw) // 2, (target_h - nh) // 2))
    return canvas_img


def wrap_title(c, text, max_width, font, size, max_lines):
    text = clean_str(text)
    if not text:
        return [""]
    words, lines, current = text.split(), [], ""
    for w in words:
        trial = (current + " " + w).strip()
        if c.stringWidth(trial, font, size) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = w
            while c.stringWidth(current + "…", font, size) > max_width and len(current) > 1:
                current = current[:-1]
            if c.stringWidth(current, font, size) > max_width:
                current += "…"
        if len(lines) >= max_lines:
            break
    if len(lines) < max_lines and current:
        lines.append(current)
    if len(lines) == max_lines and " ".join(lines).split() != words and not lines[-1].endswith("…"):
        last = lines[-1]
        while c.stringWidth(last + "…", font, size) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines[:max_lines]


def draw_image_aspect(c, pil_img, x, y, box_w, box_h):
    iw, ih = pil_img.size
    if iw <= 0 or ih <= 0:
        return
    scale = min(box_w / iw, box_h / ih)
    dw, dh = iw * scale, ih * scale
    c.drawImage(ImageReader(pil_img), x + (box_w - dw) / 2, y + (box_h - dh) / 2,
                dw, dh, mask="auto")


def add_link(c, url, rect):
    if not url:
        return
    x1, y1, x2, y2 = rect
    c.linkURL(url, (x1 - 1.5, y1 - 1.5, x2 + 1.5, y2 + 1.5), relative=0)


def draw_badge_center(c, cx, y, text, font, size, pad_x, pad_y, bg, fg,
                      radius, border=None):
    text = clean_str(text)
    if not text:
        return (0, 0, 0, 0, 0)
    c.setFont(font, size)
    tw = c.stringWidth(text, font, size)
    bw, bh = tw + 2 * pad_x, size + 2 * pad_y
    x = cx - bw / 2
    c.setFillColorRGB(*bg)
    if border:
        c.setStrokeColorRGB(*border)
        c.setLineWidth(0.8)
        c.roundRect(x, y, bw, bh, radius, stroke=1, fill=1)
    else:
        c.roundRect(x, y, bw, bh, radius, stroke=0, fill=1)
    c.setFillColorRGB(*fg)
    c.drawString(x + pad_x, y + pad_y, text)
    return (x, y, x + bw, y + bh, bh)


def load_logo(path):
    path = clean_str(path)
    if not path or not os.path.exists(path):
        return None
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        return None


# =========================================================
# Building ONE pdf at ONE discount level
# =========================================================
def build_pdf(items, out_path, *, business_name, discount=0.0, currency="£",
              logo_path="", layout="12", sections=None,
              use_compare_at=False, show_stock=False, progress=None):
    W, H = A4
    c = canvas.Canvas(out_path, pagesize=A4)
    logo_img = load_logo(logo_path)
    today = date.today().strftime("%d %B %Y")

    # ---- sizes for the chosen layout (premium: 20 or 30 per page) ----
    L = LAYOUTS.get(str(layout), LAYOUTS["12"])
    cols, rows, s = L["cols"], L["rows"], L["scale"]
    gutter = L["gutter_mm"] * mm
    img_ratio = L["img_ratio"]

    title_size = TITLE_FONT_SIZE * s
    title_gap = TITLE_LINE_GAP * s
    meta_size = max(5.0, META_FONT_SIZE * s)
    price_size = PRICE_FONT_SIZE * s
    strike_size = max(5.5, STRIKE_FONT_SIZE * s)
    pad = IMAGE_PADDING_MM * mm * s
    radius = CARD_RADIUS * s

    if sections is None:
        sections = [(None, items)]

    def page_bg():
        c.setFillColorRGB(*ZINC_BG)
        c.rect(0, 0, W, H, stroke=0, fill=1)

    def header(page_no):
        header_h = 16 * mm
        c.setFillColorRGB(*HEADER_BAR)
        c.rect(0, H - header_h, W, header_h, stroke=0, fill=1)
        if logo_img:
            box = 12 * mm
            draw_image_aspect(c, logo_img, 10 * mm,
                              H - header_h + (header_h - box) / 2, box, box)
        c.setFillColorRGB(*HEADER_TEXT)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(10 * mm + (16 * mm if logo_img else 0),
                     H - header_h + 5.3 * mm, f"{business_name} Catalog")
        c.setFont("Helvetica", 9)
        c.drawRightString(W - 10 * mm, H - header_h + 5.2 * mm, f"Page {page_no}")

    def cover():
        page_bg()
        c.setFillColorRGB(*HEADER_BAR)
        c.rect(0, H - 40 * mm, W, 40 * mm, stroke=0, fill=1)
        if logo_img:
            draw_image_aspect(c, logo_img, (W - 45 * mm) / 2,
                              H - 35 * mm - 22.5 * mm, 45 * mm, 45 * mm)

        title = f"{business_name} Product Catalog {date.today().year}"
        c.setFillColorRGB(0.10, 0.12, 0.16)
        c.setFont("Helvetica-Bold", 26)
        c.drawCentredString(W / 2, H / 2 + 10 * mm, title)

        subtitle = (f"Special price list — {discount:g}% off every item"
                    if discount > 0 else "Full price list")
        c.setFont("Helvetica", 12)
        c.setFillColorRGB(0.25, 0.27, 0.30)
        c.drawCentredString(W / 2, H / 2 - 2 * mm, subtitle)

        c.setFont("Helvetica", 10)
        c.setFillColorRGB(0.45, 0.47, 0.52)
        c.drawCentredString(W / 2, H / 2 - 10 * mm, f"Generated {today}")

        c.setStrokeColorRGB(*ACCENT)
        c.setLineWidth(2)
        c.line(W * 0.25, H / 2 - 16 * mm, W * 0.75, H / 2 - 16 * mm)
        c.showPage()

    def divider(name, sec_no, count, page_no):
        """PREMIUM: full divider page announcing the next section."""
        page_bg()
        header(page_no)
        y_mid = H * 0.58
        c.setFillColorRGB(*ACCENT)
        c.setFont("Helvetica-Bold", 11)
        c.drawCentredString(W / 2, y_mid + 18 * mm, f"S E C T I O N   {sec_no}")

        size = 34
        while c.stringWidth(name, "Helvetica-Bold", size) > W * 0.82 and size > 14:
            size -= 2
        c.setFillColorRGB(0.10, 0.12, 0.16)
        c.setFont("Helvetica-Bold", size)
        c.drawCentredString(W / 2, y_mid, name)

        c.setStrokeColorRGB(*ACCENT)
        c.setLineWidth(2)
        c.line(W * 0.36, y_mid - 8 * mm, W * 0.64, y_mid - 8 * mm)

        c.setFillColorRGB(0.40, 0.42, 0.47)
        c.setFont("Helvetica", 11)
        label = "1 product" if count == 1 else f"{count} products"
        c.drawCentredString(W / 2, y_mid - 16 * mm, label)
        c.showPage()

    def card_frame(x, y, w, h):
        c.setFillColorRGB(*SHADOW)
        c.roundRect(x + SHADOW_OFFSET * s, y - SHADOW_OFFSET * s, w, h,
                    radius, stroke=0, fill=1)
        c.setFillColorRGB(*CARD_BG)
        c.setStrokeColorRGB(*CARD_BORDER)
        c.setLineWidth(CARD_BORDER_WIDTH)
        c.roundRect(x, y, w, h, radius, stroke=1, fill=1)

    # ---- layout maths -------------------------------------------------
    cover()
    margin = PAGE_MARGIN_MM * mm
    header_h = 16 * mm
    usable_h = H - header_h - 2 * margin
    tile_w = (W - 2 * margin - (cols - 1) * gutter) / cols
    tile_h = (usable_h - (rows - 1) * gutter) / rows
    img_area_h = tile_h * img_ratio

    dpi_scale = 150 / 72
    img_box_w_px = int((tile_w - 2 * pad) * dpi_scale)
    img_box_h_px = int((img_area_h - 2 * pad) * dpi_scale)

    per_page = cols * rows
    page_no = 1
    total = sum(len(sec_items) for _, sec_items in sections) or 1
    drawn = 0

    def draw_card(p, x, y):
        url = p["url"]
        card_frame(x, y, tile_w, tile_h)
        add_link(c, url, (x, y, x + tile_w, y + tile_h))

        # ---------- product photo ----------
        img_x, img_y = x + pad, y + tile_h - img_area_h + pad
        img_w, img_h = tile_w - 2 * pad, img_area_h - 2 * pad
        if p.get("img_path"):
            try:
                img = Image.open(p["img_path"]).convert("RGB")
                tile = resize_contain(img, img_box_w_px, img_box_h_px)
                c.drawImage(ImageReader(tile), img_x, img_y, img_w, img_h,
                            preserveAspectRatio=False, mask="auto")
            except Exception:
                pass
        else:
            c.setFillColorRGB(0.955, 0.955, 0.965)
            c.roundRect(img_x, img_y, img_w, img_h, 6 * s, stroke=0, fill=1)
            c.setFillColorRGB(0.62, 0.64, 0.68)
            c.setFont("Helvetica", max(5.0, 7.5 * s))
            c.drawCentredString(img_x + img_w / 2, img_y + img_h / 2 - 3,
                                "Image unavailable")

        # ---------- title ----------
        text_top_y = y + tile_h - img_area_h - 9 * s
        title_lines = wrap_title(c, p["title"], tile_w - 14 * s,
                                 TITLE_FONT, title_size, TITLE_MAX_LINES)
        c.setFillColorRGB(0.10, 0.12, 0.16)
        c.setFont(TITLE_FONT, title_size)
        for i, line in enumerate(title_lines):
            c.drawCentredString(x + tile_w / 2, text_top_y - i * title_gap, line)
        add_link(c, url, (x + 6 * s,
                          text_top_y - len(title_lines) * title_gap - 2,
                          x + tile_w - 6 * s, text_top_y + 6 * s))

        # ---------- price badge / crossed-out price / info pill ----------
        final_price = round(p["price"] * (1 - discount / 100.0), 2)

        # PREMIUM compare-at: prefer the store's own "was" price when it
        # beats the price we're showing; otherwise fall back to the
        # pre-discount price (free behaviour).
        strike_val = None
        if use_compare_at and p.get("compare_at") and p["compare_at"] > final_price:
            strike_val = p["compare_at"]
        elif discount > 0:
            strike_val = p["price"]

        badge_y = y + 6 * s
        bx1, by1, bx2, by2, badge_h = draw_badge_center(
            c, x + tile_w / 2, badge_y, format_money(final_price, currency),
            PRICE_FONT, price_size, 7 * s, 4 * s,
            PRICE_BADGE_BG, PRICE_BADGE_TEXT, 8 * s)
        add_link(c, url, (bx1, by1, bx2, by2))

        next_y = badge_y + badge_h + 3 * s
        if strike_val:
            old = format_money(strike_val, currency)
            c.setFont(STRIKE_FONT, strike_size)
            c.setFillColorRGB(*STRIKE_COLOR)
            tw = c.stringWidth(old, STRIKE_FONT, strike_size)
            sx = x + tile_w / 2 - tw / 2
            c.drawString(sx, next_y, old)
            c.setStrokeColorRGB(*STRIKE_COLOR)
            c.setLineWidth(0.9 * s)
            c.line(sx - 1, next_y + strike_size * 0.32,
                   sx + tw + 1, next_y + strike_size * 0.32)
            next_y += strike_size + 4 * s

        # one pill showing whatever info this product has (stock = PREMIUM)
        parts = []
        if p["sku"]:
            parts.append(f"SKU {p['sku']}")
        if show_stock and p["qty"] > 0:
            parts.append(f"{p['qty']} in stock")
        pill_text = "  \u2022  ".join(parts)
        c.setFont(META_FONT, meta_size)
        if (pill_text and
                c.stringWidth(pill_text, META_FONT, meta_size) > tile_w - 20 * s):
            pill_text = parts[0]                   # too wide? keep the SKU
        if pill_text:
            px1, py1, px2, py2, _ = draw_badge_center(
                c, x + tile_w / 2, next_y, pill_text,
                META_FONT, meta_size, 6 * s, 3 * s,
                META_PILL_BG, META_PILL_TEXT, 7 * s, border=META_PILL_BORDER)
            add_link(c, url, (px1, py1, px2, py2))

    # ---- pages: (divider +) product grids, section by section ----------
    for sec_no, (sec_name, sec_items) in enumerate(sections, start=1):
        if sec_name:
            divider(sec_name, sec_no, len(sec_items), page_no)
            page_no += 1

        for start in range(0, len(sec_items), per_page):
            chunk = sec_items[start:start + per_page]
            page_bg()
            header(page_no)
            top_y = H - header_h - margin
            for n, p in enumerate(chunk):
                r, col = n // cols, n % cols
                x = margin + col * (tile_w + gutter)
                y = top_y - (r + 1) * tile_h - r * gutter
                draw_card(p, x, y)
                drawn_now = drawn + n + 1
                if progress:
                    progress(drawn_now / total, None)
            drawn += len(chunk)
            c.showPage()
            page_no += 1

    c.save()
    return out_path


# =========================================================
# The one function app.py calls
# =========================================================
def build_catalogs(items, job_dir, *, business_name, discounts, currency="£",
                   logo_path="", layout="12", section_names=None,
                   use_compare_at=False, show_stock=False, progress=None):
    """
    items         : list from load_products()  (photos not yet downloaded)
    job_dir       : private folder for this customer's job
    discounts     : e.g. [0, 10, 20]  -> makes 3 PDFs
    layout        : "12" (free) or "20"/"30" per page (premium)
    section_names : e.g. ["Seerah", "Quran"] -> divider pages (premium)
    Returns (path_to_give_customer, list_of_pdf_paths)
    """
    def report(pct, msg):
        if progress:
            progress(pct, msg)

    # 1) download all photos once (0% -> 65% of the progress bar)
    cache_dir = os.path.join(job_dir, "image_cache")
    report(0.02, "Reading your product list")
    predownload_images(
        items, cache_dir,
        progress=lambda frac, msg: report(0.02 + frac * 0.63, msg),
    )

    # 2) group into sections once (same grouping for every discount level)
    sections = assign_sections(items, section_names or [])

    # 3) build one PDF per discount level (65% -> 95%)
    safe_name = safe_filename(business_name) or "catalog"
    pdf_paths = []
    n = len(discounts)
    for i, d in enumerate(discounts):
        label = "full_price" if d == 0 else f"{d:g}pct_off"
        nice = "Full Price" if d == 0 else f"{d:g}% Off"
        report(0.65 + (i / n) * 0.30, f"Building catalog {i + 1} of {n} ({nice})")
        out = os.path.join(job_dir, f"{safe_name}_catalog_{label}.pdf")
        build_pdf(items, out, business_name=business_name, discount=d,
                  currency=currency, logo_path=logo_path, layout=layout,
                  sections=sections, use_compare_at=use_compare_at,
                  show_stock=show_stock,
                  progress=lambda frac, _m, base=0.65 + (i / n) * 0.30, span=0.30 / n:
                      report(base + frac * span, None))
        pdf_paths.append(out)

    # 4) one PDF -> hand it straight over; several -> zip them together
    if len(pdf_paths) == 1:
        report(1.0, "Done")
        return pdf_paths[0], pdf_paths

    report(0.97, "Zipping your catalogs")
    zip_path = os.path.join(job_dir, f"{safe_name}_catalogs.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in pdf_paths:
            z.write(p, arcname=os.path.basename(p))
    report(1.0, "Done")
    return zip_path, pdf_paths
