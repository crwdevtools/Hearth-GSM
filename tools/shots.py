"""Screenshot the demo UI (dev tool). Usage: python tools/shots.py OUTDIR [route ...]"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
routes = sys.argv[2:] or ["/", "/servers/1/console", "/servers/new", "/modpacks", "/admin/nodes", "/servers/2/software"]

with sync_playwright() as p:
    browser = p.chromium.launch()
    for label, viewport in (("desk", {"width": 1440, "height": 900}), ("mob", {"width": 390, "height": 844})):
        page = browser.new_page(viewport=viewport, device_scale_factor=1)
        errors = []
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto("http://127.0.0.1:8000/")
        page.wait_for_selector("input[name=username]")
        page.fill("input[name=username]", "admin")
        page.fill("input[name=password]", "demo-password-123")
        page.click("button[type=submit]")
        page.wait_for_selector(".topbar")
        for route in routes:
            page.goto(f"http://127.0.0.1:8000/#{route}")
            page.wait_for_timeout(2500)
            name = route.strip("/").replace("/", "_") or "home"
            page.screenshot(path=str(out / f"{label}-{name}.png"), full_page=label == "desk")
        print(label, "errors:", errors[:10])
        page.close()
    browser.close()
