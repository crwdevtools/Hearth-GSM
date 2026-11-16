"""Click through every Hearth flow against tools/demo.py (dev tool).

    python tools/demo.py &   then   python tools/e2e.py

Fails on any uncaught page error, console error, 5xx response, or a step whose result is wrong.
"""

from __future__ import annotations

import re
import sys
import time
import traceback

from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "panel"))
from hearth_panel.security import totp_now  # noqa: E402

BASE = "http://127.0.0.1:8000"
problems: list[str] = []
passed: list[str] = []


def watch(page: Page, who: str) -> None:
    page.on("pageerror", lambda e: problems.append(f"[{who}] page error: {e}"))
    page.on("console", lambda m: m.type == "error" and "status of 4" not in m.text
            and "ERR_TUNNEL_CONNECTION_FAILED" not in m.text  # sandbox blocks external images (avatars)
            and problems.append(f"[{who}] console: {m.text}"))
    page.on("response", lambda r: r.status >= 500 and problems.append(f"[{who}] {r.status} {r.request.method} {r.url}"))


def step(name):
    def wrap(fn):
        def run(*args):
            try:
                fn(*args)
                passed.append(name)
            except Exception as exc:  # keep going; report everything at the end
                problems.append(f"STEP FAILED: {name}: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
                traceback.print_exc(limit=1)
        return run
    return wrap


def login(page: Page, user: str, password: str) -> None:
    page.goto(BASE + "/")
    page.fill("input[name=username]", user)
    page.fill("input[name=password]", password)
    page.click("button[type=submit]")
    expect(page.locator(".topbar")).to_be_visible()


def toast(page: Page, text: str) -> None:
    expect(page.locator(".toast", has_text=text).last).to_be_visible(timeout=10_000)


def status_is(page: Page, label: str, timeout=20_000) -> None:
    expect(page.locator(".page-head .status").first).to_have_text(label, timeout=timeout)


@step("dashboard filters and search")
def dashboard(page: Page):
    page.goto(BASE + "/#/")
    expect(page.locator("tr.row")).to_have_count(5, timeout=10_000)
    page.keyboard.press("/")
    page.keyboard.type("vik")
    expect(page.locator("tr.row")).to_have_count(1)
    page.fill("[data-search]", "")
    page.get_by_role("tab", name=re.compile("Running")).click()
    expect(page.locator("tr.row").first).to_be_visible()
    page.get_by_role("tab", name=re.compile("All servers")).click()


@step("create a Vanilla server and watch it boot")
def create_vanilla(page: Page):
    page.goto(BASE + "/#/servers/new")
    page.get_by_label("Name", exact=True).fill("E2E Vanilla")
    page.get_by_role("button", name="Vanilla", exact=True).click()
    sel = page.locator("#mc-version")
    expect(sel).to_be_enabled()
    expect(sel.locator("option")).not_to_have_count(1)
    sel.select_option("1.20.4")
    page.get_by_role("button", name="2 GB", exact=True).click()
    page.get_by_text("I agree to the Minecraft").click()
    page.get_by_role("button", name="Create server").click()
    expect(page.locator("h1")).to_have_text("E2E Vanilla", timeout=10_000)
    status_is(page, "Running", 30_000)


@step("console: command round-trip and history")
def console(page: Page):
    box = page.get_by_label("Console command")
    box.fill("list")
    box.press("Enter")
    expect(page.locator(".console-out")).to_contain_text("players online", timeout=10_000)
    box.press("ArrowUp")
    expect(box).to_have_value("list")


@step("power: restart, stop, start")
def power(page: Page):
    page.get_by_role("button", name="Restart").click()
    status_is(page, "Running", 30_000)
    page.get_by_role("button", name="Stop").click()
    status_is(page, "Offline", 30_000)
    page.get_by_role("button", name="Start").click()
    status_is(page, "Running", 30_000)


@step("files: create, edit, rename, delete")
def files(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/files")
    page.get_by_role("button", name="New file").click()
    page.locator("dialog input").fill("notes.txt")
    page.locator("dialog").get_by_role("button", name="Create").click()
    editor = page.locator("textarea.editor")
    expect(editor).to_be_visible()
    editor.fill("hello hearth\n")
    page.keyboard.press("Control+s")
    toast(page, "Saved notes.txt")
    page.get_by_role("button", name="Close").click()
    page.get_by_role("button", name="Rename notes.txt").click()
    page.locator("dialog input").fill("renamed.txt")
    page.locator("dialog").get_by_role("button", name="Rename").click()
    expect(page.get_by_text("renamed.txt")).to_be_visible()
    page.get_by_text("renamed.txt").click()
    expect(page.locator("textarea.editor")).to_have_value("hello hearth\n")
    page.get_by_role("button", name="Close").click()
    page.get_by_role("button", name="Delete renamed.txt").click()
    page.locator("dialog").get_by_role("button", name="Delete").click()
    toast(page, "Deleted renamed.txt")
    expect(page.get_by_text("renamed.txt")).to_have_count(0)


@step("properties: save MOTD and difficulty")
def properties(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/properties")
    page.get_by_label("Message of the day").fill("Edited by e2e")
    page.get_by_label("Difficulty").select_option("hard")
    page.get_by_role("button", name="Save").click()
    toast(page, "Saved.")
    page.reload()
    expect(page.get_by_label("Message of the day")).to_have_value("Edited by e2e")
    expect(page.get_by_label("Difficulty")).to_have_value("hard")


@step("software: switch to Paper 1.21.1")
def software(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/software")
    expect(page.locator("#mc-version")).to_have_value("1.20.4")  # current version preselected, not LATEST
    page.get_by_role("button", name="Paper", exact=True).click()
    expect(page.locator("#mc-version option[value='1.21.1']")).to_have_count(1)
    page.locator("#mc-version").select_option("1.21.1")
    page.get_by_role("button", name="Apply and restart").click()
    page.locator("dialog").get_by_role("button", name="Apply and restart").click()
    toast(page, "Applied")
    page.goto(f"{BASE}/#/servers/{sid}/software")
    expect(page.locator(".picked").first).to_contain_text("Paper")
    expect(page.locator(".picked").first).to_contain_text("1.21.1")


@step("network: add and remove an extra port")
def network(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/network")
    rows = page.locator("tbody tr")
    expect(rows).to_have_count(1)
    page.get_by_role("button", name="Add port").click()
    expect(rows).to_have_count(2)
    page.get_by_role("button", name="Remove").click()
    expect(rows).to_have_count(1)


@step("settings: rename, resources, spec preview")
def settings(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/settings")
    page.get_by_label("Name", exact=True).fill("E2E Renamed")
    page.get_by_label("Memory").fill("3072")
    page.get_by_role("button", name="Save details").click()
    toast(page, "Saved.")
    expect(page.locator("h1")).to_have_text("E2E Renamed")
    page.get_by_text("Show container spec").click()
    expect(page.locator("pre.code")).to_contain_text('"memory_mb": 3072')
    expect(page.locator("pre.code")).to_contain_text('"TYPE": "PAPER"')


@step("header keeps button focus while status streams")
def header_focus(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/console")
    status_is(page, "Running", 30_000)
    btn = page.get_by_role("button", name="Restart")
    btn.focus()
    page.wait_for_timeout(5000)  # two or more status pushes
    assert page.evaluate("document.activeElement?.textContent?.includes('Restart')"), "focus was lost"


@step("modpacks: search, expand, deploy a pinned version")
def modpacks(page: Page):
    page.goto(BASE + "/#/modpacks")
    page.locator("[data-search]").fill("cozy")
    expect(page.locator("tr.row")).to_have_count(1, timeout=10_000)
    page.locator("tr.row").click()
    page.get_by_role("link", name="Deploy", exact=True).nth(2).click()  # 6.0 beta, 5.0, 4.0 ...
    expect(page.get_by_label("Name", exact=True)).to_have_value("Cozy Farmlands")
    expect(page.locator(".picked")).to_contain_text("Cozy Farmlands")
    expect(page.locator("#pack-version")).to_have_value("DEMOPK02V4")
    page.get_by_text("I agree to the Minecraft").click()
    page.get_by_role("button", name="Create server").click()
    expect(page.locator("h1")).to_have_text("Cozy Farmlands", timeout=10_000)
    page.get_by_role("tab", name="Software").click()
    expect(page.locator(".picked").first).to_contain_text("Cozy Farmlands 4.0")
    page.get_by_role("button", name="Check now").click()
    toast(page, "Cozy Farmlands 5.0 is available")  # the newer release, never the 6.0 beta
    page.get_by_role("button", name="Update to Cozy Farmlands 5.0").click()
    page.locator("dialog").get_by_role("button", name="Update and restart").click()
    toast(page, "Updating")
    page.get_by_role("tab", name="Software").click()
    expect(page.locator(".picked").first).to_contain_text("Cozy Farmlands 5.0")


@step("modpack picker inside the create form")
def picker(page: Page):
    page.goto(BASE + "/#/servers/new")
    page.get_by_role("button", name="Modpack", exact=True).click()
    page.get_by_label("Search modpacks").fill("magic")
    page.locator(".picker-results .radio-item", has_text="Magic Sampler").click()
    expect(page.locator("#pack-version option").first).to_contain_text("Latest release")
    page.get_by_role("button", name="Change").click()
    expect(page.get_by_label("Search modpacks")).to_be_visible()


@step("validation messages are readable, not 500s")
def validation(page: Page):
    page.goto(BASE + "/#/admin/nodes/new")
    page.get_by_label("Name").fill("bad-node")
    page.get_by_label("Agent URL").fill("ftp://nope")
    page.get_by_label("Public address").fill("x.example")
    page.get_by_role("button", name="Add node").click()
    toast(page, "Agent URL must look like")


@step("admin: nodes, ports, users, settings")
def admin(page: Page):
    page.goto(BASE + "/#/admin/nodes")
    expect(page.locator("tr.row")).to_have_count(2, timeout=15_000)
    page.locator("tr.row", has_text="home-box").click()
    page.get_by_label("Ports", exact=True).fill("26000-26002")
    page.get_by_role("button", name="Add ports").click()
    toast(page, "Added 3 ports")
    page.get_by_role("button", name="Remove port 26002").click()
    expect(page.get_by_role("button", name="Remove port 26002")).to_have_count(0)

    page.goto(BASE + "/#/admin/users")
    page.get_by_label("Username").fill("e2e-friend")
    page.get_by_label("Password").fill("e2e-friend-password")
    page.get_by_role("button", name="Add user").click()
    toast(page, "can now sign in")
    page.get_by_label("e2e-friend is admin").check()
    toast(page, "is now an admin")
    page.get_by_role("button", name="Delete e2e-friend").click()
    page.locator("dialog").get_by_role("button", name="Delete").click()
    expect(page.get_by_text("e2e-friend")).to_have_count(0)

    page.goto(BASE + "/#/admin/settings")
    page.get_by_label("API key").fill("$2a$10$e2etestkeyABCD")
    page.get_by_label("Check every").fill("60")
    page.get_by_role("button", name="Save").click()
    toast(page, "Settings saved")
    expect(page.get_by_label("API key")).to_have_attribute("placeholder", re.compile("ABCD"))
    page.get_by_role("button", name="Remove key").click()
    toast(page, "removed")

    page.goto(BASE + "/#/admin/overview")
    page.get_by_role("button", name=re.compile("Check modpack updates")).click()
    toast(page, "Checked")


@step("delete a server with typed confirmation")
def delete(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/settings")
    page.get_by_role("button", name="Delete server").click()
    dialog = page.locator("dialog")
    expect(dialog.get_by_role("button", name="Delete forever")).to_be_disabled()
    dialog.locator("input").fill("E2E Renamed")
    dialog.get_by_role("button", name="Delete forever").click()
    toast(page, "deleted")
    page.goto(f"{BASE}/#/servers/{sid}")
    expect(page.get_by_text("Server not found")).to_be_visible()


@step("friend sees only their server and no admin controls")
def friend(browser):
    page = browser.new_page()
    watch(page, "friend")
    login(page, "sam", "friend-password-1")
    expect(page.locator("tr.row")).to_have_count(1, timeout=10_000)
    expect(page.get_by_role("link", name="Admin")).to_have_count(0)
    expect(page.get_by_role("link", name="New server")).to_have_count(0)
    page.locator("tr.row").click()
    page.get_by_role("tab", name="Settings").click()
    expect(page.get_by_label("Memory")).to_be_disabled()
    expect(page.get_by_role("button", name="Delete server")).to_have_count(0)
    page.get_by_role("tab", name="Network").click()
    expect(page.get_by_role("button", name="Add port")).to_have_count(0)
    page.goto(BASE + "/#/admin/users")
    expect(page.locator(".hero")).to_be_visible()  # bounced back to the dashboard
    expect(page.get_by_role("button", name="Add user")).to_have_count(0)
    page.goto(BASE + "/#/servers/1")
    expect(page.get_by_text("Server not found")).to_be_visible()
    page.close()


@step("mobile: menu opens and navigates")
def mobile(browser):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    watch(page, "mobile")
    login(page, "admin", "demo-password-123")
    page.get_by_role("button", name="Menu").click()
    page.get_by_role("link", name="Modpacks").click()
    expect(page.locator("h1")).to_have_text("Modpacks")
    expect(page.get_by_role("link", name="Admin")).not_to_be_visible()
    page.close()


@step("backups: create, then restore with a safety backup")
def backups(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/backups")
    page.get_by_label("Backup name").fill("E2E checkpoint")
    page.get_by_role("button", name="Back up now").click()
    row = page.locator("tr", has_text="E2E checkpoint")
    expect(row.get_by_text("Ready")).to_be_visible(timeout=20_000)
    row.get_by_role("button", name="Restore").click()
    page.locator("dialog").get_by_role("button", name="Restore").click()
    toast(page, "Restoring")
    expect(page.locator("tr", has_text="Before restoring")).to_be_visible(timeout=20_000)
    status_is(page, "Running", 40_000)


@step("schedules: add a preset and run it now")
def schedules(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/schedules")
    page.get_by_role("button", name="Nightly backup at 3 AM").click()
    expect(page.locator("#sc-cron")).to_have_value("0 3 * * *")
    expect(page.get_by_text("Next:")).to_be_visible()
    page.locator("#sc-cron").fill("99 * * * *")
    expect(page.get_by_text("Minute must be between 0 and 59")).to_be_visible()
    page.locator("#sc-cron").fill("0 3 * * *")
    page.get_by_role("button", name="Add task").click()
    toast(page, "Task added")
    row = page.locator("tr", has_text="Nightly backup")
    row.get_by_role("button", name="Run now").click()
    toast(page, "Backup finished")


@step("sharing: give a friend console + power")
def sharing(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/sharing")
    page.get_by_label("Share with").fill("sam")
    page.get_by_role("button", name="Share").click()
    toast(page, "sam can now use the console")
    card = page.locator(".share-card", has_text="sam")
    expect(card.get_by_label("Console")).to_be_checked()
    card.get_by_label("Files").check()
    page.reload()
    expect(page.locator(".share-card", has_text="sam").get_by_label("Files")).to_be_checked()


@step("network: join checklist and tunnel address")
def network_extras(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/network")
    page.get_by_role("button", name="Check now").click()
    expect(page.locator(".checklist li")).to_have_count(4, timeout=15_000)
    expect(page.locator(".checklist li", has_text="Server is running")).to_have_attribute("data-ok", "true")
    page.get_by_label("Tunnel or custom address").fill("demo.joinmc.link:4321")
    page.get_by_role("button", name="Save address").click()
    toast(page, "Address saved")
    expect(page.locator(".page-head")).to_contain_text("demo.joinmc.link:4321")
    page.get_by_label("Tunnel or custom address").fill("")
    page.get_by_role("button", name="Save address").click()


@step("settings: Discord validation and auto-sleep")
def integrations(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/settings")
    page.get_by_label("Webhook URL").fill("https://example.com/not-discord")
    page.get_by_role("button", name="Save integrations").click()
    toast(page, "Paste a Discord webhook URL")
    page.get_by_label("Webhook URL").fill("")
    page.get_by_label("Sleep", exact=True).select_option("15")
    page.get_by_role("button", name="Save integrations").click()
    toast(page, "Saved.")
    page.reload()
    expect(page.get_by_label("Sleep", exact=True)).to_have_value("15")


@step("players: online list and kick")
def players_tab(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/players")
    row = page.locator(".player-list li", has_text="alex").first
    expect(row).to_be_visible(timeout=10_000)
    row.get_by_role("button", name="Kick").click()
    toast(page, "Done.")


@step("mods: compatible plugin search")
def mods_tab(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/mods")
    expect(page.get_by_text("Installed plugins")).to_be_visible()
    expect(page.get_by_text("Essentials Demo")).to_be_visible(timeout=10_000)


@step("stats charts render")
def stats(page: Page, sid: str):
    page.goto(f"{BASE}/#/servers/{sid}/console")
    expect(page.locator(".chart")).to_have_count(2, timeout=10_000)


@step("admin: activity log, user quota, tunnel")
def admin_extras(page: Page):
    page.goto(BASE + "/#/admin/activity")
    expect(page.get_by_text("backup.completed").first).to_be_visible(timeout=10_000)
    page.goto(BASE + "/#/admin/users")
    page.get_by_label("Username", exact=True).fill("builder")
    page.get_by_label("Password", exact=True).fill("builder-password-1")
    page.get_by_label("Servers they can create").fill("1")
    page.get_by_label("Memory quota", exact=True).fill("4096")
    page.get_by_role("button", name="Add user").click()
    toast(page, "builder can now sign in")
    page.goto(BASE + "/#/admin/nodes")
    page.locator("tr.row", has_text="home-box").click()
    page.get_by_label("playit.gg secret key").fill("demo-secret-key-123456")
    page.get_by_role("button", name="Start tunnel").click()
    toast(page, "Tunnel agent started")
    expect(page.get_by_text("Tunnel agent running")).to_be_visible()


@step("quota user creates their own server, and is stopped at the limit")
def quota_user(browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    watch(page, "builder")
    login(page, "builder", "builder-password-1")
    page.get_by_role("link", name="New server").click()
    expect(page.get_by_label("Owner")).to_have_count(0)
    page.get_by_label("Name", exact=True).fill("Builder Base")
    page.get_by_role("button", name="2 GB", exact=True).click()
    page.get_by_text("I agree to the Minecraft").click()
    page.get_by_role("button", name="Create server").click()
    expect(page.locator("h1")).to_have_text("Builder Base", timeout=10_000)
    page.goto(BASE + "/#/servers/new")
    page.get_by_label("Name", exact=True).fill("One too many")
    page.get_by_text("I agree to the Minecraft").click()
    page.get_by_role("button", name="Create server").click()
    toast(page, "quota allows 1")
    page.close()


@step("two-factor: enrol, then sign in with a code")
def two_factor(browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    watch(page, "2fa")
    login(page, "sam", "friend-password-1")
    page.goto(BASE + "/#/account")
    page.get_by_role("button", name="Set up two-factor sign-in").click()
    secret = page.locator("code.totp-key").inner_text().replace(" ", "")
    page.get_by_label("Code from the app").fill(totp_now(secret))
    page.get_by_role("button", name="Confirm and turn on").click()
    toast(page, "Two-factor sign-in is on")
    page.get_by_role("button", name="Sign out").click()
    page.fill("input[name=username]", "sam")
    page.fill("input[name=password]", "friend-password-1")
    page.click("button[type=submit]")
    expect(page.get_by_label("Authentication code")).to_be_visible()
    page.get_by_label("Authentication code").fill("000000")
    page.click("button[type=submit]")
    expect(page.get_by_role("alert")).to_contain_text("didn't match")
    # The code used to turn 2FA on can't be used again, so sign in with the next one.
    page.get_by_label("Authentication code").fill(totp_now(secret, time.time() + 30))
    page.click("button[type=submit]")
    expect(page.locator(".topbar")).to_be_visible()
    page.close()


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(8000)
        watch(page, "admin")
        login(page, "admin", "demo-password-123")
        dashboard(page)
        create_vanilla(page)
        sid = page.url.split("/servers/")[1].split("/")[0]
        console(page)
        power(page)
        files(page, sid)
        properties(page, sid)
        software(page, sid)
        network(page, sid)
        settings(page, sid)
        header_focus(page, sid)
        backups(page, sid)
        schedules(page, sid)
        sharing(page, sid)
        network_extras(page, sid)
        integrations(page, sid)
        players_tab(page, sid)
        mods_tab(page, sid)
        stats(page, sid)
        modpacks(page)
        picker(page)
        validation(page)
        admin(page)
        admin_extras(page)
        delete(page, sid)
        friend(browser)
        quota_user(browser)
        two_factor(browser)
        mobile(browser)
        browser.close()
    print(f"\n{len(passed)} steps passed")
    for p_ in problems:
        print("PROBLEM", p_)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
