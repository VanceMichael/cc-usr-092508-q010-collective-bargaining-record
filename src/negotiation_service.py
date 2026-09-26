"""集体协商服务：代表授权、逐轮提案、共同文本与协议形成。

服务采用事件溯源：所有变化先追加为不可变事件，当前状态由事件折叠而来。
代表更换、授权收窄、利益冲突、会谈中止与重新开局都只是新增事件，不会
抹去此前过程；服务重启后重放事件日志，即可继续处理待确认承诺、调解
事项与履行节点。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EMPLOYER = "employer"  # 企业方
WORKER = "worker"  # 职工方
SIDES = (EMPLOYER, WORKER)

ROLE_REPRESENTATIVE = "representative"  # 协商代表
ROLE_UNION_STAFF = "union_staff"  # 工会工作人员
ROLE_MEDIATOR = "mediator"  # 调解人员
ROLE_SUPERVISOR = "supervisor"  # 监督人员

TOPIC_OPEN = "open"
TOPIC_FROZEN = "frozen"  # 双方就该议题达成一致，共同条款已冻结

CLAUSE_PENDING = "pending_authorization"  # 超出授权，暂存待确认
CLAUSE_PROPOSED = "proposed"
CLAUSE_FROZEN = "frozen"
CLAUSE_WITHDRAWN = "withdrawn"
CLAUSE_SUPERSEDED = "superseded"  # 被本方新一版提案取代

COMMITMENT_PENDING = "pending"
COMMITMENT_CONFIRMED = "confirmed"
COMMITMENT_EXPIRED = "expired"

MATERIAL_SUBMITTED = "submitted"
MATERIAL_CORROBORATED = "corroborated"  # 经对方或中立者佐证

ROUND_OPEN = "open"
ROUND_SUSPENDED = "suspended"
ROUND_CLOSED = "closed"

AGREEMENT_COLLECTING = "collecting_confirmation"  # 等待双方各自确认最终文本
AGREEMENT_FORMED = "formed"  # 双方确认一致，协议形成，待签署
AGREEMENT_EFFECTIVE = "effective"  # 双方签署完成
AGREEMENT_CANCELLED = "cancelled"  # 生效前一方撤回
AGREEMENT_DISPUTED = "disputed"  # 确认文本不一致（异文），进入争议

MILESTONE_PENDING = "pending"
MILESTONE_DONE = "done"


class NegotiationError(ValueError):
    """协商规则被拒绝时抛出。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EventStore:
    """JSONL 事件日志；绑定文件路径后，服务重启可从日志恢复。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._events: list[dict[str, Any]] = []
        if self._path and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._events.append(json.loads(line))
        elif self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        if self._path:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        return list(self._events)


class NegotiationService:
    """集体协商服务：命令校验规则并追加事件，查询由状态折叠结果提供。"""

    def __init__(self, store: EventStore | None = None) -> None:
        self._store = store or EventStore()
        self.events: list[dict[str, Any]] = []
        self._keys: dict[str, int] = {}
        self.representatives: dict[str, dict[str, Any]] = {}
        self.authorizations: dict[str, list[dict[str, Any]]] = {EMPLOYER: [], WORKER: []}
        self.topics: dict[str, dict[str, Any]] = {}
        self.proposals: dict[str, dict[str, Any]] = {}
        self.clauses: dict[str, dict[str, Any]] = {}
        self.materials: dict[str, dict[str, Any]] = {}
        self.commitments: dict[str, dict[str, Any]] = {}
        self.rounds: dict[int, dict[str, Any]] = {}
        self.common_texts: list[dict[str, Any]] = []
        self.agreements: dict[str, dict[str, Any]] = {}
        self.milestones: dict[str, dict[str, Any]] = {}
        self.recusals: set[tuple[str, str]] = set()
        self.disputes: list[dict[str, Any]] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        for event in self._store.read_all():
            self._apply(event)

    # ------------------------------------------------------------------
    # 事件记录与折叠
    # ------------------------------------------------------------------

    def _record(self, type_: str, payload: dict[str, Any], key: str | None = None, at: str | None = None) -> dict[str, Any]:
        event: dict[str, Any] = {
            "seq": len(self.events) + 1,
            "type": type_,
            "payload": payload,
            "at": at or _now(),
        }
        if key:
            event["key"] = key
        self._store.append(event)
        self._apply(event)
        return event

    def _apply(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if "key" in event:
            self._keys[event["key"]] = event["seq"]
        handler = getattr(self, f"_on_{event['type']}")
        handler(event["payload"], event["at"])

    def _dedup(self, key: str | None) -> dict[str, Any] | None:
        """同一幂等键的命令只执行一次，重复提交返回首次结果。"""
        if key and key in self._keys:
            return {"deduplicated": True, "event_seq": self._keys[key]}
        return None

    # ------------------------------------------------------------------
    # 代表、回避与授权
    # ------------------------------------------------------------------

    def register_representative(
        self,
        rep_id: str,
        name: str,
        role: str,
        side: str | None = None,
        qualifications: list[str] | None = None,
        key: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        dup = self._dedup(key)
        if dup:
            return dup
        if rep_id in self.representatives:
            raise NegotiationError(f"代表编号重复：{rep_id}")
        if role == ROLE_REPRESENTATIVE and side not in SIDES:
            raise NegotiationError("协商代表必须属于企业方或职工方")
        if role in (ROLE_MEDIATOR, ROLE_SUPERVISOR) and side is not None:
            raise NegotiationError("调解人员与监督人员应保持中立，不隶属于任何一方")
        self._record(
            "representative_registered",
            {
                "rep_id": rep_id,
                "name": name,
                "role": role,
                "side": side,
                "qualifications": list(qualifications or []),
            },
            key=key,
            at=at,
        )
        return {"rep_id": rep_id}

    def replace_representative(
        self,
        old_rep_id: str,
        new_rep_id: str,
        name: str,
        qualifications: list[str] | None = None,
        reason: str = "",
        key: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """代表更换：原代表停用，过程记录保留，新代表承继本方立场。"""
        dup = self._dedup(key)
        if dup:
            return dup
        old = self._active_rep(old_rep_id)
        if new_rep_id in self.representatives:
            raise NegotiationError(f"代表编号重复：{new_rep_id}")
        self._record(
            "representative_replaced",
            {
                "old_rep_id": old_rep_id,
                "new_rep_id": new_rep_id,
                "name": name,
                "qualifications": list(qualifications or old["qualifications"]),
                "reason": reason,
            },
            key=key,
            at=at,
        )
        return {"old_rep_id": old_rep_id, "new_rep_id": new_rep_id}

    def declare_recusal(self, rep_id: str, topic_id: str, reason: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        """登记回避关系（含利益冲突），被回避代表不得就该议题行动。"""
        dup = self._dedup(key)
        if dup:
            return dup
        self._active_rep(rep_id)
        if topic_id not in self.topics:
            raise NegotiationError(f"议题不存在：{topic_id}")
        if (rep_id, topic_id) in self.recusals:
            return {"deduplicated": True}
        self._record("recusal_declared", {"rep_id": rep_id, "topic_id": topic_id, "reason": reason}, key=key, at=at)
        return {"rep_id": rep_id, "topic_id": topic_id}

    def grant_authorization(self, side: str, scopes: list[str], note: str = "", key: str | None = None, at: str | None = None) -> dict[str, Any]:
        """授予或扩大成员授权范围；缩小范围必须使用收窄命令。"""
        dup = self._dedup(key)
        if dup:
            return dup
        self._check_side(side)
        new = set(scopes)
        if not new:
            raise NegotiationError("授权范围不能为空")
        current = self.current_scopes(side)
        if current and new == current:
            raise NegotiationError("授权范围无变化")
        if current and not new >= current:
            raise NegotiationError("缩小授权范围须使用收窄命令，并保留过程记录")
        version = len(self.authorizations[side]) + 1
        self._record(
            "authorization_granted",
            {"side": side, "version": version, "scopes": sorted(new), "note": note},
            key=key,
            at=at,
        )
        return {"side": side, "version": version}

    def narrow_authorization(self, side: str, removed_scopes: list[str], reason: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        """授权收窄：生成新版本，旧版本保留在授权历史中。"""
        dup = self._dedup(key)
        if dup:
            return dup
        self._check_side(side)
        current = self.current_scopes(side)
        removed = set(removed_scopes) & current
        if not removed:
            raise NegotiationError("收窄范围必须涉及现有授权")
        version = len(self.authorizations[side]) + 1
        self._record(
            "authorization_narrowed",
            {
                "side": side,
                "version": version,
                "scopes": sorted(current - removed),
                "removed": sorted(removed),
                "reason": reason,
            },
            key=key,
            at=at,
        )
        return {"side": side, "version": version}

    def current_scopes(self, side: str) -> set[str]:
        versions = self.authorizations.get(side) or []
        return set(versions[-1]["scopes"]) if versions else set()

    def authorization_history(self, side: str) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.authorizations.get(side, [])]

    # ------------------------------------------------------------------
    # 议题、材料与会议轮次
    # ------------------------------------------------------------------

    def open_topic(self, topic_id: str, title: str, scope: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        dup = self._dedup(key)
        if dup:
            return dup
        if topic_id in self.topics:
            raise NegotiationError(f"议题编号重复：{topic_id}")
        self._record("topic_opened", {"topic_id": topic_id, "title": title, "scope": scope}, key=key, at=at)
        return {"topic_id": topic_id}

    def submit_material(
        self,
        material_id: str,
        provider_id: str,
        topic_id: str,
        summary: str,
        confidential: bool = True,
        key: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """提交成本测算等材料；保密材料不进入公开视图。"""
        dup = self._dedup(key)
        if dup:
            return dup
        provider = self._active_rep(provider_id)
        if provider["side"] not in SIDES:
            raise NegotiationError("资料提供者须为企业方或职工方成员")
        if topic_id not in self.topics:
            raise NegotiationError(f"议题不存在：{topic_id}")
        if material_id in self.materials:
            raise NegotiationError(f"材料编号重复：{material_id}")
        self._record(
            "material_submitted",
            {
                "material_id": material_id,
                "provider_id": provider_id,
                "side": provider["side"],
                "topic_id": topic_id,
                "summary": summary,
                "confidential": bool(confidential),
            },
            key=key,
            at=at,
        )
        return {"material_id": material_id}

    def acknowledge_material(self, material_id: str, rep_id: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        """佐证他方材料；资料提供者不能单独认定自己的数据成立。"""
        dup = self._dedup(key)
        if dup:
            return dup
        material = self._material(material_id)
        rep = self._active_rep(rep_id)
        if rep_id == material["provider_id"]:
            raise NegotiationError("资料提供者不能单独认定自己的数据成立")
        if rep["role"] == ROLE_SUPERVISOR:
            raise NegotiationError("监督人员不参与数据认定")
        if rep["side"] is not None and rep["side"] == material["side"]:
            raise NegotiationError("本方确认不构成独立佐证")
        if rep_id in material["acknowledgements"]:
            return {"deduplicated": True}
        self._record("material_acknowledged", {"material_id": material_id, "rep_id": rep_id}, key=key, at=at)
        return {"material_id": material_id, "status": material["status"]}

    def open_round(self, round_no: int, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        dup = self._dedup(key)
        if dup:
            return dup
        if round_no in self.rounds:
            raise NegotiationError(f"会议轮次已存在：{round_no}")
        if any(r["status"] == ROUND_OPEN for r in self.rounds.values()):
            raise NegotiationError("已有进行中的会议轮次")
        self._record("round_opened", {"round_no": round_no}, key=key, at=at)
        return {"round_no": round_no}

    def suspend_round(self, reason: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        dup = self._dedup(key)
        if dup:
            return dup
        rnd = self._open_round()
        self._record("round_suspended", {"round_no": rnd["no"], "reason": reason}, key=key, at=at)
        return {"round_no": rnd["no"], "status": ROUND_SUSPENDED}

    def resume_round(self, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        dup = self._dedup(key)
        if dup:
            return dup
        suspended = [r for r in self.rounds.values() if r["status"] == ROUND_SUSPENDED]
        if not suspended:
            raise NegotiationError("没有已中止的会议轮次")
        rnd = suspended[-1]
        self._record("round_resumed", {"round_no": rnd["no"]}, key=key, at=at)
        return {"round_no": rnd["no"], "status": ROUND_OPEN}

    def close_round(self, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        dup = self._dedup(key)
        if dup:
            return dup
        active = [r for r in self.rounds.values() if r["status"] in (ROUND_OPEN, ROUND_SUSPENDED)]
        if not active:
            raise NegotiationError("没有可结束的会议轮次")
        rnd = active[-1]
        self._record("round_closed", {"round_no": rnd["no"]}, key=key, at=at)
        return {"round_no": rnd["no"], "status": ROUND_CLOSED}

    def record_receipt(self, rep_id: str, receipt_key: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        """会议回执按回执键幂等归并，重复回执不产生新记录。"""
        dup = self._dedup(key)
        if dup:
            return dup
        if receipt_key in self.receipts:
            return {"deduplicated": True, "receipt_key": receipt_key}
        self._active_rep(rep_id)
        rnd = self._open_round()
        self._record(
            "receipt_recorded",
            {"round_no": rnd["no"], "rep_id": rep_id, "receipt_key": receipt_key},
            key=key,
            at=at,
        )
        return {"receipt_key": receipt_key, "round_no": rnd["no"]}

    # ------------------------------------------------------------------
    # 提案、条款与承诺
    # ------------------------------------------------------------------

    def submit_proposal(
        self,
        proposal_id: str,
        rep_id: str,
        topic_id: str,
        clauses: list[dict[str, Any]],
        parent_id: str | None = None,
        note: str = "",
        key: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """提交一版提案；每次让步必须引用本方上一版提案。"""
        dup = self._dedup(key)
        if dup:
            return dup
        rep = self._active_rep(rep_id)
        if rep["role"] != ROLE_REPRESENTATIVE:
            raise NegotiationError("只有双方代表可以提交提案，调解人员只能推动共同文本")
        topic = self._topic(topic_id)
        if topic["status"] != TOPIC_OPEN:
            raise NegotiationError("该议题的共同条款已冻结，未决内容请在其他议题继续协商")
        self._check_not_recused(rep_id, topic_id)
        rnd = self._open_round()
        if proposal_id in self.proposals:
            raise NegotiationError(f"提案编号重复：{proposal_id}")
        side = rep["side"]
        latest = self._latest_proposal(side, topic_id)
        expected_parent = latest["proposal_id"] if latest else None
        if parent_id != expected_parent:
            raise NegotiationError("每次让步必须引用本方上一版提案")
        scopes = self.current_scopes(side)
        clause_records = []
        seen: set[str] = set()
        for clause in clauses:
            clause_id = clause["clause_id"]
            if clause_id in self.clauses or clause_id in seen:
                raise NegotiationError(f"条款编号重复：{clause_id}")
            seen.add(clause_id)
            if not str(clause.get("text", "")).strip():
                raise NegotiationError("条款内容不能为空")
            clause_records.append(
                {
                    "clause_id": clause_id,
                    "text": clause["text"],
                    "legal_bases": list(clause.get("legal_bases", [])),
                    "public": bool(clause.get("public", False)),
                    "in_scope": topic["scope"] in scopes,
                }
            )
        self._record(
            "proposal_submitted",
            {
                "proposal_id": proposal_id,
                "rep_id": rep_id,
                "side": side,
                "topic_id": topic_id,
                "round_no": rnd["no"],
                "parent_id": parent_id,
                "clauses": clause_records,
                "note": note,
            },
            key=key,
            at=at,
        )
        return {
            "proposal_id": proposal_id,
            "clauses": {c["clause_id"]: self.clauses[c["clause_id"]]["status"] for c in clause_records},
        }

    def withdraw_clause(self, clause_id: str, rep_id: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        dup = self._dedup(key)
        if dup:
            return dup
        clause = self._clause(clause_id)
        rep = self._active_rep(rep_id)
        if rep["side"] != clause["side"]:
            raise NegotiationError("只能撤回本方条款")
        if clause["status"] not in (CLAUSE_PENDING, CLAUSE_PROPOSED):
            raise NegotiationError("已冻结或已失效的条款不能撤回")
        self._record("clause_withdrawn", {"clause_id": clause_id, "rep_id": rep_id}, key=key, at=at)
        return {"clause_id": clause_id, "status": CLAUSE_WITHDRAWN}

    def accept_clause(self, clause_id: str, rep_id: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        """接受对方条款；双方接受后条款冻结，全部议题冻结时形成最终文本。"""
        dup = self._dedup(key)
        if dup:
            return dup
        clause = self._clause(clause_id)
        rep = self._active_rep(rep_id)
        if rep["role"] != ROLE_REPRESENTATIVE:
            raise NegotiationError("只有双方代表可以接受条款")
        self._check_not_recused(rep_id, clause["topic_id"])
        if clause["status"] == CLAUSE_PENDING:
            raise NegotiationError("条款超出授权范围，暂存待确认，暂不能接受")
        if clause["status"] != CLAUSE_PROPOSED:
            raise NegotiationError("条款不在可接受状态")
        side = rep["side"]
        if side in clause["acceptances"]:
            return {"clause_id": clause_id, "deduplicated": True}
        self._record("clause_accepted", {"clause_id": clause_id, "side": side}, key=key, at=at)
        if set(clause["acceptances"]) == set(SIDES):
            self._record("clause_frozen", {"clause_id": clause_id, "topic_id": clause["topic_id"]}, at=at)
            self._maybe_finalize(at)
        return {"clause_id": clause_id, "status": clause["status"]}

    def record_commitment(
        self,
        commitment_id: str,
        rep_id: str,
        text: str,
        valid_until: str,
        proposal_id: str | None = None,
        key: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """把口头承诺落字为据并附有效期，未经对方确认不生效。"""
        dup = self._dedup(key)
        if dup:
            return dup
        rep = self._active_rep(rep_id)
        if rep["role"] != ROLE_REPRESENTATIVE:
            raise NegotiationError("只有双方代表可以作出承诺")
        if commitment_id in self.commitments:
            raise NegotiationError(f"承诺编号重复：{commitment_id}")
        if not valid_until:
            raise NegotiationError("承诺必须附有效期")
        self._record(
            "commitment_recorded",
            {
                "commitment_id": commitment_id,
                "rep_id": rep_id,
                "side": rep["side"],
                "text": text,
                "valid_until": valid_until,
                "proposal_id": proposal_id,
            },
            key=key,
            at=at,
        )
        return {"commitment_id": commitment_id}

    def confirm_commitment(self, commitment_id: str, rep_id: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        """承诺须由对方确认，防止任何一方事后否认。"""
        dup = self._dedup(key)
        if dup:
            return dup
        commitment = self._commitment(commitment_id)
        rep = self._active_rep(rep_id)
        if rep["role"] != ROLE_REPRESENTATIVE or rep["side"] not in SIDES or rep["side"] == commitment["side"]:
            raise NegotiationError("承诺须由对方代表确认")
        if commitment["status"] != COMMITMENT_PENDING:
            raise NegotiationError("承诺不在待确认状态")
        self._record("commitment_confirmed", {"commitment_id": commitment_id, "rep_id": rep_id}, key=key, at=at)
        return {"commitment_id": commitment_id, "status": COMMITMENT_CONFIRMED}

    def expire_commitments(self, today: str, at: str | None = None) -> list[str]:
        """期限届满的待确认承诺转为已过期；记录保留，可续期继续处理。"""
        expired = [
            c["commitment_id"]
            for c in self.commitments.values()
            if c["status"] == COMMITMENT_PENDING and c["valid_until"] < today
        ]
        for commitment_id in expired:
            self._record("commitment_expired", {"commitment_id": commitment_id}, at=at)
        return expired

    def renew_commitment(self, commitment_id: str, valid_until: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        dup = self._dedup(key)
        if dup:
            return dup
        commitment = self._commitment(commitment_id)
        if commitment["status"] not in (COMMITMENT_PENDING, COMMITMENT_EXPIRED):
            raise NegotiationError("只有待确认或已过期的承诺可以续期")
        self._record(
            "commitment_renewed",
            {"commitment_id": commitment_id, "valid_until": valid_until},
            key=key,
            at=at,
        )
        return {"commitment_id": commitment_id, "status": COMMITMENT_PENDING}

    # ------------------------------------------------------------------
    # 共同文本、协议形成与签署
    # ------------------------------------------------------------------

    def promote_common_text(self, mediator_id: str, clause_ids: list[str] | None = None, note: str = "", key: str | None = None, at: str | None = None) -> dict[str, Any]:
        """调解人员只能推动共同文本，不能替任何一方立场的提案或接受。"""
        dup = self._dedup(key)
        if dup:
            return dup
        rep = self._active_rep(mediator_id)
        if rep["role"] != ROLE_MEDIATOR:
            raise NegotiationError("只有调解人员可以推动共同文本")
        ids = sorted(clause_ids) if clause_ids else sorted(c["clause_id"] for c in self.clauses.values() if c["status"] == CLAUSE_FROZEN)
        if not ids:
            raise NegotiationError("尚无可纳入共同文本的冻结条款")
        for clause_id in ids:
            if self._clause(clause_id)["status"] != CLAUSE_FROZEN:
                raise NegotiationError("共同文本只能由双方已接受的冻结条款组成")
        version = len(self.common_texts) + 1
        self._record(
            "common_text_promoted",
            {"version": version, "clause_ids": ids, "mediator_id": mediator_id, "note": note},
            key=key,
            at=at,
        )
        return {"version": version, "clause_ids": ids}

    def confirm_final_text(self, agreement_id: str, rep_id: str, text_hash: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        """双方各自确认最终文本；确认一致才形成协议，异文进入争议。"""
        dup = self._dedup(key)
        if dup:
            return dup
        agreement = self._agreement(agreement_id)
        if agreement["status"] != AGREEMENT_COLLECTING:
            raise NegotiationError("协议不在等待确认状态")
        rep = self._active_rep(rep_id)
        if rep["role"] != ROLE_REPRESENTATIVE:
            raise NegotiationError("只有双方代表可以确认最终文本")
        side = rep["side"]
        if side in agreement["confirmations"]:
            return {"deduplicated": True, "agreement_id": agreement_id}
        self._record(
            "final_text_confirmed",
            {"agreement_id": agreement_id, "side": side, "text_hash": text_hash},
            key=key,
            at=at,
        )
        if len(agreement["confirmations"]) == len(SIDES):
            if len(set(agreement["confirmations"].values())) == 1:
                self._record("agreement_formed", {"agreement_id": agreement_id}, at=at)
            else:
                self._record(
                    "dispute_opened",
                    {
                        "agreement_id": agreement_id,
                        "reason": "双方确认的最终文本不一致（异文）",
                        "confirmations": dict(agreement["confirmations"]),
                    },
                    at=at,
                )
        return {"agreement_id": agreement_id, "status": agreement["status"]}

    def sign_agreement(self, agreement_id: str, rep_id: str, signature_key: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        """异步签名按方归并；双方签署完成才生效，绝不出现单方已生效。"""
        dup = self._dedup(key)
        if dup:
            return dup
        agreement = self._agreement(agreement_id)
        rep = self._active_rep(rep_id)
        if rep["role"] != ROLE_REPRESENTATIVE:
            raise NegotiationError("只有双方代表可以签署协议")
        side = rep["side"]
        if side in agreement["signatures"]:
            return {"deduplicated": True, "agreement_id": agreement_id}
        if agreement["status"] == AGREEMENT_CANCELLED:
            raise NegotiationError("协议已因一方撤回而取消，不能再签署")
        if agreement["status"] != AGREEMENT_FORMED:
            raise NegotiationError("协议尚未经双方确认形成，不能签署")
        self._record(
            "signature_recorded",
            {"agreement_id": agreement_id, "side": side, "signature_key": signature_key},
            key=key,
            at=at,
        )
        if len(agreement["signatures"]) == len(SIDES):
            self._record("agreement_effective", {"agreement_id": agreement_id}, at=at)
        return {"agreement_id": agreement_id, "status": agreement["status"]}

    def withdraw_agreement(self, agreement_id: str, rep_id: str, reason: str = "", key: str | None = None, at: str | None = None) -> dict[str, Any]:
        """生效前一方可撤回；与签署并发时按处理顺序定终态，已生效不得单方撤回。"""
        dup = self._dedup(key)
        if dup:
            return dup
        agreement = self._agreement(agreement_id)
        rep = self._active_rep(rep_id)
        if rep["role"] != ROLE_REPRESENTATIVE:
            raise NegotiationError("只有双方代表可以撤回")
        side = rep["side"]
        status = agreement["status"]
        if status == AGREEMENT_COLLECTING:
            if side not in agreement["confirmations"]:
                raise NegotiationError("本方尚未确认，无可撤回")
            self._record("confirmation_withdrawn", {"agreement_id": agreement_id, "side": side}, key=key, at=at)
            return {"agreement_id": agreement_id, "status": agreement["status"]}
        if status == AGREEMENT_FORMED:
            self._record(
                "agreement_cancelled",
                {"agreement_id": agreement_id, "side": side, "reason": reason},
                key=key,
                at=at,
            )
            return {"agreement_id": agreement_id, "status": agreement["status"]}
        if status == AGREEMENT_EFFECTIVE:
            raise NegotiationError("协议已生效，不能单方撤回，须转入争议程序")
        raise NegotiationError("协议当前状态不允许撤回")

    # ------------------------------------------------------------------
    # 履行节点
    # ------------------------------------------------------------------

    def register_milestone(self, milestone_id: str, agreement_id: str, title: str, due: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        dup = self._dedup(key)
        if dup:
            return dup
        agreement = self._agreement(agreement_id)
        if agreement["status"] != AGREEMENT_EFFECTIVE:
            raise NegotiationError("协议生效后才能登记履行节点")
        if milestone_id in self.milestones:
            raise NegotiationError(f"履行节点编号重复：{milestone_id}")
        self._record(
            "milestone_registered",
            {"milestone_id": milestone_id, "agreement_id": agreement_id, "title": title, "due": due},
            key=key,
            at=at,
        )
        return {"milestone_id": milestone_id}

    def complete_milestone(self, milestone_id: str, result: str, key: str | None = None, at: str | None = None) -> dict[str, Any]:
        dup = self._dedup(key)
        if dup:
            return dup
        milestone = self.milestones.get(milestone_id)
        if not milestone:
            raise NegotiationError(f"履行节点不存在：{milestone_id}")
        if milestone["status"] == MILESTONE_DONE:
            return {"deduplicated": True, "milestone_id": milestone_id}
        self._record("milestone_completed", {"milestone_id": milestone_id, "result": result}, key=key, at=at)
        return {"milestone_id": milestone_id, "status": MILESTONE_DONE}

    # ------------------------------------------------------------------
    # 查询：下一步、公开视图与监督追溯
    # ------------------------------------------------------------------

    def next_steps(self, actor_id: str) -> list[dict[str, Any]]:
        """按参与方角色给出适合自身的下一步。"""
        rep = self.representatives.get(actor_id)
        if not rep or not rep["active"]:
            return []
        role, side = rep["role"], rep["side"]
        steps: list[dict[str, Any]] = []
        if role == ROLE_MEDIATOR:
            for topic in self.topics.values():
                if topic["status"] == TOPIC_OPEN:
                    steps.append({"kind": "mediate_topic", "topic_id": topic["topic_id"], "detail": "推动共同文本"})
            for dispute in self.disputes:
                steps.append({"kind": "mediate_dispute", "agreement_id": dispute["agreement_id"]})
            return steps
        if role == ROLE_SUPERVISOR:
            for agreement in self.agreements.values():
                steps.append({"kind": "audit_agreement", "agreement_id": agreement["agreement_id"], "status": agreement["status"]})
            return steps
        if side not in SIDES:
            return steps
        other = WORKER if side == EMPLOYER else EMPLOYER
        for clause in self.clauses.values():
            if clause["side"] == side and clause["status"] == CLAUSE_PENDING:
                steps.append({"kind": "clause_awaiting_authorization", "clause_id": clause["clause_id"], "topic_id": clause["topic_id"]})
            if (
                role == ROLE_REPRESENTATIVE
                and clause["side"] == other
                and clause["status"] == CLAUSE_PROPOSED
                and side not in clause["acceptances"]
                and (actor_id, clause["topic_id"]) not in self.recusals
            ):
                steps.append({"kind": "review_clause", "clause_id": clause["clause_id"], "topic_id": clause["topic_id"]})
        for commitment in self.commitments.values():
            if commitment["status"] == COMMITMENT_PENDING and commitment["side"] == other:
                steps.append({"kind": "confirm_commitment", "commitment_id": commitment["commitment_id"]})
            if commitment["status"] == COMMITMENT_EXPIRED and commitment["side"] == side:
                steps.append({"kind": "renew_commitment", "commitment_id": commitment["commitment_id"]})
        for agreement in self.agreements.values():
            if agreement["status"] == AGREEMENT_COLLECTING and side not in agreement["confirmations"]:
                steps.append({"kind": "confirm_final_text", "agreement_id": agreement["agreement_id"]})
            if agreement["status"] == AGREEMENT_FORMED and side not in agreement["signatures"]:
                steps.append({"kind": "sign_agreement", "agreement_id": agreement["agreement_id"]})
            if agreement["status"] == AGREEMENT_EFFECTIVE:
                for milestone in self.milestones.values():
                    if milestone["agreement_id"] == agreement["agreement_id"] and milestone["status"] == MILESTONE_PENDING:
                        steps.append({"kind": "report_milestone", "milestone_id": milestone["milestone_id"]})
        if role == ROLE_UNION_STAFF:
            covered = self.current_scopes(side)
            for topic in self.topics.values():
                if topic["status"] == TOPIC_OPEN and topic["scope"] not in covered:
                    steps.append({"kind": "extend_authorization", "topic_id": topic["topic_id"], "scope": topic["scope"]})
        return steps

    def public_docket(self) -> dict[str, Any]:
        """公开视图：只含标明可公开的内容，保密材料与不公开条款不进入。"""
        return {
            "topics": [
                {"topic_id": t["topic_id"], "title": t["title"], "status": t["status"]} for t in self.topics.values()
            ],
            "clauses": [
                {"clause_id": c["clause_id"], "topic_id": c["topic_id"], "text": c["text"], "status": c["status"]}
                for c in self.clauses.values()
                if c["public"]
            ],
            "agreements": [
                {"agreement_id": a["agreement_id"], "status": a["status"], "text_hash": a["text_hash"]}
                for a in self.agreements.values()
            ],
        }

    def list_materials(self, actor_id: str) -> list[dict[str, Any]]:
        """保密材料只对双方代表与调解人员公开内容，其余仅见元数据。"""
        rep = self.representatives.get(actor_id)
        privileged = bool(rep and rep["active"] and rep["role"] in (ROLE_REPRESENTATIVE, ROLE_MEDIATOR))
        view = []
        for material in self.materials.values():
            entry = {
                "material_id": material["material_id"],
                "topic_id": material["topic_id"],
                "side": material["side"],
                "status": material["status"],
                "confidential": material["confidential"],
                "summary": material["summary"] if (privileged or not material["confidential"]) else None,
            }
            view.append(entry)
        return view

    def trace_clause(self, clause_id: str) -> dict[str, Any]:
        """监督追溯：从条款追到授权版本、提案变化、法律依据与履行结果。"""
        clause = self._clause(clause_id)
        chain = []
        proposal_id: str | None = clause["proposal_id"]
        while proposal_id:
            proposal = self.proposals[proposal_id]
            chain.append(
                {
                    "proposal_id": proposal["proposal_id"],
                    "side": proposal["side"],
                    "round_no": proposal["round_no"],
                    "parent_id": proposal["parent_id"],
                    "note": proposal["note"],
                    "at": proposal["at"],
                }
            )
            proposal_id = proposal["parent_id"]
        topic = self.topics[clause["topic_id"]]
        authorizations = [
            {**entry, "covered": topic["scope"] in set(entry["scopes"])}
            for entry in self.authorizations.get(clause["side"], [])
        ]
        agreement = next((a for a in self.agreements.values() if clause_id in a["clause_ids"]), None)
        result: dict[str, Any] = {
            "clause": {
                "clause_id": clause["clause_id"],
                "topic_id": clause["topic_id"],
                "side": clause["side"],
                "text": clause["text"],
                "status": clause["status"],
                "public": clause["public"],
            },
            "status_history": list(clause["history"]),
            "proposal_chain": chain,
            "authorizations": authorizations,
            "legal_bases": list(clause["legal_bases"]),
        }
        if agreement:
            result["agreement"] = {
                "agreement_id": agreement["agreement_id"],
                "status": agreement["status"],
                "text_hash": agreement["text_hash"],
                "confirmations": dict(agreement["confirmations"]),
                "signatures": dict(agreement["signatures"]),
            }
            result["milestones"] = [
                {"milestone_id": m["milestone_id"], "title": m["title"], "due": m["due"], "status": m["status"], "result": m["result"]}
                for m in self.milestones.values()
                if m["agreement_id"] == agreement["agreement_id"]
            ]
        return result

    # ------------------------------------------------------------------
    # 内部校验与状态访问
    # ------------------------------------------------------------------

    @staticmethod
    def _check_side(side: str) -> None:
        if side not in SIDES:
            raise NegotiationError(f"未知参与方：{side}")

    def _rep(self, rep_id: str) -> dict[str, Any]:
        rep = self.representatives.get(rep_id)
        if not rep:
            raise NegotiationError(f"代表不存在：{rep_id}")
        return rep

    def _active_rep(self, rep_id: str) -> dict[str, Any]:
        rep = self._rep(rep_id)
        if not rep["active"]:
            raise NegotiationError(f"代表已更换，不能继续履职：{rep_id}")
        return rep

    def _check_not_recused(self, rep_id: str, topic_id: str) -> None:
        if (rep_id, topic_id) in self.recusals:
            raise NegotiationError("存在回避关系或利益冲突，不能就该议题行动")

    def _topic(self, topic_id: str) -> dict[str, Any]:
        topic = self.topics.get(topic_id)
        if not topic:
            raise NegotiationError(f"议题不存在：{topic_id}")
        return topic

    def _clause(self, clause_id: str) -> dict[str, Any]:
        clause = self.clauses.get(clause_id)
        if not clause:
            raise NegotiationError(f"条款不存在：{clause_id}")
        return clause

    def _material(self, material_id: str) -> dict[str, Any]:
        material = self.materials.get(material_id)
        if not material:
            raise NegotiationError(f"材料不存在：{material_id}")
        return material

    def _commitment(self, commitment_id: str) -> dict[str, Any]:
        commitment = self.commitments.get(commitment_id)
        if not commitment:
            raise NegotiationError(f"承诺不存在：{commitment_id}")
        return commitment

    def _agreement(self, agreement_id: str) -> dict[str, Any]:
        agreement = self.agreements.get(agreement_id)
        if not agreement:
            raise NegotiationError(f"协议不存在：{agreement_id}")
        return agreement

    def _open_round(self) -> dict[str, Any]:
        for rnd in self.rounds.values():
            if rnd["status"] == ROUND_OPEN:
                return rnd
        raise NegotiationError("当前没有进行中的会议轮次")

    def _latest_proposal(self, side: str, topic_id: str) -> dict[str, Any] | None:
        latest = None
        for proposal in self.proposals.values():
            if proposal["side"] == side and proposal["topic_id"] == topic_id:
                latest = proposal
        return latest

    def _set_clause_status(self, clause: dict[str, Any], status: str, note: str, at: str) -> None:
        clause["status"] = status
        clause["history"].append({"status": status, "at": at, "note": note})

    def _maybe_finalize(self, at: str | None) -> None:
        """全部议题冻结时汇总共同条款形成最终文本，等待双方各自确认。"""
        if not self.topics or any(t["status"] != TOPIC_FROZEN for t in self.topics.values()):
            return
        if any(a["status"] in (AGREEMENT_COLLECTING, AGREEMENT_FORMED, AGREEMENT_EFFECTIVE) for a in self.agreements.values()):
            return
        clause_ids = sorted(c["clause_id"] for c in self.clauses.values() if c["status"] == CLAUSE_FROZEN)
        summary = [
            {
                "clause_id": cid,
                "topic_id": self.clauses[cid]["topic_id"],
                "text": self.clauses[cid]["text"],
                "legal_bases": self.clauses[cid]["legal_bases"],
            }
            for cid in clause_ids
        ]
        text_hash = hashlib.sha256(_canonical(summary).encode("utf-8")).hexdigest()
        self._record(
            "final_text_ready",
            {"agreement_id": f"agr-{text_hash[:12]}", "text_hash": text_hash, "clause_ids": clause_ids},
            at=at,
        )

    # ------------------------------------------------------------------
    # 事件折叠
    # ------------------------------------------------------------------

    def _on_representative_registered(self, p: dict[str, Any], at: str) -> None:
        self.representatives[p["rep_id"]] = {
            "rep_id": p["rep_id"],
            "name": p["name"],
            "role": p["role"],
            "side": p["side"],
            "qualifications": list(p["qualifications"]),
            "active": True,
            "replaced_by": None,
        }

    def _on_representative_replaced(self, p: dict[str, Any], at: str) -> None:
        old = self.representatives[p["old_rep_id"]]
        old["active"] = False
        old["replaced_by"] = p["new_rep_id"]
        self.representatives[p["new_rep_id"]] = {
            "rep_id": p["new_rep_id"],
            "name": p["name"],
            "role": old["role"],
            "side": old["side"],
            "qualifications": list(p["qualifications"]),
            "active": True,
            "replaced_by": None,
        }

    def _on_recusal_declared(self, p: dict[str, Any], at: str) -> None:
        self.recusals.add((p["rep_id"], p["topic_id"]))

    def _apply_authorization(self, p: dict[str, Any], at: str, kind: str) -> None:
        entry = {"side": p["side"], "version": p["version"], "scopes": list(p["scopes"]), "kind": kind, "at": at}
        if "reason" in p:
            entry["reason"] = p["reason"]
        if "note" in p:
            entry["note"] = p["note"]
        self.authorizations[p["side"]].append(entry)
        scopes = set(p["scopes"])
        for clause in self.clauses.values():
            if clause["side"] != p["side"] or clause["status"] not in (CLAUSE_PROPOSED, CLAUSE_PENDING):
                continue
            topic_scope = self.topics[clause["topic_id"]]["scope"]
            target = CLAUSE_PROPOSED if topic_scope in scopes else CLAUSE_PENDING
            if clause["status"] != target:
                note = "授权覆盖，暂存条款进入协商" if target == CLAUSE_PROPOSED else "授权收窄，条款暂存待确认"
                self._set_clause_status(clause, target, note, at)

    def _on_authorization_granted(self, p: dict[str, Any], at: str) -> None:
        self._apply_authorization(p, at, "granted")

    def _on_authorization_narrowed(self, p: dict[str, Any], at: str) -> None:
        self._apply_authorization(p, at, "narrowed")

    def _on_topic_opened(self, p: dict[str, Any], at: str) -> None:
        self.topics[p["topic_id"]] = {"topic_id": p["topic_id"], "title": p["title"], "scope": p["scope"], "status": TOPIC_OPEN}

    def _on_material_submitted(self, p: dict[str, Any], at: str) -> None:
        self.materials[p["material_id"]] = {
            "material_id": p["material_id"],
            "provider_id": p["provider_id"],
            "side": p["side"],
            "topic_id": p["topic_id"],
            "summary": p["summary"],
            "confidential": p["confidential"],
            "status": MATERIAL_SUBMITTED,
            "acknowledgements": [],
        }

    def _on_material_acknowledged(self, p: dict[str, Any], at: str) -> None:
        material = self.materials[p["material_id"]]
        material["acknowledgements"].append(p["rep_id"])
        material["status"] = MATERIAL_CORROBORATED

    def _on_round_opened(self, p: dict[str, Any], at: str) -> None:
        self.rounds[p["round_no"]] = {"no": p["round_no"], "status": ROUND_OPEN, "receipts": []}

    def _on_round_suspended(self, p: dict[str, Any], at: str) -> None:
        rnd = self.rounds[p["round_no"]]
        rnd["status"] = ROUND_SUSPENDED
        rnd["suspended_reason"] = p["reason"]

    def _on_round_resumed(self, p: dict[str, Any], at: str) -> None:
        self.rounds[p["round_no"]]["status"] = ROUND_OPEN

    def _on_round_closed(self, p: dict[str, Any], at: str) -> None:
        self.rounds[p["round_no"]]["status"] = ROUND_CLOSED

    def _on_receipt_recorded(self, p: dict[str, Any], at: str) -> None:
        self.receipts[p["receipt_key"]] = {"round_no": p["round_no"], "rep_id": p["rep_id"], "at": at}
        self.rounds[p["round_no"]]["receipts"].append(p["receipt_key"])

    def _on_proposal_submitted(self, p: dict[str, Any], at: str) -> None:
        self.proposals[p["proposal_id"]] = {
            "proposal_id": p["proposal_id"],
            "rep_id": p["rep_id"],
            "side": p["side"],
            "topic_id": p["topic_id"],
            "round_no": p["round_no"],
            "parent_id": p["parent_id"],
            "clause_ids": [c["clause_id"] for c in p["clauses"]],
            "note": p["note"],
            "at": at,
        }
        for clause in self.clauses.values():
            if clause["side"] == p["side"] and clause["topic_id"] == p["topic_id"] and clause["status"] == CLAUSE_PROPOSED:
                self._set_clause_status(clause, CLAUSE_SUPERSEDED, "被本方新一版提案取代", at)
        for record in p["clauses"]:
            status = CLAUSE_PROPOSED if record["in_scope"] else CLAUSE_PENDING
            note = "提案提交" if record["in_scope"] else "超出授权范围，暂存待确认"
            clause = {
                "clause_id": record["clause_id"],
                "proposal_id": p["proposal_id"],
                "side": p["side"],
                "topic_id": p["topic_id"],
                "text": record["text"],
                "legal_bases": list(record["legal_bases"]),
                "public": record["public"],
                "status": "",
                "acceptances": [p["side"]],
                "history": [],
            }
            self.clauses[record["clause_id"]] = clause
            self._set_clause_status(clause, status, note, at)

    def _on_clause_withdrawn(self, p: dict[str, Any], at: str) -> None:
        self._set_clause_status(self.clauses[p["clause_id"]], CLAUSE_WITHDRAWN, "本方撤回", at)

    def _on_clause_accepted(self, p: dict[str, Any], at: str) -> None:
        clause = self.clauses[p["clause_id"]]
        if p["side"] not in clause["acceptances"]:
            clause["acceptances"].append(p["side"])

    def _on_clause_frozen(self, p: dict[str, Any], at: str) -> None:
        self._set_clause_status(self.clauses[p["clause_id"]], CLAUSE_FROZEN, "双方接受，共同条款冻结", at)
        self.topics[p["topic_id"]]["status"] = TOPIC_FROZEN

    def _on_commitment_recorded(self, p: dict[str, Any], at: str) -> None:
        self.commitments[p["commitment_id"]] = {
            "commitment_id": p["commitment_id"],
            "rep_id": p["rep_id"],
            "side": p["side"],
            "text": p["text"],
            "valid_until": p["valid_until"],
            "proposal_id": p["proposal_id"],
            "status": COMMITMENT_PENDING,
        }

    def _on_commitment_confirmed(self, p: dict[str, Any], at: str) -> None:
        self.commitments[p["commitment_id"]]["status"] = COMMITMENT_CONFIRMED

    def _on_commitment_expired(self, p: dict[str, Any], at: str) -> None:
        self.commitments[p["commitment_id"]]["status"] = COMMITMENT_EXPIRED

    def _on_commitment_renewed(self, p: dict[str, Any], at: str) -> None:
        commitment = self.commitments[p["commitment_id"]]
        commitment["status"] = COMMITMENT_PENDING
        commitment["valid_until"] = p["valid_until"]

    def _on_common_text_promoted(self, p: dict[str, Any], at: str) -> None:
        self.common_texts.append(
            {"version": p["version"], "clause_ids": list(p["clause_ids"]), "mediator_id": p["mediator_id"], "note": p["note"], "at": at}
        )

    def _on_final_text_ready(self, p: dict[str, Any], at: str) -> None:
        self.agreements[p["agreement_id"]] = {
            "agreement_id": p["agreement_id"],
            "text_hash": p["text_hash"],
            "clause_ids": list(p["clause_ids"]),
            "confirmations": {},
            "signatures": {},
            "status": AGREEMENT_COLLECTING,
        }

    def _on_final_text_confirmed(self, p: dict[str, Any], at: str) -> None:
        self.agreements[p["agreement_id"]]["confirmations"][p["side"]] = p["text_hash"]

    def _on_confirmation_withdrawn(self, p: dict[str, Any], at: str) -> None:
        self.agreements[p["agreement_id"]]["confirmations"].pop(p["side"], None)

    def _on_agreement_formed(self, p: dict[str, Any], at: str) -> None:
        self.agreements[p["agreement_id"]]["status"] = AGREEMENT_FORMED

    def _on_agreement_effective(self, p: dict[str, Any], at: str) -> None:
        self.agreements[p["agreement_id"]]["status"] = AGREEMENT_EFFECTIVE

    def _on_agreement_cancelled(self, p: dict[str, Any], at: str) -> None:
        agreement = self.agreements[p["agreement_id"]]
        agreement["status"] = AGREEMENT_CANCELLED
        agreement["cancelled_by"] = p["side"]
        agreement["cancel_reason"] = p["reason"]

    def _on_dispute_opened(self, p: dict[str, Any], at: str) -> None:
        self.disputes.append({"agreement_id": p["agreement_id"], "reason": p["reason"], "at": at})
        self.agreements[p["agreement_id"]]["status"] = AGREEMENT_DISPUTED

    def _on_signature_recorded(self, p: dict[str, Any], at: str) -> None:
        self.agreements[p["agreement_id"]]["signatures"][p["side"]] = p["signature_key"]

    def _on_milestone_registered(self, p: dict[str, Any], at: str) -> None:
        self.milestones[p["milestone_id"]] = {
            "milestone_id": p["milestone_id"],
            "agreement_id": p["agreement_id"],
            "title": p["title"],
            "due": p["due"],
            "status": MILESTONE_PENDING,
            "result": None,
        }

    def _on_milestone_completed(self, p: dict[str, Any], at: str) -> None:
        milestone = self.milestones[p["milestone_id"]]
        milestone["status"] = MILESTONE_DONE
        milestone["result"] = p["result"]
