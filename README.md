# Vyrobot — staking / trading platform (Django)

Real auth, real ledger, real admin. Balances only move when money moves.

---

## 1. Run it locally (before you buy a domain)

You need Python 3.11+ installed. In a terminal:

```bash
# from inside the project folder (the one with manage.py)
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

export DJANGO_DEBUG=True           # Windows: set DJANGO_DEBUG=True
python manage.py migrate
python manage.py createsuperuser    # <-- this is your admin login (see §3)
python manage.py runserver
```

Then open:

- **User site:**  http://127.0.0.1:8000/  — sign up, then log in.
- **Admin panel:** http://127.0.0.1:8000/admin/  — log in with the superuser you just created.

There is no `index.html`. This is a Django app, not a folder of static pages:
the entry point is the `/` route, which renders the login/signup page from
`templates/auth.html`. The server builds each page.

While `DJANGO_DEBUG=True`, the user dashboard shows a **dev test faucet** so you
can give yourself test balance and try stake / withdraw / claim. It disappears
automatically when DEBUG is off.

---

## 2. The two front-end pages

- `templates/auth.html` — login + signup (keeps the blue hero + TradingView chart look).
- `templates/dashboard.html` — the logged-in user view: balance, staked, reward,
  deposit, withdraw, stake, claim, emergency withdraw, connect wallet, history.

You can restyle these freely; they're plain Django templates.

---

## 3. Your admin login — where it comes from

You do **not** hardcode an admin username/password in the code. Hardcoding
credentials in source is insecure (anyone who sees the code gets in). Django
stores admin accounts in the database, hashed.

Create yours with:

```bash
python manage.py createsuperuser
```

It will ask for a username, email, and password — those become your
`/admin/` login. Make more staff accounts the same way, or promote a user in
the admin (tick "Staff status").

If you later forget it:

```bash
python manage.py changepassword <username>
```

---

## 4. What you can do in the admin

The admin (`/admin/`) is your control panel over every user:

- **Users** — Django's built-in Users list. Click any user to see/edit their
  account. Each user has a **Wallet** (their balance, staked, reward, address)
  and a list of **Transactions** (their full history).
- **Wallets** — view balances. Note: the balance fields are read-only here on
  purpose. You change a balance through a logged action, not by typing over a
  number (see Adjustments).
- **Transactions** — every deposit, withdrawal, stake, claim, adjustment. Select
  rows and use the Actions dropdown:
  - *Confirm selected PENDING deposits* → credits the balance. Run this only for
    money you actually received.
  - *Mark selected PENDING withdrawals as PAID* → run after you've sent the payout.
  - *Reject selected PENDING withdrawals* → refunds the amount to the user.
- **Reward pool** — set `funded` to the amount you've set aside for staking
  rewards. Claims can never pay out more than this.
- **Adjustments** — the audited way to correct a balance by hand. Add a row
  (pick the user, credit/debit, amount, and a required reason). It applies once,
  atomically, and writes a matching ledger entry showing who did it and why.
  Use it for real corrections — a payment your gateway missed, a refund, a fee
  reversal. It is not a way to show a user money they never funded; a credit is
  money you actually owe them and must be able to pay out.

Everything you do in the admin reflects on the user's dashboard immediately
(their balance, their pending/approved/rejected status, their history).

---

## 5. The user flow (how it behaves)

- **Sign up → log in.** Passwords are hashed; login verifies them.
- **Deposit** creates a *pending* request. It is **not** credited until a real
  payment is confirmed — by your gateway webhook, or by you in the admin. The
  user sees it sitting as "Pending".
- **Withdraw** debits the user's available balance immediately and creates a
  pending payout. You mark it Paid once sent, or Reject to refund (the user sees
  the rejected status and can request again).
- **Stake / Unstake** move balance in and out of staking.
- **Claim** moves accrued reward into the balance, capped by the funded Reward
  pool.
- **Emergency withdraw** returns staked principal at once and forfeits pending
  reward.
- **Connect wallet** stores an address read-only as a payout destination.

---

## 6. Plug in your real payment gateway

In `accounts/views.py`, `deposit()` records the pending request. Add a webhook
view your gateway (Flutterwave / Paystack / Korapay, or on-chain) calls when a
payment settles. Have it find the pending `Transaction` by `reference`, credit
the wallet inside an atomic block (mirror the admin `confirm_deposits` action),
and mark it confirmed. Until then you can confirm deposits manually in the admin.

---

## 7. Payment gateway (NOWPayments) — real deposits

This is how a user's "Add Fund" turns into a real, credited balance.

1. In the admin, open **Payment gateway (NOWPayments)** and enter your
   **API key** and **IPN secret** (both from your NOWPayments dashboard), tick
   **Enabled**, and leave **Sandbox** on while testing.
2. In your NOWPayments dashboard, set the **IPN callback URL** to
   `https://YOUR-DOMAIN/gateway/nowpayments-ipn/`.
3. On the user dashboard, **Add Fund** now sends the user to the NOWPayments
   checkout. When they pay, NOWPayments calls your IPN URL; the app verifies the
   callback's signature and, only if it's genuine and the payment is *finished*,
   credits the user's balance and marks the deposit confirmed.

Notes:
- The IPN only reaches you on a public URL, so on `localhost` the callback won't
  arrive — test the full flow once deployed, or use the admin to confirm a
  pending deposit while developing. NOWPayments also has a sandbox for testing.
- A deposit is credited **only** by a signature-verified, finished callback.
  Forged or replayed callbacks are rejected / ignored — so balances track real
  payments.
- `accounts/gateway.py` holds the invoice call and signature check. Reference:
  https://nowpayments.io/doc

## 8. Migrating balances from v1 → v2

This copies the **real** balances recorded in v1 into v2. The numbers come from
a v1 export file (your source of truth), not from anything typed per user.

1. Export the real v1 balances into one file (CSV or JSON). CSV example:

   ```
   email,full_name,balance,staked,v1_ref
   jane@example.com,Jane Doe,1500.00,0,U1001
   sam@example.com,Sam O,230.50,100,U1002
   ```
   `email` and `balance` are required; `full_name`, `staked`, `v1_ref` optional.
   `v1_ref` (the v1 user/record id) stops a row being migrated twice.

2. Preview first (writes nothing):
   ```
   python manage.py migrate_v1 v1_export.csv
   ```
3. Apply it:
   ```
   python manage.py migrate_v1 v1_export.csv --commit
   ```

Each migrated balance becomes an audited ledger entry (kind "Migrated from v1")
carrying its v1 reference, and shows on the user's dashboard. Re-running skips
rows already migrated. New accounts are created with no password — those users
sign in via password reset, or you set one.

See `accounts/management/commands/migrate_v1.py` for the full format notes.


## 9. Going live (when you have a domain)

Set real environment variables — don't ship the dev fallbacks:

```
DJANGO_SECRET_KEY=<random 50+ chars>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=vyrobot.io,www.vyrobot.io
DJANGO_CSRF_TRUSTED_ORIGINS=https://vyrobot.io,https://www.vyrobot.io
```

Use PostgreSQL (set `DATABASE_URL`, `pip install dj-database-url`), run
`python manage.py collectstatic`, serve behind HTTPS. With DEBUG off the faucet
is gone and secure-cookie / HSTS settings switch on automatically.
# vyrobot-project
# vyrobot-project
# vyrobot-project
