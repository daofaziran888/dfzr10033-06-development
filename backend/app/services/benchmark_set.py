"""基准集领域服务：基线口径解析、可比性校验、地区明细补录与比较清单回写。

口径可比性是偏离计算的前置条件：
- 维度集合、聚合粒度、统计区间口径逐项核对，缺列/粒度不同分别单独判定；
- 每次比较只绑定一组可解释的基线与实际值，不允许跨定义计算方差；
- 历史基线（扁平字段 + 字符串方差阈值、无结构化明细）由解析器兼容归一；
- 锁定后口径不可变，只允许补录同一统计区间、合计守恒的地区明细。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# -- 可比性核对结论码 --
DIMENSION_MATCH = "DIMENSION_MATCH"
DIMENSION_MISSING_COLUMN = "DIMENSION_MISSING_COLUMN"
DIMENSION_EXTRA_COLUMN = "DIMENSION_EXTRA_COLUMN"
GRANULARITY_MATCH = "GRANULARITY_MATCH"
GRANULARITY_MISMATCH = "GRANULARITY_MISMATCH"
PERIOD_MATCH = "PERIOD_MATCH"
PERIOD_MISMATCH = "PERIOD_MISMATCH"
REGION_DETAIL_MATCH = "REGION_DETAIL_MATCH"
MISSING_REGION_DETAIL = "MISSING_REGION_DETAIL"

COMPARABLE = "COMPARABLE"
INCOMPARABLE = "INCOMPARABLE"

TOTAL_REGION = "全国"


class BenchmarkSetService(BaseModuleService):
    MODULE = "benchmark_set"
    ENTITY = "分析基线"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["基线编号", "基线名称", "基线版本", "对比维度", "聚合粒度", "统计区间", "方差阈值", "锁定状态"]
    REQUIRED_FIELDS = ["基线编号", "基线名称", "数据范围"]
    STATUS_ORDER = ["草稿", "已锁定", "对比中", "已过期"]
    ACTION_RULES = {"锁定基线": "已锁定", "启动对比": "对比中", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 基线解析：兼容历史阈值与适用范围
    # ------------------------------------------------------------------
    def parse_baseline(self, baseline_id: int) -> dict[str, Any]:
        """读取并归一基线口径，历史扁平格式与新结构化格式统一输出快照。"""
        baseline = store.find(self.MODULE, baseline_id)
        if baseline is None:
            raise ValueError(f"分析基线 {baseline_id} 不存在或已归档")
        return parse_baseline_snapshot(baseline)

    # ------------------------------------------------------------------
    # 可比性校验：生成供指标工厂使用的校验明细
    # ------------------------------------------------------------------
    def check_comparability(self, baseline_id: int, actual_id: int) -> dict[str, Any]:
        baseline = store.find(self.MODULE, baseline_id)
        actual = store.find("metric_actual", actual_id)
        if baseline is None:
            return {"ok": False, "message": f"分析基线 {baseline_id} 不存在或已归档"}
        if actual is None:
            return {"ok": False, "message": f"本期实际指标 {actual_id} 不存在"}

        snapshot = parse_baseline_snapshot(baseline)
        reasons: list[str] = []

        # 1) 维度核对：实际值缺基线维度列 => 缺列；多出维度列 => 维度集合变化，单独判定
        base_dims = list(snapshot["dimensions"])
        actual_dims = [d.strip() for d in actual.get("维度列表", []) if str(d).strip()]
        missing_cols = [d for d in base_dims if d not in actual_dims]
        extra_cols = [d for d in actual_dims if d not in base_dims]
        if missing_cols:
            dimension_code = DIMENSION_MISSING_COLUMN
            reasons.append(f"实际值缺少基线维度列：{'、'.join(missing_cols)}")
        elif extra_cols:
            dimension_code = DIMENSION_EXTRA_COLUMN
            reasons.append(f"实际值新增基线没有的维度列：{'、'.join(extra_cols)}")
        else:
            dimension_code = DIMENSION_MATCH

        # 2) 聚合粒度核对：粒度不同须单独判定，不得混算
        base_granularity = snapshot["granularity"]
        actual_granularity = str(actual.get("聚合粒度", "")).strip()
        if base_granularity == actual_granularity:
            granularity_code = GRANULARITY_MATCH
        else:
            granularity_code = GRANULARITY_MISMATCH
            reasons.append(f"聚合粒度不同：基线={base_granularity}，实际={actual_granularity or '空'}")

        # 3) 统计区间核对：比较的是窗口口径（月/日），不是两期起止日期
        base_interval = snapshot["interval"]
        actual_interval = str(actual.get("统计区间", "")).strip()
        if base_interval == actual_interval:
            period_code = PERIOD_MATCH
        else:
            period_code = PERIOD_MISMATCH
            reasons.append(f"统计区间口径不同：基线={base_interval}，实际={actual_interval or '空'}")

        # 4) 地区明细可得性：按地区比较时，历史基线必须能追到地区
        needs_region = "region" in base_dims and base_granularity == "region"
        region_cells = [
            cell for cell in snapshot["detail"]
            if cell.get("dimensions", {}).get("region") not in (None, TOTAL_REGION)
        ]
        if needs_region and not region_cells:
            region_code = MISSING_REGION_DETAIL
            reasons.append("历史基线缺少可追到地区的明细数据，地区比较无法执行")
        else:
            region_code = REGION_DETAIL_MATCH

        comparable = not reasons
        check_row = {
            "id": self._next_id("comparability_check"),
            "校验编号": f"CMP-{self._next_id('comparability_check'):04d}",
            "baseline_id": baseline_id,
            "actual_id": actual_id,
            "基线编号": baseline.get("基线编号"),
            "实际编号": actual.get("实际编号"),
            "基线版本": snapshot["version"],
            "维度核对": dimension_code,
            "粒度核对": granularity_code,
            "区间核对": period_code,
            "地区明细核对": region_code,
            "缺失维度列": missing_cols,
            "新增维度列": extra_cols,
            "可比": comparable,
            "结论": COMPARABLE if comparable else INCOMPARABLE,
            "失败原因": reasons,
            "基线口径快照": snapshot,
            "实际口径": {
                "dimensions": actual_dims,
                "granularity": actual_granularity,
                "interval": actual_interval,
                "period": [actual.get("期间开始"), actual.get("期间结束")],
            },
            "status": "可比" if comparable else "不可比",
            "pending": not comparable,
            "abnormal": not comparable,
            "创建时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("comparability_check").append(check_row)

        self._writeback_comparison_list(
            baseline, check_row, deviation_summary=None,
            note="口径可比，等待指标工厂产出偏离记录" if comparable else "口径不可比，已拦截偏离计算",
        )
        return {"ok": True, "check": check_row}

    # ------------------------------------------------------------------
    # 地区历史明细补录：同一期、合计守恒，还原地区比较
    # ------------------------------------------------------------------
    def supply_region_detail(
        self, baseline_id: int, details: list[dict[str, Any]],
        period_start: str, period_end: str,
    ) -> dict[str, Any]:
        baseline = store.find(self.MODULE, baseline_id)
        if baseline is None:
            return {"ok": False, "message": f"分析基线 {baseline_id} 不存在或已归档"}
        snapshot = parse_baseline_snapshot(baseline)

        if period_start != snapshot["period_start"] or period_end != snapshot["period_end"]:
            return {
                "ok": False,
                "message": (
                    f"补录明细必须与基线同一统计区间：{snapshot['period_start']}~"
                    f"{snapshot['period_end']}，收到 {period_start}~{period_end}"
                ),
            }

        normalized: list[dict[str, Any]] = []
        for cell in details:
            dims = cell.get("dimensions") or {}
            if "region" not in dims:
                return {"ok": False, "message": "补录明细必须携带 region 维度"}
            try:
                value = float(cell.get("value"))
            except (TypeError, ValueError):
                return {"ok": False, "message": f"地区 {dims.get('region')} 的数值无法解析"}
            normalized.append({"dimensions": dict(dims), "value": value})

        # 合计守恒：补录地区合计必须等于锁定的全国总量，锁定口径不被改写
        total_cell = next(
            (c for c in snapshot["detail"] if c.get("dimensions", {}).get("region") == TOTAL_REGION),
            None,
        )
        if total_cell is not None:
            region_sum = sum(
                c["value"] for c in normalized if c["dimensions"].get("region") != TOTAL_REGION
            )
            if abs(region_sum - float(total_cell["value"])) > 1e-9:
                return {
                    "ok": False,
                    "message": (
                        f"补录地区合计 {region_sum:g} 与基线锁定的全国总量 "
                        f"{float(total_cell['value']):g} 不一致，拒绝补录"
                    ),
                }

        existing = baseline.get("baseline_detail") or snapshot["detail"]
        merged = {dim_key(c.get("dimensions", {})): c for c in existing}
        for cell in normalized:
            merged[dim_key(cell["dimensions"])] = cell
        baseline["baseline_detail"] = list(merged.values())
        baseline["region_restored"] = True
        baseline["补录时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return {
            "ok": True,
            "baseline_id": baseline_id,
            "restored_cells": normalized,
            "message": "同期地区历史明细已补录，可重新发起可比性校验",
        }

    # ------------------------------------------------------------------
    # 比较清单回写：基准集接收指标工厂的实际计算结果
    # ------------------------------------------------------------------
    def writeback_deviation_result(self, check_id: int, deviation_summary: dict[str, Any]) -> dict[str, Any]:
        check = next(
            (row for row in store.rows("comparability_check") if int(row.get("id", 0)) == check_id),
            None,
        )
        if check is None:
            return {"ok": False, "message": f"校验明细 {check_id} 不存在"}
        baseline = store.find(self.MODULE, check["baseline_id"])
        if baseline is None:
            return {"ok": False, "message": "关联基线不存在"}
        self._writeback_comparison_list(baseline, check, deviation_summary, note="偏离计算已回写")
        return {"ok": True, "comparison_list": baseline.get("comparison_list", [])}

    def get_comparison_list(self, baseline_id: int) -> dict[str, Any]:
        baseline = store.find(self.MODULE, baseline_id)
        if baseline is None:
            return {"ok": False, "message": f"分析基线 {baseline_id} 不存在或已归档"}
        return {
            "ok": True,
            "baseline_id": baseline_id,
            "基线编号": baseline.get("基线编号"),
            "comparison_list": baseline.get("comparison_list", []),
        }

    def _writeback_comparison_list(
        self, baseline: dict[str, Any], check_row: dict[str, Any],
        deviation_summary: dict[str, Any] | None, note: str,
    ) -> None:
        comparison_list = baseline.setdefault("comparison_list", [])
        comparison_list.append({
            "check_id": check_row["id"],
            "校验编号": check_row["校验编号"],
            "actual_id": check_row["actual_id"],
            "实际编号": check_row["实际编号"],
            "基线版本": check_row["基线版本"],
            "可比": check_row["可比"],
            "失败原因": check_row["失败原因"],
            "偏离汇总": deviation_summary,
            "备注": note,
            "更新时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        if check_row["可比"]:
            baseline["status"] = "对比中"
            baseline["pending"] = True

    @staticmethod
    def _next_id(table: str) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


# -- 模块级辅助函数，供指标工厂复用同一套口径解析 --

def parse_baseline_snapshot(baseline: dict[str, Any]) -> dict[str, Any]:
    """把历史/新版基线行归一为口径快照。

    历史格式：方差阈值为字符串、无基线版本、无结构化明细、适用范围缺失。
    新格式：baseline_detail / threshold_snapshot / 统计区间 结构化字段。
    """
    # 阈值：优先结构化快照，回退历史字符串字段「方差阈值」
    threshold = baseline.get("threshold_snapshot")
    if threshold is None:
        raw_threshold = baseline.get("方差阈值", "0")
        try:
            threshold = float(str(raw_threshold).strip())
        except (TypeError, ValueError):
            threshold = 0.0

    # 维度：优先结构化列表，回退历史「对比维度」字符串
    raw_dims = baseline.get("dimensions")
    if raw_dims is None:
        raw_dims = [d.strip() for d in str(baseline.get("对比维度", "")).split(",") if d.strip()]
    dimensions = [str(d).strip() for d in raw_dims if str(d).strip()]

    period_start = baseline.get("period_start") or str(baseline.get("基线时段", "")).strip()
    period_end = baseline.get("period_end") or period_start
    interval = str(baseline.get("interval") or baseline.get("统计区间") or "P1M").strip()
    granularity = str(baseline.get("聚合粒度") or ("region" if dimensions else "total")).strip()
    scope = str(baseline.get("适用范围") or "ALL").strip() or "ALL"
    version = str(baseline.get("基线版本") or "legacy-v0").strip()

    detail = baseline.get("baseline_detail")
    detail_inferred = False
    if not detail:
        # 历史基线只有一个总量数值，构造全国单元并标记为推断明细
        try:
            total_value = float(str(baseline.get("指标数值", "0")).strip())
        except (TypeError, ValueError):
            total_value = 0.0
        detail = [{"dimensions": {"region": TOTAL_REGION}, "value": total_value}]
        detail_inferred = True

    return {
        "baseline_id": int(baseline.get("id", 0)),
        "基线编号": baseline.get("基线编号"),
        "version": version,
        "dimensions": dimensions,
        "granularity": granularity,
        "interval": interval,
        "period_start": period_start,
        "period_end": period_end,
        "scope": scope,
        "threshold": float(threshold),
        "detail": [{"dimensions": dict(c.get("dimensions", {})), "value": float(c.get("value", 0))}
                   for c in detail],
        "detail_inferred": detail_inferred,
        "locked": baseline.get("锁定状态") == "locked" or baseline.get("status") == "已锁定",
    }


def dim_key(dimensions: dict[str, Any]) -> str:
    return ",".join(f"{k}={dimensions[k]}" for k in sorted(dimensions))
