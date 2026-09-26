# C919新航点保障放行

本项目服务于C919航点启用的**逐航点放行**：把机场、机型、航线、保障单位、人员资质、备件工具、廊桥兼容和演练结果组装成可冻结、可重算、可回放的能力版本，让签派员在放行前看清尚缺条件、替代措施和最后责任人。

## 领域事实

- 南航C919航点已由四个扩展到十七个
- 广州枢纽建立飞机流、旅客流和行李流监控
- 新航点设置专属值机、航显和业务骨干保障

## 核心规则

| 编号 | 规则 | 实现 |
| --- | --- | --- |
| R1 | 新航季/换机计划冻结当日能力版本，当日航班不受后续修订影响 | `frozen_version` |
| R2 | 人员调班、设备故障、强对流、临时机位触发重算，历史评估保留 | `assessment_history` |
| R3 | 已售票航班禁止静默降级，降级须显式审批并登记替代措施 | `assess` / `SilentDowngradeError` |
| R4 | 机场与航司并发确认只产生一个生效结论，仲裁与提交顺序无关 | `arbitrate` |
| R5 | 敏感维修资料按岗位隔离（签派员/机场运行只见脱敏标记） | `redact_for_role` |
| R6 | 断网补录保留真实发生时间，记录早于发生即拒绝 | `timeline` / `validate_event_times` |
| R7 | 放行前呈现缺口、替代措施与最后责任人 | `dispatcher_view` |

## 目录说明

- `docs/domain-model.md` 领域模型：实体、状态机、岗位可见性、回放规则。
- `contracts/` 交换数据结构约定（`context.schema.json`、`release.schema.json`）。
- `fixtures/` 去标识领域样例（`context.json`、`release.json`）。
- `src/catalog.py` 领域上下文读取；`src/release.py` 放行规则引擎；`src/server.py` HTTP 入口。
- `tests/` 核对领域资料载入与七条规则。

## 本地检查

```
python3 -m unittest discover -s tests -v
```

## HTTP 入口

启动 `python3 -m src.server` 后：

- `GET /release?role=dispatcher` 签派放行视图（角色：dispatcher/airport_ops/maintenance/auditor）
- `GET /confirmations` 机场与航司确认仲裁结果（唯一 effective）
- `GET /timeline?stream=aircraft|passenger|baggage` 按真实发生时间回放事件流
