# 依赖版本兼容解析 API

基于 **Python 3.11 / FastAPI / SQLite** 的依赖版本兼容解析服务。客户端提交组件清单
（组件名、候选版本、每个版本对其他组件的 [semver](https://semver.org/) 依赖范围），
服务端构造**有向依赖图**，并用**确定性回溯搜索**选出一组同时满足全部约束的版本；
当约束无解时，返回**最小冲突链**以及每条约束的来源（谁、哪个版本、沿哪条依赖路径施加）。

相同请求通过 **SHA-256 请求指纹**实现幂等：重复提交直接复用 SQLite 中已有的解析结果。

---

## 端口

| 部署方式 | 监听地址 |
| --- | --- |
| Docker / Compose（默认） | `http://localhost:8080` |
| 本地 uvicorn | `http://127.0.0.1:8080`（可用 `--port` 改） |

容器内固定监听 `8080`；Compose 中通过 `"8080:8080"` 映射到宿主机，
改冒号左侧即可更换宿主端口（如 `"18080:8080"`）。

---

## 快速开始（Docker Compose，推荐）

```bash
docker compose up --build
```

启动时自动完成两件事：

1. **依赖初始化**：依赖在镜像构建阶段安装进容器；
2. **数据库初始化**：应用 lifespan 启动时自动创建数据目录、SQLite 文件与
   `resolutions` 表（无需手动建表）。

健康检查：

```bash
curl http://localhost:8080/health
# {"status":"ok","stored_resolutions":0}
```

SQLite 文件持久化在命名卷 `resolver-data`（容器内 `/data/resolutions.db`）。

启动后可以直接走完一遍“解析 → 再解析 → 对比”的流程（`jq` 用于取出指纹，
没有 `jq` 时可从响应 JSON 手动复制 `fingerprint` 字段）：

```bash
# 1) 提交第一次解析（基线），保存指纹
BASE_FP=$(curl -s -X POST http://localhost:8080/api/v1/resolve \
  -H 'Content-Type: application/json' -d @examples/resolve.json | jq -r .fingerprint)

# 2) 修改清单（如把根要求或候选版本改掉）后提交第二次解析，保存指纹
OTHER_FP=$(curl -s -X POST http://localhost:8080/api/v1/resolve \
  -H 'Content-Type: application/json' -d @examples/resolve.json | jq -r .fingerprint)

# 3) 对比两份已保存结果
curl -s -X POST http://localhost:8080/api/v1/resolutions/compare \
  -H 'Content-Type: application/json' \
  -d "{\"base_fingerprint\": \"$BASE_FP\", \"other_fingerprint\": \"$OTHER_FP\"}" | jq
```

停止服务用 `docker compose down`；加 `-v` 会同时删除 `resolver-data`
数据卷（清空已缓存的解析结果与对比依据）。

---

## 本地运行（无 Docker）

需要 Python 3.10+。

```bash
python -m pip install -r requirements.txt
export DATABASE_PATH=./data/resolutions.db   # 可选，默认 /data/resolutions.db
export SOLVER_MAX_ATTEMPTS=100000            # 可选
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

同样在启动时自动建库建表。交互式文档：

- Swagger UI: <http://localhost:8080/docs>
- ReDoc: <http://localhost:8080/redoc>

---

## API

### 1) 提交解析 `POST /api/v1/resolve`

请求体：

| 字段 | 说明 |
| --- | --- |
| `components[]` | 组件清单 |
| `components[].name` | 组件名（清单内唯一） |
| `components[].candidates[]` | 候选版本列表（版本号在组件内唯一） |
| `candidates[].version` | 候选版本，必须是合法 semver，如 `1.2.3`、`1.2.3-rc.1` |
| `candidates[].dependencies[]` | 该版本对其他组件的依赖：`{name, spec}` |
| `requirements[]` | 顶层（根）依赖要求，至少 1 条：`{name, spec}` |
| `spec` | semver 范围；省略或 `null` 等价于 `*` |

```bash
curl -X POST http://localhost:8080/api/v1/resolve \
  -H 'Content-Type: application/json' \
  -d @examples/resolve.json
```

返回：

- **`201 Created`**：首次求解；
- **`200 OK`**：指纹命中、复用已有结果，响应中 `"reused": true`。

成功（`status = "sat"`）响应示例（节选）：

```json
{
  "fingerprint": "36e45a12…ce26cc",
  "status": "sat",
  "reused": false,
  "assignments": [
    {"name": "web", "version": "1.0.0"},
    {"name": "http", "version": "2.4.0"},
    {"name": "logger", "version": "1.1.0"}
  ],
  "resolution_order": ["web", "http", "logger"],
  "graph": {
    "nodes": ["web", "http", "logger"],
    "edges": [
      {"source": "web", "source_version": "1.0.0",
       "target": "logger", "target_version": "1.1.0", "spec": "~1.1"}
    ]
  },
  "attempts": 5,
  "solve_ms": 0.8
}
```

无解（`status = "unsat"`）响应示例：

```json
{
  "fingerprint": "270b3b9e…",
  "status": "unsat",
  "conflict": {
    "component": "crypto",
    "component_known": true,
    "available_versions": ["0.10.1", "0.10.0", "0.9.5"],
    "constraints": [
      {
        "spec": "^0.10.2",
        "requester": "db",
        "requester_version": "2.0.0",
        "chain": ["root", "platform@1.0.0", "db@2.0.0", "crypto"]
      }
    ],
    "chains": [["root", "platform@1.0.0", "db@2.0.0", "crypto"]],
    "message": "组件 'crypto' 没有任何候选版本能同时满足: ^0.10.2"
  }
}
```

冲突字段含义：

- `component`：第一个被判定为空域的组件；
- `constraints[]`：对该组件构成冲突的**极小约束子集**（逐个尝试删除，删任何一条都将恢复可满足）；
- 每条约束带 `requester` / `requester_version`（约束施加者及其版本，根需求为 `root`）
  与 `chain`（从根沿有向依赖图到冲突组件的完整来源链）；
- `component_known=false` 表示被依赖的组件根本不在清单中。

### 2) 按指纹查询结果 `GET /api/v1/resolutions/{fingerprint}`

```bash
curl http://localhost:8080/api/v1/resolutions/36e45a12…ce26cc
```

存在返回 `200`（`"reused": true`），不存在返回 `404`。

### 3) 对比两份已保存结果 `POST /api/v1/resolutions/compare`

提交两个此前通过 `POST /api/v1/resolve` 得到的指纹，服务按组件对齐两份
`sat` 结果，给出赋值变化、依赖边增删，以及沿依赖图反向边追溯到的受影响
上游组件。

```bash
curl -X POST http://localhost:8080/api/v1/resolutions/compare \
  -H 'Content-Type: application/json' \
  -d '{"base_fingerprint": "36e45a12…ce26cc", "other_fingerprint": "9d02f7…7a1b"}'
```

请求体：

| 字段 | 说明 |
| --- | --- |
| `base_fingerprint` | 基线（旧）解析结果指纹 |
| `other_fingerprint` | 待对比（新）解析结果指纹 |

返回 `200`，响应结构：

```json
{
  "base":  {"fingerprint": "…", "status": "sat", "created_at": "…"},
  "other": {"fingerprint": "…", "status": "sat", "created_at": "…"},
  "components": {
    "added":      [{"name": "cache", "version": "1.0.0"}],
    "removed":    [{"name": "legacy", "version": "1.0.0"}],
    "upgraded":   [{"name": "http", "base_version": "2.4.0", "other_version": "3.0.1"}],
    "downgraded": [{"name": "logger", "base_version": "2.0.0", "other_version": "1.2.5"}],
    "changed":    []
  },
  "edges": {
    "added":   [{"source": "web", "source_version": "2.0.0",
                 "target": "http", "target_version": "3.0.1", "spec": "^3.0.0"}],
    "removed": [{"source": "web", "source_version": "1.0.0",
                 "target": "http", "target_version": "2.4.0", "spec": "^2.0.0"}]
  },
  "affected": [
    {"component": "web", "chain": ["http@3.0.1", "web@2.0.0"], "distance": 1}
  ]
}
```

字段语义：

- **`components`（组件赋值变化）**：按组件名对齐两份赋值后分五类——

  | 类别 | 含义 | 条目字段 |
  | --- | --- | --- |
  | `added` | 仅新结果中存在的组件 | `name`、`version`（新版本） |
  | `removed` | 仅基线中存在的组件 | `name`、`version`（旧版本） |
  | `upgraded` | 两份都有、semver 版本升高 | `name`、`base_version`、`other_version` |
  | `downgraded` | 两份都有、semver 版本降低 | 同上 |
  | `changed` | 其他版本变化（如仅 build metadata 不同：`1.0.0+a` → `1.0.0+b`） | 同上 |

  各类内部按组件名字典序排序；版本完全相同的组件不出现在任何一类中。
- **`edges`（依赖边增删）**：边以五元组
  `(source, source_version, target, target_version, spec)` 唯一标识，
  分别给出新结果相对基线的新增/移除边集合，按五元组字典序排序。下游版本
  升级或 `spec` 改变都会体现为“旧边移除 + 新边新增”。
- **`affected`（受影响上游）**：把上述五类变化组件作为源头，分别沿**两份
  图各自的反向边**做多源 BFS（每条来源链都完整落在某一份图内，不会跨版本
  拼接边）：

  - 新图 BFS 描述新状态下直接/间接依赖变化点的组件；
  - 旧图 BFS 额外覆盖“只有沿已移除组件才能追溯到”的上游；
  - 合并两份图的候选链，每个组件取**边数最少**的一条；等长时取节点标签
    字典序最小的一条。

  每项给出 `component`、`chain`（从变化点 `name@version` 到该组件的完整
  来源链）与 `distance`（链跨越的依赖边数）。变化组件自身不在此列表中。
  结果按 `(distance, chain, component)` 稳定排序；环依赖不会导致死循环。

两个指纹可以相同（返回全空差异）；交换 `base`/`other` 得到对称结果
（升级↔降级、新增边↔移除边）。

### 4) 健康检查 `GET /health`

返回服务状态与已缓存的解析结果数量。

### 错误码

| 状态码 | 场景 |
| --- | --- |
| `422` | 请求体结构/semver 版本号或范围非法、组件或候选版本重复、根要求为空；对比请求缺少指纹字段 |
| `404` | 指纹无对应结果（`GET` 查询，或 `compare` 中任一指纹不存在） |
| `409` | `compare` 的任一结果状态为 `unsat`：无解结果没有赋值图，不可比较 |
| `509` | 求解尝试次数超过 `SOLVER_MAX_ATTEMPTS`（保护性中止，结果不确定） |

---

## 支持的 semver 范围语法

实现遵循 npm semver 的常用语义（见 `app/semverx.py`）：

| 写法 | 含义 |
| --- | --- |
| `1.2.3` | 精确版本（build metadata 如 `+build` 不参与比较） |
| `>=1.2.0` `>1.2.0` `<=` `<` `=` | 比较符，可空格或逗号连接为“与” |
| `~1.2.3` | 允许 `>=1.2.3 <1.3.0`（锁 minor） |
| `^1.2.3` | 允许 `>=1.2.3 <2.0.0`；`^0.2.x`、`^0.0.x` 按 0 主版本规则收窄 |
| `1` `1.2` `1.x` `1.2.*` `*` | 部分版本 / x-range；空串等价 `*` |
| `1.2.0 - 2.0.0` | 连字符范围（右端为部分版本时取该段上界） |
| `^1.0.0 || ^2.0.0` | `||` 表示“或”，组内空格/逗号表示“与” |

预发布规则与 npm 一致：普通范围**不会**匹配预发布版本
（如 `>=1.0.0 <2.0.0` 不匹配 `1.5.0-rc.1`）；只有当某个比较符本身带相同
`major.minor.patch` 核心的预发布时（如 `>=1.5.0-rc.0 <2.0.0`），对应预发布才放行。

---

## 确定性与幂等

- **版本选择确定性**：候选版本统一按 semver 降序；未赋值组件按 MRV
  （剩余可行候选最少，其次按组件名字典序）选择；组件依赖按名字排序处理。
  因此无论清单中元素书写顺序如何，相同输入总得到相同解，且在可行时优先选择高版本。
- **指纹确定性**：请求经规范化（校验 → 组件/候选/依赖/根要求按确定键排序 →
  JSON `sort_keys`）后取 SHA-256。字段顺序、集合元素排列顺序都不影响指纹。
- **并发幂等**：服务对每个指纹加锁并双检，相同请求并发提交只会真正求解一次；
  结果通过 `INSERT OR IGNORE` 落库，重复提交返回 `200` + `reused=true`。
- **对比结果稳定**：组件变化按类别分桶、桶内按名字排序；边按完整五元组
  字典序排序；受影响上游按 `(距离, 来源链, 组件名)` 排序，等长最短链取
  节点标签字典序最小者。相同两份结果无论请求多少次、字段如何排列，对比
  响应完全一致。

---

## 无解时的“最小冲突链”如何得到

求解器在回溯过程中，每当某个组件的可行候选域被清空（或已选版本被新约束打破），
就对当时作用于该组件的约束集合拍快照，并按「约束条数少 → 链短 → 组件名字典序」
保留最优快照。搜索彻底失败后，对最优快照做**包含极小化**：逐条尝试移除约束，
若移除后仍无候选可满足，则该条不属于必要冲突——最终返回的每一条约束删任何一条
都会恢复可满足。每条约束都携带从 `root` 出发的来源链，便于定位是哪个组件的
哪个版本引入了冲突要求。

---

## 项目结构

```
.
├── app/
│   ├── main.py       # FastAPI 路由、lifespan 启动初始化、线程池求解
│   ├── models.py     # Pydantic 请求/响应模型、请求规范化与指纹
│   ├── semverx.py    # 自带 semver 解析与范围匹配引擎
│   ├── solver.py     # 有向依赖图 + 确定性回溯求解器 + 冲突极小化
│   ├── compare.py    # 两份 sat 结果对比：组件/边差异 + 反向边影响面
│   ├── service.py    # 目录构建、求解编排、按指纹加锁复用
│   └── storage.py    # SQLite 持久层（建表、读取、幂等写入）
├── tests/            # pytest：semver 引擎 / 求解器 / 对比逻辑 / API 端到端
├── examples/resolve.json
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/resolutions.db` | SQLite 文件路径 |
| `SOLVER_MAX_ATTEMPTS` | `100000` | 单次解析最大回溯尝试次数 |

---

## 运行测试

```bash
python -m pip install pytest httpx
python -m pytest -q
```

测试覆盖：semver 全部范围语法与预发布门禁、SAT/UNSAT 场景、菱形依赖、
环依赖、深度回溯、冲突链来源与极小化；结果对比的五类组件变化、边增删、
反向边最短来源链（含跨图追溯、环安全与稳定 tie-break）；以及 API 的提交、
幂等复用、指纹查询、对比 `200/404/409/422` 等。

---

## 设计说明与边界

- 清单在单次请求内自包含（不访问外部 registry）；同一组件的所有版本约束求交集，
  全图最终每个组件选定一个版本。
- 依赖图允许存在环（组件版本各自选定即可，安装顺序不在本服务职责内）。
- 求解复杂度随版本数指数增长，MRV 与“最高版本优先”在常见场景下剪枝很快；
  极端规模可通过 `SOLVER_MAX_ATTEMPTS` 限制单次耗时，超限返回 `509`。
