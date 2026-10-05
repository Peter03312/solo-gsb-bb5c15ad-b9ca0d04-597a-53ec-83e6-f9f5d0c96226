"""领域服务：首次发布、更正提交、撤回、双时间维回溯查询。

双时间维语义
------------
一条 version 行同时携带两个互不相同的时间维：

* 适用时间（valid time）：``[valid_start, valid_end)``，正文在作业现场的生效区间；
* 发布时间（transaction time）：``published_at``，服务端落库时刻，不可回填。

更正只新增行（子版本），不修改任何旧行，因此"更正发布前的查询结果"天然保持原样：
查询时以知悉时刻 ``known_as_of`` 过滤 ``published_at <= known_as_of`` 即可。

撤回
----
撤回也是**只追加事件**（``withdrawals`` 表），从不删除或改写 versions 行：
撤回时刻 ``withdrawn_at`` 由服务端记录、不接受回填。某版本在知悉时刻 k 下
可见（当时仍生效）当且仅当 ``published_at <= k`` 且
（未撤回 或 ``withdrawn_at >= k``）——两侧边界均含等号，与发布时刻
``published_at <= k`` 的约定对称。因此：

* 撤回前知悉时刻的回溯与两知悉时刻对比仍按原版本链还原（历史不变）；
* 撤回后知悉时刻下该更正不再可见，其适用子区间恢复显示父版本正文；
* 仅"当前仍生效、尚无子更正"的叶子更正可撤回：首版、已撤回版本、
  已有子更正的版本一律拒绝（撤回不删行，子更正即使已撤回也仍在历史中）；
  撤回叶子更正后，其父版本可在恢复的区间内再次更正。

版本树
------
- 首版：``revision=0``、``supersedes_id IS NULL``；
- 更正：指向被替代版本 ``supersedes_id``，编号为该规程下递增的 revision；
- 父版本的"仍生效区间" = 自身区间 − 其全部**当前仍生效**（未撤回）子更正区间；
- 查询时在所有可见候选中沿 supersedes 链取"最深"版本，即为作业时刻有效正文。

作业规程包
----------
班组开工前按（作业时刻、知悉时刻、一组规程编号）固定一个不可变快照：

- 固定：在单条写事务的一致快照上为每个编号解析当时适用版本，把正文连同
  版本号/区间/父版本/发布时刻整包复制进 ``packages``/``package_items``；
  编号重复、编号缺失、任一规程无适用正文、知悉时刻晚于当前时刻均整体拒绝、
  不留包。
- 取回：只返回固定时的快照行，之后的更正/撤回永不改变它。
- 复核：在显式只读快照事务内以复核知悉时刻（不早于包内知悉时刻、不晚于
  当前时刻）按原作业时刻逐项重新解析，给出包内版本 pinned、彼时有效版本
  current 与 changed，并汇总整体 all_match；是否变化只看版本号。
"""
from __future__ import annotations

import sqlite3
import uuid

from . import errors
from .db import read_conn, read_snapshot, write_tx
from .errors import DomainError
from .time_utils import TimeParseError, now_canonical, parse_canonical
# ---------------------------------------------------------------- 服务端时钟

# 写操作由全局锁串行化；时钟同样保证单调递增，避免连续两次发布落在同一微秒
# 而破坏 published_at 的先后可分辨性（客户端禁止回填，只允许使用该时钟）。
_last_published_at = ""


def _server_now() -> str:
    global _last_published_at
    ts = now_canonical()
    if ts <= _last_published_at:
        # 极端情况下同一微秒内连续发布：向前推进 1 微秒
        from datetime import datetime, timedelta
        dt = datetime.strptime(_last_published_at,
                               "%Y-%m-%dT%H:%M:%S.%fZ") + timedelta(microseconds=1)
        ts = dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _last_published_at = ts
    return ts

# ---------------------------------------------------------------- 区间工具


def _subtract_intervals(base: tuple[str, str],
                        holes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``[base)`` 减去 holes 的并集，返回若干不相交区间（按起点排序）。"""
    pieces = [base]
    for hs, he in sorted(holes):
        next_pieces: list[tuple[str, str]] = []
        for ps, pe in pieces:
            if he <= ps or hs >= pe:
                next_pieces.append((ps, pe))          # 不相交
            else:
                if hs > ps:
                    next_pieces.append((ps, hs))      # 左侧残余
                if he < pe:
                    next_pieces.append((he, pe))      # 右侧残余
        pieces = next_pieces
    return pieces


def _covered_by(interval: tuple[str, str],
                pieces: list[tuple[str, str]]) -> bool:
    s, e = interval
    for ps, pe in pieces:
        if ps <= s and e <= pe:
            return True
    return False


def _first_overlap(interval: tuple[str, str],
                   others: list[tuple[str, str]]) -> tuple[str, str] | None:
    s, e = interval
    for os_, oe in others:
        if s < oe and os_ < e:
            return os_, oe
    return None


def _validated_interval(start_raw: str, end_raw: str,
                        index: int | None) -> tuple[str, str]:
    """规范化区间并校验 [start, end) 非空；时间不合法抛 invalid_interval。"""
    try:
        start = parse_canonical(start_raw, "valid_start")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "valid_start", **({"index": index} if index is not None else {})})
    try:
        end = parse_canonical(end_raw, "valid_end")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "valid_end", **({"index": index} if index is not None else {})})
    if not start < end:
        raise errors.invalid_interval(
            start, end, "适用区间非法：起点必须严格早于终点（区间为 [起, 止) 且长度大于 0）",
            index)
    return start, end


def _clean_text(value: str, field: str, index: int | None) -> str:
    text = value.strip()
    if not text:
        details = {"field": field}
        if index is not None:
            details["index"] = index
        raise DomainError("invalid_body", f"{field} 不能为空字符串", 422, details)
    return text


def _clean_number(value: str, index: int | None) -> str:
    number = value.strip()
    if not number:
        details: dict = {"field": "number"}
        if index is not None:
            details["index"] = index
        raise DomainError("invalid_number", "规程编号不能为空字符串", 422, details)
    return number

# ---------------------------------------------------------------- 行读取


def _fetch_procedure(conn: sqlite3.Connection,
                     number: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM procedures WHERE number = ?", (number,)).fetchone()


def _fetch_procedure_by_id(conn: sqlite3.Connection,
                           proc_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM procedures WHERE id = ?", (proc_id,)).fetchone()


def _fetch_version(conn: sqlite3.Connection, proc_id: int,
                   revision: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM versions WHERE procedure_id = ? AND revision = ?",
        (proc_id, revision)).fetchone()


def _children(conn: sqlite3.Connection,
              version_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM versions WHERE supersedes_id = ? ORDER BY valid_start",
        (version_id,)).fetchall()


def _withdrawn_map(conn: sqlite3.Connection,
                   version_ids: list[int] | None = None) -> dict[int, str]:
    """version_id -> withdrawn_at（仅含已撤回版本）。

    不传 version_ids 时取全表（单规程场景版本量很小）。
    """
    if version_ids is not None:
        if not version_ids:
            return {}
        placeholders = ",".join("?" * len(version_ids))
        rows = conn.execute(
            f"SELECT version_id, withdrawn_at FROM withdrawals "
            f"WHERE version_id IN ({placeholders})",
            tuple(version_ids)).fetchall()
    else:
        rows = conn.execute(
            "SELECT version_id, withdrawn_at FROM withdrawals").fetchall()
    return {r["version_id"]: r["withdrawn_at"] for r in rows}


def _withdrawn_at(conn: sqlite3.Connection, version_id: int) -> str | None:
    row = conn.execute(
        "SELECT withdrawn_at FROM withdrawals WHERE version_id = ?",
        (version_id,)).fetchone()
    return None if row is None else row["withdrawn_at"]


def _remaining(conn: sqlite3.Connection,
               base: sqlite3.Row) -> list[tuple[str, str]]:
    """基版在其当前生效区间内尚未被子更正覆盖的子区间集合。

    已撤回的子更正不再占用父版本区间（撤回后该区间恢复父版本正文，可再次更正），
    故这里只扣除**仍生效**（未撤回）的子更正。
    """
    withdrawn = _withdrawn_map(conn)
    holes = [(c["valid_start"], c["valid_end"])
             for c in _children(conn, base["id"])
             if c["id"] not in withdrawn]
    return _subtract_intervals((base["valid_start"], base["valid_end"]), holes)

# ---------------------------------------------------------------- 首次发布


def publish_releases(items: list) -> dict:
    """首次发布一个或多个规程；整批原子提交。"""
    normalized: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        number = _clean_number(item.number, i)
        body = _clean_text(item.body, "body", i)
        start, end = _validated_interval(item.valid_start, item.valid_end, i)
        if number in seen:
            raise errors.duplicate_number(number, i)
        seen.add(number)
        normalized.append((number, body, start, end))

    with write_tx() as conn:
        published_at = _server_now()
        created: list[dict] = []
        for i, (number, body, start, end) in enumerate(normalized):
            if _fetch_procedure(conn, number) is not None:
                raise errors.duplicate_number(number, i)
        for number, body, start, end in normalized:
            cur = conn.execute(
                "INSERT INTO procedures (number, created_at) VALUES (?, ?)",
                (number, published_at))
            proc_id = cur.lastrowid
            conn.execute(
                "INSERT INTO versions (procedure_id, revision, body, "
                "valid_start, valid_end, supersedes_id, published_at) "
                "VALUES (?, 0, ?, ?, ?, NULL, ?)",
                (proc_id, body, start, end, published_at))
            created.append({"number": number, "revision": 0, "body": body,
                            "valid_start": start, "valid_end": end,
                            "published_at": published_at})
    return {"published_at": published_at, "items": created}

# ---------------------------------------------------------------- 更正提交


def submit_corrections(items: list) -> dict:
    """提交一条或多条更正；任一非法则整批回滚。

    同批次内条目按顺序处理，因此第 k 条可以以同批次更早插入的新版本为基版
    （此时 revision 号为可预知的该规程当前最大号 +1）。
    """
    with write_tx() as conn:
        published_at = _server_now()
        # (proc_id, base_revision) -> 本批次内已占用的子区间
        pending: dict[tuple[int, int], list[tuple[str, str]]] = {}
        created: list[dict] = []

        for i, item in enumerate(items):
            number = _clean_number(item.number, i)
            body = _clean_text(item.body, "body", i)
            sub = _validated_interval(item.valid_start, item.valid_end, i)

            proc = _fetch_procedure(conn, number)
            if proc is None:
                raise errors.procedure_not_found(number, i)
            proc_id = proc["id"]
            base = _fetch_version(conn, proc_id, item.base_revision)
            if base is None:
                raise errors.base_version_not_found(number, item.base_revision, i)

            # 1) 子区间必须整体落在基版区间内（非法区间优先于冲突判定）。
            base_iv = (base["valid_start"], base["valid_end"])
            if not (base_iv[0] <= sub[0] and sub[1] <= base_iv[1]):
                raise errors.invalid_interval(
                    sub[0], sub[1],
                    f"非法区间：更正子区间 [{sub[0]}, {sub[1]}) 超出被替代版本 "
                    f"r{item.base_revision} 的适用区间 [{base_iv[0]}, {base_iv[1]})",
                    i)

            # 1.5) 已撤回版本不再是任何更正的基版：其区间已恢复由父版本持有，
            #      若要改正文请改以父版本（当前生效版本）为被替代版本。
            base_withdrawn_at = _withdrawn_at(conn, base["id"])
            if base_withdrawn_at is not None:
                raise errors.base_version_withdrawn(
                    number, item.base_revision, base_withdrawn_at, i)

            # 2) 同一被替代版本的并发更正不得在生效区间上相互冲突。
            #    先于"剩余区间"检查：同批次更早条目虽已插入当前事务、
            #    尚非已发布历史，对它们的重叠属于并发更正，而非过期基版。
            key = (proc_id, item.base_revision)
            clash = _first_overlap(sub, pending.get(key, []))
            if clash is not None:
                raise errors.concurrent_conflict(number, item.base_revision,
                                                 clash, i)

            # 3) 扣除已发布子更正后的剩余生效区间；子区间不落在其中 -> 过期基版。
            #    （步骤 1 已保证子区间在基版区间内，故未被剩余区间覆盖的部分
            #    必然与某个已发布子更正重叠，即客户依据的是已被替代的旧版。）
            remaining = _remaining(conn, base)
            if not _covered_by(sub, remaining):
                raise errors.expired_base(number, item.base_revision, remaining, i)

            row = conn.execute(
                "SELECT COALESCE(MAX(revision), -1) + 1 AS next_revision "
                "FROM versions WHERE procedure_id = ?", (proc_id,)).fetchone()
            new_revision = row["next_revision"]

            conn.execute(
                "INSERT INTO versions (procedure_id, revision, body, "
                "valid_start, valid_end, supersedes_id, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (proc_id, new_revision, body, sub[0], sub[1],
                 base["id"], published_at))
            pending.setdefault(key, []).append(sub)
            created.append({"number": number, "revision": new_revision,
                            "supersedes_revision": item.base_revision,
                            "body": body, "valid_start": sub[0],
                            "valid_end": sub[1], "published_at": published_at})

    return {"published_at": published_at, "items": created}

# ---------------------------------------------------------------- 更正撤回


def withdraw_corrections(items: list) -> dict:
    """撤回一条或多条误发布的更正；整批原子，任一非法则整批回滚。

    撤回只向 withdrawals 追加事件行（withdrawn_at 由服务端时钟记录、不可回填），
    绝不删除或改写 versions 行。拒绝条件（命中即整体拒绝、不改历史）：

    1. 规程编号不存在 -> 404 not_found；
    2. 版本号不存在 -> 404 revision_not_found；
    3. 首版（revision=0）-> 409 first_revision_not_withdrawable；
    4. 已撤回版本 -> 409 already_withdrawn（响应带服务端记录的 withdrawn_at）；
    5. 已有子更正的版本 -> 409 version_has_children（只能撤回叶子更正）。

    写操作经全局写锁串行化，并在同一 BEGIN IMMEDIATE 事务内"校验 + 追加事件"，
    因此撤回与并发更正按确定的全局次序先后生效，同一区间不会并存两份当前正文。
    """
    with write_tx() as conn:
        withdrawn_at = _server_now()
        created: list[dict] = []

        for i, item in enumerate(items):
            number = _clean_number(item.number, i)
            revision = item.revision

            proc = _fetch_procedure(conn, number)
            if proc is None:
                raise errors.procedure_not_found(number, i)
            proc_id = proc["id"]
            version = _fetch_version(conn, proc_id, revision)
            if version is None:
                raise errors.revision_not_found(number, revision, i)

            # 3) 首版只能更正，不能撤回。
            if version["supersedes_id"] is None:
                raise errors.first_revision_not_withdrawable(number, revision, i)

            # 4) 已撤回：撤回事件只追加一次；UNIQUE(version_id) 是最终防线。
            existing = _withdrawn_at(conn, version["id"])
            if existing is not None:
                raise errors.already_withdrawn(number, revision, existing, i)

            # 5) 已有子更正：撤回只追加事件、子更正行永不删除，因此只要版本
            #    曾被更正（子行存在，无论其自身是否已撤回）就拒绝撤回本版本；
            #    可撤回的是当前仍生效的"叶子"更正，撤回后其区间恢复直接父版本。
            children = _children(conn, version["id"])
            if children:
                child_revisions = sorted(c["revision"] for c in children)
                raise errors.version_has_children(
                    number, revision, child_revisions, i)

            conn.execute(
                "INSERT INTO withdrawals (version_id, withdrawn_at) "
                "VALUES (?, ?)",
                (version["id"], withdrawn_at))
            created.append({
                "number": number,
                "revision": revision,
                "supersedes_revision": _revision_of(conn, version["supersedes_id"]),
                "body": version["body"],
                "valid_start": version["valid_start"],
                "valid_end": version["valid_end"],
                "published_at": version["published_at"],
                "withdrawn_at": withdrawn_at,
            })

    return {"withdrawn_at": withdrawn_at, "items": created}


def _revision_of(conn: sqlite3.Connection, version_id: int) -> int:
    row = conn.execute("SELECT revision FROM versions WHERE id = ?",
                       (version_id,)).fetchone()
    return row["revision"]

# ---------------------------------------------------------------- 回溯查询


def _version_payload(row: sqlite3.Row, number: str,
                     parent_rev: int | None) -> dict:
    """一个版本对外呈现的完整字段（规程包快照也按此形状留档/取回）。"""
    return {
        "number": number,
        "revision": row["revision"],
        "body": row["body"],
        "valid_start": row["valid_start"],
        "valid_end": row["valid_end"],
        "supersedes_revision": parent_rev,
        "published_at": row["published_at"],
    }


def _serialize(row: sqlite3.Row,
               parent_rev: dict[int, int],
               withdrawn_at_by_id: dict[int, str] | None = None) -> dict:
    pid = row["supersedes_id"]
    out = _version_payload(row, row["number"],
                           parent_rev.get(pid) if pid is not None else None)
    # 审计视图追加撤回时刻（仅历史接口传入该映射）；回溯接口输出约定保持不变。
    if withdrawn_at_by_id is not None:
        out["withdrawn_at"] = withdrawn_at_by_id.get(row["id"])
    return out


def _depth_of(version_id: int, by_id: dict[int, sqlite3.Row],
              depth_cache: dict[int, int]) -> int:
    """版本在替代链（correction chain）上的深度：首版为 1，每被更正一次 +1。

    作业时刻有效版本即所有覆盖该时刻的可见版本中链上最深者。
    """
    chain: list[int] = []
    cur_id: int | None = version_id
    while cur_id is not None and cur_id not in depth_cache:
        chain.append(cur_id)
        parent = by_id[cur_id]["supersedes_id"]
        cur_id = parent if parent is not None else None
    base = depth_cache[cur_id] if cur_id is not None else 0
    for vid in reversed(chain):
        base += 1
        depth_cache[vid] = base
    return depth_cache[version_id]


def _effective_at(t: str, candidates: list[sqlite3.Row],
                  by_id: dict[int, sqlite3.Row],
                  depth_cache: dict[int, int]) -> sqlite3.Row | None:
    """作业时刻 t 处沿更正链取最深的可见版本；无版本覆盖返回 None。

    左闭右开：valid_start <= t < valid_end。同深度仅作确定性兜底
    （同一父版本的更正区间互不重叠，正常数据下同深度候选只会有一个）。
    """
    best: sqlite3.Row | None = None
    best_depth = 0
    for r in candidates:
        if r["valid_start"] <= t < r["valid_end"]:
            d = _depth_of(r["id"], by_id, depth_cache)
            if (best is None or d > best_depth
                    or (d == best_depth
                        and (r["revision"], r["published_at"])
                        > (best["revision"], best["published_at"]))):
                best, best_depth = r, d
    return best


def _visible_versions(conn: sqlite3.Connection, proc_id: int,
                      known_as_of: str) -> list[sqlite3.Row]:
    """知悉时刻下"当时仍生效"的版本：已发布且（未撤回或撤回晚于该时刻）。

    边界与发布侧对称（均为含等号）：``published_at <= k`` 时版本已发布可见，
    ``withdrawn_at >= k`` 时撤回事件尚未发生、版本仍生效；严格晚于撤回时刻
    （``withdrawn_at < k``）才视为已撤回。因此对撤回之前（含撤回瞬间）的知悉
    时刻，被撤回版本仍在版本链中可见，撤回前的回溯与区间对比结果保持原样。
    """
    return conn.execute(
        """
        SELECT v.* FROM versions v
        LEFT JOIN withdrawals w ON w.version_id = v.id
         WHERE v.procedure_id = ?
           AND v.published_at <= ?
           AND (w.version_id IS NULL OR w.withdrawn_at >= ?)
        """,
        (proc_id, known_as_of, known_as_of)).fetchall()


def _resolve_effective(conn: sqlite3.Connection, proc: sqlite3.Row,
                       work_time: str, known_as_of: str
                       ) -> tuple[sqlite3.Row | None, int | None]:
    """某规程在 (作业时刻, 知悉时刻) 下实际适用的版本及其父版本号。

    沿现有双时间维约定：仅取该知悉时刻可见（已发布且未撤回或撤回不早于该
    时刻）的版本，在覆盖作业时刻（左闭右开）的候选中沿 supersedes 链取最深者。
    无适用正文时版本为 None。固定与复核共用本函数，语义与回溯查询完全一致。
    """
    visible = _visible_versions(conn, proc["id"], known_as_of)
    by_id = {v["id"]: v for v in visible}
    depth_cache: dict[int, int] = {}
    best = _effective_at(work_time, visible, by_id, depth_cache)
    if best is None:
        return None, None
    parent_rev: int | None = None
    if best["supersedes_id"] is not None:
        parent = by_id.get(best["supersedes_id"])
        if parent is not None:
            parent_rev = parent["revision"]
        else:
            # 理论上最深候选的父版本必然可见；兜底从全量版本读取父版本号。
            parent_rev = _revision_of(conn, best["supersedes_id"])
    return best, parent_rev


def query_effective(work_time_raw: str, known_as_of_raw: str | None) -> dict:
    """返回截至知悉时刻已发布、且在作业时刻有效的各规程正文与版本。"""
    try:
        work_time = parse_canonical(work_time_raw, "work_time")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422, {"field": "work_time"})
    if known_as_of_raw is None or not str(known_as_of_raw).strip():
        known_as_of = now_canonical()
    else:
        try:
            known_as_of = parse_canonical(known_as_of_raw, "known_as_of")
        except TimeParseError as exc:
            raise DomainError("invalid_time", str(exc), 422,
                              {"field": "known_as_of"})

    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT v.*, p.number AS number
              FROM versions v
              JOIN procedures p ON p.id = v.procedure_id
              LEFT JOIN withdrawals w ON w.version_id = v.id
             WHERE v.published_at <= ?
               AND (w.version_id IS NULL OR w.withdrawn_at >= ?)
               AND v.valid_start <= ?
               AND ? < v.valid_end
             ORDER BY p.number
            """,
            (known_as_of, known_as_of, work_time, work_time)).fetchall()
        if not rows:
            return {"work_time": work_time, "known_as_of": known_as_of,
                    "count": 0, "items": []}

        # 取候选涉及的全部版本，构建父链并计算深度。
        proc_ids = {r["procedure_id"] for r in rows}
        all_versions: list[sqlite3.Row] = []
        for proc_id in proc_ids:
            all_versions.extend(conn.execute(
                "SELECT * FROM versions WHERE procedure_id = ?",
                (proc_id,)).fetchall())

        by_id = {v["id"]: v for v in all_versions}
        parent_rev = {v["id"]: v["revision"] for v in all_versions}
        depth_cache: dict[int, int] = {}

        best: dict[int, sqlite3.Row] = {}
        best_depth: dict[int, int] = {}
        for r in rows:
            pid = r["procedure_id"]
            d = _depth_of(r["id"], by_id, depth_cache)
            cur = best.get(pid)
            if (cur is None or d > best_depth[pid]
                    # 同深度理论上不会出现（覆盖互不重叠）；以下仅为确定性兜底
                    or (d == best_depth[pid]
                        and (r["revision"], r["published_at"])
                        > (cur["revision"], cur["published_at"]))):
                best[pid] = r
                best_depth[pid] = d

        items = sorted((_serialize(r, parent_rev) for r in best.values()),
                       key=lambda x: x["number"])

    return {"work_time": work_time, "known_as_of": known_as_of,
            "count": len(items), "items": items}

# ---------------------------------------------------------------- 历史查看


def list_history(number_raw: str) -> dict:
    """返回某规程全部版本（管理用审计视图，不做知悉时刻裁剪）。"""
    number = number_raw.strip()
    with read_conn() as conn:
        proc = _fetch_procedure(conn, number) if number else None
        if proc is None:
            raise DomainError("not_found", f"规程编号 {number or '(空)'} 不存在",
                              404, {"number": number})
        rows = conn.execute(
            """
            SELECT v.*, p.number AS number
              FROM versions v
              JOIN procedures p ON p.id = v.procedure_id
             WHERE v.procedure_id = ?
             ORDER BY v.revision
            """, (proc["id"],)).fetchall()
        parent_rev: dict[int, int] = {}
        if rows:
            ids = [r["supersedes_id"] for r in rows if r["supersedes_id"] is not None]
            if ids:
                placeholders = ",".join("?" * len(ids))
                for prow in conn.execute(
                        f"SELECT id, revision FROM versions WHERE id IN ({placeholders})",
                        ids):
                    parent_rev[prow["id"]] = prow["revision"]
        # 撤回版本与撤回时刻对审计可见（撤回只追加事件、版本行仍在）。
        withdrawn = _withdrawn_map(conn, [r["id"] for r in rows])
        items = [_serialize(r, parent_rev, withdrawn) for r in rows]
    return {"number": number, "count": len(items), "items": items}


# ---------------------------------------------------------------- 两知悉时刻区间对比


def _merge_adjacent(segments: list[tuple[str, str, int | None, int | None]]
                    ) -> list[tuple[str, str, int | None, int | None]]:
    """合并首尾相接且两侧版本完全相同的相邻片段（区间均为左闭右开）。"""
    merged: list[tuple[str, str, int | None, int | None]] = []
    for seg in segments:
        s, e, left, right = seg
        if merged and merged[-1][1] == s and merged[-1][2:] == (left, right):
            merged[-1] = (merged[-1][0], e, left, right)
        else:
            merged.append(seg)
    return merged


def compare_interval(number_raw: str,
                     known_before_raw: str,
                     known_after_raw: str,
                     work_start_raw: str,
                     work_end_raw: str) -> dict:
    """比较同一规程在先后两个知悉时刻、对作业区间 [work_start, work_end) 的
    实际适用内容，逐段返回两侧可见版本不同的最大片段。

    各侧状态按发布时间（published_at <= 知悉时刻）沿更正链还原：
    覆盖同一作业时刻的候选版本中，链上最深者为该时刻的有效版本；
    无任何可见版本覆盖时该侧记为空（revision/body 均为 null）。
    """
    # 入参全部校验通过后再查库；任何非法输入均只读、不写历史。
    number = number_raw.strip()
    try:
        work_start = parse_canonical(work_start_raw, "work_start")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "work_start"})
    try:
        work_end = parse_canonical(work_end_raw, "work_end")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "work_end"})
    if not work_start < work_end:
        raise errors.invalid_interval(
            work_start, work_end,
            "作业区间非法：work_start 必须严格早于 work_end（区间为 [起, 止) 且长度大于 0）")
    try:
        known_before = parse_canonical(known_before_raw, "known_before")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "known_before"})
    try:
        known_after = parse_canonical(known_after_raw, "known_after")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "known_after"})
    if not known_before < known_after:
        raise errors.invalid_known_order(known_before, known_after)

    with read_conn() as conn:
        proc = _fetch_procedure(conn, number) if number else None
        if proc is None:
            raise DomainError("not_found", f"规程编号 {number or '(空)'} 不存在",
                              404, {"number": number})

        before_rows = _visible_versions(conn, proc["id"], known_before)
        after_rows = _visible_versions(conn, proc["id"], known_after)

        # 任一版本（含对某侧不可见的版本）的区间端点都可能是有效正文切换点，
        # 故用两侧并集的全部端点切分作业区间；片段内部两侧版本各自恒定。
        all_rows = {v["id"]: v for v in before_rows}
        for v in after_rows:
            all_rows.setdefault(v["id"], v)
        depth_cache: dict[int, int] = {}

        bounds = {work_start, work_end}
        for v in all_rows.values():
            if v["valid_end"] <= work_start or work_end <= v["valid_start"]:
                continue                        # 与作业区间不相交
            bounds.add(max(v["valid_start"], work_start))
            bounds.add(min(v["valid_end"], work_end))
        ordered = sorted(bounds)

        raw: list[tuple[str, str, int | None, int | None]] = []
        for s, e in zip(ordered, ordered[1:]):
            lv = _effective_at(s, before_rows, all_rows, depth_cache)
            rv = _effective_at(s, after_rows, all_rows, depth_cache)
            raw.append((s, e, lv["id"] if lv is not None else None,
                        rv["id"] if rv is not None else None))

        # 合并相邻且两侧版本相同的片段，再剔除两侧版本相同（含均为空）的片段。
        segments: list[dict] = []
        for s, e, lid, rid in _merge_adjacent(raw):
            if lid == rid:
                continue
            lv = all_rows.get(lid) if lid is not None else None
            rv = all_rows.get(rid) if rid is not None else None
            segments.append({
                "start": s,
                "end": e,
                "before": None if lv is None else {
                    "revision": lv["revision"], "body": lv["body"]},
                "after": None if rv is None else {
                    "revision": rv["revision"], "body": rv["body"]},
            })

    return {"number": number,
            "known_before": known_before,
            "known_after": known_after,
            "work_start": work_start,
            "work_end": work_end,
            "count": len(segments),
            "segments": segments}


# ---------------------------------------------------------------- 作业规程包


def _package_time(value: str, field: str) -> str:
    """解析固定/复核入参中的时刻（必须带时区）。"""
    try:
        return parse_canonical(value, field)
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422, {"field": field})


def create_package(work_time_raw: str, known_as_of_raw: str,
                   numbers: list) -> dict:
    """按作业时刻、知悉时刻与一组规程编号固定作业规程包。

    整体语义（任一不满足即**整体拒绝、不留包**）：

    * 编号列表非空且不得重复（空列表在请求模型层 422，重复 422
      duplicate_package_number）；
    * 每个编号都必须存在（404 procedure_not_found）；
    * 在包知悉时刻下、对作业时刻，每个规程都必须有已发布且生效（未撤回）
      的适用正文（沿用左闭右开与撤回边界），否则 409 no_applicable_version；
    * 知悉时刻不得晚于服务端当前时刻（422 known_as_of_in_future）。

    全部校验与快照写入在同一条 BEGIN IMMEDIATE 事务内完成，读取的是该
    事务的一致快照，不会拼接并发写入前后的结果；失败即回滚、不留包。
    """
    work_time = _package_time(work_time_raw, "work_time")
    known_as_of = _package_time(known_as_of_raw, "known_as_of")

    cleaned: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(numbers):
        number = raw.strip() if isinstance(raw, str) else ""
        if not number:
            raise DomainError("invalid_number", "规程编号不能为空字符串",
                              422, {"field": "numbers", "index": i})
        if number in seen:
            raise errors.duplicate_package_number(number, i)
        seen.add(number)
        cleaned.append(number)

    with write_tx() as conn:
        # 服务端当前时刻取自事务内：知悉时刻晚于该时刻即视为"未来"而拒绝，
        # 固定时刻 created_at 同样由服务端单调时钟记录、接口不接受回填。
        server_now = _server_now()
        if known_as_of > server_now:
            raise errors.known_as_of_in_future(known_as_of, server_now)

        resolved: list[tuple[int, dict]] = []
        for i, number in enumerate(cleaned):
            proc = _fetch_procedure(conn, number)
            if proc is None:
                raise errors.package_procedure_not_found(number, i)
            best, parent_rev = _resolve_effective(
                conn, proc, work_time, known_as_of)
            if best is None:
                raise errors.no_applicable_version(
                    number, work_time, known_as_of, i)
            payload = _version_payload(best, number, parent_rev)
            resolved.append((proc["id"], payload))

        package_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO packages (id, work_time, known_as_of, created_at, "
            "item_count) VALUES (?, ?, ?, ?, ?)",
            (package_id, work_time, known_as_of, server_now, len(resolved)))
        for position, (proc_id, payload) in enumerate(resolved):
            conn.execute(
                "INSERT INTO package_items (package_id, position, procedure_id, "
                "number, revision, body, valid_start, valid_end, "
                "supersedes_revision, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (package_id, position, proc_id, payload["number"],
                 payload["revision"], payload["body"], payload["valid_start"],
                 payload["valid_end"], payload["supersedes_revision"],
                 payload["published_at"]))

    return _package_response(package_id, work_time, known_as_of, server_now,
                             [payload for _, payload in resolved])


def _package_rows(conn: sqlite3.Connection,
                  package_id: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    pkg = conn.execute("SELECT * FROM packages WHERE id = ?",
                       (package_id,)).fetchone()
    if pkg is None:
        raise errors.package_not_found(package_id)
    items = conn.execute(
        "SELECT * FROM package_items WHERE package_id = ? ORDER BY position",
        (package_id,)).fetchall()
    return pkg, items


def _item_payload(row: sqlite3.Row) -> dict:
    return {"number": row["number"], "revision": row["revision"],
            "body": row["body"], "valid_start": row["valid_start"],
            "valid_end": row["valid_end"],
            "supersedes_revision": row["supersedes_revision"],
            "published_at": row["published_at"]}


def _package_response(package_id: str, work_time: str, known_as_of: str,
                      created_at: str, items: list[dict]) -> dict:
    return {"id": package_id, "work_time": work_time,
            "known_as_of": known_as_of, "created_at": created_at,
            "count": len(items), "items": items}


def get_package(package_id_raw: str) -> dict:
    """按标识取回包内容：永远返回固定时留下的快照（含正文），不受之后的
    更正、撤回影响。包不存在 -> 404 package_not_found。
    """
    package_id = package_id_raw.strip()
    with read_snapshot() as conn:
        pkg, rows = _package_rows(conn, package_id)
        items = [_item_payload(r) for r in rows]
        return _package_response(pkg["id"], pkg["work_time"],
                                 pkg["known_as_of"], pkg["created_at"], items)


def review_package(package_id_raw: str, review_known_raw: str) -> dict:
    """复核作业规程包：以复核知悉时刻按原作业时刻逐项核对包内版本与彼时
    有效版本是否一致。

    复核知悉时刻必须不早于包内知悉时刻、且不晚于服务端当前时刻；
    包不存在 -> 404。包读取与对每个规程的版本解析在同一只读快照事务内
    完成，保证"整体是否仍一致"基于同一数据版本、不拼接并发写入。
    """
    package_id = package_id_raw.strip()
    review_known = _package_time(review_known_raw, "known_as_of")

    with read_snapshot() as conn:
        # 只读路径必须使用不触碰写状态/写锁的墙上时钟：写路径的 _server_now
        # 依赖共享单调状态，在本读事务持有快照期间若并发写事务获取写锁，会令
        # 本连接的读事务被隐式结束、从而破坏快照隔离。读侧取"当前时刻"与
        # /effective 缺省知悉时刻一致，直接读墙上时钟即可。
        server_now = now_canonical()
        if review_known > server_now:
            raise errors.known_as_of_in_future(review_known, server_now)

        pkg, pinned_rows = _package_rows(conn, package_id)
        package_known = pkg["known_as_of"]
        work_time = pkg["work_time"]
        if review_known < package_known:
            raise errors.review_known_too_early(review_known, package_known)

        items: list[dict] = []
        all_match = True
        for row in pinned_rows:
            proc = _fetch_procedure_by_id(conn, row["procedure_id"])
            best, parent_rev = (None, None)
            if proc is not None:
                best, parent_rev = _resolve_effective(
                    conn, proc, work_time, review_known)
            current = (None if best is None
                       else _version_payload(best, row["number"], parent_rev))
            pinned = _item_payload(row)
            # 是否变化只看版本号（与两知悉时刻对比约定一致）；规程在复核时刻
            # 已无任何适用正文（current 为 null）也视为发生了变化。
            changed = current is None or current["revision"] != row["revision"]
            if changed:
                all_match = False
            items.append({"number": row["number"], "pinned": pinned,
                          "current": current, "changed": changed})

    return {"id": package_id, "work_time": work_time,
            "package_known_as_of": package_known,
            "known_as_of": review_known,
            "created_at": pkg["created_at"],
            "count": len(items),
            "all_match": all_match,
            "items": items}
