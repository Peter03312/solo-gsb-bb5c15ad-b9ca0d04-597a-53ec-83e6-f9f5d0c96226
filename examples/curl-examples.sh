#!/bin/sh
# 调用示例（curl）。前置：服务已在本机启动。
# 约定时间均为 UTC、ISO-8601；区间语义为 [起, 止)。
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"

# --- 1. 首次发布 ---------------------------------------------------------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo

# --- 2. 更正（只覆盖 3 月子区间；published_at 由服务端记录，不可回填） ---
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期还须穿戴防割手套。"
      }'; echo

# --- 3. 回溯查询：作业时刻在 2 月（更正区间外） -> 初版 -----------------
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-02-15T08:00:00Z"; echo

# --- 4. 回溯查询：作业时刻在 3 月（更正区间内） -> 更正正文 -------------
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-03-15T08:00:00Z"; echo

# --- 5. 知悉时刻早于任何发布 -> 空结果 ----------------------------------
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-03-15T08:00:00Z&known_as_of=2025-06-01T00:00:00Z"; echo

# --- 6a. 顺序请求却与"已发布"更正重叠 -> 409 过期基版，历史不变 ----------
curl -sS -i -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "base_revision": 0,
        "valid_start": "2026-03-20T00:00:00Z",
        "valid_end":   "2026-03-25T00:00:00Z",
        "body": "依据了旧版 r0 的已失效部分，应被拒绝"
      }'; echo

# --- 6b. 同一批次内并发更正相互冲突 -> 409，整批回滚 --------------------
curl -sS -i -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{ "items": [
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-05-01T00:00:00Z", "valid_end": "2026-05-20T00:00:00Z",
         "body": "并发更正 A"},
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-05-10T00:00:00Z", "valid_end": "2026-05-30T00:00:00Z",
         "body": "并发更正 B（与 A 冲突，整批拒绝）"}
      ]}'; echo

# --- 6c. 回填发布时间：published_at 为额外字段，422 拒绝 -----------------
curl -sS -i -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "body": "重复编号且回填发布时间",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "published_at": "2000-01-01T00:00:00Z"
      }'; echo

# --- 7. 批量更正：整批原子提交（任一条非法则全部不入库） -----------------
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{ "items": [
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-05-01T00:00:00Z", "valid_end": "2026-06-01T00:00:00Z",
         "body": "5 月更正"},
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-06-01T00:00:00Z", "valid_end": "2026-07-01T00:00:00Z",
         "body": "6 月更正（与上一条首尾相接，允许）"}
      ]}'; echo

# --- 8. 版本历史（审计视图） --------------------------------------------
curl -sS "$BASE_URL/api/v1/procedures/SOP-CURL-001/history"; echo

# --- 9a. 两知悉时刻对比：前侧取发布之前，作业区间覆盖全年 ---------------
#       只有更正子区间（3 月）两侧版本不同；before 为 null（当时无任何版本）
curl -sS --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2025-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2030-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'; echo

# --- 9b. 收窄作业区间到 2-5 月：null→r0 与 null→r1 两段 -----------------
curl -sS --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2025-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2030-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-02-01T00:00:00Z' \
  --data-urlencode 'work_end=2026-05-01T00:00:00Z'; echo

# --- 9c. 知悉顺序颠倒 -> 422 invalid_known_order（不写历史） -------------
curl -sS -i --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2030-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2025-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'; echo

# --- 9d. 空作业区间（起=止） -> 422 invalid_interval ---------------------
curl -sS -i --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2025-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2030-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-03-01T00:00:00Z' \
  --data-urlencode 'work_end=2026-03-01T00:00:00Z'; echo

# ------------------------------------------------------------------------
# 撤回（本轮新增）：值班员发现更正误发布时，提交"规程编号 + 更正版本号"。
# 撤回时刻 withdrawn_at 由服务端记录、不接受回填；撤回只追加事件、不删版本。
# 前置数据：r1 为上面第 2 步发布的 3 月更正。
# ------------------------------------------------------------------------

# --- 10. 撤回误发布的更正 r1 --------------------------------------------
# 成功 201，返回服务端记录的 withdrawn_at（与发布时间一样不可回填）
curl -sS -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CURL-001", "revision": 1 }'; echo

# --- 11a. 撤回后回溯：3 月子区间恢复父版本 r0 正文 -----------------------
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-03-15T08:00:00Z"; echo

# --- 11b. 撤回前知悉时刻的回溯仍按原版本链 -> 历史不变 -------------------
# 知悉时刻取早于本轮服务运行的固定时刻：本脚本第 2 步的更正尚未发布，
# 故 3 月返回父版本 r0（演示"撤回不改变撤回前查询"请把该参数改为
# r1 发布之后、撤回之前的时刻，可从第 2/10 步响应中读取）。
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-03-15T08:00:00Z&known_as_of=2026-10-01T00:00:00Z"; echo

# --- 11c. 历史审计：r1 行仍在，且带 withdrawn_at -------------------------
curl -sS "$BASE_URL/api/v1/procedures/SOP-CURL-001/history"; echo

# --- 12. 撤回后父版本可在恢复区间再次更正（新的 r2 指向 r0） -------------
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "更正正文（撤回误发版本后重新发布）：仍须穿戴防割手套。"
      }'; echo

# --- 13a. 重复撤回已撤回版本 -> 409 already_withdrawn（历史不变） --------
curl -sS -i -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CURL-001", "revision": 1 }'; echo

# --- 13b. 撤回首版 -> 409 first_revision_not_withdrawable ---------------
curl -sS -i -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CURL-001", "revision": 0 }'; echo

# --- 13c. 撤回不存在的版本号 -> 404 revision_not_found -------------------
curl -sS -i -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CURL-001", "revision": 99 }'; echo

# --- 13d. 回填撤回时刻：withdrawn_at 为额外字段 -> 422 validation_error ---
curl -sS -i -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CURL-001", "revision": 2,
        "withdrawn_at": "2000-01-01T00:00:00Z" }'; echo

# --- 14. 批量撤回：整批原子，任一条非法全部不追加 -------------------------
# 此处第二条撤首版非法，第一条（合法）也一并回滚
curl -sS -i -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "items": [
        { "number": "SOP-CURL-001", "revision": 2 },
        { "number": "SOP-CURL-001", "revision": 0 }
      ]}'; echo

# ------------------------------------------------------------------------
# 作业规程包（本轮新增）：班组开工前按"作业时刻 + 知悉时刻 + 一组规程编号"
# 固定。包一经形成不可变：之后的更正/撤回不影响按标识取回；复核以不早于
# 包内知悉时刻、不晚于当前时刻的复核知悉时刻逐项核对是否变化。
# 知悉时刻不得晚于服务端当前时刻，故此处取运行时 UTC（先停顿 1 秒，
# 保证晚于上面的发布时刻）。
# ------------------------------------------------------------------------
sleep 1
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# --- 15. 为规程包准备两个规程 --------------------------------------------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PKG-001",
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PKG-002",
        "body": "另一规程初版：动火作业须办理动火证。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo

# --- 16. 固定作业规程包：201 返回包标识、各规程当时版本/正文、两种时刻 ----
PKG=$(curl -sS -X POST "$BASE_URL/api/v1/packages" \
  -H 'Content-Type: application/json' \
  -d "{
        \"work_time\": \"2026-03-15T08:00:00Z\",
        \"known_as_of\": \"$NOW\",
        \"numbers\": [\"SOP-PKG-001\", \"SOP-PKG-002\"]
      }")
echo "$PKG"
PKG_ID=$(printf '%s' "$PKG" | sed -n 's/.*"id": "\([0-9a-f]\{32\}\)".*/\1/p')
echo "固定得到包标识：$PKG_ID"

# --- 17. 按标识取回：永远是固定时原样快照 --------------------------------
curl -sS "$BASE_URL/api/v1/packages/$PKG_ID"; echo

# --- 18. 固定后再更正 SOP-PKG-001 的 3 月子区间 ---------------------------
sleep 1
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PKG-001",
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期还须穿戴防割手套。"
      }'; echo

# --- 19a. 取回包：内容保持固定时原样（仍是 r0 初版，不随后续更正变化） ----
curl -sS "$BASE_URL/api/v1/packages/$PKG_ID"; echo

# --- 19b. 复核（以当前时刻为复核知悉时刻）：逐项给出包内版本 pinned、
#         彼时有效版本 current 与 changed，并给整体 all_match=false --------
sleep 1
REVIEW_NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
curl -sS "$BASE_URL/api/v1/packages/$PKG_ID/review?known_as_of=$REVIEW_NOW"; echo

# --- 20a. 编号重复 -> 422 duplicate_package_number，整体拒绝不留包 --------
curl -sS -i -X POST "$BASE_URL/api/v1/packages" \
  -H 'Content-Type: application/json' \
  -d "{
        \"work_time\": \"2026-03-15T08:00:00Z\",
        \"known_as_of\": \"$NOW\",
        \"numbers\": [\"SOP-PKG-001\", \"SOP-PKG-001\"]
      }"; echo

# --- 20b. 编号缺失 -> 404 procedure_not_found -----------------------------
curl -sS -i -X POST "$BASE_URL/api/v1/packages" \
  -H 'Content-Type: application/json' \
  -d "{
        \"work_time\": \"2026-03-15T08:00:00Z\",
        \"known_as_of\": \"$NOW\",
        \"numbers\": [\"SOP-PKG-001\", \"NO-SUCH\"]
      }"; echo

# --- 20c. 作业时刻无适用正文 -> 409 no_applicable_version（不留包） -------
curl -sS -i -X POST "$BASE_URL/api/v1/packages" \
  -H 'Content-Type: application/json' \
  -d "{
        \"work_time\": \"2027-06-01T00:00:00Z\",
        \"known_as_of\": \"$NOW\",
        \"numbers\": [\"SOP-PKG-001\"]
      }"; echo

# --- 20d. 知悉时刻晚于服务端当前时刻 -> 422 known_as_of_in_future --------
curl -sS -i -X POST "$BASE_URL/api/v1/packages" \
  -H 'Content-Type: application/json' \
  -d '{
        "work_time": "2026-03-15T08:00:00Z",
        "known_as_of": "2030-01-01T00:00:00Z",
        "numbers": ["SOP-PKG-001"]
      }'; echo

# --- 20e. 复核知悉时刻早于包内知悉时刻 -> 422 review_known_too_early ------
curl -sS -i "$BASE_URL/api/v1/packages/$PKG_ID/review?known_as_of=2025-01-01T00:00:00Z"; echo

# --- 20f. 取回/复核不存在的包 -> 404 package_not_found --------------------
curl -sS -i "$BASE_URL/api/v1/packages/0123456789abcdef0123456789abcdef"; echo
curl -sS -i "$BASE_URL/api/v1/packages/0123456789abcdef0123456789abcdef/review?known_as_of=$NOW"; echo

# ------------------------------------------------------------------------
# 前置规程闭包（本轮新增）：首次发布/更正可携带前置规程编号列表
# （发布省略视为空；更正省略继承被替代版本；显式空列表清空；编号可尚未
# 发布，但不得含空白或重复）。闭包查询按根编号 + 作业时刻 + 知悉时刻在
# 同一读取快照中递归解析，返回去重后的根及前置规程清单（前置优先）。
# ------------------------------------------------------------------------

# --- 21. 先发布根规程并声明前置（前置可晚于引用者发布） -------------------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CL-001",
        "body": "根规程：高处作业须按本规程执行。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-CL-002", "SOP-CL-003"]
      }'; echo

# --- 22. 前置尚未发布 -> 404 prerequisite_not_found（说明缺口，整体失败） -
curl -sS -i "$BASE_URL/api/v1/procedures/SOP-CL-001/closure?work_time=2026-03-15T08:00:00Z"; echo

# --- 23. 补齐前置规程（中间规程再引用基础规程，演示递归与去重） -----------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CL-003",
        "body": "基础规程：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CL-002",
        "body": "中间规程：动火作业须办理动火证。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-CL-003"]
      }'; echo

# --- 24. 闭包查询：前置优先、按编号去重、根在最后，含编号/版本/正文 -------
curl -sS "$BASE_URL/api/v1/procedures/SOP-CL-001/closure?work_time=2026-03-15T08:00:00Z"; echo

# --- 25a. 更正省略前置列表 -> 继承被替代版本 ------------------------------
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CL-001",
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "根规程 3 月版：大风天停止高处作业。"
      }'; echo

# --- 25b. 显式空列表 -> 清空前置 ------------------------------------------
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CL-001",
        "base_revision": 1,
        "valid_start": "2026-03-10T00:00:00Z",
        "valid_end":   "2026-03-20T00:00:00Z",
        "body": "根规程 3 月中旬版：改按现场交底执行。",
        "prerequisites": []
      }'; echo

# --- 26. 引用随版本适用区间变化：3 月中旬闭包只剩根规程自身 ---------------
curl -sS "$BASE_URL/api/v1/procedures/SOP-CL-001/closure?work_time=2026-03-15T08:00:00Z"; echo

# --- 27. 历史审计：各版本创建时的前置列表（r0/r1 继承、r2 清空） ----------
curl -sS "$BASE_URL/api/v1/procedures/SOP-CL-001/history"; echo

# --- 28a. 前置编号为空白 -> 422 invalid_number（整批拒绝） ----------------
curl -sS -i -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CL-BAD-1", "body": "x",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["  "]
      }'; echo

# --- 28b. 前置编号重复 -> 422 duplicate_prerequisite_number ---------------
curl -sS -i -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CL-BAD-2", "body": "x",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-CL-003", "SOP-CL-003"]
      }'; echo

# --- 28c. 引用环 -> 409 prerequisite_cycle（返回环路径，不返回部分清单） --
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CL-CYC-A", "body": "A",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-CL-CYC-B"] }'; echo
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CL-CYC-B", "body": "B",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-CL-CYC-A"] }'; echo
curl -sS -i "$BASE_URL/api/v1/procedures/SOP-CL-CYC-A/closure?work_time=2026-03-15T08:00:00Z"; echo
