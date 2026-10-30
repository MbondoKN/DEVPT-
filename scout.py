#!/usr/bin/env python3
"""
Daily job scout: civil engineering + international development, entry to mid level.

Sources (all free; each is skipped cleanly if not configured):
  1. UN Careers RSS     - all UN Secretariat departments and offices (no description text in feed)
  2. ReliefWeb API      - UN agencies and NGOs worldwide, full job text. Needs RELIEFWEB_APPNAME (free)
  3. Remotive API       - remote jobs, no key
  4. Claude web agent   - OPTIONAL and paid; skipped unless ANTHROPIC_API_KEY is set

Filters (see config.json "filters"): entry to mid level only, no language requirements beyond
English/Kiswahili, no visa-restricted or nationals-only posts, degree must accept civil engineering
or a development-studies type field.

Output: data/jobs.json, docs/index.html, docs/jobs.csv
Usage:  python scout.py   |   python scout.py --demo
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
F = CFG["filters"]
STORE = ROOT / "data" / "jobs.json"
STORE_VERSION = 2
DOCS = ROOT / "docs"
TODAY = dt.date.today().isoformat()
HEADERS = {"User-Agent": "job-scout/2.0 (personal job alert)"}
REJECTED, ACCEPTED = {}, {}


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
    """YYYY-MM-DD from ISO, US (MM/DD/YYYY) or RFC 822 dates; None if unparseable."""
    if not value:
        return None
    value = str(value).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    if m:
        return m.group(1)
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", value)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except Exception:
        return None


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def has_any(text, terms):
    t = (text or "").lower()
    return any(term in t for term in terms)


# ---------------------------------------------------------------- filter rules
SENIOR_RE = re.compile(r"(?<!\w)(" + "|".join(re.escape(t) for t in F["senior_title_terms"]) + r")(?!\w)")

OTHER_LANGS = ["french", "spanish", "arabic", "portuguese", "russian", "chinese", "mandarin", "german",
               "italian", "hindi", "urdu", "bengali", "hausa", "amharic", "somali", "turkish", "farsi", "persian",
               "dari", "pashto", "bahasa", "indonesian", "nepali", "burmese", "thai", "vietnamese", "korean",
               "japanese", "ukrainian", "tigrinya", "kinyarwanda", "lingala", "kirundi", "swedish", "dutch",
               "romanian", "khmer", "lao", "tagalog", "creole", "haitian", "pidgin"]
LANG_RE = re.compile(r"\b(" + "|".join(OTHER_LANGS) + r")\b")
SOFT = ("asset", "advantage", "desirable", "preferred", "a plus", "considered", "beneficial", "welcome", "nice to have")
HARD = ("fluen", "proficien", "required", "mandatory", "essential", "must", "knowledge of", "command of",
        "speak", "written and spoken", "working knowledge", "oral")

VISA_RES = [re.compile(p) for p in [
    r"no visa sponsorship", r"visa sponsorship (?:is )?(?:not|unavailable)", r"unable to (?:provide )?(?:visa )?sponsor",
    r"(?:do|does|will|can) not (?:provide |offer |support )?(?:visa |work permit )?sponsor",
    r"not (?:able|in a position|willing) to sponsor", r"without (?:the need for )?(?:visa )?sponsorship",
    r"sponsorship (?:is )?not (?:available|provided|offered)",
    r"must (?:already )?(?:have|hold|possess) (?:the )?(?:legal )?(?:right|authori[sz]ation|work (?:permit|authori[sz]ation|visa))",
    r"must be (?:legally )?(?:authori[sz]ed|eligible|entitled|permitted) to work",
    r"right to work in", r"existing work (?:permit|authori[sz]ation)",
    r"(?:responsible for|responsibility to) (?:obtain|securing|obtaining) (?:their|your|own)? ?(?:visa|work permit)",
    r"internal candidates only", r"for internal candidates", r"locally recruited", r"local candidates only",
]]
NATIONALS_ONLY_RE = re.compile(r"([^.;\n]{0,120}?)\b(?:nationals?|citizens?)(?: or permanent residents?)?\s+only")
NON_ENGLISH_RE = re.compile(r"[áéíóúñçãõàèùâêôœ]|\b(para|del|los|las|une|pour|des|chargé|consultoría|asistente)\b")
OTHER_ENG_RE = re.compile(r"\b(electrical|mechanical|software|computer|chemical|mining|petroleum|aeronautical|biomedical|electronic|industrial|telecommunication)s?\s+engineering")


def is_home(location, title=""):
    return has_any(f"{location} {title}", F["home_keywords"])


def language_blocked(text):
    """Return the first non-English/Kiswahili language that is *required* (not optional), else None."""
    for s in re.split(r"[.;\n•]+", text.lower()):
        m = LANG_RE.search(s)
        if not m:
            continue
        if re.search(r"english\s+(?:or|and/or)\s+\w+|\w+\s+(?:or|and/or)\s+english", s):
            continue  # either language is acceptable
        if any(w in s for w in SOFT):
            continue
        if any(w in s for w in HARD):
            return m.group(1)
    return None


def visa_blocked(text):
    low = text.lower()
    if any(p.search(low) for p in VISA_RES):
        return True
    for m in NATIONALS_ONLY_RE.finditer(low):
        clause = m.group(1)
        # "Kenyan or Ugandan nationals only", "East African nationals only", "all nationalities" stay open
        if not any(term in clause for term in F["open_nationality_terms"]):
            return True
    return False


def years_required(text):
    nums = [int(n) for n in re.findall(r"(\d{1,2})\s*\+?\s*(?:years?|yrs)\b[^.]{0,60}?experience", text.lower())]
    return min(nums) if nums else None


def degree_fit(text):
    """'Civil engineering' / 'Engineering' / 'Development' if the stated degree fields accept you,
    'Not stated' if no degree is mentioned, None if a degree in some other field is required."""
    low = text.lower()
    if not re.search(r"degree|bachelor|master['’]?s|\bmsc\b|\bbsc\b|university", low):
        return "Not stated"
    if re.search(r"\b(civil|structural|geotechnical|water|infrastructure|construction|environmental)\s+engineering", low):
        return "Civil engineering"
    if has_any(low, F["degree_fields_dev"]):
        return "Development"
    if re.search(r"\bengineering\b", low) and not OTHER_ENG_RE.search(low):
        return "Engineering"
    return None


def evaluate(j):
    """Apply every filter. Returns extra fields if the job passes, otherwise None."""
    def no(reason):
        REJECTED[reason] = REJECTED.get(reason, 0) + 1
        return None

    title, loc, src = j["title"], j["location"], j["source"]
    tl, text = title.lower(), j.get("_text", "")
    home = is_home(loc, title)
    level = (j.get("level") or "").upper().strip()

    if has_any(title, CFG["exclude_terms"]):
        return no("wrong kind of role")
    if SENIOR_RE.search(tl):
        return no("too senior")
    if NON_ENGLISH_RE.search(tl):
        return no("non-English posting")
    if level.startswith("I-") or "intern" in tl.split() or "internship" in (j.get("jobtype") or "").lower():
        if not F["include_internships"]:
            return no("internship")
    elif src == "UN Careers":
        if not level:
            if not home:
                return no("local hire abroad")
        elif level not in F["allowed_levels"]:
            return no("level (outside entry-to-mid)")
        elif level.startswith("NO-") and not home:
            return no("national officer post abroad")
    if re.search(r"\b(national|local)\s+(consultant|expert|individual contractor|contractor|coordinator|officer)", tl) and not home:
        return no("national/local post abroad")
    if src == "ReliefWeb" and level and level.lower() not in [e.lower() for e in F["allowed_experience"]]:
        return no("experience level")
    if src == "Remotive":
        where = loc.lower()
        if not any(k in where for k in ("worldwide", "anywhere", "africa", "emea", "kenya", "global")) and where != "remote":
            return no("remote job locked to other regions")

    terms = CFG["engineering_terms"] + CFG["development_terms"]
    if src == "UN Careers":
        relevant = j.get("family", "") in F["relevant_un_families"] or has_any(title, terms)
    elif src == "Remotive":
        relevant = has_any(title, terms)
    else:
        relevant = has_any(f"{title} {j.get('summary', '')}", terms)
    if not relevant:
        return no("not relevant to your fields")

    full = f"{title}. {text}"
    if visa_blocked(full):
        return no("visa-restricted / nationals only")
    fit = "Not verified"
    if text:
        if language_blocked(full):
            return no("needs another language")
        yrs = years_required(text)
        if yrs and yrs > F["max_years_required"]:
            return no("too much experience required")
        fit = degree_fit(text)
        if fit is None:
            return no("degree in an unrelated field")
    ACCEPTED[src] = ACCEPTED.get(src, 0) + 1
    return {"level": level, "degree_fit": fit, "verified": "Full text" if text else "Feed only"}


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
    if any(k in loc for k in ("remote", "home-based", "home based", "anywhere", "virtual", "worldwide")):
        return "Remote"
    if any(k in loc for k in AFRICA):
        return "Africa"
    return "Global / other"


# ---------------------------------------------------------------- source: ReliefWeb API
def collect_reliefweb():
    cfg = CFG.get("reliefweb", {})
    app = os.getenv("RELIEFWEB_APPNAME")
    if not cfg.get("enabled") or not app:
        log("ReliefWeb API: skipped (no RELIEFWEB_APPNAME). This is the main source for NGOs and UN agencies.")
        return []
    terms = " OR ".join(f'"{k}"' if " " in k else k for k in cfg["keywords"])
    body = {
        "limit": 500,
        "sort": ["date.created:desc"],
        "query": {"value": terms, "fields": ["title"], "operator": "OR"},
        "fields": {"include": ["title", "url", "source.name", "country.name", "city.name", "date.closing",
                               "date.created", "body", "how_to_apply", "experience.name",
                               "career_categories.name", "type.name"]},
    }
    try:
        r = requests.post(f"https://api.reliefweb.int/v2/jobs?appname={app}", json=body, headers=HEADERS, timeout=120)
        r.raise_for_status()
        out = []
        for item in r.json().get("data", []):
            f = item.get("fields", {})
            as_list = lambda v: v if isinstance(v, list) else ([v] if v else [])
            places = [c["name"] for c in as_list(f.get("city"))] + [c["name"] for c in as_list(f.get("country"))]
            exp = (as_list(f.get("experience")) or [{}])[0].get("name", "")
            cats = "; ".join(c["name"] for c in as_list(f.get("career_categories")))
            out.append({
                "title": f.get("title", ""),
                "organization": (as_list(f.get("source")) or [{}])[0].get("name", ""),
                "location": ", ".join(places) or "Worldwide",
                "url": f.get("url", ""),
                "deadline": to_date((f.get("date") or {}).get("closing")),
                "posted": to_date((f.get("date") or {}).get("created")),
                "summary": cats, "source": "ReliefWeb", "level": exp,
                "jobtype": ", ".join(t["name"] for t in as_list(f.get("type"))),
                "_text": strip_html(f"{f.get('body', '')} {f.get('how_to_apply', '')}")[:12000],
            })
        log(f"ReliefWeb API: {len(out)} results")
        return out
    except Exception as e:
        log(f"ReliefWeb API: failed ({e})")
        return []


# ---------------------------------------------------------------- source: UN Careers RSS (+ generic RSS)
def _text(el, tag):
    found = el.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_feed(content, feed):
    root = ET.fromstring(content)
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        title, link, desc = _text(it, "title"), _text(it, "link"), _text(it, "description")
        if feed.get("type") == "uncareers":
            kv = {}
            for line in re.sub(r"<br\s*/?>", "\n", desc).split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    kv[k.strip().lower()] = strip_html(v)
            out.append({
                "title": title, "organization": kv.get("department/office", ""),
                "location": kv.get("duty station", "").title(), "url": link,
                "deadline": to_date(kv.get("deadline")), "posted": to_date(kv.get("posted date")),
                "summary": " | ".join(x for x in (kv.get("job family"), kv.get("job network")) if x),
                "source": feed["name"], "level": kv.get("level", ""), "family": kv.get("job family", ""),
                "_text": "",
            })
        else:
            out.append({"title": title, "organization": "", "location": "", "url": link, "deadline": None,
                        "posted": to_date(_text(it, "pubDate")), "summary": strip_html(desc)[:200],
                        "source": feed["name"], "_text": strip_html(desc)[:8000]})
    for it in root.iter(atom + "entry"):
        link = it.find(atom + "link")
        out.append({"title": _text(it, atom + "title"), "organization": "", "location": "",
                    "url": link.get("href", "") if link is not None else "", "deadline": None,
                    "posted": to_date(_text(it, atom + "updated")),
                    "summary": strip_html(_text(it, atom + "summary"))[:200], "source": feed["name"],
                    "_text": strip_html(_text(it, atom + "summary"))[:8000]})
    return out


def collect_rss():
    out = []
    for feed in CFG.get("rss_feeds", []):
        if not feed.get("enabled", True):
            continue
        try:
            r = requests.get(feed["url"], headers=HEADERS, timeout=60)
            r.raise_for_status()
            items = parse_feed(r.content, feed)
            out += items
            log(f"RSS {feed['name']}: {len(items)} items")
        except Exception as e:
            log(f"RSS {feed.get('name')}: failed ({e})")
    return out


# ---------------------------------------------------------------- source: Remotive
def collect_remotive():
    cfg = CFG.get("remotive", {})
    if not cfg.get("enabled"):
        return []
    out = []
    for term in cfg.get("search_terms", []):
        try:
            r = requests.get("https://remotive.com/api/remote-jobs", params={"search": term}, headers=HEADERS, timeout=45)
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                where = j.get("candidate_required_location") or ""
                out.append({
                    "title": j.get("title", ""), "organization": j.get("company_name", ""),
                    "location": f"Remote ({where})" if where else "Remote",
                    "url": j.get("url", ""), "deadline": None, "posted": to_date(j.get("publication_date")),
                    "summary": strip_html(j.get("description", ""))[:160], "source": "Remotive",
                    "_text": strip_html(j.get("description", ""))[:8000],
                })
        except Exception as e:
            log(f"Remotive '{term}': failed ({e})")
    log(f"Remotive: {len(out)} raw results")
    return out


# ---------------------------------------------------------------- source: Claude agent (optional, paid)
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
        log("Claude agent: skipped (optional, paid)")
        return []
    import anthropic

    client = anthropic.Anthropic()
    model = os.getenv("CLAUDE_MODEL") or CFG["model"]
    out = []
    for topic in cfg["searches"]:
        prompt = (
            f"Today is {TODAY}. Search the web for job openings that are open right now.\n\nTopic: {topic}\n\n"
            f"Candidate context: {CFG['profile']}\n\nRules:\n"
            "- Only entry to mid-level roles that need English and at most Kiswahili, with visa support or open to "
            "Kenyan nationals.\n"
            "- Only include postings you actually saw in search results, with a direct URL. Never invent anything.\n"
            "- Skip anything whose deadline has passed.\n"
            f"- At most {cfg['max_results_per_search']} postings.\n"
            "- Reply with ONLY a JSON array (no prose, no code fences) with keys: title, organization, location, "
            "url, deadline (YYYY-MM-DD or null), posted (YYYY-MM-DD or null), summary (max 20 words). "
            "If nothing is found, reply with []."
        )
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": cfg["max_web_searches_per_topic"]}]
        messages = [{"role": "user", "content": prompt}]
        try:
            for _ in range(6):
                resp = client.messages.create(model=model, max_tokens=4000, tools=tools, messages=messages)
                if resp.stop_reason != "pause_turn":
                    break
                messages.append({"role": "assistant", "content": resp.content})
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            for j in extract_json_array(text):
                if isinstance(j, dict):
                    j["source"] = "Web agent"
                    out.append(j)
        except Exception as e:
            log(f"Claude agent: failed on '{topic[:40]}...' ({e})")
    log(f"Claude agent: {len(out)} results")
    return out


# ---------------------------------------------------------------- store
def load_store():
    if STORE.exists():
        data = json.loads(STORE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == STORE_VERSION:
            return data["jobs"]
    return {}  # old format or first run: start clean so earlier, looser results disappear


def save_store(store):
    STORE.parent.mkdir(exist_ok=True)
    STORE.write_text(json.dumps({"version": STORE_VERSION, "jobs": store}, indent=1, ensure_ascii=False), encoding="utf-8")


def clean(job):
    j = {k: str(job.get(k) or "").strip() for k in ("title", "organization", "location", "url", "summary", "source")}
    j["deadline"], j["posted"] = to_date(job.get("deadline")), to_date(job.get("posted"))
    for k in ("level", "family", "jobtype", "_text"):
        j[k] = str(job.get(k) or "")
    return j


def merge(store, found):
    url_index = {norm_url(v["url"]): k for k, v in store.items() if v.get("url")}
    new = 0
    for raw in found:
        j = clean(raw)
        if not j["title"] or not j["url"].lower().startswith(("http://", "https://")):
            continue
        if j["deadline"] and j["deadline"] < TODAY:
            REJECTED["deadline passed"] = REJECTED.get("deadline passed", 0) + 1
            continue
        extra = evaluate(j)
        if extra is None:
            continue
        j.update(extra)
        for k in ("_text", "family", "jobtype"):
            j.pop(k, None)
        key = url_index.get(norm_url(j["url"])) or job_id(j)
        if key in store:
            old = store[key]
            old.update(last_seen=TODAY, level=j["level"], degree_fit=j["degree_fit"], verified=j["verified"])
            old["deadline"] = j["deadline"] or old.get("deadline")
            old["organization"] = old.get("organization") or j["organization"]
            old["location"] = old.get("location") or j["location"]
            old["source"] = ", ".join(sorted(set(old["source"].split(", ")) | {j["source"]}))
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
        r.update(id=k, category=categorize(j), region=region_of(j.get("location")), is_new=j["first_seen"] == TODAY)
        out.append(r)
    out.sort(key=lambda r: (not r["is_new"], r["deadline"] or "9999", r["title"].lower()))
    return out


# ---------------------------------------------------------------- output
def write_csv(data):
    DOCS.mkdir(exist_ok=True)
    cols = ["title", "organization", "location", "region", "category", "level", "degree_fit", "verified",
            "deadline", "posted", "first_seen", "source", "url"]
    with open(DOCS / "jobs.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(data)


def write_html(data, filename="index.html"):
    DOCS.mkdir(exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = (ROOT / "template.html").read_text(encoding="utf-8")
    page = (page.replace("__DATA__", payload).replace("__UPDATED__", TODAY)
                .replace("__NEW_COUNT__", str(sum(1 for r in data if r["is_new"]))))
    (DOCS / filename).write_text(page, encoding="utf-8")


DEMO = [
    {"title": "Graduate Structural Engineer", "organization": "Example Consulting Engineers", "location": "Nairobi, Kenya",
     "url": "https://example.com/jobs/1", "deadline": "2026-11-20", "posted": TODAY, "summary": "Structural design",
     "source": "ReliefWeb", "level": "0-2 years", "_text": "Bachelor's degree in civil engineering. Fluency in English."},
    {"title": "Climate Resilience Programme Officer", "organization": "Example UN Agency", "location": "Nairobi, Kenya",
     "url": "https://example.com/jobs/2", "deadline": "2026-11-12", "posted": TODAY, "summary": "Climate adaptation",
     "source": "ReliefWeb", "level": "3-4 years", "_text": "Master's degree in development studies or a related field."},
    {"title": "Sustainable Development Officer", "organization": "Resident Coordinator System", "location": "New York",
     "url": "https://example.com/jobs/3", "deadline": "2026-12-01", "posted": "2026-10-25", "summary": "Sustainable Development",
     "source": "UN Careers", "level": "P-3", "family": "Sustainable Development", "_text": ""},
]


def main():
    if "--demo" in sys.argv:
        store = {}
        merge(store, DEMO)
        write_html(rows(store), "demo.html")
        log("Wrote docs/demo.html")
        return
    store = load_store()
    found = collect_reliefweb() + collect_rss() + collect_remotive() + collect_agent()
    new = merge(store, found)
    prune(store)
    save_store(store)
    data = rows(store)
    write_html(data)
    write_csv(data)
    log("Kept by source: " + (", ".join(f"{k} {v}" for k, v in sorted(ACCEPTED.items())) or "none"))
    log("Filtered out: " + (", ".join(f"{k} {v}" for k, v in sorted(REJECTED.items(), key=lambda x: -x[1])) or "nothing"))
    log(f"Done: {len(store)} open jobs tracked, {new} new today")


if __name__ == "__main__":
    main()
