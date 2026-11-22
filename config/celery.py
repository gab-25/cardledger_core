"""Celery app. No beat schedule is configured; the sync_catalog command runs the sync in-process, not as a task."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("cardledger")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
