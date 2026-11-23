"""
Collects fruit & vegetable prices for Bengaluru from Zepto, Blinkit and Instamart
and merges them into docs/data/prices.json (the file the web page reads).

Run:  python scraper/scrape.py
Needs: pip install playwright && playwright install chromium
Debug output (screenshots / page text) goes to scraper/debug/ so it can be tuned.

Note: Zepto and Blinkit block many foreign / data-centre networks (e.g. GitHub's US servers).
Run this from a computer on an Indian internet connection if they show as "blocked".
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
INSTAMART_CATS = [("Top deals", "https://instamart.in/c/fresh-fruits/st"),
                  ("Top deals", "https://instamart.in/c/fresh-vegetables/st")]
# Side-menu / tab names on Instamart's fruit & vegetable pages (each is a subcategory)
INSTAMART_TABS = ["Fresh Fruits", "Seasonal Fruits", "Premium Produce", "Exotic Fruits", "Cut Fruits and Juices",
                  "Fresh Vegetables", "Exotic Vegetables", "Leafy and Seasonings", "Cuts and sprouts",
                  "Certified Organics"]
CAT_WORDS = re.compile(r"fruit|vegetable|veg\b|leafy|herb|exotic|salad|sprout|cut\b|fresh", re.I)
BLOCKED = {}

# ---------- parsing helpers ----------
UNIT_WORDS = r"(?:kg|g|gm|gms|ml|ltr|l|litre|pc|pcs|piece|pieces|bunch|pack)"
UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(" + UNIT_WORDS + r")\b", re.I)
UNIT_LINE = re.compile(r"\s*\d+(?:\.\d+)?\s*" + UNIT_WORDS + r"(?:\s*x\s*\d+)?\s*", re.I)
NOISE = re.compile(r"^(add|added|buy|notify|out of stock|sold out|\d+\s*mins?|\d+%\s*off|off|₹?\s*\d+\s*off|sponsored|ad|view|see all)$", re.I)

def norm_unit(u):
    m = UNIT_RE.search(u or "")
    if not m: return None
    q, unit = float(m.group(1)), m.group(2).lower()
    mult = re.search(r"x\s*(\d+)", u or "", re.I)
    if mult: q *= int(mult.group(1))
    if unit == "kg": return q * 1000, "g"
    if unit in ("g", "gm", "gms"): return q, "g"
    if unit in ("l", "ltr", "litre"): return q * 1000, "ml"
    if unit == "ml": return q, "ml"
    return q, "pc"

def clean_unit(u):
    return re.sub(r"\s+", " ", u).strip() if u else u

def parse_card(text):
    lines = [l.strip() for l in text.replace("\r", "").split("\n") if l.strip()]
    lines = [l for l in lines if not NOISE.match(l)]
    # prices: ₹ amounts, else bare numbers on their own lines: "21" then "26" (price, then MRP)
    amounts = [int(float(a)) for a in re.findall(r"₹\s*(\d+(?:\.\d+)?)", text)]
    if not amounts:
        amounts = [int(l) for l in lines if re.fullmatch(r"\d{1,4}", l)]
    if not amounts: return None
    price = amounts[0]
    mrp = amounts[1] if len(amounts) > 1 and amounts[1] > price else None
    unit = None
    for l in lines:  # a line that is only a pack size, e.g. "500 g" or "200 g x 2"
        if UNIT_LINE.fullmatch(l):
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
    n = re.sub(r"\b(fresh|organic|premium|local|farm|daily)\b", "", n)
    n = re.sub(r"[^a-z ]", "", n)
    return re.sub(r"\s+", " ", n).strip()

FRUITS = "apple,banana,orange,pomegranate,papaya,grapes,mango,melon,pineapple,guava,pear,plum,sapota,chikoo,mosambi,sweet lime,strawberr,fruit".split(",")
LEAFY = "spinach,palak,coriander,mint,curry leaves,methi,fenugreek,dill,amaranth,basil,parsley,spring onion,leaves,leafy,bunch".split(",")

def sub_from_label(label):
    l = (label or "").lower()
    if not l or "top deals" in l: return None
    if "exotic" in l: return "Exotics"
    if "leafy" in l or "herb" in l: return "Leafy & Herbs"
    if "cut" in l and "fruit" in l: return "Cut Fruits & Juices"
    if "sprout" in l or "cuts" in l: return "Cuts & Sprouts"
    if "organic" in l: return "Organic"
    if "premium" in l: return "Premium Produce"
    if "fruit" in l: return "Fruits"
    if "veg" in l: return "Vegetables"
    return None

def classify(name, label):
    s = sub_from_label(label)
    if s: return s
    n = name.lower()
    if any(w in n for w in ("avocado", "dragon", "zucchini", "broccoli", "lettuce", "asparagus", "celery", "leek", "iceberg", "baby corn", "blueberr", "kiwi")): return "Exotics"
    if any(w in n for w in LEAFY): return "Leafy & Herbs"
    if any(re.search(rf"\b{w}", n) for w in FRUITS): return "Fruits"
    return "Vegetables"

# ---------- browser helpers ----------
JS_CARDS = """
(hrefPart) => {
  const out = [], seen = new Set();
  const add = (el, href) => { const t = (el.innerText || "").trim(); if (t.length > 8 && t.length < 500 && !seen.has(t)) { seen.add(t); out.push({text: t, href: href || ""}); } };
  document.querySelectorAll('a[href*="' + hrefPart + '"]').forEach(a => {
    let el = a;
    for (let i = 0; i < 4 && el && !/[0-9]/.test(el.innerText || ""); i++) el = el.parentElement;
    if (el) add(el, a.getAttribute("href"));
  });
  if (out.length < 5) {   // no product links: find the smallest blocks that look like a product tile
    const unitRe = /^\\s*\\d+(\\.\\d+)?\\s*(kg|g|gm|gms|ml|ltr|l|litre|pc|pcs|piece|pieces|bunch|pack)\\b/i;
    const isCard = d => {
      const t = d.innerText || "";
      if (t.length < 12 || t.length > 450 || !d.querySelector("img")) return false;
      const lines = t.split("\\n").map(s => s.trim()).filter(Boolean);
      return lines.some(l => unitRe.test(l)) && (/₹\\s*\\d+/.test(t) || lines.some(l => /^\\d{1,4}$/.test(l)));
    };
    const cands = [...document.querySelectorAll("div")].filter(isCard);
    cands.forEach(d => {
      let hasChild = false;
      for (const c of cands) { if (c !== d && d.contains(c)) { hasChild = true; break; } }
      if (!hasChild) add(d, "");
    });
  }
  return out;
}
"""

def is_blocked(page):
    try:
        t = page.inner_text("body")[:800].lower()
    except Exception:
        return False
    return any(s in t for s in ("403 error", "access denied", "you have been blocked", "request blocked", "verify you are human"))

LOC_PLACEHOLDER = re.compile(r"search (for )?(an? )?(delivery )?(area|location|address)|pincode|area or address|enter.*location", re.I)

def set_location(page, name):
    """Best-effort: type the pincode in whichever location box the site shows."""
    try:
        box = page.get_by_placeholder(LOC_PLACEHOLDER).first
        if box.count() == 0:
            for t in ("Add your location", "Select Location", "Select location", "Detect my location", "Delivery in"):
                loc = page.get_by_text(t, exact=False).first
                if loc.count():
                    try:
                        loc.click(timeout=3000, force=True); page.wait_for_timeout(1500)
                    except Exception:
                        pass
                    break
            box = page.get_by_placeholder(LOC_PLACEHOLDER).first
        if box.count() == 0:
            print(f"[{name}] no location box found"); return
        box.fill(PINCODE); page.wait_for_timeout(3000)
        clicked = False
        for sel in ('[data-testid*="suggest" i]', '[data-testid*="address" i]', '[role="option"]'):
            el = page.locator(sel).first
            if el.count():
                try:
                    el.click(timeout=3000); clicked = True; break
                except Exception:
                    pass
        if not clicked:
            page.keyboard.press("ArrowDown"); page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
        for t in ("Confirm", "Confirm & Proceed", "Confirm location", "Proceed", "Continue"):
            b = page.get_by_role("button", name=re.compile(t, re.I)).first
            if b.count():
                try:
                    b.click(timeout=3000)
                except Exception:
                    pass
                break
        page.wait_for_timeout(3000)
        txt = page.inner_text("body")[:4000]
        print(f"[{name}] location text found on page:", bool(re.search(r"bengaluru|bangalore|" + PINCODE, txt, re.I)))
    except Exception as e:
        print(f"[{name}] location step: {e}")
    try:
        page.screenshot(path=os.path.join(DEBUG, f"{name}_after_location.png"))
    except Exception:
        pass

def collect(page, cfg, store, label):
    for c in page.evaluate(JS_CARDS, cfg["product_href"]):
        t = c["text"]
        if t in store:
            if store[t]["category"] == "Top deals" and label != "Top deals":
                store[t]["category"] = label   # a specific subcategory beats the generic list
            continue
        p = parse_card(t)
        if p:
            p["category"] = label; p["url"] = c["href"]; store[t] = p

def scroll_collect(page, cfg, store, label, max_rounds=25):
    last, stale = -1, 0
    for _ in range(max_rounds):
        collect(page, cfg, store, label)
        page.mouse.move(800, 600)
        page.mouse.wheel(0, 2200); page.wait_for_timeout(900)
        if len(store) == last:
            stale += 1
            if stale >= 3: break
        else:
            stale, last = 0, len(store)
    collect(page, cfg, store, label)

def click_tab(page, label):
    try:
        loc = page.get_by_text(label, exact=True).first
        if loc.count() == 0: return False
        loc.click(timeout=4000); page.wait_for_timeout(2500)
        return True
    except Exception as e:
        print(f"  tab '{label}' not clicked: {str(e)[:80]}")
        return False

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
    store = {}
    try:
        page.goto(cfg["home"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        if is_blocked(page):
            BLOCKED[name] = True
            print(f"[{name}] BLOCKED: the site refused this network (try from an Indian internet connection).")
            page.screenshot(path=os.path.join(DEBUG, f"{name}_blocked.png"))
            return []
        set_location(page, name)
        cats = INSTAMART_CATS if name == "instamart" else find_category_links(page, cfg)
        print(f"[{name}] categories: {[c[0] for c in cats]}")
        page.screenshot(path=os.path.join(DEBUG, f"{name}_home.png"))
        done_tabs = set()
        for label, url in cats:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3500)
                n0 = len(store)
                scroll_collect(page, cfg, store, label)
                print(f"[{name}] {label} ({url}): +{len(store) - n0} products")
                if name == "instamart":
                    for tab in INSTAMART_TABS:
                        if tab in done_tabs: continue
                        if click_tab(page, tab):
                            done_tabs.add(tab)
                            n1 = len(store)
                            scroll_collect(page, cfg, store, tab)
                            print(f"[{name}]   tab {tab}: +{len(store) - n1} products")
            except Exception as e:
                print(f"[{name}] category {label} failed: {e}")
        if not store:
            page.screenshot(path=os.path.join(DEBUG, f"{name}_empty.png"))
            open(os.path.join(DEBUG, f"{name}_empty.txt"), "w").write(page.inner_text("body")[:6000])
    except Exception:
        traceback.print_exc()
    finally:
        page.close()
    return list(store.values())

# ---------- merge ----------
def to_entry(p):
    e = {"price": p["price"], "unit": clean_unit(p["unit"])}
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
                                      permissions=["geolocation"], viewport={"width": 1366, "height": 900},
                                      extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"})
            results[name] = scrape_platform(ctx, name)
            ctx.close()
        browser.close()

    merged = {}
    for name in PLATFORMS:
        got = results[name]
        status[name] = {"ok": len(got) > 0, "count": len(got),
                        "scraped_at": now if got else prev_plat.get(name, {}).get("scraped_at")}
        if BLOCKED.get(name): status[name]["blocked"] = True
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
