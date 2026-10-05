# 安全作业规程历史回溯服务（SOP History Service）

基于 **Python 3.12 + FastAPI + SQLite** 的双时间维（bitemporal）规程版本库，
提供可调用的 HTTP API：

- **首次发布**：录入规程编号、正文及 UTC 适用区间 `[起, 止)`；
- **更正提交**：编号 + 被替代版本 + 该版本当前生效区间内的子区间 + 新正文，
  发布时间由服务端记录、不可回填；
- **回溯查询**：输入**作业时刻**与**知悉时刻**，返回截至知悉时刻已发布、
  且在作业时刻有效的正文与版本；无匹配返回空列表。
- **两知悉时刻对比**：输入规程编号、先后两个带时区的知悉时刻与非空作业区间，
  逐段比较同一规程在两侧知悉时刻下对该作业区间的实际适用内容，返回发生
  变化的最大片段（起止 + 两侧版本号/正文，无适用版本一侧为 `null`）。
- **前置规程闭包**：首次发布/更正可携带前置规程编号列表（发布省略视为空、
  更正省略继承被替代版本、显式空列表清空；编号可尚未发布，但不得含空白或
  重复）。按根编号 + 作业时刻 + 知悉时刻在同一读取快照中递归解析，返回
  去重后的根及前置规程清单（前置优先，含编号、版本、正文）；根不存在、
  引用编号不存在或当时无适用正文时说明缺口并整体失败，引用成环时返回环
  路径、不返回部分清单。
- **更正撤回**：值班员发现更正误发布时，提交规程编号与更正版本号撤回；
  撤回时刻由服务端记录、不接受回填。撤回只追加事件、不删除原版本，
  撤回后该更正的适用子区间恢复父版本正文，父版本可在恢复区间再次更正。
- **作业规程包（固定 / 取回 / 复核）**：班组开工前按**作业时刻、知悉时刻及
  一组规程编号**固定作业规程包，返回包标识、各规程当时适用的版本与正文及
  两种时刻；编号重复、缺失或任一规程当时无适用正文时整体拒绝、不留包，
  知悉时刻不得晚于服务端当前时刻。包一经形成即不可变（后来的更正/撤回不
  影响按标识取回）；复核时以不早于包内知悉时刻、不晚于当前时刻的复核知悉
  时刻，按原作业时刻逐项返回包内版本、彼时有效版本及是否变化，并给整体
  是否仍一致。
- **前置规程闭包**：首次发布/更正可携带前置规程编号列表（发布省略视为空、
  更正省略继承被替代版本、显式空列表清空；编号可晚于引用者发布，但不得
  含空白或重复）。按根编号 + 作业时刻 + 知悉时刻在同一读取快照中递归解析，
  返回去重后的根及前置规程清单（前置优先，含编号、版本、正文）；根不存在、
  引用编号不存在或当时无适用正文时说明缺口并整体失败，引用成环时返回环
  路径、不返回部分清单。

更正只新增行、不改写任何旧行（append-only）；撤回同样只向 `withdrawals`
表追加一行事件、从不删除或改写版本行，因此"撤回前知悉时刻的查询结果"天然保持原样。

---

## 一、快速开始（一条命令）

前置：已安装 Docker 与 Docker Compose v2（`docker compose version` 可用）。

```bash
docker compose up --build
```

该命令依次完成：**安装依赖 → 初始化数据库（幂等建表）→ 启动 HTTP 服务**。
数据库文件位于命名卷 `sop-data` 内（容器路径 `/data/sop.db`），容器重建后数据保留。

服务启动后：

| 用途 | 地址 |
| --- | --- |
| API 根（端点列表） | <http://localhost:8080/> |
| 健康检查 | <http://localhost:8080/health> |
| Swagger 交互文档 | <http://localhost:8080/docs> |
| OpenAPI 描述 | <http://localhost:8080/openapi.json> |

后台运行 / 停止 / 清理：

```bash
docker compose up --build -d      # 后台
docker compose logs -f            # 查看日志
docker compose down               # 停止并移除容器（保留数据卷）
docker compose down -v            # 连同数据卷一起删除（清空历史）
```

### 不用 Docker 的本地运行方式

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
SOP_DB_PATH=./sop.db uvicorn app.main:app --host 0.0.0.0 --port 8080
# 数据库表在服务启动时自动创建，无需额外初始化步骤
```

---

## 二、配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SOP_DB_PATH` | `/data/sop.db` | SQLite 数据库文件路径，启动时自动建表 |
| `SOP_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `SOP_PORT` | `8080` | **容器内**监听端口 |
| `SOP_BUSY_TIMEOUT_MS` | `5000` | SQLite 写锁等待毫秒数 |

修改宿主机映射端口（例如改用 18080）：

```bash
SOP_PORT=18080 docker compose up --build
# 此时访问 http://localhost:18080
```

> 说明：`docker-compose.yml` 中端口映射写作 `"${SOP_PORT:-8080}:8080"`，
> 即该变量改变的是宿主机侧端口；容器内始终监听 8080。

---

## 三、API 说明

所有时间均为 **ISO-8601 且必须带时区**（如 `2026-03-01T00:00:00Z` 或
`2026-03-01T08:00:00+08:00`），服务端统一规范化为 UTC 微秒串：
`YYYY-MM-DDTHH:MM:SS.ffffffZ`。区间一律为**左闭右开 `[起, 止)`**。

### 1. 首次发布 `POST /api/v1/releases`

请求体支持单条对象，或 `{"items": [...]}` 批量提交（整批原子，任一非法全部不入库）。

```json
{
  "number": "SOP-001",
  "body": "初版正文：进入车间必须佩戴护目镜。",
  "valid_start": "2026-01-01T00:00:00Z",
  "valid_end":   "2027-01-01T00:00:00Z",
  "prerequisites": ["SOP-100", "SOP-101"]
}
```

- 成功 `201`，返回服务端记录的 `published_at` 与版本号（首版 `revision=0`），
  并回落实际记录的前置规程列表 `prerequisites`。
- `prerequisites` 为**可选**：省略视为空列表（旧请求不带该字段，行为不变）；
  编号可引用尚未发布的规程（前置可晚于引用者发布），但列表不得含空白或
  重复编号（分别 `422 invalid_number` / `422 duplicate_prerequisite_number`）。
- 编号已存在 → `409 duplicate_number`。
- `起 >= 止`、缺时区等 → `422 invalid_interval / invalid_time`。
- 请求中出现 `published_at` 等额外字段 → `422 validation_error`（禁止回填）。

### 2. 提交更正 `POST /api/v1/corrections`

```json
{
  "number": "SOP-001",
  "base_revision": 0,
  "valid_start": "2026-03-01T00:00:00Z",
  "valid_end":   "2026-04-01T00:00:00Z",
  "body": "更正正文：3 月检修期还须穿戴防割手套。"
}
```

语义与校验（任一不满足则**整体拒绝、不写任何历史**）：

1. 子区间必须整体落在被替代版本的适用区间内，否则 `422 invalid_interval`；
2. 子区间必须落在被替代版本的**当前仍生效区间**（自身区间减去其已发布子更正）
   内；落在已被覆盖处 → `409 expired_base_version`（过期基版，响应中带
   `remaining_intervals` 提示当前可更正区间；应改以最新正文版本为基版）；
3. 同一批次中对同一被替代版本的多条更正子区间不得相互重叠，
   否则 `409 concurrent_correction_conflict`，整批回滚；
4. 编号不存在 → `404 not_found`；基版版本号不存在 → `404 base_version_not_found`。

更正版本号 `revision` 在该规程内递增；`supersedes_revision` 标明被替代版本，
支持"更正之更正"的版本链。同批条目按顺序处理，可在同一批中先更正 r0 得到 r1、
再以 r1 为基版继续更正。

更正同样可携带 `prerequisites`（可选）：

- **省略** → 继承被替代版本的前置规程列表（旧请求行为不变）；
- **显式空列表 `[]`** → 清空该版本的前置引用；
- **显式非空列表** → 使用新列表（校验同首次发布：不得含空白或重复编号，
  编号可尚未发布）。

引用记录在新版本行上、随其适用区间/发布时间/撤回自然变化，不改写历史。

### 3. 撤回更正 `POST /api/v1/withdrawals`

值班员发现**更正误发布**时使用。请求体只含规程编号与更正版本号，同样支持
单条对象或 `{"items": [...]}` 批量（整批原子，任一非法全部不追加事件）：

```json
{
  "number": "SOP-001",
  "revision": 1
}
```

- 撤回时刻 `withdrawn_at` 与发布时刻一样**由服务端单调时钟记录，接口不接受
  该字段**（请求中出现 `withdrawn_at` 等额外字段 → `422 validation_error`）。
- **撤回只追加事件**：向 `withdrawals` 表插入一行，绝不删除或改写 versions 行。
- 成功 `201`，返回服务端记录的 `withdrawn_at` 与被撤回版本信息（编号、版本号、
  父版本号、正文、适用区间、原 `published_at`、`withdrawn_at`）。

可撤回条件（任一不满足则**整体拒绝、不追加任何事件、不改变历史**）：

| 情况 | 状态码 | error.code |
| --- | --- | --- |
| 规程编号不存在 | `404` | `not_found` |
| 版本号在该规程下不存在 | `404` | `revision_not_found` |
| 撤回的是**首版** r0（首版只能更正、不能撤回） | `409` | `first_revision_not_withdrawable` |
| 该更正**已经撤回**（撤回事件只追加一次） | `409` | `already_withdrawn`（响应带原 `withdrawn_at`） |
| 该版本**已有子更正**（撤回不删行，子更正即使自身已撤回也仍在历史中；可撤回的是当前仍生效的叶子更正） | `409` | `version_has_children`（响应带 `child_revisions`） |

撤回的双时间维效果：

- **撤回前的知悉时刻**：回溯与区间对比仍按原版本链还原，被撤回版本照常可见
  （可见条件 `published_at <= k` 且未撤回或 `withdrawn_at >= k`，
  两侧边界均含等号，与发布时刻约定对称）；
- **撤回后的知悉时刻**：被撤回更正不再可见，其适用子区间**恢复父版本正文**；
  父版本的可更正剩余区间重新包含该子区间，可直接以父版本为基版**再次更正**
  （以已撤回版本为基版提交更正 → `409 base_version_withdrawn`）；
- **历史接口**始终能看到被撤回版本及其 `withdrawn_at`（见第 4 节）。

撤回与并发更正共用同一把全局写锁，并在同一条 `BEGIN IMMEDIATE` 事务内完成
"资格校验 + 追加事件"，因此二者按确定的全局先后次序生效：对同一版本的并发
撤回只有一份成功（其余得到 `409 already_withdrawn`），任何知悉时刻下同一
作业区间至多存在一份当前正文。

### 4. 回溯查询 `GET /api/v1/effective`

| 查询参数 | 必填 | 说明 |
| --- | --- | --- |
| `work_time` | 是 | 作业时刻（ISO-8601 带时区） |
| `known_as_of` | 否 | 知悉时刻；缺省取服务端当前时刻 |

```bash
curl 'http://localhost:8080/api/v1/effective?work_time=2026-03-15T08:00:00Z&known_as_of=2026-05-01T00:00:00Z'
```

返回（按规程编号排序）：

```json
{
  "work_time": "2026-03-15T08:00:00.000000Z",
  "known_as_of": "2026-05-01T00:00:00.000000Z",
  "count": 1,
  "items": [
    {
      "number": "SOP-001",
      "revision": 1,
      "body": "更正正文：……",
      "valid_start": "2026-03-01T00:00:00.000000Z",
      "valid_end":   "2026-04-01T00:00:00.000000Z",
      "supersedes_revision": 0,
      "published_at": "2026-02-10T03:12:44.918203Z"
    }
  ]
}
```

- 只返回满足 `published_at <= known_as_of`、且 `valid_start <= work_time < valid_end`
  的版本；已撤回版本还须满足撤回时刻不早于知悉时刻（`withdrawn_at >= known_as_of`），
  撤回之后的查询会跳过它并回落到父版本。同一规程存在版本链时返回最深（最新）的
  有效版本；
- 更正仅覆盖指定子区间，其余作业时刻仍返回父版本正文；撤回更正后其子区间恢复
  父版本正文，但以撤回前的知悉时刻查询仍看到被撤回版本（历史不变）；
- 无任何匹配时返回 `200` 与 `{"count": 0, "items": []}`。

### 5. 版本历史（审计视图）`GET /api/v1/procedures/{number}/history`

返回该规程的全部版本，不做知悉时刻裁剪，供审计/排障使用。编号不存在 → `404`。
每个版本均带 `withdrawn_at` 字段：未撤回为 `null`，已撤回事发时为服务端记录的
撤回时刻（版本行本身始终保留）；并带 `prerequisites`：该版本创建时记录的前置
规程编号列表（既有数据无前置行，视为 `[]`）。

### 6. 两知悉时刻对比 `GET /api/v1/procedures/{number}/diff`

值班人员核对"同一段作业、先后两次知悉时看到的规程是否一致"。查询参数全部必填、
均为 ISO-8601 **带时区**时间：

| 参数 | 说明 |
| --- | --- |
| `known_before` | 较早的知悉时刻（严格早于 `known_after`） |
| `known_after` | 较晚的知悉时刻 |
| `work_start` | 作业区间起点（含） |
| `work_end` | 作业区间终点（不含） |

```bash
curl -sS --get 'http://localhost:8080/api/v1/procedures/SOP-001/diff' \
  --data-urlencode 'known_before=2026-02-01T00:00:00Z' \
  --data-urlencode 'known_after=2026-06-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'
```

语义：

- 各侧状态沿**现有更正链按发布时间**还原：仅纳入该知悉时刻下"仍生效"的版本
  （`published_at <= 知悉时刻` 且未撤回或撤回时刻不早于该知悉时刻），
  作业时刻落在哪些版本区间内就沿 `supersedes` 链取最深（最新）者；
  无任何可见版本覆盖时该侧记为 `null`；撤回只影响撤回之后的知悉时刻，
  撤回前一侧仍能看到被撤回版本（历史不变）；
- 用两侧可见版本的全部区间端点对作业区间 `[work_start, work_end)` 做左闭右开
  切分，片段内两侧版本各自恒定；再**合并首尾相接且两侧版本相同的相邻片段**；
- 只返回两侧版本不同的**最大片段**：逐段给出 `start/end` 与
  `before/after`（各自 `{"revision", "body"}` 或 `null`）；两侧版本相同
  （含两侧均无版本）的片段不返回；
- **是否变化只看版本号**：即使新版本正文与旧版完全相同，只要版本不同就算变化。

响应（沿用上面"3 月更正"的数据；前侧知悉时刻早于一切发布）：

```json
{
  "number": "SOP-001",
  "known_before": "2025-01-01T00:00:00.000000Z",
  "known_after": "2026-06-01T00:00:00.000000Z",
  "work_start": "2026-01-01T00:00:00.000000Z",
  "work_end": "2027-01-01T00:00:00.000000Z",
  "count": 3,
  "segments": [
    {"start": "2026-01-01T00:00:00.000000Z", "end": "2026-03-01T00:00:00.000000Z",
     "before": null, "after": {"revision": 0, "body": "初版正文：……"}},
    {"start": "2026-03-01T00:00:00.000000Z", "end": "2026-04-01T00:00:00.000000Z",
     "before": null, "after": {"revision": 1, "body": "更正正文：……"}},
    {"start": "2026-04-01T00:00:00.000000Z", "end": "2027-01-01T00:00:00.000000Z",
     "before": null, "after": {"revision": 0, "body": "初版正文：……"}}
  ]
}
```

无变化时 `200` 且 `{"count": 0, "segments": []}`。错误均为只读、不写任何历史：

| 情况 | 状态码 | error.code |
| --- | --- | --- |
| 规程编号不存在（或为空） | `404` | `not_found` |
| 任一时刻缺时区 / 不可解析 | `422` | `invalid_time` |
| `work_start >= work_end`（空作业区间） | `422` | `invalid_interval` |
| `known_before >= known_after`（知悉顺序颠倒或相等） | `422` | `invalid_known_order` |
| 缺少必填查询参数 | `422` | `validation_error` |

### 7. 作业规程包：固定 / 取回 / 复核

班组开工前把"这次作业涉及哪些规程、按什么时刻认定"一次性固定下来，之后
即使规程被更正或撤回，也能随时取回**固定时的原样内容**，并复核当前看到的
版本是否仍与包内一致。可见性、左闭右开区间、撤回边界全部沿用前述约定。

#### 7.1 固定作业规程包 `POST /api/v1/packages`

```json
{
  "work_time": "2026-03-15T08:00:00Z",
  "known_as_of": "2026-09-30T00:00:00Z",
  "numbers": ["SOP-001", "SOP-002"]
}
```

- `numbers` 非空、不得重复；服务端**保留调用方传入的编号顺序**。
- 固定在**单条 `BEGIN IMMEDIATE` 事务**内完成：事务的一致快照上解析每个
  规程在 `(work_time, known_as_of)` 下适用的版本并整包落库，校验失败即
  回滚——不会拼接并发写入前后的结果，也绝不留下半截包。
- 成功 `201`，返回：

```json
{
  "id": "8c5f0c4f9e1a4b0e9d7a2f6c1b8e3a4d",
  "work_time": "2026-03-15T08:00:00.000000Z",
  "known_as_of": "2026-09-30T00:00:00.000000Z",
  "created_at": "2026-10-05T07:12:31.441902Z",
  "count": 2,
  "items": [
    {"number": "SOP-001", "revision": 1, "body": "……",
     "valid_start": "2026-03-01T00:00:00.000000Z",
     "valid_end":   "2026-04-01T00:00:00.000000Z",
     "supersedes_revision": 0,
     "published_at": "2026-02-10T03:12:44.918203Z"},
    {"number": "SOP-002", "revision": 0, "body": "……", "...": "..."}
  ]
}
```

拒绝情况（任一命中即**整体拒绝、不留包**）：

| 情况 | 状态码 | error.code |
| --- | --- | --- |
| 编号在列表中重复 | `422` | `duplicate_package_number`（details 带 `index`/`number`） |
| 某编号不存在 | `404` | `procedure_not_found`（details 带 `index`/`number`） |
| 某规程在该作业时刻、该知悉时刻下无已发布且生效的适用正文 | `409` | `no_applicable_version` |
| `known_as_of` 晚于服务端当前时刻 | `422` | `known_as_of_in_future` |
| 时刻缺时区/不可解析、编号为空串 | `422` | `invalid_time` / `invalid_number` |
| `numbers` 为空/缺失、出现 `created_at` 等额外字段 | `422` | `validation_error` |

> "无适用正文"覆盖两种数据情形：该知悉时刻下尚无已发布版本，或作业时刻
> 不在任何可见版本的 `[valid_start, valid_end)` 内（含已被撤回且回落不到
> 其他版本时）。固定时刻 `created_at` 由服务端单调时钟记录，接口不接受回填。

#### 7.2 按标识取回 `GET /api/v1/packages/{id}`

返回与固定成功时**完全相同**的响应。包内容（版本号、正文、区间、发布时刻）
在固定时整包复制为快照写入 `package_items`，此后的更正、撤回只作用于
`versions`/`withdrawals`，因此取回结果永远保持原样。标识不存在 →
`404 package_not_found`。

#### 7.3 复核 `GET /api/v1/packages/{id}/review?known_as_of=...`

`known_as_of`（复核知悉时刻）必须**不早于包内知悉时刻**、且**不晚于服务端
当前时刻**；服务端按**包内原作业时刻**、以同一数据快照逐项重新解析彼时有效
版本：

```bash
curl -sS 'http://localhost:8080/api/v1/packages/8c5f0c4f9e1a4b0e9d7a2f6c1b8e3a4d/review?known_as_of=2026-10-05T00:00:00Z'
```

```json
{
  "id": "8c5f0c4f9e1a4b0e9d7a2f6c1b8e3a4d",
  "work_time": "2026-03-15T08:00:00.000000Z",
  "package_known_as_of": "2026-09-30T00:00:00.000000Z",
  "known_as_of": "2026-10-05T00:00:00.000000Z",
  "created_at": "2026-10-05T07:12:31.441902Z",
  "count": 2,
  "all_match": false,
  "items": [
    {"number": "SOP-001",
     "pinned":  {"revision": 1, "body": "包内版本全文……",
                 "valid_start": "……", "valid_end": "……",
                 "supersedes_revision": 0, "published_at": "……"},
     "current": {"revision": 2, "body": "彼时有效版本全文……",
                 "valid_start": "……", "valid_end": "……",
                 "supersedes_revision": 1, "published_at": "……"},
     "changed": true},
    {"number": "SOP-002", "pinned": {"...": "..."},
     "current": {"...": "..."}, "changed": false}
  ]
}
```

- `pinned` 为包内固定版本（快照），`current` 为复核知悉时刻下按原作业时刻
  解析到的有效版本；**是否变化只看版本号**（与第 6 节一致），版本不同即
  `changed=true`；若彼时已无任何适用正文，`current` 为 `null` 且记为变化。
- 所有项均未变化时 `all_match=true`；任一项变化即为 `false`。
- 包读取与逐项解析在同一只读快照事务内完成，`all_match` 基于同一数据版本，
  不会拼入并发写入的中间结果。
- 边界含等号：以**包内知悉时刻本身**复核合法（不早于），且必然完全一致。

错误：

| 情况 | 状态码 | error.code |
| --- | --- | --- |
| 包标识不存在 | `404` | `package_not_found` |
| 复核知悉时刻早于包内知悉时刻 | `422` | `review_known_too_early` |
| 复核知悉时刻晚于服务端当前时刻 | `422` | `known_as_of_in_future` |
| 时刻缺时区/不可解析 | `422` | `invalid_time` |
| 缺少 `known_as_of` 参数 | `422` | `validation_error` |

### 8. 前置规程闭包 `GET /api/v1/procedures/{number}/closure`

班组按一份规程准备作业时，需连同该时点**有效的前置规程**一并核对。首次发布
与更正均可携带 `prerequisites`（前置规程编号列表）：

- **首次发布省略视为空**；**更正省略继承被替代版本**；**显式空列表清空**；
- 前置编号**可晚于引用者发布**（写入时不校验存在性），但列表**不得含空白
  或重复编号**（`422 invalid_number` / `422 duplicate_prerequisite_number`）；
- 引用记录在每个版本行上、只随版本创建写入、从不改写：因此引用随版本的
  适用区间、发布时间与撤回自然变化（沿用双时间维可见性），历史保持不变；
  旧请求（不带该字段）与既有数据（无前置行）均视为空列表，照常可用。

查询参数：

| 查询参数 | 必填 | 说明 |
| --- | --- | --- |
| `work_time` | 是 | 作业时刻（ISO-8601 带时区） |
| `known_as_of` | 否 | 知悉时刻；缺省取服务端当前时刻 |

```bash
curl -sS 'http://localhost:8080/api/v1/procedures/SOP-001/closure?work_time=2026-03-15T08:00:00Z&known_as_of=2026-10-01T00:00:00Z'
```

服务端在**同一只读快照事务**中按现有回溯语义递归解析：先取根规程在
`(work_time, known_as_of)` 下的适用版本，再按该版本创建时记录的前置列表
递归展开（前置优先、按编号去重、根在最后）。返回：

```json
{
  "root": "SOP-001",
  "work_time": "2026-03-15T08:00:00.000000Z",
  "known_as_of": "2026-10-01T00:00:00.000000Z",
  "count": 3,
  "items": [
    {"number": "SOP-100", "revision": 0, "body": "基础规程正文……",
     "valid_start": "……", "valid_end": "……",
     "supersedes_revision": null, "published_at": "……",
     "prerequisites": []},
    {"number": "SOP-200", "revision": 1, "body": "中间规程正文……",
     "prerequisites": ["SOP-100"], "...": "..."},
    {"number": "SOP-001", "revision": 0, "body": "根规程正文……",
     "prerequisites": ["SOP-200", "SOP-100"], "...": "..."}
  ]
}
```

整体失败（说明缺口、**不返回部分清单**）：

| 情况 | 状态码 | error.code |
| --- | --- | --- |
| 根规程编号不存在 | `404` | `not_found` |
| 引用的前置编号不存在（details 带 `number`/`referenced_by`） | `404` | `prerequisite_not_found` |
| 某规程当时无适用正文（details 带缺口编号与时刻） | `409` | `no_applicable_version` |
| 前置引用成环（details 带环路径 `cycle`，如 `["A","B","A"]`） | `409` | `prerequisite_cycle` |
| 时刻缺时区/不可解析 | `422` | `invalid_time` |
| 缺少 `work_time` 参数 | `422` | `validation_error` |

---

## 四、调用示例

- Python（仅标准库）：`python examples/python_client.py`
  （可带参数指定地址：`python examples/python_client.py http://localhost:8080`）
- curl：`sh examples/curl-examples.sh`（可用 `BASE_URL` 环境变量改地址）

示例覆盖：首次发布 → 子区间更正 → 区间外/内查询 → 知悉时刻早于发布返回空 →
过期基版拒绝 → 同批并发更正冲突整批回滚 → 回填发布时间被拒 → 链式更正 → 历史查看
→ 两知悉时刻对比（含 `null` 一侧与顺序颠倒报错）→ **误发布更正的撤回
（撤回后恢复父版本、撤回前历史不变、历史带撤回时刻、恢复区间再次更正、
重复撤回/撤首版/版本不存在/回填撤回时刻各类拒绝、撤回前后对比）**→
**作业规程包（固定并取回原样 → 后续更正不改变包内容 → 复核逐项比对并给整体
结论 → 编号重复/缺失/无适用正文/未来知悉时刻/复核时刻过早/包不存在等报错）**→
**前置规程闭包（发布携带前置且前置可晚发布 → 缺口整体失败 → 补齐后前置优先
去重返回 → 更正继承/显式清空 → 引用随版本区间变化 → 历史逐版本前置列表 →
空白/重复编号与引用环拒绝）**。

---

## 五、设计要点

- **双时间维**：每条版本行同时带适用区间（valid time）与发布时间
  （transaction time）。发布时间由服务端单调时钟生成，接口不接受该字段；
  所有写操作只 `INSERT`、从不 `UPDATE/DELETE` 业务行。
- **撤回即事件**：撤回不改写版本行，只在 `withdrawals` 表追加
  `(version_id, withdrawn_at)`（服务端时钟、`UNIQUE(version_id)`）。版本在
  知悉时刻 k 可见 ⇔ `published_at <= k` 且（未撤回或 `withdrawn_at >= k`）；
  撤回之后该区间恢复父版本正文，父版本剩余区间重新可更正。
- **版本树**：首版 `revision=0`；更正以 `supersedes_id` 指向被替代版本。
  父版本的"仍生效区间"= 自身区间 − 其全部**未撤回**子更正区间。查询时在
  候选版本中沿替代链取最深者。撤回仅允许作用于当前仍生效且没有任何子更正
  （子更正行即使自身已撤回也仍在历史中）的叶子更正；首版不可撤回。
- **作业规程包即快照**：`packages` 记录包的两种时刻与服务端固定时刻
  `created_at`；`package_items` 把每个规程当时适用版本的**正文连同版本号、
  区间、父版本号、发布时刻完整复制**（不做后期 JOIN），因此之后任何
  更正/撤回都只改活动历史、不触及包快照，按标识取回永远是固定时原样。
  固定在写事务的一致快照上"解析 + 落库"原子完成（失败回滚、不留包）；
  复核在显式只读快照事务（`BEGIN` 后事务内读到同一版本）内对所有规程逐项
  重新解析，保证 `all_match` 不拼接并发写入。是否变化只看版本号，撤回后
  回落父版本、无任何适用版本（`current=null`）均记为变化。
- **原子拒绝**：写操作在单条 `BEGIN IMMEDIATE` 写事务内完成"校验 + 插入"，
  进程内写锁串行化；校验失败即回滚，绝不留下半截历史。撤回与并发更正由此
  按确定次序先后生效，同一区间不会并存两份当前正文。
- **前置规程引用随版本走**：`version_prerequisites` 只在版本创建时随版本行
  一同插入（`version_id + position + number`），从不改写；前置编号不做外键，
  允许引用尚未发布的规程。首次发布省略视为空、更正省略时把被替代版本的
  前置行复制到新版本、显式空列表即不插入任何行；既有版本没有前置行，读取
  视为空列表。闭包查询在单一只读快照事务内深度优先展开（前置优先、按编号
  去重、根在最后），缺口（编号不存在/当时无适用正文）与引用环整体失败，
  不返回部分清单。
- **时间存储**：统一存为定长 UTC 字符串，字典序与时间序一致，区间过滤直接用
  字符串比较；WAL 模式下读写不互斥。

## 六、测试

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

共 98 个用例，覆盖双时间维可见性边界（含微秒）、时区偏移规范化、更正仅覆盖
子区间、链式更正、四类整体拒绝、批量原子回滚、两知悉时刻区间对比、**更正撤回
（撤回后恢复父版本正文、撤回前知悉时刻历史不变、撤回时刻边界、首版/重复撤回/
已有子更正/以撤回版本为基版等拒绝、撤回后再次更正、撤回与并发撤回的确定性
次序）**、**作业规程包（固定返回标识/版本/正文/两种时刻、编号重复/缺失/空编号/
无适用正文/未来知悉时刻等整体拒绝且不留包、后续更正与撤回下取回内容不变、
复核逐项 pinned/current/changed 与整体 all_match、复核时刻过早/未来/包不存在
报错、只读快照不受并发写入影响）**、**前置规程闭包（发布携带/省略前置、更正
继承/显式清空/显式覆盖、前置晚于引用者发布、空白与重复编号拒绝、批量原子
回滚、根不存在/引用缺口/当时无适用正文整体失败、自引用与多节点环路径、
引用随知悉时刻与撤回变化、历史逐版本前置列表、单只读快照隔离）**及完整
HTTP 流程。

> 本机未安装 fastapi/httpx 时，HTTP 用例自动跳过（服务层用例不受影响）；
> Docker 镜像与 CI 安装 `requirements-dev.txt` 后全部运行。

## 七、项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由与错误处理
│   ├── service.py       # 领域服务：发布 / 更正 / 撤回 / 回溯 / 对比 /
│   │                    #   前置闭包 / 规程包
│   ├── db.py            # SQLite 连接、建表（含 withdrawals、packages、
│   │                    #   version_prerequisites 等）、写事务与只读快照事务
│   ├── schemas.py       # 请求模型（额外字段一律拒绝）
│   ├── errors.py        # 领域错误（稳定 code）
│   ├── time_utils.py    # UTC 时间解析与规范化
│   └── config.py        # 环境变量配置
├── examples/
│   ├── python_client.py # 标准库 Python 调用示例
│   └── curl-examples.sh # curl 调用示例
├── tests/test_service.py
├── Dockerfile
├── docker-entrypoint.sh
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```
