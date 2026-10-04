# Visual Window App — C 背景窗口 × Web 控制台共建事务

本仓库包含两部分：

1. **原始 C SDL2 桌面窗口程序**（`src/`，需要 SDL2 + Xvfb/noVNC，见文末）；
2. **背景窗口共建事务系统**（新增）：C 终端渲染设备 + Web 控制台/API/数据库，
   在**无显示、无第三方依赖**的环境下即可运行，演示"工作副本 → 预备资源 →
   整版原子提交 → 设备确认"的完整事务语义。

设计细节见 **[`docs/design.md`](docs/design.md)**。

## 快速开始

```bash
make device          # 编译 C 设备端（gcc + pthread，无需 SDL）
make console         # 启动 Web 控制台 + API + 数据库 + C 设备
# 浏览器打开 http://127.0.0.1:8080 （三栏：草稿 / 期望状态 / 现场结果）

make demo            # 一键运行四个事务场景（自动建库、起服务、断言、收尾）
```

`make demo` 覆盖（共 22 条断言）：

| 场景 | 断言要点 |
|---|---|
| A 预览图下载慢于取消 | generation 栅栏丢弃迟到纹理；临时纹理不污染正式版本；现场 sha 不变 |
| B 两人基于旧版本保存 | 后到者 409 `DRAFT_STALE`，不覆盖先发布者，rev 只前进一次 |
| C 保存成功但确认丢失 | 504 `CONFIRMATION_LOST` + 方案挂起 + 待处理原因；凭 plan_id 幂等核对补登记 |
| D 标题有效图片不可解码 | 草稿可存；发布返回 `field=image` 能力差异；整版失败旧版保留 |
| E 字段不支持（附加） | zoom=300 → `UNSUPPORTED_RANGE, supported=25..200` |

## 事务模型一句话版

保存跨越 **数据库 / 图片下载 / 终端渲染** 三类资源，没有单一分布式事务；
采用 **stage-and-activate**：

- `PREPARE`：设备读盘（下载）+ SHA-256 校验 + PNG/JPEG 解码 + 逐字段能力校验，
  全部成功才进入暂存区；任一失败整版拒绝，**前台旧版不动**；
- `ACTIVATE`：设备 mutex 内 `front = staging` 一次整版指针交换（原子呈现点），
  以 `plan_id` 幂等；
- 确认成功后，数据库在一个事务里以 `WHERE rev=expected_rev`（CAS）推进版本；
- 确认丢失只挂起（`activating`），通过 `reconcile` 核对收敛，绝不盲目重发。

## HTTP API 摘要

| 方法/路径 | 作用 |
|---|---|
| `POST /api/images` | 上传编辑图片（原始字节，内容寻址 sha256；坏图也存，标 `decodable=false`） |
| `POST /api/drafts` | 保存工作副本，必须带 `base_rev`（版本条件） |
| `POST /api/drafts/{id}/preview/begin|cancel` | 会话级预览 / 取消（撤销临时纹理，不写版本） |
| `POST /api/publish` | 登记方案 + 预备 + 原子提交；body 带 `expected_rev`，可 `_lose_confirm` 故障注入 |
| `POST /api/plans/{id}/reconcile` | 凭 plan_id 幂等核对（确认丢失/重启后收敛） |
| `GET  /api/plans` / `…/events` | 发布方案列表 / 单个方案的审计事件 |
| `GET  /api/state` | 三栏数据：`drafts`（工作副本）、`desired`（期望状态）、`actual`（C 设备现场） |
| `GET  /api/device/events` | 设备端事件流（预览丢弃、原子切换等） |

**HTTP 200 只代表请求被处理；是否真正生效以 `/api/state` 的现场（actual）为准。**

HTTP 状态约定：409 版本冲突 ｜ 422 能力差异（整版拒绝、旧版保留）｜
503 设备不可达 ｜ 504 确认结果未知（必须 reconcile）。

## 目录

```
device/device.c          C 设备端（双缓冲、预览栅栏、SHA-256、PNG/JPEG 校验）
server/app.py            API + SQLite + 发布编排 + 重启扫描 + 核对
server/device_client.py  设备进程客户端（超时即"结果未知"）
server/imglib.py         PNG/JPEG 探测
web/index.html           三栏管理页
demo/run_demo.py         四+一场景端到端断言
runtime/                 运行期产物（数据库、图片、设备日志，git 已忽略）
docs/design.md           事务设计与方案对比（stage-and-activate vs 逐字段 Saga）
src/                     原始 SDL2 窗口程序（见下）
```

---

## 附：原始 C SDL2 GUI 程序

基于 SDL2 的真实桌面窗口，渲染 `assets/background.png`；通过
Xvfb + x11vnc + noVNC 在浏览器查看。

```text
Browser (http://localhost:6080) → noVNC → x11vnc → Xvfb → C SDL2 App
```

```bash
docker compose up --build     # 需要 SDL2 / Xvfb / x11vnc / noVNC 镜像
```

- C11 + SDL2 + SDL2_image，严格编译 `-Wall -Wextra -Werror`
- 窗口 1280×720，事件循环处理刷新与关闭
