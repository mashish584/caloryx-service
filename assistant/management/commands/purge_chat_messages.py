"""Purge ChatMessage rows past their retention window (§12.14, Chunk 8c).

Raw phrasing has little value once parsed - `ChatMessage` is kept for a much
shorter window than `LoggedMeal` (§12.14). No scheduler exists in this repo
(same as every seed_* command) - run this by hand or from an external cron.

    python manage.py purge_chat_messages
    python manage.py purge_chat_messages --days 14   # override the default window
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from common.db import get_client


class Command(BaseCommand):
    help = "Delete ChatMessage rows older than the retention window."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Override settings.CHAT_MESSAGE_RETENTION_DAYS.",
        )

    def handle(self, *args, **options) -> None:
        days = options["days"] if options["days"] is not None else settings.CHAT_MESSAGE_RETENTION_DAYS
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        deleted = get_client().chatmessage.delete_many(where={"createdAt": {"lt": cutoff}})
        self.stdout.write(
            self.style.SUCCESS("deleted {} chat messages older than {} days".format(deleted, days))
        )
