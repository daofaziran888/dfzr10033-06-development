"""基准集口径可比性与偏离记录集成测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store
from app.services.benchmark_set import BenchmarkSetService
from app.services.metric_factory import MetricFactoryService

client = TestClient(app)


@pytest.fixture
def services():
    return {"benchmark": BenchmarkSetService(), "metric": MetricFactoryService()}


def _baseline(编号: str) -> dict:
    return next(r for r in store.rows("benchmark_set") if r["基线编号"] == 编号)


def _actual(编号: str) -> dict:
    return next(r for r in store.rows("metric_actual") if r["实际编号"] == 编号)


def _check(services, base_no: str, act_no: str) -> dict:
    result = services["benchmark"].check_comparability(
        _baseline(base_no)["id"], _actual(act_no)["id"])
    assert result["ok"], result.get("message")
    return result["check"]


class TestBaselineParsing:
    def test_legacy_baseline_flat_fields_are_normalized(self, services):
        """历史阈值（字符串）与无明细基线要被解析器兼容归一。"""
        snapshot = services["benchmark"].parse_baseline(_baseline("BENC-0001")["id"])
        assert snapshot["version"] == "legacy-v0"
        assert snapshot["threshold"] == 0.1
        assert snapshot["interval"] == "P1M"
        assert snapshot["detail_inferred"] is True
        assert snapshot["detail"] == [{"dimensions": {"region": "全国"}, "value": 1000.0}]
        assert snapshot["scope"] == "ALL"

    def test_structured_baseline_keeps_version_threshold(self, services):
        snapshot = services["benchmark"].parse_baseline(_baseline("BENC-0002")["id"])
        assert snapshot["version"] == "v1.1"
        assert snapshot["threshold"] == 0.1
        assert snapshot["granularity"] == "region"
        assert snapshot["dimensions"] == ["region"]
        assert snapshot["period_start"] == "2026-09-01"
        regions = {cell["dimensions"]["region"]: cell["value"] for cell in snapshot["detail"]}
        assert regions == {"东区": 100.0, "西区": 100.0, "全国": 200.0}


class TestComparabilityCheck:
    def test_matching_caliber_is_comparable(self, services):
        check = _check(services, "BENC-0002", "ACT-0001")
        assert check["结论"] == "COMPARABLE"
        assert check["维度核对"] == "DIMENSION_MATCH"
        assert check["粒度核对"] == "GRANULARITY_MATCH"
        assert check["区间核对"] == "PERIOD_MATCH"
        assert check["地区明细核对"] == "REGION_DETAIL_MATCH"
        # 每次比较只绑定一组可解释的基线与实际值
        assert check["baseline_id"] == _baseline("BENC-0002")["id"]
        assert check["actual_id"] == _actual("ACT-0001")["id"]

    def test_dimension_change_is_separately_judged_incomparable(self, services):
        """本期统计维度变化：新增列与聚合粒度不同须分别判定，且拦截方差计算。"""
        check = _check(services, "BENC-0002", "ACT-0003")
        assert check["结论"] == "INCOMPARABLE"
        assert check["维度核对"] == "DIMENSION_EXTRA_COLUMN"
        assert check["新增维度列"] == ["category"]
        assert check["粒度核对"] == "GRANULARITY_MISMATCH"
        assert check["区间核对"] == "PERIOD_MATCH"
        reason_text = "；".join(check["失败原因"])
        assert "新增基线没有的维度列" in reason_text
        assert "聚合粒度不同" in reason_text

        result = services["metric"].generate_deviation_records(check["id"])
        assert result["ok"] is False
        assert "拒绝跨定义计算方差" in result["message"]
        assert store.rows("deviation_record") == [] or all(
            r["check_id"] != check["id"] for r in store.rows("deviation_record"))

    def test_missing_region_detail_reports_traceable_reason(self, services):
        """缺历史地区数据时，地区比较失败原因可核对。"""
        check = _check(services, "BENC-0003", "ACT-0001")
        assert check["结论"] == "INCOMPARABLE"
        assert check["地区明细核对"] == "MISSING_REGION_DETAIL"
        assert any("缺少可追到地区的明细" in r for r in check["失败原因"])


class TestRegionDetailRestore:
    def test_supply_same_period_detail_restores_region_comparison(self, services):
        baseline_id = _baseline("BENC-0003")["id"]
        # 第一次比较因缺地区明细失败
        first = services["benchmark"].check_comparability(baseline_id, _actual("ACT-0001")["id"])
        assert first["check"]["结论"] == "INCOMPARABLE"

        # 补录同一期、合计守恒的地区明细
        supply = services["benchmark"].supply_region_detail(
            baseline_id,
            details=[
                {"dimensions": {"region": "东区"}, "value": 100.0},
                {"dimensions": {"region": "西区"}, "value": 100.0},
            ],
            period_start="2026-09-01", period_end="2026-09-30")
        assert supply["ok"], supply["message"]

        recheck = services["benchmark"].check_comparability(baseline_id, _actual("ACT-0001")["id"])
        assert recheck["check"]["结论"] == "COMPARABLE"
        assert recheck["check"]["地区明细核对"] == "REGION_DETAIL_MATCH"

    def test_supply_rejects_different_period(self, services):
        result = services["benchmark"].supply_region_detail(
            _baseline("BENC-0003")["id"],
            details=[{"dimensions": {"region": "东区"}, "value": 200.0}],
            period_start="2026-08-01", period_end="2026-08-31")
        assert result["ok"] is False
        assert "同一统计区间" in result["message"]

    def test_supply_rejects_sum_violation(self, services):
        result = services["benchmark"].supply_region_detail(
            _baseline("BENC-0003")["id"],
            details=[{"dimensions": {"region": "东区"}, "value": 150.0}],
            period_start="2026-09-01", period_end="2026-09-30")
        assert result["ok"] is False
        assert "全国总量" in result["message"]


class TestDeviationRecords:
    def test_east_plus_ten_west_minus_ten_no_alert_at_threshold(self, services):
        """东区+10、西区-10、全国合计不变：偏离率恰为10%，不超过0.1阈值，均不告警。"""
        check = _check(services, "BENC-0002", "ACT-0001")
        result = services["metric"].generate_deviation_records(check["id"])
        assert result["ok"], result.get("message")
        summary = result["summary"]
        assert summary["alert_count"] == 0
        assert summary["threshold"] == 0.1  # 阈值来自基线 v1.1 版本快照
        assert summary["baseline_version"] == "v1.1"

        verdicts = {v["region"]: v for v in summary["region_verdicts"]}
        east, west, total = verdicts["东区"], verdicts["西区"], verdicts["全国"]
        # 两期数值核对
        assert (east["baseline_value"], east["actual_value"]) == (100.0, 110.0)
        assert (west["baseline_value"], west["actual_value"]) == (100.0, 90.0)
        assert (total["baseline_value"], total["actual_value"]) == (200.0, 200.0)
        # 偏离率与告警结论
        assert east["deviation_rate"] == pytest.approx(0.1) and east["alerted"] is False
        assert west["deviation_rate"] == pytest.approx(-0.1) and west["alerted"] is False
        assert total["deviation_rate"] == 0.0 and total["alerted"] is False
        # 告警所指范围：没有任何单元触发
        assert all(v["alerted"] is False for v in summary["region_verdicts"])

    def test_region_owners_alert_but_national_report_does_not(self, services):
        """东区+12%、西区-12%、全国合计不变：地区负责人触发偏离，总量报表不触发。"""
        check = _check(services, "BENC-0002", "ACT-0002")
        result = services["metric"].generate_deviation_records(check["id"])
        summary = result["summary"]
        assert summary["alert_count"] == 2
        verdicts = {v["region"]: v for v in summary["region_verdicts"]}
        assert verdicts["东区"]["alerted"] is True
        assert verdicts["西区"]["alerted"] is True
        assert verdicts["全国"]["alerted"] is False
        assert verdicts["全国"]["delta"] == 0.0

        # 偏离记录可关联回基线与版本
        records = [r for r in store.rows("deviation_record") if r["check_id"] == check["id"]]
        assert {r["基线编号"] for r in records} == {"BENC-0002"}
        assert {r["基线版本"] for r in records} == {"v1.1"}
        alerted = [r for r in records if r["触发偏离"]]
        assert {r["维度单元"]["region"] for r in alerted} == {"东区", "西区"}

    def test_zero_denominator_does_not_trigger_alert(self, services):
        """零分母不能直接触发偏离告警：登记为零分母不可计算。"""
        check = _check(services, "BENC-0004", "ACT-0004")
        result = services["metric"].generate_deviation_records(check["id"])
        summary = result["summary"]
        assert summary["zero_denominator_count"] == 1
        south = next(v for v in summary["region_verdicts"] if v["region"] == "南区")
        assert south["baseline_value"] == 0.0 and south["deviation_rate"] is None
        assert south["verdict"] == "零分母不可计算" and south["alerted"] is False

        records = [r for r in store.rows("deviation_record") if r["check_id"] == check["id"]]
        south_record = next(r for r in records if r["维度单元"]["region"] == "南区")
        assert south_record["判定结论"] == "零分母不可计算"
        assert south_record["触发偏离"] is False


class TestComparisonListWriteback:
    def test_benchmark_receives_results_in_comparison_list(self, services):
        check = _check(services, "BENC-0002", "ACT-0001")
        services["metric"].generate_deviation_records(check["id"])
        listing = services["benchmark"].get_comparison_list(_baseline("BENC-0002")["id"])
        assert listing["ok"]
        entries = [e for e in listing["comparison_list"] if e["check_id"] == check["id"]]
        # 校验时回写一次、偏离结果回写一次
        assert len(entries) == 2
        final_entry = entries[-1]
        assert final_entry["偏离汇总"]["threshold"] == 0.1
        assert final_entry["偏离汇总"]["alert_count"] == 0
        assert final_entry["实际编号"] == "ACT-0001"

    def test_incomparable_check_is_recorded_with_failure_reason(self, services):
        check = _check(services, "BENC-0002", "ACT-0003")
        listing = services["benchmark"].get_comparison_list(_baseline("BENC-0002")["id"])
        entry = next(e for e in listing["comparison_list"] if e["check_id"] == check["id"])
        assert entry["可比"] is False
        assert entry["失败原因"] == check["失败原因"]


class TestBenchmarkApi:
    def test_check_and_deviation_endpoints(self):
        baseline_id = _baseline("BENC-0002")["id"]
        actual_id = _actual("ACT-0002")["id"]
        resp = client.post(f"/api/benchmark/baselines/{baseline_id}/check/{actual_id}")
        assert resp.status_code == 200 and resp.json()["ok"]
        check_id = resp.json()["check"]["id"]

        dev_resp = client.post(f"/api/benchmark/checks/{check_id}/deviations")
        assert dev_resp.status_code == 200
        body = dev_resp.json()
        assert body["ok"] and body["summary"]["alert_count"] == 2

        list_resp = client.get(f"/api/benchmark/baselines/{baseline_id}/comparison-list")
        assert list_resp.status_code == 200
        assert list_resp.json()["comparison_list"]

    def test_snapshot_endpoint_parses_legacy_baseline(self):
        resp = client.get(f"/api/benchmark/baselines/{_baseline('BENC-0001')['id']}/snapshot")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] and payload["snapshot"]["threshold"] == 0.1
        assert payload["snapshot"]["detail_inferred"] is True
