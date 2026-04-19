# Hotel Rate Finder

Compare standard and corporate rates across **Hilton**, **Marriott**, and **Hyatt** in real time.

![Hotel Rate Finder](https://images.unsplash.com/photo-1555881400-74d7acaacd8b?w=800&q=80)

## Features

- Search hotels by city for any date range
- Compare standard rates vs. corporate/negotiated rates side by side
- Supports Hilton, Marriott, and Hyatt
- Live prices scraped directly from each chain's website
- One-click booking links with dates and corporate code pre-applied

## Corporate code examples

| Chain | Code | Company |
|-------|------|---------|
| Hilton | `deloitte`, `pwc`, `google` | Various |
| Marriott | `dtc`, `eyc`, `mck` | Deloitte, EY, McKinsey |
| Hyatt | `20725`, `35466`, `NC22008` | Deloitte, McKinsey, Bain |

## Requirements

- Python 3.11+
- Google Chrome installed (used for browser automation)

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/michael01810/hotel-rate-finder.git
cd hotel-rate-finder

# 2. Create a virtual environment (optional but recommended)
python -m venv venv
source venv/bin/activate      # macOS/Linux
venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the server
python -m uvicorn hilton_app:app --port 8001

# 5. Open your browser
# Go to http://localhost:8001
```

## How it works

- **Hilton**: Intercepts the `shopMultiPropAvail` GraphQL API via Chrome DevTools Protocol
- **Marriott**: Loads `findHotels.mi` search page and extracts prices from the DOM
- **Hyatt**: Loads the Hyatt search page and extracts prices from hotel cards

Each search opens a Chrome window (visible) to load the hotel chain's website. Corporate rates are fetched by reloading the search with the corporate code applied.

## Notes

- Chrome windows will open and close automatically during each search — this is normal
- Searches take 30–90 seconds depending on how many corporate codes you enter
- Prices are live from each chain's website and shown in the hotel's local currency
