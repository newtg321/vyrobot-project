"""
Data model for Vyrobot.

Design principles (these are what make the app honest rather than a fake-balance
"investment" front):

1. A user's balance starts at 0 and only ever changes through a recorded
   Transaction. There is no code path anywhere that invents or auto-inflates a
   balance. Search the codebase for `.balance` writes — every one is backed by a
   ledger row.

2. Deposits are PENDING until confirmed by a real settlement source (a payment
   gateway webhook, or a manual admin confirmation of a received transfer).
   Confirmation is the only thing that credits a balance.

3. Staking rewards accrue on real staked principal at a disclosed APR, but a
   claim can never pay out more than the operator has actually funded into the
   RewardPool. Unfunded yield cannot be withdrawn. This cap is the guardrail
   against a Ponzi-style "infinite returns" display.

4. Withdrawals debit the available balance immediately and create a PENDING
   payout that an operator must settle (or reject, which refunds). The money
   leaves the user's internal balance the moment they request it — the UI never
   shows a balance the platform isn't actually holding against.
"""
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

ZERO = Decimal("0")
SECONDS_PER_YEAR = Decimal(365 * 24 * 60 * 60)


class Wallet(models.Model):
    """One internal ledger wallet per user. All amounts are platform-internal
    units (treat as your settlement currency, e.g. USDT). This is NOT a
    custodial key store — `address` below is only a user-supplied address for
    display / payout destination."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="wallet")

    # Available, spendable balance.
    balance = models.DecimalField(max_digits=20, decimal_places=8, default=ZERO)
    # Principal currently staked (locked, earning reward).
    staked = models.DecimalField(max_digits=20, decimal_places=8, default=ZERO)
    # Reward accrued but not yet claimed.
    reward_accrued = models.DecimalField(max_digits=20, decimal_places=8, default=ZERO)
    last_accrued_at = models.DateTimeField(default=timezone.now)

    # Active stake lock. stake_days = 0 means no active stake.
    stake_days = models.PositiveIntegerField(default=0)
    staked_at = models.DateTimeField(null=True, blank=True)
    unlock_at = models.DateTimeField(null=True, blank=True)

    # User-supplied external wallet address (read-only; we never hold its keys
    # and never move its tokens).
    address = models.CharField(max_length=128, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Wallet({self.user.username})"

    @property
    def has_active_stake(self):
        return self.staked > ZERO and self.unlock_at is not None

    @property
    def is_locked(self):
        return self.has_active_stake and timezone.now() < self.unlock_at

    # --- reward accrual -------------------------------------------------
    def accrue(self):
        """Fold elapsed staking reward into reward_accrued. Pure function of
        staked principal, disclosed APR and real elapsed time."""
        now = timezone.now()
        elapsed = Decimal((now - self.last_accrued_at).total_seconds())
        if self.staked > ZERO and elapsed > ZERO:
            apr = Decimal(str(settings.STAKING_APR))
            earned = (self.staked * apr * elapsed / SECONDS_PER_YEAR)
            self.reward_accrued = (self.reward_accrued + earned).quantize(Decimal("0.00000001"))
        self.last_accrued_at = now


class RewardPool(models.Model):
    """Singleton pool the operator funds. Claims draw down from `funded`.
    If this is empty, no reward can be claimed — by design."""

    funded = models.DecimalField(max_digits=20, decimal_places=8, default=ZERO)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Reward pool"
        verbose_name_plural = "Reward pool"

    def __str__(self):
        return f"RewardPool(funded={self.funded})"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Transaction(models.Model):
    DEPOSIT = "deposit"        # manual deposit request (admin-confirmed)
    ADD_FUND = "add_fund"      # gateway deposit via NOWPayments
    WITHDRAW = "withdraw"
    STAKE = "stake"
    UNSTAKE = "unstake"
    CLAIM = "claim"
    EMERGENCY = "emergency_withdraw"
    ADJUSTMENT = "adjustment"  # audited manual support correction
    MIGRATION = "migration"    # balance carried over from v1 by the migration tool
    FAUCET = "faucet"  # dev/test only
    KIND_CHOICES = [
        (DEPOSIT, "Deposit request"),
        (ADD_FUND, "Add fund (gateway)"),
        (WITHDRAW, "Withdraw"),
        (STAKE, "Stake"),
        (UNSTAKE, "Unstake"),
        (CLAIM, "Claim reward"),
        (ADJUSTMENT, "Support adjustment"),
        (MIGRATION, "Migrated from v1"),
        (EMERGENCY, "Emergency withdraw"),
        (FAUCET, "Test faucet (dev only)"),
    ]

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    PAID = "paid"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (CONFIRMED, "Confirmed"),
        (REJECTED, "Rejected"),
        (PAID, "Paid / settled"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="transactions")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    amount = models.DecimalField(max_digits=20, decimal_places=8)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=CONFIRMED)
    reference = models.CharField(max_length=80, blank=True, default="")
    note = models.CharField(max_length=255, blank=True, default="")
    # Withdrawal destination (coin/network + address). Empty for other kinds.
    coin = models.CharField(max_length=24, blank=True, default="")
    to_address = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount} [{self.status}] {self.user.username}"


class Adjustment(models.Model):
    """An audited manual balance correction made by an operator.

    This is the ONLY way staff can change a user's balance by hand, and it is
    deliberately a first-class, logged record rather than an editable number on
    the wallet. Every adjustment:
      - requires a human-readable reason,
      - records who made it and when,
      - writes a matching Transaction into the user's ledger.

    Use it to correct real settlement errors (e.g. a gateway payment that the
    webhook missed), refunds, or fee reversals — things that actually happened.
    It is not a tool for crediting returns or balances a user never funded; a
    positive adjustment is a liability the operator genuinely owes and must be
    able to pay out.
    """

    POSITIVE = "credit"
    NEGATIVE = "debit"
    DIRECTION_CHOICES = [(POSITIVE, "Credit (+)"), (NEGATIVE, "Debit (-)")]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="adjustments")
    direction = models.CharField(max_length=6, choices=DIRECTION_CHOICES, default=POSITIVE)
    amount = models.DecimalField(max_digits=20, decimal_places=8)
    reason = models.CharField(max_length=255)  # required
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="adjustments_made",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    applied = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        sign = "+" if self.direction == self.POSITIVE else "-"
        return f"Adjustment {sign}{self.amount} {self.user.username} ({self.reason})"

    @property
    def signed_amount(self):
        return self.amount if self.direction == self.POSITIVE else -self.amount


class RequestHistory(Transaction):
    """Admin list of DEPOSIT REQUESTS users have made (the manual 'Request'
    button path). Withdrawals have their own 'Balance withdrawal' list."""

    class Meta:
        proxy = True
        verbose_name = "Request history"
        verbose_name_plural = "Request history"


class WithdrawalRequest(Transaction):
    """Admin 'Balance withdrawal' list: pending withdrawal payouts with the coin
    network and destination address the user chose. Approve once you've sent the
    payout; the balance was already debited when the user requested it."""

    class Meta:
        proxy = True
        verbose_name = "Balance withdrawal"
        verbose_name_plural = "Balance withdrawal"


class AddFundHistory(Transaction):
    """Admin list of gateway 'Add Fund' deposits (NOWPayments). Separate from
    Request history. A row becomes 'confirmed' when NOWPayments confirms the
    payment."""

    class Meta:
        proxy = True
        verbose_name = "Add fund history"
        verbose_name_plural = "Add fund history"


class ManageWallet(Wallet):
    """Admin 'Manage wallet' entry: the list of registered users and their
    balances. Opening a user lets an operator post a credit (or debit) — for
    example to reconcile a NOWPayments payment that came in underpaid or on the
    wrong coin and so was not auto-credited. Every change made here is written
    to the user's ledger as an audited entry, so the balance always traces back
    to a recorded reason."""

    class Meta:
        proxy = True
        verbose_name = "Manage wallet"
        verbose_name_plural = "Manage wallet"


class GatewaySettings(models.Model):
    """NOWPayments configuration, editable in the admin. One row.

    For production, environment variables are more secure than storing keys in
    the database; these fields let you get running from the admin UI. The IPN
    secret is what proves a deposit callback really came from NOWPayments — a
    balance is credited only when a callback's signature verifies against it.
    """

    api_key = models.CharField(max_length=255, blank=True, default="")
    ipn_secret = models.CharField(max_length=255, blank=True, default="")
    price_currency = models.CharField(max_length=10, default="usd")
    sandbox = models.BooleanField(
        default=True, help_text="Use NOWPayments sandbox for testing. Turn off for live.")
    enabled = models.BooleanField(
        default=False, help_text="When off, 'Add fund' falls back to a manual pending request.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Payment gateway (NOWPayments)"
        verbose_name_plural = "Payment gateway (NOWPayments)"

    def __str__(self):
        return f"NOWPayments ({'sandbox' if self.sandbox else 'live'}, {'enabled' if self.enabled else 'off'})"

    @property
    def api_base(self):
        return "https://api-sandbox.nowpayments.io/v1" if self.sandbox else "https://api.nowpayments.io/v1"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SupportMessage(models.Model):
    """A message a user sends from the Support page. Staff read these in admin."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="support_messages")
    subject = models.CharField(max_length=150)
    body = models.TextField()
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Support: {self.subject} ({self.user.username})"


class StakedUser(Wallet):
    """Admin 'Stakes' list: users who currently have an active stake, with the
    staked amount, lock period and unlock date. Read-only view over wallets."""

    class Meta:
        proxy = True
        verbose_name = "Stake"
        verbose_name_plural = "Stakes"
