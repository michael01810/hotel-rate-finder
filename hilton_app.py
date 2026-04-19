import csv
import json
import asyncio
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import zendriver as zd
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse
from scrapling.fetchers import Fetcher

CORP_CODES: list[dict] = []
_csv = Path(__file__).parent / "hilton_preferred_rates.csv"
if _csv.exists():
    with open(_csv, newline="", encoding="utf-8") as f:
        CORP_CODES = list(csv.DictReader(f))

app = FastAPI()

AUTOCOMPLETE = "https://www.hilton.com/dx-customer/autocomplete?input={q}&language=en"
SEARCH_URL = (
    "https://www.hilton.com/en/search/"
    "?query={q}&arrivalDate={arrival}&departureDate={departure}"
    "&numRooms=1&numAdults=1&numChildren=0&room1ChildAges=&room1AdultAges="
)
BOOKING = (
    "https://www.hilton.com/en/book/reservation/rooms/"
    "?ctyhocn={ctyhocn}&arrivalDate={arrival}&departureDate={departure}&room1NumAdults=1"
)

# Extract hotel cards from the search results DOM.
# Each card has an anchor linking to /en/hotels/{ctyhocn}-{slug}/.
_DOM_EXTRACT_JS = """
(() => {
    const results = [];
    const seen = new Set();
    document.querySelectorAll('a[href*="/en/hotels/"]').forEach(a => {
        const m = a.href.match(/\\/en\\/hotels\\/([a-z0-9]{5,8})-/i);
        if (!m) return;
        const ctyhocn = m[1].toUpperCase();
        if (seen.has(ctyhocn)) return;
        seen.add(ctyhocn);
        const container = a.closest('article') || a.closest('li') ||
            a.closest('[class*="card"]') || a.closest('[class*="property"]') ||
            a.closest('[class*="Property"]') || a.parentElement;
        const nameEl = container && (
            container.querySelector('h2') || container.querySelector('h3') ||
            container.querySelector('[class*="name"]') || container.querySelector('[class*="Name"]') ||
            container.querySelector('[class*="title"]') || container.querySelector('[class*="Title"]')
        );
        const name = (nameEl?.innerText || a.innerText || '').trim().split('\\n')[0].trim();
        if (name && name.length > 3 && name.length < 120) {
            results.push({ctyhocn, name, url: a.href});
        }
    });
    return results;
})()
"""


def _fetch_json(url: str):
    page = Fetcher.get(url, stealthy_headers=True)
    try:
        return json.loads(page.get_all_text(ignore_tags=("script", "style")))
    except Exception:
        return None


async def _search_with_prices(
    search_query: str, arrival: str, departure: str, corp_codes: list[str] | None = None
) -> list[dict]:
    base_url = SEARCH_URL.format(
        q=quote(search_query), arrival=arrival, departure=departure
    )

    # current_phase tracks which search is active so the single handler routes correctly
    current_phase: list = [None]  # None = standard; str = corp code name
    pending_rids: dict[str, str | None] = {}  # request_id -> phase at capture time

    browser = await zd.start(headless=False)
    try:
        tab = await browser.get("about:blank")
        await tab.send(zd.cdp.network.enable())

        def on_response(event):
            if not isinstance(event, zd.cdp.network.ResponseReceived):
                return
            url = event.response.url
            if "graphql" in url and "shopMultiPropAvail" in url:
                pending_rids[event.request_id] = current_phase[0]

        tab.add_handler(zd.cdp.network.ResponseReceived, on_response)

        async def collect_phase(search_url: str) -> dict[str, dict]:
            """Navigate, wait, drain the rids that appeared during this phase."""
            seen_before = set(pending_rids)
            await tab.get(search_url)
            await asyncio.sleep(12)
            new_rids = [r for r in pending_rids if r not in seen_before]
            prices: dict[str, dict] = {}
            for rid in new_rids:
                try:
                    result = await tab.send(zd.cdp.network.get_response_body(request_id=rid))
                    body = result[0] if isinstance(result, tuple) else str(result)
                    data = json.loads(body)
                    for item in data.get("data", {}).get("shopMultiPropAvail", []):
                        if item.get("ctyhocn"):
                            prices[item["ctyhocn"]] = item
                except Exception:
                    pass
            return prices

        # Standard search
        current_phase[0] = None
        std_prices = await collect_phase(base_url)

        hotel_meta: dict[str, dict] = {}
        try:
            dom_result = await tab.evaluate(_DOM_EXTRACT_JS)
            if isinstance(dom_result, list):
                for card in dom_result:
                    cty = card.get("ctyhocn")
                    if cty and cty not in hotel_meta:
                        hotel_meta[cty] = {"name": card.get("name"), "url": card.get("url")}
        except Exception:
            pass

        # One additional search per corp code
        corp_data: dict[str, dict[str, dict]] = {}
        for code in (corp_codes or []):
            current_phase[0] = code
            corp_data[code] = await collect_phase(base_url + f"&pnd={quote(code)}")
    finally:
        await browser.stop()

    results = []
    for ctyhocn, price_data in std_prices.items():
        summary = price_data.get("summary", {})
        if summary.get("status", {}).get("type") != "AVAILABLE":
            continue
        lowest = summary.get("lowest") or {}
        meta = hotel_meta.get(ctyhocn, {})

        corp_rates = []
        for code in (corp_codes or []):
            item = corp_data.get(code, {}).get(ctyhocn)
            if not item:
                continue
            s = item.get("summary", {})
            if s.get("status", {}).get("type") != "AVAILABLE":
                continue
            lo = s.get("lowest") or {}
            amt = lo.get("rateAmount")
            if amt is not None:
                corp_rates.append({
                    "code": code,
                    "price": amt,
                    "price_fmt": lo.get("rateAmountFmt"),
                })
        corp_rates.sort(key=lambda x: x["price"])

        results.append({
            "ctyhocn": ctyhocn,
            "name": meta.get("name") or ctyhocn,
            "hotel_url": meta.get("url") or f"https://www.hilton.com/en/hotels/{ctyhocn.lower()}/",
            "price": lowest.get("rateAmount"),
            "price_fmt": lowest.get("rateAmountFmt"),
            "currency": price_data.get("currencyCode"),
            "corp_rates": corp_rates,
        })

    results.sort(key=lambda h: h["price"] or 9_999_999)
    return results


# ---------------------------------------------------------------------------
# Marriott
# ---------------------------------------------------------------------------

NOMINATIM = "https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1"

# ---------------------------------------------------------------------------
# Hyatt
# ---------------------------------------------------------------------------

HYATT_SEARCH = (
    "https://www.hyatt.com/search/hotels/en-US/{location}"
    "?checkinDate={ci}&checkoutDate={co}&rooms=1&adults=1&kids=0&corp_id={corp_id}"
)
HYATT_BOOK = (
    "https://www.hyatt.com/shop/rooms/{code}"
    "?checkinDate={ci}&checkoutDate={co}&rooms=1&adults=1&kids=0&corp_id={corp_id}"
)


def _hyatt_url(location: str, arrival: str, departure: str, corp_id: str = "standard") -> str:
    return HYATT_SEARCH.format(
        location=quote(location, safe=""),
        ci=arrival, co=departure,
        corp_id=quote(str(corp_id)),
    )


_HYATT_DOM_JS = """
(() => {
    const results = [];
    const seen = new Set();
    document.querySelectorAll('a[href]').forEach(a => {
        const m = a.href.match(/[/]shop[/]rooms[/]([a-z0-9]+)[?]/i);
        if (!m) return;
        const code = m[1].toUpperCase();
        if (seen.has(code)) return;
        seen.add(code);

        let card = a.parentElement;
        for (let i = 0; i < 14; i++) {
            if (!card) break;
            if (card.querySelector('[class*="HotelCard_header_redesign_list"]')) break;
            card = card.parentElement;
        }
        if (!card) return;

        const nameEl = card.querySelector('[class*="HotelCard_header_redesign_list"], [class*="HotelCard_header"]');
        const name = nameEl ? nameEl.innerText.trim() : code;

        let price = null, currency = 'USD';
        card.querySelectorAll('*').forEach(el => {
            if (price !== null || el.children.length > 0) return;
            const cls = el.className.toString();
            if (cls.includes('be-text-section-1') && cls.includes('be-text-on-light')) {
                const raw = (el.innerText || '').trim();
                const sym = raw.match(/^([^0-9]+)/);
                if (sym) {
                    const s = sym[1].trim();
                    if (s === '\u00a5' || s === 'JPY') currency = 'JPY';
                    else if (s === '\u20ac' || s === 'EUR') currency = 'EUR';
                    else if (s === '\u00a3' || s === 'GBP') currency = 'GBP';
                    else currency = 'USD';
                }
                const num = parseFloat(raw.replace(/[^0-9.]/g, ''));
                if (num > 0) price = num;
            }
        });

        const cleanUrl = a.href.split('&hpesrId')[0];
        results.push({code, name, price, currency, url: cleanUrl});
    });
    return results;
})()
"""


async def _search_hyatt_with_prices(
    location: str, arrival: str, departure: str,
    corp_codes: list[str] | None = None,
) -> list[dict]:
    browser = await zd.start(headless=False)
    try:
        tab = await browser.get("about:blank")

        async def collect_phase(url: str) -> dict[str, dict]:
            await tab.get(url)
            await asyncio.sleep(14)
            try:
                await tab.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await asyncio.sleep(3)
                await tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(3)
            except Exception:
                pass
            try:
                items = await tab.evaluate(_HYATT_DOM_JS)
                if isinstance(items, list):
                    return {item["code"]: item for item in items if item.get("code")}
            except Exception:
                pass
            return {}

        std_prices = await collect_phase(_hyatt_url(location, arrival, departure))

        corp_data: dict[str, dict[str, dict]] = {}
        for code in (corp_codes or []):
            corp_data[code] = await collect_phase(_hyatt_url(location, arrival, departure, code))
    finally:
        await browser.stop()

    results = []
    for prop_code, hdata in std_prices.items():
        rate = hdata.get("price")
        currency = hdata.get("currency", "USD")
        book_url = HYATT_BOOK.format(
            code=prop_code.lower(), ci=arrival, co=departure, corp_id="standard"
        )
        hotel_url = hdata.get("url") or book_url

        corp_rates = []
        for code in (corp_codes or []):
            cd = corp_data.get(code, {}).get(prop_code)
            if cd and cd.get("price") is not None:
                corp_rates.append({
                    "code": code,
                    "price": cd["price"],
                    "price_fmt": None,
                    "book_url": HYATT_BOOK.format(
                        code=prop_code.lower(), ci=arrival, co=departure,
                        corp_id=quote(str(code)),
                    ),
                })
        corp_rates.sort(key=lambda x: x["price"])

        results.append({
            "code": prop_code,
            "name": hdata.get("name") or prop_code,
            "hotel_url": hotel_url,
            "book_url": book_url,
            "price": rate,
            "price_fmt": None,
            "currency": currency,
            "corp_rates": corp_rates,
            "corp_links": [],
        })

    results.sort(key=lambda h: h["price"] or 9_999_999)
    return results


# {ci}/{co} = MM/DD/YYYY, {dest} = URL-encoded city, {lat}/{lon} = floats
MARRIOTT_SEARCH = (
    "https://www.marriott.com/search/findHotels.mi"
    "?fromDate={ci}&toDate={co}"
    "&destinationAddress.destination={dest}"
    "&destinationAddress.address={dest}"
    "&destinationAddress.latitude={lat}"
    "&destinationAddress.longitude={lon}"
    "&numberOfRooms=1&numAdultsPerRoom=1&childrenCount=0"
    "&lengthOfStay=1&recordsPerPage=40"
    "&isTransient=true&view=list&isSearch=true"
    "&initialRequest=true&deviceType=desktop-web"
    "&t-start={arrival}&t-end={departure}"
    "&clusterCode={cluster}{corp_suffix}"
)

MARRIOTT_BOOKING = (
    "https://www.marriott.com/reservation/availabilitySearch.mi"
    "?marshaCode={code}&fromDate={ci}&toDate={co}&numberOfRooms=1&numAdultsPerRoom=1"
)

# Extracts hotel code, name, URL, and price from Marriott's rendered DOM.
_MARRIOTT_DOM_JS = """
(() => {
    const results = [];
    const seen = new Set();

    function getCode(href) {
        if (!href) return null;
        let m = href.match(/[/]hotels[/]travel[/]([a-z0-9]{5})-/i);
        if (m) return m[1].toUpperCase();
        m = href.match(/[/]hotels[/]([a-z0-9]{5})-/i);
        if (m) return m[1].toUpperCase();
        m = href.match(/[/]hotel-rooms[/]([a-z0-9]{5})-/i);
        if (m) return m[1].toUpperCase();
        return null;
    }

    document.querySelectorAll('a[href]').forEach(a => {
        const href = a.href || '';
        const code = getCode(href);
        if (!code || seen.has(code)) return;
        seen.add(code);

        const card = a.closest('[class*="property-card"]') || a.closest('[class*="property-card-container"]')
                  || a.closest('[class*="card"]') || a.closest('[class*="property"]')
                  || a.closest('article') || a.closest('li') || a.parentElement;

        const nameEl = card && (
            card.querySelector('[class*="t-subtitle"]') || card.querySelector('h2') ||
            card.querySelector('h3') || card.querySelector('[class*="name"]')
        );
        const name = (nameEl?.innerText || '').trim().split('\\n')[0].trim();
        if (!name || name.length <= 3 || name.length >= 120) return;

        // Marriott renders price as <span class="m-price">21,850</span>
        // with currency in <span class="idr-currency-label">JPY / Night</span>
        let price = null;
        const priceEl = card && card.querySelector('[class*="m-price"]');
        if (priceEl) {
            price = parseFloat((priceEl.innerText || '').replace(/,/g, '').trim());
            if (isNaN(price)) price = null;
        }

        const currEl = card && card.querySelector('[class*="currency-label"]');
        const currency = currEl ? (currEl.innerText || '').split('/')[0].trim() : 'USD';

        results.push({code, name, url: href, price, currency});
    });

    return results;
})()
"""


def _geocode(location: str) -> dict | None:
    """Return {lat, lon, display} using Nominatim (stdlib only, no API key needed)."""
    try:
        req = urllib.request.Request(
            NOMINATIM.format(q=quote(location)),
            headers={"User-Agent": "HotelRateFinder/1.0 (personal use)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data:
            return {"lat": data[0]["lat"], "lon": data[0]["lon"],
                    "display": data[0].get("display_name", location)}
    except Exception:
        pass
    return None


def _marriott_url(lat: str, lon: str, dest: str, arrival: str, departure: str,
                  corp_code: str | None = None) -> str:
    ci = datetime.strptime(arrival, "%Y-%m-%d").strftime("%m/%d/%Y")
    co = datetime.strptime(departure, "%Y-%m-%d").strftime("%m/%d/%Y")
    cluster = "corp" if corp_code else "none"
    corp_suffix = f"&corporateCode={quote(corp_code)}" if corp_code else ""
    return MARRIOTT_SEARCH.format(
        ci=ci, co=co, dest=quote(dest, safe=""),
        lat=lat, lon=lon, arrival=arrival, departure=departure,
        cluster=cluster, corp_suffix=corp_suffix,
    )


def _parse_marriott_response(body: str) -> dict[str, dict]:
    """
    Recursively search a Marriott JSON response for arrays of hotel objects
    (identified by presence of a marshaCode/propertyCode field).
    Returns {marshaCode: {code, name, url, rate, rate_fmt}}.
    """
    try:
        data = json.loads(body)
    except Exception:
        return {}

    CODE_KEYS = ("marshaCode", "marshacode", "propertyCode", "hotelCode", "code")
    RATE_KEYS = ("lowestAveragePrice", "lowestRate", "startingRateAmount",
                 "averagePricePerNight", "price", "rate", "startingRate")

    def extract_rate(h: dict) -> tuple[float | None, str | None]:
        for k in RATE_KEYS:
            v = h.get(k)
            if isinstance(v, (int, float)):
                return float(v), None
            if isinstance(v, dict):
                amt = v.get("amount") or v.get("value") or v.get("rate") or v.get("price")
                fmt = v.get("formattedAmount") or v.get("display") or v.get("formatted")
                if isinstance(amt, (int, float)):
                    return float(amt), fmt
        return None, None

    def find_hotels(obj, depth: int = 0) -> dict[str, dict]:
        if depth > 6:
            return {}
        if isinstance(obj, list) and obj:
            first = obj[0] if isinstance(obj[0], dict) else None
            if first and any(k in first for k in CODE_KEYS):
                result = {}
                for h in obj:
                    if not isinstance(h, dict):
                        continue
                    code = next((h[k] for k in CODE_KEYS if h.get(k)), None)
                    if not code:
                        continue
                    rate, rate_fmt = extract_rate(h)
                    name = (h.get("name") or h.get("hotelName") or h.get("propertyName") or code)
                    url = (h.get("propertyDetailsUrl") or h.get("hotelUrl") or
                           h.get("detailPageUrl") or "")
                    if url and not url.startswith("http"):
                        url = "https://www.marriott.com" + url
                    result[code.upper()] = {
                        "code": code.upper(), "name": name, "url": url,
                        "rate": rate, "rate_fmt": rate_fmt,
                        "currency": h.get("currency") or h.get("currencyCode") or "USD",
                    }
                if result:
                    return result
        if isinstance(obj, dict):
            combined = {}
            for v in obj.values():
                combined.update(find_hotels(v, depth + 1))
            return combined
        return {}

    return find_hotels(data)


async def _search_marriott_with_prices(
    lat: str, lon: str, dest: str, arrival: str, departure: str,
    corp_codes: list[str] | None = None,
) -> list[dict]:
    ci = datetime.strptime(arrival, "%Y-%m-%d").strftime("%m/%d/%Y")
    co = datetime.strptime(departure, "%Y-%m-%d").strftime("%m/%d/%Y")

    pending_rids: dict[str, None] = {}

    browser = await zd.start(headless=False)
    try:
        tab = await browser.get("about:blank")
        await tab.send(zd.cdp.network.enable())

        _STATIC = (".js", ".css", ".png", ".jpg", ".gif", ".svg",
                   ".woff", ".woff2", ".ttf", ".ico", ".webp")

        def on_response(event):
            if not isinstance(event, zd.cdp.network.ResponseReceived):
                return
            url = event.response.url
            if "marriott.com" not in url:
                return
            if any(url.split("?")[0].endswith(ext) for ext in _STATIC):
                return
            pending_rids[event.request_id] = None

        tab.add_handler(zd.cdp.network.ResponseReceived, on_response)

        async def collect_phase(search_url: str) -> dict[str, dict]:
            seen_before = set(pending_rids)
            await tab.get(search_url)
            await asyncio.sleep(10)

            # Scroll to trigger lazy-loaded hotel cards
            try:
                await tab.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await asyncio.sleep(2)
                await tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(3)
            except Exception:
                pass

            combined: dict[str, dict] = {}

            _SPURIOUS_CODES = {"SUCCESS", "ERROR", "LOGIN", "CLOSE", "HOTEL", "BRAND", "CHAIN"}

            # --- API responses first ---
            new_rids = [r for r in pending_rids if r not in seen_before]
            for rid in new_rids:
                try:
                    result = await tab.send(zd.cdp.network.get_response_body(request_id=rid))
                    body = result[0] if isinstance(result, tuple) else str(result)
                    parsed = _parse_marriott_response(body)
                    combined.update(parsed)
                except Exception:
                    pass

            try:
                dom_items = await tab.evaluate(_MARRIOTT_DOM_JS)
                if isinstance(dom_items, list):
                    for item in dom_items:
                        code = item.get("code", "").upper()
                        if not code or code in _SPURIOUS_CODES:
                            continue
                        if code not in combined:
                            combined[code] = {
                                "code": code,
                                "name": item.get("name") or code,
                                "url": item.get("url") or "",
                                "rate": item.get("price"),
                                "rate_fmt": None,
                                "currency": item.get("currency") or "USD",
                            }
                        elif combined[code].get("rate") is None and item.get("price") is not None:
                            combined[code]["rate"] = item["price"]
                            combined[code]["currency"] = item.get("currency") or combined[code].get("currency") or "USD"
            except Exception:
                pass

            return combined

        # Standard search
        std_prices = await collect_phase(_marriott_url(lat, lon, dest, arrival, departure))

        corp_data: dict[str, dict[str, dict]] = {}
        for code in (corp_codes or []):
            corp_data[code] = await collect_phase(
                _marriott_url(lat, lon, dest, arrival, departure, code)
            )
    finally:
        await browser.stop()

    results = []
    for prop_code, hdata in std_prices.items():
        rate = hdata.get("rate")  # may be None — still include the hotel
        hotel_url = hdata.get("url") or f"https://www.marriott.com/hotels/travel/{prop_code.lower()}/"

        book_base = f"{hotel_url}?fromDate={ci}&toDate={co}&numberOfRooms=1&numAdultsPerRoom=1"

        corp_rates = []
        for code in (corp_codes or []):
            cd = corp_data.get(code, {}).get(prop_code)
            if cd and cd.get("rate") is not None:
                corp_rates.append({
                    "code": code,
                    "price": cd["rate"],
                    "price_fmt": cd.get("rate_fmt"),
                    "book_url": book_base + f"&clusterCode=corp&corporateCode={quote(code)}",
                })
        corp_rates.sort(key=lambda x: x["price"])

        results.append({
            "code": prop_code,
            "name": hdata.get("name") or prop_code,
            "hotel_url": hotel_url,
            "book_url": book_base,
            "price": rate,
            "price_fmt": hdata.get("rate_fmt"),
            "currency": hdata.get("currency", "USD"),
            "corp_rates": corp_rates,
            "corp_links": [],
        })

    results.sort(key=lambda h: h["price"] or 9_999_999)
    return results


# ---------------------------------------------------------------------------


def _parse_corp_codes(codes: str) -> list[str]:
    result = []
    for part in codes.split(","):
        code = part.strip().split()[0].strip("()") if part.strip() else ""
        if code:
            result.append(code)
    return result


async def run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


@app.get("/")
def index():
    return HTMLResponse((Path(__file__).parent / "hilton_app.html").read_text("utf-8"))


@app.get("/api/codes")
def codes():
    return [c["Name"] for c in CORP_CODES]


@app.get("/api/search")
async def search(location: str, arrival: str, departure: str, codes: str = ""):
    filter_codes = {c.strip().lower() for c in codes.split(",") if c.strip()}
    use_codes = [c for c in CORP_CODES if c["Name"].lower() in filter_codes] if filter_codes else []

    async def stream():
        try:
            yield {"event": "status", "data": json.dumps({"message": f"Looking up \"{location}\"..."})}

            ac_data = await run(_fetch_json, AUTOCOMPLETE.format(q=quote(location)))
            if not ac_data or ac_data.get("status") != "OK":
                yield {"event": "error", "data": json.dumps({"message": f"Could not find location: {location}"})}
                return

            predictions = ac_data.get("predictions", [])
            if not predictions:
                yield {"event": "error", "data": json.dumps({"message": f"No results for: {location}"})}
                return

            pred = next(
                (p for p in predictions if p.get("type") in ("geocode", "locality") and p.get("address", {}).get("city")),
                predictions[0]
            )
            addr = pred.get("address", {})
            city = addr.get("city", "")
            country = addr.get("countryName", "")
            display_loc = f"{city+', ' if city else ''}{country or location}"
            search_query = pred.get("description") or city or location

            corp_code_names = [c["Name"] for c in use_codes] if filter_codes else []
            status_suffix = f" + {len(corp_code_names)} corp code{'s' if len(corp_code_names) != 1 else ''}" if corp_code_names else ""
            yield {"event": "status", "data": json.dumps({"message": f"Fetching live Hilton rates for {display_loc}{status_suffix}..."})}

            hotels = await _search_with_prices(search_query, arrival, departure, corp_code_names or None)

            if not hotels:
                yield {"event": "error", "data": json.dumps({"message": f"No available rates found for {display_loc}."})}
                return

            for h in hotels:
                book_url = BOOKING.format(ctyhocn=h["ctyhocn"], arrival=arrival, departure=departure)
                corp_links = [
                    {"code": c["Name"], "link": book_url + f"&pnd={c['Name']}"}
                    for c in use_codes
                ]
                yield {
                    "event": "hotel_found",
                    "data": json.dumps({
                        "ctyhocn": h["ctyhocn"],
                        "name": h["name"],
                        "hotel_url": h["hotel_url"],
                        "book_url": book_url,
                        "price": h["price"],
                        "price_fmt": h.get("price_fmt"),
                        "currency": h.get("currency"),
                        "corp_links": corp_links,
                        "corp_rates": h.get("corp_rates", []),
                        "filtered": bool(filter_codes),
                    })
                }
                await asyncio.sleep(0)

            yield {
                "event": "done",
                "data": json.dumps({"total_hotels": len(hotels), "codes_checked": len(use_codes)})
            }

        except Exception as e:
            yield {"event": "error", "data": json.dumps({"message": str(e)})}

    return EventSourceResponse(stream())


@app.get("/api/search/marriott")
async def search_marriott(location: str, arrival: str, departure: str, codes: str = ""):
    corp_codes = _parse_corp_codes(codes)

    async def stream():
        try:
            yield {"event": "status", "data": json.dumps({"message": f"Looking up \"{location}\"..."})}

            geo = await run(_geocode, location)
            if not geo:
                yield {"event": "error", "data": json.dumps({"message": f"Could not geocode: {location}"})}
                return

            display_loc = geo["display"].split(",")[0] + ", " + geo["display"].split(",")[-1].strip()
            suffix = f" + {len(corp_codes)} corp code{'s' if len(corp_codes) != 1 else ''}" if corp_codes else ""
            yield {"event": "status", "data": json.dumps({"message": f"Fetching Marriott rates for {display_loc}{suffix}..."})}

            hotels = await _search_marriott_with_prices(
                geo["lat"], geo["lon"], location, arrival, departure, corp_codes or None
            )

            if not hotels:
                yield {"event": "error", "data": json.dumps({"message": f"No available Marriott rates found for {display_loc}."})}
                return

            for h in hotels:
                yield {
                    "event": "hotel_found",
                    "data": json.dumps({
                        "ctyhocn": h["code"],
                        "name": h["name"],
                        "hotel_url": h["hotel_url"],
                        "book_url": h["book_url"],
                        "price": h["price"],
                        "price_fmt": h.get("price_fmt"),
                        "currency": h.get("currency"),
                        "corp_links": [],
                        "corp_rates": h.get("corp_rates", []),
                        "filtered": bool(corp_codes),
                    })
                }
                await asyncio.sleep(0)

            yield {"event": "done", "data": json.dumps({"total_hotels": len(hotels), "codes_checked": len(corp_codes)})}

        except Exception as e:
            yield {"event": "error", "data": json.dumps({"message": str(e)})}

    return EventSourceResponse(stream())


@app.get("/api/search/hyatt")
async def search_hyatt(location: str, arrival: str, departure: str, codes: str = ""):
    corp_codes = _parse_corp_codes(codes)

    async def stream():
        try:
            suffix = f" + {len(corp_codes)} corp code{'s' if len(corp_codes) != 1 else ''}" if corp_codes else ""
            yield {"event": "status", "data": json.dumps({"message": f"Fetching Hyatt rates for {location}{suffix}..."})}

            hotels = await _search_hyatt_with_prices(location, arrival, departure, corp_codes or None)

            if not hotels:
                yield {"event": "error", "data": json.dumps({"message": f"No available Hyatt rates found for {location}."})}
                return

            for h in hotels:
                yield {
                    "event": "hotel_found",
                    "data": json.dumps({
                        "ctyhocn": h["code"],
                        "name": h["name"],
                        "hotel_url": h["hotel_url"],
                        "book_url": h["book_url"],
                        "price": h["price"],
                        "price_fmt": h.get("price_fmt"),
                        "currency": h.get("currency"),
                        "corp_links": [],
                        "corp_rates": h.get("corp_rates", []),
                        "filtered": bool(corp_codes),
                    })
                }
                await asyncio.sleep(0)

            yield {"event": "done", "data": json.dumps({"total_hotels": len(hotels), "codes_checked": len(corp_codes)})}

        except Exception as e:
            yield {"event": "error", "data": json.dumps({"message": str(e)})}

    return EventSourceResponse(stream())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
