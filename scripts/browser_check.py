"""Exercise a real local UI with headless Chromium; browser dependencies are optional.

uv run --group browser python scripts/browser_check.py
"""

import argparse
import ipaddress
import json
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from chronicle.cli import default_data_dir
from chronicle.web import create_server


def check(url, screenshots):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url, wait_until="networkidle")
            expect(page.locator(".stat")).to_have_count(4)
            page.get_by_role("searchbox").fill("cbrd_27461")
            expect(page.locator("#case-count")).to_have_text("1 testcase")
            with page.expect_response(
                lambda response: "/api/overview?suite=sql" in response.url
            ) as stats:
                page.locator(".case-button").first.click()
            observations = stats.value.json()["counts"]["observations"]
            expect(page.locator(".stat").first).to_contain_text(f"{observations:,}")
            expect(page.get_by_text("This testcase has failed before.", exact=True)).to_be_visible()
            expect(page.locator("#detail")).to_be_visible()
            page.screenshot(path=str(screenshots / "desktop.png"))
            page.get_by_role("button", name="Coverage", exact=True).click()
            expect(page.get_by_role("dialog", name="Evidence coverage")).to_be_visible()
            page.get_by_role("button", name="Close coverage").click()
            page.get_by_role("button", name="View evidence for snapshot").first.click()
            expect(page.get_by_role("link", name="Retained bytes ↗").first).to_be_visible()
            files = page.locator(".source-file").count()
            page.get_by_role("button", name="Close evidence").click()
            # Selecting an older failure changes the target, and browser history
            # restores the investigation state in both directions.
            page.get_by_role("button", name="Use as target", exact=True).first.click()
            expect(page.locator(".target-label")).to_contain_text("selected failure")
            assert "execution_id=" in page.url
            page.go_back(wait_until="networkidle")
            expect(page.locator(".target-label")).to_contain_text("latest indexed failure")
            page.go_forward(wait_until="networkidle")
            expect(page.locator(".target-label")).to_contain_text("selected failure")
            page.get_by_role("button", name="Reset", exact=True).click()
            page.locator("#since").fill("2026-10-14")
            page.locator("#until").fill("2026-10-16")
            page.get_by_role("button", name="Apply", exact=True).click()
            expect(page.get_by_text("This testcase has failed before.", exact=True)).to_be_visible()
            assert "since=2026-10-14" in page.url and "until=2026-10-16" in page.url
            page.get_by_role("button", name="Reset", exact=True).click()
            page.locator("#outcome").select_option("PASS")
            page.get_by_role("button", name="Apply", exact=True).click()
            expect(
                page.get_by_text("No recorded failures in this result", exact=True)
            ).to_be_visible()
            page.get_by_role("button", name="Reset", exact=True).click()
            expect(page.get_by_text("This testcase has failed before.", exact=True)).to_be_visible()
            # The URL preserves testcase selection, suite, and unknown variant.
            saved = page.url
            page.reload(wait_until="networkidle")
            expect(page.get_by_text("This testcase has failed before.", exact=True)).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(screenshots / "mobile.png"), full_page=True)
            assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth"), (
                "mobile page overflows"
            )
            page.get_by_role("button", name="Coverage", exact=True).click()
            page.keyboard.press("Escape")
            expect(page.get_by_role("dialog", name="Evidence coverage")).not_to_be_visible()
            page.get_by_role("button", name="All testcases").click()
            page.locator("#failed-only").uncheck()
            expect(page.locator("#case-count")).to_have_text("1 testcase")
            page.get_by_role("searchbox").fill("no-such-testcase-chronicle")
            expect(page.get_by_text("No retained testcase matches", exact=True)).to_be_visible()
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "desktop": "passed",
                        "mobile": "passed",
                        "javascript_errors": errors,
                        "evidence_files": files,
                        "deep_link": saved,
                        "screenshots": str(screenshots),
                    },
                    indent=2,
                )
            )
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--lan", action="store_true", help="Verify access through a LAN interface")
    args = parser.parse_args()
    screenshots = args.screenshots or Path(tempfile.mkdtemp(prefix="tcc-browser-"))
    screenshots.mkdir(parents=True, exist_ok=True)
    server = create_server(args.data_dir, port=0, local=not args.lan)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = "127.0.0.1"
        if args.lan:
            address = next(
                (
                    value
                    for _, value in server.access_addresses
                    if not ipaddress.ip_address(value).is_loopback
                ),
                None,
            )
            if address is None:
                parser.error("--lan requires an active LAN or VPN address")
        host = f"[{address}]" if ":" in address else address
        check(f"http://{host}:{server.server_port}/?token={server.access_token}", screenshots)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


if __name__ == "__main__":
    main()
