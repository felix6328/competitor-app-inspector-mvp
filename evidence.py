#!/usr/bin/env python3
"""Offline evidence grading for one inspection app directory.

Reads files that runner.py has already written. Does not connect to a device,
does not read UI copy into the grade, and does not invent missing steps.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# payment_stop_texts 里这些词也会出现在非收银台页面。命中它们只能说明文本出现过。
BROAD_PAYMENT_TERMS = frozenset({
    "支付宝",
    "微信支付",
    "银行卡",
    "信用卡",
    "借记卡",
    "闪付",
    "花呗",
    "白条",
    "云闪付",
})

# dismiss_popup_texts 中可能改变权益或授权的文案。
DESTRUCTIVE_DISMISS_TEXTS = (
    "放弃优惠",
    "同意并使用",
    "始终允许",
    "仅在使用中允许",
    "同意",
    "允许",
)

# runner.Inspector.run 在走完搜索和详情时一定会尝试留下的标签。
# 文件名是 "{序号}_{标签}"，与 report 的 step_key 对齐时去掉第一段序号。
CORE_STEP_LABELS = ("01_launch", "03_search_results", "04_product_detail")

FLAG_ORDER = (
    "evidence_empty",
    "missing_actions_log",
    "missing_result_event",
    "collection_failed",
    "login_or_risk_blocked",
    "safety_boundary",
    "sensitive_input_stop",
    "payment_boundary_stop",
    "broad_payment_match",
    "payment_stop_unspecified",
    "popup_dismissed",
    "destructive_popup_dismiss",
    "popup_visual_not_captured",
    "coordinate_fallback_used",
    "screenshot_xml_unpaired",
    "no_screenshots",
    "no_ui_trees",
    "core_step_missing",
    "baseline_absent",
    "baseline_app_missing",
    "baseline_present",
    "experiment_bucket_missing",
)

FLAG_LABELS = {
    "evidence_empty": "没有可阅读证据",
    "missing_actions_log": "没有动作日志",
    "missing_result_event": "没有权威结束事件",
    "collection_failed": "采集失败",
    "login_or_risk_blocked": "登录或风控阻塞",
    "safety_boundary": "安全拦截",
    "sensitive_input_stop": "敏感输入页停止",
    "payment_boundary_stop": "支付边界停止",
    "broad_payment_match": "支付泛词命中",
    "payment_stop_unspecified": "支付停止词未指明",
    "popup_dismissed": "点击过弹窗按钮",
    "destructive_popup_dismiss": "点击过权益或授权文案",
    "popup_visual_not_captured": "弹窗外观未留存",
    "coordinate_fallback_used": "使用了坐标兜底",
    "screenshot_xml_unpaired": "截图与 UI 树不成对",
    "no_screenshots": "没有截图",
    "no_ui_trees": "没有 UI 树",
    "core_step_missing": "核心步骤缺失",
    "baseline_absent": "未提供基线",
    "baseline_app_missing": "基线中没有该平台",
    "baseline_present": "已提供该平台基线",
    "experiment_bucket_missing": "实验分组未采集",
}

REFUSAL_TEXT = {
    "evidence_empty": "目录里没有可阅读的巡检证据，不能撰写该平台的体验结论。",
    "collection_failed": "采集失败。没有走到的步骤记为缺失，不能解释成产品没有对应能力。",
    "login_or_risk_blocked": "登录或风控截断了旅程。只描述阻塞现场，不比较登录后的页面，也不推测实验分组。",
    "coordinate_fallback_used": "该步使用了坐标点击或坐标输入。落点不能当成用户选择，也不能当成信息架构上的入口。",
    "destructive_popup_dismiss": "自动点击过「放弃优惠」或授权类文案。随后页面不能当作未被巡检改写的用户路径。",
    "popup_visual_not_captured": "弹窗在留下自身截图之前就被点掉。可以引用日志中的按钮文案，不能描述弹窗布局或视觉层级。",
    "screenshot_xml_unpaired": "截图与 UI 树不成对。缺的那一半不补写。",
    "broad_payment_match": "支付停止命中的是泛词。只能说明界面文本里出现过该词，不能断言已经进入收银台。",
    "payment_stop_unspecified": "结果写了支付边界停止，但没有留下命中的具体文案。页面类型需人工看截图，不能从状态名反推。",
    "no_screenshots": "没有截图。不写视觉层级、布局或页面外观。",
    "no_ui_trees": "没有 UI 树。不把截图上的主观阅读写成可复核的文案事实。",
    "core_step_missing": "约定的核心步骤文件缺失。缺失步骤不参与比较。",
    "baseline_absent": "没有基线。本次只作当次观察，不写相对上一期的改版。",
    "baseline_app_missing": "基线目录里没有该平台。不写该平台的本期变化。",
    "missing_result_event": "没有 result 事件。结束状态来自启发式，置信度低于 runner 在结束时写入的结果。",
    "sensitive_input_stop": "出现敏感输入提示后已停止。不记录、不推测任何凭据，也不把该页写成可完成的支付体验。",
    "safety_boundary": "触发了安全拦截。拦截之后没有发生的点击，不能补写成用户路径。",
    "experiment_bucket_missing": "没有实验分组记录。页面差异不能归因到 A/B。",
}

GRADE_LABELS = {
    "refuse": "拒绝产品推断",
    "describe_only": "只可描述采集标记",
    "quotable_with_limits": "可引用，但须附原始文件并遵守限度",
}

# 这些标记会污染整段旅程的产品解释。弹窗外观缺失只限制弹窗本身，不降级后续已成对的步骤。
DESCRIBE_ONLY_FLAGS = frozenset({
    "collection_failed",
    "login_or_risk_blocked",
    "safety_boundary",
    "sensitive_input_stop",
    "broad_payment_match",
    "payment_stop_unspecified",
    "destructive_popup_dismiss",
    "coordinate_fallback_used",
    "screenshot_xml_unpaired",
    "no_screenshots",
    "no_ui_trees",
    "core_step_missing",
    "missing_result_event",
})

# 需要写进“拒绝推断”的标记。实验分组缺失另作常驻限度。
REFUSAL_FLAGS = DESCRIBE_ONLY_FLAGS | {
    "evidence_empty",
    "popup_visual_not_captured",
    "baseline_absent",
    "baseline_app_missing",
}


def step_key(stem: str) -> str:
    """Align with report.compare_with_baseline: drop the leading snapshot index."""
    return stem.split("_", 1)[-1]


def load_events(app_dir: Path) -> list[dict[str, Any]]:
    path = app_dir / "actions.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _is_destructive_dismiss(value: str) -> bool:
    """权益和授权按钮用整词；更长的固定文案允许包含匹配。"""
    for word in DESTRUCTIVE_DISMISS_TEXTS:
        if word in {"同意", "允许"}:
            if value == word or value.startswith(word):
                return True
        elif word in value:
            return True
    return False


def _matched_payment_term(stop_reason: str) -> str | None:
    """Pull the matched UI phrase out of runner.py stop reasons.

    Runner writes halfwidth parentheses, for example ``(匹配项: 支付宝)``.
    """
    for pattern in (r"匹配项:\s*(.+)", r"检测到:\s*(.+)"):
        found = re.search(pattern, stop_reason)
        if not found:
            continue
        term = re.split(r"[)，。；,)]", found.group(1).strip(), maxsplit=1)[0].strip()
        if term:
            return term
    return None


def _snapshot_labels(events: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for event in events:
        if event.get("event") == "snapshot" and event.get("label"):
            labels.append(str(event["label"]))
    return labels


def _dismiss_values(event: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("text", "description"):
        raw = event.get(key)
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    return values


def _uses_coordinate(event: dict[str, Any]) -> bool:
    name = str(event.get("event") or "")
    if "coordinate" in name:
        return True
    selector = str(event.get("selector") or "")
    method = str(event.get("method") or "")
    return selector.startswith("coordinate=") or method.startswith("coordinate:")


def _ordered_flags(flags: set[str]) -> list[str]:
    return [flag for flag in FLAG_ORDER if flag in flags]


def assess_app_dir(app_dir: Path, baseline_dir: Path | None = None) -> dict[str, Any]:
    """Grade one app directory. `baseline_dir` is the run-level baseline, if any."""
    events = load_events(app_dir)
    pngs = {step_key(path.stem) for path in app_dir.glob("*.png")}
    xmls = {step_key(path.stem) for path in app_dir.glob("*.xml")}
    observed = sorted(pngs | xmls)
    flags: set[str] = {"experiment_bucket_missing"}

    if not (app_dir / "actions.jsonl").exists():
        flags.add("missing_actions_log")
    if not pngs:
        flags.add("no_screenshots")
    if not xmls:
        flags.add("no_ui_trees")
    if pngs.symmetric_difference(xmls):
        flags.add("screenshot_xml_unpaired")
    if not events and not pngs and not xmls:
        flags.add("evidence_empty")

    result_events = [event for event in events if event.get("event") == "result"]
    result = result_events[-1] if result_events else {}
    if not result_events:
        flags.add("missing_result_event")
    status = str(result.get("status") or "")
    stop_reason = str(result.get("stop_reason") or "")
    if not status:
        for event in events:
            if event.get("event") == "error":
                status = "error"
                stop_reason = str(event.get("error") or stop_reason)
    if status == "error" or any(event.get("event") == "error" for event in events):
        # result 事件是权威结束状态。只有它本身为 error，或根本没有 result
        # 却出现 error 事件时，才算采集失败。安全停止前的重试错误不升级。
        if status == "error" or not result_events:
            flags.add("collection_failed")

    if status.startswith("stopped_login"):
        flags.add("login_or_risk_blocked")
    if status == "stopped_safety_boundary" or any(event.get("event") in {"safety_halt", "safety_intercept"} for event in events):
        flags.add("safety_boundary")
    if status == "stopped_sensitive_input_detected":
        flags.add("sensitive_input_stop")
    if status == "stopped_payment_page" or "payment" in status:
        flags.add("payment_boundary_stop")
        term = _matched_payment_term(stop_reason)
        if term is None:
            flags.add("payment_stop_unspecified")
        elif term in BROAD_PAYMENT_TERMS:
            flags.add("broad_payment_match")

    dismiss_events = [event for event in events if event.get("event") == "dismiss_popup"]
    if dismiss_events:
        flags.add("popup_dismissed")
        for event in dismiss_events:
            if any(_is_destructive_dismiss(value) for value in _dismiss_values(event)):
                flags.add("destructive_popup_dismiss")
        snapshot_labels = _snapshot_labels(events)
        if not any("popup" in label.lower() for label in snapshot_labels):
            flags.add("popup_visual_not_captured")

    if any(_uses_coordinate(event) for event in events):
        flags.add("coordinate_fallback_used")

    early_stop = "login_or_risk_blocked" in flags and "03_search_results" not in observed
    launched_into_payment = (
        "payment_boundary_stop" in flags
        and "03_search_results" not in observed
        and not any(event.get("event") == "input" for event in events)
    )
    if not early_stop and not launched_into_payment:
        missing_core = [label for label in CORE_STEP_LABELS if label not in observed]
        if missing_core and "evidence_empty" not in flags:
            flags.add("core_step_missing")

    app_baseline = (baseline_dir / app_dir.name) if baseline_dir else None
    if baseline_dir is None:
        flags.add("baseline_absent")
    elif app_baseline is None or not app_baseline.exists():
        flags.add("baseline_app_missing")
    else:
        flags.add("baseline_present")

    ordered = _ordered_flags(flags)
    if "evidence_empty" in flags or ({"no_screenshots", "no_ui_trees"} <= flags and "collection_failed" in flags):
        grade = "refuse"
    elif DESCRIBE_ONLY_FLAGS & flags:
        grade = "describe_only"
    else:
        grade = "quotable_with_limits"

    if "evidence_empty" in flags:
        refusals = [REFUSAL_TEXT["evidence_empty"]]
    else:
        refusals = [REFUSAL_TEXT[flag] for flag in ordered if flag in REFUSAL_FLAGS]

    standing_limits = [
        REFUSAL_TEXT["experiment_bucket_missing"],
        "单次、单设备和单一账号路径只覆盖该次现场，不能写成全部用户。",
    ]
    missing_core = [label for label in CORE_STEP_LABELS if label not in observed]
    return {
        "app": app_dir.name,
        "grade": grade,
        "grade_label": GRADE_LABELS[grade],
        "flags": ordered,
        "flag_labels": [FLAG_LABELS[flag] for flag in ordered],
        "refusals": refusals,
        "standing_limits": standing_limits,
        "observed_steps": observed,
        "missing_core_steps": missing_core,
        "result_status": status or "缺失",
        "result_stop_reason": stop_reason or "缺失",
        "has_authoritative_result": bool(result_events),
        "screenshot_count": len(list(app_dir.glob("*.png"))),
        "xml_count": len(list(app_dir.glob("*.xml"))),
    }
