# StockSmart — Small Business Inventory System

IT Capstone project. A web app that lets a small business track products, suppliers,
sales and deliveries, get low-stock alerts, see a dashboard, and ask an AI assistant
about stock in plain English.

## Features (MVP progress)

| Feature | Status |
| --- | --- |
| User accounts and roles (owner / employee) | ✅ API + login screen |
| Product management (add, edit, search, categories) | ✅ |
| Stock tracking (sales, deliveries, adjustments, full history) | ✅ |
| Suppliers and low-stock alerts | ✅ |
| Dashboard and CSV report | ✅ |
| AI inventory assistant | ✅ built-in mode; optional AI model mode |

## Tech stack

Python · FastAPI · SQLAlchemy · SQLite (dev) / PostgreSQL (deploy) · HTML/CSS/JavaScript front end (no build step) ·
OpenAI-compatible API (optional, OpenAI or local Ollama) · pytest · Playwright (screenshots) · Git/GitHub · Render (deploy)

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.seed          # demo data (first time only)
python main.py              # http://localhost:7860
```

Or just run `./run.sh` (macOS/Linux) or `run.bat` (Windows), which does all of the above.

Demo logins: `owner` / `owner1234` and `employee` / `employee1234`.
REST API docs: http://localhost:7860/api/docs

Optional environment variables:

- `OPENAI_API_KEY` — turns on AI model mode for the assistant (OpenAI)
- `OPENAI_BASE_URL` — use any OpenAI-compatible server instead, e.g. a free local model with Ollama: `http://localhost:11434/v1`
- `STOCKSMART_MODEL` — model name (default `gpt-4o-mini`; e.g. `llama3.1` for Ollama)
- `DATABASE_URL` — e.g. a PostgreSQL URL for deployment
- `SECRET_KEY` — signing key for API tokens (set this in production)
- `STOCKSMART_PUBLIC_URL` — the public address of this app (e.g. `https://stocksmart.onrender.com`), used for POS webhook URLs
- `POS_SYNC_MINUTES` — how often to check connected POS systems for new sales (default `5`, `0` turns it off)

The easier way to turn on AI is the **AI providers** page (see below). Without any provider, the Assistant page runs in built-in mode and routes simple
questions ("what's running low?", "sold 3 SNK-001") to the same tools.

## Tests

```bash
python -m pytest -q        # services, REST API, assistant, POS integrations
```

## Project layout

```
app/
  db.py           database engine and sessions
  models.py       User, Supplier, Product, StockMovement
  services.py     business rules (every stock change is logged in one transaction)
  auth.py         password hashing and signed tokens
  api.py          FastAPI REST endpoints (role checks, assistant)
  agent_tools.py  assistant tools + optional AI model loop
  seed.py         sample data
  deps.py         login / role checks shared by the API routes
  pos_api.py      REST endpoints for POS integrations (/api/pos/...)
  ai.py           AI provider presets and automatic fallback between them
  ai_api.py       REST endpoints for AI provider settings (/api/ai/...)
  pos/
    base.py       connector interface (PosAdapter) and shared types
    square.py     Square connector
    clover.py     Clover connector
    generic.py    any other POS: signed webhook or CSV import
    sync.py       applies POS sales to stock; pushes counts back out
frontend/
  index.html      single-page web app
  styles.css      styling (light and dark mode, mobile layout)
  app.js          pages: login, dashboard, products, record stock,
                  history, suppliers, assistant
main.py           runs API (/api) + web front end (/) together
tests/            pytest suite
```

## Known limitations (so far)

- Not deployed yet (planned: Render).
- The web front end hides owner-only actions (add/edit/delete products, adjustments, suppliers, CSV export) from employees; the REST API still enforces roles on every request.
- SQLite is used locally; PostgreSQL row locking is in place but not yet tested against Postgres.

## Web front end

The front end is plain HTML, CSS and JavaScript in `frontend/`, served by `main.py`
at `/`. It calls the REST API at `/api` with the login token.

| Page | What it does |
| --- | --- |
| Log in | Token login, one-click demo accounts |
| Dashboard | Reorder list with stock gauges and "Email supplier" links, store totals, top sellers |
| Products | Search, category and low-stock filters; owners add, edit, delete and export CSV |
| Record stock | Sale / delivery / adjustment with product search and a live "after" preview |
| History | Every stock change, filter by product or type |
| Suppliers | Contact details, product counts, items needing reorder; owners add suppliers |
| Assistant | Chat with the inventory assistant (`POST /api/assistant`) |
| Point of sale | Owners only: connect Square, Clover or any other POS (see below) |

Each stock gauge shows quantity as a bar; the dark mark is the reorder level and the bar
turns red at or below it.

API additions made for the front end: `GET /auth/me` (current user) and
`POST /assistant` (`{"message": "..."}` → `{"reply": "...", "mode": "built-in" | "ai"}`).

## Point-of-sale integrations

Owners connect POS systems under **Point of sale**. Several can run at once — for
example a Square register at the counter and Clover for online orders.

**How it works**

1. **Sales come in.** Each POS sale line becomes a stock movement (a refund adds stock back).
   StockSmart checks Square and Clover for new sales every few minutes (`POS_SYNC_MINUTES`),
   when you press *Sync sales now*, or instantly through a webhook if you set one up.
2. **Each sale is counted once.** Every sale line is stored with its POS order and line id, so
   a sale that arrives by webhook *and* by polling — or a CSV imported twice — is not double counted.
3. **Items are matched to products.** *Import items* loads the POS catalog and pairs items whose
   SKU matches a StockSmart SKU. Pick the rest by hand on the *Items* tab.
4. **Problems are held, not lost.** Sales for unmatched items, or sales larger than the stock on
   hand, wait on the *Activity* tab. Fix the match or the count, then press *Retry*.
5. **Stock goes back out (optional).** With *Send stock counts* on, StockSmart sets the stock level
   in that POS whenever a product changes — from a sale in another POS, a delivery, or an adjustment.
   That keeps every POS showing the same number.

Only sales made after a connection is added are imported, so existing stock isn't reduced twice.

### Square

1. At <https://developer.squareup.com/apps> create an application.
2. Copy the **access token** (Sandbox for testing, Production for real sales) and the
   **Location ID** (Locations page). The token needs `ORDERS_READ`, `ITEMS_READ`, and
   `INVENTORY_WRITE` if StockSmart should send stock counts.
3. In StockSmart: *Point of sale → Connect a POS → Square*, paste them, then press *Import items*.
4. Optional instant updates: in your Square app add a webhook subscription for **order.updated**,
   using the webhook URL shown on the connection's *Setup* tab, and paste the subscription's
   **signature key** into the connection settings. Square signs the exact URL, so set
   `STOCKSMART_PUBLIC_URL` to your public address.

### Clover

1. Find your **Merchant ID** (13 characters, in the Clover web dashboard URL).
2. Create an **API token**: Clover dashboard → *Account & Setup* → *API Tokens*. Allow Orders (read)
   and Inventory (read, plus write to send stock counts).
3. In StockSmart: *Point of sale → Connect a POS → Clover*, choose your region, paste them, then
   press *Import items*.
4. Optional instant updates: in a Clover app's webhook settings, use the webhook URL from the *Setup*
   tab and subscribe to Orders. When Clover sends its verification code, it appears on the
   connection; paste it back into Clover. Then copy the app's **Clover Auth Code** into the
   connection's *Webhook auth code* setting.

Clover records one line item per unit, so each line counts as one unit sold.

### Any other POS (Toast, Lightspeed, Shopify POS, SumUp, Zettle, …)

Use **Other POS**. Two options:

- **CSV import** — export sales from your POS and upload the file on the *Setup* tab. It needs a
  SKU column and a quantity column. Common names are recognised (`sku`, `code`, `upc`, `qty`,
  `units sold`, `order number`, `receipt`). Include an order or receipt number so a re-import
  never double counts. Negative quantities are refunds.
- **Webhook** — have the POS, or a no-code tool like Zapier, Make or n8n, POST each sale:

  ```http
  POST https://your-stocksmart/api/pos/webhooks/<id>
  Content-Type: application/json
  X-StockSmart-Signature: sha256=<hex HMAC-SHA256 of the raw body, keyed with the connection's secret>

  {"order_id": "1042", "lines": [{"sku": "COF-001", "quantity": 2}, {"sku": "SNK-002", "quantity": -1}]}
  ```

  Sign it in Python with
  `"sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()`.

### Adding a dedicated connector for another POS

Write one class in `app/pos/` and register it in `app/pos/__init__.py`. The settings form,
API, matching, de-duplication, retries and stock push all work automatically.

```python
class ToastAdapter(PosAdapter):
    provider, label = "toast", "Toast"
    description = "Toast restaurant POS."
    supports_polling = supports_catalog = True
    fields = [FieldSpec("access_token", "API token", secret=True),
              FieldSpec("restaurant_id", "Restaurant GUID")]

    def check(self) -> str: ...                     # confirm credentials
    def fetch_sales(self, since): ...               # -> ([SaleLine...], newest_timestamp)
    def list_catalog(self): ...                     # -> [CatalogItem...]
    def push_stock(self, counts): ...               # optional: set stock levels
    def verify_webhook(self, headers, body, url): ...  # optional: webhooks
    def handle_webhook(self, payload): ...
```

`tests/test_pos_adapters.py` shows how to test a connector with a fake HTTP client.

### Notes

- POS webhooks need StockSmart at a public HTTPS address (deployed, or through a tunnel such as
  ngrok). Polling works anywhere, including your own computer.
- API tokens are stored in the database. Keep the database private, and use a token with only the
  permissions listed above.
- Square and Clover rate-limit their APIs. The default 5-minute polling interval is well within
  their limits for a single store.

## AI assistant with free providers

Owners add AI services under **Assistant → AI providers**. Paste a free API key from each,
press **Load models** to pick a model, and put them in order. The assistant tries them in
order for each question:

- **A provider hits its free limit:** it rests (for the time the provider asks, about an hour for a
  daily limit) and the next one answers. The page shows which ones are resting.
- **A key is wrong or a model doesn't support tools:** it's skipped and marked *Problem*.
- **Every provider is unavailable:** the assistant answers in built-in mode.
- **Each reply shows which provider answered** ("Answered by Groq · llama-3.3-70b-versatile").
- **It remembers the last few messages,** so follow-ups like "sold 2 of those" work.

**It never records a sale twice.** If a provider records a sale or delivery and then fails
before finishing, StockSmart reports what was recorded and does not hand the question to the
next provider.

| Provider | Free key | Notes |
| --- | --- | --- |
| Google Gemini | <https://aistudio.google.com/apikey> | Generous daily limit. Outside the EU/UK, free-tier prompts may be used to improve Google's products. |
| Groq | <https://console.groq.com/keys> | Fast; open models such as Llama. |
| Cerebras | <https://cloud.cerebras.ai> | Fast; press *Load models* to choose. |
| OpenRouter | <https://openrouter.ai/keys> | Choose a model ending in `:free` that supports tools. |
| Mistral | <https://console.mistral.ai/api-keys> | The free plan requires agreeing to data training. |
| GitHub Models | GitHub token with the *Models* permission | Includes some OpenAI models. |
| Ollama | none | Runs on your own computer; unlimited and private. |
| Other | — | Any OpenAI-compatible service: enter its base URL. |

Free limits change often. **Test** on each provider checks that the key works *and* that the
model can use StockSmart's tools, without changing any stock. ChatGPT Plus or Claude
subscriptions don't include API access, so OpenAI and Anthropic models need a paid API key
(or, for some OpenAI models, GitHub Models). Use one free account per provider; creating
several to get around limits usually breaks providers' terms.

The `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `STOCKSMART_MODEL` environment variables still work
and are tried first.
