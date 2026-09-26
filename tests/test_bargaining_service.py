"""校验集体协商过程服务的授权、提案、冻结、生效与追溯规则。"""

import unittest

from src.bargaining_service import BargainingService


def build_service() -> BargainingService:
    """搭好第三轮协商现场：双方代表、调解与监督人员、两个议题。"""
    service = BargainingService()
    service.add_representative("W1", "labor", "职工代表大会授权书", now=1)
    service.add_representative("E1", "enterprise", "企业法定代表人委托书", now=1)
    service.add_representative("M1", None, "调解员聘书", now=1, role="mediator")
    service.add_representative("S1", None, "监督员聘书", now=1, role="supervisor")
    service.set_mandate("W1", ["工资调整"], now=1)
    service.set_mandate("E1", ["工资调整", "福利置换"], now=1)
    service.add_topic("T-wage", "工资调整", ["劳动法第三十五条", "集体合同规定第八条"], now=1)
    service.add_topic("T-benefit", "福利置换", ["集体合同规定第十二条"], now=1)
    service.open_round("R3", 3, now=2)
    return service


def wage_clause(text: str, public: bool = True) -> dict:
    return {"key": "w1", "subject": "工资调整", "text": text, "public": public}


def benefit_clause(text: str, public: bool = False) -> dict:
    return {"key": "b1", "subject": "福利置换", "text": text, "public": public}


class ProposalChainTest(unittest.TestCase):
    def test_concession_must_reference_previous_version(self) -> None:
        service = build_service()
        service.submit_proposal("L1", "W1", "T-wage", [wage_clause("加薪5%")], now=3)
        with self.assertRaisesRegex(ValueError, "让步必须引用上一版提案"):
            service.submit_proposal("L2", "W1", "T-wage", [wage_clause("加薪4%")], now=4)
        proposal = service.submit_proposal("L2", "W1", "T-wage", [wage_clause("加薪4%")], now=4, based_on="L1")
        self.assertEqual(proposal["based_on"], "L1")
        self.assertEqual(service.state["proposals"]["L1"]["status"], "superseded")

    def test_first_proposal_cannot_reference_previous(self) -> None:
        service = build_service()
        with self.assertRaisesRegex(ValueError, "首版提案无上一版可引用"):
            service.submit_proposal("L1", "W1", "T-wage", [wage_clause("加薪5%")], now=3, based_on="X0")

    def test_clause_must_mark_public_content(self) -> None:
        service = build_service()
        clause = {"key": "w1", "subject": "工资调整", "text": "加薪5%"}
        with self.assertRaisesRegex(ValueError, "可公开内容"):
            service.submit_proposal("L1", "W1", "T-wage", [clause], now=3)

    def test_commitment_valid_until_is_recorded(self) -> None:
        service = build_service()
        proposal = service.submit_proposal(
            "L1", "W1", "T-wage", [wage_clause("加薪5%")], now=3, valid_until=10
        )
        self.assertEqual(proposal["valid_until"], 10)


class MandateAndParkingTest(unittest.TestCase):
    def test_clause_outside_mandate_is_parked_not_negotiated(self) -> None:
        service = build_service()
        proposal = service.submit_proposal(
            "L1", "W1", "T-benefit", [benefit_clause("年假置换为补贴")], now=3
        )
        self.assertEqual(proposal["clauses"][0]["status"], "parked")
        parked = list(service.state["parked"].values())
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0]["reason"], "超出授权范围")
        self.assertEqual(parked[0]["status"], "pending")

    def test_parked_clause_confirmed_only_after_mandate_expanded(self) -> None:
        service = build_service()
        service.submit_proposal("L1", "W1", "T-benefit", [benefit_clause("年假置换为补贴")], now=3)
        parked_id = next(iter(service.state["parked"]))
        with self.assertRaisesRegex(ValueError, "仍超出授权范围"):
            service.confirm_parked(parked_id, "W1", now=4)
        service.set_mandate("W1", ["工资调整", "福利置换"], now=5)
        record = service.confirm_parked(parked_id, "W1", now=6)
        self.assertEqual(record["status"], "confirmed")
        self.assertEqual(service.state["proposals"]["L1"]["clauses"][0]["status"], "active")

    def test_parked_clause_cannot_be_confirmed_by_other_party(self) -> None:
        service = build_service()
        service.submit_proposal("L1", "W1", "T-benefit", [benefit_clause("年假置换为补贴")], now=3)
        parked_id = next(iter(service.state["parked"]))
        with self.assertRaisesRegex(ValueError, "只能由本方代表确认"):
            service.confirm_parked(parked_id, "E1", now=4)

    def test_mandate_narrowing_parks_active_clauses_and_keeps_history(self) -> None:
        service = build_service()
        service.set_mandate("W1", ["工资调整", "福利置换"], now=2)
        service.submit_proposal("L1", "W1", "T-benefit", [benefit_clause("年假置换为补贴")], now=3)
        mandate = service.set_mandate("W1", ["工资调整"], now=4)
        self.assertEqual(mandate["kind"], "narrowed")
        clause = service.state["proposals"]["L1"]["clauses"][0]
        self.assertEqual(clause["status"], "parked")
        parked = list(service.state["parked"].values())
        self.assertEqual(parked[0]["reason"], "授权收窄")
        kinds = [item["kind"] for item in service.state["representatives"]["W1"]["mandates"]]
        self.assertEqual(kinds, ["initial", "expanded", "narrowed"])


class MaterialTest(unittest.TestCase):
    def test_confidential_material_needs_other_party_corroboration(self) -> None:
        service = build_service()
        service.submit_material("C1", "E1", "经营测算（保密）", confidential=True, now=3)
        with self.assertRaisesRegex(ValueError, "不能单独认定自己数据成立"):
            service.corroborate_material("C1", "E1", now=3)
        material = service.corroborate_material("C1", "W1", now=4)
        self.assertEqual(material["status"], "established")
        again = service.corroborate_material("C1", "W1", now=5)
        self.assertEqual(len(again["corroborations"]), 1)

    def test_confidential_material_stays_out_of_public_record(self) -> None:
        service = build_service()
        service.submit_material("C1", "E1", "经营测算（保密）", confidential=True, now=3)
        record = service.public_record()
        self.assertNotIn("materials", record)
        self.assertNotIn("经营测算", str(record))


class RoundAndReceiptTest(unittest.TestCase):
    def test_duplicate_receipts_merge_idempotently(self) -> None:
        service = build_service()
        first = service.record_receipt("R3", "labor", "签到表-1", now=3)
        second = service.record_receipt("R3", "labor", "签到表-1", now=4)
        self.assertEqual(first, second)
        receipts = service.state["rounds"]["R3"]["receipts"]
        self.assertEqual(len(receipts), 1)
        events = [e for e in service.state["events"] if e["kind"] == "receipt_recorded"]
        self.assertEqual(len(events), 1)

    def test_suspend_and_resume_keep_history(self) -> None:
        service = build_service()
        service.record_receipt("R3", "labor", "签到表-1", now=3)
        service.suspend_round("R3", now=4)
        service.resume_round("R3", now=5)
        service.open_round("R4", 4, now=6, resumed_from="R3")
        kinds = [e["kind"] for e in service.state["events"]]
        self.assertIn("round_suspended", kinds)
        self.assertIn("round_resumed", kinds)
        self.assertEqual(service.state["rounds"]["R3"]["receipts"], {"labor:签到表-1": {"party": "labor", "key": "签到表-1", "at": 3}})
        self.assertEqual(service.state["rounds"]["R4"]["resumed_from"], "R3")


class RepresentativeChangeTest(unittest.TestCase):
    def test_replacement_keeps_predecessor_actions(self) -> None:
        service = build_service()
        service.submit_proposal("L1", "W1", "T-wage", [wage_clause("加薪5%")], now=3)
        service.replace_representative("W1", "W2", "新的职工代表授权书", now=4)
        self.assertEqual(service.state["representatives"]["W1"]["status"], "replaced")
        self.assertEqual(service.state["proposals"]["L1"]["rep"], "W1")
        with self.assertRaisesRegex(ValueError, "代表资格已失效"):
            service.submit_proposal("L2", "W1", "T-wage", [wage_clause("加薪4%")], now=5, based_on="L1")
        proposal = service.submit_proposal("L2", "W2", "T-wage", [wage_clause("加薪4%")], now=5, based_on="L1")
        self.assertEqual(proposal["rep"], "W2")

    def test_recusal_blocks_topic_and_is_kept(self) -> None:
        service = build_service()
        service.declare_recusal("E1", topic="T-benefit", reason="与福利供应商存在关联", now=2)
        with self.assertRaisesRegex(ValueError, "回避关系"):
            service.submit_proposal("E1", "E1", "T-benefit", [benefit_clause("补贴方案")], now=3)
        recusals = service.state["representatives"]["E1"]["recusals"]
        self.assertEqual(recusals[0]["topic"], "T-benefit")


class MediatorBoundaryTest(unittest.TestCase):
    def test_mediator_cannot_submit_party_proposal(self) -> None:
        service = build_service()
        with self.assertRaisesRegex(ValueError, "不能提交本方提案"):
            service.submit_proposal("M1", "M1", "T-wage", [wage_clause("折中3%")], now=3)

    def test_mediator_pushes_common_text_but_cannot_accept_for_party(self) -> None:
        service = build_service()
        draft = service.propose_common_text("D1", "M1", "T-wage", "w1", "加薪3%", now=3)
        self.assertEqual(draft["acceptances"], {})
        with self.assertRaisesRegex(ValueError, "不能代表协商方接受"):
            service.accept_common_text("D1", "M1", now=4)
        service.accept_common_text("D1", "W1", now=4)
        clause = service.accept_common_text("D1", "E1", now=5)
        self.assertEqual(clause["id"], "T-wage:w1")
        self.assertEqual(clause["text"], "加薪3%")


class FreezeAndAgreementTest(unittest.TestCase):
    def _freeze_wage_clause(self, service: BargainingService) -> None:
        service.submit_proposal("L1", "W1", "T-wage", [wage_clause("加薪5%")], now=3)
        service.submit_proposal("F1", "E1", "T-wage", [wage_clause("加薪2%")], now=3)
        service.propose_common_text("D1", "M1", "T-wage", "w1", "加薪3%", now=4)
        service.accept_common_text("D1", "W1", now=5)
        service.accept_common_text("D1", "E1", now=5)

    def test_partial_agreement_freezes_clause_and_rest_continues(self) -> None:
        service = build_service()
        self._freeze_wage_clause(service)
        self.assertIn("T-wage:w1", service.state["frozen"])
        self.assertEqual(service.state["topics"]["T-benefit"]["status"], "open")
        service.submit_proposal("L2", "W1", "T-benefit", [benefit_clause("年假置换为补贴")], now=6)
        self.assertEqual(service.state["topics"]["T-benefit"]["status"], "open")

    def test_agreement_effective_only_after_both_confirm_same_text(self) -> None:
        service = build_service()
        self._freeze_wage_clause(service)
        agreement = service.assemble_agreement(now=6)
        service.confirm_agreement("W1", agreement["digest"], now=7)
        self.assertEqual(agreement["status"], "confirming")
        service.confirm_agreement("E1", agreement["digest"], now=8)
        self.assertEqual(agreement["status"], "effective")

    def test_concurrent_sign_and_withdraw_never_unilaterally_effective(self) -> None:
        service = build_service()
        self._freeze_wage_clause(service)
        agreement = service.assemble_agreement(now=6)
        service.confirm_agreement("W1", agreement["digest"], now=7)
        service.withdraw_agreement("E1", now=7)
        self.assertEqual(agreement["status"], "withdrawn")
        self.assertNotEqual(agreement["status"], "effective")
        with self.assertRaisesRegex(ValueError, "当前不能确认"):
            service.confirm_agreement("E1", agreement["digest"], now=8)

    def test_withdrawn_text_can_be_reassembled_and_history_kept(self) -> None:
        service = build_service()
        self._freeze_wage_clause(service)
        first = service.assemble_agreement(now=6)
        service.withdraw_agreement("E1", now=7)
        second = service.assemble_agreement(now=8)
        self.assertEqual(second["version"], 2)
        self.assertEqual(service.state["agreements"][0]["status"], "withdrawn")
        service.confirm_agreement("W1", second["digest"], now=9)
        service.confirm_agreement("E1", second["digest"], now=9)
        self.assertEqual(second["status"], "effective")

    def test_divergent_texts_enter_dispute(self) -> None:
        service = build_service()
        self._freeze_wage_clause(service)
        agreement = service.assemble_agreement(now=6)
        service.confirm_agreement("W1", agreement["digest"], now=7)
        service.confirm_agreement("E1", "0" * 64, now=7)
        self.assertEqual(agreement["status"], "disputed")
        self.assertEqual(service.state["disputes"][0]["kind"], "异文")

    def test_async_signatures_merge_idempotently(self) -> None:
        service = build_service()
        self._freeze_wage_clause(service)
        agreement = service.assemble_agreement(now=6)
        service.confirm_agreement("W1", agreement["digest"], now=7)
        service.confirm_agreement("W1", agreement["digest"], now=8)
        service.confirm_agreement("E1", agreement["digest"], now=9)
        service.confirm_agreement("E1", agreement["digest"], now=10)
        self.assertEqual(agreement["status"], "effective")
        events = [e for e in service.state["events"] if e["kind"] == "agreement_confirmed"]
        self.assertEqual(len(events), 2)

    def test_withdrawal_after_effectiveness_becomes_dispute(self) -> None:
        service = build_service()
        self._freeze_wage_clause(service)
        agreement = service.assemble_agreement(now=6)
        service.confirm_agreement("W1", agreement["digest"], now=7)
        service.confirm_agreement("E1", agreement["digest"], now=7)
        service.withdraw_agreement("E1", now=8)
        self.assertEqual(agreement["status"], "effective")
        self.assertEqual(service.state["disputes"][0]["kind"], "撤回已生效协议")


class RestartAndNextStepTest(unittest.TestCase):
    def test_snapshot_restore_continues_pending_items(self) -> None:
        service = build_service()
        service.submit_proposal(
            "L1", "W1", "T-benefit", [benefit_clause("年假置换为补贴")], now=3, valid_until=5
        )
        restored = BargainingService.restore(service.snapshot())
        self.assertEqual(restored.snapshot(), service.snapshot())
        restored.set_mandate("W1", ["工资调整", "福利置换"], now=6)
        parked_id = next(iter(restored.state["parked"]))
        record = restored.confirm_parked(parked_id, "W1", now=7)
        self.assertEqual(record["status"], "confirmed")

    def test_expired_commitments_and_missed_fulfillment_remain_actionable(self) -> None:
        service = build_service()
        service.submit_proposal("L1", "W1", "T-wage", [wage_clause("加薪5%")], now=3, valid_until=5)
        service.propose_common_text("D1", "M1", "T-wage", "w1", "加薪3%", now=4)
        service.accept_common_text("D1", "W1", now=4)
        service.accept_common_text("D1", "E1", now=4)
        service.add_fulfillment_node("N1", "T-wage:w1", due=6, now=4)
        service._touch(10)
        items = service.pending_items()
        self.assertEqual(items["lapsed"]["labor"], ["L1"])
        self.assertEqual(items["fulfillment_missed"], ["N1"])
        node = service.record_fulfillment("N1", "已补发差额", now=11)
        self.assertTrue(node["late"])
        steps = service.next_steps("labor")
        self.assertTrue(any("承诺已到期" in step for step in steps))

    def test_next_steps_fit_each_role(self) -> None:
        service = build_service()
        service.submit_proposal("L1", "W1", "T-benefit", [benefit_clause("年假置换为补贴")], now=3)
        service.submit_proposal("F1", "E1", "T-benefit", [benefit_clause("维持现状", public=True)], now=3)
        labor_steps = service.next_steps("labor")
        self.assertTrue(any("暂存条款" in step for step in labor_steps))
        mediator_steps = service.next_steps("mediator")
        self.assertTrue(any("推动共同文本" in step for step in mediator_steps))
        supervisor_steps = service.next_steps("supervisor")
        self.assertTrue(any("追溯" in step for step in supervisor_steps))
        enterprise_steps = service.next_steps("enterprise")
        self.assertFalse(any("暂存条款" in step for step in enterprise_steps))


class AuditTraceTest(unittest.TestCase):
    def test_supervisor_traces_clause_to_mandate_proposals_law_and_fulfillment(self) -> None:
        service = build_service()
        service.submit_proposal("L1", "W1", "T-wage", [wage_clause("加薪5%")], now=3)
        service.submit_proposal("L2", "W1", "T-wage", [wage_clause("加薪4%")], now=4, based_on="L1")
        service.submit_proposal("F1", "E1", "T-wage", [wage_clause("加薪2%")], now=4)
        service.propose_common_text("D1", "M1", "T-wage", "w1", "加薪3%", now=5)
        service.accept_common_text("D1", "W1", now=5)
        service.accept_common_text("D1", "E1", now=5)
        agreement = service.assemble_agreement(now=6)
        service.confirm_agreement("W1", agreement["digest"], now=7)
        service.confirm_agreement("E1", agreement["digest"], now=7)
        service.add_fulfillment_node("N1", "T-wage:w1", due=30, now=8)
        service.record_fulfillment("N1", "差额已随9月工资发放", now=9)

        trace = service.audit_clause("T-wage:w1")
        self.assertEqual(trace["legal_basis"], ["劳动法第三十五条", "集体合同规定第八条"])
        labor_lineage = trace["proposal_lineage"]["labor"]
        self.assertEqual([item["proposal"] for item in labor_lineage], ["L1", "L2"])
        self.assertEqual(labor_lineage[1]["based_on"], "L1")
        self.assertEqual(labor_lineage[0]["mandate_scope"], ["工资调整"])
        self.assertEqual(trace["acceptances"]["labor"]["rep"], "W1")
        self.assertEqual(trace["fulfillment"][0]["result"], "差额已随9月工资发放")
        self.assertEqual(trace["agreement_versions"], [1])

    def test_public_record_contains_only_public_clauses(self) -> None:
        service = build_service()
        service.submit_proposal(
            "L1",
            "W1",
            "T-wage",
            [wage_clause("加薪5%", public=True), {"key": "w2", "subject": "工资调整", "text": "内部测算口径", "public": False}],
            now=3,
        )
        record = service.public_record()
        texts = str(record)
        self.assertIn("加薪5%", texts)
        self.assertNotIn("内部测算口径", texts)


if __name__ == "__main__":
    unittest.main()
