"""Offline checks for evidence grades and the daily report.

These tests build temporary directories. They do not connect to a device,
import uiautomator2, or call a network API.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import evidence
import report


def _event(payload: dict) -> str:
    return json.dumps({"time": "2026-11-01T21:10:00+08:00", **payload}, ensure_ascii=False)


def _write_events(directory: Path, events: list[dict]) -> None:
    text = "\n".join(_event(event) for event in events) + "\n"
    (directory / "actions.jsonl").write_text(text, encoding="utf-8")


def _snapshot(directory: Path, index: int, label: str, xml: str = "<hierarchy />", png: bytes = b"png") -> None:
    stem = f"{index:02d}_{label}"
    (directory / f"{stem}.png").write_bytes(png)
    (directory / f"{stem}.xml").write_text(xml, encoding="utf-8")


def _result(status: str, stop_reason: str = "已结束") -> dict:
    return {"event": "result", "status": status, "stop_reason": stop_reason, "error": "", "duration_seconds": 3}


class EvidenceGradeTests(unittest.TestCase):
    def test_empty_directory_refuses_inference(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            assessed = evidence.assess_app_dir(Path(raw))
        self.assertEqual(assessed["grade"], "refuse")
        self.assertTrue(any("没有可阅读" in item for item in assessed["refusals"]))
        self.assertEqual(len(assessed["refusals"]), 1)

    def test_complete_steps_are_quotable_and_still_limit_baseline_and_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "澄市"
            app.mkdir()
            _snapshot(app, 1, "01_launch")
            _snapshot(app, 2, "03_search_results")
            _snapshot(app, 3, "04_product_detail")
            _write_events(app, [_result("reached_checkout_safely", "已安全进入确认订单结算页")])
            assessed = evidence.assess_app_dir(app)
        self.assertEqual(assessed["grade"], "quotable_with_limits")
        self.assertIn("baseline_absent", assessed["flags"])
        self.assertIn("experiment_bucket_missing", assessed["flags"])
        self.assertTrue(any("不能归因到 A/B" in item for item in assessed["standing_limits"]))
        self.assertTrue(any("不写相对上一期" in item for item in assessed["refusals"]))
        self.assertEqual(assessed["missing_core_steps"], [])

    def test_benign_popup_does_not_downgrade_later_steps(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "app"
            baseline = Path(raw) / "baseline" / "app"
            app.mkdir()
            baseline.mkdir(parents=True)
            _snapshot(app, 1, "01_launch")
            _snapshot(app, 2, "03_search_results")
            _snapshot(app, 3, "04_product_detail")
            _write_events(app, [
                {"event": "dismiss_popup", "text": "我知道了"},
                _result("reached_checkout_safely"),
            ])
            assessed = evidence.assess_app_dir(app, baseline.parent)
        self.assertEqual(assessed["grade"], "quotable_with_limits")
        self.assertIn("popup_visual_not_captured", assessed["flags"])
        self.assertNotIn("destructive_popup_dismiss", assessed["flags"])
        self.assertIn("baseline_present", assessed["flags"])
        self.assertTrue(any("不能描述弹窗布局" in item for item in assessed["refusals"]))

    def test_destructive_dismiss_and_coordinate_block_product_conclusions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "app"
            app.mkdir()
            _snapshot(app, 1, "01_launch")
            _snapshot(app, 2, "03_search_results")
            _snapshot(app, 3, "04_product_detail")
            _write_events(app, [
                {"event": "dismiss_popup", "text": "放弃优惠"},
                {"event": "tap", "selector": "coordinate=(250,1150)", "purpose": "first_product"},
                _result("reached_checkout_safely"),
            ])
            assessed = evidence.assess_app_dir(app)
        self.assertEqual(assessed["grade"], "describe_only")
        self.assertIn("destructive_popup_dismiss", assessed["flags"])
        self.assertIn("coordinate_fallback_used", assessed["flags"])
        joined = "\n".join(assessed["refusals"])
        self.assertIn("放弃优惠", joined)
        self.assertIn("坐标", joined)

    def test_authorization_tap_is_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "app"
            app.mkdir()
            _snapshot(app, 1, "01_launch")
            _write_events(app, [
                {"event": "dismiss_popup", "text": "允许"},
                _result("stopped_login_or_risk_protection", "检测到登录或风控保护"),
            ])
            assessed = evidence.assess_app_dir(app)
        self.assertEqual(assessed["grade"], "describe_only")
        self.assertIn("destructive_popup_dismiss", assessed["flags"])
        self.assertIn("login_or_risk_blocked", assessed["flags"])
        self.assertNotIn("core_step_missing", assessed["flags"])

    def test_broad_payment_term_uses_runner_wording(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "app"
            app.mkdir()
            _snapshot(app, 1, "01_launch")
            _snapshot(app, 2, "03_search_results")
            _snapshot(app, 3, "04_product_detail")
            _write_events(app, [
                _result("stopped_payment_page", "检测到支付方式/收银台页面 (匹配项: 支付宝)，立即停止"),
            ])
            broad = evidence.assess_app_dir(app)
            _write_events(app, [
                _result("stopped_payment_page", "检测到支付方式/收银台页面 (匹配项: 支付宝收银台)，立即停止"),
            ])
            specific = evidence.assess_app_dir(app)
        self.assertIn("broad_payment_match", broad["flags"])
        self.assertEqual(broad["grade"], "describe_only")
        self.assertTrue(any("不能断言已经进入收银台" in item for item in broad["refusals"]))
        self.assertNotIn("broad_payment_match", specific["flags"])
        self.assertNotIn("payment_stop_unspecified", specific["flags"])

    def test_failed_collection_without_files_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "app"
            app.mkdir()
            _write_events(app, [{"event": "error", "error": "找不到搜索框"}])
            assessed = evidence.assess_app_dir(app)
        self.assertEqual(assessed["grade"], "refuse")
        self.assertIn("collection_failed", assessed["flags"])
        self.assertTrue(any("不能解释成产品没有对应能力" in item for item in assessed["refusals"]))

    def test_unpaired_screenshot_is_describe_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "app"
            app.mkdir()
            _snapshot(app, 1, "01_launch")
            _snapshot(app, 2, "03_search_results")
            _snapshot(app, 3, "04_product_detail")
            detail_xml = next(app.glob("*04_product_detail.xml"))
            detail_xml.unlink()
            _write_events(app, [_result("reached_checkout_safely")])
            assessed = evidence.assess_app_dir(app)
        self.assertEqual(assessed["grade"], "describe_only")
        self.assertIn("screenshot_xml_unpaired", assessed["flags"])
        self.assertNotIn("no_ui_trees", assessed["flags"])


class DailyReportTests(unittest.TestCase):
    def test_missing_device_fields_stay_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw) / "run"
            app = run / "demo"
            app.mkdir(parents=True)
            _snapshot(app, 1, "01_launch")
            _snapshot(app, 2, "03_search_results")
            _snapshot(app, 3, "04_product_detail")
            _write_events(app, [_result("reached_checkout_safely")])
            path = report.generate(run, query="洗衣液")
            text = path.read_text(encoding="utf-8")
            payload = json.loads((run / "report.json").read_text(encoding="utf-8"))
        self.assertIn("证据质量与推断边界", text)
        self.assertIn("日报和变化摘要", text)
        self.assertIn("拒绝推断", text)
        self.assertNotIn("24030PN60C", text)
        self.assertNotIn("1440x3200", text)
        self.assertNotIn("OS1.0", text)
        self.assertNotIn("Xiaomi 14 Ultra", text)
        self.assertEqual(payload["report_role"], "daily_change_summary")
        self.assertEqual(payload["evidence"][0]["grade"], "quotable_with_limits")
        self.assertIn("营销与价格优惠信息清单", text)

    def test_recorded_device_is_shown_and_change_clue_is_not_a_redesign(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = root / "run"
            baseline = root / "baseline"
            current = run / "demo"
            previous = baseline / "demo"
            current.mkdir(parents=True)
            previous.mkdir(parents=True)
            _snapshot(previous, 1, "01_launch", xml="<hierarchy><node text=\"红包\" /></hierarchy>", png=b"old")
            _snapshot(current, 1, "01_launch", xml="<hierarchy><node text=\"优惠券立减\" /></hierarchy>", png=b"new")
            _snapshot(current, 2, "03_search_results")
            _snapshot(current, 3, "04_product_detail")
            _write_events(current, [_result("reached_checkout_safely")])
            (run / "device_env.json").write_text(json.dumps({
                "host": {"system": "Linux", "release": "6.1"},
                "device": {
                    "brand": "Example",
                    "model": "Phone",
                    "android_version": "14",
                    "screen_size": "1000x2000",
                    "screen_density": 400,
                    "connected_serial": "SERIAL-TEST",
                },
            }), encoding="utf-8")
            path = report.generate(run, baseline, query="洗衣液")
            text = path.read_text(encoding="utf-8")
        self.assertIn("Example / Phone", text)
        self.assertIn("SERIAL-TEST", text)
        self.assertIn("1000x2000", text)
        self.assertIn("可考虑人工专题", text)
        self.assertIn("不是改版结论", text)
        self.assertIn("优惠券", text)
        self.assertIn("本次未再现", text)
        self.assertNotIn("失效优惠", text)
        self.assertNotIn("完全一致", text)

    def test_marketing_absence_does_not_invent_promotions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw) / "run"
            app = run / "demo"
            app.mkdir(parents=True)
            _snapshot(app, 1, "01_launch", xml="<hierarchy><node text=\"设置\" /></hierarchy>")
            _write_events(app, [_result("error", "找不到搜索框")])
            # error result is authoritative
            path = report.generate(run)
            text = path.read_text(encoding="utf-8")
            payload = json.loads(path.with_name("report.json").read_text(encoding="utf-8"))
        self.assertIn("未在当前流程页面中检测到显著营销文案", text)
        self.assertEqual(payload["evidence"][0]["grade"], "describe_only")
        self.assertIn("collection_failed", payload["evidence"][0]["flags"])


if __name__ == "__main__":
    unittest.main()
