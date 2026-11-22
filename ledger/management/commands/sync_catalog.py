from django.core.management.base import BaseCommand, CommandError

from ledger import catalog_sync


class Command(BaseCommand):
    help = (
        "Run the catalog sync (the generator's catalog files and Cardmarket's prices of every game) "
        "in this process; the outcome is also written to the SyncLog."
    )

    def handle(self, *args, **options):
        try:
            results = catalog_sync.run_sync()
        except catalog_sync.CatalogSyncError as exc:
            raise CommandError(f"Catalog sync failed: {exc}") from exc
        self.stdout.write(f"Catalog sync done ({len(results)} games)")
