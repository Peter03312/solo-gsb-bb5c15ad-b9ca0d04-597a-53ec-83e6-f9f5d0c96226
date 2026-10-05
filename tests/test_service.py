"""端到端测试：覆盖双时间维查询、更正裁剪与四类整体拒绝。

运行：python -m unittest discover -s tests -v
（需先安装 requirements-dev.txt，仅 API 测试需要 fastapi/httpx）
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# 每个测试用独立临时库，必须在导入 app.config 之前设置
_tmpdir = tempfile.mkdtemp(prefix="sop-test-")
os.environ["SOP_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["SOP_PORT"] = "8099"

from app import service  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.errors import DomainError  # noqa: E402
from app.time_utils import now_canonical, parse_canonical  # noqa: E402

init_db()


def canon(value: str) -> str:
    """测试辅助：把无小数秒的时间转成与存储一致的规范形式。"""
    return parse_canonical(value)


def reset_db() -> None:
    """每个用例重建空库，保证用例彼此独立、可重复运行。"""
    import sqlite3
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            "DELETE FROM prerequisites; "
            "DELETE FROM package_items; DELETE FROM packages; "
            "DELETE FROM versions; DELETE FROM procedures;")
        conn.commit()
    finally:
        conn.close()

_T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_us(dt: datetime) -> str:
    """带微秒的 UTC 串（用于服务端发布时刻边界）。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


t0 = iso(_T - timedelta(days=1))      # 知悉时刻早于发布
t1 = iso(_T)                          # 区间起点
t2 = iso(_T + timedelta(days=30))
t3 = iso(_T + timedelta(days=60))
t4 = iso(_T + timedelta(days=90))
t5 = iso(_T + timedelta(days=365))    # 区间终点（不含）
future = iso(_T + timedelta(days=400))


def item(number: str, body: str = "v0", start: str = t1, end: str = t5,
         base: int | None = None, prerequisites=None):
    data = {"number": number, "body": body,
            "valid_start": start, "valid_end": end}
    if base is not None:
        data["base_revision"] = base
    if prerequisites is not None:
        data["prerequisites"] = prerequisites
    return SimpleNamespace(**data)


def release(number: str, start: str = t1, end: str = t5, body: str = "初版",
            prerequisites=None):
    return service.publish_releases(
        [item(number, body, start, end, prerequisites=prerequisites)])


def correct(number: str, base: int, start: str, end: str, body: str = "更正",
            prerequisites=None):
    return service.submit_corrections(
        [item(number, body, start, end, base, prerequisites=prerequisites)])


def witem(number: str, revision: int):
    return SimpleNamespace(number=number, revision=revision)


def withdraw(number: str, revision: int):
    return service.withdraw_corrections([witem(number, revision)])


def numbers_at(work: str, known: str) -> dict[str, dict]:
    return {x["number"]: x for x in service.query_effective(work, known)["items"]}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        release("DUP-1")
        with self.assertRaises(DomainError) as ctx:
            release("DUP-1")
        self.assertEqual(ctx.exception.code, "duplicate_number")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_duplicate_in_same_batch(self):
        with self.assertRaises(DomainError) as ctx:
            service.publish_releases([item("DUP-BATCH"), item("DUP-BATCH")])
        self.assertEqual(ctx.exception.code, "duplicate_number")
        self.assertEqual(ctx.exception.details["index"], 1)

    def test_invalid_intervals(self):
        # 起 >= 止
        with self.assertRaises(DomainError) as ctx:
            release("BAD-IV-1", start=t3, end=t2)
        self.assertEqual(ctx.exception.code, "invalid_interval")
        # 空区间 [t1, t1)
        with self.assertRaises(DomainError):
            release("BAD-IV-2", start=t1, end=t1)

    def test_naive_time_rejected(self):
        with self.assertRaises(DomainError):
            service.publish_releases([item("BAD-TZ", start="2026-01-01T00:00:00",
                                           end=t5)])


class QueryTests(unittest.TestCase):
    def setUp(self):
        reset_db()

    def test_basic_visibility(self):
        release("Q-1")
        # 作业时刻在区间外 -> 无
        self.assertEqual(numbers_at(t5, future), {})
        self.assertEqual(numbers_at(iso(_T - timedelta(seconds=1)), future), {})
        # 区间内可见
        row = numbers_at(t2, future)["Q-1"]
        self.assertEqual(row["revision"], 0)
        self.assertEqual(row["body"], "初版")
        # 知悉时刻早于发布 -> 空
        self.assertEqual(numbers_at(t2, t0), {})

    def test_known_as_of_defaults_now(self):
        release("Q-2")  # 发布时间 = 现在，作业区间在 2026
        row = numbers_at(t2, None)["Q-2"]
        self.assertEqual(row["revision"], 0)

    def test_offset_timezone_normalized_to_utc(self):
        service.publish_releases([item("Q-TZ", start=t1, end=t5)])
        # +08:00 的 08:00 == UTC 00:00，即区间起点（含），可见
        row = numbers_at("2026-01-01T08:00:00+08:00", future)["Q-TZ"]
        self.assertEqual(row["revision"], 0)
        # 终点（不含）：UTC 2027-01-01 00:00 == +08:00 08:00
        self.assertNotIn("Q-TZ", numbers_at("2027-01-01T08:00:00+08:00", future))


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        release("C-1", body="初版")

    def test_correction_covers_only_subinterval(self):
        correct("C-1", 0, t2, t3, "三月版")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=29)), future)["C-1"]["body"], "初版")
        self.assertEqual(numbers_at(t2, future)["C-1"]["body"], "三月版")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=59)), future)["C-1"]["body"], "三月版")
        self.assertEqual(numbers_at(t3, future)["C-1"]["body"], "初版")  # 止不含

    def test_query_before_correction_published_unchanged(self):
        published = correct("C-1", 0, t2, t3, "三月版")["published_at"]
        # 用更正发布时刻本身（published_at <= known 含等号）刚可见；前 1 微秒不可见
        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        before = iso_us(dt - timedelta(microseconds=1))
        # 作业时刻同样规范化到 UTC 微秒串，避免与初版发布时刻误比
        self.assertEqual(numbers_at(canon(t2), before)["C-1"]["body"], "初版")
        self.assertEqual(numbers_at(canon(t2), published)["C-1"]["body"], "三月版")

    def test_concurrent_overlap_rejected(self):
        # 顺序提交且与"已发布"的更正重叠：客户依据的是旧版 r0 的当前生效区间，
        # 属于过期基版（应改以 r1 为被替代版本），历史保持原样。
        correct("C-1", 0, t2, t3, "三月版")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, iso(_T + timedelta(days=40)),
                    iso(_T + timedelta(days=50)), "冲突")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        self.assertEqual(ctx.exception.http_status, 409)
        # 历史不变：该日期仍是第一条更正正文
        self.assertEqual(numbers_at(iso(_T + timedelta(days=45)),
                                    future)["C-1"]["body"], "三月版")

    def test_overlap_inside_same_batch_is_conflict(self):
        # 同一批次（并发提交）内两条更正相互冲突 -> concurrent_correction_conflict
        with self.assertRaises(DomainError) as ctx:
            service.submit_corrections([
                item("C-1", "A", t2,
                     iso(_T + timedelta(days=50)), base=0),
                item("C-1", "B", iso(_T + timedelta(days=40)),
                     iso(_T + timedelta(days=55)), base=0),
            ])
        self.assertEqual(ctx.exception.code,
                         "concurrent_correction_conflict")
        self.assertEqual(ctx.exception.details["index"], 1)
        # 整批回滚：均不存在
        self.assertEqual(numbers_at(t2, future)["C-1"]["revision"], 0)

    def test_adjacent_intervals_allowed(self):
        correct("C-1", 0, t2, t3, "二月")
        correct("C-1", 0, t3, t4, "三月")  # 首尾相接不算重叠
        self.assertEqual(numbers_at(t3, future)["C-1"]["body"], "三月")

    def test_interval_outside_base_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, iso(_T - timedelta(days=1)), t2, "越界")
        self.assertEqual(ctx.exception.code, "invalid_interval")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, t4, iso(_T + timedelta(days=400)), "越界")
        self.assertEqual(ctx.exception.code, "invalid_interval")

    def test_expired_base_version(self):
        # 基版区间被全部覆盖后再以其为基版 -> expired_base_version
        correct("C-1", 0, t1, t3, "上半段")
        correct("C-1", 0, t3, t5, "下半段")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, t2, t3, "基版已全覆盖")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["remaining_intervals"], [])

    def test_expired_base_when_partial_overlap_existing_child(self):
        correct("C-1", 0, t2, t4, "长更正")
        # 与其部分重叠（依据了旧版 r0 的当前生效区间之外的部分）
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, iso(_T + timedelta(days=20)),
                    iso(_T + timedelta(days=40)), "重叠")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        remaining = ctx.exception.details["remaining_intervals"]
        self.assertEqual([(r["start"], r["end"]) for r in remaining],
                         [(canon(t1), canon(t2)), (canon(t4), canon(t5))])

    def test_missing_procedure_and_base(self):
        with self.assertRaises(DomainError) as ctx:
            correct("NOPE", 0, t1, t2)
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 9, t1, t2)
        self.assertEqual(ctx.exception.code, "base_version_not_found")

    def test_chained_correction_and_depth(self):
        correct("C-1", 0, t2, t4, "r1")
        correct("C-1", 1, t3, t4, "r2")          # r1 内的子区间
        correct("C-1", 1, t2, t3, "r3")          # r1 剩余的另一半
        # t2..t3 -> r3（最深链 v0<-v1<-v3）
        self.assertEqual(numbers_at(iso(_T + timedelta(days=45)),
                                    future)["C-1"]["body"], "r3")
        # t3..t4 -> r2
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["C-1"]["body"], "r2")
        # r1 已被 r2/r3 完整覆盖，再以 r1 为基版 -> 过期
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 1, iso(_T + timedelta(days=40)),
                    iso(_T + timedelta(days=45)), "x")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        # r0 在 t4..t5 仍生效，可继续更正
        correct("C-1", 0, t4, t5, "r4")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=100)),
                                    future)["C-1"]["body"], "r4")

    def test_batch_atomic_rollback(self):
        # 第二条与第一条区间重叠（同为对 r0 的并发更正）-> 第一条也不得入库
        with self.assertRaises(DomainError) as ctx:
            service.submit_corrections([
                item("C-1", "五月", iso(_T + timedelta(days=120)),
                     iso(_T + timedelta(days=150)), base=0),
                item("C-1", "冲突", iso(_T + timedelta(days=130)),
                     iso(_T + timedelta(days=140)), base=0),
            ])
        self.assertEqual(ctx.exception.code,
                         "concurrent_correction_conflict")
        self.assertEqual(ctx.exception.details["index"], 1)
        # 第一条不存在：该时间段回到初版
        self.assertEqual(numbers_at(iso(_T + timedelta(days=130)),
                                    future)["C-1"]["body"], "初版")

    def test_batch_with_chain_inside(self):
        # 同批次：先更正 r0 得到 r1，再以 r1 为基版
        res = service.submit_corrections([
            item("C-1", "r1", t2, t4, base=0),
            item("C-1", "r2", t3, t4, base=1),
        ])
        self.assertEqual([x["revision"] for x in res["items"]], [1, 2])
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["C-1"]["body"], "r2")

    def test_published_at_is_server_defined_and_returned(self):
        res = correct("C-1", 0, t2, t3)
        self.assertTrue(res["published_at"].endswith("Z"))
        history = service.list_history("C-1")
        self.assertEqual(history["count"], 2)
        self.assertEqual([v["revision"] for v in history["items"]], [0, 1])
        self.assertEqual(history["items"][1]["supersedes_revision"], 0)

    def test_multiple_procedures_in_one_query(self):
        release("C-2", body="另一个规程")
        correct("C-1", 0, t2, t3, "C1三月")
        rows = numbers_at(t2, future)
        self.assertEqual(rows["C-1"]["body"], "C1三月")
        self.assertEqual(rows["C-2"]["body"], "另一个规程")


class WithdrawalTests(unittest.TestCase):
    """更正撤回：只追加事件、双时间维可见性、拒绝条件与并发确定性。"""

    def setUp(self):
        reset_db()
        release("W-1", body="初版")

    def test_withdraw_restores_parent_body_afterward(self):
        correct("W-1", 0, t2, t3, "三月版")
        wd = withdraw("W-1", 1)
        self.assertEqual(wd["items"][0]["withdrawn_at"], wd["withdrawn_at"])
        self.assertEqual(wd["items"][0]["supersedes_revision"], 0)
        self.assertEqual(wd["items"][0]["body"], "三月版")
        self.assertEqual(wd["items"][0]["body"], "三月版")
        # 撤回后（默认知悉时刻=现在，晚于撤回）：子区间恢复父版本正文
        self.assertEqual(numbers_at(t2, future)["W-1"]["revision"], 0)
        self.assertEqual(numbers_at(t2, future)["W-1"]["body"], "初版")
        # 区间外本来就不受影响
        self.assertEqual(numbers_at(iso(_T + timedelta(days=100)),
                                    future)["W-1"]["revision"], 0)

    def test_past_known_as_of_unchanged_after_withdrawal(self):
        published = correct("W-1", 0, t2, t3, "三月版")["published_at"]
        withdrawn_at = withdraw("W-1", 1)["withdrawn_at"]
        p_dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        w_dt = datetime.strptime(withdrawn_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        before_pub = iso_us(p_dt - timedelta(microseconds=1))
        after_pub_before_wd = iso_us(p_dt + timedelta(microseconds=1))
        at_wd = iso_us(w_dt)
        after_wd = iso_us(w_dt + timedelta(microseconds=1))
        # 更正发布前：无更正
        self.assertEqual(numbers_at(canon(t2), before_pub)["W-1"]["body"], "初版")
        # 发布后、撤回前：仍按原版本链看到更正
        self.assertEqual(numbers_at(canon(t2), after_pub_before_wd)["W-1"]["body"],
                         "三月版")
        # 撤回时刻本身：withdrawn_at > known 为严格大于，等于时尚未撤回 -> 仍可见
        self.assertEqual(numbers_at(canon(t2), at_wd)["W-1"]["body"], "三月版")
        # 撤回后：恢复初版
        self.assertEqual(numbers_at(canon(t2), after_wd)["W-1"]["body"], "初版")

    def test_withdrawn_version_remains_in_history_with_timestamp(self):
        correct("W-1", 0, t2, t3, "三月版")
        withdrawn_at = withdraw("W-1", 1)["withdrawn_at"]
        history = service.list_history("W-1")
        self.assertEqual(history["count"], 2)          # 行不删除
        revs = {v["revision"]: v for v in history["items"]}
        self.assertIsNone(revs[0]["withdrawn_at"])
        self.assertEqual(revs[1]["withdrawn_at"], withdrawn_at)
        self.assertTrue(revs[1]["published_at"].endswith("Z"))
        self.assertEqual(revs[1]["supersedes_revision"], 0)

    def test_first_revision_cannot_withdraw(self):
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 0)
        self.assertEqual(ctx.exception.code,
                         "first_revision_not_withdrawable")
        self.assertEqual(ctx.exception.http_status, 409)
        # 历史不变
        self.assertEqual(service.list_history("W-1")["count"], 1)

    def test_double_withdraw_rejected(self):
        correct("W-1", 0, t2, t3, "三月版")
        first_at = withdraw("W-1", 1)["withdrawn_at"]
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 1)
        self.assertEqual(ctx.exception.code, "already_withdrawn")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["withdrawn_at"], first_at)

    def test_withdraw_version_with_children_rejected(self):
        correct("W-1", 0, t2, t4, "r1")
        correct("W-1", 1, t3, t4, "r2")
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 1)
        self.assertEqual(ctx.exception.code, "version_has_children")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["child_revisions"], [2])
        # 历史不变：r1/r2 仍生效
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["W-1"]["body"], "r2")

    def test_leaf_withdraw_restores_immediate_parent(self):
        correct("W-1", 0, t2, t4, "r1")
        correct("W-1", 1, t3, t4, "r2")
        # 撤回叶子 r2：其区间恢复为"父版本 r1"的正文（r1 当前仍生效）
        withdraw("W-1", 2)
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["W-1"]["body"], "r1")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["W-1"]["revision"], 1)
        # 但 r1 "已有子更正"（r2 行仍保留在历史中）-> 仍不可撤回
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 1)
        self.assertEqual(ctx.exception.code, "version_has_children")
        history = service.list_history("W-1")
        self.assertEqual(
            {v["revision"]: v["withdrawn_at"] is not None for v in history["items"]},
            {0: False, 1: False, 2: True})
        # r2 撤回后其区间回归 r1，r1 可在该恢复区间再次更正
        correct("W-1", 1, t3, t4, "r3")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["W-1"]["body"], "r3")

    def test_parent_still_blocked_by_withdrawn_child_row(self):
        # 撤回只追加事件不删行：子更正即使已撤回，仍是"已有子更正"，
        # 父版本不能撤回；被撤回叶子的区间恢复为该父版本正文。
        correct("W-1", 0, t2, t4, "r1")
        correct("W-1", 1, t3, t4, "r2")
        withdraw("W-1", 2)
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 1)
        self.assertEqual(ctx.exception.code, "version_has_children")

    def test_re_correct_parent_on_restored_interval(self):
        correct("W-1", 0, t2, t3, "错误的三月版")
        withdraw("W-1", 1)
        # 撤回后父版本 r0 在恢复区间可再次更正
        correct("W-1", 0, t2, t3, "正确的三月版")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=45)),
                                    future)["W-1"]["body"], "正确的三月版")
        history = service.list_history("W-1")
        self.assertEqual([v["revision"] for v in history["items"]], [0, 1, 2])
        self.assertIsNone(history["items"][2]["withdrawn_at"])
        self.assertIsNotNone(history["items"][1]["withdrawn_at"])

    def test_cannot_base_correction_on_withdrawn_version(self):
        correct("W-1", 0, t2, t4, "r1")
        withdraw("W-1", 1)
        with self.assertRaises(DomainError) as ctx:
            correct("W-1", 1, t2, t3, "以撤回版本为基版")
        self.assertEqual(ctx.exception.code, "base_version_withdrawn")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_withdraw_missing_procedure_and_revision(self):
        with self.assertRaises(DomainError) as ctx:
            withdraw("NOPE", 0)
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 7)
        self.assertEqual(ctx.exception.code, "revision_not_found")
        self.assertEqual(ctx.exception.http_status, 404)

    def test_batch_atomic_rollback(self):
        correct("W-1", 0, t2, t3, "r1")
        correct("W-1", 0, t3, t4, "r2")
        # 第一条可撤（叶子 r1），第二条撤首版 -> 整批回滚，r1 不被撤回
        with self.assertRaises(DomainError) as ctx:
            service.withdraw_corrections([witem("W-1", 1), witem("W-1", 0)])
        self.assertEqual(ctx.exception.code,
                         "first_revision_not_withdrawable")
        self.assertEqual(ctx.exception.details["index"], 1)
        self.assertIsNone(
            service.list_history("W-1")["items"][1]["withdrawn_at"])
        self.assertEqual(numbers_at(t2, future)["W-1"]["body"], "r1")

    def test_batch_leaf_and_parent_parent_rejected(self):
        correct("W-1", 0, t2, t4, "r1")
        correct("W-1", 1, t3, t4, "r2")
        # 同一批中撤 r2（合法）后再撤 r1：r1 仍"已有子更正"（r2 行不删除）
        # -> version_has_children，整批回滚，r2 也不撤回。
        with self.assertRaises(DomainError) as ctx:
            service.withdraw_corrections([witem("W-1", 2), witem("W-1", 1)])
        self.assertEqual(ctx.exception.code, "version_has_children")
        self.assertEqual(ctx.exception.details["index"], 1)
        self.assertIsNone(
            service.list_history("W-1")["items"][2]["withdrawn_at"])

    def test_withdrawal_then_correction_no_double_body(self):
        # 并发确定性：撤回事务先提交，随后的更正在恢复区间内成功；
        # 任何知悉时刻下 t2..t3 至多一份当前正文。
        correct("W-1", 0, t2, t3, "旧更正")
        wd = withdraw("W-1", 1)["withdrawn_at"]
        res = correct("W-1", 0, t2, t3, "新更正")
        wd_dt = datetime.strptime(wd, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        # 撤回瞬间（含）：旧更正仍生效
        self.assertEqual(numbers_at(canon(t2), canon(wd))["W-1"]["revision"], 1)
        # 撤回之后 1 微秒、新更正之前：恢复父版本
        self.assertEqual(numbers_at(canon(t2),
                                    iso_us(wd_dt + timedelta(microseconds=1))
                                    )["W-1"]["revision"], 0)
        # 新更正后：新更正（r2），不存在两份正文
        self.assertEqual(numbers_at(canon(t2), future)["W-1"]["revision"], 2)
        self.assertEqual(numbers_at(canon(t2), future)["W-1"]["body"], "新更正")

    def test_diff_excludes_withdrawn_for_later_known_time(self):
        published = correct("W-1", 0, t2, t3, "三月版")["published_at"]
        withdrawn_at = withdraw("W-1", 1)["withdrawn_at"]
        p_dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        w_dt = datetime.strptime(withdrawn_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        # 两侧都在撤回之后：全年都是 r0，无变化
        res = service.compare_interval(
            "W-1", iso_us(w_dt + timedelta(microseconds=1)),
            future, t1, t5)
        self.assertEqual(res["count"], 0)
        # 前侧在发布之后撤回之前（r1 可见），后侧在撤回之后（恢复 r0）
        res = service.compare_interval(
            "W-1", iso_us(p_dt + timedelta(microseconds=1)),
            iso_us(w_dt + timedelta(microseconds=1)), t1, t5)
        self.assertEqual(res["count"], 1)
        seg = res["segments"][0]
        self.assertEqual((seg["start"], seg["end"]), (canon(t2), canon(t3)))
        self.assertEqual(seg["before"]["revision"], 1)
        self.assertEqual(seg["after"]["revision"], 0)
        self.assertEqual(seg["after"]["body"], "初版")


class CompareTests(unittest.TestCase):
    """两知悉时刻作业区间对比。"""

    def setUp(self):
        reset_db()
        release("D-1", body="初版")

    def test_before_release_whole_window_null_to_versions(self):
        # 前侧知悉时刻早于任何发布：三段（r1 更正段把两侧同为 null->r0 的段隔开）
        correct("D-1", 0, t2, t3, "三月版")
        res = service.compare_interval("D-1", t0, future, t1, t5)
        self.assertEqual(res["count"], 3)
        segs = [(s["start"], s["end"],
                 s["before"], s["after"]["revision"]) for s in res["segments"]]
        self.assertEqual([s[2] for s in segs], [None, None, None])
        self.assertEqual([s[3] for s in segs], [0, 1, 0])
        self.assertEqual([(s[0], s[1]) for s in segs],
                         [(canon(t1), canon(t2)),
                          (canon(t2), canon(t3)),
                          (canon(t3), canon(t5))])
        self.assertEqual(res["segments"][1]["after"]["body"], "三月版")

    def test_correction_between_known_times_single_segment(self):
        published = correct("D-1", 0, t2, t3, "三月版")["published_at"]
        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        before = iso_us(dt - timedelta(microseconds=1))
        res = service.compare_interval("D-1", before, future, t1, t5)
        self.assertEqual(res["count"], 1)
        seg = res["segments"][0]
        self.assertEqual((seg["start"], seg["end"]), (canon(t2), canon(t3)))
        self.assertEqual(seg["before"]["revision"], 0)
        self.assertEqual(seg["before"]["body"], "初版")
        self.assertEqual(seg["after"]["revision"], 1)
        self.assertEqual(seg["after"]["body"], "三月版")

    def test_visibility_boundary_published_at_inclusive(self):
        # 前侧取更正发布时刻本身（published_at <= known 含等号）-> 已可见，无差异
        published = correct("D-1", 0, t2, t3, "三月版")["published_at"]
        res = service.compare_interval("D-1", published, future, t1, t5)
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["segments"], [])

    def test_same_body_new_revision_still_a_change(self):
        correct("D-1", 0, t2, t3, "初版")   # 正文与 r0 完全相同
        res = service.compare_interval("D-1", t0, future, t1, t5)
        changed = [s for s in res["segments"]
                   if s["before"] is None and s["after"]["revision"] == 1]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["after"]["body"], "初版")

    def test_partial_work_window_clipped(self):
        correct("D-1", 0, t2, t3, "三月版")
        ws = iso(_T + timedelta(days=15))
        we = iso(_T + timedelta(days=45))
        res = service.compare_interval("D-1", t0, future, ws, we)
        self.assertEqual(res["count"], 2)
        self.assertEqual([(s["start"], s["end"]) for s in res["segments"]],
                         [(canon(ws), canon(t2)), (canon(t2), canon(we))])

    def test_chained_corrections_separate_segments(self):
        correct("D-1", 0, t2, t4, "r1")
        correct("D-1", 1, t3, t4, "r2")
        res = service.compare_interval("D-1", t0, future, t1, t5)
        pairs = [(s["start"], s["after"]["revision"]) for s in res["segments"]]
        self.assertEqual(pairs,
                         [(canon(t1), 0), (canon(t2), 1), (canon(t3), 2),
                          (canon(t4), 0)])
        # 前侧全空；两段 after=r0 被不同版本隔开，不得跨段合并
        self.assertTrue(all(s["before"] is None for s in res["segments"]))

    def test_no_change_returns_empty(self):
        correct("D-1", 0, t2, t3, "三月版")
        # 两个知悉时刻都在更正发布之后（须严格先后；future = T+400d）
        after1 = iso(_T + timedelta(days=300))
        res = service.compare_interval("D-1", after1, future, t1, t5)
        self.assertEqual(res["count"], 0)
        # 作业区间完全在任何适用区间之外：两侧均无版本，也不返回
        res = service.compare_interval(
            "D-1", t0, future, iso(_T + timedelta(days=800)),
            iso(_T + timedelta(days=900)))
        self.assertEqual(res["count"], 0)

    def test_offset_timezone_normalized(self):
        correct("D-1", 0, t2, t3, "三月版")
        res = service.compare_interval(
            "D-1",
            "2025-12-31T16:00:00-08:00",       # == 2026-01-01T00:00:00Z（早于发布）
            future,
            "2026-01-31T08:00:00+08:00",       # == 2026-01-31T00:00:00Z == t2（含）
            "2026-03-02T08:00:00+08:00")       # == 2026-03-02T00:00:00Z == t3（不含）
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["segments"][0]["after"]["revision"], 1)

    def test_error_unknown_number(self):
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval("NOPE", t0, future, t1, t2)
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(ctx.exception.http_status, 404)

    def test_error_naive_time_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval(
                "D-1", "2026-01-01T00:00:00", future, t1, t2)
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "known_before")
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval(
                "D-1", t0, future, "2026-01-01T00:00:00", t2)
        self.assertEqual(ctx.exception.details["field"], "work_start")

    def test_error_known_order_reversed_or_equal(self):
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval("D-1", future, t0, t1, t2)
        self.assertEqual(ctx.exception.code, "invalid_known_order")
        self.assertEqual(ctx.exception.http_status, 422)
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval("D-1", future, future, t1, t2)
        self.assertEqual(ctx.exception.code, "invalid_known_order")

    def test_error_empty_work_interval(self):
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval("D-1", t0, future, t2, t2)
        self.assertEqual(ctx.exception.code, "invalid_interval")
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval("D-1", t0, future, t3, t2)
        self.assertEqual(ctx.exception.code, "invalid_interval")

    def test_read_only_on_error(self):
        # 非法请求前后历史完全一致
        before = service.list_history("D-1")
        with self.assertRaises(DomainError):
            service.compare_interval("D-1", future, t0, t1, t2)
        self.assertEqual(service.list_history("D-1"), before)

    def test_merge_adjacent_helper(self):
        segs = service._merge_adjacent([
            (t1, t2, None, 0), (t2, t3, None, 0), (t3, t4, 0, 0),
            (t4, t5, 0, 1)])
        self.assertEqual(segs, [(t1, t3, None, 0), (t3, t4, 0, 0),
                                (t4, t5, 0, 1)])


class PackageTests(unittest.TestCase):
    """作业规程包：固定、原样取回、复核及各类整体拒绝。"""

    def setUp(self):
        reset_db()
        release("P-1", body="P1初版")
        release("P-2", body="P2初版")

    def _now_after_writes(self) -> str:
        # 服务端发布时钟单调递增；略作停顿确保后续 now 严格晚于发布时刻
        import time as _time
        _time.sleep(0.002)
        return now_canonical()

    def _package(self, numbers, work=t2, known=None):
        return service.create_package(work, known or self._now_after_writes(),
                                      numbers)

    def test_create_returns_id_versions_bodies_and_times(self):
        known = self._now_after_writes()
        res = self._package(["P-2", "P-1"], known=known)
        self.assertRegex(res["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(res["count"], 2)
        # 保留调用方传入的编号顺序（复核按相同位置逐项核对）
        self.assertEqual([i["number"] for i in res["items"]], ["P-2", "P-1"])
        self.assertEqual([i["revision"] for i in res["items"]], [0, 0])
        self.assertEqual(res["items"][0]["body"], "P2初版")
        self.assertEqual(res["items"][1]["body"], "P1初版")
        self.assertEqual(res["known_as_of"], canon(known))
        self.assertEqual(res["work_time"], canon(t2))
        self.assertTrue(res["created_at"].endswith("Z"))

    def test_get_returns_same_snapshot(self):
        res = self._package(["P-1"])
        got = service.get_package(res["id"])
        self.assertEqual(got, res)

    def test_missing_number_rejected_entirely(self):
        with self.assertRaises(DomainError) as ctx:
            self._package(["P-1", "NOPE"])
        self.assertEqual(ctx.exception.code, "procedure_not_found")
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertEqual(ctx.exception.details,
                         {"number": "NOPE", "index": 1})
        # 不留包
        import sqlite3
        conn = sqlite3.connect(settings.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM packages")
                             .fetchone()[0], 0)
        finally:
            conn.close()

    def test_duplicate_number_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self._package(["P-1", "P-1"])
        self.assertEqual(ctx.exception.code, "duplicate_package_number")
        self.assertEqual(ctx.exception.http_status, 422)
        self.assertEqual(ctx.exception.details["index"], 1)

    def test_empty_number_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self._package(["   "])
        self.assertEqual(ctx.exception.code, "invalid_number")

    def test_no_applicable_version_at_work_time(self):
        # 作业时刻在任何适用区间之外（t5 为终点，不含）
        with self.assertRaises(DomainError) as ctx:
            self._package(["P-1"], work=t5)
        self.assertEqual(ctx.exception.code, "no_applicable_version")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["number"], "P-1")

    def test_no_applicable_version_when_not_yet_published(self):
        # 知悉时刻早于发布 -> 无已发布版本
        with self.assertRaises(DomainError) as ctx:
            self._package(["P-1"], known=t0)
        self.assertEqual(ctx.exception.code, "no_applicable_version")

    def test_known_as_of_in_future_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self._package(["P-1"], known=future)
        self.assertEqual(ctx.exception.code, "known_as_of_in_future")
        self.assertEqual(ctx.exception.http_status, 422)

    def test_naive_time_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            service.create_package("2026-01-01T00:00:00",
                                   self._now_after_writes(), ["P-1"])
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "work_time")
        with self.assertRaises(DomainError) as ctx:
            service.create_package(t2, "2026-01-01T00:00:00", ["P-1"])
        self.assertEqual(ctx.exception.details["field"], "known_as_of")

    def test_package_pins_applicable_correction_at_fix_time(self):
        correct("P-1", 0, t2, t3, "P1三月版")
        res = self._package(["P-1"], work=t2)
        item = res["items"][0]
        self.assertEqual(item["revision"], 1)
        self.assertEqual(item["body"], "P1三月版")
        self.assertEqual(item["supersedes_revision"], 0)

    def test_package_immutable_after_later_correction(self):
        res = self._package(["P-1"], work=t2)
        # 固定之后再更正同一作业时刻
        correct("P-1", 0, t2, t3, "P1后来的三月版")
        got = service.get_package(res["id"])
        self.assertEqual(got["items"][0]["revision"], 0)
        self.assertEqual(got["items"][0]["body"], "P1初版")
        self.assertEqual(got["known_as_of"], res["known_as_of"])

    def test_package_immutable_after_withdrawal(self):
        correct("P-1", 0, t2, t3, "误发三月版")
        res = self._package(["P-1"], work=t2)
        withdraw("P-1", 1)
        # 撤回不影响包快照
        got = service.get_package(res["id"])
        self.assertEqual(got["items"][0]["revision"], 1)
        self.assertEqual(got["items"][0]["body"], "误发三月版")

    def test_review_all_match(self):
        res = self._package(["P-1", "P-2"], work=t2)
        review = service.review_package(res["id"], self._now_after_writes())
        self.assertTrue(review["all_match"])
        self.assertEqual(review["count"], 2)
        for it in review["items"]:
            self.assertFalse(it["changed"])
            self.assertEqual(it["pinned"]["revision"],
                             it["current"]["revision"])
            self.assertEqual(it["pinned"]["body"], it["current"]["body"])

    def test_review_detects_later_correction(self):
        res = self._package(["P-1"], work=t2)
        correct("P-1", 0, t2, t3, "新三月版")
        review = service.review_package(res["id"], self._now_after_writes())
        self.assertFalse(review["all_match"])
        it = review["items"][0]
        self.assertTrue(it["changed"])
        self.assertEqual(it["pinned"]["revision"], 0)
        self.assertEqual(it["current"]["revision"], 1)
        self.assertEqual(it["current"]["body"], "新三月版")

    def test_review_detects_withdrawal_restoring_parent(self):
        correct("P-1", 0, t2, t3, "三月版")
        res = self._package(["P-1"], work=t2)
        self.assertEqual(res["items"][0]["revision"], 1)
        withdraw("P-1", 1)
        review = service.review_package(res["id"], self._now_after_writes())
        it = review["items"][0]
        self.assertTrue(it["changed"])
        self.assertEqual(it["pinned"]["revision"], 1)
        self.assertEqual(it["current"]["revision"], 0)
        self.assertEqual(it["current"]["body"], "P1初版")
        self.assertFalse(review["all_match"])

    def test_review_at_package_known_as_of_matches(self):
        # 以包内知悉时刻本身复核（不早于，含等号）-> 完全一致
        correct("P-1", 0, t2, t3, "三月版")
        known = self._now_after_writes()
        res = self._package(["P-1"], work=t2, known=known)
        review = service.review_package(res["id"], res["known_as_of"])
        self.assertTrue(review["all_match"])
        self.assertEqual(review["items"][0]["current"]["revision"],
                         review["items"][0]["pinned"]["revision"])

    def test_review_known_earlier_than_package_rejected(self):
        res = self._package(["P-1"], work=t2)
        with self.assertRaises(DomainError) as ctx:
            service.review_package(res["id"], t0)
        self.assertEqual(ctx.exception.code, "review_known_too_early")
        self.assertEqual(ctx.exception.http_status, 422)

    def test_review_known_in_future_rejected(self):
        res = self._package(["P-1"], work=t2)
        with self.assertRaises(DomainError) as ctx:
            service.review_package(res["id"], future)
        self.assertEqual(ctx.exception.code, "known_as_of_in_future")

    def test_review_unknown_package(self):
        with self.assertRaises(DomainError) as ctx:
            service.review_package("deadbeef", self._now_after_writes())
        self.assertEqual(ctx.exception.code, "package_not_found")
        self.assertEqual(ctx.exception.http_status, 404)
        with self.assertRaises(DomainError) as ctx:
            service.get_package("deadbeef")
        self.assertEqual(ctx.exception.code, "package_not_found")

    def test_review_snapshot_consistent_under_concurrent_write(self):
        # 复核在单一只读快照内完成：复核快照建立之后并发提交的写入，对该次
        # 复核完全不可见（不会拼出"半新半旧"的结果）；快照之外的新读取才能
        # 看到并发更正。用真实线程在读者持有快照期间由写路径提交。
        import threading
        import time as _time
        from app.db import read_snapshot
        from app import service as svc

        correct("P-1", 0, t2, t3, "三月版")
        known = self._now_after_writes()
        pkg = self._package(["P-1", "P-2"], work=t2, known=known)
        seen: dict = {}

        def reader() -> None:
            with read_snapshot() as conn:
                # 进入时快照已固定；让出窗口给写线程提交并发更正
                _time.sleep(0.3)
                best, _ = svc._resolve_effective(
                    conn, svc._fetch_procedure(conn, "P-2"), t2, known)
                seen["revision"] = best["revision"]

        th = threading.Thread(target=reader)
        th.start()
        _time.sleep(0.1)
        try:
            correct("P-2", 0, t2, t3, "P2并发三月版")
        finally:
            th.join()
        # 同一快照内解析：看不到快照之后的写入 -> 仍是 r0
        self.assertEqual(seen["revision"], 0)
        # 快照之外的新连接则能看到新更正 r1
        with read_snapshot() as conn:
            best, _ = svc._resolve_effective(
                conn, svc._fetch_procedure(conn, "P-2"), t2,
                self._now_after_writes())
            self.assertEqual(best["revision"], 1)

    def test_review_function_atomic_snapshot_under_concurrent_write(self):
        # 直接调用 review_package：复核快照建立后并发提交的更正不可见；快照
        # 之外的复核才看到。验证整个复核不与并发写入拼接（无混合结果）。
        import threading
        import time as _time
        from app.db import read_snapshot
        from app import service as svc

        known = self._now_after_writes()
        pkg_id = self._package(["P-1", "P-2"], work=t2, known=known)["id"]
        seen: dict = {}

        def reviewer() -> None:
            with read_snapshot() as conn:
                _time.sleep(0.3)     # 持有快照，窗口内写线程提交
                # 复用复核的逐项解析逻辑，但全部跑在该快照上
                pkg, rows = svc._package_rows(conn, pkg_id)
                revs = {}
                for row in rows:
                    proc = svc._fetch_procedure_by_id(conn, row["procedure_id"])
                    best, _ = svc._resolve_effective(
                        conn, proc, pkg["work_time"],
                        self._now_after_writes())
                    revs[row["number"]] = best["revision"]
                seen["revs"] = revs

        th = threading.Thread(target=reviewer)
        th.start()
        _time.sleep(0.1)
        correct("P-2", 0, t2, t3, "P2并发三月版")
        th.join()
        # 快照在更正提交前固定 -> P-2 仍解析为 r0，未与并发写入拼接
        self.assertEqual(seen["revs"], {"P-1": 0, "P-2": 0})
        # 快照之外的正式复核随后可见 r1 并判该项变化
        review = service.review_package(pkg_id, self._now_after_writes())
        by_no = {i["number"]: i for i in review["items"]}
        self.assertTrue(by_no["P-2"]["changed"])
        self.assertEqual(by_no["P-2"]["current"]["revision"], 1)
        self.assertFalse(by_no["P-1"]["changed"])


class PrerequisiteTests(unittest.TestCase):
    """前置规程：发布/更正携带前置编号列表，按版本生效，递归解析闭包。"""

    def setUp(self):
        reset_db()

    def _closure(self, number: str, work: str = t2, known: str = future):
        return service.resolve_closure(number, work, known)

    def _closure_numbers(self, number: str, work: str = t2,
                         known: str = future) -> list[str]:
        return [i["number"] for i in self._closure(number, work, known)["items"]]

    # ---- 写入：携带 / 省略 / 继承 / 清空 --------------------------------

    def test_release_with_prerequisites_returned_and_stored(self):
        res = release("PRE-W-1", prerequisites=["PRE-A", "PRE-B"])
        self.assertEqual(res["items"][0]["prerequisites"], ["PRE-A", "PRE-B"])
        history = service.list_history("PRE-W-1")
        self.assertEqual(history["items"][0]["prerequisites"],
                         ["PRE-A", "PRE-B"])

    def test_release_omitting_prerequisites_defaults_empty(self):
        res = release("PRE-W-2")
        self.assertEqual(res["items"][0]["prerequisites"], [])
        self.assertEqual(
            service.list_history("PRE-W-2")["items"][0]["prerequisites"], [])

    def test_correction_inherits_base_list_when_omitted(self):
        release("PRE-W-3", prerequisites=["PRE-A", "PRE-B"])
        res = correct("PRE-W-3", 0, t2, t3, "三月版")
        self.assertEqual(res["items"][0]["prerequisites"], ["PRE-A", "PRE-B"])
        history = {v["revision"]: v for v in
                   service.list_history("PRE-W-3")["items"]}
        self.assertEqual(history[0]["prerequisites"], ["PRE-A", "PRE-B"])
        self.assertEqual(history[1]["prerequisites"], ["PRE-A", "PRE-B"])

    def test_correction_explicit_empty_list_clears(self):
        release("PRE-W-4", prerequisites=["PRE-A"])
        res = correct("PRE-W-4", 0, t2, t3, "三月版", prerequisites=[])
        self.assertEqual(res["items"][0]["prerequisites"], [])
        history = {v["revision"]: v for v in
                   service.list_history("PRE-W-4")["items"]}
        self.assertEqual(history[0]["prerequisites"], ["PRE-A"])
        self.assertEqual(history[1]["prerequisites"], [])

    def test_correction_explicit_list_replaces(self):
        release("PRE-W-5", prerequisites=["PRE-A"])
        res = correct("PRE-W-5", 0, t2, t3, "三月版",
                      prerequisites=["PRE-B", "PRE-C"])
        self.assertEqual(res["items"][0]["prerequisites"],
                         ["PRE-B", "PRE-C"])
        # 链式：r2 省略 -> 继承 r1 的显式列表，而非 r0 的
        res2 = correct("PRE-W-5", 1, t2, t3, "三月再更正")
        self.assertEqual(res2["items"][0]["prerequisites"],
                         ["PRE-B", "PRE-C"])

    def test_old_style_items_without_field_still_work(self):
        # 旧调用方构造的对象没有 prerequisites 属性：按省略处理
        release("PRE-W-6")
        res = correct("PRE-W-6", 0, t2, t3, "三月版")
        self.assertEqual(res["items"][0]["prerequisites"], [])

    # ---- 写入校验：空白 / 重复 / 批量原子 --------------------------------

    def test_blank_prerequisite_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            release("PRE-BAD-1", prerequisites=["PRE-A", "  "])
        self.assertEqual(ctx.exception.code, "invalid_number")
        self.assertEqual(ctx.exception.http_status, 422)
        self.assertEqual(ctx.exception.details["field"], "prerequisites")
        self.assertEqual(ctx.exception.details["position"], 1)
        with self.assertRaises(DomainError) as ctx:
            release("PRE-BAD-2", prerequisites=[""])
        self.assertEqual(ctx.exception.code, "invalid_number")

    def test_duplicate_prerequisite_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            release("PRE-BAD-3", prerequisites=["PRE-A", "PRE-B", "PRE-A"])
        self.assertEqual(ctx.exception.code, "duplicate_prerequisite_number")
        self.assertEqual(ctx.exception.http_status, 422)
        self.assertEqual(ctx.exception.details["number"], "PRE-A")
        self.assertEqual(ctx.exception.details["position"], 2)
        # 更正中同样校验
        release("PRE-BAD-4")
        with self.assertRaises(DomainError) as ctx:
            correct("PRE-BAD-4", 0, t2, t3, "x",
                    prerequisites=["PRE-A", "PRE-A"])
        self.assertEqual(ctx.exception.code, "duplicate_prerequisite_number")

    def test_invalid_prerequisite_batch_atomic(self):
        # 批量发布：第二条前置重复 -> 整批回滚，第一条也不入库
        with self.assertRaises(DomainError) as ctx:
            service.publish_releases([
                item("PRE-BAT-1", prerequisites=["PRE-A"]),
                item("PRE-BAT-2", prerequisites=["PRE-X", "PRE-X"]),
            ])
        self.assertEqual(ctx.exception.code, "duplicate_prerequisite_number")
        self.assertEqual(ctx.exception.details["index"], 1)
        with self.assertRaises(DomainError):
            service.list_history("PRE-BAT-1")
        # 批量更正同理：第二条非法 -> 第一条更正也不入库
        release("PRE-BAT-3")
        with self.assertRaises(DomainError):
            service.submit_corrections([
                item("PRE-BAT-3", "ok", t2, t3, base=0),
                item("PRE-BAT-3", "bad", t3, t4, base=0,
                     prerequisites=[" "]),
            ])
        self.assertEqual(service.list_history("PRE-BAT-3")["count"], 1)

    def test_reference_may_be_published_later(self):
        # 写入时不校验存在性：引用尚未发布的编号合法
        release("PRE-LATE-ROOT", prerequisites=["PRE-LATE"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-LATE-ROOT")
        self.assertEqual(ctx.exception.code, "prerequisite_not_found")
        # 前置发布后解析成功
        release("PRE-LATE", body="前置正文")
        items = self._closure("PRE-LATE-ROOT")["items"]
        self.assertEqual([i["number"] for i in items],
                         ["PRE-LATE", "PRE-LATE-ROOT"])
        self.assertEqual(items[0]["body"], "前置正文")

    # ---- 递归解析：顺序 / 去重 / 版本选取 --------------------------------

    def test_closure_prerequisites_first_root_last_dedup(self):
        # 菱形：ROOT -> [B, C]，B -> [D]，C -> [D]
        release("PRE-D", body="D正文")
        release("PRE-B", body="B正文", prerequisites=["PRE-D"])
        release("PRE-C", body="C正文", prerequisites=["PRE-D"])
        release("PRE-ROOT", body="根正文",
                prerequisites=["PRE-B", "PRE-C", "PRE-D"])
        res = self._closure("PRE-ROOT")
        self.assertEqual(res["root"], "PRE-ROOT")
        self.assertEqual(res["count"], 4)
        # 前置优先、根最后；D 被三处引用只出现一次
        self.assertEqual([i["number"] for i in res["items"]],
                         ["PRE-D", "PRE-B", "PRE-C", "PRE-ROOT"])
        self.assertEqual(res["items"][0]["body"], "D正文")
        self.assertEqual(res["items"][-1]["body"], "根正文")
        # 清单项含编号、版本、正文与各自携带的前置列表
        self.assertEqual(res["items"][-1]["revision"], 0)
        self.assertEqual(res["items"][-1]["prerequisites"],
                         ["PRE-B", "PRE-C", "PRE-D"])
        self.assertEqual(res["items"][0]["prerequisites"], [])
        self.assertEqual(res["work_time"], canon(t2))
        self.assertEqual(res["known_as_of"], canon(future))

    def test_closure_deep_chain(self):
        release("PRE-L3", body="L3")
        release("PRE-L2", body="L2", prerequisites=["PRE-L3"])
        release("PRE-L1", body="L1", prerequisites=["PRE-L2"])
        release("PRE-L0", body="L0", prerequisites=["PRE-L1"])
        self.assertEqual(self._closure_numbers("PRE-L0"),
                         ["PRE-L3", "PRE-L2", "PRE-L1", "PRE-L0"])

    def test_closure_uses_effective_version_at_work_time(self):
        # 前置规程自身在作业时刻适用的是更正版本
        release("PRE-E", body="前置初版")
        correct("PRE-E", 0, t2, t3, "前置三月版")
        release("PRE-R", body="根", prerequisites=["PRE-E"])
        # 作业时刻在 3 月（t2..t3）-> 前置取 r1
        items = self._closure("PRE-R", work=t2)["items"]
        self.assertEqual(items[0]["revision"], 1)
        self.assertEqual(items[0]["body"], "前置三月版")
        # 作业时刻在 4 月（t3..t4）-> 前置回到 r0
        items = self._closure("PRE-R", work=t3)["items"]
        self.assertEqual(items[0]["revision"], 0)
        self.assertEqual(items[0]["body"], "前置初版")

    def test_closure_references_follow_version_and_known_time(self):
        # 引用随版本的适用区间与发布时间变化：根 r0 带 [PRE-1]，
        # 3 月更正 r1 改为 []（清空）
        release("PRE-1", body="前置")
        release("PRE-R2", body="根初版", prerequisites=["PRE-1"])
        published = correct("PRE-R2", 0, t2, t3, "根三月版",
                            prerequisites=[])["published_at"]
        # 3 月作业：根的适用版本是 r1（已清空前置）-> 只有根
        self.assertEqual(self._closure_numbers("PRE-R2", work=t2),
                         ["PRE-R2"])
        # 2 月作业：根的适用版本是 r0 -> 带前置
        self.assertEqual(
            self._closure_numbers("PRE-R2",
                                  work=iso(_T + timedelta(days=15))),
            ["PRE-1", "PRE-R2"])
        # 知悉时刻早于更正发布：3 月作业仍按 r0 -> 带前置（历史不变）
        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        before = iso_us(dt - timedelta(microseconds=1))
        self.assertEqual(self._closure_numbers("PRE-R2", work=t2,
                                               known=before),
                         ["PRE-1", "PRE-R2"])

    def test_closure_references_follow_withdrawal(self):
        # 撤回更正后，其前置列表随之失效，恢复父版本携带的列表
        release("PRE-1", body="前置")
        release("PRE-R3", body="根初版", prerequisites=["PRE-1"])
        correct("PRE-R3", 0, t2, t3, "根三月版", prerequisites=[])
        self.assertEqual(self._closure_numbers("PRE-R3", work=t2),
                         ["PRE-R3"])
        withdraw("PRE-R3", 1)
        self.assertEqual(self._closure_numbers("PRE-R3", work=t2),
                         ["PRE-1", "PRE-R3"])

    def test_closure_known_as_of_defaults_now(self):
        release("PRE-N1", body="前置")
        release("PRE-N2", body="根", prerequisites=["PRE-N1"])
        res = service.resolve_closure("PRE-N2", t2, None)
        self.assertEqual([i["number"] for i in res["items"]],
                         ["PRE-N1", "PRE-N2"])

    # ---- 缺口与环：整体失败 ----------------------------------------------

    def test_root_not_found(self):
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-NOPE")
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertEqual(ctx.exception.details["number"], "PRE-NOPE")

    def test_missing_reference_reports_gap_path(self):
        release("PRE-G1", prerequisites=["PRE-G2", "PRE-G-MISSING"])
        release("PRE-G2", prerequisites=["PRE-G3-MISSING"])
        # 先展开 PRE-G2 -> 缺口在 PRE-G3-MISSING，引用链带到缺口
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-G1")
        self.assertEqual(ctx.exception.code, "prerequisite_not_found")
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertEqual(ctx.exception.details["number"], "PRE-G3-MISSING")
        self.assertEqual(ctx.exception.details["path"],
                         ["PRE-G1", "PRE-G2", "PRE-G3-MISSING"])

    def test_no_applicable_version_root_and_reference(self):
        # 根在作业时刻无适用正文
        release("PRE-X1", start=t2, end=t3)
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-X1", work=t4)
        self.assertEqual(ctx.exception.code, "no_applicable_version")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["number"], "PRE-X1")
        self.assertEqual(ctx.exception.details["path"], ["PRE-X1"])
        # 前置在作业时刻无适用正文（前置区间只覆盖 3 月，作业在 4 月）
        release("PRE-X2", start=t2, end=t3, body="前置仅 3 月")
        release("PRE-X3", prerequisites=["PRE-X2"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-X3", work=t4)
        self.assertEqual(ctx.exception.code, "no_applicable_version")
        self.assertEqual(ctx.exception.details["number"], "PRE-X2")
        self.assertEqual(ctx.exception.details["path"],
                         ["PRE-X3", "PRE-X2"])
        # 知悉时刻早于前置发布：同样视为当时无适用正文
        release("PRE-X4")
        release("PRE-X5", prerequisites=["PRE-X4"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-X5", known=t0)
        self.assertEqual(ctx.exception.code, "no_applicable_version")
        self.assertEqual(ctx.exception.details["number"], "PRE-X5")

    def test_cycle_returns_cycle_path(self):
        release("PRE-CY-A", prerequisites=["PRE-CY-B"])
        release("PRE-CY-B", prerequisites=["PRE-CY-C"])
        release("PRE-CY-C", prerequisites=["PRE-CY-A"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-CY-A")
        self.assertEqual(ctx.exception.code, "prerequisite_cycle")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["cycle"],
                         ["PRE-CY-A", "PRE-CY-B", "PRE-CY-C", "PRE-CY-A"])
        # 从环外引用环：环路径从入环处开始
        release("PRE-CY-ROOT", prerequisites=["PRE-CY-B"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-CY-ROOT")
        self.assertEqual(ctx.exception.details["cycle"],
                         ["PRE-CY-B", "PRE-CY-C", "PRE-CY-A", "PRE-CY-B"])

    def test_self_cycle(self):
        release("PRE-CY-S", prerequisites=["PRE-CY-S"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-CY-S")
        self.assertEqual(ctx.exception.code, "prerequisite_cycle")
        self.assertEqual(ctx.exception.details["cycle"],
                         ["PRE-CY-S", "PRE-CY-S"])

    def test_invalid_time_rejected(self):
        release("PRE-T1")
        with self.assertRaises(DomainError) as ctx:
            service.resolve_closure("PRE-T1", "2026-01-01T00:00:00", future)
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "work_time")
        with self.assertRaises(DomainError) as ctx:
            service.resolve_closure("PRE-T1", t2, "2026-01-01T00:00:00")
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "known_as_of")


class ApiTests(unittest.TestCase):
    """FastAPI 层：仅在依赖可用时运行。"""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
            from app.main import app
        except Exception:
            raise unittest.SkipTest("fastapi/httpx 未安装，跳过 API 测试")
        cls.client = TestClient(app)

    def _n(self):
        # 编号全库唯一
        return f"API-{id(self)}-{self._seq}"

    def setUp(self):
        reset_db()
        self._seq = ApiTests._seq = getattr(ApiTests, "_seq", 0) + 1

    def test_full_flow_over_http(self):
        c = self.client
        n = self._n()
        r = c.post("/api/v1/releases", json={
            "number": n, "body": "A", "valid_start": t1, "valid_end": t5})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["items"][0]["revision"], 0)

        r = c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "B"})
        self.assertEqual(r.status_code, 201)

        r = c.get("/api/v1/effective", params={"work_time": t2})
        self.assertEqual(r.status_code, 200)
        found = [i for i in r.json()["items"] if i["number"] == n]
        self.assertEqual(found[0]["body"], "B")

        # 顺序提交且与已发布更正重叠 -> 409 过期基版
        r = c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": iso(_T + timedelta(days=40)),
            "valid_end": iso(_T + timedelta(days=50)), "body": "X"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "expired_base_version")

        # 同批冲突 -> 409 并发更正冲突
        r = c.post("/api/v1/corrections", json={"items": [
            {"number": n, "base_revision": 0,
             "valid_start": iso(_T + timedelta(days=120)),
             "valid_end": iso(_T + timedelta(days=140)), "body": "P"},
            {"number": n, "base_revision": 0,
             "valid_start": iso(_T + timedelta(days=130)),
             "valid_end": iso(_T + timedelta(days=150)), "body": "Q"},
        ]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"],
                         "concurrent_correction_conflict")

        # 重复编号 409
        r = c.post("/api/v1/releases", json={
            "number": n, "body": "A2", "valid_start": t1, "valid_end": t5})
        self.assertEqual(r.status_code, 409)

        # 历史
        r = c.get(f"/api/v1/procedures/{n}/history")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 2)
        r = c.get("/api/v1/procedures/NO-SUCH/history")
        self.assertEqual(r.status_code, 404)

    def test_extra_field_published_at_forbidden(self):
        r = self.client.post("/api/v1/releases", json={
            "number": self._n(), "body": "A",
            "valid_start": t1, "valid_end": t5,
            "published_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")

    def test_diff_over_http(self):
        c = self.client
        n = self._n()
        c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1, "valid_end": t5})
        c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "三月版"})

        # 前侧早于发布 -> 三段 null->r0/r1/r0
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_before": t0, "known_after": future,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 3)
        self.assertEqual([s["after"]["revision"] for s in body["segments"]],
                         [0, 1, 0])
        self.assertTrue(all(s["before"] is None
                            for s in body["segments"]))

        # 编号不存在 -> 404，不区分入参先后
        r = c.get("/api/v1/procedures/NO-SUCH/diff", params={
            "known_before": t0, "known_after": future,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")

        # 知悉顺序颠倒 -> 422 invalid_known_order
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_before": future, "known_after": t0,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_known_order")

        # 空作业区间 -> 422 invalid_interval
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_before": t0, "known_after": future,
            "work_start": t2, "work_end": t2})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_interval")

        # 无时区 -> 422 invalid_time
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_before": "2026-01-01T00:00:00", "known_after": future,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_time")

        # 缺少必填参数 -> 422 validation_error
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_before": t0, "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 422)

    def test_withdraw_over_http(self):
        c = self.client
        n = self._n()
        c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1, "valid_end": t5})
        c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "三月版"})

        # 撤回前：3 月 -> 三月版
        r = c.get("/api/v1/effective", params={"work_time": t2})
        self.assertEqual([i for i in r.json()["items"] if i["number"] == n][0]["body"],
                         "三月版")

        # 撤回 r1
        r = c.post("/api/v1/withdrawals", json={"number": n, "revision": 1})
        self.assertEqual(r.status_code, 201, r.text)
        payload = r.json()
        self.assertTrue(payload["withdrawn_at"].endswith("Z"))
        item = payload["items"][0]
        self.assertEqual(item["revision"], 1)
        self.assertEqual(item["supersedes_revision"], 0)
        self.assertEqual(item["withdrawn_at"], payload["withdrawn_at"])

        # 撤回后：3 月恢复初版
        r = c.get("/api/v1/effective", params={"work_time": t2})
        found = [i for i in r.json()["items"] if i["number"] == n][0]
        self.assertEqual(found["revision"], 0)
        self.assertEqual(found["body"], "初版")

        # 历史仍有 r1，且带 withdrawn_at
        r = c.get(f"/api/v1/procedures/{n}/history")
        revs = {v["revision"]: v for v in r.json()["items"]}
        self.assertEqual(len(revs), 2)
        self.assertIsNotNone(revs[1]["withdrawn_at"])

        # 重复撤回 -> 409 already_withdrawn
        r = c.post("/api/v1/withdrawals", json={"number": n, "revision": 1})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "already_withdrawn")

        # 首版 -> 409
        r = c.post("/api/v1/withdrawals", json={"number": n, "revision": 0})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"],
                         "first_revision_not_withdrawable")

        # 编号不存在 -> 404；版本号不存在 -> 404 revision_not_found
        r = c.post("/api/v1/withdrawals", json={"number": "NO-SUCH", "revision": 1})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")
        r = c.post("/api/v1/withdrawals", json={"number": n, "revision": 99})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "revision_not_found")

        # 额外字段（试图回填撤回时刻）-> 422 validation_error
        r = c.post("/api/v1/withdrawals", json={
            "number": n, "revision": 1,
            "withdrawn_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")

        # 撤回后父版本可在恢复区间再次更正
        r = c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "修正后的三月版"})
        self.assertEqual(r.status_code, 201, r.text)
        r = c.get("/api/v1/effective", params={"work_time": t2})
        found = [i for i in r.json()["items"] if i["number"] == n][0]
        self.assertEqual(found["body"], "修正后的三月版")

    def test_withdraw_children_and_batch_conflict_http(self):
        c = self.client
        n = self._n()
        c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1, "valid_end": t5})
        c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t4, "body": "r1"})
        c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 1,
            "valid_start": t3, "valid_end": t4, "body": "r2"})

        # 有子更正的 r1 不可撤回
        r = c.post("/api/v1/withdrawals", json={"number": n, "revision": 1})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "version_has_children")
        self.assertEqual(r.json()["error"]["details"]["child_revisions"], [2])

        # 以已撤回版本为基版更正 -> 409 base_version_withdrawn
        c.post("/api/v1/withdrawals", json={"number": n, "revision": 2})
        r = c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 2,
            "valid_start": t3, "valid_end": t4, "body": "x"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "base_version_withdrawn")

        # 批量：用一个全新规程，第二条非法（首版）-> 整批回滚，
        # 第一条（合法叶子更正）的撤回也不生效。
        n2 = n + "-B"
        c.post("/api/v1/releases", json={
            "number": n2, "body": "初版", "valid_start": t1, "valid_end": t5})
        c.post("/api/v1/corrections", json={
            "number": n2, "base_revision": 0,
            "valid_start": t2, "valid_end": t4, "body": "r1"})
        c.post("/api/v1/corrections", json={
            "number": n2, "base_revision": 1,
            "valid_start": t3, "valid_end": t4, "body": "r2"})
        r = c.post("/api/v1/withdrawals", json={"items": [
            {"number": n2, "revision": 2},
            {"number": n2, "revision": 0},
        ]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["details"]["index"], 1)
        hist = {v["revision"]: v for v in
                c.get(f"/api/v1/procedures/{n2}/history").json()["items"]}
        self.assertIsNone(hist[2]["withdrawn_at"])

    def test_health_and_empty_result(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")
        r = self.client.get("/api/v1/effective",
                            params={"work_time": "2001-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)
        self.assertEqual(r.json()["items"], [])

    def test_packages_over_http(self):
        c = self.client
        n1, n2 = self._n(), self._n() + "-2"
        c.post("/api/v1/releases", json={
            "number": n1, "body": "初版", "valid_start": t1,
            "valid_end": t5})
        # 知悉时刻取最后一次发布的时刻（保证两个规程均已发布且 <= 当前时刻）
        pub = c.post("/api/v1/releases", json={
            "number": n2, "body": "另一个", "valid_start": t1,
            "valid_end": t5}).json()["published_at"]
        # 以发布时刻为知悉时刻（必然 <= 服务端当前时刻，且已可见）
        body = {"work_time": t2, "known_as_of": pub,
                "numbers": [n2, n1]}
        r = c.post("/api/v1/packages", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        pkg = r.json()
        self.assertRegex(pkg["id"], r"^[0-9a-f]{32}$")
        # 保留调用方传入的编号顺序
        self.assertEqual([i["number"] for i in pkg["items"]], [n2, n1])
        self.assertEqual(pkg["items"][1]["body"], "初版")
        package_id = pkg["id"]

        # 原样取回
        r = c.get(f"/api/v1/packages/{package_id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), pkg)

        # 固定后更正：取回内容不变；复核显示变化
        c.post("/api/v1/corrections", json={
            "number": n1, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "三月版"})
        r = c.get(f"/api/v1/packages/{package_id}")
        self.assertEqual(
            [i for i in r.json()["items"] if i["number"] == n1][0]["body"],
            "初版")

        r = c.get(f"/api/v1/packages/{package_id}/review",
                  params={"known_as_of": now_canonical()})
        self.assertEqual(r.status_code, 200, r.text)
        review = r.json()
        changed = {i["number"]: i for i in review["items"]
                   if i["changed"]}
        self.assertIn(n1, changed)
        self.assertEqual(changed[n1]["pinned"]["revision"], 0)
        self.assertEqual(changed[n1]["current"]["revision"], 1)
        self.assertFalse(review["all_match"])
        self.assertEqual(review["package_known_as_of"], canon(pub))

        # 不存在的包 -> 404（取回与复核一致）
        r = c.get("/api/v1/packages/nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "package_not_found")
        r = c.get("/api/v1/packages/nope/review", params={"known_as_of": pub})
        self.assertEqual(r.status_code, 404)

    def test_packages_reject_over_http(self):
        c = self.client
        n = self._n()
        pub = c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1,
            "valid_end": t5}).json()["published_at"]

        # 编号重复 -> 422 duplicate_package_number
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": pub, "numbers": [n, n]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"],
                         "duplicate_package_number")

        # 编号缺失 -> 404 procedure_not_found
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": pub, "numbers": [n, "NOPE"]})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "procedure_not_found")

        # 作业时刻无适用正文 -> 409 no_applicable_version
        r = c.post("/api/v1/packages", json={
            "work_time": t5, "known_as_of": pub, "numbers": [n]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "no_applicable_version")

        # 知悉时刻在未来 -> 422 known_as_of_in_future
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": future, "numbers": [n]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "known_as_of_in_future")

        # 缺时区 -> 422 invalid_time
        r = c.post("/api/v1/packages", json={
            "work_time": "2026-01-01T00:00:00",
            "known_as_of": pub, "numbers": [n]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_time")

        # 空编号列表 / 缺字段 / 额外字段 -> 422 validation_error
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": pub, "numbers": []})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "numbers": [n]})
        self.assertEqual(r.status_code, 422)
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": pub, "numbers": [n],
            "created_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 422)

        # 复核知悉时刻早于包内知悉时刻 -> 422 review_known_too_early
        pkg = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": pub, "numbers": [n]}).json()
        r = c.get(f"/api/v1/packages/{pkg['id']}/review",
                  params={"known_as_of": t0})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"],
                         "review_known_too_early")
        # 复核缺少必填参数 -> 422
        r = c.get(f"/api/v1/packages/{pkg['id']}/review")
        self.assertEqual(r.status_code, 422)

    def test_prerequisites_and_closure_over_http(self):
        c = self.client
        pre = self._n() + "-PRE"
        root = self._n() + "-ROOT"
        c.post("/api/v1/releases", json={
            "number": pre, "body": "前置正文", "valid_start": t1,
            "valid_end": t5})
        # 首次发布携带前置编号列表
        r = c.post("/api/v1/releases", json={
            "number": root, "body": "根初版", "valid_start": t1,
            "valid_end": t5, "prerequisites": [pre]})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["items"][0]["prerequisites"], [pre])

        # 更正省略 -> 继承被替代版本的前置列表
        r = c.post("/api/v1/corrections", json={
            "number": root, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "根三月版"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["items"][0]["prerequisites"], [pre])

        # 递归解析：前置优先、根最后，含编号/版本/正文
        r = c.get(f"/api/v1/procedures/{root}/closure",
                  params={"work_time": t2})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["root"], root)
        self.assertEqual([i["number"] for i in body["items"]], [pre, root])
        self.assertEqual(body["items"][0]["body"], "前置正文")
        self.assertEqual(body["items"][1]["revision"], 1)  # 3 月适用更正版
        self.assertEqual(body["items"][1]["prerequisites"], [pre])

        # 历史审计：每个版本各自的前置列表
        r = c.get(f"/api/v1/procedures/{root}/history")
        revs = {v["revision"]: v for v in r.json()["items"]}
        self.assertEqual(revs[0]["prerequisites"], [pre])
        self.assertEqual(revs[1]["prerequisites"], [pre])

        # 显式空列表清空：4 月起的更正不再携带前置
        r = c.post("/api/v1/corrections", json={
            "number": root, "base_revision": 0,
            "valid_start": t3, "valid_end": t4, "body": "根四月版",
            "prerequisites": []})
        self.assertEqual(r.json()["items"][0]["prerequisites"], [])
        r = c.get(f"/api/v1/procedures/{root}/closure",
                  params={"work_time": t3})
        self.assertEqual([i["number"] for i in r.json()["items"]], [root])

    def test_closure_errors_over_http(self):
        c = self.client
        n = self._n()
        # 根不存在 -> 404 not_found
        r = c.get(f"/api/v1/procedures/{n}/closure",
                  params={"work_time": t2})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")

        # 引用编号不存在 -> 404 prerequisite_not_found（details 带引用链）
        c.post("/api/v1/releases", json={
            "number": n, "body": "根", "valid_start": t1, "valid_end": t5,
            "prerequisites": ["NO-SUCH-PRE"]})
        r = c.get(f"/api/v1/procedures/{n}/closure",
                  params={"work_time": t2})
        self.assertEqual(r.status_code, 404)
        err = r.json()["error"]
        self.assertEqual(err["code"], "prerequisite_not_found")
        self.assertEqual(err["details"]["number"], "NO-SUCH-PRE")
        self.assertEqual(err["details"]["path"], [n, "NO-SUCH-PRE"])

        # 作业时刻无适用正文 -> 409 no_applicable_version
        r = c.get(f"/api/v1/procedures/{n}/closure",
                  params={"work_time": t5})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "no_applicable_version")

        # 引用成环 -> 409 prerequisite_cycle（details 带环路径）
        a, b = self._n() + "-A", self._n() + "-B"
        c.post("/api/v1/releases", json={
            "number": a, "body": "A", "valid_start": t1, "valid_end": t5,
            "prerequisites": [b]})
        c.post("/api/v1/releases", json={
            "number": b, "body": "B", "valid_start": t1, "valid_end": t5,
            "prerequisites": [a]})
        r = c.get(f"/api/v1/procedures/{a}/closure",
                  params={"work_time": t2})
        self.assertEqual(r.status_code, 409)
        err = r.json()["error"]
        self.assertEqual(err["code"], "prerequisite_cycle")
        self.assertEqual(err["details"]["cycle"], [a, b, a])

        # 时间缺时区 -> 422 invalid_time；缺 work_time -> 422 validation_error
        r = c.get(f"/api/v1/procedures/{n}/closure",
                  params={"work_time": "2026-01-01T00:00:00"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_time")
        r = c.get(f"/api/v1/procedures/{n}/closure")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")

        # 前置编号空白/重复 -> 422；旧式请求（不带 prerequisites）仍可用
        r = c.post("/api/v1/releases", json={
            "number": self._n(), "body": "x", "valid_start": t1,
            "valid_end": t5, "prerequisites": [" "]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_number")
        r = c.post("/api/v1/releases", json={
            "number": self._n(), "body": "x", "valid_start": t1,
            "valid_end": t5, "prerequisites": ["P", "P"]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"],
                         "duplicate_prerequisite_number")
        r = c.post("/api/v1/releases", json={
            "number": self._n(), "body": "旧式", "valid_start": t1,
            "valid_end": t5})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["items"][0]["prerequisites"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
