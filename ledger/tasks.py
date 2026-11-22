from celery import shared_task

from . import catalog_sync


@shared_task(name="ledger.sync_catalog")
def sync_catalog():
    """Import the changed catalog files of every game and its Cardmarket prices (outcome in the SyncLog)."""
    catalog_sync.run_sync()
