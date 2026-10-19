#!/usr/bin/env python3
"""Android 竞品 App 路径巡检程序 (Xiaomi 14 Ultra / Android 14+ 真机适配)

【运行架构说明】
- 宿主机 (Host): Mac 或 Windows PC，运行本 Python 脚本 (runner.py)
- 被测设备 (DUT): 小米14 Ultra (HyperOS / Android 14+) 或其他 Android 10+ 真机，通过 USB 或局域网 Wi-Fi ADB 与宿主机连接
- 控制通道: uiautomator2 服务运行在手机端，宿主机通过 RPC 指令完成点击、滑动、输入与界面层次树采集

【绝对安全底线 (Zero Payment Risk)】
1. 严禁输入卡号、有效期、CVV、短信验证码、支付密码。
2. 严禁点击“提交订单”、“立即支付”、“确认支付”、“立即付款”、“去支付”、“极速支付”、“免密支付”等直接扣款按钮。
3. 到达银行卡/支付方式选择页或收银台立即停止并捕获快照。
4. 遇到登录拦截、滑动验证码、人机验证或风控提示时立即停止，绝不尝试绕过。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import uiautomator2 as u2
except ImportError:
    u2 = None

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent


class SafetyHaltException(Exception):
    """Triggered when safety boundary is reached or high-risk action intercepted."""
    pass


@dataclass
class AppResult:
    key: str
    name: str
    package: str
    status: str = "not_started"
    stop_reason: str = ""
    error: str = ""
    start_time: str = ""
    end_time: str = ""
    duration_seconds: float = 0.0
    steps: list[dict[str, Any]] = field(default_factory=list)
    marketing_signals: list[dict[str, Any]] = field(default_factory=list)


class Inspector:
    def __init__(self, device: Any, app: dict[str, Any], out_dir: Path, query: str, defaults: dict[str, Any]):
        self.d = device
        self.app = app
        self.out_dir = out_dir
        self.query = query
        self.defaults = defaults
        self.app_dir = out_dir / self._safe_name(app["display_name"])
        self.app_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.app_dir / "actions.jsonl"
        self.readable_log_path = self.app_dir / "execution.log"
        self.snapshot_index = 0
        self.marketing_taxonomy = defaults.get("marketing_taxonomy", {})
        self.prohibited_texts = [str(x).strip() for x in defaults.get("prohibited_action_texts", []) if x]
        self.payment_stop_texts = [str(x).strip() for x in defaults.get("payment_stop_texts", []) if x]
        self.sensitive_input_prompts = [str(x).strip() for x in defaults.get("sensitive_input_prompts", []) if x]
        self.login_or_risk_texts = [str(x).strip() for x in defaults.get("login_or_risk_texts", []) if x]
        self.dismiss_popup_texts = [str(x).strip() for x in defaults.get("dismiss_popup_texts", []) if x]

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", value)

    def log(self, event: dict[str, Any]) -> None:
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        event = {"time": now_iso, **event}
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        
        # Human-readable formatted log
        evt_type = event.get("event", "info")
        msg = f"[{now_iso}] [{evt_type.upper()}] "
        if "step" in event:
            msg += f"Step: {event['step']} | "
        if "label" in event:
            msg += f"Label: {event['label']} | "
        if "selector" in event:
            msg += f"Selector: {event['selector']} | "
        if "value" in event:
            msg += f"Value: {event['value']} | "
        if "reason" in event:
            msg += f"Reason: {event['reason']} | "
        if "error" in event:
            msg += f"Error: {event['error']} | "
        if "signals_count" in event:
            msg += f"Signals: {event['signals_count']} items | "
        with self.readable_log_path.open("a", encoding="utf-8") as f:
            f.write(msg.strip() + "\n")

    def wait(self, seconds: float | None = None) -> None:
        s = self.defaults.get("settle_seconds", 2.0) if seconds is None else seconds
        time.sleep(s)

    def check_prohibited_action(self, text: str) -> bool:
        """Verify whether an action text contains high-risk payment/submission triggers."""
        for prohibited in self.prohibited_texts:
            if prohibited and prohibited in text:
                return True
        return False

    def is_login_or_risk_screen(self, xml: str | None = None) -> str | None:
        """Detect whether the current screen requires login or shows a risk/captcha verification."""
        visible = self.visible_texts(xml)
        for candidate in self.login_or_risk_texts:
            for text in visible:
                if candidate in text:
                    return candidate
        return None

    def is_sensitive_input_screen(self, xml: str | None = None) -> str | None:
        """Detect whether the screen prompts for bank card number, CVV, or password."""
        visible = self.visible_texts(xml)
        for candidate in self.sensitive_input_prompts:
            for text in visible:
                if candidate in text:
                    return candidate
        return None

    def payment_visible(self, xml: str | None = None) -> str | None:
        """Check if cashier, bank card selector, or payment method page is visible."""
        texts = self.visible_texts(xml)
        for candidate in self.payment_stop_texts:
            for text in texts:
                if candidate in text:
                    return candidate
        return None

    def snapshot(self, label: str) -> tuple[Path, Path, str]:
        """Capture screenshot and UI hierarchy with metadata logging."""
        self.snapshot_index += 1
        stem = f"{self.snapshot_index:02d}_{self._safe_name(label)}"
        image_path = self.app_dir / f"{stem}.png"
        xml_path = self.app_dir / f"{stem}.xml"

        # Capture screenshot
        self.d.screenshot(str(image_path))
        # Dump UI hierarchy
        xml = self.d.dump_hierarchy()
        xml_path.write_text(xml, encoding="utf-8")

        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()[:16]
        file_size_kb = round(image_path.stat().st_size / 1024, 1)

        self.log({
            "event": "snapshot",
            "index": self.snapshot_index,
            "label": label,
            "screenshot": image_path.name,
            "xml": xml_path.name,
            "sha256": digest,
            "file_size_kb": file_size_kb
        })

        signals = self.extract_marketing_signals(xml, label)
        if signals:
            self.log({
                "event": "marketing_signals",
                "label": label,
                "signals_count": len(signals),
                "signals": signals
            })
        return image_path, xml_path, xml

    def visible_texts(self, xml: str | None = None) -> list[str]:
        xml = xml if xml is not None else self.d.dump_hierarchy()
        values = re.findall(r'\b(?:text|content-desc)="([^"<>]*)"', xml)
        return sorted({v.strip() for v in values if v.strip()})

    def extract_marketing_signals(self, xml: str | None = None, step_label: str = "") -> list[dict[str, Any]]:
        """Extract structured marketing signals categorized by taxonomy."""
        xml = xml if xml is not None else self.d.dump_hierarchy()
        found: list[dict[str, Any]] = []
        seen_texts: set[str] = set()

        try:
            root = ET.fromstring(xml)
            for elem in root.iter():
                text = (elem.attrib.get("text") or "").strip()
                desc = (elem.attrib.get("content-desc") or "").strip()
                content = text or desc
                if not content or content in seen_texts:
                    continue

                for category, keywords in self.marketing_taxonomy.items():
                    matched_kw = None
                    for kw in keywords:
                        if kw in content:
                            matched_kw = kw
                            break
                    if matched_kw:
                        seen_texts.add(content)
                        # Extract price / discount numbers if present
                        amounts = re.findall(r'[¥￥]?\d+(?:\.\d+)?', content)
                        found.append({
                            "category": category,
                            "keyword": matched_kw,
                            "content": content,
                            "amounts": amounts,
                            "resource_id": elem.attrib.get("resource-id", ""),
                            "bounds": elem.attrib.get("bounds", ""),
                            "step": step_label
                        })
                        break
        except Exception as exc:
            # Fallback to regex text search if XML parsing encounters unexpected tags
            for category, keywords in self.marketing_taxonomy.items():
                for kw in keywords:
                    pattern = rf'\b(?:text|content-desc)="([^"<>]*?{re.escape(kw)}[^"<>]*?)"'
                    for match in re.findall(pattern, xml):
                        clean = match.strip()
                        if clean and clean not in seen_texts:
                            seen_texts.add(clean)
                            found.append({
                                "category": category,
                                "keyword": kw,
                                "content": clean,
                                "amounts": re.findall(r'[¥￥]?\d+(?:\.\d+)?', clean),
                                "resource_id": "",
                                "bounds": "",
                                "step": step_label
                            })
        return found

    def tap_text_any(self, texts: Iterable[str], timeout: float | None = None, purpose: str = "") -> str | None:
        """Safely tap an element by candidate texts. Enforces safety checks on all candidates!"""
        deadline = time.time() + (timeout or self.defaults.get("action_timeout_seconds", 8.0))
        candidates = [str(x).strip() for x in texts if str(x).strip()]

        # SAFETY CHECK 1: Ensure candidates don't request payment execution
        for candidate in candidates:
            if self.check_prohibited_action(candidate):
                self.log({
                    "event": "safety_intercept",
                    "reason": f"Refused to tap prohibited text '{candidate}'",
                    "purpose": purpose
                })
                raise SafetyHaltException(f"安全拦截：严禁点击支付提交文案 '{candidate}'")

        while time.time() < deadline:
            for text in candidates:
                obj = self.d(text=text)
                if obj.exists:
                    # Double check actual node text before clicking
                    info = obj.info or {}
                    actual_text = info.get("text", "") or text
                    if self.check_prohibited_action(actual_text):
                        raise SafetyHaltException(f"安全拦截：目标节点包含禁止操作文案 '{actual_text}'")
                    bounds = info.get("bounds", {})
                    obj.click()
                    self.log({"event": "tap", "selector": f"text={text}", "bounds": bounds, "purpose": purpose})
                    return text

                desc = self.d(description=text)
                if desc.exists:
                    info = desc.info or {}
                    actual_desc = info.get("contentDescription", "") or text
                    if self.check_prohibited_action(actual_desc):
                        raise SafetyHaltException(f"安全拦截：目标节点描述包含禁止操作文案 '{actual_desc}'")
                    bounds = info.get("bounds", {})
                    desc.click()
                    self.log({"event": "tap", "selector": f"description={text}", "bounds": bounds, "purpose": purpose})
                    return text
            time.sleep(0.3)
        return None

    def dismiss_popups(self, max_rounds: int = 3) -> int:
        """Dismiss common startup and system popups (privacy, permissions, updates, floating coupons)."""
        dismissed_count = 0
        for _ in range(max_rounds):
            tapped = False
            for text in self.dismiss_popup_texts:
                obj = self.d(text=text)
                if obj.exists:
                    obj.click()
                    self.log({"event": "dismiss_popup", "text": text})
                    dismissed_count += 1
                    tapped = True
                    time.sleep(0.5)
                    break
                desc = self.d(description=text)
                if desc.exists:
                    desc.click()
                    self.log({"event": "dismiss_popup", "description": text})
                    dismissed_count += 1
                    tapped = True
                    time.sleep(0.5)
                    break
            if not tapped:
                break
        return dismissed_count

    def input_search(self) -> bool:
        """Robust search input handling across Taobao, JD, Douyin, Meituan."""
        # 1. Check if an active EditText is already displayed
        fields = self.d(className="android.widget.EditText")
        if fields.exists:
            fields[0].click()
            fields[0].set_text(self.query)
            self.log({"event": "input", "value": self.query, "method": "EditText_direct"})
            return True

        # 2. Try resource-ids configured for this app
        for res_id in self.app.get("search_entry_ids", []):
            obj = self.d(resourceId=res_id)
            if obj.exists:
                obj.click()
                self.wait(1.0)
                edit = self.d(className="android.widget.EditText")
                if edit.exists:
                    edit[0].set_text(self.query)
                    self.log({"event": "input", "value": self.query, "method": f"id:{res_id}"})
                    return True

        # 3. Try candidate search entry texts
        entry = self.tap_text_any(self.app.get("search_entry_texts", []), timeout=3.0, purpose="open_search")
        if entry:
            self.wait(1.0)
            fields = self.d(className="android.widget.EditText")
            if fields.exists:
                fields[0].set_text(self.query)
                self.log({"event": "input", "value": self.query, "method": f"after_entry:{entry}"})
                return True

        # Some apps expose only a non-clickable search container in the UI
        # tree. Use a configured point inside the visible search bar.
        for point in self.app.get("search_tap_points", []):
            try:
                x, y = int(point[0]), int(point[1])
                self.d.click(x, y)
                self.wait(0.8)
                fields = self.d(className="android.widget.EditText")
                if fields.exists:
                    fields[0].set_text(self.query)
                    self.log({"event": "input", "value": self.query, "method": f"coordinate:{x},{y}"})
                    return True
            except Exception as exc:
                self.log({"event": "search_coordinate_fallback_error", "point": point, "error": str(exc)})
        return False

    def tap_search_submit(self) -> bool:
        """Submit the search keyword via search button or keyboard enter."""
        # First try pressing enter key
        try:
            self.d.press("enter")
            self.log({"event": "search_submit", "method": "key_enter"})
            return True
        except Exception:
            pass

        # Try tapping search submit buttons
        tapped = self.tap_text_any(["搜索", "确认", "提交", "完成", "搜全网"], timeout=2.0, purpose="submit_search")
        if tapped:
            return True
        return False

    def tap_first_product(self) -> bool:
        """Tap the first valid product item, explicitly filtering out ads and live stream tags."""
        # Special handling for Douyin: switch to '商品' tab if available
        for tab_text in self.app.get("search_tab_texts", []):
            tab = self.d(text=tab_text)
            if tab.exists:
                tab.click()
                self.log({"event": "tap", "selector": f"tab={tab_text}", "purpose": "switch_to_product_tab"})
                self.wait(1.5)
                self.snapshot("search_products_tab")
                break

        # Some shopping feeds render product cards as images/canvas and expose
        # no useful clickable text. Keep a per-app coordinate fallback after
        # the semantic selectors above have had a chance to work.
        for point in self.app.get("product_tap_points", []):
            try:
                x, y = int(point[0]), int(point[1])
                self.d.click(x, y)
                self.log({"event": "tap", "selector": f"coordinate=({x},{y})", "purpose": "first_product"})
                return True
            except Exception as exc:
                self.log({"event": "product_coordinate_fallback_error", "point": point, "error": str(exc)})

        # Avoid ads ("广告", "推广") and live stream tags ("直播中")
        # Find clickable nodes with price symbol
        try:
            nodes = self.d.xpath('//*[@clickable="true"]')
            for node in nodes.all():
                info = node.info or {}
                text = info.get("text", "") or ""
                desc = info.get("contentDescription", "") or ""
                combined = f"{text} {desc}"

                # Skip non-products or live streams
                if any(bad in combined for bad in ["直播中", "广告", "推广", "综合", "筛选", "店铺"]):
                    continue

                bounds = info.get("bounds", {})
                top = bounds.get("top", 0)
                bottom = bounds.get("bottom", 0)
                height = bottom - top

                # Must be in the central content area (below search header, reasonable height)
                if top > 250 and height > 120:
                    if "¥" in combined or "￥" in combined or "元" in combined or "销量" in combined:
                        node.click()
                        self.log({"event": "tap", "selector": "product_card_with_price", "purpose": "first_product", "bounds": bounds})
                        return True
        except Exception as exc:
            self.log({"event": "product_xpath_heuristic_error", "error": str(exc)})

        # Fallback to textContains price symbol
        for sym in ["¥", "￥", "到手价", "件", "销量"]:
            obj = self.d(textContains=sym)
            if obj.exists:
                for item in obj:
                    info = item.info or {}
                    top = info.get("bounds", {}).get("top", 0)
                    if top > 250:
                        item.click()
                        self.log({"event": "tap", "selector": f"textContains={sym}", "purpose": "first_product"})
                        return True
        return False

    def handle_sku_modal(self) -> bool:
        """Handle specification/SKU selection sheet if it appears after clicking buy."""
        time.sleep(1.0)
        xml = self.d.dump_hierarchy()
        # Look for SKU confirmation buttons
        for sku_btn_text in self.app.get("sku_confirm_texts", ["确定", "立即购买", "下一步"]):
            btn = self.d(text=sku_btn_text)
            if btn.exists:
                info = btn.info or {}
                # Ensure it's located in the bottom sheet area
                if info.get("bounds", {}).get("top", 0) > 1000:
                    self.snapshot("sku_modal_detected")
                    btn.click()
                    self.log({"event": "tap", "selector": f"sku_confirm={sku_btn_text}", "purpose": "confirm_sku"})
                    return True
        return False

    def tap_buy_action(self) -> str | None:
        """Tap a semantic buy label or a configured canvas-backed fallback."""
        tapped = self.tap_text_any(self.app.get("buy_texts", ["立即购买", "购买"]), timeout=4.0, purpose="buy_action")
        if tapped:
            return tapped
        for point in self.app.get("buy_tap_points", []):
            try:
                x, y = int(point[0]), int(point[1])
                self.d.click(x, y)
                self.log({"event": "tap", "selector": f"coordinate=({x},{y})", "purpose": "buy_action"})
                return f"coordinate=({x},{y})"
            except Exception as exc:
                self.log({"event": "buy_coordinate_fallback_error", "point": point, "error": str(exc)})
        return None

    def run(self) -> AppResult:
        result = AppResult(self.app["key"], self.app["display_name"], self.app["package"])
        start_ts = time.time()
        result.start_time = datetime.now().astimezone().isoformat(timespec="seconds")

        try:
            # 1. Launch App
            # A previous run may leave the App on a detail/checkout page. A
            # force-stop gives scheduled runs a deterministic fresh task stack
            # without clearing account data or login state.
            try:
                self.d.app_stop(self.app["package"])
                self.log({"event": "reset_app_state", "method": "app_stop"})
            except Exception as exc:
                self.log({"event": "reset_app_state_warning", "error": str(exc)})
            self.d.app_start(self.app["package"])
            self.log({"event": "launch", "package": self.app["package"]})
            self.wait(3.0)
            # Some apps (notably Douyin) restore the last private conversation
            # or deep link instead of their home feed after a force-stop.
            # Configurable back-and-relaunch returns the run to a stable home.
            for _ in range(int(self.app.get("reset_back_presses", 0))):
                self.d.press("back")
                self.wait(0.8)
                self.d.app_start(self.app["package"])
                self.wait(2.0)
            _, _, xml = self.snapshot("01_launch")

            # Check if launch screen already shows cashier
            payment = self.payment_visible(xml)
            if payment:
                result.status = "stopped_payment_page"
                result.stop_reason = f"支付方式已显示 (检测到: {payment})，立即停止"
                return result

            # 2. Clean Popups
            dismissed = self.dismiss_popups()
            if dismissed > 0:
                self.wait(1.0)
                self.snapshot("02_after_dismiss_popups")

            # Check for login intercept
            login_intercept = self.is_login_or_risk_screen()
            if login_intercept:
                result.status = "stopped_login_or_risk_protection"
                result.stop_reason = f"检测到登录或风控保护 ({login_intercept})，按安全规则立即停止，不绕过"
                self.snapshot("login_or_risk_stopped")
                return result

            # 3. Search Product
            if not self.input_search():
                raise RuntimeError("找不到搜索框；请调整 config/apps.yaml 中的 search_entry_texts 或 search_entry_ids")
            self.wait(0.8)

            if not self.tap_search_submit():
                raise RuntimeError("找不到搜索提交按钮且回车未触发提交")
            self.wait(2.5)
            self.snapshot("03_search_results")

            # 4. Tap First Product
            if not self.tap_first_product():
                raise RuntimeError("未能从搜索结果中定位到商品卡片；请检查网络或调整 selector 规则")
            self.wait(2.5)
            _, _, detail_xml = self.snapshot("04_product_detail")

            # 5. Extract Detail Marketing and try expanding coupon sheet
            tapped_coupon = None
            if not self.app.get("skip_coupon_expand", False):
                tapped_coupon = self.tap_text_any(["领券", "优惠券", "满减", "立减", "优惠", "红包"], timeout=1.5, purpose="expand_coupons")
            if tapped_coupon:
                self.wait(1.2)
                self.snapshot("05_coupons_drawer")
                # Dismiss coupon sheet: tap outside or close button or back
                if not self.tap_text_any(["完成", "关闭", "我知道了"], timeout=1.0, purpose="close_coupons"):
                    self.d.press("back")
                self.wait(1.0)

            # Check payment visible before proceeding to purchase
            if self.payment_visible():
                result.status = "stopped_payment_page"
                result.stop_reason = "进入结算流程前已检测到支付方式/收银台页面，按规则立即停止"
                return result

            # 6. Click Buy Button
            buy_tapped = self.tap_buy_action()
            if not buy_tapped:
                raise RuntimeError("未找到购买入口按钮；请检查详情页文案并更新 config/apps.yaml 的 buy_texts")
            self.wait(1.5)
            self.snapshot("06_after_buy_action")

            # 7. Check SKU Modal
            sku_handled = self.handle_sku_modal()
            if sku_handled:
                self.wait(2.0)
                self.snapshot("07_after_sku_confirm")

            # 8. Check for Order Confirmation / Cashier / Payment methods
            # Check sensitive input fields
            sensitive = self.is_sensitive_input_screen()
            if sensitive:
                result.status = "stopped_sensitive_input_detected"
                result.stop_reason = f"检测到敏感输入项 ({sensitive})，按安全规则立即停止，不输入任何信息"
                return result

            # Check if payment page is already visible
            _, _, cur_xml = self.snapshot("08_checkout_or_cashier")
            payment = self.payment_visible(cur_xml)
            if payment:
                result.status = "stopped_payment_page"
                result.stop_reason = f"检测到支付方式/收银台页面 (匹配项: {payment})，立即停止，绝不提交订单或支付"
                return result

            # Try to tap payment method row on order confirmation page if not already in cashier
            for p_entry in self.app.get("payment_entry_texts", ["支付方式", "付款方式", "银行卡"]):
                obj = self.d(text=p_entry)
                if obj.exists:
                    info = obj.info or {}
                    # Do NOT tap if it looks like submit
                    if not self.check_prohibited_action(info.get("text", "")):
                        obj.click()
                        self.log({"event": "tap", "selector": f"payment_entry={p_entry}", "purpose": "inspect_payment_options"})
                        self.wait(1.5)
                        break

            _, _, final_xml = self.snapshot("09_final_payment_checkpoint")
            payment = self.payment_visible(final_xml)
            if payment:
                result.status = "stopped_payment_page"
                result.stop_reason = f"成功到达银行卡/支付方式页面 (匹配项: {payment})，根据安全策略立即停止，绝不执行付款"
            else:
                result.status = "reached_checkout_safely"
                result.stop_reason = "已安全进入确认订单结算页并采集优惠营销信息；已严密阻止点击提交订单，巡检安全结束"

            return result

        except SafetyHaltException as she:
            result.status = "stopped_safety_boundary"
            result.stop_reason = str(she)
            self.log({"event": "safety_halt", "reason": str(she)})
            return result
        except Exception as exc:
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            self.log({
                "event": "error",
                "error": result.error,
                "traceback": traceback.format_exc(limit=4)
            })
            return result
        finally:
            end_ts = time.time()
            result.end_time = datetime.now().astimezone().isoformat(timespec="seconds")
            result.duration_seconds = round(end_ts - start_ts, 1)
            # Persist the authoritative outcome so report.py does not have to
            # infer status from incidental strings such as a payment-row tap.
            self.log({
                "event": "result",
                "status": result.status,
                "stop_reason": result.stop_reason,
                "error": result.error,
                "duration_seconds": result.duration_seconds,
            })


def collect_device_and_host_env(device: Any, serial: str | None = None) -> dict[str, Any]:
    """Collect comprehensive host and Xiaomi 14 Ultra device environment information."""
    env: dict[str, Any] = {
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": sys.version.split()[0],
            "role": "Host Controller (Mac/PC 运行端)"
        },
        "device": {
            "role": "Device Under Test (DUT 被测手机端)",
            "connected_serial": serial or "default",
            "brand": "Xiaomi",
            "model": "Xiaomi 14 Ultra",
            "raw_info": {}
        }
    }

    try:
        raw_info = device.info
        env["device"]["raw_info"] = raw_info
        env["device"]["screen_size"] = f"{raw_info.get('displayWidth', 0)}x{raw_info.get('displayHeight', 0)}"
        env["device"]["screen_density"] = raw_info.get("displayDensity", 0)
        env["device"]["sdk_int"] = raw_info.get("sdkInt", 0)
    except Exception as exc:
        env["device"]["raw_info_error"] = str(exc)

    # Shell queries for Xiaomi 14 Ultra / HyperOS specific properties
    shell_queries = {
        "model": "getprop ro.product.model",
        "brand": "getprop ro.product.brand",
        "marketname": "getprop ro.product.marketname",
        "hyperos_version": "getprop ro.miui.ui.version.name",
        "hyperos_incremental": "getprop ro.build.version.incremental",
        "android_version": "getprop ro.build.version.release",
        "security_patch": "getprop ro.build.version.security_patch",
        "battery_summary": "dumpsys battery | grep level",
        "wifi_status": "dumpsys wifi | grep 'Wi-Fi is' | head -n 1"
    }

    for key, cmd in shell_queries.items():
        try:
            val = device.shell(cmd).output.strip()
            env["device"][key] = val
        except Exception:
            env["device"][key] = "unknown"

    # Query installed versions of the four inspected apps
    app_pkgs = {
        "taobao": "com.taobao.taobao",
        "jd": "com.jingdong.app.mall",
        "douyin": "com.ss.android.ugc.aweme",
        "meituan": "com.sankuai.meituan"
    }
    env["installed_apps"] = {}
    for app_name, pkg in app_pkgs.items():
        try:
            out = device.shell(f"dumpsys package {pkg} | grep versionName | head -n 1").output.strip()
            version = out.split("=")[-1] if "=" in out else out
            env["installed_apps"][app_name] = {"package": pkg, "version": version or "installed"}
        except Exception:
            env["installed_apps"][app_name] = {"package": pkg, "version": "unknown"}

    return env


def load_config(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if yaml is None:
        raise RuntimeError("缺少 PyYAML 依赖，请在宿主机激活虚拟环境并执行: pip install -r requirements.txt")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    apps = []
    for key, value in data.get("apps", {}).items():
        apps.append({"key": key, **value})
    return data.get("defaults", {}), apps


def main() -> int:
    parser = argparse.ArgumentParser(description="安全竞品 App 路径巡检程序 (Xiaomi 14 Ultra 真机)")
    parser.add_argument("--query", default="洗衣液", help="四个 App 统一搜索的商品关键词")
    parser.add_argument("--config", type=Path, default=ROOT / "config/apps.yaml", help="配置文件路径")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs", help="输出根目录")
    parser.add_argument("--baseline", type=Path, default=None, help="基线比对目录 (上一次运行的时间戳目录)")
    parser.add_argument("--serial", default=None, help="ADB 设备序列号 (单设备时可省略)")
    parser.add_argument("--app", default=None, help="指定巡检单个 App (如 taobao, jd, douyin, meituan)")
    args = parser.parse_args()

    if u2 is None or yaml is None:
        missing = []
        if u2 is None:
            missing.append("uiautomator2")
        if yaml is None:
            missing.append("PyYAML")
        print(f"环境提示：缺少必要依赖 [{', '.join(missing)}]，请在宿主机执行：")
        print("  pip install -r requirements.txt")
        return 1

    defaults, apps = load_config(args.config)
    if args.app:
        apps = [a for a in apps if a["key"] == args.app]
        if not apps:
            print(f"错误：配置中未找到 App '{args.app}'")
            return 1

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.output / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("【安全竞品 App 路径巡检程序】")
    print(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"搜索商品: {args.query}")
    print(f"输出目录: {out_dir}")
    print("安全底线: 到达支付方式/收银台页面立即停止，绝不提交订单或发起真实支付！")
    print("=" * 65)

    # Connect to device
    try:
        device = u2.connect(args.serial) if args.serial else u2.connect()
        print(f"成功连接 Android 设备 (serial: {args.serial or 'default'})")
    except Exception as exc:
        print(f"\n无法连接 Android 设备：{exc}")
        print("排查建议：")
        print("1. 确保手机已通过 USB 数据线连接 Mac/PC 并在手机上点击'允许 USB 调试'")
        print("2. 小米手机 (HyperOS) 必须在'开发者选项'中额外打开【USB 调试（安全设置）】以允许模拟点击")
        print("3. 在终端执行 `adb devices` 确认设备处于 device 状态")
        return 2

    # Collect and save environment info
    try:
        env_info = collect_device_and_host_env(device, args.serial)
        (out_dir / "device_env.json").write_text(json.dumps(env_info, ensure_ascii=False, indent=2), encoding="utf-8")
        dev = env_info.get("device", {})
        print(f"被测设备: {dev.get('brand', 'Xiaomi')} {dev.get('model', '14 Ultra')} | OS: {dev.get('hyperos_version', 'HyperOS')} (Android {dev.get('android_version', '14')}) | 分辨率: {dev.get('screen_size', '1440x3200')}")
    except Exception as exc:
        print(f"采集设备环境信息警告: {exc}")

    results = []
    for app in apps:
        print(f"\n▶ 正在巡检: [{app['display_name']}] (包名: {app['package']})")
        inspector = Inspector(device, app, out_dir, args.query, defaults)
        result = inspector.run()
        results.append(result)
        status_color = "✓" if "stopped" in result.status or "reached" in result.status else "✗"
        print(f"  {status_color} 状态: {result.status} | 耗时: {result.duration_seconds}s | 停止原因: {result.stop_reason or result.error}")

    # Generate Report using report.py module
    try:
        import report
        report_file = report.generate(out_dir, args.baseline, query=args.query)
        print(f"\n✓ 巡检报告已生成: {report_file}")
    except Exception as exc:
        print(f"生成报告异常: {exc}")
        traceback.print_exc()

    return 0 if any(r.status != "error" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
