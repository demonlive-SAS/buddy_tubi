#!/usr/bin/env python3
"""
Tubi Live Channels Scraper - Playwright edition

Outputs: tubi_playlist.m3u, tubi_epg.xml, tubi_fallback_ids.json (ID cache)

Setup:
    pip install requests playwright
    python -m playwright install --with-deps chromium

Usage:
    python tubi_scraper.py
    python tubi_scraper.py --proxy http://host:port     # US exit if you're outside the US
    python tubi_scraper.py --categories                 # group-title by Tubi category
    python tubi_scraper.py --debug                      # save network log + page HTML to ./debug/

How it works:
    1. Open tubitv.com/live in headless Chromium and record every Tubi API response.
    2. Pull channel IDs from the page state / captured API JSON, and reuse the
       auth headers the page itself used.
    3. Query the EPG endpoint in batches through the browser's request context
       (same cookies + headers as the real site), merging in any EPG rows the
       page already loaded on its own.
    4. Write M3U + XMLTV; cache the ID list for the next run.
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse, urlunparse

LIVE_PAGE_URL = "https://tubitv.com/live"
EPG_ENDPOINTS = [
    "https://tubitv.com/oz/epg/programming",
    "https://epg-cdn.production-public.tubi.io/content/epg/programming",
]
TUBI_HOSTS = ("tubitv.com", "tubi.io", "adrise.tv")

EPG_BATCH_SIZE = 50
GROUP_TITLE = "Fast Tubi"
FALLBACK_FILE = "tubi_fallback_ids.json"
CACHE_MIN_IDS = 100
EPG_URL_TVG = ("https://raw.githubusercontent.com/BuddyChewChew/tubi-scraper/"
               "refs/heads/main/tubi_epg.xml")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

SEED_FALLBACK_IDS = [
    400000008, 400000011, 400000012, 400000024, 400000028, 400000030, 400000031,
    400000033, 400000056, 400000059, 400000062, 400000063, 400000067, 400000069,
    400000070, 400000073, 400000074, 400000083, 400000085, 400000086, 400000087,
    400000088, 400000089, 400000090, 400000092, 400000094, 400000095, 400000096,
    400000098, 400000099, 400000104, 400000105, 400000106, 400000108, 400000117,
    400000119, 400000121, 400000164, 400000169, 400000179, 400000195, 400000196,
    400000247, 400000251, 400000285, 400000287, 400000289, 400000290, 400000291,
    400000294, 400000296, 400000299, 400000303, 400000304,
    555113, 555119, 555121, 555124, 555126, 555127, 555130, 555382,
    556174, 557344, 557345, 559144, 560215, 571664,
    613683, 613695, 613758, 613759, 613761, 618762, 618763, 628893, 629323,
    653199, 653200, 653208, 656575, 670602, 670604, 671073, 671083,
    673411, 673498, 673499, 673500, 677011, 680705, 682057, 682059, 682634,
    684164, 684165, 684167, 684170, 691129, 692051, 692057, 692086, 692087,
    692090, 692114, 694174, 700406, 700407, 700414, 700418, 711410,
    715946, 715947, 715948, 715949, 715950, 715951, 715952, 724209,
]

DEBUG = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def dump_debug(name, text):
    if not DEBUG:
        return
    os.makedirs("debug", exist_ok=True)
    with open(os.path.join("debug", name), "w", encoding="utf-8") as f:
        f.write(text)


def _to_id(v):
    if isinstance(v, dict):
        v = v.get("content_id") or v.get("id")
    try:
        n = int(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def find_key(obj, wanted):
    if isinstance(obj, dict):
        if wanted in obj:
            return obj[wanted]
        for v in obj.values():
            r = find_key(v, wanted)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, wanted)
            if r is not None:
                return r
    return None


def ids_from_page_data(data):
    """(ids, {id: category}) from a contentIdsByContainer structure."""
    by_container = find_key(data, "contentIdsByContainer")
    ids, cats = [], {}
    if isinstance(by_container, dict):
        for cat_list in by_container.values():
            if not isinstance(cat_list, list):
                continue
            for cat in cat_list:
                if not isinstance(cat, dict):
                    continue
                name = cat.get("name") or "Other"
                for entry in cat.get("contents", []):
                    cid = _to_id(entry)
                    if cid:
                        ids.append(cid)
                        cats.setdefault(str(cid), name)
    return list(dict.fromkeys(ids)), cats


def ids_from_container_json(data):
    ids = []
    if isinstance(data, dict):
        for row in data.get("rows") or []:
            for item in (row.get("contents") or []) if isinstance(row, dict) else []:
                cid = _to_id(item)
                if cid:
                    ids.append(cid)
        if not ids:
            for item in data.get("contents") or []:
                cid = _to_id(item)
                if cid:
                    ids.append(cid)
    return list(dict.fromkeys(ids))


def ids_from_generic_lists(obj, out=None):
    """Lists stored under keys like content_ids / contentIds / channel_ids."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in ("content_ids", "contentids", "channel_ids", "channelids") \
                    and isinstance(v, list):
                out.extend(i for i in map(_to_id, v) if i)
            else:
                ids_from_generic_lists(v, out)
    elif isinstance(obj, list):
        for v in obj:
            ids_from_generic_lists(v, out)
    return out


def rows_from_epg_response(data):
    if isinstance(data, dict):
        rows = data.get("rows") or data.get("contents") or data.get("data") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


def looks_like_epg_row(r):
    return bool(r.get("content_id") or r.get("id")) and (r.get("programs") or r.get("video_resources"))


# ---------------------------------------------------------------------------
# browser capture + EPG fetch (everything that needs Playwright)
# ---------------------------------------------------------------------------
def run_browser(proxy):
    """Returns (ids, categories, epg_rows, discovered_live)."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    captured = []        # (url, status, parsed_json)
    seen_urls = []       # (method, status, url)
    auth_headers = {}
    page_rows = {}       # rows the page loaded by itself
    epg_urls = []

    with sync_playwright() as p:
        launch_kw = {"headless": True,
                     "args": ["--disable-blink-features=AutomationControlled"]}
        if proxy:
            launch_kw["proxy"] = {"server": proxy}
        browser = p.chromium.launch(**launch_kw)
        ctx = browser.new_context(user_agent=UA, locale="en-US",
                                  timezone_id="America/New_York",
                                  viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()

        def on_request(req):
            if any(h in req.url for h in TUBI_HOSTS) and req.resource_type in ("fetch", "xhr"):
                for k, v in req.headers.items():
                    if k.lower() == "authorization" or k.lower().startswith("x-"):
                        auth_headers[k] = v

        def on_response(resp):
            url = resp.url
            if not any(h in url for h in TUBI_HOSTS):
                return
            ctype = (resp.headers.get("content-type") or "").lower()
            if resp.request.resource_type in ("fetch", "xhr") or "json" in ctype:
                seen_urls.append((resp.request.method, resp.status, url))
            if "json" not in ctype:
                return
            try:
                data = resp.json()
            except Exception:
                return
            captured.append((url, resp.status, data))
            if "epg/programming" in url:
                epg_urls.append(url.split("?")[0])
                for row in rows_from_epg_response(data):
                    if looks_like_epg_row(row):
                        page_rows[str(row.get("content_id") or row.get("id"))] = row

        page.on("request", on_request)
        page.on("response", on_response)

        print("Opening tubitv.com/live in headless Chromium...")
        try:
            page.goto(LIVE_PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        except PWTimeout:
            print("  page load timed out; continuing with whatever loaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeout:
            pass

        html = page.content()
        dump_debug("live_page.html", html)
        low = html.lower()
        if "not available in europe" in low or "gdpr.tubi.tv" in low:
            print("  geo-blocked (non-US IP). Use --proxy with a US exit.")

        # Nudge lazy loading of the guide
        for _ in range(8):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(700)
        page.wait_for_timeout(2000)

        # --- collect channel IDs ------------------------------------------------
        ids, cats = [], {}
        try:
            state = page.evaluate(
                "() => { try { return JSON.parse(JSON.stringify(window.__data)); }"
                " catch (e) { return null; } }")
        except Exception:
            state = None
        if state:
            ids, cats = ids_from_page_data(state)
            print(f"  window.__data: {len(ids)} IDs")

        if not ids:
            for url, status, data in captured:
                got, c = ids_from_page_data(data)
                if not got:
                    got = ids_from_container_json(data)
                if not got:
                    got = ids_from_generic_lists(data)
                if len(got) > len(ids):
                    ids, cats = list(dict.fromkeys(got)), c
                    print(f"  {url.split('?')[0]}: {len(ids)} IDs")

        discovered = bool(ids)
        if not ids:
            print("Live discovery found no IDs - using cached/seed IDs for the EPG calls")
            ids = load_cached_ids()

        # --- EPG via the browser's request context -----------------------------
        hdrs = dict(auth_headers)
        endpoints = list(dict.fromkeys(epg_urls + EPG_ENDPOINTS))
        epg_rows, seen = [], set()

        def add_rows(rows):
            n = 0
            for r in rows:
                cid = str(r.get("content_id") or r.get("id") or "")
                if cid and cid not in seen:
                    seen.add(cid)
                    epg_rows.append(r)
                    n += 1
            return n

        if ids:
            working = None
            batches = [ids[i:i + EPG_BATCH_SIZE] for i in range(0, len(ids), EPG_BATCH_SIZE)]
            for n, batch in enumerate(batches, 1):
                params = {"content_id": ",".join(map(str, batch))}
                got = []
                for url in ([working] if working else endpoints):
                    try:
                        r = ctx.request.get(url, params=params, headers=hdrs, timeout=40000)
                        if r.status != 200:
                            print(f"  batch {n}/{len(batches)} {urlparse(url).netloc}: "
                                  f"HTTP {r.status} {r.text()[:120]!r}")
                            continue
                        got = rows_from_epg_response(r.json())
                        if got:
                            working = url
                            break
                    except Exception as e:
                        print(f"  batch {n}/{len(batches)} {urlparse(url).netloc}: {e}")
                print(f"EPG batch {n}/{len(batches)}: +{add_rows(got)} channels")

        # Merge rows the page fetched itself (helps if our own calls were blocked)
        extra = add_rows(page_rows.values())
        if extra:
            print(f"Merged {extra} extra channels captured from the page's own EPG calls")

        # --- diagnostics --------------------------------------------------------
        print(f"Tubi API calls seen: {len(seen_urls)}")
        for m, s, u in seen_urls[:30]:
            print(f"  {m} {s} {u[:140]}")
        if DEBUG:
            dump_debug("network.json", json.dumps(
                {"requests": seen_urls, "auth_header_names": list(auth_headers)}, indent=2))
        browser.close()

    return ids, cats, epg_rows, discovered


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
def load_cached_ids():
    if os.path.exists(FALLBACK_FILE):
        try:
            with open(FALLBACK_FILE, encoding="utf-8") as f:
                ids = [int(x) for x in json.load(f).get("ids", [])]
            if ids:
                print(f"Using {len(ids)} cached IDs from {FALLBACK_FILE}")
                return ids
        except Exception as e:
            print(f"Could not read {FALLBACK_FILE}: {e}")
    print(f"Using {len(SEED_FALLBACK_IDS)} built-in seed IDs")
    return list(SEED_FALLBACK_IDS)


def save_cached_ids(ids):
    if len(ids) < CACHE_MIN_IDS:
        return
    with open(FALLBACK_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(timezone.utc).isoformat(),
                   "count": len(ids), "ids": sorted(set(ids))}, f, indent=2)
    print(f"Cached {len(ids)} IDs to {FALLBACK_FILE}")


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
def clean_stream_url(url):
    p = urlparse(unquote(url))
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def pick_manifest(channel):
    cands = []
    for res in channel.get("video_resources") or []:
        url = (res.get("manifest") or {}).get("url")
        if url:
            cands.append((bool(res.get("license_server") or res.get("drm")), url))
    cands.sort(key=lambda x: x[0])
    return clean_stream_url(cands[0][1]) if cands else ""


def thumbnail(channel):
    imgs = channel.get("images") or {}
    thumb = imgs.get("thumbnail") or imgs.get("landscape") or []
    if isinstance(thumb, str):
        return thumb
    return thumb[0] if thumb else ""


def build_m3u(rows, categories, use_categories):
    lines = [f'#EXTM3U url-tvg="{EPG_URL_TVG}"',
             f"# Generated {datetime.now(timezone.utc).isoformat()}"]
    seen_urls = set()
    for ch in sorted(rows, key=lambda c: (c.get("title") or "").lower()):
        url = pick_manifest(ch)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        cid = str(ch.get("content_id") or ch.get("id") or "")
        name = (ch.get("title") or "Unknown").replace(",", " ")
        group = categories.get(cid, GROUP_TITLE) if use_categories else GROUP_TITLE
        lines.append(f'#EXTINF:-1 tvg-id="{cid}" tvg-logo="{thumbnail(ch)}" '
                     f'group-title="{group}",{name}')
        lines.append(url)
    print(f"Playlist: {len(seen_urls)} playable channels")
    return "\n".join(lines) + "\n"


def xmltv_time(iso):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(iso, fmt).strftime("%Y%m%d%H%M%S +0000")
        except (ValueError, TypeError):
            pass
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")
    except (ValueError, TypeError):
        return iso or ""


def build_xmltv(rows):
    root = ET.Element("tv", {"generator-info-name": "tubi-scraper"})
    for ch in rows:
        cid = str(ch.get("content_id") or ch.get("id") or "")
        c = ET.SubElement(root, "channel", id=cid)
        ET.SubElement(c, "display-name").text = ch.get("title") or "Unknown"
        if thumbnail(ch):
            ET.SubElement(c, "icon", src=thumbnail(ch))
    for ch in rows:
        cid = str(ch.get("content_id") or ch.get("id") or "")
        for prog in ch.get("programs") or []:
            p = ET.SubElement(root, "programme", channel=cid,
                              start=xmltv_time(prog.get("start_time")),
                              stop=xmltv_time(prog.get("end_time")))
            ET.SubElement(p, "title").text = prog.get("title") or ""
            if prog.get("description"):
                ET.SubElement(p, "desc").text = prog["description"]
    return ET.ElementTree(root)


# ---------------------------------------------------------------------------
def main():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=os.environ.get("TUBI_PROXY"),
                    help="e.g. http://host:port (US exit)")
    ap.add_argument("--categories", action="store_true",
                    help="use Tubi categories for group-title")
    ap.add_argument("--debug", action="store_true",
                    help="save network log and page HTML to ./debug/")
    args = ap.parse_args()
    DEBUG = args.debug

    ids, categories, rows, discovered = run_browser(args.proxy)

    if discovered:
        save_cached_ids(ids)
    if not rows:
        print("ERROR: no EPG data. Check for a geo-block (use --proxy with a US IP) "
              "or a changed API; rerun with --debug and inspect debug/network.json.")
        sys.exit(1)

    with open("tubi_playlist.m3u", "w", encoding="utf-8") as f:
        f.write(build_m3u(rows, categories, args.categories))
    build_xmltv(rows).write("tubi_epg.xml", encoding="utf-8", xml_declaration=True)
    print(f"Done: {len(rows)} channels -> tubi_playlist.m3u, tubi_epg.xml")


if __name__ == "__main__":
    main()
