from django.core.management.base import BaseCommand

from ledger import snapshots


class Command(BaseCommand):
    help = (
        "Write today's stock value (Cardmarket trend × copies) of every user and game they have items of; a rerun "
        "the same day overwrites it. Schedule it daily (the catalog sync also runs it after the prices)."
    )

    def handle(self, *args, **options):
        written = snapshots.snapshot_stock_values()
        self.stdout.write(f"Stock values: {written} snapshots written")
