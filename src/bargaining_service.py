"""集体协商过程服务：代表授权、逐轮提案、共同文本与协议形成。

服务只依赖标准库，不连接外部业务系统。全部状态可通过
snapshot/restore 持久化，期限届满或服务重启后，待确认承诺、
调解事项和履行节点继续处理。任何过程事件只追加、不抹除。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


PARTIES = ("enterprise", "labor")
ROLES = ("representative", "mediator", "supervisor")


def other_party(party: str) -> str:
    if party not in PARTIES:
        raise ValueError("未知协商方")
    return PARTIES[1] if party == PARTIES[0] else PARTIES[0]


class BargainingService:
    """记录一次集体协商从授权到协议履行的完整过程。"""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = state if state is not None else self._fresh()

    @staticmethod
    def _fresh() -> dict[str, Any]:
        return {
            "now": 0,
            "representatives": {},
            "topics": {},
            "materials": {},
            "proposals": {},
            "parked": {},
            "rounds": {},
            "drafts": {},
            "frozen": {},
            "agreements": [],
            "fulfillment": {},
            "disputes": [],
            "events": [],
            "counters": {"parked": 0, "proposal": 0},
        }

    @classmethod
    def restore(cls, data: dict[str, Any]) -> "BargainingService":
        """从快照恢复服务，重启后待办事项继续处理。"""
        return cls(json.loads(json.dumps(data, ensure_ascii=False)))

    def snapshot(self) -> dict[str, Any]:
        """生成可 JSON 序列化的完整状态快照。"""
        return json.loads(json.dumps(self.state, ensure_ascii=False))

    # ---- 基础工具 ----

    def _touch(self, now: int | None) -> None:
        if now is not None:
            if not isinstance(now, int) or isinstance(now, bool) or now < 0:
                raise ValueError("时间戳无效")
            self.state["now"] = max(self.state["now"], now)

    def _event(self, event_kind: str, **data: Any) -> None:
        self.state["events"].append(
            {
                "seq": len(self.state["events"]) + 1,
                "kind": event_kind,
                "at": self.state["now"],
                "data": data,
            }
        )

    def _rep(self, rep_id: str) -> dict[str, Any]:
        rep = self.state["representatives"].get(rep_id)
        if rep is None:
            raise ValueError("代表不存在")
        return rep

    def _active_rep(self, rep_id: str) -> dict[str, Any]:
        rep = self._rep(rep_id)
        if rep["status"] != "active":
            raise ValueError("代表资格已失效")
        return rep

    @staticmethod
    def _recused(rep: dict[str, Any], *, topic: str | None = None, party: str | None = None) -> bool:
        for entry in rep["recusals"]:
            if topic is not None and entry.get("topic") == topic:
                return True
            if party is not None and entry.get("party") == party:
                return True
        return False

    @staticmethod
    def _scope(rep: dict[str, Any]) -> list[str]:
        return rep["mandates"][-1]["scope"] if rep["mandates"] else []

    # ---- 代表资格、回避与授权 ----

    def add_representative(
        self,
        rep_id: str,
        party: str | None,
        qualification: str,
        now: int | None = None,
        role: str = "representative",
    ) -> dict[str, Any]:
        """登记代表、调解人员或监督人员，保存其资格证明。"""
        self._touch(now)
        if role not in ROLES:
            raise ValueError("未知角色")
        if role == "representative" and party not in PARTIES:
            raise ValueError("代表必须属于协商一方")
        if rep_id in self.state["representatives"]:
            raise ValueError("代表编号已存在")
        if not qualification.strip():
            raise ValueError("代表资格证明不能为空")
        rep = {
            "id": rep_id,
            "party": party if role == "representative" else None,
            "role": role,
            "qualification": qualification,
            "status": "active",
            "recusals": [],
            "mandates": [],
            "replaced_by": None,
            "added_at": self.state["now"],
        }
        self.state["representatives"][rep_id] = rep
        self._event("representative_added", rep=rep_id, party=rep["party"], role=role)
        return rep

    def replace_representative(self, old_id: str, new_id: str, qualification: str, now: int | None = None) -> dict[str, Any]:
        """更换代表：原代表行为全部保留，新代表承接当前授权范围。"""
        self._touch(now)
        old = self._active_rep(old_id)
        new = self.add_representative(new_id, old["party"], qualification, now, role=old["role"])
        if old["mandates"]:
            new["mandates"].append(
                {"scope": list(self._scope(old)), "kind": "initial", "at": self.state["now"]}
            )
        old["status"] = "replaced"
        old["replaced_by"] = new_id
        self._event("representative_replaced", old=old_id, new=new_id)
        return new

    def declare_recusal(
        self,
        rep_id: str,
        now: int | None = None,
        topic: str | None = None,
        party: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """登记回避关系（利益冲突），登记后相关操作被拒绝，记录不抹除。"""
        self._touch(now)
        rep = self._rep(rep_id)
        if topic is None and party is None:
            raise ValueError("回避关系须指明议题或当事方")
        if topic is not None and topic not in self.state["topics"]:
            raise ValueError("议题不存在")
        if party is not None and party not in PARTIES:
            raise ValueError("未知协商方")
        entry = {"topic": topic, "party": party, "reason": reason, "at": self.state["now"]}
        rep["recusals"].append(entry)
        self._event("recusal_declared", rep=rep_id, topic=topic, party=party)
        return entry

    def set_mandate(self, rep_id: str, scope: list[str], now: int | None = None) -> dict[str, Any]:
        """登记成员授权范围；收窄时在途条款转入暂存，历史授权不抹除。"""
        self._touch(now)
        rep = self._active_rep(rep_id)
        if rep["role"] != "representative":
            raise ValueError("只有协商代表可以接受授权")
        if any(not isinstance(item, str) or not item.strip() for item in scope):
            raise ValueError("授权范围包含空内容")
        new_scope = sorted(set(scope))
        old_scope = self._scope(rep)
        if new_scope == old_scope:
            return rep["mandates"][-1] if rep["mandates"] else {}
        old_set, new_set = set(old_scope), set(new_scope)
        if not old_set:
            kind = "initial"
        elif new_set < old_set:
            kind = "narrowed"
        elif new_set > old_set:
            kind = "expanded"
        else:
            kind = "adjusted"
        mandate = {"scope": new_scope, "kind": kind, "at": self.state["now"]}
        rep["mandates"].append(mandate)
        self._event("mandate_set", rep=rep_id, kind=kind, scope=new_scope)
        if kind == "narrowed":
            removed = old_set - new_set
            for proposal in self.state["proposals"].values():
                if proposal["party"] != rep["party"] or proposal["status"] != "active":
                    continue
                for clause in proposal["clauses"]:
                    if clause["status"] == "active" and clause["subject"] in removed:
                        self._park_clause(proposal, clause, "授权收窄")
        return mandate

    # ---- 议题与保密材料 ----

    def add_topic(self, topic_id: str, title: str, legal_basis: list[str], now: int | None = None) -> dict[str, Any]:
        """登记议题及其法律依据。"""
        self._touch(now)
        if topic_id in self.state["topics"]:
            raise ValueError("议题编号已存在")
        if not title.strip():
            raise ValueError("议题名称不能为空")
        if not legal_basis or any(not isinstance(item, str) or not item.strip() for item in legal_basis):
            raise ValueError("议题须附法律依据")
        topic = {"id": topic_id, "title": title, "legal_basis": list(legal_basis), "status": "open"}
        self.state["topics"][topic_id] = topic
        self._event("topic_added", topic=topic_id)
        return topic

    def submit_material(
        self, material_id: str, rep_id: str, title: str, confidential: bool, now: int | None = None
    ) -> dict[str, Any]:
        """提交成本测算等材料；保密材料不进入公开记录。"""
        self._touch(now)
        rep = self._active_rep(rep_id)
        if material_id in self.state["materials"]:
            raise ValueError("材料编号已存在")
        material = {
            "id": material_id,
            "title": title,
            "provider": rep_id,
            "provider_party": rep["party"],
            "confidential": bool(confidential),
            "status": "submitted",
            "corroborations": [],
            "at": self.state["now"],
        }
        self.state["materials"][material_id] = material
        self._event("material_submitted", material=material_id, provider=rep_id, confidential=bool(confidential))
        return material

    def corroborate_material(self, material_id: str, rep_id: str, now: int | None = None) -> dict[str, Any]:
        """他方佐证材料成立；资料提供者不能单独认定自己数据成立。"""
        self._touch(now)
        material = self.state["materials"].get(material_id)
        if material is None:
            raise ValueError("材料不存在")
        rep = self._active_rep(rep_id)
        if rep["party"] == material["provider_party"]:
            raise ValueError("资料提供者不能单独认定自己数据成立")
        if any(item["rep"] == rep_id for item in material["corroborations"]):
            return material
        material["corroborations"].append(
            {"rep": rep_id, "party": rep["party"], "at": self.state["now"]}
        )
        material["status"] = "established"
        self._event("material_corroborated", material=material_id, rep=rep_id)
        return material

    # ---- 会议轮次与回执 ----

    def open_round(
        self, round_id: str, number: int, now: int | None = None, resumed_from: str | None = None
    ) -> dict[str, Any]:
        """开启会议轮次；重新开局可引用此前轮次，历史轮次不抹除。"""
        self._touch(now)
        if round_id in self.state["rounds"]:
            raise ValueError("轮次编号已存在")
        if resumed_from is not None and resumed_from not in self.state["rounds"]:
            raise ValueError("被恢复的轮次不存在")
        session = {
            "id": round_id,
            "number": number,
            "status": "open",
            "receipts": {},
            "opened_at": self.state["now"],
            "resumed_from": resumed_from,
        }
        self.state["rounds"][round_id] = session
        self._event("round_opened", round=round_id, number=number, resumed_from=resumed_from)
        return session

    def record_receipt(self, round_id: str, party: str, receipt_key: str, now: int | None = None) -> dict[str, Any]:
        """登记会议回执；重复回执按键幂等归并。"""
        self._touch(now)
        session = self.state["rounds"].get(round_id)
        if session is None:
            raise ValueError("轮次不存在")
        if party not in PARTIES:
            raise ValueError("未知协商方")
        key = f"{party}:{receipt_key}"
        existing = session["receipts"].get(key)
        if existing is not None:
            return existing
        receipt = {"party": party, "key": receipt_key, "at": self.state["now"]}
        session["receipts"][key] = receipt
        self._event("receipt_recorded", round=round_id, party=party, key=receipt_key)
        return receipt

    def suspend_round(self, round_id: str, now: int | None = None) -> dict[str, Any]:
        """中止会谈，过程记录保留。"""
        self._touch(now)
        session = self.state["rounds"].get(round_id)
        if session is None:
            raise ValueError("轮次不存在")
        if session["status"] != "open":
            raise ValueError("只能中止进行中的轮次")
        session["status"] = "suspended"
        self._event("round_suspended", round=round_id)
        return session

    def resume_round(self, round_id: str, now: int | None = None) -> dict[str, Any]:
        """恢复被中止的轮次。"""
        self._touch(now)
        session = self.state["rounds"].get(round_id)
        if session is None:
            raise ValueError("轮次不存在")
        if session["status"] != "suspended":
            raise ValueError("只能恢复被中止的轮次")
        session["status"] = "open"
        self._event("round_resumed", round=round_id)
        return session

    # ---- 提案与暂存 ----

    def _latest_proposal(self, party: str, topic_id: str) -> dict[str, Any] | None:
        candidates = [
            item
            for item in self.state["proposals"].values()
            if item["party"] == party and item["topic"] == topic_id
        ]
        return max(candidates, key=lambda item: item["seq"], default=None)

    def _park_clause(self, proposal: dict[str, Any], clause: dict[str, Any], reason: str) -> dict[str, Any]:
        self.state["counters"]["parked"] += 1
        parked_id = f"P{self.state['counters']['parked']}"
        clause["status"] = "parked"
        record = {
            "id": parked_id,
            "proposal": proposal["id"],
            "party": proposal["party"],
            "topic": proposal["topic"],
            "clause_key": clause["key"],
            "subject": clause["subject"],
            "text": clause["text"],
            "reason": reason,
            "status": "pending",
            "created_at": self.state["now"],
            "resolved_at": None,
            "resolved_by": None,
        }
        self.state["parked"][parked_id] = record
        self._event("clause_parked", parked=parked_id, proposal=proposal["id"], reason=reason)
        return record

    def submit_proposal(
        self,
        proposal_id: str,
        rep_id: str,
        topic_id: str,
        clauses: list[dict[str, Any]],
        now: int | None = None,
        based_on: str | None = None,
        valid_until: int | None = None,
    ) -> dict[str, Any]:
        """提交提案；让步必须引用上一版提案，每条条款须标明可公开内容。

        超出当前授权范围的条款只暂存待确认，不进入在途提案。
        """
        self._touch(now)
        rep = self._active_rep(rep_id)
        if rep["role"] != "representative":
            raise ValueError("调解与监督人员不能提交本方提案")
        topic = self.state["topics"].get(topic_id)
        if topic is None:
            raise ValueError("议题不存在")
        if topic["status"] != "open":
            raise ValueError("议题已关闭")
        if self._recused(rep, topic=topic_id):
            raise ValueError("存在回避关系，不能参与该议题")
        if proposal_id in self.state["proposals"]:
            raise ValueError("提案编号已存在")
        if not clauses:
            raise ValueError("提案条款不能为空")
        latest = self._latest_proposal(rep["party"], topic_id)
        if latest is None and based_on is not None:
            raise ValueError("首版提案无上一版可引用")
        if latest is not None and based_on != latest["id"]:
            raise ValueError("让步必须引用上一版提案")
        scope = set(self._scope(rep))
        prepared: list[dict[str, Any]] = []
        for clause in clauses:
            for field in ("key", "subject", "text"):
                if not isinstance(clause.get(field), str) or not clause[field].strip():
                    raise ValueError("提案条款内容不完整")
            if not isinstance(clause.get("public"), bool):
                raise ValueError("提案条款须标明可公开内容")
            prepared.append(
                {
                    "key": clause["key"],
                    "subject": clause["subject"],
                    "text": clause["text"],
                    "public": clause["public"],
                    "status": "active" if clause["subject"] in scope else "pending-park",
                }
            )
        if latest is not None:
            latest["status"] = "superseded"
            for clause in latest["clauses"]:
                if clause["status"] == "active":
                    clause["status"] = "superseded"
        self.state["counters"]["proposal"] += 1
        proposal = {
            "id": proposal_id,
            "seq": self.state["counters"]["proposal"],
            "party": rep["party"],
            "rep": rep_id,
            "topic": topic_id,
            "based_on": based_on,
            "clauses": prepared,
            "valid_until": valid_until,
            "status": "active",
            "mandate_scope": sorted(scope),
            "at": self.state["now"],
        }
        self.state["proposals"][proposal_id] = proposal
        self._event(
            "proposal_submitted",
            proposal=proposal_id,
            party=rep["party"],
            topic=topic_id,
            based_on=based_on,
            valid_until=valid_until,
        )
        for clause in prepared:
            if clause["status"] == "pending-park":
                self._park_clause(proposal, clause, "超出授权范围")
        return proposal

    def confirm_parked(self, parked_id: str, rep_id: str, now: int | None = None) -> dict[str, Any]:
        """确认暂存条款：须由本方在授权范围内的代表确认。"""
        self._touch(now)
        record = self.state["parked"].get(parked_id)
        if record is None:
            raise ValueError("暂存条款不存在")
        rep = self._active_rep(rep_id)
        if rep["party"] != record["party"]:
            raise ValueError("只能由本方代表确认暂存条款")
        if record["status"] != "pending":
            raise ValueError("暂存条款已处理")
        if record["subject"] not in self._scope(rep):
            raise ValueError("暂存条款仍超出授权范围")
        record["status"] = "confirmed"
        record["resolved_at"] = self.state["now"]
        record["resolved_by"] = rep_id
        proposal = self.state["proposals"][record["proposal"]]
        for clause in proposal["clauses"]:
            if clause["key"] == record["clause_key"]:
                clause["status"] = "active" if proposal["status"] == "active" else "confirmed"
        self._event("parked_confirmed", parked=parked_id, rep=rep_id)
        return record

    def reject_parked(self, parked_id: str, rep_id: str, now: int | None = None) -> dict[str, Any]:
        """驳回暂存条款，记录保留。"""
        self._touch(now)
        record = self.state["parked"].get(parked_id)
        if record is None:
            raise ValueError("暂存条款不存在")
        rep = self._active_rep(rep_id)
        if rep["party"] != record["party"]:
            raise ValueError("只能由本方代表驳回暂存条款")
        if record["status"] != "pending":
            raise ValueError("暂存条款已处理")
        record["status"] = "rejected"
        record["resolved_at"] = self.state["now"]
        record["resolved_by"] = rep_id
        proposal = self.state["proposals"][record["proposal"]]
        for clause in proposal["clauses"]:
            if clause["key"] == record["clause_key"]:
                clause["status"] = "rejected"
        self._event("parked_rejected", parked=parked_id, rep=rep_id)
        return record

    # ---- 共同文本与条款冻结 ----

    def propose_common_text(
        self, draft_id: str, rep_id: str, topic_id: str, clause_key: str, text: str, now: int | None = None
    ) -> dict[str, Any]:
        """提出共同文本草案；调解人员只能推动共同文本，不能代写本方立场。"""
        self._touch(now)
        rep = self._active_rep(rep_id)
        if rep["role"] == "supervisor":
            raise ValueError("监督人员不能提出共同文本")
        topic = self.state["topics"].get(topic_id)
        if topic is None:
            raise ValueError("议题不存在")
        if topic["status"] != "open":
            raise ValueError("议题已关闭")
        clause_id = f"{topic_id}:{clause_key}"
        if clause_id in self.state["frozen"]:
            raise ValueError("条款已冻结")
        if draft_id in self.state["drafts"]:
            raise ValueError("草案编号已存在")
        if not text.strip():
            raise ValueError("共同文本不能为空")
        draft = {
            "id": draft_id,
            "topic": topic_id,
            "clause_key": clause_key,
            "text": text,
            "proposed_by": rep_id,
            "proposer_role": rep["role"],
            "acceptances": {},
            "status": "open",
            "at": self.state["now"],
        }
        self.state["drafts"][draft_id] = draft
        self._event("common_text_proposed", draft=draft_id, by=rep_id, role=rep["role"])
        if rep["role"] == "representative":
            if self._recused(rep, topic=topic_id):
                raise ValueError("存在回避关系，不能参与该议题")
            draft["acceptances"][rep["party"]] = {
                "rep": rep_id,
                "mandate_scope": list(self._scope(rep)),
                "at": self.state["now"],
            }
            self._event("common_text_accepted", draft=draft_id, party=rep["party"], rep=rep_id)
        return draft

    def accept_common_text(self, draft_id: str, rep_id: str, now: int | None = None) -> dict[str, Any]:
        """协商方接受共同文本；双方接受后条款冻结，未决内容继续协商。"""
        self._touch(now)
        draft = self.state["drafts"].get(draft_id)
        if draft is None:
            raise ValueError("草案不存在")
        clause_id = f"{draft['topic']}:{draft['clause_key']}"
        if draft["status"] == "frozen":
            return self.state["frozen"][clause_id]
        rep = self._active_rep(rep_id)
        if rep["role"] != "representative":
            raise ValueError("调解与监督人员不能代表协商方接受共同文本")
        if self._recused(rep, topic=draft["topic"]):
            raise ValueError("存在回避关系，不能参与该议题")
        if rep["party"] in draft["acceptances"]:
            return draft
        draft["acceptances"][rep["party"]] = {
            "rep": rep_id,
            "mandate_scope": list(self._scope(rep)),
            "at": self.state["now"],
        }
        self._event("common_text_accepted", draft=draft_id, party=rep["party"], rep=rep_id)
        if all(party in draft["acceptances"] for party in PARTIES):
            draft["status"] = "frozen"
            clause = {
                "id": clause_id,
                "topic": draft["topic"],
                "clause_key": draft["clause_key"],
                "text": draft["text"],
                "draft": draft_id,
                "acceptances": {party: dict(item) for party, item in draft["acceptances"].items()},
                "frozen_at": self.state["now"],
            }
            self.state["frozen"][clause_id] = clause
            self._event("clause_frozen", clause=clause_id, draft=draft_id)
            return clause
        return draft

    # ---- 协议形成、签署与撤回 ----

    def _current_agreement(self) -> dict[str, Any] | None:
        return self.state["agreements"][-1] if self.state["agreements"] else None

    def _digest(self, clause_ids: list[str]) -> str:
        payload = {
            "clauses": [[cid, self.state["frozen"][cid]["text"]] for cid in clause_ids],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def assemble_agreement(self, now: int | None = None) -> dict[str, Any]:
        """把已冻结的共同条款汇总为最终文本，等待双方各自确认。

        文本未变化时幂等返回；此前文本被撤回或进入争议时可重新汇总，
        历史版本保留。
        """
        self._touch(now)
        if not self.state["frozen"]:
            raise ValueError("尚无可汇总的共同条款")
        clause_ids = sorted(self.state["frozen"])
        current = self._current_agreement()
        if current is not None and current["status"] in ("confirming", "effective"):
            if current["clause_ids"] == clause_ids:
                return current
            raise ValueError("已有进行中的协议文本")
        agreement = {
            "version": len(self.state["agreements"]) + 1,
            "clause_ids": clause_ids,
            "digest": self._digest(clause_ids),
            "status": "confirming",
            "confirmations": {},
            "assembled_at": self.state["now"],
        }
        self.state["agreements"].append(agreement)
        self._event("agreement_assembled", version=agreement["version"], digest=agreement["digest"])
        return agreement

    def confirm_agreement(self, rep_id: str, digest: str, now: int | None = None) -> dict[str, Any]:
        """一方确认最终文本；双方确认同一文本后协议才生效。

        异步签名按方幂等归并；双方文本不一致（异文）进入争议。
        """
        self._touch(now)
        rep = self._active_rep(rep_id)
        if rep["role"] != "representative":
            raise ValueError("调解与监督人员不能确认协议")
        agreement = self._current_agreement()
        if agreement is None:
            raise ValueError("尚未汇总最终文本")
        if agreement["status"] == "effective":
            existing = agreement["confirmations"].get(rep["party"])
            if existing is not None and existing["digest"] == digest:
                return agreement
            raise ValueError("协议已生效")
        if agreement["status"] != "confirming":
            raise ValueError("协议文本当前不能确认")
        if self._recused(rep, party=other_party(rep["party"])):
            raise ValueError("存在回避关系，不能确认协议")
        existing = agreement["confirmations"].get(rep["party"])
        if existing is not None and existing["digest"] == digest:
            return agreement
        agreement["confirmations"][rep["party"]] = {
            "rep": rep_id,
            "digest": digest,
            "at": self.state["now"],
        }
        self._event(
            "agreement_confirmed",
            party=rep["party"],
            rep=rep_id,
            digest=digest,
            replaced=existing is not None,
        )
        confirmations = agreement["confirmations"]
        if all(party in confirmations for party in PARTIES):
            if all(item["digest"] == agreement["digest"] for item in confirmations.values()):
                agreement["status"] = "effective"
                agreement["effective_at"] = self.state["now"]
                self._event("agreement_effective", version=agreement["version"])
            else:
                agreement["status"] = "disputed"
                dispute = {
                    "kind": "异文",
                    "version": agreement["version"],
                    "digests": {party: item["digest"] for party, item in confirmations.items()},
                    "at": self.state["now"],
                }
                self.state["disputes"].append(dispute)
                self._event("agreement_disputed", version=agreement["version"], reason="异文")
        return agreement

    def withdraw_agreement(self, rep_id: str, now: int | None = None) -> dict[str, Any]:
        """撤回最终文本；未生效前任何一方撤回即阻止生效，不出现单方已生效。"""
        self._touch(now)
        rep = self._active_rep(rep_id)
        if rep["role"] != "representative":
            raise ValueError("调解与监督人员不能撤回协议")
        agreement = self._current_agreement()
        if agreement is None:
            raise ValueError("尚未汇总最终文本")
        if agreement["status"] == "effective":
            dispute = {
                "kind": "撤回已生效协议",
                "version": agreement["version"],
                "by": rep["party"],
                "at": self.state["now"],
            }
            self.state["disputes"].append(dispute)
            self._event("agreement_disputed", version=agreement["version"], reason="撤回已生效协议")
            return agreement
        if agreement["status"] == "withdrawn":
            return agreement
        if agreement["status"] != "confirming":
            raise ValueError("协议文本当前不能撤回")
        agreement["status"] = "withdrawn"
        agreement["withdrawn_by"] = rep["party"]
        agreement["withdrawn_at"] = self.state["now"]
        self._event("agreement_withdrawn", version=agreement["version"], by=rep["party"])
        return agreement

    # ---- 履行节点 ----

    def add_fulfillment_node(self, node_id: str, clause_id: str, due: int, now: int | None = None) -> dict[str, Any]:
        """为已冻结条款登记履行节点。"""
        self._touch(now)
        if clause_id not in self.state["frozen"]:
            raise ValueError("履行节点须对应已冻结条款")
        if node_id in self.state["fulfillment"]:
            raise ValueError("履行节点编号已存在")
        node = {
            "id": node_id,
            "clause": clause_id,
            "due": due,
            "status": "pending",
            "result": None,
            "recorded_at": None,
            "late": False,
        }
        self.state["fulfillment"][node_id] = node
        self._event("fulfillment_node_added", node=node_id, clause=clause_id, due=due)
        return node

    def record_fulfillment(self, node_id: str, result: str, now: int | None = None) -> dict[str, Any]:
        """记录履行结果；逾期节点继续处理并标记迟延。"""
        self._touch(now)
        node = self.state["fulfillment"].get(node_id)
        if node is None:
            raise ValueError("履行节点不存在")
        if node["status"] == "done":
            if node["result"] == result:
                return node
            raise ValueError("履行结果已记录")
        if not result.strip():
            raise ValueError("履行结果不能为空")
        node["status"] = "done"
        node["result"] = result
        node["recorded_at"] = self.state["now"]
        node["late"] = self.state["now"] > node["due"]
        self._event("fulfillment_recorded", node=node_id, late=node["late"])
        return node

    # ---- 待办、下一步与追溯 ----

    def pending_items(self) -> dict[str, Any]:
        """汇总期限届满或重启后仍需处理的事项。"""
        now = self.state["now"]
        agreement = self._current_agreement()
        awaiting: dict[str, bool] = {party: False for party in PARTIES}
        if agreement is not None and agreement["status"] == "confirming":
            for party in PARTIES:
                awaiting[party] = party not in agreement["confirmations"]
        lapsed: dict[str, list[str]] = {party: [] for party in PARTIES}
        for proposal in self.state["proposals"].values():
            if (
                proposal["status"] == "active"
                and proposal["valid_until"] is not None
                and proposal["valid_until"] < now
            ):
                lapsed[proposal["party"]].append(proposal["id"])
        drafts_awaiting: dict[str, list[str]] = {party: [] for party in PARTIES}
        for draft in self.state["drafts"].values():
            if draft["status"] == "open":
                for party in PARTIES:
                    if party not in draft["acceptances"]:
                        drafts_awaiting[party].append(draft["id"])
        return {
            "parked": [dict(item) for item in self.state["parked"].values() if item["status"] == "pending"],
            "drafts_awaiting": drafts_awaiting,
            "agreement_status": agreement["status"] if agreement else None,
            "agreement_awaiting": awaiting,
            "lapsed": lapsed,
            "fulfillment_open": [
                node["id"] for node in self.state["fulfillment"].values() if node["status"] == "pending"
            ],
            "fulfillment_missed": [
                node["id"]
                for node in self.state["fulfillment"].values()
                if node["status"] == "pending" and node["due"] < now
            ],
            "disputes": [dict(item) for item in self.state["disputes"]],
            "topics_open": [
                topic["id"] for topic in self.state["topics"].values() if topic["status"] == "open"
            ],
        }

    def next_steps(self, actor: str) -> list[str]:
        """参与方查看适合自身的下一步。"""
        items = self.pending_items()
        steps: list[str] = []
        if actor in PARTIES:
            for record in items["parked"]:
                if record["party"] == actor:
                    steps.append(f"暂存条款{record['id']}（{record['subject']}）待本方确认或驳回")
            for draft_id in items["drafts_awaiting"][actor]:
                steps.append(f"共同文本草案{draft_id}待本方接受")
            if items["agreement_awaiting"][actor]:
                steps.append("最终文本待本方确认")
            for proposal_id in items["lapsed"][actor]:
                steps.append(f"提案{proposal_id}的承诺已到期，需续延或提出新版本")
            for topic_id in items["topics_open"]:
                if self._latest_proposal(actor, topic_id) is None:
                    steps.append(f"议题{topic_id}尚无本方在途提案")
        elif actor == "mediator":
            for topic_id in items["topics_open"]:
                has_draft = any(
                    draft["topic"] == topic_id and draft["status"] == "open"
                    for draft in self.state["drafts"].values()
                )
                both_sides = all(
                    self._latest_proposal(party, topic_id) is not None for party in PARTIES
                )
                if both_sides and not has_draft:
                    steps.append(f"议题{topic_id}双方提案齐备，可推动共同文本")
            if items["disputes"]:
                steps.append("存在争议事项，需组织调解")
        elif actor == "supervisor":
            steps.append("可借助audit_clause从协议条款追溯授权、提案变化、法律依据和履行结果")
            if items["disputes"]:
                steps.append("存在争议记录，需监督处理")
        else:
            raise ValueError("未知参与方")
        if actor in PARTIES or actor == "supervisor":
            for node_id in items["fulfillment_missed"]:
                steps.append(f"履行节点{node_id}已逾期，仍需记录履行结果")
        return steps

    def audit_clause(self, clause_id: str) -> dict[str, Any]:
        """监督追溯：从协议条款追到授权、提案变化、法律依据和履行结果。"""
        clause = self.state["frozen"].get(clause_id)
        if clause is None:
            raise ValueError("条款不存在或未冻结")
        topic = self.state["topics"][clause["topic"]]
        lineage: dict[str, list[dict[str, Any]]] = {party: [] for party in PARTIES}
        for proposal in sorted(self.state["proposals"].values(), key=lambda item: item["seq"]):
            if proposal["topic"] != clause["topic"]:
                continue
            for clause_item in proposal["clauses"]:
                if clause_item["key"] == clause["clause_key"]:
                    lineage[proposal["party"]].append(
                        {
                            "proposal": proposal["id"],
                            "based_on": proposal["based_on"],
                            "text": clause_item["text"],
                            "status": clause_item["status"],
                            "mandate_scope": list(proposal["mandate_scope"]),
                            "at": proposal["at"],
                        }
                    )
        return {
            "clause": dict(clause),
            "legal_basis": list(topic["legal_basis"]),
            "acceptances": {party: dict(item) for party, item in clause["acceptances"].items()},
            "proposal_lineage": lineage,
            "fulfillment": [
                dict(node) for node in self.state["fulfillment"].values() if node["clause"] == clause_id
            ],
            "agreement_versions": [
                agreement["version"]
                for agreement in self.state["agreements"]
                if clause_id in agreement["clause_ids"]
            ],
        }

    def public_record(self) -> dict[str, Any]:
        """公开记录：只包含标明可公开的内容，保密成本材料不进入。"""
        proposals = []
        for proposal in sorted(self.state["proposals"].values(), key=lambda item: item["seq"]):
            public_clauses = [
                {"key": clause["key"], "text": clause["text"]}
                for clause in proposal["clauses"]
                if clause["public"]
            ]
            if public_clauses:
                proposals.append(
                    {
                        "proposal": proposal["id"],
                        "party": proposal["party"],
                        "topic": proposal["topic"],
                        "clauses": public_clauses,
                    }
                )
        agreement = self._current_agreement()
        return {
            "topics": [
                {"id": topic["id"], "title": topic["title"]}
                for topic in self.state["topics"].values()
            ],
            "frozen_clauses": [
                {"id": clause["id"], "text": clause["text"]}
                for clause in self.state["frozen"].values()
            ],
            "proposals": proposals,
            "agreement": (
                {"version": agreement["version"], "status": agreement["status"]}
                if agreement
                else None
            ),
        }
