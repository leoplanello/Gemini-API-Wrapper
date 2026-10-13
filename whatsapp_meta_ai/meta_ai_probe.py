"""Prototype: measure Meta AI reply latency through the WhatsApp Beta desktop WebView2 (CDP on 127.0.0.1:9444).

Only the "Meta AI" chat is touched: the script looks that chat up by its title, never lists or reads other chats.
  python meta_ai_probe.py --inspect          # check that the Meta AI chat + composer are reachable (no message sent)
  python meta_ai_probe.py "your question"    # send one question, print time-to-first-text and time-to-complete
"""
import sys, time, json
from playwright.sync_api import sync_playwright

PORT = 9444
COMPOSER = 'footer div[contenteditable="true"], div[contenteditable="true"][data-tab="10"], div[role="textbox"][contenteditable="true"]'
INCOMING = 'div.message-in, [data-id^="false_"]'


def find_page(browser):
    for ctx in browser.contexts:
        for pg in ctx.pages:
            if "web.whatsapp.com" in pg.url or "whatsapp" in pg.url.lower():
                return pg
    pages = [pg for ctx in browser.contexts for pg in ctx.pages]
    return pages[0] if pages else None


def open_meta_ai(page):
    # Look the chat up by title only (no enumeration of other chats).
    item = page.locator('span[title="Meta AI"]').first
    if item.count() == 0:
        # fall back to the search box
        box = page.locator('div[contenteditable="true"][data-tab="3"], [aria-label*="Search" i] [contenteditable="true"]').first
        box.click(); box.fill("Meta AI"); page.wait_for_timeout(800)
        item = page.locator('span[title="Meta AI"]').first
    if item.count() == 0:
        raise SystemExit("Meta AI chat not found in the chat list")
    item.click(); page.wait_for_timeout(600)


def last_incoming_text(page):
    return page.evaluate("""(sel) => {
        const els = Array.from(document.querySelectorAll(sel));
        const last = els[els.length - 1];
        return last ? (last.innerText || '').trim() : '';
    }""", INCOMING)


def send(page, text):
    box = page.locator(COMPOSER).last
    box.click()
    page.keyboard.insert_text(text)
    page.keyboard.press("Enter")


def main():
    inspect = len(sys.argv) > 1 and sys.argv[1] == "--inspect"
    question = None if inspect else (sys.argv[1] if len(sys.argv) > 1 else "Reply with one word: ok")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        page = find_page(browser)
        if not page:
            raise SystemExit("no WhatsApp page found")
        print("page url:", page.url[:60])
        open_meta_ai(page)
        print("composer found:", page.locator(COMPOSER).count() > 0,
              "| incoming-message nodes:", page.locator(INCOMING).count())
        if inspect:
            return
        before = last_incoming_text(page)
        t0 = time.time(); send(page, question)
        first = None; last = ""; stable_since = None
        while time.time() - t0 < 90:
            page.wait_for_timeout(100)
            cur = last_incoming_text(page)
            if cur and cur != before:
                if first is None:
                    first = time.time() - t0
                if cur != last:
                    last = cur; stable_since = time.time()
                elif time.time() - stable_since > 1.5:
                    break
        print(json.dumps({"first_text_s": None if first is None else round(first, 2),
                          "complete_s": round(time.time() - t0 - (1.5 if first else 0), 2),
                          "reply": last[:200]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
