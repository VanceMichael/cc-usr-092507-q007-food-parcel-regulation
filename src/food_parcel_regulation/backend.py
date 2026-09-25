"""协同后端门面：装配事件存储、时态主数据与各应用服务。

``Backend.reopen`` 从同一事件日志重建全部状态——温控时限、待核查事项、
回执催办都由状态推导，重启后继续计算，不依赖进程内定时器。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .application import (
    BatchService,
    LeadService,
    ParcelService,
    PickupObservation,
    PickupService,
    Repository,
)
from .collaboration import ComplaintService, HandoverService, ResponsibilityView, Scheduler
from .eventstore import EventStore
from .masterdata import MasterDataService, MasterDataView
from .timemodel import Clock, SystemClock


class Backend:
    def __init__(self, store: EventStore | None = None, clock: Clock | None = None,
                 path: str | Path | None = None) -> None:
        self.store = store or EventStore(path)
        self.clock = clock or SystemClock()
        self.repo = Repository(self.store)
        self.master = MasterDataView(self.store)
        self.master_data = MasterDataService(self.store)
        self.pickups = PickupService(self.store, self.clock, self.repo, self.master)
        self.parcels = ParcelService(self.store, self.clock, self.repo, self.master)
        self.batches = BatchService(self.store, self.clock, self.repo)
        self.leads = LeadService(self.store, self.clock, self.repo, self.master)
        self.handovers = HandoverService(self.store, self.clock, self.repo, self.master)
        self.complaints = ComplaintService(self.store, self.clock, self.repo)
        self.scheduler = Scheduler(self.clock, self.repo)
        self.view = ResponsibilityView(self.repo, self.master)

    @classmethod
    def reopen(cls, path: str | Path, clock: Clock | None = None) -> "Backend":
        """从持久化事件日志重启：状态全部重放恢复。"""
        return cls(store=EventStore(path), clock=clock)

    def refresh(self) -> None:
        """主数据/仓储在同一进程内随提交即时更新；跨进程重放日志后可调用刷新。"""
        self.repo = Repository(self.store)
        self.master = MasterDataView(self.store)
        self.pickups = PickupService(self.store, self.clock, self.repo, self.master)
        self.parcels = ParcelService(self.store, self.clock, self.repo, self.master)
        self.batches = BatchService(self.store, self.clock, self.repo)
        self.leads = LeadService(self.store, self.clock, self.repo, self.master)
        self.handovers = HandoverService(self.store, self.clock, self.repo, self.master)
        self.complaints = ComplaintService(self.store, self.clock, self.repo)
        self.scheduler = Scheduler(self.clock, self.repo)
        self.view = ResponsibilityView(self.repo, self.master)
