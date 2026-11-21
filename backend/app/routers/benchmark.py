"""基准集与指标工厂协作接口：口径可比性校验、偏离记录、比较清单回写。

路由层只做参数装配，业务判断全部留在 services 内。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.benchmark_set import BenchmarkSetService
from app.services.metric_factory import MetricFactoryService
from app.store import store

router = APIRouter(prefix="/api/benchmark", tags=["基准集比较"])

benchmark_svc = BenchmarkSetService()
metric_svc = MetricFactoryService()


class ActualPayload(BaseModel):
    实际编号: str | None = None
    指标编号: str
    维度列表: list[str] = Field(default_factory=list)
    聚合粒度: str
    统计区间: str
    期间开始: str
    期间结束: str
    actual_detail: list[dict[str, Any]]


class RegionDetailPayload(BaseModel):
    period_start: str
    period_end: str
    details: list[dict[str, Any]]


@router.get("/baselines/{baseline_id}/snapshot")
def baseline_snapshot(baseline_id: int) -> dict[str, Any]:
    """基线解析结果：兼容历史阈值与适用范围后的归一口径快照。"""
    try:
        return {"ok": True, "snapshot": benchmark_svc.parse_baseline(baseline_id)}
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}


@router.post("/baselines/{baseline_id}/check/{actual_id}")
def check_comparability(baseline_id: int, actual_id: int) -> dict[str, Any]:
    """用基线时段口径与本期实际指标做可比性校验，产出校验明细。"""
    return benchmark_svc.check_comparability(baseline_id, actual_id)


@router.post("/actuals")
def register_actual(payload: ActualPayload) -> dict[str, Any]:
    """指标工厂登记本期实际指标数据。"""
    return metric_svc.register_actual(payload.model_dump())


@router.post("/checks/{check_id}/deviations")
def generate_deviations(check_id: int) -> dict[str, Any]:
    """指标工厂依据可比性校验明细生成偏离记录并回写比较清单。"""
    return metric_svc.generate_deviation_records(check_id)


@router.post("/baselines/{baseline_id}/region-detail")
def supply_region_detail(baseline_id: int, payload: RegionDetailPayload) -> dict[str, Any]:
    """补录同一期能还原地区的历史明细，补后可重新校验。"""
    return benchmark_svc.supply_region_detail(
        baseline_id, payload.details, payload.period_start, payload.period_end)


@router.get("/baselines/{baseline_id}/comparison-list")
def get_comparison_list(baseline_id: int) -> dict[str, Any]:
    """读取基线的比较清单（含回写的偏离汇总）。"""
    return benchmark_svc.get_comparison_list(baseline_id)


@router.get("/checks")
def list_checks() -> dict[str, Any]:
    rows = store.rows("comparability_check")
    return {"ok": True, "items": rows, "total": len(rows)}


@router.get("/deviations")
def list_deviations() -> dict[str, Any]:
    rows = store.rows("deviation_record")
    return {"ok": True, "items": rows, "total": len(rows)}


@router.get("/actuals")
def list_actuals() -> dict[str, Any]:
    rows = store.rows("metric_actual")
    return {"ok": True, "items": rows, "total": len(rows)}
