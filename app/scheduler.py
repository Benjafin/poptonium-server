"""Holder for the process-wide APScheduler instance.

The scheduler is created in ``server.startup`` and stored here so other modules
(ratings sync registration, admin status/job-run) can reach it without a circular
import. Access it as ``scheduler.scheduler`` and assign to rebind.
"""

from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler: Optional[AsyncIOScheduler] = None
