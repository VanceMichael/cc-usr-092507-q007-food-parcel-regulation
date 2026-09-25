# 食品寄递线索协同闭环

连接食品经营资质核验、寄递事件和跨部门风险处置的协同后端。纯 Python 3.11+ 标准库实现，
采用仅追加事件日志（event sourcing），无外部服务依赖。

## 领域范围

系统涉及食品协议客户、快递网点人员、邮政管理人员、市场监管人员。后端落实以下业务规则：

- **时态核验**：客户资质、经营场所、外设仓库、专属核验码、规则书均按半开生效区间
  `[valid_from, valid_to)` 查询；揽件当时的规则版本号随线索固化，历史判定不随规则升级改变。
- **验视最小留存**：揽件只保存当时所见的证照摘要（哈希）、货物类别、温控条件和位置，
  不保存证照影像原件。
- **离线重传幂等**：相同流水 + 相同报文幂等返回首次揽件；相同流水 + 不同报文进入隔离，
  不生成第二次揽件。
- **线索分级**：地址不符、证照过期、包装异常、温控中断四类线索按生效规则版本形成
  info / general / major 不同级别。
- **职权分离**：寄递企业只能暂停尚未发出的包裹；是否核查、放行、立案由市场监管人员独立
  决定；原上报人永远不能关闭或处置自己上报的线索。
- **决定一致性**：企业暂停与监管冻结在单一槽位内互斥、有序替换；决定带单调时间戳，
  旧决定不能覆盖新状态；磁盘上不会留下放行与冻结并存的记录。
- **链路归集**：后续转运、退回、投诉都归入原包裹/关联线索链路。
- **风险传播边界**：风险扩大只波及实际关联批次中的未发出件和在途件；已妥投事实保留，
  转为通知责任；已退回终结件不再处置。
- **移交与扫描并发安全**：部门移交与包裹扫描即使同时发生，冻结期扫描被拒，
  双方接口可见权限范围内责任和完整交接时间线。
- **重启续跑**：温控时限、待核查事项、回执催办全部由事件状态推导，进程重启后继续生效。

## 模块结构

| 模块 | 职责 |
| --- | --- |
| `timemodel.py` | 时钟、生效区间、时态查询辅助 |
| `errors.py` | 领域错误（含隔离、旧决定、越权等） |
| `eventstore.py` | JSONL 仅追加事件存储；一行一个原子批次，`os.replace` 原子落盘 |
| `masterdata.py` | 客户/资质/场所/仓库/核验码/规则书的时态登记与查询 |
| `aggregates.py` | 揽件台账、包裹、批次、线索、移交、投诉的纯函数重放规则与全部不变量 |
| `application.py` | 揽件验视、包裹流转、温控中断、线索处置、风险扩围等命令服务；提交前预演 |
| `collaboration.py` | 部门移交、投诉归链、到期事项调度、权限责任视图与交接时间线 |
| `backend.py` | 装配门面；`Backend.reopen(path)` 从事件日志重启恢复 |

`contracts/domain.schema.json` 定义领域资料结构，`fixtures/domain.json` 提供不含真实身份
信息的示例，`context.py` 负责读取并检查这些资料。

## 快速示例

```python
from datetime import timedelta
from food_parcel_regulation import Backend, PickupObservation
from food_parcel_regulation.timemodel import FakeClock

backend = Backend(clock=FakeClock())
# …登记客户、场所、外设仓库、资质、核验码、发布规则书…

# 首次揽件（设备离线后重传同一报文也安全）
result = backend.pickups.accept(observation, actor="courier:k1")

# 企业只能暂停未发出件；监管独立核查/冻结/放行/立案
backend.parcels.enterprise_hold(result["parcel_id"], "courier:k1")
backend.leads.accept_for_check(result["lead_ids"][0], "regulator:r2")
backend.leads.freeze_parcel(result["parcel_id"], result["lead_ids"][0], "regulator:r2")

# 风险扩围：关联批次中未决件冻结，已妥投件转通知责任
backend.leads.expand_risk(lead_id, "regulator:r2", batch_ids=["batch-1"])

# 到期事项（重启后同样可推导）
backend.scheduler.due_items()
# -> 温控时限 / 待核查 / 回执催办
```

## 持久化与重启

```python
backend = Backend(path="data/events.jsonl")          # 事件原子追加到 JSONL
restored = Backend.reopen("data/events.jsonl")       # 重放恢复全部状态
```

- 每行 JSON 是一个原子批次（一个业务动作涉及的多个聚合事件），重放时整组可见或不可见；
- 命令在提交前用与重放完全相同的 reducer 预演，非法事件整批不落盘。

## 开发命令

- 运行测试：`python3 -m unittest discover -s tests -v`
- 编译检查：`python3 -m compileall -q src`

上述命令只读取仓库内资料，不需要连接外部业务服务。
