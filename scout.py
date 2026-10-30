#!/usr/bin/env python3
"""
Daily job scout for civil engineering + international development roles.

Sources (all optional, each skipped cleanly if not configured):
  1. ReliefWeb API      - needs RELIEFWEB_APPNAME (free to register)
  2. RSS / Atom feeds   - listed in config.json
  3. Claude web agent   - needs ANTHROPIC_API_KEY; searches the open web

Output:
  data/jobs.json   persistent store (first_seen / last_seen per job)
  docs/index.html  searchable, sortable table
  docs/jobs.csv    same data for Excel / Sheets

Usage:
  python scout.py          normal daily run
  python scout.py --demo   write docs/demo.html from sample data (no network, no store changes)
"""
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STORE = ROOT / "data" / "jobs.json"
DOCS = ROOT / "docs"
TODAY = dt.date.today().isoformat()
HEADERS = {"User-Agent": "job-scout/1.0 (personal job alert)"}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- helpers
def norm_url(url):
    try:
        p = urlparse(url.strip())
        q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid"))]
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", urlencode(q), ""))
    except Exception:
        return url.strip()


def job_id(job):
    key = "|".join(re.sub(r"\W+", " ", (job.get(k) or "").lower()).strip() for k in ("title", "organization", "location"))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def to_date(value):
    """Return YYYY-MM-DD or None from ISO strings / RFC 822 dates."""
    if not value:
        return None
    value = str(value).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    if m:
        return m.group(1)
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except Exception:
        return None


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def has_any(text, terms):
    t = text.lower()
    return any(term in t for term in terms)


def is_relevant(job):
    text = f"{job.get('title', '')} {job.get('summary', '')}"
    if has_any(job.get("title", ""), CFG["exclude_terms"]):
        return False
    return has_any(text, CFG["engineering_terms"]) or has_any(text, CFG["development_terms"])


def categorize(job):
    text = f"{job.get('title', '')} {job.get('summary', '')}"
    eng = has_any(text, CFG["engineering_terms"])
    dev = has_any(text, CFG["development_terms"])
    if eng and dev:
        return "Both"
    if eng:
        return "Civil engineering"
    if dev:
        return "International development"
    return "Other"


EAST_AFRICA = ["uganda", "tanzania", "rwanda", "ethiopia", "burundi", "south sudan", "somalia",
               "djibouti", "eritrea", "east africa"]
AFRICA = ["africa", "nigeria", "ghana", "south africa", "egypt", "zambia", "malawi", "mozambique",
          "zimbabwe", "senegal", "ivoire", "cameroon", "congo", "drc", "sudan", "mali", "niger",
          "chad", "morocco", "tunisia", "namibia", "botswana", "madagascar", "sierra leone", "liberia"]


def region_of(location):
    loc = (location or "").lower()
    if any(k in loc for k in ("kenya", "nairobi", "mombasa", "kisumu", "nakuru", "eldoret")):
        return "Kenya"
    if any(k in loc for k in EAST_AFRICA):
        return "East Africa"
    if any(k in loc for k in ("remote", "home-based", "home based", "anywhere", "virtual")):
        return "Remote"
    if any(k in loc for k in AFRICA):
        return "Africa"
    return "Global / other"


# ---------------------------------------------------------------- source 1: ReliefWeb
def collect_reliefweb():
    cfg = CFG.get("reliefweb", {})
    app = os.getenv("RELIEFWEB_APPNAME")
    if not cfg.get("enabled") or not app:
        log("ReliefWeb: skipped (no RELIEFWEB_APPNAME)")
        return []
    terms = " OR ".join(f'"{k}"' if " " in k else k for k in cfg["keywords"])
    body = {
        "limit": 200,
        "sort": ["date.created:desc"],
        "query": {"value": terms, "fields": ["title"], "operator": "OR"},
        "fields": {"include": ["title", "url", "source.name", "country.name", "date.closing",
                               "date.created", "city.name"]},
    }
    try:
        r = requests.post(f"https://api.reliefweb.int/v2/jobs?appname={app}", json=body, headers=HEADERS, timeout=60)
        r.raise_for_status()
        out = []
        for item in r.json().get("data", []):
            f = item.get("fields", {})
            places = [c["name"] for c in f.get("city", [])] + [c["name"] for c in f.get("country", [])]
            out.append({
                "title": f.get("title", ""),
                "organization": (f.get("source") or [{}])[0].get("name", ""),
                "location": ", ".join(places),
                "url": f.get("url", ""),
                "deadline": to_date((f.get("date") or {}).get("closing")),
                "posted": to_date((f.get("date") or {}).get("created")),
                "summary": "",
                "source": "ReliefWeb",
            })
        log(f"ReliefWeb: {len(out)} results")
        return out
    except Exception as e:
        log(f"ReliefWeb: failed ({e})")
        return []


# ---------------------------------------------------------------- source 2: RSS / Atom
def _text(el, tag):
    found = el.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def collect_rss():
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    for feed in CFG.get("rss_feeds", []):
        if not feed.get("enabled", True):
            continue
        try:
            r = requests.get(feed["url"], headers=HEADERS, timeout=45)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            count = 0
            for it in root.iter("item"):
                out.append({
                    "title": _text(it, "title"), "organization": "", "location": "",
                    "url": _text(it, "link"), "deadline": None,
                    "posted": to_date(_text(it, "pubDate")),
                    "summary": strip_html(_text(it, "description"))[:200], "source": feed["name"],
                })
                count += 1
            for it in root.iter(atom + "entry"):
                link = it.find(atom + "link")
                out.append({
                    "title": _text(it, atom + "title"), "organization": "", "location": "",
                    "url": link.get("href", "") if link is not None else "", "deadline": None,
                    "posted": to_date(_text(it, atom + "updated")),
                    "summary": strip_html(_text(it, atom + "summary"))[:200], "source": feed["name"],
                })
                count += 1
            log(f"RSS {feed['name']}: {count} items")
        except Exception as e:
            log(f"RSS {feed.get('name')}: failed ({e})")
    return out


# ---------------------------------------------------------------- source 3: Claude web agent
def extract_json_array(text):
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def collect_agent():
    cfg = CFG.get("agent", {})
    if not cfg.get("enabled") or not os.getenv("ANTHROPIC_API_KEY"):
        log("Claude agent: skipped (no ANTHROPIC_API_KEY)")
        return []
    import anthropic

    client = anthropic.Anthropic()
    model = os.getenv("CLAUDE_MODEL") or CFG["model"]
    out = []
    for topic in cfg["searches"]:
        prompt = (
            f"Today is {TODAY}. Search the web for job openings that are open right now.\n\n"
            f"Topic: {topic}\n\n"
            f"Candidate context: {CFG['profile']}\n\n"
            "Rules:\n"
            "- Only include postings you actually saw in search results, with a direct URL to the posting "
            "or the employer's page for it. Never invent or guess a URL, organisation or deadline.\n"
            "- Skip anything whose deadline has already passed or that looks closed or expired.\n"
            f"- Return at most {cfg['max_results_per_search']} postings, best matches first.\n"
            "- Reply with ONLY a JSON array, no prose and no code fences. Each element has the keys: "
            "title, organization, location, url, deadline (YYYY-MM-DD or null), posted (YYYY-MM-DD or null), "
            "summary (max 20 words).\n"
            "- If you find nothing, reply with []."
        )
        tools = [{"type": "web_search_20250305", "name": "web_search",
                  "max_uses": cfg["max_web_searches_per_topic"]}]
        messages = [{"role": "user", "content": prompt}]
        try:
            resp = None
            for _ in range(6):  # server-side tool turns can pause; continue them
                resp = client.messages.create(model=model, max_tokens=4000, tools=tools, messages=messages)
                if resp.stop_reason != "pause_turn":
                    break
                messages.append({"role": "assistant", "content": resp.content})
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            items = extract_json_array(text)
            for j in items:
                if isinstance(j, dict):
                    j["source"] = "Web agent"
                    out.append(j)
            log(f"Claude agent: {len(items)} results for '{topic[:50]}...'")
        except Exception as e:
            log(f"Claude agent: failed on '{topic[:40]}...' ({e})")
    return out


# ---------------------------------------------------------------- store
def load_store():
    if STORE.exists():
        return json.loads(STORE.read_text(encoding="utf-8"))
    return {}


def clean(job):
    j = {k: str(job.get(k) or "").strip() for k in ("title", "organization", "location", "url", "summary", "source")}
    j["deadline"] = to_date(job.get("deadline"))
    j["posted"] = to_date(job.get("posted"))
    return j


def merge(store, found):
    url_index = {norm_url(v["url"]): k for k, v in store.items() if v.get("url")}
    new = 0
    for raw in found:
        j = clean(raw)
        if not j["title"] or not j["url"].lower().startswith(("http://", "https://")):
            continue
        if j["deadline"] and j["deadline"] < TODAY:
            continue
        if not is_relevant(j):
            continue
        key = url_index.get(norm_url(j["url"])) or job_id(j)
        if key in store:
            old = store[key]
            old["last_seen"] = TODAY
            old["deadline"] = j["deadline"] or old.get("deadline")
            old["organization"] = old.get("organization") or j["organization"]
            old["location"] = old.get("location") or j["location"]
            sources = set(old["source"].split(", ")) | {j["source"]}
            old["source"] = ", ".join(sorted(sources))
        else:
            j.update(first_seen=TODAY, last_seen=TODAY)
            store[key] = j
            url_index[norm_url(j["url"])] = key
            new += 1
    return new


def prune(store):
    stale = (dt.date.today() - dt.timedelta(days=CFG["stale_days"])).isoformat()
    for k in list(store):
        j = store[k]
        if (j.get("deadline") and j["deadline"] < TODAY) or j.get("last_seen", TODAY) < stale:
            del store[k]


def rows(store):
    out = []
    for k, j in store.items():
        r = dict(j)
        r["id"] = k
        r["category"] = categorize(j)
        r["region"] = region_of(j.get("location"))
        r["is_new"] = j["first_seen"] == TODAY
        out.append(r)
    out.sort(key=lambda r: (not r["is_new"], r["deadline"] or "9999", r["title"].lower()))
    return out


# ---------------------------------------------------------------- output
def write_csv(data):
    DOCS.mkdir(exist_ok=True)
    cols = ["title", "organization", "location", "region", "category", "deadline", "posted",
            "first_seen", "source", "url"]
    with open(DOCS / "jobs.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(data)


def write_html(data, filename="index.html"):
    DOCS.mkdir(exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = (ROOT / "template.html").read_text(encoding="utf-8")
    page = (page.replace("__DATA__", payload)
                .replace("__UPDATED__", TODAY)
                .replace("__NEW_COUNT__", str(sum(1 for r in data if r["is_new"]))))
    (DOCS / filename).write_text(page, encoding="utf-8")


DEMO = [
    {"title": "Graduate Structural Engineer", "organization": "Example Consulting Engineers", "location": "Nairobi, Kenya",
     "url": "https://example.com/jobs/1", "deadline": "2026-11-20", "posted": TODAY, "summary": "Structural design of multi-storey buildings",
     "source": "Web agent"},
    {"title": "Climate Resilience Programme Officer", "organization": "Example UN Agency", "location": "Nairobi, Kenya",
     "url": "https://example.com/jobs/2", "deadline": "2026-11-12", "posted": TODAY, "summary": "Climate adaptation programme", "source": "ReliefWeb"},
    {"title": "WASH Infrastructure Engineer", "organization": "Example NGO", "location": "Kakuma, Kenya",
     "url": "https://example.com/jobs/3", "deadline": "2026-12-01", "posted": "2026-10-25", "summary": "Water and sanitation infrastructure", "source": "UNjobs"},
    {"title": "Resilient Infrastructure Specialist (Remote)", "organization": "Example Dev Bank", "location": "Remote",
     "url": "https://example.com/jobs/4", "deadline": None, "posted": "2026-10-20", "summary": "Climate-resilient infrastructure advisory", "source": "Web agent"},
]


def main():
    if "--demo" in sys.argv:
        store = {}
        merge(store, DEMO)
        for j in store.values():
            if j["title"].startswith(("WASH", "Resilient")):
                j["first_seen"] = "2026-10-25"
        write_html(rows(store), "demo.html")
        log("Wrote docs/demo.html")
        return

    store = load_store()
    found = collect_reliefweb() + collect_rss() + collect_agent()
    new = merge(store, found)
    prune(store)
    STORE.parent.mkdir(exist_ok=True)
    STORE.write_text(json.dumps(store, indent=1, ensure_ascii=False), encoding="utf-8")
    data = rows(store)
    write_html(data)
    write_csv(data)
    log(f"Done: {len(store)} open jobs tracked, {new} new today")


if __name__ == "__main__":
    main()
