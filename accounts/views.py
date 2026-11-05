import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction as db_tx
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import gateway
from .forms import LoginForm, SignupForm
from .models import GatewaySettings, RewardPool, Transaction, Wallet, ZERO

MIN_AMOUNT = Decimal("0.00000001")


def _is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _fig(w):
    return {
        "balance": f"{w.balance:.2f}",
        "staked": f"{w.staked:.2f}",
        "reward": f"{w.reward_accrued:.2f}",
    }


def _respond(request, level, message, wallet=None):
    """For AJAX, return JSON (no page reload). Otherwise flash + redirect."""
    if _is_ajax(request):
        data = {"ok": level != "error", "level": level, "message": message}
        if wallet is not None:
            data["wallet"] = _fig(wallet)
        return JsonResponse(data)
    getattr(messages, level if level in ("error", "success", "info", "warning") else "info")(request, message)
    return redirect("dashboard")


def _parse_amount(raw):
    try:
        amt = Decimal(str(raw)).quantize(Decimal("0.00000001"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if amt < MIN_AMOUNT:
        return None
    return amt


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def auth_view(request):
    """Combined login + signup page (same look as the original)."""
    if request.user.is_authenticated:
        return redirect("dashboard")

    login_form = LoginForm()
    signup_form = SignupForm()
    active = "signup" if request.GET.get("tab") == "signup" else "login"

    if request.method == "POST":
        which = request.POST.get("form")
        if which == "signup":
            active = "signup"
            signup_form = SignupForm(request.POST)
            if signup_form.is_valid():
                signup_form.save()
                messages.success(request, "Account created. Please log in.")
                return redirect("auth")
        else:
            login_form = LoginForm(request.POST)
            if login_form.is_valid():
                ident = login_form.cleaned_data["identifier"].strip()
                password = login_form.cleaned_data["password"]
                # Resolve the identifier to an account by username OR email,
                # then verify the hashed password. No "accept anything" shortcut.
                from django.contrib.auth.models import User
                account = (
                    User.objects.filter(username__iexact=ident).first()
                    or User.objects.filter(email__iexact=ident).first()
                )
                user = None
                if account is not None:
                    user = authenticate(request, username=account.username, password=password)
                if user is not None:
                    login(request, user)
                    return redirect("dashboard")
                messages.error(request, "Invalid login or password.")

    return render(request, "auth.html", {
        "login_form": login_form,
        "signup_form": signup_form,
        "active": active,
    })

def home(request):
    return render(request, "index.html", {
        "apr_pct": settings.STAKING_APR * 100,
        "stake_days": settings.STAKE_DAY_OPTIONS,
    })

def connect(request):
    return render(request, "connect.html")

def import_view(request):
    return render(request, "import.html")

def logout_view(request):
    logout(request)
    return redirect("auth")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@login_required
def dashboard(request):
    wallet = request.user.wallet
    # Fold in reward accrued since last visit so the figure shown is current.
    with db_tx.atomic():
        w = Wallet.objects.select_for_update().get(pk=wallet.pk)
        w.accrue()
        w.save(update_fields=["reward_accrued", "last_accrued_at"])
        wallet = w

    pool = RewardPool.get()
    # Hide not-yet-successful gateway deposits: an Add Fund only shows in history
    # once the payment is confirmed.
    txns = (
        request.user.transactions
        .exclude(kind=Transaction.ADD_FUND, status=Transaction.PENDING)[:25]
    )
    cfg = GatewaySettings.get()
    return render(request, "dashboard.html", {
        "wallet": wallet,
        "pool": pool,
        "apr_pct": settings.STAKING_APR * 100,
        "transactions": txns,
        "debug": settings.DEBUG,
        "gateway_enabled": bool(cfg.enabled and cfg.api_key),
        "stake_days_options": settings.STAKE_DAY_OPTIONS,
        "networks": settings.WITHDRAW_NETWORKS,
        "emergency_pct": settings.EMERGENCY_RETURN_PCT,
        "penalty_pct": 100 - settings.EMERGENCY_RETURN_PCT,
        "is_locked": wallet.is_locked,
        "has_stake": wallet.has_active_stake,
        "unlock_at": wallet.unlock_at,
    })


# ---------------------------------------------------------------------------
# Deposit request -> records a PENDING request for an admin to confirm.
# No gateway, nothing credited here. (The manual path, alongside Add Fund.)
# ---------------------------------------------------------------------------
@login_required
@require_POST
def deposit(request):
    amt = _parse_amount(request.POST.get("amount"))
    if amt is None:
        return _respond(request, "error", "Enter a valid amount.")

    Transaction.objects.create(
        user=request.user,
        kind=Transaction.DEPOSIT,
        amount=amt,
        status=Transaction.PENDING,
        reference=uuid.uuid4().hex[:16],
        note="Deposit request — awaiting admin confirmation.",
    )
    return _respond(
        request, "info",
        "Request received. An admin will review and confirm it. Your balance "
        "updates only after the request is confirmed.")


# ---------------------------------------------------------------------------
# Add fund -> sends the user to the NOWPayments checkout for a real payment.
# The balance is credited later, by the IPN webhook, only when NOWPayments
# confirms the payment settled. Nothing is credited here.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def add_fund(request):
    amt = _parse_amount(request.POST.get("amount"))
    if amt is None:
        messages.error(request, "Enter a valid amount.")
        return redirect("dashboard")

    cfg = GatewaySettings.get()
    if not (cfg.enabled and cfg.api_key):
        # No gateway configured: do not create any history row. The front-end
        # modal already tells the user it's unavailable; this is just a guard.
        messages.error(request, "Add Fund is unavailable right now. Please contact admin.")
        return redirect("dashboard")

    order_id = uuid.uuid4().hex[:16]
    # A pending ADD_FUND row holds the order so the IPN can match it to this
    # user. It is hidden from the user's history until the payment confirms.
    txn = Transaction.objects.create(
        user=request.user,
        kind=Transaction.ADD_FUND,
        amount=amt,
        status=Transaction.PENDING,
        reference=order_id,
        note="Awaiting payment confirmation.",
    )

    if True:
        ipn_url = request.build_absolute_uri(reverse("nowpayments_ipn"))
        success = request.build_absolute_uri(reverse("dashboard"))
        try:
            invoice_url, invoice_id = gateway.create_invoice(
                amount=amt,
                order_id=order_id,
                ipn_url=ipn_url,
                success_url=success,
                cancel_url=success,
                description=f"Vyrobot deposit for {request.user.email}",
            )
            if invoice_id:
                txn.note = f"NOWPayments invoice {invoice_id}"
                txn.save(update_fields=["note"])
            return redirect(invoice_url)
        except RuntimeError as e:
            txn.note = f"Gateway error: {e}"
            txn.save(update_fields=["note"])
            messages.error(
                request,
                "Could not start the payment right now. Please try again or "
                "contact support.",
            )
            return redirect("dashboard")


# ---------------------------------------------------------------------------
# NOWPayments IPN webhook. Credits a deposit ONLY when the callback's
# signature verifies and the payment status is finished. CSRF-exempt because
# it's a server-to-server call from NOWPayments, not a browser form.
# ---------------------------------------------------------------------------
@csrf_exempt
@require_POST
def nowpayments_ipn(request):
    sig = request.headers.get("x-nowpayments-sig", "")
    if not gateway.verify_ipn_signature(request.body, sig):
        return HttpResponseBadRequest("bad signature")

    import json
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("bad body")

    order_id = data.get("order_id", "")
    status = (data.get("payment_status") or "").lower()

    # Only a fully settled payment credits the balance.
    if status == "finished":
        with db_tx.atomic():
            txn = (
                Transaction.objects.select_for_update()
                .filter(kind=Transaction.ADD_FUND, reference=order_id,
                        status=Transaction.PENDING)
                .first()
            )
            if txn:  # idempotent: nothing happens if already confirmed
                w = Wallet.objects.select_for_update().get(user=txn.user)
                w.balance += txn.amount
                w.save(update_fields=["balance"])
                txn.status = Transaction.CONFIRMED
                txn.note = f"Paid via NOWPayments ({data.get('payment_id', '')})."
                txn.save(update_fields=["status", "note"])

    elif status in ("partially_paid", "failed", "expired", "refunded"):
        # Underpaid / wrong coin / failed: do NOT credit automatically. Record
        # what NOWPayments actually received so an operator can reconcile it in
        # Manage wallet, crediting the real amount received.
        txn = Transaction.objects.filter(
            kind=Transaction.ADD_FUND, reference=order_id,
            status=Transaction.PENDING,
        ).first()
        if txn:
            actually_paid = data.get("actually_paid") or data.get("pay_amount") or "?"
            pay_cur = data.get("pay_currency", "")
            txn.note = (
                f"NOWPayments status '{status}': received {actually_paid} {pay_cur}. "
                f"Not auto-credited — reconcile manually if the payment is real."
            )
            txn.save(update_fields=["note"])

    # Acknowledge receipt for every valid, signed callback.
    return HttpResponse("ok")


def _q(d):
    return Decimal(d).quantize(Decimal("0.00000001"))


# ---------------------------------------------------------------------------
# Withdraw -> choose coin/network + address; debits balance now, pending payout.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def withdraw(request):
    amt = _parse_amount(request.POST.get("amount"))
    if amt is None:
        return _respond(request, "error", "Enter a valid amount.")
    coin = (request.POST.get("coin") or "").strip()
    address = (request.POST.get("address") or "").strip()
    if coin not in settings.WITHDRAW_NETWORKS:
        return _respond(request, "error", "Choose a withdrawal network.")
    if not address:
        return _respond(request, "error", "Paste the wallet address to withdraw to.")

    with db_tx.atomic():
        w = Wallet.objects.select_for_update().get(user=request.user)
        if w.balance <= 0:
            return _respond(request, "error", "You have no balance to withdraw.")
        if amt > w.balance:
            return _respond(request, "error", "Amount is more than your available balance.")
        w.balance -= amt
        w.save(update_fields=["balance"])
        Transaction.objects.create(
            user=request.user,
            kind=Transaction.WITHDRAW,
            amount=amt,
            status=Transaction.PENDING,
            reference=uuid.uuid4().hex[:16],
            coin=coin,
            to_address=address,
            note=f"Payout of {amt} to {coin} address.",
        )
    return _respond(
        request, "success",
        "Withdrawal requested. Your balance was debited and the payout is "
        "pending.", wallet=w)


# ---------------------------------------------------------------------------
# Stake -> lock the amount for a chosen period (days). One active stake.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def stake(request):
    amt = _parse_amount(request.POST.get("amount"))
    if amt is None:
        return _respond(request, "error", "Enter a valid amount.")
    try:
        days = int(request.POST.get("days", "0"))
    except (TypeError, ValueError):
        days = 0
    if days not in settings.STAKE_DAY_OPTIONS:
        return _respond(request, "error", "Choose a lock period (14, 21 or 28 days).")

    with db_tx.atomic():
        w = Wallet.objects.select_for_update().get(user=request.user)
        if w.balance <= 0:
            return _respond(request, "error", "You have no balance to stake.")
        if amt > w.balance:
            return _respond(request, "error", "Amount is more than your available balance.")
        now = timezone.now()
        w.balance -= amt
        if w.has_active_stake:
            # Add to the existing stake and shift the unlock to the new period.
            w.accrue()  # settle reward on the old principal up to now
            w.staked += amt
        else:
            w.staked = amt
            w.reward_accrued = ZERO
        w.stake_days = days
        w.staked_at = now
        w.unlock_at = now + timedelta(days=days)
        w.last_accrued_at = now
        w.save()
        Transaction.objects.create(
            user=request.user, kind=Transaction.STAKE, amount=amt,
            status=Transaction.CONFIRMED,
            note=f"Staked {amt}; total {w.staked} locked {days} days; unlocks {w.unlock_at:%Y-%m-%d %H:%M} UTC.",
        )
    return _respond(
        request, "success",
        f"Staked. Total staked is now {w.staked}, locked {days} days — unlocks "
        f"{w.unlock_at:%Y-%m-%d %H:%M} UTC.", wallet=w)


# ---------------------------------------------------------------------------
# Recompound -> restart the same lock period and roll funded reward into stake.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def recompound(request):
    with db_tx.atomic():
        w = Wallet.objects.select_for_update().get(user=request.user)
        if not w.has_active_stake:
            return _respond(request, "error", "You have no active stake to recompound.", wallet=w)
        pool = RewardPool.objects.select_for_update().get_or_create(pk=1)[0]
        w.accrue()
        # Only funded reward can be rolled into principal.
        rolled = min(w.reward_accrued, pool.funded)
        if rolled > ZERO:
            w.reward_accrued -= rolled
            w.staked += rolled
            pool.funded -= rolled
            pool.save(update_fields=["funded"])
        now = timezone.now()
        w.staked_at = now
        w.unlock_at = now + timedelta(days=w.stake_days)
        w.last_accrued_at = now
        w.save()
        Transaction.objects.create(
            user=request.user, kind=Transaction.STAKE, amount=rolled,
            status=Transaction.CONFIRMED,
            note=f"Recompounded: +{_q(rolled)} reward; relocked {w.stake_days} days.",
        )
    msg = f"Re-staked for {w.stake_days} days."
    if rolled > ZERO:
        msg += f" Added {_q(rolled)} reward to your stake."
    return _respond(request, "success", msg, wallet=w)


# ---------------------------------------------------------------------------
# Unstake -> only after the lock completes; returns full principal.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def unstake(request):
    with db_tx.atomic():
        w = Wallet.objects.select_for_update().get(user=request.user)
        if not w.has_active_stake:
            return _respond(request, "error", "You have nothing staked to unstake.", wallet=w)
        if w.is_locked:
            return _respond(
                request, "error",
                f"Your stake is still locked until {w.unlock_at:%Y-%m-%d %H:%M} UTC. "
                f"Use Emergency Withdraw to exit early (with a penalty).", wallet=w)
        w.accrue()
        principal = w.staked
        w.balance += principal
        w.staked = ZERO
        w.stake_days = 0
        w.staked_at = None
        w.unlock_at = None
        w.save()
        Transaction.objects.create(
            user=request.user, kind=Transaction.UNSTAKE, amount=principal,
            status=Transaction.CONFIRMED,
            note="Lock complete; principal returned.",
        )
    return _respond(
        request, "success",
        "Unstaked. Principal returned to your balance. Any reward is claimable.",
        wallet=w)


# ---------------------------------------------------------------------------
# Claim reward -> only after the lock completes; capped by the funded pool.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def claim(request):
    with db_tx.atomic():
        w = Wallet.objects.select_for_update().get(user=request.user)
        if w.is_locked:
            return _respond(
                request, "error",
                "Stake period is not complete. Wait until staking completes to "
                "claim your reward.", wallet=w)
        pool = RewardPool.objects.select_for_update().get_or_create(pk=1)[0]
        w.accrue()

        claimable = min(w.reward_accrued, pool.funded)
        if claimable < MIN_AMOUNT:
            w.save(update_fields=["reward_accrued", "last_accrued_at"])
            if w.reward_accrued >= MIN_AMOUNT:
                return _respond(
                    request, "error",
                    "Reward pool is currently unfunded, so there is nothing to "
                    "pay out right now. Your accrued reward is preserved.", wallet=w)
            return _respond(request, "info", "No reward to claim yet.", wallet=w)

        w.reward_accrued -= claimable
        w.balance += claimable
        pool.funded -= claimable
        w.save(update_fields=["balance", "reward_accrued", "last_accrued_at"])
        pool.save(update_fields=["funded"])
        Transaction.objects.create(
            user=request.user, kind=Transaction.CLAIM, amount=claimable,
            status=Transaction.CONFIRMED,
        )
    return _respond(request, "success", "Reward claimed to your balance.", wallet=w)


# ---------------------------------------------------------------------------
# Emergency withdraw -> early exit before unlock: return EMERGENCY_RETURN_PCT%
# of principal, forfeit the rest and the pending reward. After unlock it simply
# returns the full principal.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def emergency_withdraw(request):
    with db_tx.atomic():
        w = Wallet.objects.select_for_update().get(user=request.user)
        if not w.has_active_stake:
            return _respond(request, "info", "You have nothing staked.", wallet=w)
        w.accrue()
        principal = w.staked
        locked = w.is_locked
        forfeited_reward = w.reward_accrued

        if locked:
            pct = Decimal(settings.EMERGENCY_RETURN_PCT)
            returned = _q(principal * pct / Decimal(100))
            penalty = _q(principal - returned)
            note = (
                f"Early exit: returned {returned} ({settings.EMERGENCY_RETURN_PCT}%), "
                f"penalty {penalty} ({100 - settings.EMERGENCY_RETURN_PCT}%) in USD, "
                f"reward {_q(forfeited_reward)} cancelled."
            )
            msg = (
                f"Emergency withdrawal done. {returned} returned to your balance; "
                f"{penalty} penalty deducted and reward cancelled."
            )
        else:
            returned = principal
            note = "Lock complete; full principal returned via emergency withdraw."
            msg = "Principal returned to your balance."

        w.balance += returned
        w.staked = ZERO
        w.stake_days = 0
        w.staked_at = None
        w.unlock_at = None
        w.reward_accrued = ZERO
        w.save()
        Transaction.objects.create(
            user=request.user, kind=Transaction.EMERGENCY, amount=returned,
            status=Transaction.CONFIRMED, note=note,
        )
    return _respond(request, "success", msg, wallet=w)


# ---------------------------------------------------------------------------
# Connect wallet -> store a read-only address. We never take custody / keys.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def connect_wallet(request):
    address = (request.POST.get("address") or "").strip()
    if not address:
        return _respond(request, "error", "Provide a wallet address.")
    if len(address) > 128:
        return _respond(request, "error", "Address too long.")
    w = request.user.wallet
    w.address = address
    w.save(update_fields=["address"])
    return _respond(request, "success", "Wallet address saved (read-only).")


# ---------------------------------------------------------------------------
# DEV-ONLY test faucet. Gated hard on DEBUG. Lets you credit your own test
# balance locally so you can exercise stake/withdraw flows. This endpoint does
# NOT exist when DEBUG is off, and must never ship enabled.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def dev_faucet(request):
    if not settings.DEBUG:
        messages.error(request, "Not available.")
        return redirect("dashboard")
    amt = _parse_amount(request.POST.get("amount")) or Decimal("100")
    with db_tx.atomic():
        w = Wallet.objects.select_for_update().get(user=request.user)
        w.balance += amt
        w.save(update_fields=["balance"])
        Transaction.objects.create(
            user=request.user, kind=Transaction.FAUCET, amount=amt,
            status=Transaction.CONFIRMED, note="DEV-ONLY test credit.",
        )
    messages.warning(request, f"[DEV] Credited {amt} test units.")
    return redirect("dashboard")


# ---------------------------------------------------------------------------
# Account menu pages: Profile, Settings, FAQ, Support
# ---------------------------------------------------------------------------
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from .models import SupportMessage


@login_required
def profile(request):
    u = request.user
    if request.method == "POST":
        u.first_name = (request.POST.get("first_name") or "").strip()
        u.last_name = (request.POST.get("last_name") or "").strip()
        email = (request.POST.get("email") or "").strip().lower()
        if email and email != u.email:
            from django.contrib.auth.models import User
            if User.objects.filter(email__iexact=email).exclude(pk=u.pk).exists():
                messages.error(request, "That email is already in use.")
                return redirect("profile")
            u.email = email
        u.save(update_fields=["first_name", "last_name", "email"])
        messages.success(request, "Profile updated.")
        return redirect("profile")
    return render(request, "profile.html", {"u": u})


@login_required
def account_settings(request):
    u = request.user
    pw_form = PasswordChangeForm(u)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "email":
            email = (request.POST.get("email") or "").strip().lower()
            if not email:
                messages.error(request, "Enter an email.")
            else:
                from django.contrib.auth.models import User
                if User.objects.filter(email__iexact=email).exclude(pk=u.pk).exists():
                    messages.error(request, "That email is already in use.")
                else:
                    u.email = email
                    u.save(update_fields=["email"])
                    messages.success(request, "Email updated.")
            return redirect("settings")
        elif action == "password":
            pw_form = PasswordChangeForm(u, request.POST)
            if pw_form.is_valid():
                pw_form.save()
                update_session_auth_hash(request, u)
                messages.success(request, "Password changed.")
                return redirect("settings")
            messages.error(request, "Please correct the errors below.")
    return render(request, "settings.html", {"u": u, "pw_form": pw_form})


@login_required
def faq(request):
    return render(request, "faq.html")


@login_required
def support(request):
    if request.method == "POST":
        subject = (request.POST.get("subject") or "").strip()
        body = (request.POST.get("body") or "").strip()
        if not subject or not body:
            messages.error(request, "Fill in both subject and message.")
            return redirect("support")
        SupportMessage.objects.create(user=request.user, subject=subject[:150], body=body)
        messages.success(request, "Message sent to support. We'll get back to you.")
        return redirect("support")
    return render(request, "support.html")
