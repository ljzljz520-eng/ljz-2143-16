# C 背景窗口 × Web 控制台：共建事务设计

> 场景：管理员在 Web 控制台编辑**工作副本**（编辑图片、缩放、标题、暗化），
> 保存草稿（带版本条件），然后登记**发布方案**；C 背景窗口设备（终端渲染器）
> 先在本地**预备资源**（下载图片、解码、能力校验），再**整版原子提交**到现场，
> 最后回送**设备确认**。预览只属于当前编辑会话，取消必须撤销临时纹理。

## 1. 为什么不能说"一个数据库事务包办所有步骤"

一次"发布并上屏"跨越三类互相没有共享事务的资源：

| 资源 | 动作 | 失败方式 |
|---|---|---|
| SQLite（`runtime/console.db`） | 登记方案、推进 `config.rev`（CAS） | 锁冲突、磁盘故障 |
| 图片下载/读盘（内容寻址文件 `runtime/images/`） | 设备把文件读入并解码 | 文件缺失、校验和不符、不可解码、太慢 |
| C 终端渲染器（`device/device`） | 暂存纹理 → 锁内整版指针交换 | 字段能力不支持、管道断裂、**确认丢失** |

数据库的 `BEGIN…COMMIT` 无法把"设备 GPU/纹理是否已经切换"纳入同一个提交点。
因此这里的目标不是"全局 ACID"，而是：

1. **原子呈现**：设备端从旧版本到新版本没有中间态、没有半更新（字段撕裂）；
2. **版本安全**：任何管理员都不能覆盖别人已发布的配置；
3. **结果可判定**：任何一步失败/丢消息后，都能凭 `plan_id` 把系统核对到一个确定状态；
4. **预览隔离**：编辑会话的临时纹理永远不污染正式版本。

## 2. 两种"原子呈现"路线对比（本实现采用方案一）

### 方案一：预备资源后提交版本（stage-and-activate，本实现）

```
控制台          数据库(方案/版本)         C 设备
  │  POST /publish  │                       │
  │ ───────────────►│ INSERT plans(prepared)│  ① 登记意图（独立短事务）
  │                 │                       │
  │  PREPARE(全部字段+sha256+本地路径) ─────►│  ② 慢速工作全部在暂存区：
  │                 │                       │     读盘(下载)+校验和+PNG/JPEG解码
  │  ok / 逐字段差异 ◄───────────────────── │     + 四个字段能力校验
  │  (失败即终止，旧版纹丝不动)              │
  │  ACTIVATE(plan_id, new_rev) ───────────►│  ③ 唯一原子点：mutex 内
  │  设备确认 ◄──────────────────────────── │     front = staging（整版交换）
  │ ───────────────►│ 一个DB事务里：        │
  │                 │  UPDATE config SET rev=new … WHERE rev=expected
  │                 │  plans.state=applied  │
```

- **原子性来自设备端的双缓冲指针交换**：`PREPARE` 时慢速 IO/解码全在暂存区，
  前台照常渲染旧版；`ACTIVATE` 只在一个互斥区内做 `front = staging`，
  渲染线程任何时刻看到的都是完整的旧版或完整的新版，没有字段撕裂。
- **提交顺序**：设备先切换（可逆性差但单点），数据库随后以 `WHERE rev=expected`
  做 CAS 推进；CAS 失败意味着有人抢先，此时以设备现场/方案核对收敛（见 §6）。
- **幂等键**：`plan_id`。设备保留最近若干已应用的 plan；同一个 `ACTIVATE`
  重放返回 `already-active` 而不会二次切换——这是"确认丢失"安全恢复的基础。

### 方案二：逐字段补偿（Saga，未采用）

先逐字段下发：图片 → 缩放 → 标题 → 暗化；每步失败执行逆操作补偿。

| | 方案一 stage-and-activate | 方案二 逐字段 Saga |
|---|---|---|
| 现场中间态 | 无（单指针交换） | **有**：屏幕会短暂出现"新图+旧标题"等撕裂组合 |
| 失败后恢复 | 整版拒绝，旧版天然保留 | 依赖每步补偿正确；暗化/纹理等补偿本身也可能失败 |
| 能力差异处理 | 预备阶段一次性聚合所有字段差异 | 前面字段已生效才发现后面字段不支持，需回滚 |
| 复杂度 | 设备需双缓冲 | 协调器需为每字段写补偿动作 |

对"背景窗口整版呈现"这种用户可见场景，撕裂不可接受，故选方案一。
Saga 的残余思想只保留在**运维侧**：`reconcile`（核对）而不是自动补偿字段。

## 3. 数据模型（SQLite）

- `config(id=1, rev, title, zoom, darken, image_sha256, image_w/h, updated_*, published_plan)`
  —— **期望状态**：最近一次被设备确认过的版本。推进只允许
  `UPDATE … WHERE rev=<expected>`（CAS）。
- `drafts(draft_id, admin, base_rev, title, zoom, darken, image_sha256,
  image_decodable, …)` —— **工作副本**。保存时必须带 `base_rev`；
  图片允许不可解码（`image_decodable=0`），草稿仍成立，发布会被设备拒绝。
- `plans(plan_id, admin, expected_rev, new_rev, …, state, device_diff,
  pending_reason, apply_count, applied_at)` —— 发布方案全生命周期：
  `prepared → activating → applied`；异常：`rejected`（能力差异）、
  `failed`（设备拒绝/不可达）、`interrupted`（服务重启中断，待核对）。
- `plan_events` —— 每个动作的审计流（登记、预备、提交、确认丢失、核对…）。

图片按 **sha256 内容寻址**存放（`runtime/images/<前2位>/<sha>.<png|jpeg|bin>`），
设备读文件即模拟"下载"，服务端以 sha256 作为预备资源的等价判定。

## 4. 版本条件（防止覆盖另一位管理员）

两道独立的版本检查：

1. **草稿侧（乐观锁）**：`POST /api/drafts` 必须声明 `base_rev`。
   - `base_rev > 当前 rev` → 409 `BASE_REV_IN_FUTURE`；
   - `base_rev < 当前 rev` → 允许保存，但响应带 `stale_warning=true`；
2. **发布侧（CAS）**：`POST /api/publish` 必须带 `expected_rev`，且
   草稿的 `base_rev == config.rev == expected_rev`，否则
   - `DRAFT_STALE`（草稿基于旧版）或 `REVISION_CONFLICT`（CAS 不满足）→ HTTP 409。

后到管理员的旧草稿被整单拒绝，**现场与 config 都不动**，不会覆盖先发布者。
内容指纹与现场完全一致时走 no-op，不制造新版本（天然幂等）。

## 5. 预览：只属于当前编辑会话的临时纹理

- `PREVIEW_BEGIN(session, sha256, delay_ms)` 在设备里启动一条慢速加载（工作线程），
  结果只作为渲染线程的 **session overlay**，没有任何写库动作、不进暂存区。
- **generation 栅栏**：每次 BEGIN 自增 `gen`；CANCEL 把状态置为 `canceled`。
  加载线程在锁外完成慢速下载后，重新拿锁时若发现
  `gen 已变` 或 `state==canceled`，**直接丢弃已下载的纹理**。
  这精确覆盖"预览图下载慢于取消"：取消先返回、下载后完成，纹理也不会上屏。
- 下一次 BEGIN 取代旧会话；设备事件流记录
  `arrived after cancel, texture discarded` 以便审计。

## 6. 确认丢失与幂等核对（关键）

`ACTIVATE` 发出后超时/管道断裂时，**无法从本地判断设备是否已经切换**。
系统绝不盲目重发，也绝不回滚数据库版本，而是：

1. 方案置 `activating`，`pending_reason="ACTIVATE 结果不确定（可能已应用）"`；
2. API 返回 **HTTP 504 `CONFIRMATION_LOST`**（不是 200！），附
   `plan_id` 与 `/api/plans/<id>/reconcile`；
3. 核对动作：
   - 设备 `STATUS.applied` 含该 plan → 设备已应用，补做数据库 CAS 并置 `applied`；
   - 暂存仍在但未应用 → 幂等重投 `ACTIVATE`（同 plan，设备返回 already-active 也安全）；
   - 设备重启、暂存与 applied 都丢失：
     - 若 config 已含同一版本 → 方案收敛为 `applied`（不重推版本）；
     - 否则置 `interrupted` 并保留待处理原因，等人工决策（不越过版本条件自动重提）。
4. 服务启动时 `startup_sweep` 把残留在 `prepared/activating` 的方案统一挂为
   `interrupted`，提示"ACTIVATE 结果未知，需要 reconcile"。

## 7. 字段能力差异与整版失败

设备能力在 `HELLO` 声明：zoom 25..200%、darken 0..90%、PNG/JPEG 解码。
`PREPARE` 聚合**全部**字段的差异后一次返回，形如：

```
field=zoom  code=UNSUPPORTED_RANGE   requested=300 supported=25..200 unit=%;
field=image code=image_not_decodable path_len=128;
field=darken code=UNSUPPORTED_RANGE  requested=150 supported=0..90 unit=%;
```

- 任一字段不通过 → **整版不暂存**（没有半写入），前台旧版继续渲染；
- API 返回 422 `CAPABILITY_DIFF` + 结构化 `capability_diff[]`；
- 方案登记为 `rejected` 并记录待处理原因；`config.rev` 与现场都不变。
- "标题有效而图片不可解码"正是这个分支：标题/缩放/暗化合法，
  只有图片字段报 `image_not_decodable`，整版失败、旧版保留。

## 8. HTTP 语义与管理页三栏

**HTTP 状态码不等于应用成功**：

| HTTP | 含义 |
|---|---|
| 200 | 请求被处理，业务结果仍要看 `applied` 字段 |
| 409 | 版本条件不满足（草稿过期 / CAS 冲突） |
| 422 | 设备能力差异，整版被拒，旧版保留 |
| 503 | 设备不可达（预备阶段传输失败） |
| 504 | 提交结果不确定（确认丢失），必须凭 plan_id 核对 |

管理页（`GET /`）同时显示三栏，并以设备现场数据作为"是否生效"的唯一裁判：

1. **工作副本（草稿）**：各管理员的 draft、`base_rev`、图片是否可解码；
2. **期望状态（数据库）**：`config` 最新已确认 rev + 未决方案与待处理原因；
3. **现场结果（C 设备）**：`STATUS` 的标题/缩放/暗化/图片 sha、预览叠加态、
   已应用 plan 列表。
   顶部用 ✅/⛔ 明确标出"现场 = 期望"还是"现场 ≠ 期望"。

## 9. 四个演示场景与断言

`make demo`（或 `python3 demo/run_demo.py`）端到端跑 22 条断言：

- **A 预览图下载慢于取消**：1.5s 慢下载，0.1s 后取消 → 设备事件里有
  `arrived after cancel, texture discarded`，现场 sha 不变，预览态为 `canceled`；
- **B 两人基于旧版本保存**：Bob 先发布成功（rev→2），Alice 基于 rev1 发布 →
  409 `DRAFT_STALE`，现场保持 Bob 的标题，rev 只前进一次；
- **C 保存成功但确认响应丢失**（勾选故障注入）：504 + 方案挂 `activating` +
  待处理原因；reconcile 发现设备确实已应用并补登记；再次核对幂等，不产生第二版；
- **D 标题有效而图片不可解码**：草稿可存（标题合法），发布返回
  `field=image / image_not_decodable`，422，整版失败、旧版保留，方案 `rejected`；
- **E（附加）字段不支持**：zoom=300 → `UNSUPPORTED_RANGE`，
  明确给出 `requested=300 supported=25..200`。

## 10. 代码地图

```
device/device.c          C 设备端：双缓冲 front/staging、预览 generation 栅栏、
                         PNG/JPEG 头解码、SHA-256 校验、逐字段能力差异、事件流
server/device_client.py  设备进程客户端（行协议、超时即"结果未知"、故障注入）
server/imglib.py         与设备端一致的 PNG/JPEG 探测（无第三方依赖）
server/app.py            SQLite 模型 + HTTP API + stage-and-activate 编排
                         + 启动扫描 + reconcile
web/index.html           三栏管理页（草稿 / 期望 / 现场），3s 轮询
demo/run_demo.py         四+一 场景的端到端断言
```

### 局限（诚实说明）

- 设备模型为 headless 终端渲染器：解码是真实的 PNG/JPEG 头与尺寸解析，
  但没有逐像素上屏；与 `src/` 下 SDL2 窗口程序共享同一场景模型
  （image/zoom/title/darken），真实 GUI 环境中 `ACTIVATE` 对应
  SDL_Texture 双缓冲交换，语义一致。
- 单设备、单实例；多设备需要在 `plans` 上扩展每设备确认表，
  原子呈现边界仍是"每台设备各自一次指针交换"。
