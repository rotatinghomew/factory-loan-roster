# 园区共享用工召回与履约后端

Python 3.11 标准库实现，无第三方依赖。领域服务、JSON 持久化和 HTTP 接口分别在：

- `shared_labor/service.py`：班次、条款版本、候选班组、时间冲突、召回、接替、考勤对账、工资差额和权限视图。
- `shared_labor/server.py`：JSON HTTP API。
- `shared_labor/report.py`：读取联调快照并输出日终对账报告。
- `tests/test_backend.py`：业务规则与 HTTP 测试。

联调数据采用 [《共享用工交换约定》](交换约定.md) 的字段和时间口径。`data/labor_20261203.json` 含未核认、冲突或变化中的来源事实；领域服务会保留来源记录，再给出核对后的状态。

## 1. 运行与测试

```sh
python3 -m unittest discover -s tests -v
```

启动共享用工 API：

```sh
python3 -m shared_labor --host 127.0.0.1 --port 8080 --clock 13:10
# 或 python app.py --port 8080 --clock 13:10
```

需要跨重启保留通过 API 写入的状态时：

```sh
python3 -m shared_labor --db runtime/state.json --clock 13:10
```

输出快照日终业务报告（会执行已到召回截点且无合规接替的强制停止）：

```sh
mkdir -p runtime
python3 -m shared_labor.report --at 13:10 > runtime/report.json
```

原有的只读交换报文服务仍可单独运行：

```sh
python3 -m http.server 8040 --bind 127.0.0.1 --directory data
```

## 2. 调用身份与隐私范围

本地联调用请求头表明调用方，生产环境应替换为真实账号鉴权：

```http
X-Actor-Factory: FB
X-Actor-Person: W1
```

也可用查询参数 `?as=FB`、`?factory=FB`、`?person=W1`。不显式传身份时，访问路径中的工厂或工人 ID 会作为该查询的本地调用方；但用 A 厂身份请求 B 厂数据会返回 `403`。

- 工人只能读取本人的完整记录。
- 工厂看板只把 `liable_factory`/`reporter` 为本厂的工资凭证放入 `wage_items_only_own_factory` 和 `wage_settlement`。
- 原厂可查看本厂工人的班次、地点、时间、召回截点等管理信息；非本厂承担的小时工资率会脱敏。
- 接收厂可查看本班候选、资质覆盖、在岗回执和本厂工资事项。

## 3. 一个工人的完整记录

```sh
curl -sS http://127.0.0.1:8080/api/workers/W1/record \
  -H 'X-Actor-Person: W1' | python3 -m json.tool
```

返回中包括：

- `work_record[].location/place`：地点；
- `work_record[].start/end`、`shift`：班次；
- `work_record[].pay`、`cents_hour`：当前条款报酬；
- `responsible_units`：工人所属原厂、现场管理/接收厂、工资承担单位；
- `next_work`：接下来要去的地点和时间；
- `promises_by_factory`：对原厂自留班及各接收厂分别作出的承诺；
- `recognized_hours`：由两厂在岗凭据核对出的区间；
- `pay`：工资凭证和补偿；
- `pending_money_or_evidence`：仍缺工资确认、归属争议或补偿凭证的区间；
- `availability_conflicts`、`commitment_conflicts`：快照中保留的时间冲突。

## 4. 日常接班 API

### 原厂报送人员技能、可借时间和自留班

```sh
curl -sS -X POST http://127.0.0.1:8080/api/workers \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Factory: OR' \
  -d '{"id":"W9","origin":"OR","skills":["machine","lead"]}'

curl -sS -X POST http://127.0.0.1:8080/api/availability \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Factory: OR' \
  -d '{"person":"W9","start":"08:00","end":"17:00","kind":"lend"}'

curl -sS -X POST http://127.0.0.1:8080/api/availability \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Factory: OR' \
  -d '{"person":"W9","start":"13:00","end":"14:00","kind":"own"}'
```

`own` 区间优先于 `lend`；重叠超过零分钟会列入 `availability_conflicts`，可借区间计算时扣除自留班。

### 接收厂提开工要求和持证组合

```sh
curl -sS -X POST http://127.0.0.1:8080/api/shifts \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Factory: FB' \
  -d '{
    "id":"S9",
    "host":"FB",
    "start":"09:00","end":"13:00",
    "min_people":2,
    "required_skills":["machine","lead"],
    "terms":{
      "place":"east-line-2",
      "cents_hour":3300,
      "recall_cutoff":"10:10",
      "recall_compensation":1200
    }
  }'

curl -sS http://127.0.0.1:8080/api/shifts/S9/staffing?at=09:00
```

`segments[].can_open` 只有在同一时段同时满足以下条件才为真：

1. 当前条款版本的 `accept` 有效；
2. 人数达到 `min_people`；
3. 在岗人员技能并集覆盖 `required_skills`；
4. 时间完全落在有效可借区间，且不与另一方承诺或自留班重叠。

一项资质可由同一工人持有，但人数下限仍独立计算。缺少必要资质时，系统不会通过“删掉缺资质的人”得到可开班结论；删除后若人数或资质不满足，仍返回 `can_open=false`、`missing_skills` 和人员阻塞原因。

### 工人同意、拒绝、撤回

```sh
curl -sS -X POST http://127.0.0.1:8080/api/consents \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Person: W9' \
  -d '{"person":"W9","shift":"S9","decision":"accept","at":"08:05"}'
```

`decision` 可为 `accept`、`reject`、`withdraw`。同一工人的有效承诺区间与另一方重叠超过零分钟时，新的 `accept` 返回 `409`，错误体含冲突班次、工厂和重叠分钟。

### 地点、计酬或时间变化必须重新询问

```sh
curl -sS -X POST http://127.0.0.1:8080/api/shifts/S9/terms \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Factory: FB' \
  -d '{"cents_hour":3600,"place":"north-line"}'
```

这会生成新的 `revision`，为旧条款接受者创建 `awaiting_response` 请求。旧条款的同意不再计入当前班组；接受旧版本会返回 `409 terms_superseded`，必须接受新版本。传 `start/end` 修改时间也同样生成新版本。

## 5. 召回、接替与强制停止

### 尚未达成承诺

若只有询问/候选、没有有效 `accept`，原厂可收回：

```sh
curl -sS -X POST http://127.0.0.1:8080/api/commitments/revoke-uncommitted \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Factory: OR' \
  -d '{"person":"W9","shift":"S9"}'
```

也可撤销某条可借时间：`POST /api/availability/{id}/revoke`。

### 已答应后的正常交回

```sh
# 原厂申请召回
curl -sS -X POST http://127.0.0.1:8080/api/recalls \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Factory: OR' \
  -d '{"id":"R9","person":"W9","shift":"S9","requested":"09:50","effective":"10:15"}'

# 接收厂请替代人同意当前条款；接口故障会保留 failed outbox
curl -sS -X POST http://127.0.0.1:8080/api/recalls/R9/replacement-request \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Factory: FB' \
  -d '{"person":"W4"}'

# 替代人本人同意当前条款
curl -sS -X POST http://127.0.0.1:8080/api/consents \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Person: W4' \
  -d '{"person":"W4","shift":"S9","decision":"accept","at":"10:01"}'

# 接收厂确认合规接替
curl -sS -X POST http://127.0.0.1:8080/api/recalls/R9/host-confirmation \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Factory: FB' \
  -d '{"replacement":"W4","at":"10:04"}'

# 工人重新同意交回
curl -sS -X POST http://127.0.0.1:8080/api/recalls/R9/worker-response \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Person: W9' \
  -d '{"decision":"accept","at":"10:14"}'
```

系统会模拟召回后的班组：原工人从生效时点移除，替代人从该时点加入；人数和资质仍满足时，`replacement_check.compliant=true`，召回才成为 `completed_return`。

接替申请未回时状态是 `awaiting_replacement_response`；接口异常时是 `replacement_interface_failed`，并在 `outbox` 中留下失败消息。两种情况下 `worker_agreed` 都保持 `false`，不会把工人伪造成“同意撤班”。可调用 `/api/recalls/{id}/retry` 重试。

### 截点后无合规接替

```sh
curl -sS -X POST http://127.0.0.1:8080/api/recalls/R2/force-stop \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Factory: OR' \
  -d '{"at":"10:15"}'
```

条件：

- 当前操作时间已到条款的 `recall_cutoff`；
- 尚无合规接替；
- 停止时点不早于截点且仍有未履行区间。

强制停止后：

- 召回状态为 `force_stopped`；
- `worker_return_consent` 为 `not_agreed_but_contractually_stopped`，原 `accept` 不被改写；
- 生成一次 `recall_compensation` 责任，金额为条款固定补偿；
- 重新核查班组，生成 `shortfalls`，写明缺几人、缺哪些证、影响区间和在岗候选。

日终可批量处理所有已到截点且无合规接替的召回：

```sh
curl -sS -X POST http://127.0.0.1:8080/api/recalls/process-due \
  -H 'Content-Type: application/json' \
  -d '{"at":"13:10"}'
```

## 6. 在岗回执与争议

```sh
curl -sS -X POST http://127.0.0.1:8080/api/attendance \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Factory: FB' \
  -d '{"person":"W1","shift":"S1","start":"09:00","end":"10:17","received":"10:22"}'
```

迟到的回执不会覆盖或丢弃另一厂记录。系统按人员合并所有在岗区间，并在断点处核对：

- 只有一厂记录或两厂不重叠：`confirmed`；
- 两厂记录在同一分钟段重叠：实际在岗事实保留，状态为 `attribution_disputed`，生成 `D...` 争议交两厂核对；
- 两厂确认责任单位后，可通过 `/api/disputes/{id}/check` 记录双方核对，或由管理员 `/resolve`。

种子数据的核对结果为：

| 区间 | 状态 | 凭据 |
|---|---|---|
| 09:00–10:15 | FB 单源确认 | E1 |
| 10:15–10:17 | 两厂重叠、归属争议 | E1/E2 |
| 10:17–10:50 | OR 单源确认 | E2 |

## 7. 工资确认与差额

工资金额按适用条款的 `cents_hour × 分钟 / 60` 计算，四舍五入到整数分。

```sh
curl -sS 'http://127.0.0.1:8080/api/settlement?person=W1&shift=S1'
```

种子中：

- `M1/BASE-W1`：09:00–10:15，75 分钟，金额 `4125`；
- `M2/DIFF-W1`：10:15–10:17，2 分钟，差额 `110`；
- 已确认工资合计 `4235`，不会把整个 09:00–13:00 班次再发一次。

若后续凭证是覆盖旧区间的累计总额，提交时带 `"cumulative": true`，接口只确认 `新总额 - 已确认总额`。也可调用：

```sh
curl -sS -X POST http://127.0.0.1:8080/api/pay-difference \
  -H 'Content-Type: application/json' \
  -d '{"person":"W1","shift":"S1","revised_interval":{"start":"09:00","end":"10:17"},"revised_amount":4300}'
```

返回 `already_confirmed_amount=4235`、`difference_only_amount=65`。

## 8. 工厂看板

```sh
curl -sS http://127.0.0.1:8080/api/factories/FB \
  -H 'X-Actor-Factory: FB' | python3 -m json.tool

curl -sS http://127.0.0.1:8080/api/factories/OR \
  -H 'X-Actor-Factory: OR' | python3 -m json.tool
```

看板包含本厂开班状态、班组分段、召回/接替、与本厂相关的在岗记录、欠班、争议，以及严格按工资责任单位过滤的结算。

## 9. 主要 HTTP 路由

| 方法与路径 | 用途 |
|---|---|
| `GET /api/workers/{id}/record` | 工人一站式记录 |
| `POST /api/workers` / `POST /api/workers/{id}/skills` | 人员与胜任资质 |
| `GET/POST /api/availability` | 可借时间、自留班 |
| `POST /api/availability/{id}/revoke` | 撤回未达成承诺对应的时间 |
| `POST /api/commitments/revoke-uncommitted` | 未接受前收回邀约/询问 |
| `GET/POST /api/shifts` | 开工要求 |
| `GET /api/shifts/{id}/staffing` | 真实班组资质/人数核查 |
| `POST /api/shifts/{id}/terms` | 地点、计酬、时间等条款升版并重新询问 |
| `POST /api/shifts/{id}/ask` | 请某个工人对当前条款表态 |
| `GET/POST /api/consents` | 同意、拒绝、撤回 |
| `POST /api/recalls` | 原厂申请召回 |
| `POST /api/recalls/{id}/worker-response` | 工人重新同意/拒绝交回 |
| `POST /api/recalls/{id}/replacement-request` | 发起替代人接口 |
| `POST /api/recalls/{id}/retry` | 重试故障接口 |
| `POST /api/recalls/{id}/host-confirmation` | 接收厂确认合规接替 |
| `POST /api/recalls/{id}/force-stop` | 截点后停止未履行部分 |
| `POST /api/recalls/process-due` | 日终/定时批量处理 |
| `GET/POST /api/attendance` | 两厂在岗凭据与核对 |
| `GET /api/disputes` / `POST /api/disputes/{id}/check` | 归属争议与两厂核对 |
| `GET/POST /api/pay-acks` | 工资确认凭证 |
| `GET /api/settlement` / `POST /api/pay-difference` | 结算与差额 |
| `GET /api/shortfalls` / `GET /api/liabilities` | 欠班与补偿责任 |
| `POST /api/liabilities/{id}/ack` | 报送补偿支付/确认凭证 |
