"""集体协商服务的行为测试：授权、提案链、冻结、协议、幂等与追溯。"""

import tempfile
import unittest
from pathlib import Path

from src.negotiation_service import (
    AGREEMENT_CANCELLED,
    AGREEMENT_COLLECTING,
    AGREEMENT_DISPUTED,
    AGREEMENT_EFFECTIVE,
    AGREEMENT_FORMED,
    CLAUSE_FROZEN,
    CLAUSE_PENDING,
    CLAUSE_PROPOSED,
    CLAUSE_SUPERSEDED,
    COMMITMENT_CONFIRMED,
    COMMITMENT_EXPIRED,
    COMMITMENT_PENDING,
    EMPLOYER,
    MATERIAL_CORROBORATED,
    ROLE_MEDIATOR,
    ROLE_REPRESENTATIVE,
    ROLE_SUPERVISOR,
    ROLE_UNION_STAFF,
    TOPIC_FROZEN,
    TOPIC_OPEN,
    WORKER,
    EventStore,
    NegotiationError,
    NegotiationService,
)


def boot() -> NegotiationService:
    """搭建含双方代表、工会工作人员、调解与监督人员的服务。"""
    svc = NegotiationService()
    svc.register_representative("wr1", "职工代表甲", ROLE_REPRESENTATIVE, side=WORKER, qualifications=["职工民主选举产生"])
    svc.register_representative("er1", "企业代表甲", ROLE_REPRESENTATIVE, side=EMPLOYER, qualifications=["企业法定代表人书面授权"])
    svc.register_representative("er2", "企业代表乙", ROLE_REPRESENTATIVE, side=EMPLOYER)
    svc.register_representative("us1", "工会干事", ROLE_UNION_STAFF, side=WORKER)
    svc.register_representative("med", "调解员", ROLE_MEDIATOR)
    svc.register_representative("sup", "监督员", ROLE_SUPERVISOR)
    svc.open_topic("t-wage", "工资调整方案", scope="工资调整")
    svc.open_topic("t-benefit", "福利置换方案", scope="福利置换")
    return svc


def boot_authorized() -> NegotiationService:
    svc = boot()
    svc.grant_authorization(WORKER, ["工资调整", "福利置换"], note="成员大会授权")
    svc.grant_authorization(EMPLOYER, ["工资调整", "福利置换"], note="董事会授权")
    svc.open_round(3)
    return svc


def finalize_agreement(svc: NegotiationService) -> str:
    """双方各提一个议题并互相接受，自动形成最终文本。"""
    svc.submit_proposal("p-w1", "wr1", "t-wage", clauses=[
        {"clause_id": "c-wage", "text": "基本工资上调4%", "legal_bases": ["劳动法第四十六条"], "public": True},
    ])
    svc.accept_clause("c-wage", "er1")
    svc.submit_proposal("p-e1", "er1", "t-benefit", clauses=[
        {"clause_id": "c-benefit", "text": "新增年度体检福利", "legal_bases": ["劳动合同法第四条"], "public": True},
    ])
    svc.accept_clause("c-benefit", "wr1")
    (agreement_id,) = svc.agreements
    return agreement_id


class AuthorizationScopeTest(unittest.TestCase):
    def test_out_of_scope_clause_is_parked_until_authorized(self) -> None:
        svc = boot()
        svc.grant_authorization(WORKER, ["工资调整"], note="成员授权仅覆盖工资调整")
        svc.grant_authorization(EMPLOYER, ["工资调整", "福利置换"])
        svc.open_round(3)
        result = svc.submit_proposal("p-w1", "wr1", "t-benefit", clauses=[
            {"clause_id": "c-b1", "text": "以弹性福利置换部分加薪", "legal_bases": ["劳动合同法第四条"]},
        ])
        self.assertEqual(result["clauses"]["c-b1"], CLAUSE_PENDING)
        with self.assertRaisesRegex(NegotiationError, "暂存待确认"):
            svc.accept_clause("c-b1", "er1")
        kinds = {step["kind"] for step in svc.next_steps("wr1")}
        self.assertIn("clause_awaiting_authorization", kinds)
        # 成员补充授权后，暂存条款进入协商并可被接受冻结
        svc.grant_authorization(WORKER, ["工资调整", "福利置换"], note="成员补充授权福利置换")
        self.assertEqual(svc.clauses["c-b1"]["status"], CLAUSE_PROPOSED)
        svc.accept_clause("c-b1", "er1")
        self.assertEqual(svc.clauses["c-b1"]["status"], CLAUSE_FROZEN)
        self.assertEqual(svc.topics["t-benefit"]["status"], TOPIC_FROZEN)

    def test_narrowing_parks_uncovered_clauses_and_keeps_history(self) -> None:
        svc = boot_authorized()
        svc.submit_proposal("p-w1", "wr1", "t-benefit", clauses=[
            {"clause_id": "c-b1", "text": "以弹性福利置换部分加薪"},
        ])
        self.assertEqual(svc.clauses["c-b1"]["status"], CLAUSE_PROPOSED)
        svc.narrow_authorization(WORKER, ["福利置换"], reason="成员大会收回福利置换授权")
        self.assertEqual(svc.clauses["c-b1"]["status"], CLAUSE_PENDING)
        history = svc.authorization_history(WORKER)
        self.assertEqual([h["kind"] for h in history], ["granted", "narrowed"])
        self.assertIn("福利置换", history[0]["scopes"])
        # 收窄前的过程仍在事件日志中
        types = [event["type"] for event in svc.events]
        self.assertIn("proposal_submitted", types)
        self.assertIn("authorization_narrowed", types)


class ProposalChainTest(unittest.TestCase):
    def test_concession_must_reference_previous_proposal(self) -> None:
        svc = boot_authorized()
        svc.submit_proposal("p-e1", "er1", "t-wage", clauses=[
            {"clause_id": "c-w1", "text": "首轮方案：上调2%", "public": True},
        ])
        with self.assertRaisesRegex(NegotiationError, "上一版提案"):
            svc.submit_proposal("p-e2", "er1", "t-wage", clauses=[
                {"clause_id": "c-w2", "text": "让步方案：上调3%"},
            ])
        svc.submit_proposal("p-e2", "er1", "t-wage", parent_id="p-e1", clauses=[
            {"clause_id": "c-w2", "text": "让步方案：上调3%", "public": True},
        ])
        self.assertEqual(svc.clauses["c-w1"]["status"], CLAUSE_SUPERSEDED)
        self.assertEqual(svc.proposals["p-e2"]["parent_id"], "p-e1")

    def test_public_marks_control_public_docket(self) -> None:
        svc = boot_authorized()
        svc.submit_proposal("p-e1", "er1", "t-wage", clauses=[
            {"clause_id": "c-open", "text": "可公开条款", "public": True},
            {"clause_id": "c-secret", "text": "不公开条款", "public": False},
        ])
        docket = svc.public_docket()
        texts = {clause["text"] for clause in docket["clauses"]}
        self.assertIn("可公开条款", texts)
        self.assertNotIn("不公开条款", texts)


class MaterialTest(unittest.TestCase):
    def test_provider_cannot_self_certify_and_confidentiality_holds(self) -> None:
        svc = boot_authorized()
        svc.submit_material("m1", "er1", "t-wage", "经营测算（保密成本材料）", confidential=True)
        with self.assertRaisesRegex(NegotiationError, "不能单独认定"):
            svc.acknowledge_material("m1", "er1")
        with self.assertRaisesRegex(NegotiationError, "独立佐证"):
            svc.acknowledge_material("m1", "er2")
        svc.acknowledge_material("m1", "wr1")
        self.assertEqual(svc.materials["m1"]["status"], MATERIAL_CORROBORATED)
        # 保密材料不向监督人员与公开视图泄露内容
        self.assertIsNone(svc.list_materials("sup")[0]["summary"])
        self.assertEqual(svc.list_materials("wr1")[0]["summary"], "经营测算（保密成本材料）")
        self.assertNotIn("materials", svc.public_docket())


class MediatorTest(unittest.TestCase):
    def test_mediator_only_pushes_common_text(self) -> None:
        svc = boot_authorized()
        with self.assertRaisesRegex(NegotiationError, "调解人员只能推动共同文本"):
            svc.submit_proposal("p-m1", "med", "t-wage", clauses=[{"clause_id": "c-m", "text": "越权提案"}])
        svc.submit_proposal("p-w1", "wr1", "t-wage", clauses=[{"clause_id": "c-w", "text": "上调4%", "public": True}])
        with self.assertRaisesRegex(NegotiationError, "只有双方代表"):
            svc.accept_clause("c-w", "med")
        svc.accept_clause("c-w", "er1")
        result = svc.promote_common_text("med", note="汇总已冻结条款")
        self.assertEqual(result["clause_ids"], ["c-w"])
        self.assertEqual(svc.common_texts[0]["version"], 1)


class ProcessPreservationTest(unittest.TestCase):
    def test_replacement_recusal_and_round_changes_preserve_history(self) -> None:
        svc = boot_authorized()
        svc.submit_proposal("p-w1", "wr1", "t-wage", clauses=[{"clause_id": "c-w", "text": "上调4%"}])
        # 代表更换：原代表不能继续履职，新代表承继立场，过程保留
        svc.replace_representative("wr1", "wr2", "职工代表乙", reason="工作调动")
        with self.assertRaisesRegex(NegotiationError, "已更换"):
            svc.submit_proposal("p-w2", "wr1", "t-wage", parent_id="p-w1", clauses=[{"clause_id": "c-x", "text": "x"}])
        svc.submit_proposal("p-w2", "wr2", "t-wage", parent_id="p-w1", clauses=[{"clause_id": "c-w2", "text": "上调3.5%"}])
        # 利益冲突回避：被回避代表不得就该议题行动
        svc.declare_recusal("er1", "t-wage", "与议题存在利益冲突")
        with self.assertRaisesRegex(NegotiationError, "回避"):
            svc.accept_clause("c-w2", "er1")
        # 会谈中止与重新开局：历史不抹去
        svc.suspend_round("企业方申请核实经营数据")
        with self.assertRaisesRegex(NegotiationError, "没有进行中的会议轮次"):
            svc.submit_proposal("p-e1", "er2", "t-benefit", clauses=[{"clause_id": "c-b", "text": "y"}])
        svc.resume_round()
        svc.record_receipt("wr2", "rcpt-r3-wr2")
        svc.close_round()
        svc.open_round(4)
        svc.submit_proposal("p-e1", "er2", "t-benefit", clauses=[{"clause_id": "c-b", "text": "年度体检"}])
        types = [event["type"] for event in svc.events]
        for expected in ("representative_replaced", "recusal_declared", "round_suspended", "round_resumed", "round_closed"):
            self.assertIn(expected, types)


class AgreementTest(unittest.TestCase):
    def test_partial_freeze_then_full_agreement_and_trace(self) -> None:
        svc = boot_authorized()
        # 先就工资议题达成一致并冻结，福利议题继续协商
        svc.submit_proposal("p-w1", "wr1", "t-wage", clauses=[
            {"clause_id": "c-wage", "text": "基本工资上调4%", "legal_bases": ["劳动法第四十六条"], "public": True},
        ])
        svc.accept_clause("c-wage", "er1")
        self.assertEqual(svc.topics["t-wage"]["status"], TOPIC_FROZEN)
        self.assertEqual(svc.topics["t-benefit"]["status"], TOPIC_OPEN)
        with self.assertRaisesRegex(NegotiationError, "已冻结"):
            svc.submit_proposal("p-w2", "wr1", "t-wage", parent_id="p-w1", clauses=[{"clause_id": "c-x", "text": "x"}])
        svc.submit_proposal("p-e1", "er1", "t-benefit", clauses=[
            {"clause_id": "c-benefit", "text": "新增年度体检福利", "legal_bases": ["劳动合同法第四条"], "public": True},
        ])
        svc.accept_clause("c-benefit", "wr1")
        # 全部议题冻结后自动形成最终文本，双方各自确认才形成协议
        agreement_id = next(iter(svc.agreements))
        agreement = svc.agreements[agreement_id]
        self.assertEqual(agreement["status"], AGREEMENT_COLLECTING)
        svc.confirm_final_text(agreement_id, "wr1", agreement["text_hash"])
        self.assertEqual(svc.agreements[agreement_id]["status"], AGREEMENT_COLLECTING)
        svc.confirm_final_text(agreement_id, "er1", agreement["text_hash"])
        self.assertEqual(svc.agreements[agreement_id]["status"], AGREEMENT_FORMED)
        # 双方签署完成才生效
        svc.sign_agreement(agreement_id, "wr1", "sig-wr1")
        self.assertEqual(svc.agreements[agreement_id]["status"], AGREEMENT_FORMED)
        svc.sign_agreement(agreement_id, "er1", "sig-er1")
        self.assertEqual(svc.agreements[agreement_id]["status"], AGREEMENT_EFFECTIVE)
        # 履行节点与监督追溯
        svc.register_milestone("ms1", agreement_id, "补发调薪差额", due="2026-12-31")
        svc.complete_milestone("ms1", "已于2026-11-30补发到位")
        trace = svc.trace_clause("c-wage")
        self.assertEqual(trace["legal_bases"], ["劳动法第四十六条"])
        self.assertEqual(trace["proposal_chain"][0]["proposal_id"], "p-w1")
        self.assertTrue(all(a["covered"] for a in trace["authorizations"]))
        self.assertEqual(trace["agreement"]["status"], AGREEMENT_EFFECTIVE)
        self.assertEqual(trace["milestones"][0]["result"], "已于2026-11-30补发到位")

    def test_divergent_confirmations_open_dispute(self) -> None:
        svc = boot_authorized()
        agreement_id = finalize_agreement(svc)
        correct = svc.agreements[agreement_id]["text_hash"]
        svc.confirm_final_text(agreement_id, "er1", correct)
        svc.confirm_final_text(agreement_id, "wr1", "异文-hash")
        self.assertEqual(svc.agreements[agreement_id]["status"], AGREEMENT_DISPUTED)
        self.assertEqual(svc.disputes[0]["agreement_id"], agreement_id)
        kinds = {step["kind"] for step in svc.next_steps("med")}
        self.assertIn("mediate_dispute", kinds)

    def test_sign_and_withdraw_never_unilaterally_effective(self) -> None:
        # 情形一：签署与撤回并发，撤回先处理，后续签署被拒绝
        svc = boot_authorized()
        agreement_id = finalize_agreement(svc)
        text_hash = svc.agreements[agreement_id]["text_hash"]
        svc.confirm_final_text(agreement_id, "wr1", text_hash)
        svc.confirm_final_text(agreement_id, "er1", text_hash)
        svc.sign_agreement(agreement_id, "wr1", "sig-wr1")
        self.assertEqual(svc.agreements[agreement_id]["status"], AGREEMENT_FORMED)
        svc.withdraw_agreement(agreement_id, "er1", reason="董事会未批准")
        self.assertEqual(svc.agreements[agreement_id]["status"], AGREEMENT_CANCELLED)
        with self.assertRaisesRegex(NegotiationError, "已因一方撤回而取消"):
            svc.sign_agreement(agreement_id, "er1", "sig-er1")
        # 情形二：双方签署完成后生效，生效后不得单方撤回
        svc2 = boot_authorized()
        agreement_id2 = finalize_agreement(svc2)
        text_hash2 = svc2.agreements[agreement_id2]["text_hash"]
        svc2.confirm_final_text(agreement_id2, "wr1", text_hash2)
        svc2.confirm_final_text(agreement_id2, "er1", text_hash2)
        svc2.sign_agreement(agreement_id2, "wr1", "sig-wr1")
        svc2.sign_agreement(agreement_id2, "er1", "sig-er1")
        self.assertEqual(svc2.agreements[agreement_id2]["status"], AGREEMENT_EFFECTIVE)
        with self.assertRaisesRegex(NegotiationError, "不能单方撤回"):
            svc2.withdraw_agreement(agreement_id2, "wr1")


class IdempotencyTest(unittest.TestCase):
    def test_duplicate_receipts_and_signatures_merge(self) -> None:
        svc = boot_authorized()
        svc.record_receipt("wr1", "rcpt-1")
        again = svc.record_receipt("wr1", "rcpt-1")
        self.assertTrue(again["deduplicated"])
        self.assertEqual(len(svc.receipts), 1)
        agreement_id = finalize_agreement(svc)
        text_hash = svc.agreements[agreement_id]["text_hash"]
        svc.confirm_final_text(agreement_id, "wr1", text_hash)
        svc.confirm_final_text(agreement_id, "er1", text_hash)
        svc.sign_agreement(agreement_id, "wr1", "sig-wr1")
        repeat = svc.sign_agreement(agreement_id, "wr1", "sig-wr1")
        self.assertTrue(repeat["deduplicated"])
        signatures = [e for e in svc.events if e["type"] == "signature_recorded"]
        self.assertEqual(len(signatures), 1)

    def test_command_keys_are_idempotent(self) -> None:
        svc = boot_authorized()
        first = svc.submit_proposal("p-w1", "wr1", "t-wage", key="cmd-1", clauses=[
            {"clause_id": "c-w", "text": "上调4%"},
        ])
        second = svc.submit_proposal("p-w1", "wr1", "t-wage", key="cmd-1", clauses=[
            {"clause_id": "c-w", "text": "上调4%"},
        ])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["proposal_id"], "p-w1")
        self.assertEqual(len(svc.proposals), 1)


class CommitmentAndRestartTest(unittest.TestCase):
    def test_commitment_lifecycle_and_restart_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            svc = NegotiationService(EventStore(store_path))
            svc.register_representative("wr1", "职工代表甲", ROLE_REPRESENTATIVE, side=WORKER)
            svc.register_representative("er1", "企业代表甲", ROLE_REPRESENTATIVE, side=EMPLOYER)
            svc.register_representative("med", "调解员", ROLE_MEDIATOR)
            svc.open_topic("t-wage", "工资调整方案", scope="工资调整")
            svc.grant_authorization(WORKER, ["工资调整"])
            svc.grant_authorization(EMPLOYER, ["工资调整"])
            svc.open_round(3)
            svc.record_commitment("cm1", "er1", "第三轮后提供完整经营测算", valid_until="2026-10-01")
            with self.assertRaisesRegex(NegotiationError, "对方代表确认"):
                svc.confirm_commitment("cm1", "er1")
            # 期限届满：待确认承诺转为过期，可续期继续处理
            self.assertEqual(svc.expire_commitments("2026-10-02"), ["cm1"])
            self.assertEqual(svc.commitments["cm1"]["status"], COMMITMENT_EXPIRED)
            svc.renew_commitment("cm1", "2026-10-15")
            self.assertEqual(svc.commitments["cm1"]["status"], COMMITMENT_PENDING)
            # 服务重启：待确认承诺、调解事项继续处理
            restored = NegotiationService(EventStore(store_path))
            self.assertEqual(restored.commitments["cm1"]["status"], COMMITMENT_PENDING)
            kinds = {step["kind"] for step in restored.next_steps("wr1")}
            self.assertIn("confirm_commitment", kinds)
            self.assertIn("mediate_topic", {step["kind"] for step in restored.next_steps("med")})
            restored.confirm_commitment("cm1", "wr1")
            self.assertEqual(restored.commitments["cm1"]["status"], COMMITMENT_CONFIRMED)
            # 重启后完成协议与履行节点
            restored.submit_proposal("p-w1", "wr1", "t-wage", clauses=[
                {"clause_id": "c-w", "text": "上调4%", "legal_bases": ["劳动法第四十六条"], "public": True},
            ])
            restored.accept_clause("c-w", "er1")
            agreement_id = next(iter(restored.agreements))
            text_hash = restored.agreements[agreement_id]["text_hash"]
            restored.confirm_final_text(agreement_id, "wr1", text_hash)
            restored.confirm_final_text(agreement_id, "er1", text_hash)
            restored.sign_agreement(agreement_id, "wr1", "sig-wr1")
            restored.sign_agreement(agreement_id, "er1", "sig-er1")
            restored.register_milestone("ms1", agreement_id, "补发调薪差额", due="2026-12-31")
            # 再次重启，履行节点仍可处理
            revived = NegotiationService(EventStore(store_path))
            self.assertIn("report_milestone", {step["kind"] for step in revived.next_steps("er1")})
            revived.complete_milestone("ms1", "已补发到位")
            self.assertEqual(revived.trace_clause("c-w")["milestones"][0]["result"], "已补发到位")


class NextStepsTest(unittest.TestCase):
    def test_union_staff_sees_authorization_gap_and_supervisor_audits(self) -> None:
        svc = boot()
        svc.grant_authorization(WORKER, ["工资调整"])
        svc.grant_authorization(EMPLOYER, ["工资调整", "福利置换"])
        svc.open_round(3)
        staff_steps = svc.next_steps("us1")
        self.assertIn("extend_authorization", {step["kind"] for step in staff_steps})
        gap = next(step for step in staff_steps if step["kind"] == "extend_authorization")
        self.assertEqual(gap["scope"], "福利置换")
        # 监督人员看到待审计协议；未形成协议前没有监督事项
        self.assertEqual(svc.next_steps("sup"), [])
        svc2 = boot_authorized()
        finalize_agreement(svc2)
        audit = svc2.next_steps("sup")
        self.assertEqual(audit[0]["kind"], "audit_agreement")
        self.assertEqual(audit[0]["status"], AGREEMENT_COLLECTING)


if __name__ == "__main__":
    unittest.main()
