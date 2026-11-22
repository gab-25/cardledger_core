from django.core.management.base import BaseCommand

from ledger import tasks


class Command(BaseCommand):
    help = (
        "Queue the catalog sync (the generator's catalog files and Cardmarket's prices of every game) "
        "on the Celery worker."
    )

    def handle(self, *args, **options):
        result = tasks.sync_catalog.delay()
        self.stdout.write(f"Catalog sync queued (task {result.id})")
