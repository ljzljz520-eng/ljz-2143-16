"""app.py — Web 控制台 + API + 数据库登记（背景窗口共建事务）

设计要点（详见 docs/design.md）：
  * 保存流程跨越 数据库事务 / 图片下载 / C 终端渲染 三类资源，没有任何单一
    分布式事务；本实现采用「预备资源后提交版本 + 设备端整版原子指针交换」
    （stage-and-activate），而不是逐字段补偿（Saga）。
  * 草稿保存走版本条件（乐观锁 base_rev）；发布走 CAS（expected_rev）。
    两位管理员基于旧版本保存时，后到的一位不会覆盖已发布配置。
  * HTTP 200 只代表"请求被处理"；applied 字段与现场轮询才代表是否真正上屏。
  * 设备端确认丢失（activate 超时）-> 计划停在 activating，可凭 plan_id 幂等核对。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from device_client import DeviceClient, DeviceTransportError  # noqa: E402
import imglib  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, "runtime")
IMGDIR = os.path.join(RUNTIME, "images")
DBPATH = os.path.join(RUNTIME, "console.db")
WEB = os.path.join(ROOT, "web")
DEVICE_BIN = os.path.join(ROOT, "device", "device")
SEED_SRC = os.path.join(ROOT, "assets", "background.png")

os.makedirs(IMGDIR, exist_ok=True)

PUBLISH_LOCK = threading.Lock()   # 发布方案串行（避免并发交错提交）

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
  id INTEGER PRIMARY KEY CHECK (id=1),
  rev INTEGER NOT NULL,
  title TEXT NOT NULL,
  zoom INTEGER NOT NULL,
  darken INTEGER NOT NULL,
  image_sha256 TEXT NOT NULL,
  image_path TEXT NOT NULL,
  image_w INTEGER NOT NULL,
  image_h INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  published_plan TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
  draft_id TEXT PRIMARY KEY,
  admin TEXT NOT NULL,
  base_rev INTEGER NOT NULL,
  title TEXT NOT NULL,
  zoom INTEGER NOT NULL,
  darken INTEGER NOT NULL,
  image_sha256 TEXT NOT NULL,
  image_path TEXT NOT NULL,
  image_decodable INTEGER NOT NULL,
  image_w INTEGER NOT NULL,
  image_h INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
  plan_id TEXT PRIMARY KEY,
  admin TEXT NOT NULL,
  expected_rev INTEGER NOT NULL,
  new_rev INTEGER NOT NULL,
  title TEXT NOT NULL,
  zoom INTEGER NOT NULL,
  darken INTEGER NOT NULL,
  image_sha256 TEXT NOT NULL,
  image_path TEXT NOT NULL,
  state TEXT NOT NULL,            -- prepared|rejected|activating|applied|failed|interrupted
  device_diff TEXT,
  pending_reason TEXT,
  apply_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  applied_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL
);
"""


def now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


class DB:
    def __init__(self, path: str):
        self.local = threading.local()
        self.path = path

    def conn(self) -> sqlite3.Connection:
        c = getattr(self.local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            self.local.conn = c
        return c

    def begin(self):
        self.conn().execute("BEGIN IMMEDIATE")

    def commit(self):
        self.conn().execute("COMMIT")

    def rollback(self):
        self.conn().execute("ROLLBACK")


db = DB(DBPATH)

# ---------------------------- 初始化 ----------------------------

def seed_config() -> sqlite3.Row:
    c = db.conn()
    row = c.execute("SELECT * FROM config WHERE id=1").fetchone()
    if row:
        return row
    with open(SEED_SRC, "rb") as f:
        data = f.read()
    dec, fmt, w, h = imglib.probe(data)
    assert dec, "seed image must be decodable"
    sha = imglib.sha256_bytes(data)
    dst = os.path.join(IMGDIR, sha[:2])
    os.makedirs(dst, exist_ok=True)
    path = os.path.join(dst, sha + "." + fmt)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    ts = now_iso()
    db.begin()
    try:
        c.execute("""INSERT INTO config
            (id,rev,title,zoom,darken,image_sha256,image_path,image_w,image_h,
             updated_at,updated_by,published_plan)
            VALUES (1,1,?,100,0,?,?,?,?,?,'system',NULL)""",
                  ("初始背景", sha, path, w, h, ts))
        c.execute("""INSERT INTO plan_events(plan_id,at,actor,kind,message)
                    VALUES (NULL,?,'system','seed','初始发布 rev=1（随服务播种）')""", (ts,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return c.execute("SELECT * FROM config WHERE id=1").fetchone()


# ---------------------------- 领域辅助 ----------------------------

def log_event(plan_id: str | None, actor: str, kind: str, message: str) -> None:
    db.conn().execute(
        "INSERT INTO plan_events(plan_id,at,actor,kind,message) VALUES (?,?,?,?,?)",
        (plan_id, now_iso(), actor, kind, message))


def store_image(data: bytes) -> dict:
    sha = imglib.sha256_bytes(data)
    dec, fmt, w, h = imglib.probe(data)
    ext = fmt if dec else "bin"
    d = os.path.join(IMGDIR, sha[:2])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sha}.{ext}")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return {"sha256": sha, "path": path, "decodable": dec, "format": fmt,
            "width": w, "height": h, "bytes": len(data)}


def parse_diff(text: str) -> list[dict]:
    """field=image code=image_not_decodable path_len=..;  -> dict list"""
    out = []
    for part in (text or "").split(";"):
        part = part.strip()
        if not part:
            continue
        item = {}
        for kv_ in part.split():
            if "=" in kv_:
                k, v = kv_.split("=", 1)
                item[k] = v
        out.append(item)
    return out


def desired_payload(cfg: sqlite3.Row, state: str | None, plan: sqlite3.Row | None) -> dict:
    d = {"rev": cfg["rev"], "title": cfg["title"], "zoom": cfg["zoom"],
         "darken": cfg["darken"], "image_sha256": cfg["image_sha256"],
         "image_w": cfg["image_w"], "image_h": cfg["image_h"],
         "updated_at": cfg["updated_at"], "updated_by": cfg["updated_by"],
         "published_plan": cfg["published_plan"]}
    if state and plan:
        d["pending"] = {"state": state, "plan_id": plan["plan_id"],
                        "new_rev": plan["new_rev"], "reason": plan["pending_reason"]}
    return d


# ---------------------------- 发布核心：stage-and-activate ----------------------------

def reconcile_plan(plan_id: str) -> dict:
    """凭 plan_id 幂等核对：设备可能已经应用，只是确认丢了。"""
    c = db.conn()
    plan = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    if not plan:
        return {"ok": False, "error": "plan_not_found"}
    result: dict = {"plan_id": plan_id, "state": plan["state"]}
    if plan["state"] in ("applied", "rejected", "failed"):
        result["ok"] = plan["state"] == "applied"
        result["note"] = "终态，无需核对"
        return result

    # prepared / activating / interrupted -> 问设备现场
    try:
        st = device.status()
    except DeviceTransportError as e:
        c.execute("UPDATE plans SET pending_reason=?, updated_at=? WHERE plan_id=?",
                  (f"核对时设备不可达: {e}", now_iso(), plan_id))
        log_event(plan_id, "system", "reconcile-unreachable", str(e))
        result.update(ok=False, state=plan["state"], pending_reason=str(e))
        return result

    applied = st.get("applied", "")
    ids = {x.split(":")[0] for x in applied.split(",") if x}
    staging_id = st.get("staging", "-")
    db.begin()
    try:
        if plan_id in ids:
            # 设备实际上已经应用 —— 典型的"确认响应丢失"
            _mark_applied(plan, st)
            log_event(plan_id, "system", "reconcile-applied",
                      "核对发现设备已应用（此前确认丢失），补登记")
            result.update(ok=True, state="applied", note="device had applied; now recorded")
        elif plan["state"] in ("prepared", "activating") and staging_id == plan_id:
            # 暂存仍在设备上：ACTIVATE 是按 plan_id 幂等的，安全重投
            log_event(plan_id, plan["admin"], "retry-activate",
                      "暂存仍在，核对后幂等重投 ACTIVATE")
            _do_activate(plan)
            result.update(ok=True, state="applied", note="activate delivered on reconcile")
        elif plan["state"] in ("prepared", "activating"):
            # 暂存已不在（例如设备重启），但方案从未被确认应用 ->
            # 重新 PREPARE（幂等）再 ACTIVATE；版本条件仍以计划登记时的 expected_rev 为准
            log_event(plan_id, plan["admin"], "retry-prepare",
                      "暂存已不在设备，重新预备资源后再提交")
            db.commit()  # 设备 IO 不持有写事务
            prep = device.prepare(plan_id, plan["new_rev"], plan["image_path"],
                                  plan["image_sha256"], plan["title"],
                                  plan["zoom"], plan["darken"])
            db.begin()
            if prep.get("ok") != "1":
                c.execute("UPDATE plans SET state='interrupted', pending_reason=?, updated_at=? "
                          "WHERE plan_id=?",
                          (f"核对重预备被设备拒绝: {prep.get('changes')}",
                           now_iso(), plan_id))
                log_event(plan_id, "system", "retry-prepare-rejected",
                          prep.get("changes", ""))
                db.commit()
                result.update(ok=False, state="interrupted",
                              pending_reason=prep.get("changes"))
                return result
            fresh = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            _do_activate(fresh)
            result.update(ok=True, state="applied",
                          note="re-prepared and activated on reconcile")
        else:
            # 设备现场没有这个 plan（典型：设备重启）。能否安全重试，取决于
            # 该方案的新版本是否已经被其他路径确认进 config。
            cfg_now = c.execute("SELECT * FROM config WHERE id=1").fetchone()
            same_as_config = (
                cfg_now["rev"] >= plan["new_rev"]
                and cfg_now["title"] == plan["title"]
                and cfg_now["zoom"] == plan["zoom"]
                and cfg_now["darken"] == plan["darken"]
                and cfg_now["image_sha256"] == plan["image_sha256"])
            if same_as_config:
                # config 已包含同一版本：不需要、也不允许再把 rev 往前推。
                # 把方案收敛为 applied（与"确认丢失后核对发现已应用"等价）。
                c.execute("UPDATE plans SET state='applied', applied_at=?, updated_at=?, "
                          "pending_reason=NULL WHERE plan_id=?",
                          (now_iso(), now_iso(), plan_id))
                log_event(plan_id, "system", "reconcile-superseded",
                          "设备重启丢失 applied 记录，但 config 已含同一版本，方案收敛为 applied")
                result.update(ok=True, state="applied",
                              note="config already carries this version; plan converged")
            else:
                c.execute("UPDATE plans SET state='interrupted', pending_reason=?, updated_at=? "
                          "WHERE plan_id=?",
                          ("设备现场未包含该方案，且 config 尚未包含该版本，"
                           "需要人工决定重试或放弃（不自动重提，避免越过版本条件）",
                           now_iso(), plan_id))
                log_event(plan_id, "system", "reconcile-missing",
                          "设备 applied 列表无此 plan，挂起待处理")
                result.update(ok=False, state="interrupted",
                              pending_reason="设备现场未包含该方案")
        db.commit()
    except DeviceTransportError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    return result


def _mark_applied(plan: sqlite3.Row, st: dict) -> None:
    """设备确认成功（或核对发现已应用）：在一个数据库事务里
    1) 推进 config 版本 CAS；2) 计划置 applied。"""
    c = db.conn()
    ts = now_iso()
    cur = c.execute("""UPDATE config SET rev=?, title=?, zoom=?, darken=?,
                  image_sha256=?, image_path=?, image_w=?, image_h=?,
                  updated_at=?, updated_by=?, published_plan=?
                  WHERE id=1 AND rev=?""",
                    (plan["new_rev"], plan["title"], plan["zoom"], plan["darken"],
                     plan["image_sha256"], plan["image_path"],
                     int(st.get("live_w", 0) or 0), int(st.get("live_h", 0) or 0),
                     ts, plan["admin"], plan["plan_id"], plan["expected_rev"]))
    if cur.rowcount != 1:
        raise RuntimeError("config CAS failed during _mark_applied")
    c.execute("""UPDATE plans SET state='applied', applied_at=?, updated_at=?,
                  apply_count=apply_count+1, pending_reason=NULL WHERE plan_id=?""",
              (ts, ts, plan["plan_id"]))
    log_event(plan["plan_id"], plan["admin"], "applied",
              f"rev {plan['expected_rev']} -> {plan['new_rev']} 已在设备现场原子呈现")


def _do_activate(plan: sqlite3.Row) -> dict:
    ts = now_iso()
    try:
        res = device.activate(plan["plan_id"], plan["new_rev"], ts,
                              lose_response=_fault_lose_confirm)
    except DeviceTransportError as e:
        # 关键：不知道设备到底应用了没有 -> 标记 activating + 待处理原因，
        # 保留暂存，等待 reconcile，绝不盲目重发提交。
        db.conn().execute(
            "UPDATE plans SET state='activating', pending_reason=?, updated_at=? WHERE plan_id=?",
            (f"ACTIVATE 结果不确定（可能已应用）: {e}", ts, plan["plan_id"]))
        log_event(plan["plan_id"], "system", "activate-uncertain", str(e))
        raise
    if res.get("ok") != "1":
        db.conn().execute(
            "UPDATE plans SET state='failed', pending_reason=?, updated_at=? WHERE plan_id=?",
            (f"ACTIVATE 被设备拒绝: {res.get('err')}", ts, plan["plan_id"]))
        log_event(plan["plan_id"], "system", "activate-rejected", json.dumps(res, ensure_ascii=False))
        raise RuntimeError(f"activate rejected: {res.get('err')}")
    _mark_applied(plan, res)
    return res


_fault_lose_confirm = False  # 演示用故障注入开关（仅一次）


def do_publish(admin: str, draft_id: str, expected_rev: int,
               lose_confirm: bool = False) -> dict:
    global _fault_lose_confirm
    with PUBLISH_LOCK:
        c = db.conn()
        _fault_lose_confirm = lose_confirm
        draft = c.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if not draft:
            return {"applied": False, "error": "draft_not_found"}

        cfg = c.execute("SELECT * FROM config WHERE id=1").fetchone()

        # —— 版本条件 #1：草稿必须显式声明它基于哪个已发布 rev ——
        if draft["base_rev"] != cfg["rev"]:
            return {"applied": False, "error": "DRAFT_STALE",
                    "detail": {"draft_base_rev": draft["base_rev"],
                               "current_rev": cfg["rev"],
                               "hint": "草稿基于旧版本，另一位管理员可能已发布；请拉取最新版本后重建草稿，不会覆盖现场"}}

        # —— 版本条件 #2：发布 CAS ——
        if expected_rev != cfg["rev"]:
            return {"applied": False, "error": "REVISION_CONFLICT",
                    "detail": {"expected_rev": expected_rev, "current_rev": cfg["rev"]}}

        # 内容指纹：与现场完全一致 -> no-op（幂等，不造新版本）
        if (draft["title"] == cfg["title"] and draft["zoom"] == cfg["zoom"]
                and draft["darken"] == cfg["darken"]
                and draft["image_sha256"] == cfg["image_sha256"]):
            return {"applied": True, "noop": True, "rev": cfg["rev"],
                    "note": "草稿与现场一致，无需发布"}

        plan_id = "plan-" + uuid.uuid4().hex[:12]
        new_rev = cfg["rev"] + 1
        ts = now_iso()

        # 第一步：数据库登记发布方案（独立短事务；设备资源尚未预备）
        db.begin()
        try:
            c.execute("""INSERT INTO plans
                (plan_id,admin,expected_rev,new_rev,title,zoom,darken,
                 image_sha256,image_path,state,created_at,updated_at,apply_count)
                VALUES (?,?,?,?,?,?,?,?,?, 'prepared', ?, ?, 0)""",
                      (plan_id, admin, expected_rev, new_rev, draft["title"],
                       draft["zoom"], draft["darken"], draft["image_sha256"],
                       draft["image_path"], ts, ts))
            log_event(plan_id, admin, "plan-created",
                      f"登记发布方案，期望基线 rev={expected_rev}，目标 rev={new_rev}")
            db.commit()
        except Exception:
            db.rollback()
            raise

        # 第二步：预备资源 —— 设备端读盘（=下载）+ 解码 + 全部字段能力校验
        try:
            prep = device.prepare(plan_id, new_rev, draft["image_path"],
                                  draft["image_sha256"], draft["title"],
                                  draft["zoom"], draft["darken"])
        except DeviceTransportError as e:
            db.begin()
            c.execute("UPDATE plans SET state='failed', pending_reason=?, updated_at=? WHERE plan_id=?",
                      (f"PREPARE 传输失败: {e}", now_iso(), plan_id))
            log_event(plan_id, "system", "prepare-transport-failed", str(e))
            db.commit()
            return {"applied": False, "error": "DEVICE_UNREACHABLE", "plan_id": plan_id,
                    "detail": str(e)}

        ok = prep.get("ok") == "1"
        db.begin()
        try:
            if not ok:
                # 整版应用失败：保留旧版（设备端连暂存都不会接受），返回逐字段能力差异
                diffs = parse_diff(prep.get("changes", ""))
                c.execute("UPDATE plans SET state='rejected', device_diff=?, pending_reason=?, "
                          "updated_at=? WHERE plan_id=?",
                          (json.dumps(diffs, ensure_ascii=False),
                           "设备能力不满足或资源不可解码，整版未暂存，现场保持旧版",
                           now_iso(), plan_id))
                log_event(plan_id, "system", "prepared-rejected",
                          f"能力差异: {prep.get('changes','')}")
                db.commit()
                return {"applied": False, "error": "CAPABILITY_DIFF", "plan_id": plan_id,
                        "rev_unchanged": cfg["rev"],
                        "capability_diff": diffs}

            log_event(plan_id, admin, "prepared",
                      f"设备预备成功 {prep.get('w')}x{prep.get('h')}，等待原子提交")

            # 第三步：提交版本 —— ACTIVATE 是设备端锁内整版指针交换（原子呈现点）
            try:
                act = _do_activate(c.execute("SELECT * FROM plans WHERE plan_id=?",
                                             (plan_id,)).fetchone())
            except DeviceTransportError as e:
                db.commit()
                return {"applied": False, "confirmed": False,
                        "error": "CONFIRMATION_LOST", "plan_id": plan_id,
                        "current_rev": cfg["rev"],
                        "detail": ("ACTIVATE 已发出但结果不确定，设备可能已经呈现新版本；"
                                   f"原因: {e}。请用 plan_id 调用 reconcile 幂等核对，勿重复提交"),
                        "reconcile_hint": f"/api/plans/{plan_id}/reconcile"}
            db.commit()
        except Exception:
            db.rollback()
            raise

        return {"applied": True, "plan_id": plan_id,
                "rev": new_rev, "device": act}


# ---------------------------- HTTP 层 ----------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "BGConsole/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] " + (fmt % args) + "\n")

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                return self._serve_file("index.html", "text/html; charset=utf-8")
            if p == "/api/state":
                return self._state()
            if p.startswith("/api/plans/") and p.endswith("/events"):
                pid = p.split("/")[3]
                rows = db.conn().execute(
                    "SELECT * FROM plan_events WHERE plan_id=? ORDER BY id", (pid,)).fetchall()
                return self._json([dict(r) for r in rows])
            if p == "/api/plans":
                rows = db.conn().execute(
                    "SELECT * FROM plans ORDER BY created_at DESC LIMIT 20").fetchall()
                return self._json([dict(r) for r in rows])
            if p.startswith("/api/images/"):
                sha = p.rsplit("/", 1)[-1]
                row = db.conn().execute(
                    "SELECT image_path FROM config WHERE image_sha256=?", (sha,)).fetchone()
                if not row:
                    drow = db.conn().execute(
                        "SELECT image_path FROM drafts WHERE image_sha256=? LIMIT 1",
                        (sha,)).fetchone()
                    row = drow
                if not row:
                    base = os.path.join(IMGDIR, sha[:2])
                    for ext in ("png", "jpeg", "jpg", "bin"):
                        cand = os.path.join(base, sha + "." + ext)
                        if os.path.exists(cand):
                            row = type("R", (), {"image_path": cand})()
                            break
                if row and os.path.exists(row["image_path"]):
                    with open(row["image_path"], "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    ct = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else (
                        "image/jpeg" if data[:2] == b"\xff\xd8" else "application/octet-stream")
                    self.send_header("Content-Type", ct)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                return self._json({"error": "not_found"}, 404)
            if p == "/api/device/events":
                try:
                    evts = device.events()
                    return self._json({"events": evts})
                except DeviceTransportError as e:
                    return self._json({"error": str(e)}, 503)
            return self._json({"error": "not_found", "path": p}, 404)
        except Exception as e:
            return self._json({"error": "server_error", "detail": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p == "/api/images":
                return self._upload_image()
            if p == "/api/drafts":
                return self._save_draft()
            if p.startswith("/api/drafts/") and p.endswith("/preview/begin"):
                return self._preview(p.split("/")[3], cancel=False)
            if p.startswith("/api/drafts/") and p.endswith("/preview/cancel"):
                return self._preview(p.split("/")[3], cancel=True)
            if p.startswith("/api/plans/") and p.endswith("/reconcile"):
                pid = p.split("/")[3]
                return self._json(reconcile_plan(pid))
            if p == "/api/publish":
                payload = json.loads(self._body() or b"{}")
                res = do_publish(payload.get("admin", "anonymous"),
                                 payload["draft_id"], int(payload["expected_rev"]),
                                 lose_confirm=bool(payload.get("_lose_confirm")))
                status = 200
                if res.get("error") == "CAPABILITY_DIFF":
                    status = 422
                elif res.get("error") == "CONFIRMATION_LOST":
                    # 注意：这是"结果不确定"，不是 4xx 业务失败；客户端必须去核对
                    status = 504
                elif res.get("error") in ("DRAFT_STALE", "REVISION_CONFLICT"):
                    status = 409
                elif res.get("error") == "DEVICE_UNREACHABLE":
                    status = 503
                return self._json(res, status)
            return self._json({"error": "not_found", "path": p}, 404)
        except DeviceTransportError as e:
            return self._json({"error": "device_transport", "detail": str(e)}, 503)
        except Exception as e:
            return self._json({"error": "server_error", "detail": str(e)}, 500)

    # ---- handlers ----

    def _serve_file(self, name: str, ctype: str):
        path = os.path.join(WEB, name)
        if not os.path.exists(path):
            return self._json({"error": "ui missing"}, 404)
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _upload_image(self):
        q = parse_qs(urlparse(self.path).query)
        data = self._body()
        if not data:
            return self._json({"error": "empty body"}, 400)
        info = store_image(data)
        info["suggested_name"] = q.get("name", [""])[0]
        return self._json(info, 201)

    def _save_draft(self):
        payload = json.loads(self._body() or b"{}")
        admin = payload.get("admin", "anonymous")
        base_rev = int(payload.get("base_rev", -1))
        title = payload.get("title", "")
        zoom = int(payload.get("zoom", 0))
        darken = int(payload.get("darken", 0))
        image_sha = payload.get("image_sha256", "")
        # 版本条件 + 基本合法性
        cfg = db.conn().execute("SELECT * FROM config WHERE id=1").fetchone()
        if base_rev < 1:
            return self._json({"saved": False, "error": "BASE_REV_REQUIRED"}, 400)
        if base_rev > cfg["rev"]:
            return self._json({"saved": False, "error": "BASE_REV_IN_FUTURE",
                               "current_rev": cfg["rev"]}, 409)
        if not title or len(title) > 200:
            return self._json({"saved": False, "error": "TITLE_INVALID"}, 400)
        if not (0 <= zoom <= 1000 and 0 <= darken <= 100):
            return self._json({"saved": False, "error": "FIELD_RANGE_INVALID"}, 400)
        # 解析图片（允许"不可解码"内容先存草稿：标题等字段仍然有效）
        path = None
        if image_sha:
            d = os.path.join(IMGDIR, image_sha[:2])
            for ext in ("png", "jpeg", "jpg", "bin"):
                cand = os.path.join(d, image_sha + "." + ext)
                if os.path.exists(cand):
                    path = cand
                    break
        if not path:
            return self._json({"saved": False, "error": "IMAGE_NOT_UPLOADED"}, 400)
        with open(path, "rb") as f:
            data = f.read()
        dec, fmt, w, h = imglib.probe(data)
        actual_sha = imglib.sha256_bytes(data)
        draft_id = "draft-" + uuid.uuid4().hex[:12]
        ts = now_iso()
        db.begin()
        try:
            db.conn().execute("""INSERT INTO drafts
                (draft_id,admin,base_rev,title,zoom,darken,image_sha256,image_path,
                 image_decodable,image_w,image_h,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (draft_id, admin, base_rev, title, zoom, darken, actual_sha, path,
                 1 if dec else 0, w, h, ts))
            log_event(None, admin, "draft-saved",
                      f"草稿 {draft_id} 基于 rev={base_rev} 保存，图片可解码={dec}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        stale = base_rev < cfg["rev"]
        return self._json({"saved": True, "draft_id": draft_id, "base_rev": base_rev,
                           "current_rev": cfg["rev"], "stale_warning": stale,
                           "image_decodable": dec, "image_w": w, "image_h": h,
                           "image_sha256": actual_sha}, 201)

    def _preview(self, draft_id: str, cancel: bool):
        row = db.conn().execute("SELECT * FROM drafts WHERE draft_id=?",
                                (draft_id,)).fetchone()
        if not row:
            return self._json({"error": "draft_not_found"}, 404)
        body = json.loads(self._body() or b"{}")
        session = body.get("session") or f"sess-{uuid.uuid4().hex[:8]}"
        if cancel:
            res = device.preview_cancel(session)
            return self._json({"preview": "cancelled", "session": session, "device": res})
        delay = int(body.get("delay_ms", 0))
        res = device.preview_begin(session, row["image_path"], row["image_sha256"], delay)
        return self._json({"preview": res.get("preview"), "session": session,
                           "note": "仅当前编辑会话生效，不写入任何版本",
                           "device": res}, 200)

    def _state(self):
        c = db.conn()
        cfg = c.execute("SELECT * FROM config WHERE id=1").fetchone()
        pend = c.execute("SELECT * FROM plans WHERE state IN ('prepared','activating',"
                         "'interrupted') ORDER BY created_at DESC LIMIT 1").fetchone()
        drafts = [dict(r) for r in c.execute(
            "SELECT * FROM drafts ORDER BY updated_at DESC LIMIT 10").fetchall()]
        plans = [dict(r) for r in c.execute(
            "SELECT * FROM plans ORDER BY created_at DESC LIMIT 10").fetchall()]
        try:
            st = device.status()
            live = {"reachable": True, "live_w": int(st.get("live_w", 0)),
                    "live_h": int(st.get("live_h", 0)),
                    "live_zoom": int(st.get("live_zoom", 0)),
                    "live_darken": int(st.get("live_darken", 0)),
                    "live_title": st.get("live_title", ""),
                    "live_sha256": st.get("live_sha256", ""),
                    "preview_state": st.get("pstate"),
                    "preview_session": st.get("psession"),
                    "applied": st.get("applied", "")}
        except DeviceTransportError as e:
            live = {"reachable": False, "error": str(e)}
        return self._json({
            "desired": desired_payload(cfg, pend["state"] if pend else None,
                                       pend),       # 期望状态（数据库最新已确认版本）
            "drafts": drafts,                       # 工作副本
            "plans": plans,                         # 发布方案与待处理原因
            "actual": live})                        # 现场结果（来自 C 设备）


device: DeviceClient  # 启动时注入


def startup_sweep():
    """服务重启后：把残留在 prepared/activating 的方案挂为 interrupted（待人工核对）。
    注意不盲目重发 activate —— 设备可能已经应用了，只是当时确认丢失。"""
    c = db.conn()
    rows = c.execute("SELECT plan_id FROM plans WHERE state IN ('prepared','activating')").fetchall()
    for r in rows:
        c.execute("UPDATE plans SET state='interrupted', pending_reason=?, updated_at=? "
                  "WHERE plan_id=?",
                  ("服务重启中断，ACTIVATE 结果未知，需要 reconcile 核对设备现场",
                   now_iso(), r["plan_id"]))
        log_event(r["plan_id"], "system", "startup-sweep", "重启扫描：挂起待核对")



def _install_signal_handlers(httpd, dev):
    import signal
    def _quit(signum, frame):
        # 不能在信号处理器里调用 httpd.shutdown()（会死锁）；
        # dev.close() 只做管道写入 + 进程组回收，异步信号安全度足够。
        try:
            dev.close_nowait()
        finally:
            os._exit(0)
    signal.signal(signal.SIGTERM, _quit)
    signal.signal(signal.SIGINT, _quit)


def main():
    port = int(os.environ.get("PORT", "8080"))
    db.conn().executescript(SCHEMA)
    cfg = seed_config()
    global device
    device = DeviceClient(DEVICE_BIN, cfg["image_path"], cfg["image_sha256"],
                          cfg["title"], RUNTIME)
    startup_sweep()
    hello = device.hello()
    sys.stderr.write(f"[server] device hello: {hello}\n")
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    _install_signal_handlers(httpd, device)
    sys.stderr.write(f"[server] console on http://127.0.0.1:{port}\n")
    try:
        httpd.serve_forever()
    finally:
        device.close()


if __name__ == "__main__":
    main()
