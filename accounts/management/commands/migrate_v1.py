"""
Migrate real user balances from Vyrobot v1 into v2.

This is a DATA migration: it reads an export of the actual balances recorded in
your v1 system and carries each one over. The numbers come from the v1 export
file — the source of truth — not from anything typed in by hand per user.

What it does per row:
  - finds the user by email (or creates the account if it doesn't exist yet),
  - credits their v2 wallet with the v1 balance exactly once,
  - writes an audited ledger Transaction (kind="migration") carrying the v1
    reference, so every migrated figure traces back to a v1 record.

It is idempotent: each row carries a stable reference (the `v1_ref` column, or
the email if that's absent). Running the command again skips rows already
migrated, so you can re-run it safely after fixing a few lines.

--------------------------------------------------------------------------------
HOW TO GET THE EXPORT FROM v1
--------------------------------------------------------------------------------
Export the real balances from your v1 database/app into ONE file. Two formats
are accepted:

CSV (headers required):
    email,full_name,balance,staked,v1_ref
    jane@example.com,Jane Doe,1500.00,0,U1001
    sam@example.com,Sam O,230.50,100,U1002

JSON (list of objects):
    [
      {"email":"jane@example.com","full_name":"Jane Doe","balance":"1500.00","v1_ref":"U1001"},
      {"email":"sam@example.com","full_name":"Sam O","balance":"230.50","staked":"100","v1_ref":"U1002"}
    ]

Columns:
  email      (required)  - the user's login in v2
  balance    (required)  - their available balance in v1
  full_name  (optional)  - used only when creating a new account
  staked     (optional)  - carried into their staked amount
  v1_ref     (optional)  - v1 user/record id; used to prevent double-migration

--------------------------------------------------------------------------------
RUN IT
--------------------------------------------------------------------------------
    python manage.py migrate_v1 path\\to\\v1_export.csv            # preview only
    python manage.py migrate_v1 path\\to\\v1_export.csv --commit   # actually write

Without --commit it runs a dry run and changes nothing, so you can check the
summary first. New accounts are created with no usable password; those users
sign in via your normal password-reset/"forgot password" path, or you set one.
"""
import csv
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_tx

from accounts.models import Transaction, Wallet, ZERO


def _dec(raw, default="0"):
    try:
        return Decimal(str(raw if raw not in (None, "") else default)).quantize(Decimal("0.00000001"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _load(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, list):
            raise CommandError("JSON export must be a list of row objects.")
        return data
    # default: CSV
    return list(csv.DictReader(text.splitlines()))


class Command(BaseCommand):
    help = "Migrate real user balances from a v1 export file into v2."

    def add_arguments(self, parser):
        parser.add_argument("export_file", help="Path to the v1 export (.csv or .json)")
        parser.add_argument("--commit", action="store_true",
                            help="Actually write changes. Without it, this is a dry run.")

    def handle(self, *args, **opts):
        path = Path(opts["export_file"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        rows = _load(path)
        if not rows:
            raise CommandError("Export file has no rows.")

        commit = opts["commit"]
        created = migrated = skipped = errors = 0
        total_balance = ZERO

        for i, row in enumerate(rows, start=1):
            email = (row.get("email") or "").strip().lower()
            if not email:
                self.stderr.write(f"  row {i}: missing email -> skipped")
                errors += 1
                continue

            balance = _dec(row.get("balance"))
            staked = _dec(row.get("staked"), "0")
            if balance is None or staked is None or balance < ZERO or staked < ZERO:
                self.stderr.write(f"  row {i} ({email}): bad balance/staked -> skipped")
                errors += 1
                continue

            v1_ref = (row.get("v1_ref") or email).strip()
            reference = f"v1:{v1_ref}"

            if Transaction.objects.filter(kind=Transaction.MIGRATION, reference=reference).exists():
                skipped += 1
                continue

            if not commit:
                # dry run: just tally
                migrated += 1
                total_balance += balance
                if not User.objects.filter(username=email).exists():
                    created += 1
                continue

            with db_tx.atomic():
                user, was_created = User.objects.get_or_create(
                    username=email,
                    defaults={"email": email},
                )
                if was_created:
                    name = (row.get("full_name") or "").strip()
                    if name:
                        parts = name.split(" ", 1)
                        user.first_name = parts[0]
                        user.last_name = parts[1] if len(parts) > 1 else ""
                    user.set_unusable_password()  # user resets to set a password
                    user.save()
                    created += 1

                w = Wallet.objects.select_for_update().get(user=user)
                w.balance += balance
                w.staked += staked
                w.save(update_fields=["balance", "staked"])

                Transaction.objects.create(
                    user=user,
                    kind=Transaction.MIGRATION,
                    amount=balance,
                    status=Transaction.CONFIRMED,
                    reference=reference,
                    note=f"Migrated from v1 (ref {v1_ref}); staked {staked}.",
                )
                migrated += 1
                total_balance += balance

        mode = "COMMITTED" if commit else "DRY RUN (nothing written)"
        self.stdout.write(self.style.SUCCESS(
            f"\n{mode}\n"
            f"  rows in file : {len(rows)}\n"
            f"  to migrate   : {migrated}\n"
            f"  new accounts : {created}\n"
            f"  already done : {skipped}\n"
            f"  errors       : {errors}\n"
            f"  total balance carried over: {total_balance}\n"
        ))
        if not commit and migrated:
            self.stdout.write("Re-run with --commit to apply.")
