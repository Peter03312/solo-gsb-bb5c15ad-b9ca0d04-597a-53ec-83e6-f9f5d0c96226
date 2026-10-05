#!/usr/bin/env python3
"""调用示例：仅依赖标准库，直接运行即可走一遍完整业务场景。

前置：服务已在本机启动（docker compose up --build 或 uvicorn app.main:app）。
用法：python examples/python_client.py [BASE_URL]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title: str, status: int, payload: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    number = "SOP-DEMO-001"

    # 1) 首次发布：全年适用的初版正文
    status, res = call("POST", "/api/v1/releases", {
        "number": number,
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    show("1. 首次发布", status, res)

    # 2) 更正：仅覆盖 3 月这一子区间；发布时间由服务端记录
    status, res = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期还须穿戴防割手套。",
    })
    show("2. 提交更正（子区间）", status, res)
    published_r1 = res["items"][0]["published_at"]

    # 3) 查询：2 月（更正区间之外）-> 仍是初版
    status, res = call("GET", "/api/v1/effective?work_time=2026-02-15T08:00:00Z")
    show("3. 作业时刻 2026-02-15（更正区间外）", status, res)

    # 4) 查询：3 月（更正区间之内）-> 更正正文
    status, res = call("GET", "/api/v1/effective?work_time=2026-03-15T08:00:00Z")
    show("4. 作业时刻 2026-03-15（更正区间内）", status, res)

    # 5) 回溯：知悉时刻取 2025 年（任何版本都尚未发布）-> 空结果
    status, res = call("GET",
                       "/api/v1/effective?work_time=2026-03-15T08:00:00Z"
                       "&known_as_of=2025-06-01T00:00:00Z")
    show("5. 知悉时刻早于发布 -> 空", status, res)

    # 6a) 顺序请求，却与"已发布"更正区间重叠 -> 409 过期基版，历史不变
    status, res = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 0,
        "valid_start": "2026-03-20T00:00:00Z",
        "valid_end": "2026-03-25T00:00:00Z",
        "body": "依据了旧版 r0 的已失效部分，必须被拒绝。",
    })
    show("6a. 过期基版（顺序重叠已发布更正，整体拒绝）", status, res)

    # 6b) 同一批次内两条对 r0 的并发更正相互重叠 -> 409，整批回滚
    status, res = call("POST", "/api/v1/corrections", {"items": [
        {"number": number, "base_revision": 0,
         "valid_start": "2026-05-01T00:00:00Z",
         "valid_end": "2026-05-20T00:00:00Z", "body": "并发更正 A"},
        {"number": number, "base_revision": 0,
         "valid_start": "2026-05-10T00:00:00Z",
         "valid_end": "2026-05-30T00:00:00Z", "body": "并发更正 B（与 A 冲突）"},
    ]})
    show("6b. 并发更正冲突（同批重叠，整批回滚）", status, res)

    # 6c) 试图回填发布时间：额外字段被拒绝（published_at 只由服务端记录）
    status, res = call("POST", "/api/v1/releases", {
        "number": number,
        "body": "重复编号且回填发布时间",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "published_at": "2000-01-01T00:00:00Z",
    })
    show("6c. 回填发布时间/重复编号被拒绝", status, res)

    # 7) 以 r1 为基版做"更正之更正"（链式）
    status, res = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 1,
        "valid_start": "2026-03-10T00:00:00Z",
        "valid_end": "2026-03-20T00:00:00Z",
        "body": "再更正：3 月 10-20 日改为全套防割装备。",
    })
    show("7. 链式更正 r1 -> r2", status, res)

    status, res = call("GET",
                       "/api/v1/procedures/" + number + "/history")
    show("8. 版本历史", status, res)

    # 9) 两知悉时刻作业区间对比（见文件下方 _diff_examples）
    _diff_examples(number, published_r1)

    # 10) 撤回误发布的更正（本轮新增；独立规程演示，见 _withdrawal_examples）
    _withdrawal_examples()

    # 11) 作业规程包的固定、取回与复核（独立规程演示，见 _package_examples）
    _package_examples()

    # 12) 前置规程闭包（本轮新增；独立规程演示，见 _closure_examples）
    _closure_examples()


def _closure_examples() -> None:
    """前置规程闭包：版本级前置引用 + 按根规程递归核对当时有效的前置清单。"""
    root, mid, base = "SOP-DEMO-CL-001", "SOP-DEMO-CL-002", "SOP-DEMO-CL-003"

    # 12.1) 前置可晚于引用者发布：先发布根规程并声明前置（此时前置尚不存在）
    status, res = call("POST", "/api/v1/releases", {
        "number": root,
        "body": "根规程：高处作业须按本规程执行。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": [mid, base],
    })
    show("12.1 首次发布携带前置规程列表（前置尚未发布也允许）", status, res)

    # 12.2) 此时闭包存在缺口：引用的前置编号不存在 -> 404，不返回部分清单
    status, res = call(
        "GET", f"/api/v1/procedures/{root}/closure"
               "?work_time=2026-03-15T08:00:00Z")
    show("12.2 前置未发布 -> 404 prerequisite_not_found（说明缺口）", status, res)

    # 12.3) 补齐前置规程（中间规程再引用基础规程，演示递归与去重）
    call("POST", "/api/v1/releases", {
        "number": base,
        "body": "基础规程：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    call("POST", "/api/v1/releases", {
        "number": mid,
        "body": "中间规程：动火作业须办理动火证。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": [base],
    })

    # 12.4) 闭包查询：前置优先、按编号去重、根在最后，含编号/版本/正文
    status, res = call(
        "GET", f"/api/v1/procedures/{root}/closure"
               "?work_time=2026-03-15T08:00:00Z")
    show("12.4 闭包查询（前置优先 + 去重，根在最后）", status, res)

    # 12.5) 更正省略前置列表 -> 继承被替代版本；显式空列表 -> 清空
    status, res = call("POST", "/api/v1/corrections", {
        "number": root,
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "根规程 3 月版：大风天停止高处作业。",
    })
    show("12.5a 更正省略前置 -> 继承被替代版本", status, res)
    status, res = call("POST", "/api/v1/corrections", {
        "number": root,
        "base_revision": 1,
        "valid_start": "2026-03-10T00:00:00Z",
        "valid_end": "2026-03-20T00:00:00Z",
        "body": "根规程 3 月中旬版：改按现场交底执行。",
        "prerequisites": [],
    })
    show("12.5b 显式空列表 -> 清空前置", status, res)

    # 12.6) 引用随版本适用区间变化：3 月中旬闭包只剩根规程自身
    status, res = call(
        "GET", f"/api/v1/procedures/{root}/closure"
               "?work_time=2026-03-15T08:00:00Z")
    show("12.6 3 月中旬（r2 生效、前置已清空）", status, res)

    # 12.7) 历史审计：各版本创建时的前置列表（r0/r1 继承、r2 清空）
    status, res = call("GET", f"/api/v1/procedures/{root}/history")
    show("12.7 版本历史（各版本的前置列表）", status,
         [{"revision": v["revision"], "prerequisites": v["prerequisites"]}
          for v in res["items"]])

    # 12.8) 各类拒绝：空白/重复编号、引用环
    status, res = call("POST", "/api/v1/releases", {
        "number": "SOP-DEMO-CL-BAD-1",
        "body": "x",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": ["  "],
    })
    show("12.8a 前置编号为空白 -> 422 invalid_number", status, res)
    status, res = call("POST", "/api/v1/releases", {
        "number": "SOP-DEMO-CL-BAD-2",
        "body": "x",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-DEMO-CL-003", "SOP-DEMO-CL-003"],
    })
    show("12.8b 前置编号重复 -> 422 duplicate_prerequisite_number", status, res)
    call("POST", "/api/v1/releases", {
        "number": "SOP-DEMO-CL-CYC-A", "body": "A",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-DEMO-CL-CYC-B"]})
    call("POST", "/api/v1/releases", {
        "number": "SOP-DEMO-CL-CYC-B", "body": "B",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-DEMO-CL-CYC-A"]})
    status, res = call(
        "GET", "/api/v1/procedures/SOP-DEMO-CL-CYC-A/closure"
               "?work_time=2026-03-15T08:00:00Z")
    show("12.8c 引用环 -> 409 prerequisite_cycle（返回环路径）", status, res)


def _withdrawal_examples() -> None:
    """撤回示例：撤回只追加事件，撤回前历史不变，撤回后区间恢复父版本正文。"""
    number = "SOP-DEMO-WD-001"

    # 10.1) 发布首版 + 一条覆盖 3 月的更正
    call("POST", "/api/v1/releases", {
        "number": number,
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    published = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "误发布的更正正文。",
    })[1]["items"][0]["published_at"]

    # 10.2) 撤回 r1：只提交编号与更正版本号；withdrawn_at 由服务端记录
    status, res = call("POST", "/api/v1/withdrawals",
                       {"number": number, "revision": 1})
    show("10.2 撤回误发布更正 r1（withdrawn_at 服务端记录）", status, res)
    withdrawn_at = res["items"][0]["withdrawn_at"]

    # 10.3) 撤回后回溯：3 月区间恢复父版本 r0 正文
    status, res = call("GET",
                       "/api/v1/effective?work_time=2026-03-15T08:00:00Z")
    show("10.3 撤回后回溯（3 月恢复初版）", status,
         [i for i in res["items"] if i["number"] == number])

    # 10.4) 撤回前知悉时刻的回溯仍按原版本链 -> 仍是 r1（历史不变）
    status, res = call(
        "GET",
        "/api/v1/effective?work_time=2026-03-15T08:00:00Z"
        f"&known_as_of={published}")
    show("10.4 以撤回前知悉时刻回溯（仍看到被撤回的 r1）", status,
         [i for i in res["items"] if i["number"] == number])

    # 10.5) 历史审计：r1 行仍在，且带 withdrawn_at
    status, res = call("GET", f"/api/v1/procedures/{number}/history")
    show("10.5 版本历史（r1 仍在并带 withdrawn_at）", status, res)

    # 10.6) 撤回后父版本可在恢复的区间再次更正
    status, res = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "重新发布的正确更正正文。",
    })
    show("10.6 在恢复区间以 r0 为基版再次更正（r2）", status, res)

    # 10.7) 各类拒绝：重复撤回 / 撤首版 / 版本不存在 / 回填撤回时刻
    status, res = call("POST", "/api/v1/withdrawals",
                       {"number": number, "revision": 1})
    show("10.7a 重复撤回 -> 409 already_withdrawn", status, res)
    status, res = call("POST", "/api/v1/withdrawals",
                       {"number": number, "revision": 0})
    show("10.7b 撤回首版 -> 409 first_revision_not_withdrawable", status, res)
    status, res = call("POST", "/api/v1/withdrawals",
                       {"number": number, "revision": 99})
    show("10.7c 版本号不存在 -> 404 revision_not_found", status, res)
    status, res = call("POST", "/api/v1/withdrawals",
                       {"number": number, "revision": 2,
                        "withdrawn_at": "2000-01-01T00:00:00Z"})
    show("10.7d 回填撤回时刻 -> 422 validation_error", status, res)

    # 10.8) 两知悉时刻对比：撤回前一刻 vs 撤回后一刻，3 月段 r1 -> r0
    import urllib.parse
    from datetime import datetime, timedelta, timezone

    def us_iso(canonical: str, delta_microseconds: int) -> str:
        dt = datetime.strptime(canonical, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        return (dt + timedelta(microseconds=delta_microseconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")

    q = urllib.parse.urlencode({
        "known_before": published,                      # 撤回前：r1 可见
        "known_after": us_iso(withdrawn_at, 1),         # 撤回后：恢复 r0
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{number}/diff?{q}")
    show("10.8 撤回前后两知悉时刻对比（仅 3 月段 r1→r0）", status, res)


def _package_examples() -> None:
    """作业规程包：开工前固定、原样取回、之后更正/撤回下的复核。"""
    import time
    from datetime import datetime, timezone

    def utc_now_iso() -> str:
        # 停顿 1 秒，保证知悉时刻严格晚于紧接着的发布时刻（服务端记录）
        time.sleep(1)
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    n1, n2 = "SOP-DEMO-PKG-001", "SOP-DEMO-PKG-002"
    call("POST", "/api/v1/releases", {
        "number": n1,
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    call("POST", "/api/v1/releases", {
        "number": n2,
        "body": "另一规程初版：动火作业须办理动火证。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })

    # 11.1) 固定作业规程包：作业时刻 + 知悉时刻 + 编号列表
    known_fix = utc_now_iso()
    status, res = call("POST", "/api/v1/packages", {
        "work_time": "2026-03-15T08:00:00Z",
        "known_as_of": known_fix,
        "numbers": [n1, n2],
    })
    show("11.1 固定作业规程包（201，返回标识/版本/正文/两种时刻）", status, res)
    package_id = res["id"]
    fixed_revision = res["items"][0]["revision"]

    # 11.2) 按标识原样取回
    status, res = call("GET", f"/api/v1/packages/{package_id}")
    show("11.2 按标识取回（固定时原样）", status, res)

    # 11.3) 固定之后对 n1 发布 3 月更正
    status, res = call("POST", "/api/v1/corrections", {
        "number": n1,
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期还须穿戴防割手套。",
    })
    show("11.3 固定后再更正 n1", status, res)

    # 11.4) 再次取回：包内容保持固定时原样（仍是初版 r0）
    status, res = call("GET", f"/api/v1/packages/{package_id}")
    show("11.4 取回包（后续更正不影响包快照）", status, res)
    assert res["items"][0]["revision"] == fixed_revision

    # 11.5) 复核：以当前时刻为复核知悉时刻 -> n1 changed，整体 all_match=false
    status, res = call(
        "GET", f"/api/v1/packages/{package_id}/review"
               f"?known_as_of={utc_now_iso()}")
    show("11.5 复核（n1 包内 r0、彼时 r1，整体不再一致）", status, res)

    # 11.6) 以包内知悉时刻本身复核（含等号，不早于即可）-> 完全一致
    status, res = call(
        "GET", f"/api/v1/packages/{package_id}/review"
               f"?known_as_of={known_fix}")
    show("11.6 以包内知悉时刻复核（all_match=true）", status, res)

    # 11.7) 各类拒绝
    status, res = call("POST", "/api/v1/packages", {
        "work_time": "2026-03-15T08:00:00Z",
        "known_as_of": known_fix,
        "numbers": [n1, n1]})
    show("11.7a 编号重复 -> 422 duplicate_package_number", status, res)
    status, res = call("POST", "/api/v1/packages", {
        "work_time": "2026-03-15T08:00:00Z",
        "known_as_of": known_fix,
        "numbers": [n1, "NO-SUCH"]})
    show("11.7b 编号缺失 -> 404 procedure_not_found", status, res)
    status, res = call("POST", "/api/v1/packages", {
        "work_time": "2027-06-01T08:00:00Z",
        "known_as_of": known_fix,
        "numbers": [n1]})
    show("11.7c 作业时刻无适用正文 -> 409 no_applicable_version", status, res)
    status, res = call("POST", "/api/v1/packages", {
        "work_time": "2026-03-15T08:00:00Z",
        "known_as_of": "2030-01-01T00:00:00Z",
        "numbers": [n1]})
    show("11.7d 知悉时刻在未来 -> 422 known_as_of_in_future", status, res)
    status, res = call(
        "GET", "/api/v1/packages/deadbeefdeadbeefdeadbeefdeadbeef"
               "/review?known_as_of=" + known_fix)
    show("11.7e 包不存在 -> 404 package_not_found", status, res)
    status, res = call(
        "GET", f"/api/v1/packages/{package_id}/review"
               "?known_as_of=2025-01-01T00:00:00Z")
    show("11.7f 复核知悉时刻早于包内知悉时刻 -> 422 review_known_too_early",
         status, res)


def _diff_examples(number: str, published_r1: str) -> None:
    """对比接口示例：known_before / known_after 卡在 r1 发布时刻前后一微秒。"""
    import urllib.parse
    from datetime import datetime, timedelta, timezone

    def us_iso(canonical: str, delta_microseconds: int) -> str:
        dt = datetime.strptime(canonical, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        return (dt + timedelta(microseconds=delta_microseconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")

    # 9a) 全年作业区间：只有 3 月这一段在两侧分别是 r0 / r1；
    #     两侧同为 r0 的相邻部分不返回
    q = urllib.parse.urlencode({
        "known_before": us_iso(published_r1, -1),
        "known_after": us_iso(published_r1, 1),
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{number}/diff?{q}")
    show("9a. 两知悉时刻对比（全年区间，仅 3 月段 r0→r1）", status, res)

    # 9b) 前侧取很早以前（一切版本均未发布）：before=null 逐段列出。
    #     作业区间只问 2-5 月，其中 r1 还被第 7 步的 r2 切成 r1/r2/r1 三段
    q = urllib.parse.urlencode({
        "known_before": "2025-01-01T00:00:00Z",
        "known_after": "2030-01-01T00:00:00Z",
        "work_start": "2026-02-01T00:00:00Z",
        "work_end": "2026-05-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{number}/diff?{q}")
    show("9b. 前侧无任何可见版本（before 为 null 也逐段列出）", status, res)

    # 9c) 知悉顺序颠倒 -> 422 invalid_known_order，不写历史
    q = urllib.parse.urlencode({
        "known_before": "2030-01-01T00:00:00Z",
        "known_after": "2025-01-01T00:00:00Z",
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{number}/diff?{q}")
    show("9c. 知悉顺序颠倒被拒绝", status, res)


if __name__ == "__main__":
    main()
