"""
scraper.py — Playwright scraper for dtek-krem.com.ua/ua/shutdowns

CONFIRMED DOM:
  #city     — city autocomplete (custom AJAX, two elements on page → always use .first)
  #street   — street autocomplete (disabled until city is clicked from dropdown)
  #house_num — building (custom dropdown, disabled until street is clicked)

  After address selection, result is either:
    A) Weekly schedule TABLE
    B) Yellow status box  → {"_status_message": "..."}
"""

import asyncio
import json
import logging
import time
from typing import Optional

from playwright.async_api import async_playwright, Page, Browser, BrowserContext

logger = logging.getLogger(__name__)

URL = "https://www.dtek-krem.com.ua/ua/shutdowns"

_playwright_inst = None
_browser: Optional[Browser] = None
_lock = asyncio.Lock()


async def get_browser() -> Browser:
    global _playwright_inst, _browser
    async with _lock:
        if _browser is None or not _browser.is_connected():
            _playwright_inst = await async_playwright().start()
            _browser = await _playwright_inst.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            logger.info("Chromium launched")
    return _browser


async def new_page() -> tuple[Page, BrowserContext]:
    browser = await get_browser()
    ctx = await browser.new_context(
        locale="uk-UA",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
    )
    page = await ctx.new_page()
    return page, ctx


async def dismiss_popup(page: Page) -> None:
    try:
        btn = page.locator(".modal__close").first
        await btn.wait_for(state="visible", timeout=4000)
        await btn.click()
        await page.wait_for_timeout(500)
        logger.info("Popup dismissed")
    except Exception:
        logger.info("No popup")


async def _wait_not_disabled(page: Page, selector: str, timeout: float = 8.0) -> None:
    """Poll until the element's disabled attribute is removed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        disabled = await page.evaluate(
            f'() => {{ const el = document.querySelector("{selector}"); return !el || el.disabled; }}'
        )
        if not disabled:
            return
        await page.wait_for_timeout(200)
    logger.warning("_wait_not_disabled timed out for %s", selector)


async def _type_and_click(page: Page, input_id: str, query: str, select_value: str = None, wait_ms: int = 1500) -> list[str]:
    """
    Combined: type query into #input_id, read dropdown items, then click the item
    matching select_value (if provided) — all in one go while dropdown is still open.

    Returns list of suggestion texts.
    select_value: the text to click (can be partial). If None, just returns suggestions.
    """
    inp = page.locator(f"#{input_id}").first
    await inp.wait_for(state="visible", timeout=8000)
    await inp.click()
    await inp.fill("")
    await inp.type(query, delay=80)
    await page.wait_for_timeout(wait_ms)

    inp_box = await inp.bounding_box()
    if not inp_box:
        return []

    ib = inp_box["y"] + inp_box["height"]  # bottom of input
    il = inp_box["x"]                       # left
    ir = inp_box["x"] + inp_box["width"]    # right

    # Get dropdown items with coordinates in one JS call
    items_with_coords = await page.evaluate(f"""
        () => {{
            const seen = new Set();
            const results = [];
            document.querySelectorAll("*").forEach(el => {{
                if (!el.offsetParent) return;
                const r = el.getBoundingClientRect();
                if (r.top < {ib - 10} || r.top > {ib + 400}) return;
                if (r.left < {il - 30} || r.right > {ir + 200}) return;
                const t = (el.innerText || "").trim();
                // Short single-line text only — skip table headers, multi-line blocks
                if (!t || t.length > 80 || t.includes("\\n")) return;
                const childText = Array.from(el.children).map(c=>(c.innerText||"").trim()).join("");
                if (childText === t) return;
                if (!seen.has(t)) {{
                    seen.add(t);
                    results.push({{text: t, cx: r.left + r.width/2, cy: r.top + r.height/2}});
                }}
            }});
            return results;
        }}
    """)

    # Filter out table time-slot headers (e.g. "00-01") and day names that leak in
    import re as _re
    _timeslot = _re.compile(r"^[0-9]{2}-[0-9]{2}$")
    _days = {"Понеділок","Вівторок","Середа","Четвер","П'ятниця","Субота","Неділя"}
    items_data = [
        i["text"] for i in items_with_coords
        if not _timeslot.match(i["text"]) and i["text"] not in _days
    ]
    logger.info("Dropdown #%s %r → %d: %s", input_id, query, len(items_data), items_data[:5])

    if select_value and items_with_coords:
        val_lower = select_value.lower()
        best = (
            next((i for i in items_with_coords if i["text"] == select_value), None) or
            next((i for i in items_with_coords if val_lower in i["text"].lower()), None) or
            items_with_coords[0]
        )
        # page.mouse.click fires a real pointer event — required to enable downstream inputs
        await page.mouse.click(best["cx"], best["cy"])
        logger.info("Mouse-clicked %r at (%.0f,%.0f) for #%s", best["text"], best["cx"], best["cy"], input_id)
        await page.wait_for_timeout(300)

    return items_data


async def _get_building_options(page: Page, query: str = "") -> list[str]:
    """
    #house_num is a text input with list="data_house" datalist.
    Type a prefix and read datalist options — there is no click-to-open dropdown.
    """
    inp = page.locator("#house_num").first
    try:
        await inp.click(timeout=3000)
        await inp.fill("")
        if query:
            await inp.type(query, delay=80)
        await page.wait_for_timeout(800)
    except Exception as e:
        logger.warning("house_num click/type failed: %s", e)
        return []

    opts = await page.evaluate("""
        () => {
            const dl = document.getElementById("data_house");
            if (dl && dl.options.length > 0)
                return Array.from(dl.options).map(o => o.value.trim()).filter(Boolean);
            // Fallback: read from input's list attribute
            const inp = document.querySelector("#house_num");
            if (!inp) return [];
            const listId = inp.getAttribute("list");
            const dl2 = listId ? document.getElementById(listId) : null;
            if (dl2) return Array.from(dl2.options).map(o => o.value.trim()).filter(Boolean);
            return [];
        }
    """)

    logger.info("Building datalist options for %r: %s", query, opts[:10])
    return opts


async def _select_building(page: Page, building: str, options: list[str]) -> None:
    """
    Type building number into #house_num (datalist input), then pick from datalist.
    Uses page.mouse.click on the matched datalist suggestion.
    """
    inp = page.locator("#house_num").first

    # Step 1: type the building number to trigger datalist
    try:
        await inp.click(timeout=3000)
        await inp.fill("")
        await inp.type(building, delay=80)
        await page.wait_for_timeout(800)
    except Exception as e:
        logger.warning("house_num type failed: %s", e)
        return

    # Step 2: read datalist options
    opts = await page.evaluate("""
        () => {
            const dl = document.getElementById("data_house");
            if (dl) return Array.from(dl.options).map(o => ({value: o.value.trim()})).filter(o => o.value);
            return [];
        }
    """)

    if opts:
        # Find best match
        bld_lower = building.lower()
        best = (
            next((o["value"] for o in opts if o["value"] == building), None) or
            next((o["value"] for o in opts if bld_lower in o["value"].lower()), None) or
            opts[0]["value"]
        )
        logger.info("Building best match from datalist: %r", best)

        # Fill with the exact datalist value and fire change event
        await inp.fill(best)
        await page.evaluate(
            """(v) => {
                const el = document.querySelector("#house_num");
                if (!el) return;
                el.value = v;
                ["input", "change"].forEach(ev =>
                    el.dispatchEvent(new Event(ev, {bubbles: true}))
                );
            }""",
            best
        )
        logger.info("Building set to: %r", best)
        # Press Enter to submit and trigger schedule load
        await inp.press("Enter")
    else:
        # No datalist — just set the value directly and press Enter
        logger.warning("No datalist options for building %r — setting directly", building)
        await inp.fill(building)
        await page.evaluate(
            """(v) => {
                const el = document.querySelector("#house_num");
                if (!el) return;
                el.value = v;
                ["input", "change"].forEach(ev =>
                    el.dispatchEvent(new Event(ev, {bubbles: true}))
                );
            }""",
            building
        )
        await inp.press("Enter")


async def _get_status_message(page: Page) -> Optional[str]:
    """
    Detect the yellow info box shown when no schedule exists for the address.
    We look for a div/section that:
      - contains relevant keywords
      - is NOT the main page wrapper (exclude elements with many children)
      - has reasonable text length (100-800 chars)
    """
    return await page.evaluate("""
        () => {
            const keywords = ["відсутн","стабіліз","аварійн","екстрен","оновлення інформації"];
            // Only look inside known wrapper classes for the result area
            const containers = document.querySelectorAll(
                "[class*=result], [class*=answer], [class*=info-block], [class*=warning], " +
                "[class*=alert], [class*=notice], [class*=shutdown], [class*=status]"
            );
            for (const el of containers) {
                if (!el.offsetParent) continue;
                // Must not be a huge container (page wrapper)
                if (el.children.length > 10) continue;
                const t = el.innerText.trim();
                if (t.length > 100 && t.length < 800 &&
                    keywords.some(k => t.toLowerCase().includes(k))) {
                    return t;
                }
            }
            // Fallback: any visible paragraph-level element with keyword + date mention
            for (const el of document.querySelectorAll("p, [class*=text]")) {
                if (!el.offsetParent) continue;
                const t = el.innerText.trim();
                if (t.length > 80 && t.length < 600 &&
                    keywords.some(k => t.toLowerCase().includes(k)) &&
                    t.includes("20")) {  // date year
                    return t;
                }
            }
            return null;
        }
    """)


async def _parse_today_schedule(page: Page) -> dict:
    """Click 'на сьогодні' with mouse.click, parse TABLE 0."""
    btn_info = await page.evaluate("""
        () => {
            const btns = Array.from(document.querySelectorAll("button, [role=tab], a"));
            const btn = btns.find(b => {
                if (!b.offsetParent) return false;
                // Own text only — not innerText which includes children
                const own = (b.childNodes[0] && b.childNodes[0].nodeValue || b.innerText || "")
                    .toLowerCase().trim();
                const inner = (b.innerText || "").toLowerCase().trim();
                // Must be short (tab button, not a container) and contain the keyword
                return inner.length < 50 && inner.includes("сьогодні") && !inner.includes("завтра");
            });
            if (!btn) return null;
            const r = btn.getBoundingClientRect();
            return {cx: r.left + r.width/2, cy: r.top + r.height/2, text: btn.innerText.trim()};
        }
    """)

    if btn_info:
        await page.mouse.click(btn_info["cx"], btn_info["cy"])
        logger.info("Mouse-clicked today button: %r", btn_info["text"][:40])
        await page.wait_for_timeout(1500)
    else:
        logger.warning("Today button not found")

    slots = await _read_table0(page)
    return {"today": slots} if slots else {}


async def _parse_tomorrow_schedule(page: Page) -> dict:
    """Find 'на завтра' button by position and mouse.click it, then parse TABLE 0."""
    # Find the button coordinates via JS, then use page.mouse.click (real pointer event)
    btn_info = await page.evaluate("""
        () => {
            const btns = Array.from(document.querySelectorAll("button, [role=tab], a, div, span"));
            const btn = btns.find(b => {
                if (!b.offsetParent) return false;
                const inner = (b.innerText || "").toLowerCase().trim();
                return inner.length < 50 && inner.includes("завтра") && !inner.includes("сьогодні");
            });
            if (!btn) return null;
            const r = btn.getBoundingClientRect();
            return {cx: r.left + r.width/2, cy: r.top + r.height/2, text: btn.innerText.trim()};
        }
    """)

    if not btn_info:
        logger.warning("Tomorrow button not found")
        return {}

    # Snapshot table HTML before click
    before = await page.evaluate("""
        () => {
            const t = document.querySelectorAll("table")[0];
            return t ? t.innerHTML : "";
        }
    """)

    await page.mouse.click(btn_info["cx"], btn_info["cy"])
    logger.info("Mouse-clicked tomorrow button: %r", btn_info["text"][:40])

    # Wait up to 5s for the table to actually change
    for _ in range(25):
        await page.wait_for_timeout(200)
        after = await page.evaluate("""
            () => {
                const t = document.querySelectorAll("table")[0];
                return t ? t.innerHTML : "";
            }
        """)
        if after != before:
            logger.info("Tomorrow table updated after %.1fs", (_ + 1) * 0.2)
            break
    else:
        logger.warning("Tomorrow table did not change after 5s")

    return await _read_table0(page)


async def _parse_today_tab(page: Page) -> dict:
    """Click 'на сьогодні' with mouse.click to restore today view."""
    btn_info = await page.evaluate("""
        () => {
            const btns = Array.from(document.querySelectorAll("button, [role=tab], a, div, span"));
            const btn = btns.find(b => {
                if (!b.offsetParent) return false;
                const inner = (b.innerText || "").toLowerCase().trim();
                return inner.length < 50 && inner.includes("сьогодні") && !inner.includes("завтра");
            });
            if (!btn) return null;
            const r = btn.getBoundingClientRect();
            return {cx: r.left + r.width/2, cy: r.top + r.height/2};
        }
    """)
    if btn_info:
        await page.mouse.click(btn_info["cx"], btn_info["cy"])
        await page.wait_for_timeout(1500)
    return await _read_table0(page)


async def _read_table0(page: Page) -> dict:
    """Read the VISIBLE single-row schedule table after a tab click.
    The site uses two hidden/shown tables — find the one that is actually visible.
    """
    return await page.evaluate("""
    () => {
        // Find the VISIBLE table — offsetParent != null and visible in viewport
        const tables = Array.from(document.querySelectorAll("table"));
        // The specific schedule tables have a single tbody row (today/tomorrow)
        // Pick the one that is currently visible (display != none, visibility != hidden)
        const visible = tables.find(t => {
            if (!t.offsetParent) return false;
            const style = window.getComputedStyle(t);
            if (style.display === "none" || style.visibility === "hidden") return false;
            const rows = t.querySelectorAll("tbody tr");
            // Single-row tables are the today/tomorrow specific schedules
            return rows.length === 1;
        });
        const t = visible || tables[0];
        if (!t) return {};

        const headerCells = t.querySelectorAll("thead th, thead td");
        const slots = [];
        headerCells.forEach((th, i) => { if (i > 0) slots.push(th.innerText.trim()); });
        const rows = t.querySelectorAll("tbody tr");
        let hours = {};
        rows.forEach(row => {
            const cells = row.querySelectorAll("td");
            if (!cells.length) return;
            for (let i = 1; i < cells.length; i++) {
                const cls = cells[i].className || "";
                let s;
                if      (cls.includes("cell-non-scheduled"))   s = "on";
                else if (cls.includes("cell-scheduled-maybe")) s = "maybe";
                else if (cls.includes("cell-first-half"))      s = "first30";
                else if (cls.includes("cell-second-half"))     s = "last30";
                else if (cls.includes("cell-scheduled"))       s = "off";
                else                                           s = "on";
                hours[slots[i-1] || `slot_${String(i).padStart(2,"0")}`] = s;
            }
        });
        return hours;
    }
    """)



async def _parse_schedule(page: Page) -> dict:
    """
    Parse the weekly schedule table.
    Confirmed CSS classes (from --cells debug):
      cell-non-scheduled   → on      (no outage)
      cell-scheduled-maybe → maybe   (possible outage)
      cell-scheduled       → off     (definite outage)
      cell-first-half      → first30 (outage in first 30 min)
      cell-second-half     → last30  (outage in last 30 min)
      current-day          → row marker, ignore
    """
    return await page.evaluate("""
    () => {
        const schedule = {};
        const headerCells = document.querySelectorAll("table thead th, table thead td");
        const slots = [];
        headerCells.forEach((th, i) => {
            if (i > 0) slots.push(th.innerText.trim() || `slot_${String(i).padStart(2,"0")}`);
        });

        document.querySelectorAll("table tbody tr").forEach(row => {
            const cells = row.querySelectorAll("td");
            if (!cells.length) return;
            const day = cells[0].innerText.trim();
            if (!day) return;
            const hours = {};
            for (let i = 1; i < cells.length; i++) {
                const cls = cells[i].className || "";
                let s;
                if      (cls.includes("cell-non-scheduled"))   s = "on";
                else if (cls.includes("cell-scheduled-maybe")) s = "maybe";
                else if (cls.includes("cell-first-half"))      s = "first30";
                else if (cls.includes("cell-second-half"))     s = "last30";
                else if (cls.includes("cell-scheduled"))       s = "off";
                else                                           s = "on";
                hours[slots[i-1] || `slot_${String(i).padStart(2,"0")}`] = s;
            }
            if (Object.keys(hours).length > 0) schedule[day] = hours;
        });
        return schedule;
    }
    """)


# ─────────────────────────── public API ──────────────────────────────────────

async def search_cities(query: str) -> list[str]:
    page, ctx = await new_page()
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await dismiss_popup(page)
        return await _type_and_click(page, "city", query)
    except Exception as e:
        logger.error("search_cities: %s", e)
        return []
    finally:
        await ctx.close()


async def search_streets(city: str, street_query: str) -> list[str]:
    page, ctx = await new_page()
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await dismiss_popup(page)

        city_items = await _type_and_click(page, "city", city, select_value=city)
        await _wait_not_disabled(page, "#street")
        await page.wait_for_timeout(300)

        return await _type_and_click(page, "street", street_query)
    except Exception as e:
        logger.error("search_streets: %s", e)
        return []
    finally:
        await ctx.close()


async def get_building_list(city: str, street: str) -> list[str]:
    page, ctx = await new_page()
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await dismiss_popup(page)

        await _type_and_click(page, "city", city, select_value=city)
        await _wait_not_disabled(page, "#street")
        await page.wait_for_timeout(300)

        await _type_and_click(page, "street", street, select_value=street)
        await _wait_not_disabled(page, "#house_num")
        await page.wait_for_timeout(300)

        return await _get_building_options(page, query="")
    except Exception as e:
        logger.error("get_building_list: %s", e)
        return []
    finally:
        await ctx.close()


async def get_schedule(city: str, street: str, building: str) -> Optional[dict]:
    """
    Returns:
      {"_status_message": "..."} — yellow info box (no schedule for this address)
      {day: {hour: status}}     — schedule grid
      None                      — on error
    """
    page, ctx = await new_page()
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await dismiss_popup(page)

        await _type_and_click(page, "city", city, select_value=city)
        await _wait_not_disabled(page, "#street")
        await page.wait_for_timeout(300)

        await _type_and_click(page, "street", street, select_value=street)
        await _wait_not_disabled(page, "#house_num")
        await page.wait_for_timeout(300)

        building_opts = await _get_building_options(page)
        await _select_building(page, building, building_opts)
        await page.wait_for_timeout(3000)  # wait for schedule to reload

        # Parse today and tomorrow — use mouse.click for both tabs
        today_data    = await _parse_today_schedule(page)
        tomorrow_data = await _parse_tomorrow_schedule(page)
        # Switch back to today tab
        await _parse_today_tab(page)

        logger.info("Today slots: %d, Tomorrow slots: %d",
                    len(today_data.get("today", {})), len(tomorrow_data))

        # Scrape queue number (e.g. "Черга 4.1") shown next to building field
        queue = await page.evaluate("""
            () => {
                const all = Array.from(document.querySelectorAll("*"));
                const el = all.find(e => {
                    if (!e.offsetParent) return false;
                    const t = (e.innerText || "").trim();
                    return t.startsWith("Черга") && t.length < 20 && e.children.length === 0;
                });
                return el ? el.innerText.trim() : null;
            }
        """)
        if queue:
            logger.info("Queue: %s", queue)

        result = {}
        if today_data.get("today"):
            result["_today"]    = today_data["today"]
        if tomorrow_data:
            result["_tomorrow"] = tomorrow_data
        if queue:
            result["_queue"] = queue
        return result if result else None

    except Exception as e:
        logger.error("get_schedule: %s", e)
        return None
    finally:
        await ctx.close()


async def dump_cell_classes(city: str, street: str, building: str) -> None:
    """Debug helper — print all CSS classes/colors in table after address selection."""
    page, ctx = await new_page()
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await dismiss_popup(page)

        await _type_and_click(page, "city", city, select_value=city)
        await _wait_not_disabled(page, "#street")
        await page.wait_for_timeout(300)

        await _type_and_click(page, "street", street, select_value=street)
        await _wait_not_disabled(page, "#house_num")
        await page.wait_for_timeout(300)

        building_opts = await _get_building_options(page)
        await _select_building(page, building, building_opts)
        await page.wait_for_timeout(2500)

        info = await page.evaluate("""
        () => {
            const seen = {};
            document.querySelectorAll("table tbody tr td").forEach(td => {
                const k = (td.className||"(none)") + " | " + window.getComputedStyle(td).backgroundColor;
                seen[k] = (seen[k]||0) + 1;
            });
            return seen;
        }
        """)
        print("\n=== CELL CLASSES ===")
        for k, n in sorted(info.items(), key=lambda x: -x[1]):
            cls, bg = k.split(" | ", 1)
            print(f"  {n:4}x  {cls!r:45}  {bg}")
    finally:
        await ctx.close()
