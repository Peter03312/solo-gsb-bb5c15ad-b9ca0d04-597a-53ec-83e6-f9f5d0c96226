"""领域错误：带稳定的机器可读 code，路由层统一转成 HTTP 响应。"""
from __future__ import annotations


class DomainError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 422,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


def duplicate_number(number: str, at_index: int | None = None) -> DomainError:
    details = {"number": number}
    if at_index is not None:
        details["index"] = at_index
    return DomainError("duplicate_number", f"规程编号 {number} 已存在，禁止重复发布",
                       http_status=409, details=details)


def invalid_interval(start: str, end: str, reason: str,
                     at_index: int | None = None) -> DomainError:
    details = {"start": start, "end": end, "reason": reason}
    if at_index is not None:
        details["index"] = at_index
    return DomainError("invalid_interval", reason, http_status=422, details=details)


def invalid_known_order(before: str, after: str) -> DomainError:
    details = {"known_before": before, "known_after": after,
               "reason": "知悉时刻顺序非法：known_before 必须严格早于 known_after"}
    return DomainError("invalid_known_order",
                       details["reason"], http_status=422, details=details)


def procedure_not_found(number: str, at_index: int | None = None) -> DomainError:
    details = {"number": number}
    if at_index is not None:
        details["index"] = at_index
    return DomainError("not_found", f"规程编号 {number} 不存在，无法更正",
                       http_status=404, details=details)


def base_version_not_found(number: str, revision: int,
                           at_index: int | None = None) -> DomainError:
    details = {"number": number, "revision": revision}
    if at_index is not None:
        details["index"] = at_index
    return DomainError("base_version_not_found",
                       f"规程 {number} 的版本 r{revision} 不存在",
                       http_status=404, details=details)


def expired_base(number: str, revision: int, remaining: list[tuple[str, str]],
                 at_index: int | None = None) -> DomainError:
    details = {"number": number, "revision": revision,
               "remaining_intervals": [{"start": s, "end": e} for s, e in remaining]}
    if at_index is not None:
        details["index"] = at_index
    if remaining:
        msg = (f"规程 {number} r{revision} 在请求子区间处已被既有更正覆盖，"
               f"请改以最新正文版本为被替代版本（过期基版）")
    else:
        msg = (f"规程 {number} r{revision} 的生效区间已被既有更正全部覆盖，"
               f"不能再以其为被替代版本（过期基版）")
    return DomainError("expired_base_version", msg, http_status=409, details=details)


def concurrent_conflict(number: str, revision: int,
                        conflict_with: tuple[str, str],
                        at_index: int | None = None) -> DomainError:
    cs, ce = conflict_with
    details = {"number": number, "revision": revision,
               "conflict_interval": {"start": cs, "end": ce}}
    if at_index is not None:
        details["index"] = at_index
    return DomainError(
        "concurrent_correction_conflict",
        f"对规程 {number} r{revision} 的并发更正区间与既有更正 [{cs}, {ce}) 重叠，"
        f"同一版本的更正子区间不得相互冲突",
        http_status=409, details=details)


def revision_not_found(number: str, revision: int,
                       at_index: int | None = None) -> DomainError:
    details = {"number": number, "revision": revision}
    if at_index is not None:
        details["index"] = at_index
    return DomainError("revision_not_found",
                       f"规程 {number} 的版本 r{revision} 不存在，无法撤回",
                       http_status=404, details=details)


def first_revision_not_withdrawable(number: str, revision: int,
                                    at_index: int | None = None) -> DomainError:
    details = {"number": number, "revision": revision}
    if at_index is not None:
        details["index"] = at_index
    return DomainError(
        "first_revision_not_withdrawable",
        f"规程 {number} r{revision} 是首版，首版只能更正不能撤回",
        http_status=409, details=details)


def already_withdrawn(number: str, revision: int, withdrawn_at: str,
                      at_index: int | None = None) -> DomainError:
    details = {"number": number, "revision": revision,
               "withdrawn_at": withdrawn_at}
    if at_index is not None:
        details["index"] = at_index
    return DomainError(
        "already_withdrawn",
        f"规程 {number} r{revision} 已于 {withdrawn_at} 撤回，"
        f"撤回只追加一次事件、不改变历史，不能重复撤回",
        http_status=409, details=details)


def version_has_children(number: str, revision: int,
                         child_revisions: list[int],
                         at_index: int | None = None) -> DomainError:
    details = {"number": number, "revision": revision,
               "child_revisions": child_revisions}
    if at_index is not None:
        details["index"] = at_index
    return DomainError(
        "version_has_children",
        f"规程 {number} r{revision} 已存在子更正 r{child_revisions}，"
        f"只能撤回当前仍生效且没有子更正的叶子版本（撤回不删除版本行）",
        http_status=409, details=details)


def base_version_withdrawn(number: str, revision: int, withdrawn_at: str,
                           at_index: int | None = None) -> DomainError:
    details = {"number": number, "revision": revision,
               "withdrawn_at": withdrawn_at}
    if at_index is not None:
        details["index"] = at_index
    return DomainError(
        "base_version_withdrawn",
        f"规程 {number} r{revision} 已于 {withdrawn_at} 撤回，"
        f"不能以已撤回版本为被替代版本提交更正",
        http_status=409, details=details)


# ---------------------------------------------------------------- 作业规程包


def duplicate_package_number(number: str, at_index: int) -> DomainError:
    return DomainError(
        "duplicate_package_number",
        f"规程编号 {number} 在同一固定请求中重复出现，规程包的编号列表必须互不相同",
        http_status=422, details={"number": number, "index": at_index})


def package_procedure_not_found(number: str, at_index: int) -> DomainError:
    return DomainError(
        "procedure_not_found",
        f"规程编号 {number} 不存在，无法固定作业规程包",
        http_status=404, details={"number": number, "index": at_index})


def no_applicable_version(number: str, work_time: str,
                          known_as_of: str, at_index: int) -> DomainError:
    return DomainError(
        "no_applicable_version",
        f"规程 {number} 在知悉时刻 {known_as_of} 下、对作业时刻 {work_time} "
        f"没有已发布且生效（未撤回）的适用正文，作业规程包整体拒绝、不留包",
        http_status=409,
        details={"number": number, "index": at_index,
                 "work_time": work_time, "known_as_of": known_as_of})


def known_as_of_in_future(known_as_of: str, now: str) -> DomainError:
    return DomainError(
        "known_as_of_in_future",
        f"知悉时刻 {known_as_of} 晚于服务端当前时刻 {now}，不允许固定/复核未来才知悉的状态",
        http_status=422,
        details={"known_as_of": known_as_of, "server_now": now})


def package_not_found(package_id: str) -> DomainError:
    return DomainError(
        "package_not_found", f"作业规程包 {package_id} 不存在",
        http_status=404, details={"package_id": package_id})


def review_known_too_early(known_as_of: str, package_known: str) -> DomainError:
    return DomainError(
        "review_known_too_early",
        f"复核知悉时刻 {known_as_of} 早于包内知悉时刻 {package_known}，"
        f"复核只能以不早于固定时知悉时刻的时间进行",
        http_status=422,
        details={"known_as_of": known_as_of,
                 "package_known_as_of": package_known})


# ---------------------------------------------------------------- 前置规程


def duplicate_prerequisite_number(number: str, at_index: int | None,
                                  position: int) -> DomainError:
    details: dict = {"number": number, "position": position}
    if at_index is not None:
        details["index"] = at_index
    return DomainError(
        "duplicate_prerequisite_number",
        f"前置规程编号 {number} 在列表中重复出现（第 {position} 项），"
        f"前置编号列表必须互不相同",
        http_status=422, details=details)


def prerequisite_not_found(number: str, path: list[str]) -> DomainError:
    return DomainError(
        "prerequisite_not_found",
        f"前置规程编号 {number} 不存在（引用链 {' -> '.join(path)}），"
        f"前置规程清单整体失败",
        http_status=404,
        details={"number": number, "path": path})


def closure_no_applicable_version(number: str, work_time: str,
                                  known_as_of: str,
                                  path: list[str]) -> DomainError:
    return DomainError(
        "no_applicable_version",
        f"规程 {number} 在知悉时刻 {known_as_of} 下、对作业时刻 {work_time} "
        f"没有已发布且生效（未撤回）的适用正文（引用链 {' -> '.join(path)}），"
        f"前置规程清单整体失败",
        http_status=409,
        details={"number": number, "work_time": work_time,
                 "known_as_of": known_as_of, "path": path})


def prerequisite_cycle(cycle: list[str]) -> DomainError:
    return DomainError(
        "prerequisite_cycle",
        f"前置规程引用存在环：{' -> '.join(cycle)}，不返回部分清单",
        http_status=409, details={"cycle": cycle})
