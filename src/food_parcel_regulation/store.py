"""持久化：整体快照 + 重启恢复。

用带标签的 JSON 编解码保存全部领域状态（时态档案、验视快照、
线索与包裹状态机、交接时间线、调度器催办计数）。时间一律存
ISO 文本，枚举带类型标签，保证重放后 ``is`` 比较仍然成立。

写入采用临时文件原子替换，避免重启时读到半截快照。
"""

from __future__ import annotations

import json
import os
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum

from . import clues as clues_mod
from . import complaints as complaints_mod
from . import handoff as handoff_mod
from . import parcels as parcels_mod
from . import pickup as pickup_mod
from . import rules as rules_mod
from . import scheduler as scheduler_mod
from . import temporal as temporal_mod

# ---- 枚举与数据类注册表（重放时按标签还原真实类型）----

_ENUMS: dict[str, type[Enum]] = {}
for _mod in (rules_mod, pickup_mod, parcels_mod, clues_mod,
             complaints_mod, handoff_mod, temporal_mod, scheduler_mod):
    for _name in dir(_mod):
        _obj = getattr(_mod, _name)
        if isinstance(_obj, type) and issubclass(_obj, Enum) and _obj is not Enum:
            _ENUMS[_obj.__name__] = _obj

# Actor 角色枚举单独加入
from .actors import Org, Role  # noqa: E402

_ENUMS["Role"] = Role
_ENUMS["Org"] = Org

_DATACLASSES: dict[str, type] = {}
for _mod in (temporal_mod, pickup_mod, rules_mod, clues_mod, parcels_mod,
             complaints_mod, handoff_mod, scheduler_mod):
    for _name in dir(_mod):
        _obj = getattr(_mod, _name)
        if is_dataclass(_obj) and isinstance(_obj, type):
            _DATACLASSES[_obj.__name__] = _obj


def encode(value):
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, Enum):
        v = value.value
        return {"__enum__": type(value).__name__, "v": v}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__dc__": type(value).__name__,
            "f": {f.name: encode(getattr(value, f.name)) for f in fields(value)},
        }
    if isinstance(value, frozenset):
        return {"__fset__": [encode(x) for x in value]}
    if isinstance(value, tuple):
        return {"__tuple__": [encode(x) for x in value]}
    if isinstance(value, list):
        return [encode(x) for x in value]
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return {k: encode(v) for k, v in value.items()}
        return {"__dict__": [[encode(k), encode(v)] for k, v in value.items()]}
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(x) for x in value]
    if isinstance(value, dict):
        if "__dt__" in value:
            return datetime.fromisoformat(value["__dt__"])
        if "__enum__" in value:
            return _ENUMS[value["__enum__"]](value["v"])
        if "__fset__" in value:
            return frozenset(decode(x) for x in value["__fset__"])
        if "__tuple__" in value:
            return tuple(decode(x) for x in value["__tuple__"])
        if "__dict__" in value:
            return {decode(k): decode(v) for k, v in value["__dict__"]}
        if "__dc__" in value:
            cls = _DATACLASSES[value["__dc__"]]
            kwargs = {k: decode(v) for k, v in value["f"].items()}
            return cls(**kwargs)
        return {k: decode(v) for k, v in value.items()}
    return value


# ---- 注册表级快照组装/还原 ----

def _versions_of(registry) -> dict:
    return encode(registry._versions)


def _restore_versions(registry, raw) -> None:
    registry._versions = decode(raw)


def snapshot(service) -> dict:
    profiles = {}
    for cid, profile in service._profiles.items():
        profiles[cid] = {
            "qualifications": _versions_of(profile.qualifications),
            "premises": _versions_of(profile.premises),
            "warehouses": _versions_of(profile.warehouses),
            "codes": _versions_of(profile.codes),
        }
    return {
        "format": 1,
        "rules": encode(service.rules._rules),
        "profiles": profiles,
        "pickup_records": encode(service.pickups._records),
        "quarantine": encode(service.pickups._quarantine),
        "pickup_seq": service.pickups._seq,
        "clues": encode(service.clues._clues),
        "clue_seq": service.clues._seq,
        "parcels": encode(service.parcels._parcels),
        "scans": encode(service.parcels._scans),
        "duties": encode(service.parcels._duties),
        "duty_seq": service.parcels._duty_seq,
        "complaints": encode(service.complaints._items),
        "complaint_seq": service.complaints._seq,
        "handoffs": encode(service.handoffs._handoffs),
        "handoff_seq": service.handoffs._seq,
        "timeline": encode(service.handoffs.timeline),
        "scheduler": encode(service.scheduler.state()),
    }


def restore(service, raw: dict) -> None:
    service.rules._rules = decode(raw["rules"])

    for cid, prow in raw["profiles"].items():
        profile = temporal_mod.CustomerProfile(customer_id=cid)
        _restore_versions(profile.qualifications, prow["qualifications"])
        _restore_versions(profile.premises, prow["premises"])
        _restore_versions(profile.warehouses, prow["warehouses"])
        _restore_versions(profile.codes, prow["codes"])
        service._profiles[cid] = profile

    service.pickups._records = decode(raw["pickup_records"])
    service.pickups._quarantine = decode(raw["quarantine"])
    service.pickups._seq = raw.get("pickup_seq", 0)

    service.clues._clues = decode(raw["clues"])
    service.clues._seq = raw.get("clue_seq", 0)

    service.parcels._parcels = decode(raw["parcels"])
    service.parcels._scans = decode(raw["scans"])
    service.parcels._duties = decode(raw["duties"])
    service.parcels._duty_seq = raw.get("duty_seq", 0)

    service.complaints._items = decode(raw["complaints"])
    service.complaints._seq = raw.get("complaint_seq", 0)

    service.handoffs._handoffs = decode(raw["handoffs"])
    service.handoffs._seq = raw.get("handoff_seq", 0)
    service.handoffs.timeline = decode(raw["timeline"])

    service.scheduler.restore_state(decode(raw.get("scheduler", {})))


def save(path, service) -> None:
    data = json.dumps(snapshot(service), ensure_ascii=False, indent=2)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.replace(tmp, path)


def load(path, service) -> None:
    with open(path, encoding="utf-8") as fh:
        restore(service, json.load(fh))
