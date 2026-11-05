from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from django.db import transaction as db_tx
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.html import format_html

from .models import (
    Adjustment, AddFundHistory, GatewaySettings, ManageWallet, RequestHistory,
    RewardPool, StakedUser, SupportMessage, Transaction, Wallet,
    WithdrawalRequest, ZERO,
)


@admin.register(StakedUser)
class StakedUserAdmin(admin.ModelAdmin):
    """Read-only list of users with an active stake."""
    list_display = ("user", "staked", "stake_days", "staked_at", "unlock_at")
    search_fields = ("user__username", "user__email")

    def get_queryset(self, request):
        return super().get_queryset(request).filter(staked__gt=ZERO)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SupportMessage)
class SupportMessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "subject", "resolved")
    list_filter = ("resolved",)
    search_fields = ("user__username", "user__email", "subject", "body")
    list_editable = ("resolved",)


def _user_link(user):
    """A clickable link to a user's admin page."""
    if not user:
        return "—"
    url = reverse("admin:auth_user_change", args=[user.pk])
    label = user.get_username()
    email = user.email or ""
    return format_html('<a href="{}">{}</a>{}', url, label,
                       format_html(' · <a href="{}">{}</a>', url, email) if email else "")


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    # Balance, staked and reward are editable inline so you can correct a wallet
    # if a calculation went wrong. Any change is written to the user's ledger so
    # there's a record of the correction.
    list_display = ("user", "balance", "staked", "reward_accrued", "address", "created_at")
    list_editable = ("balance", "staked", "reward_accrued")
    list_display_links = None
    search_fields = ("user__username", "user__email", "address")
    readonly_fields = ("created_at",)

    def save_model(self, request, obj, form, change):
        with db_tx.atomic():
            w = Wallet.objects.select_for_update().get(pk=obj.pk)
            changes = []
            for field in ("balance", "staked", "reward_accrued"):
                new = getattr(obj, field)
                old = getattr(w, field)
                if new is None or new < ZERO:
                    self.message_user(request, f"{field} cannot be negative.", messages.ERROR)
                    return
                if new != old:
                    changes.append(f"{field} {old}→{new}")
                    setattr(w, field, new)
            if not changes:
                return
            w.save(update_fields=["balance", "staked", "reward_accrued"])
            Transaction.objects.create(
                user=w.user,
                kind=Transaction.ADJUSTMENT,
                amount=ZERO,
                status=Transaction.CONFIRMED,
                note=f"Wallet corrected by {request.user.username}: " + ", ".join(changes),
            )
        self.message_user(request, "Wallet updated and logged.", messages.SUCCESS)


@admin.register(RewardPool)
class RewardPoolAdmin(admin.ModelAdmin):
    list_display = ("funded", "updated_at")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "kind", "amount", "status", "reference")
    list_filter = ("kind", "status")
    search_fields = ("user__username", "user__email", "reference")
    actions = ["confirm_deposits", "mark_withdrawals_paid", "reject_withdrawals"]

    @admin.action(description="Confirm selected PENDING deposits (credits balance)")
    def confirm_deposits(self, request, queryset):
        """Where a real, received payment becomes a balance. Only run it for
        deposits you have actually received and verified (gateway settlement /
        on-chain confirmation)."""
        n = 0
        for txn in queryset.filter(kind=Transaction.DEPOSIT, status=Transaction.PENDING):
            with db_tx.atomic():
                w = Wallet.objects.select_for_update().get(user=txn.user)
                w.balance += txn.amount
                w.save(update_fields=["balance"])
                txn.status = Transaction.CONFIRMED
                txn.note = "Confirmed by operator."
                txn.save(update_fields=["status", "note"])
                n += 1
        self.message_user(request, f"Confirmed {n} deposit(s).", messages.SUCCESS)

    @admin.action(description="Mark selected PENDING withdrawals as PAID (you sent the money)")
    def mark_withdrawals_paid(self, request, queryset):
        """Run this once you have actually sent the payout. The balance was
        already debited when the user requested it; this just closes the
        request as settled."""
        n = 0
        for txn in queryset.filter(kind=Transaction.WITHDRAW, status=Transaction.PENDING):
            txn.status = Transaction.PAID
            txn.note = "Payout sent."
            txn.save(update_fields=["status", "note"])
            n += 1
        self.message_user(request, f"Marked {n} withdrawal(s) as paid.", messages.SUCCESS)

    @admin.action(description="Reject selected PENDING withdrawals (refunds balance)")
    def reject_withdrawals(self, request, queryset):
        n = 0
        for txn in queryset.filter(kind=Transaction.WITHDRAW, status=Transaction.PENDING):
            with db_tx.atomic():
                w = Wallet.objects.select_for_update().get(user=txn.user)
                w.balance += txn.amount
                w.save(update_fields=["balance"])
                txn.status = Transaction.REJECTED
                txn.note = "Rejected; amount refunded."
                txn.save(update_fields=["status", "note"])
                n += 1
        self.message_user(request, f"Rejected and refunded {n} withdrawal(s).", messages.SUCCESS)


@admin.register(Adjustment)
class AdjustmentAdmin(admin.ModelAdmin):
    """Audited manual balance correction. Adding a row here applies it once,
    atomically, and writes a matching ledger Transaction. Existing rows are
    read-only so the audit trail can't be rewritten."""

    list_display = ("created_at", "user", "direction", "amount", "reason", "created_by", "applied")
    list_filter = ("direction", "applied")
    search_fields = ("user__username", "user__email", "reason")

    def get_readonly_fields(self, request, obj=None):
        if obj:  # already created -> fully locked
            return ("user", "direction", "amount", "reason", "created_by", "created_at", "applied")
        return ("created_by", "applied")

    def has_change_permission(self, request, obj=None):
        # allow viewing, never editing an applied record
        return obj is None or not obj.applied

    def has_delete_permission(self, request, obj=None):
        return False  # keep the audit trail intact

    def save_model(self, request, obj, form, change):
        if change or obj.applied:
            return  # never re-apply
        if obj.amount is None or obj.amount <= ZERO:
            self.message_user(request, "Amount must be positive.", messages.ERROR)
            return
        with db_tx.atomic():
            w = Wallet.objects.select_for_update().get(user=obj.user)
            delta = obj.signed_amount
            if w.balance + delta < ZERO:
                self.message_user(
                    request,
                    "Debit would make the balance negative; not applied.",
                    messages.ERROR,
                )
                return
            w.balance += delta
            w.save(update_fields=["balance"])
            obj.created_by = request.user
            obj.applied = True
            super().save_model(request, obj, form, change)
            Transaction.objects.create(
                user=obj.user,
                kind=Transaction.ADJUSTMENT,
                amount=delta,
                status=Transaction.CONFIRMED,
                note=f"Adjustment by {request.user.username}: {obj.reason}",
            )
        self.message_user(request, "Adjustment applied and logged.", messages.SUCCESS)


@admin.register(RequestHistory)
class RequestHistoryAdmin(admin.ModelAdmin):
    """'Request history': the manual deposit requests and withdrawals users have
    made. You can correct a PENDING request's amount inline, then Approve or
    Reject it. Approving a deposit credits the user; approving a withdrawal
    marks it paid. The user's dashboard reflects the new status."""

    list_display = ("created_at", "account", "kind", "amount", "status", "decision", "reference", "note")
    list_editable = ("amount",)
    list_display_links = None
    list_filter = ("kind", "status")
    search_fields = ("user__username", "user__email", "reference")
    date_hierarchy = "created_at"

    @admin.display(description="User")
    def account(self, obj):
        return _user_link(obj.user)

    @admin.display(description="Approve / Reject")
    def decision(self, obj):
        """Per-row Approve and Reject buttons (only for pending requests).
        Edit the amount and click Save first if you need to correct it, then
        Approve."""
        if obj.status != Transaction.PENDING:
            return "—"
        return format_html(
            '<a class="button" style="background:#166534;color:#fff;padding:3px 10px;border-radius:5px" '
            'href="{}/approve/">Approve</a>&nbsp;'
            '<a class="button" style="background:#991b1b;color:#fff;padding:3px 10px;border-radius:5px" '
            'href="{}/reject/">Reject</a>',
            obj.pk, obj.pk,
        )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(kind=Transaction.DEPOSIT)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        # Only a PENDING request's amount may be edited; a settled one is locked
        # so changing its amount can't desync the balance.
        current = Transaction.objects.get(pk=obj.pk)
        if current.status != Transaction.PENDING:
            self.message_user(
                request, "Only pending requests can be edited; left unchanged.",
                messages.WARNING)
            return
        if obj.amount is None or obj.amount <= ZERO:
            self.message_user(request, "Amount must be positive.", messages.ERROR)
            return
        obj.save(update_fields=["amount"])
        self.message_user(request, f"Amount updated to {obj.amount}.", messages.SUCCESS)

    # --- per-row Approve / Reject -------------------------------------------
    def get_urls(self):
        custom = [
            path("<int:pk>/approve/", self.admin_site.admin_view(self.approve_one),
                 name="accounts_requesthistory_approve"),
            path("<int:pk>/reject/", self.admin_site.admin_view(self.reject_one),
                 name="accounts_requesthistory_reject"),
        ]
        return custom + super().get_urls()

    def _back(self, request):
        return redirect(request.META.get("HTTP_REFERER")
                        or reverse("admin:accounts_requesthistory_changelist"))

    def approve_one(self, request, pk):
        txn = Transaction.objects.filter(pk=pk).first()
        if not txn or txn.status != Transaction.PENDING:
            self.message_user(request, "Request is not pending.", messages.WARNING)
            return self._back(request)
        with db_tx.atomic():
            txn = Transaction.objects.select_for_update().get(pk=pk)
            if txn.kind == Transaction.DEPOSIT:
                w = Wallet.objects.select_for_update().get(user=txn.user)
                w.balance += txn.amount
                w.save(update_fields=["balance"])
                txn.status = Transaction.CONFIRMED
                txn.note = "Approved by admin."
            elif txn.kind == Transaction.WITHDRAW:
                txn.status = Transaction.PAID
                txn.note = "Payout sent."
            txn.save(update_fields=["status", "note"])
        self.message_user(request, "Approved. It now shows on the user's dashboard.", messages.SUCCESS)
        return self._back(request)

    def reject_one(self, request, pk):
        txn = Transaction.objects.filter(pk=pk).first()
        if not txn or txn.status != Transaction.PENDING:
            self.message_user(request, "Request is not pending.", messages.WARNING)
            return self._back(request)
        with db_tx.atomic():
            txn = Transaction.objects.select_for_update().get(pk=pk)
            if txn.kind == Transaction.WITHDRAW:
                w = Wallet.objects.select_for_update().get(user=txn.user)
                w.balance += txn.amount  # refund held amount
                w.save(update_fields=["balance"])
            txn.status = Transaction.REJECTED
            txn.note = "Rejected by admin."
            txn.save(update_fields=["status", "note"])
        self.message_user(request, "Rejected. The user sees it as rejected.", messages.SUCCESS)
        return self._back(request)


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    """'Balance withdrawal': users' pending payouts, with the coin network and
    destination address. Send the payout on your end, then click Approve — the
    user's dashboard then shows the withdrawal as approved. Reject refunds it."""

    list_display = ("created_at", "account", "amount", "coin", "to_address",
                    "status", "decision")
    list_filter = ("status", "coin")
    search_fields = ("user__username", "user__email", "to_address", "reference")
    date_hierarchy = "created_at"

    @admin.display(description="User")
    def account(self, obj):
        return _user_link(obj.user)

    @admin.display(description="Approve / Reject")
    def decision(self, obj):
        if obj.status != Transaction.PENDING:
            return "—"
        return format_html(
            '<a class="button" style="background:#166534;color:#fff;padding:3px 10px;border-radius:5px" '
            'href="{}/approve/">Approve</a>&nbsp;'
            '<a class="button" style="background:#991b1b;color:#fff;padding:3px 10px;border-radius:5px" '
            'href="{}/reject/">Reject</a>',
            obj.pk, obj.pk,
        )

    def get_queryset(self, request):
        return super().get_queryset(request).filter(kind=Transaction.WITHDRAW)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        custom = [
            path("<int:pk>/approve/", self.admin_site.admin_view(self.approve_one),
                 name="accounts_withdrawalrequest_approve"),
            path("<int:pk>/reject/", self.admin_site.admin_view(self.reject_one),
                 name="accounts_withdrawalrequest_reject"),
        ]
        return custom + super().get_urls()

    def _back(self, request):
        return redirect(request.META.get("HTTP_REFERER")
                        or reverse("admin:accounts_withdrawalrequest_changelist"))

    def approve_one(self, request, pk):
        txn = Transaction.objects.filter(pk=pk, kind=Transaction.WITHDRAW).first()
        if not txn or txn.status != Transaction.PENDING:
            self.message_user(request, "Withdrawal is not pending.", messages.WARNING)
            return self._back(request)
        txn.status = Transaction.PAID
        txn.note = "Payout sent (approved)."
        txn.save(update_fields=["status", "note"])
        self.message_user(request, "Approved. User sees the withdrawal as approved.", messages.SUCCESS)
        return self._back(request)

    def reject_one(self, request, pk):
        txn = Transaction.objects.filter(pk=pk, kind=Transaction.WITHDRAW).first()
        if not txn or txn.status != Transaction.PENDING:
            self.message_user(request, "Withdrawal is not pending.", messages.WARNING)
            return self._back(request)
        with db_tx.atomic():
            txn = Transaction.objects.select_for_update().get(pk=pk)
            w = Wallet.objects.select_for_update().get(user=txn.user)
            w.balance += txn.amount  # refund
            w.save(update_fields=["balance"])
            txn.status = Transaction.REJECTED
            txn.note = "Rejected; amount refunded."
            txn.save(update_fields=["status", "note"])
        self.message_user(request, "Rejected and refunded.", messages.SUCCESS)
        return self._back(request)


@admin.register(AddFundHistory)
class AddFundHistoryAdmin(admin.ModelAdmin):
    """Read-only 'Add fund history': gateway (NOWPayments) deposits. A row is
    'confirmed' once NOWPayments confirms the payment. Separate from Request
    history. Username and email link into the user's account."""

    list_display = ("created_at", "account", "amount", "status", "reference", "note")
    list_filter = ("status",)
    search_fields = ("user__username", "user__email", "reference")
    date_hierarchy = "created_at"

    @admin.display(description="User (click to open)")
    def account(self, obj):
        return _user_link(obj.user)

    def get_queryset(self, request):
        return super().get_queryset(request).filter(kind=Transaction.ADD_FUND)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ManageWalletForm(forms.ModelForm):
    """Edit the user's balance directly. Saving sets the balance to the value
    you enter; the change (the difference) is recorded to the user's ledger."""

    class Meta:
        model = ManageWallet
        fields = ("balance",)
        labels = {"balance": "Balance"}
        help_texts = {"balance": "Type the correct balance and Save."}


@admin.register(ManageWallet)
class ManageWalletAdmin(admin.ModelAdmin):
    """'Manage wallet' — the list of registered users and their balances. Open a
    user to edit the balance and Save (e.g. to set the real amount when a
    NOWPayments deposit came in underpaid / wrong coin and wasn't auto-credited).
    The detail page shows only the balance field. The change is written to the
    user's ledger so you keep a record of what was adjusted."""

    form = ManageWalletForm
    # Edit the balance right in the list. Username/email are plain text (not
    # links), so no user is clickable and no extra page opens.
    list_display = ("username_col", "email_col", "balance")
    list_editable = ("balance",)
    list_display_links = None
    search_fields = ("user__username", "user__email")
    fields = ("balance",)  # detail page (if reached) shows only the balance

    @admin.display(description="Username")
    def username_col(self, obj):
        return obj.user.get_username()

    @admin.display(description="Email")
    def email_col(self, obj):
        return obj.user.email

    def has_add_permission(self, request):
        return False  # one wallet per user, created automatically

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        new_balance = obj.balance
        if new_balance is None or new_balance < ZERO:
            self.message_user(request, "Balance cannot be negative.", messages.ERROR)
            return
        with db_tx.atomic():
            w = Wallet.objects.select_for_update().get(pk=obj.pk)
            old = w.balance
            delta = (new_balance - old)
            w.balance = new_balance
            w.save(update_fields=["balance"])
            if delta != ZERO:
                Transaction.objects.create(
                    user=w.user,
                    kind=Transaction.ADJUSTMENT,
                    amount=delta,
                    status=Transaction.CONFIRMED,
                    note=f"Balance set to {new_balance} by {request.user.username} (was {old}).",
                )
        self.message_user(
            request, f"Balance updated to {new_balance}. It now shows on the user's dashboard.",
            messages.SUCCESS,
        )


@admin.register(GatewaySettings)
class GatewaySettingsAdmin(admin.ModelAdmin):
    """Configure NOWPayments here (your 'Payment gateway' page). Enter the API
    key and IPN secret from your NOWPayments dashboard, tick Enabled, and the
    'Add fund' button on the user dashboard will send users to NOWPayments.
    A deposit is credited only when NOWPayments confirms the payment."""

    list_display = ("__str__", "enabled", "sandbox", "price_currency", "updated_at")

    fieldsets = (
        ("Credentials (from your NOWPayments dashboard)", {
            "fields": ("api_key", "ipn_secret"),
        }),
        ("Options", {
            "fields": ("price_currency", "sandbox", "enabled"),
            "description": (
                "Set the IPN callback URL in your NOWPayments dashboard to "
                "https://YOUR-DOMAIN/gateway/nowpayments-ipn/ — that is the "
                "address NOWPayments calls to confirm a payment."
            ),
        }),
    )

    def has_add_permission(self, request):
        # single settings row
        return not GatewaySettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
