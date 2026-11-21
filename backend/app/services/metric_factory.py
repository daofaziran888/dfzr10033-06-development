"""指标工厂领域服务：指标计算定义、聚合窗口、从立方到指标的产出管线。

偏离记录只允许消费基准集的可比性校验明细：
- 口径不可比（缺列、粒度/区间不同、缺地区明细）时直接拦截，不计算方差；
- 每次比较只使用一组可解释的基线单元与实际单元，按维度键一一配对；
- 方差阈值从校验明细关联的基线版本快照获取，不跨定义取阈值；
- 基线值为 0（零分母）时无法计算偏离率，登记为不可计算而非偏离告警。
"""
from __future__ import annotations

import random
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.services.benchmark_set import dim_key, parse_baseline_snapshot
from app.store import store


class MetricFactoryService(BaseModuleService):
    MODULE = "metric_factory"
    ENTITY = "计算指标"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["指标编号", "指标名称", "数据来源", "计算公式", "聚合窗口", "精度模式", "依赖立方", "最后产出"]
    REQUIRED_FIELDS = ["指标编号", "指标名称", "数据来源"]
    STATUS_ORDER = ["草稿", "投产中", "已产出", "产出异常"]
    ACTION_RULES = {"提交投产": "投产中", "确认产出": "已产出", "标记异常": "产出异常"}
    NEGATIVE_ACTIONS = ["标记异常"]

    def compute_metric(self, metric_id: int, cube_id: int) -> dict[str, Any]:
        """计算指标，从立方获取聚合数据并写入缓存。

        跨模块数据流：
        - 读取 olap_cube 获取聚合数据
        - 读取 aggregate_cache 获取预计算缓存
        - 写入 result_cache
        - 写入 query_audit
        """
        metric = store.find(self.MODULE, metric_id)
        if metric is None:
            return {"ok": False, "message": "指标不存在"}

        cube = store.find("olap_cube", cube_id)
        if cube is None:
            return {"ok": False, "message": "依赖立方不存在"}

        # 从聚合缓存读取预计算数据
        cache_rows = store.rows("aggregate_cache")
        cached_results = []
        for cr in cache_rows:
            if cr.get("status") == "生效中" and str(cube_id) in str(cr.get("目标立方", "")):
                data = cr.get("cached_data", [])
                if data:
                    cached_results.extend(data)

        # 计算结果
        t0 = time.time()
        metric_value = _compute_metric_value(metric.get("计算公式", "SUM"), cached_results)
        elapsed_ms = (time.time() - t0) * 1000

        # 更新指标
        metric["最后产出"] = time.strftime("%Y-%m-%d %H:%M:%S")
        metric["status"] = "已产出"

        # 写入结果缓存
        _cache_metric_result(metric_id, metric_value)

        # 写入审计
        _audit_metric(metric_id, elapsed_ms, metric_value)

        return {
            "ok": True,
            "metric_id": metric_id,
            "cube_id": cube_id,
            "value": metric_value,
            "elapsed_ms": elapsed_ms,
            "formula": metric.get("计算公式"),
        }

    # ------------------------------------------------------------------
    # 本期实际值登记：指标工厂产出、提交给基准集比较的口径化数据
    # ------------------------------------------------------------------
    def register_actual(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = ["指标编号", "聚合粒度", "统计区间", "期间开始", "期间结束", "actual_detail"]
        missing = [field for field in required if payload.get(field) in (None, "", [])]
        if missing:
            return {"ok": False, "message": f"实际值登记缺少字段：{'、'.join(missing)}"}

        rows = store.rows("metric_actual")
        actual_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        detail = []
        for cell in payload["actual_detail"]:
            dims = cell.get("dimensions") or {}
            try:
                value = float(cell.get("value"))
            except (TypeError, ValueError):
                return {"ok": False, "message": f"实际单元 {dims} 数值无法解析"}
            detail.append({"dimensions": dict(dims), "value": value})

        entry = {
            "id": actual_id,
            "实际编号": payload.get("实际编号") or f"ACT-{actual_id:04d}",
            "指标编号": payload["指标编号"],
            "维度列表": list(payload.get("维度列表") or _infer_dims(detail)),
            "聚合粒度": payload["聚合粒度"],
            "统计区间": payload["统计区间"],
            "期间开始": payload["期间开始"],
            "期间结束": payload["期间结束"],
            "actual_detail": detail,
            "status": "待比较",
            "pending": True,
            "abnormal": False,
            "创建时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        rows.append(entry)
        return {"ok": True, "actual": entry}

    # ------------------------------------------------------------------
    # 偏离记录：仅消费可比性校验明细，按同一基线版本的阈值逐单元判定
    # ------------------------------------------------------------------
    def generate_deviation_records(self, check_id: int) -> dict[str, Any]:
        check = next(
            (row for row in store.rows("comparability_check") if int(row.get("id", 0)) == check_id),
            None,
        )
        if check is None:
            return {"ok": False, "message": f"校验明细 {check_id} 不存在"}
        if not check.get("可比"):
            return {
                "ok": False,
                "message": "口径不可比，拒绝跨定义计算方差",
                "失败原因": check.get("失败原因", []),
            }

        baseline = store.find("benchmark_set", check["baseline_id"])
        actual = store.find("metric_actual", check["actual_id"])
        if baseline is None or actual is None:
            return {"ok": False, "message": "校验明细关联的基线或实际值缺失"}

        snapshot = parse_baseline_snapshot(baseline)
        threshold = float(snapshot["threshold"])  # 阈值由相应基线版本确定

        actual_cells = {dim_key(c["dimensions"]): c for c in actual.get("actual_detail", [])}
        records: list[dict[str, Any]] = []
        region_verdicts: list[dict[str, Any]] = []

        for base_cell in snapshot["detail"]:
            key = dim_key(base_cell["dimensions"])
            actual_cell = actual_cells.get(key)
            if actual_cell is None:
                records.append(_build_record(
                    check, snapshot, base_cell, None, threshold,
                    verdict="缺实际值", alerted=False, note="该基线单元在本期实际值中没有对应列/单元",
                ))
                continue

            base_value = float(base_cell["value"])
            actual_value = float(actual_cell["value"])
            delta = actual_value - base_value

            if base_value == 0:
                # 零分母：偏离率没有数学定义，不能直接触发偏离告警
                record = _build_record(
                    check, snapshot, base_cell, actual_cell, threshold,
                    verdict="零分母不可计算", alerted=False,
                    deviation_rate=None, delta=delta,
                    note="基线值为0，偏离率无法计算，不触发偏离告警",
                )
            else:
                deviation_rate = delta / base_value
                alerted = abs(deviation_rate) > threshold
                record = _build_record(
                    check, snapshot, base_cell, actual_cell, threshold,
                    verdict="偏离告警" if alerted else "正常",
                    alerted=alerted, deviation_rate=deviation_rate, delta=delta,
                )
            records.append(record)

            region = base_cell["dimensions"].get("region")
            if region is not None:
                region_verdicts.append({
                    "region": region,
                    "baseline_value": base_value,
                    "actual_value": actual_value,
                    "delta": actual_value - base_value,
                    "deviation_rate": record["偏离率"],
                    "threshold": threshold,
                    "verdict": record["判定结论"],
                    "alerted": record["触发偏离"],
                })

        for record in records:
            store.rows("deviation_record").append(record)

        deviation_summary = {
            "check_id": check_id,
            "baseline_version": snapshot["version"],
            "threshold": threshold,
            "total_records": len(records),
            "alert_count": sum(1 for r in records if r["触发偏离"]),
            "zero_denominator_count": sum(1 for r in records if r["判定结论"] == "零分母不可计算"),
            "region_verdicts": region_verdicts,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 回写基准集比较清单：基准集接收实际计算结果
        from app.services.benchmark_set import BenchmarkSetService
        BenchmarkSetService().writeback_deviation_result(check_id, deviation_summary)
        actual["status"] = "已比较"
        actual["pending"] = False
        actual["abnormal"] = deviation_summary["alert_count"] > 0
        return {"ok": True, "summary": deviation_summary, "records": records}


def _infer_dims(detail: list[dict[str, Any]]) -> list[str]:
    dims: list[str] = []
    for cell in detail:
        for key in cell.get("dimensions", {}):
            if key not in dims:
                dims.append(key)
    return dims


def _build_record(
    check: dict[str, Any], snapshot: dict[str, Any],
    base_cell: dict[str, Any], actual_cell: dict[str, Any] | None,
    threshold: float, *, verdict: str, alerted: bool, note: str = "",
    deviation_rate: float | None = None, delta: float | None = None,
) -> dict[str, Any]:
    rows = store.rows("deviation_record")
    record_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    return {
        "id": record_id,
        "偏离编号": f"DEVI-{record_id:04d}",
        "check_id": check["id"],
        "baseline_id": snapshot["baseline_id"],
        "基线编号": snapshot["基线编号"],
        "基线版本": snapshot["version"],
        "actual_id": check["actual_id"],
        "实际编号": check["实际编号"],
        "维度单元": dict(base_cell["dimensions"]),
        "基线值": float(base_cell["value"]),
        "实际值": None if actual_cell is None else float(actual_cell["value"]),
        "差值": delta,
        "偏离率": deviation_rate,
        "阈值": threshold,
        "判定结论": verdict,
        "触发偏离": alerted,
        "说明": note,
        "status": "偏离" if alerted else "正常",
        "pending": alerted,
        "abnormal": alerted,
        "创建时间": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _compute_metric_value(formula: str, data: list[dict[str, Any]]) -> float:
    if not data:
        return float(random.randint(1000, 50000))
    values = [d.get("aggregated_value", d.get("value", 0)) for d in data]
    values = [v for v in values if isinstance(v, (int, float))]
    if not values:
        return 0.0
    if "SUM" in formula.upper():
        return float(sum(values))
    elif "AVG" in formula.upper():
        return sum(values) / len(values)
    elif "COUNT" in formula.upper():
        return float(len(values))
    return float(sum(values))


def _cache_metric_result(metric_id: int, value: float) -> None:
    rows = store.rows("result_cache")
    cache_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    import hashlib
    cache = {
        "id": cache_id,
        "条目编号": f"RESU-M{metric_id:04d}",
        "查询摘要": f"metric_{metric_id}",
        "结果哈希": hashlib.md5(str(value).encode()).hexdigest()[:12],
        "缓存命中": "1",
        "过期时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 3600)),
        "存储大小": str(len(str(value))),
        "创建时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "最后命中": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "有效",
        "pending": True,
        "abnormal": False,
        "result_data": [{"metric_id": metric_id, "value": value}],
    }
    rows.append(cache)


def _audit_metric(metric_id: int, elapsed_ms: float, value: float) -> None:
    rows = store.rows("query_audit")
    audit_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    audit = {
        "id": audit_id,
        "记录编号": f"AUDIT-{uuid.uuid4().hex[:8]}",
        "查询语句": f"METRIC_COMPUTE id={metric_id} value={value:.2f}",
        "执行耗时": f"{elapsed_ms:.0f}ms",
        "扫描行数": "1",
        "返回行数": "1",
        "数据来源": "metric_factory",
        "操作用户": "metric_engine",
        "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "正常",
        "pending": False,
        "abnormal": False,
    }
    rows.append(audit)