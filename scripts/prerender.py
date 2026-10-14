"""
Pre-render every page of the site into plain HTML so search engines can read it.

Run after build_data.py (the GitHub workflow does this every morning):
    python scripts/prerender.py

It serves the repo locally, opens the site in headless Chromium, visits every route
(sections, this week's games, every player on the board), and saves the finished page as
<route>/index.html. It also writes 404.html (so any other path still loads the app) and a
sitemap.xml listing every page. Generated files are deployed but not committed.
"""
import asyncio
import functools
import http.server
import json
import os
import re
import shutil
import threading
import unicodedata
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SITE = "https://tdsignal.app"
PORT = 8123
SECTIONS = {"slate": "/", "td": "/td-model/", "props": "/props/", "snaps": "/snap-counts/",
            "defense": "/defenses/", "method": "/how-it-works/", "record": "/record/"}


def log(*a):
    print("[prerender]", *a, flush=True)


def slug(name):
    # must match slug() in index.html
    s = unicodedata.normalize("NFD", str(name or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"['.]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def routes(data):
    week = (data.get("meta") or {}).get("week", 0)
    out = [(k, v) for k, v in SECTIONS.items()]
    for g in data.get("GAMES", []):
        out.append((f"game/{g['id']}", f"/week-{week}/{g['away'].lower()}-at-{g['home'].lower()}/"))
    seen = set()
    for p in data.get("P", []):
        s = slug(p["name"])
        if s and s not in seen:
            seen.add(s)
            out.append((f"player/{s}", f"/players/{s}/"))
    return out


def serve():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a, **k):
            pass
    handler = functools.partial(Quiet, directory=ROOT)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


async def render_all(route_list):
    from playwright.async_api import async_playwright
    pages = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.add_init_script("window.__PRERENDER = true;")
        await page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
        await page.wait_for_function("window.__ready === true", timeout=30000)
        for route, path in route_list:
            await page.evaluate("r => window.__go(r)", route)
            await page.wait_for_timeout(60)
            html = await page.content()
            pages[path] = "<!DOCTYPE html>\n" + html.split(">", 1)[1] if html.lower().startswith("<!doctype") else "<!DOCTYPE html>\n" + html
        await browser.close()
    return pages


def write_sitemap(paths):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    def pri(p):
        return "1.0" if p == "/" else "0.9" if p in SECTIONS.values() else "0.8" if p.startswith("/week-") else "0.7"
    urls = "".join(f"  <url><loc>{SITE}{p}</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>{pri(p)}</priority></url>\n"
                   for p in paths)
    with open(os.path.join(ROOT, "sitemap.xml"), "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                + urls + "</urlset>\n")


def main():
    data_path = os.path.join(ROOT, "data.json")
    if not os.path.exists(data_path):
        log("no data.json yet; skipping")
        return
    data = json.load(open(data_path))
    route_list = routes(data)
    # keep an untouched copy of the app as the fallback for any other path
    shutil.copyfile(os.path.join(ROOT, "index.html"), os.path.join(ROOT, "404.html"))
    httpd = serve()
    try:
        pages = asyncio.run(render_all(route_list))
    finally:
        httpd.shutdown()
    for path, html in pages.items():
        target = os.path.join(ROOT, path.strip("/"), "index.html") if path != "/" else os.path.join(ROOT, "index.html.prerendered")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write(html)
    # replace the home page last, so a failure above never leaves a half-written site
    if "/" in pages:
        os.replace(os.path.join(ROOT, "index.html.prerendered"), os.path.join(ROOT, "index.html"))
    write_sitemap([p for _, p in route_list])
    log(f"rendered {len(pages)} pages and wrote sitemap.xml")


if __name__ == "__main__":
    main()
