scraper/"""
Collects fruit & vegetable prices for Bengaluru from Zepto, Blinkit and Instamart
and merges them into docs/data/prices.json (the file the web page reads).

Run:  python scraper/scrape.py
Needs: pip install playwright && playwright install chromium
Debug output (screenshots / page text) goes to scraper/debug/ so selectors can be tuned.
"""
import json, os, re, sys, datetime, traceback
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "data", "prices.json")
DEBUG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")
os.makedirs(DEBUG, exist_ok=True)

PINCODE = os.environ.get("PINCODE", "560034")  # Koramangala, Bengaluru. Any Bengaluru pincode works.
LAT, LNG = 12.9352, 77.6245
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PLATFORMS = {
    "zepto":     {"home": "https://www.zepto.com/",   "product_href": "/pn/",  "cat_href": "/cn/"},
    "blinkit":   {"home": "https://blinkit.com/",     "product_href": "/prn/", "cat_href": "/cn/"},
    "instamart": {"home": "https://instamart.in/",    "product_href": "/p/",   "cat_href": "/c/"},
}
# Instamart category pages are known; the other two are discovered from the homepage after location is set.
INSTAMART_CATS = [("Fresh Vegetables", "https://instamart.in/c/fresh-vegetables/st"),
                  ("Fresh Fruits", "https://instamart.in/c/fresh-fruits/st")]
CAT_WORDS = re.compile(r"fruit|vegetable|veg\b|leafy|herb|exotic|salad|sprout|cut\b|fresh", re.I)

# ---------- parsing helpers ----------
UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g|gm|gms|ml|ltr|l|litre|pc|pcs|piece|pieces|bunch|pack)\b", re.I)
NOISE = re.compile(r"^(add|added|buy|notify|out of stock|sold out|\d+\s*mins?|\d+%\s*off|off|₹?\s*\d+\s*off|sponsored|ad|view|see all)$", re.I)

def norm_unit(u):
    m = UNIT_RE.search(u or "")
    if not m: return None
    q, unit = float(m.group(1)), m.group(2).lower()
    if unit in ("kg",): return q * 1000, "g"
    if unit in ("g", "gm", "gms"): return q, "g"
    if unit in ("l", "ltr", "litre"): return q * 1000, "ml"
    if unit == "ml": return q, "ml"
    return q, "pc"

def clean_unit(u):
    m = UNIT_RE.search(u or "")
    if not m: return u
    q = m.group(1).rstrip("0").rstrip(".") if "." in m.group(1) else m.group(1)
    return f"{q} {m.group(2).lower()}"

def parse_card(text):
    lines = [l.strip() for l in text.replace("\r", "").split("\n") if l.strip()]
    lines = [l for l in lines if not NOISE.match(l)]
    # prices: ₹ amounts, else trailing bare integers
    amounts = [int(float(a)) for a in re.findall(r"₹\s*(\d+(?:\.\d+)?)", text)]
    if not amounts:  # sites that show bare numbers on their own lines: "90" then "180" (price, then MRP)
        amounts = [int(l) for l in lines if re.fullmatch(r"\d{1,4}", l)]
    if not amounts: return None
    price = amounts[0]
    mrp = amounts[1] if len(amounts) > 1 and amounts[1] > price else None
    unit = None
    for l in lines:  # a line that is only a pack size, e.g. "500 g"
        if re.fullmatch(r"\s*\d+(?:\.\d+)?\s*(kg|g|gm|gms|ml|ltr|l|litre|pc|pcs|piece|pieces|bunch|pack)\s*", l, re.I):
            unit = l; break
    name = None
    for l in lines:
        if re.search(r"[A-Za-z]{3,}", l) and "₹" not in l and l != unit and not re.search(r"% ?off", l, re.I):
            name = l; break
    if not name: return None
    if unit is None:
        m = UNIT_RE.search(name)
        unit = m.group(0) if m else None
    return {"name": name, "unit": unit, "price": price, "mrp": mrp}

def clean_name(n):
    n = re.sub(r"\(.*?\)", "", n)
    n = UNIT_RE.sub("", n)
    return re.sub(r"\s+", " ", n).strip(" -,")

def key(name):
    n = clean_name(name).lower()
    n = re.sub(r"\b(fresh|organic|premium|local|farm)\b", "", n)
    n = re.sub(r"[^a-z ]", "", n)
    return re.sub(r"\s+", " ", n).strip()

FRUITS = "apple,banana,orange,pomegranate,papaya,grapes,mango,melon,pineapple,guava,pear,plum,sapota,chikoo,mosambi,sweet lime,strawberr,fruit".split(",")
LEAFY = "spinach,palak,coriander,mint,curry leaves,methi,fenugreek,dill,amaranth,basil,parsley,spring onion,leaves,leafy,bunch".split(",")

def classify(name, fallback):
    n = name.lower()
    if any(w in n for w in ("avocado", "dragon", "zucchini", "broccoli", "lettuce", "asparagus", "celery", "leek", "iceberg", "baby corn", "blueberr", "kiwi")): return "Exotics"
    if any(w in n for w in LEAFY): return "Leafy & Herbs"
    if any(re.search(rf"\b{w}", n) for w in FRUITS): return "Fruits"
    if fallback and re.search(r"fruit", fallback, re.I): return "Fruits"
    return "Vegetables"

# ---------- browser helpers ----------
JS_CARDS = """
(hrefPart) => {
  const out = [], seen = new Set();
  const add = (el, href) => { const t = el.innerText || ""; if (t.length > 8 && t.length < 400 && !seen.has(t)) { seen.add(t); out.push({text: t, href: href || ""}); } };
  document.querySelectorAll('a[href*="' + hrefPart + '"]').forEach(a => {
    let el = a;
    for (let i = 0; i < 4 && el && !/[0-9]/.test(el.innerText || ""); i++) el = el.parentElement;
    if (el) add(el, a.getAttribute("href"));
  });
  if (out.length < 5) {   // fallback: smallest elements that look like a product tile (image + price)
    document.querySelectorAll("div").forEach(d => {
      const t = d.innerText || "";
      if (/₹\\s*\\d+/.test(t) && t.length < 250 && d.querySelector("img") && !d.querySelector("div div div div div")) add(d, "");
    });
  }
  return out;
}
"""

def set_location(page, name):
    """Best-effort: type the pincode in whichever location box the site shows."""
    try:
        for pat in (r"search delivery location|pincode|enter.*location|search for area", ):
            box = page.get_by_placeholder(re.compile(pat, re.I)).first
            if box.count() == 0:
                for t in ("Select Location", "Select location", "Delivery", "Bangalore", "Bengaluru", "Detect my location"):
                    loc = page.get_by_text(t, exact=False).first
                    if loc.count():
                        loc.click(timeout=3000); page.wait_for_timeout(1500); break
                box = page.get_by_placeholder(re.compile(pat, re.I)).first
            if box.count():
                box.fill(PINCODE); page.wait_for_timeout(2500)
                page.keyboard.press("ArrowDown"); page.keyboard.press("Enter")
                page.wait_for_timeout(2500)
                for t in ("Confirm", "Confirm & Proceed", "Confirm location", "Proceed"):
                    b = page.get_by_role("button", name=re.compile(t, re.I)).first
                    if b.count():
                        b.click(timeout=3000); break
                page.wait_for_timeout(2500)
    except Exception as e:
        print(f"[{name}] location step: {e}")

def scroll_all(page, rounds=12):
    last = 0
    for _ in range(rounds):
        page.mouse.wheel(0, 2500); page.wait_for_timeout(900)
        h = page.evaluate("document.body.scrollHeight")
        if h == last: break
        last = h

def find_category_links(page, cfg):
    links = page.evaluate("""(h) => [...document.querySelectorAll('a[href*="'+h+'"]')].map(a => ({href: a.href, text: (a.innerText||a.getAttribute('aria-label')||'').trim()}))""", cfg["cat_href"])
    seen, out = set(), []
    for l in links:
        if l["href"] in seen: continue
        if CAT_WORDS.search(l["text"]) or CAT_WORDS.search(l["href"]):
            seen.add(l["href"]); out.append((l["text"] or "Fresh", l["href"]))
    return out[:15]

def scrape_platform(ctx, name):
    cfg = PLATFORMS[name]
    page = ctx.new_page()
    items = []
    try:
        page.goto(cfg["home"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        set_location(page, name)
        cats = INSTAMART_CATS if name == "instamart" else find_category_links(page, cfg)
        print(f"[{name}] categories: {[c[0] for c in cats]}")
        page.screenshot(path=os.path.join(DEBUG, f"{name}_home.png"))
        for label, url in cats:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                scroll_all(page)
                cards = page.evaluate(JS_CARDS, cfg["product_href"])
                n0 = len(items)
                for c in cards:
                    p = parse_card(c["text"])
                    if p: p["category"] = label; p["url"] = c["href"]; items.append(p)
                print(f"[{name}] {label}: {len(items) - n0} products")
            except Exception as e:
                print(f"[{name}] category {label} failed: {e}")
        if not items:
            page.screenshot(path=os.path.join(DEBUG, f"{name}_empty.png"))
            open(os.path.join(DEBUG, f"{name}_empty.txt"), "w").write(page.inner_text("body")[:5000])
    except Exception:
        traceback.print_exc()
    finally:
        page.close()
    return items

# ---------- merge ----------
def to_entry(p):
    e = {"price": p["price"], "unit": clean_unit(p["unit"]) if p["unit"] else None}
    nu = norm_unit(p["unit"])
    if nu and nu[1] in ("g", "ml") and nu[0] > 0:
        e["unit_price"] = round(p["price"] * 1000 / nu[0], 1)
        e["unit_label"] = "kg" if nu[1] == "g" else "l"
    if p.get("mrp"): e["mrp"] = p["mrp"]
    return e

def main():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    prev = {}
    if os.path.exists(OUT):
        prev = json.load(open(OUT))
    prev_items = {} if prev.get("sample") else {key(i["name"]): i for i in prev.get("items", [])}
    prev_plat = {} if prev.get("sample") else prev.get("platforms", {})

    results, status = {}, {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        for name in PLATFORMS:
            ctx = browser.new_context(user_agent=UA, locale="en-IN", timezone_id="Asia/Kolkata",
                                      geolocation={"latitude": LAT, "longitude": LNG},
                                      permissions=["geolocation"], viewport={"width": 1366, "height": 900})
            results[name] = scrape_platform(ctx, name)
            ctx.close()
        browser.close()

    merged = {}
    for name in PLATFORMS:
        got = results[name]
        status[name] = {"ok": len(got) > 0, "count": len(got),
                        "scraped_at": now if got else prev_plat.get(name, {}).get("scraped_at")}
        for p in got:
            k = key(p["name"])
            if not k: continue
            row = merged.setdefault(k, {"name": clean_name(p["name"]).title(), "cat": p["category"]})
            row.setdefault(name, to_entry(p))
    # keep old prices for a platform that failed this run
    for name in PLATFORMS:
        if not status[name]["ok"]:
            for k, it in prev_items.items():
                if it.get(name):
                    merged.setdefault(k, {"name": it["name"], "cat": it.get("subcategory", "")})[name] = it[name]

    items = []
    for k, row in merged.items():
        it = {"name": row["name"], "subcategory": classify(row["name"], row.get("cat")),
              **{n: row.get(n) for n in PLATFORMS}}
        items.append(it)
    items.sort(key=lambda i: i["name"].lower())

    if not items:
        print("Nothing collected on any platform; keeping previous file.")
        sys.exit(1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"sample": False, "updated_at": now, "pincode": PINCODE, "platforms": status, "items": items},
              open(OUT, "w"), ensure_ascii=False, indent=1)
    print(f"Wrote {len(items)} items. Status: {status}")

if __name__ == "__main__":
    main()
