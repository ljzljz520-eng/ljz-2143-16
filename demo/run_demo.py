#!/usr/bin/env python3
"""run_demo.py — 四个事务场景的端到端演示（对真实 HTTP API + C 设备）

A. 预览图下载慢于取消          —— 临时纹理必须撤销，现场不动
B. 两人基于旧版本保存          —— 后到者被版本条件拒绝，绝不覆盖已发布配置
C. 保存成功但确认响应丢失      —— 方案挂起 + plan_id 幂等核对补登记
D. 标题有效而图片不可解码      —— 逐字段能力差异，整版失败保留旧版
附加 E. 设备某字段不支持（zoom 超出设备能力）
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8080"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))

def req(method, path, body=None, raw=None, ctype="application/json"):
    data = None
    if raw is not None:
        data = raw
    elif body is not None:
        data = json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": ctype} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

def upload(path=None, data=None, name="img"):
    if data is None:
        with open(path, "rb") as f:
            data = f.read()
        name = os.path.basename(path)
    return req("POST", "/api/images?name=" + name, raw=data,
               ctype="application/octet-stream")[1]

def save_draft(admin, base_rev, title, zoom, darken, sha):
    return req("POST", "/api/drafts",
               {"admin": admin, "base_rev": base_rev, "title": title,
                "zoom": zoom, "darken": darken, "image_sha256": sha})

def state():
    return req("GET", "/api/state")[1]

def section(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)

# ---------------------------------------------------------------- A
section("场景 A：预览图下载慢于取消（临时纹理只属于当前编辑会话）")
img = upload(os.path.join(ROOT, "assets", "background.png"))
s = state(); rev0 = s["desired"]["rev"]; live0 = s["actual"]["live_sha256"]
st, d = save_draft("alice", rev0, "A-预览用草稿", 100, 0, img["sha256"])
draft_a = d["draft_id"]
# 设备端模拟 1.5 秒慢下载，立即取消
req("POST", f"/api/drafts/{draft_a}/preview/begin",
    {"session": "sess-A", "delay_ms": 1500})
print("  预览已请求（下载需 1.5s），0.1s 后立刻取消…")
time.sleep(0.1)
req("POST", f"/api/drafts/{draft_a}/preview/cancel", {"session": "sess-A"})
time.sleep(2.2)
_, st = req("GET", "/api/device/events")
evts = " ".join(st["events"])
print("  设备事件:\n   " + "\n   ".join(st["events"][-6:]))
check("慢于取消的下载完成后纹理被丢弃（generation 栅栏）",
      "arrived after cancel" in evts or "superseded" in evts)
s = state()
check("取消后现场图片 sha 未改变（临时纹理不污染正式版本）",
      s["actual"]["live_sha256"] == live0)
check("设备预览状态为 canceled", s["actual"]["preview_state"] == "canceled")

# ---------------------------------------------------------------- B
section("场景 B：两位管理员基于同一旧版本保存——后到者不得覆盖已发布配置")
st, db = save_draft("bob", rev0, "B-Bob 的新版本", 110, 10, img["sha256"])
bob_draft = db["draft_id"]
# alice 也基于 rev0 做了草稿（内容不同）
st, da = save_draft("alice", rev0, "B-Alice 基于旧版", 150, 30, img["sha256"])
alice_draft = da["draft_id"]
# bob 先发布成功
st, pb = req("POST", "/api/publish",
             {"admin": "bob", "draft_id": bob_draft, "expected_rev": rev0})
check("Bob 发布成功并得到设备确认", pb.get("applied") is True and pb.get("rev") == rev0 + 1,
      f"rev -> {pb.get('rev')}")
# alice 再基于旧 rev 发布 -> 必须拒绝
st, pa = req("POST", "/api/publish",
             {"admin": "alice", "draft_id": alice_draft, "expected_rev": rev0})
print(f"  Alice 发布响应 HTTP={st}: {pa.get('error')} {pa.get('detail','')}")
check("Alice 基于旧版本被拒（DRAFT_STALE / REVISION_CONFLICT）",
      st == 409 and pa["error"] in ("DRAFT_STALE", "REVISION_CONFLICT"))
s = state()
check("现场仍是 Bob 发布的标题，Alice 没有覆盖",
      s["desired"]["title"] == "B-Bob 的新版本" and s["actual"]["live_title"] == "B-Bob 的新版本")
check("期望 rev 恰好前进 1 次", s["desired"]["rev"] == rev0 + 1)
rev_after_b = s["desired"]["rev"]

# ---------------------------------------------------------------- C
section("场景 C：保存成功，但 ACTIVATE 确认响应丢失（结果未知）")
st, dc = save_draft("carol", rev_after_b, "C-Carol 确认丢失版", 130, 25, img["sha256"])
carol_draft = dc["draft_id"]
st, pc = req("POST", "/api/publish",
             {"admin": "carol", "draft_id": carol_draft,
              "expected_rev": rev_after_b, "_lose_confirm": True})
plan_c = pc.get("plan_id")
print(f"  HTTP={st} error={pc.get('error')} plan={plan_c}")
check("确认丢失返回 504 而非伪装成功", st == 504 and pc["error"] == "CONFIRMATION_LOST")
check("响应明确要求凭 plan_id 幂等核对，不重复提交",
      plan_c and str(pc.get("reconcile_hint", "")).endswith("/reconcile"))
s = state()
plan_row = next(p for p in s["plans"] if p["plan_id"] == plan_c)
check("方案挂起在 activating 并记录待处理原因",
      plan_row["state"] == "activating" and plan_row["pending_reason"])
print("  —— 网络恢复后，凭 plan_id 核对（不是重新发布）——")
st, rc = req("POST", f"/api/plans/{plan_c}/reconcile")
check("核对发现设备其实已应用，补登记成功（幂等）", rc.get("ok") is True,
      rc.get("note", ""))
s = state()
check("数据库期望状态前进到新版本，且与现场一致",
      s["desired"]["rev"] == rev_after_b + 1
      and s["actual"]["live_title"] == "C-Carol 确认丢失版"
      and s["desired"]["title"] == s["actual"]["live_title"])
# 再核对一次，必须幂等
st, rc2 = req("POST", f"/api/plans/{plan_c}/reconcile")
check("重复核对保持终态，不产生第二个版本", rc2.get("state") == "applied")
rev_cur = s["desired"]["rev"]

# ---------------------------------------------------------------- D
section("场景 D：标题有效，但图片不可解码——整版失败，旧版保留")
bad = upload(data=os.urandom(128), name="garbage_for_demo.bin")
check("坏图被标记 decodable=false（仍可登记工作副本）", bad["decodable"] is False)
st, dd = save_draft("dora", rev_cur, "D-完全合法的标题", 100, 0, bad["sha256"])
check("草稿保存成功：标题等字段有效，仅图片标记不可解码",
      dd["saved"] is True and dd["image_decodable"] is False)
live_before = state()["actual"]["live_sha256"]
st, pd = req("POST", "/api/publish",
             {"admin": "dora", "draft_id": dd["draft_id"], "expected_rev": rev_cur})
print(f"  HTTP={st} error={pd.get('error')}")
diffs = pd.get("capability_diff", [])
print("  设备返回逐字段能力差异:")
for x in diffs: print("   ", x)
check("发布返回 CAPABILITY_DIFF(422)", st == 422 and pd["error"] == "CAPABILITY_DIFF")
check("差异具体定位到 field=image / image_not_decodable",
      any([x.get("field") == "image" and "not_decodable" in x.get("code", "") for x in diffs]))
s = state()
check("整版应用失败，现场保留旧版（图片/标题/rev 全部不动）",
      s["actual"]["live_sha256"] == live_before
      and s["actual"]["live_title"] == "C-Carol 确认丢失版"
      and s["desired"]["rev"] == rev_cur)
plan_d = next(p for p in s["plans"] if p["state"] == "rejected")
check("数据库登记 rejected 方案与待处理原因", plan_d["pending_reason"])

# ---------------------------------------------------------------- E
section("场景 E（附加）：设备某字段不支持——zoom=300 超出设备能力 25..200")
st, de = save_draft("erin", rev_cur, "E-缩放超界", 300, 10, img["sha256"])
st, pe = req("POST", "/api/publish",
             {"admin": "erin", "draft_id": de["draft_id"], "expected_rev": rev_cur})
diffs = pe.get("capability_diff", [])
z = next((x for x in diffs if x.get("field") == "zoom"), None)
print("  zoom 差异:", z)
check("返回具体能力差异：UNSUPPORTED_RANGE 且给出支持区间",
      st == 422 and z and z["code"] == "UNSUPPORTED_RANGE"
      and z["supported"] == "25..200" and z["requested"] == "300")
s = state()
check("现场仍为旧版", s["desired"]["rev"] == rev_cur
      and s["actual"]["live_title"] == "C-Carol 确认丢失版")

# ---------------------------------------------------------------- 汇总
section("三栏对照（管理页同时展示）")
s = state()
print("  草稿(工作副本):")
for x in s["drafts"][:5]:
    print(f"    {x['draft_id']} by {x['admin']:6s} base_rev={x['base_rev']} "
          f"图={'ok' if x['image_decodable'] else 'BAD'} 标题={x['title']}")
print("  期望状态: rev=%s 标题=%s zoom=%s darken=%s" %
      (s["desired"]["rev"], s["desired"]["title"], s["desired"]["zoom"], s["desired"]["darken"]))
print("  现场结果: %s zoom=%s darken=%s" %
      (s["actual"]["live_title"], s["actual"]["live_zoom"], s["actual"]["live_darken"]))
match = (s["desired"]["title"] == s["actual"]["live_title"]
         and s["desired"]["zoom"] == s["actual"]["live_zoom"]
         and s["desired"]["darken"] == s["actual"]["live_darken"])
check("期望状态 == 现场结果（应用成功以现场为准，而非 HTTP 状态码）", match)

print("\n" + "#" * 78)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL: print("  FAIL:", f)
    sys.exit(1)
print("全部场景断言通过 ✅")
