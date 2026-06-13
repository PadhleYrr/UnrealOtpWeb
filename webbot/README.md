# UnrealOTP — Web App (Grizzly SMS powered)

A full website version of the UnrealOTP bot — same features as the Telegram
bot (browse services, buy virtual numbers, receive OTPs, wallet/deposits,
order history, referral, API key) but as a normal web app with sign up/login.

## What changed from the bot

- **Provider**: now uses **Grizzly SMS** (`https://api.grizzlysms.com`,
  SMS-Activate-compatible `handler_api.php` protocol) instead of UOTP.
- **Storage**: SQLite (`unrealotp.db`) instead of MongoDB — simple, file-based,
  zero extra services to run.
- **Auth**: email + password, session cookie based.
- **Frontend**: the same `index.html` UI, but all mock data/JS replaced with
  real `fetch()` calls to the Flask backend.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set your Grizzly SMS API key (get it from grizzlysms.com dashboard):
   ```bash
   export GRIZZLY_API_KEY="your_real_api_key_here"
   ```

3. (Optional) Tune pricing/markup/country in `app.py`:
   - `MARKUP_INR` — flat ₹ added to every OTP price
   - `MARKUP_PCT` — percentage markup
   - `USD_TO_INR` — conversion rate from Grizzly's USD pricing to ₹
   - `DEFAULT_COUNTRY` — "22" = India (SMS-Activate/Grizzly country code)

4. Run:
   ```bash
   python app.py
   ```
   Visit http://localhost:5000

## API endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/register` | create account |
| POST | `/api/login` | sign in |
| POST | `/api/logout` | sign out |
| GET | `/api/me` | current user |
| POST | `/api/regen-key` | regenerate API key |
| GET | `/api/services?country=22` | live services + ₹ prices |
| GET | `/api/countries` | country list (raw from Grizzly) |
| GET | `/api/wallet` | balance + recent transactions |
| POST | `/api/deposit` | simulated deposit (wire to real gateway in prod) |
| POST | `/api/buy` | buy a number `{service_code, country}` |
| GET | `/api/order/<id>/status` | poll for OTP |
| POST | `/api/order/<id>/cancel` | cancel + refund |
| POST | `/api/order/<id>/resend` | request another code |
| GET | `/api/orders` | order history |

## Important: deposits

`/api/deposit` currently credits the wallet immediately on UTR submission —
this is a placeholder exactly like the original demo UI. For production,
replace this with a real payment gateway (Cashfree/Razorpay UPI, or
NOWPayments for crypto) and only credit the balance after the webhook
confirms payment.

## Notes on pricing

Grizzly returns prices in USD-equivalent via `getPricesV2`. The backend
converts to ₹ using `USD_TO_INR` and adds your markup (`MARKUP_INR` /
`MARKUP_PCT`) — so users only ever see the marked-up ₹ price, never the raw
provider cost.
