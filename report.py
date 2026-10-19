#!/usr/bin/env python3
"""竞品 App 路径巡检报告生成器 (Report Generator & Baseline Diff Engine)

可独立运行，无需连接真机即可对已完成的巡检目录重新生成富文本 Markdown 报告和 JSON 数据。
纯标准库实现（内建 xml.etree.ElementTree、json、hashlib 等）；
若环境中存在 Pillow，则自动启用 dHash 感知哈希进行高精度视觉差异比对。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Optional Pillow for perceptual image diff (dHash)
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

# Marketing Taxonomy for classification
MARKETING_TAXONOMY = {
    "coupons": {
        "title": "优惠券/立减",
        "keywords": ["优惠券", "领券", "满减", "立减", "店铺券", "平台券", "品类券", "折扣券", "减¥", "省¥", "领券立减"]
    },
    "red_packets": {
        "title": "红包/津贴",
        "keywords": ["红包", "店铺红包", "平台红包", "膨胀红包", "天降红包", "购物津贴", "消费券", "福利金"]
    },
    "memberships": {
        "title": "会员权益",
        "keywords": ["88VIP", "PLUS会员", "品牌会员", "粉丝专享", "试用会员", "省钱卡", "神会员", "尊享会员", "会员专享", "会员立省"]
    },
    "activities_subsidies": {
        "title": "大促/补贴活动",
        "keywords": ["百亿补贴", "官方立减", "国家补贴", "国补", "以旧换新", "限时秒杀", "限时抢购", "跨店满减", "预售", "拼团", "惊喜券", "超级补贴"]
    },
    "pricing_prompts": {
        "title": "价格提示",
        "keywords": ["到手价", "券后价", "预估到手", "折后价", "补贴价", "促销价", "专享价", "折扣", "免单"]
    },
    "payment_bank_promos": {
        "title": "银行卡/支付优惠",
        "keywords": ["银行卡立减", "信用卡立减", "借记卡立减", "分期免息", "分期优惠", "支付立减", "随机立减", "白条立减", "花呗分期", "笔笔减", "减免"]
    }
}


def compute_dhash(img_path: Path) -> str | None:
    """Compute difference hash (dHash) of an image using Pillow."""
    if not HAS_PILLOW or not img_path.exists():
        return None
    try:
        with Image.open(img_path) as img:
            resized = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(resized.getdata())
            diff = []
            for row in range(8):
                for col in range(8):
                    diff.append(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
            val = 0
            for b in diff:
                val = (val << 1) | (1 if b else 0)
            return f"{val:016x}"
    except Exception:
        return None


def calculate_similarity(h1: str, h2: str) -> float:
    """Calculate similarity percentage between two dHash strings."""
    try:
        dist = bin(int(h1, 16) ^ int(h2, 16)).count("1")
        return round((64 - dist) / 64.0 * 100, 1)
    except Exception:
        return 0.0


def extract_texts_from_xml(xml_path: Path) -> set[str]:
    """Extract all text and content-desc strings from a UI hierarchy XML file."""
    if not xml_path.exists():
        return set()
    raw = xml_path.read_text(encoding="utf-8", errors="ignore")
    values = re.findall(r'\b(?:text|content-desc)="([^"<>]*)"', raw)
    return {v.strip() for v in values if v.strip()}


def extract_marketing_details(xml_path: Path) -> list[dict[str, Any]]:
    """Deep-parse UI tree to extract marketing and promotional nodes with adjacent context."""
    if not xml_path.exists():
        return []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    try:
        raw = xml_path.read_text(encoding="utf-8", errors="ignore")
        root = ET.fromstring(raw)
        for elem in root.iter():
            text = (elem.attrib.get("text") or "").strip()
            desc = (elem.attrib.get("content-desc") or "").strip()
            content = text or desc
            if not content or content in seen:
                continue

            for cat_key, cat_meta in MARKETING_TAXONOMY.items():
                matched_kw = None
                for kw in cat_meta["keywords"]:
                    if kw in content:
                        matched_kw = kw
                        break
                if matched_kw:
                    seen.add(content)
                    amounts = re.findall(r'[¥￥]?\d+(?:\.\d+)?', content)
                    results.append({
                        "category_key": cat_key,
                        "category_title": cat_meta["title"],
                        "keyword": matched_kw,
                        "content": content,
                        "amounts": amounts,
                        "resource_id": elem.attrib.get("resource-id", ""),
                        "bounds": elem.attrib.get("bounds", "")
                    })
                    break
    except Exception:
        pass
    return results


def compare_with_baseline(current_app_dir: Path, baseline_app_dir: Path | None) -> dict[str, Any]:
    """Compare screenshots, UI texts, and marketing signals between current run and baseline."""
    diff_result: dict[str, Any] = {
        "has_baseline": bool(baseline_app_dir and baseline_app_dir.exists()),
        "visual_diffs": [],
        "text_diffs": [],
        "marketing_diffs": []
    }
    if not diff_result["has_baseline"] or not baseline_app_dir:
        return diff_result

    # 1. Visual Comparison (dHash + sha256)
    old_pngs = {p.stem.split("_", 1)[-1]: p for p in baseline_app_dir.glob("*.png")}
    for img in sorted(current_app_dir.glob("*.png")):
        step_key = img.stem.split("_", 1)[-1]
        if step_key in old_pngs:
            old_img = old_pngs[step_key]
            curr_sha = hashlib.sha256(img.read_bytes()).hexdigest()[:16]
            old_sha = hashlib.sha256(old_img.read_bytes()).hexdigest()[:16]
            
            sim = None
            if HAS_PILLOW:
                h1 = compute_dhash(img)
                h2 = compute_dhash(old_img)
                if h1 and h2:
                    sim = calculate_similarity(h1, h2)

            if curr_sha != old_sha:
                diff_result["visual_diffs"].append({
                    "step": step_key,
                    "similarity": sim,
                    "curr_sha": curr_sha,
                    "old_sha": old_sha,
                    "changed": True
                })

    # 2. Textual Comparison
    old_xmls = {p.stem.split("_", 1)[-1]: p for p in baseline_app_dir.glob("*.xml")}
    for xml_file in sorted(current_app_dir.glob("*.xml")):
        step_key = xml_file.stem.split("_", 1)[-1]
        if step_key in old_xmls:
            curr_texts = extract_texts_from_xml(xml_file)
            old_texts = extract_texts_from_xml(old_xmls[step_key])

            added = sorted(curr_texts - old_texts)
            removed = sorted(old_texts - curr_texts)
            if added or removed:
                # Filter out pure timestamps or battery percentages
                filtered_added = [t for t in added if not re.match(r'^\d{1,2}:\d{2}$', t)]
                filtered_removed = [t for t in removed if not re.match(r'^\d{1,2}:\d{2}$', t)]
                diff_result["text_diffs"].append({
                    "step": step_key,
                    "added_count": len(filtered_added),
                    "removed_count": len(filtered_removed),
                    "added": filtered_added[:15],
                    "removed": filtered_removed[:15]
                })

            # 3. Marketing Differences
            curr_mkt = {m["content"] for m in extract_marketing_details(xml_file)}
            old_mkt = {m["content"] for m in extract_marketing_details(old_xmls[step_key])}
            mkt_new = sorted(curr_mkt - old_mkt)
            mkt_lost = sorted(old_mkt - curr_mkt)
            if mkt_new or mkt_lost:
                diff_result["marketing_diffs"].append({
                    "step": step_key,
                    "new_promos": mkt_new[:10],
                    "lost_promos": mkt_lost[:10]
                })

    return diff_result


def parse_app_status(app_dir: Path) -> tuple[str, str, float]:
    """Parse status, stop reason, and duration from actions.jsonl or log files."""
    status = "completed"
    stop_reason = "正常完成"
    duration = 0.0

    action_log = app_dir / "actions.jsonl"
    if action_log.exists():
        timestamps = []
        for line in action_log.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                if "time" in evt:
                    timestamps.append(evt["time"])
                if evt.get("event") == "result":
                    # runner.py writes this authoritative terminal event in
                    # finally; prefer it over heuristic string matching.
                    status = evt.get("status", status)
                    stop_reason = evt.get("stop_reason") or evt.get("error") or stop_reason
                elif evt.get("event") == "error":
                    status = "error"
                    stop_reason = evt.get("error", "未知执行错误")
                elif evt.get("event") == "safety_intercept" or evt.get("event") == "safety_halt":
                    status = "stopped_safety_boundary"
                    stop_reason = evt.get("reason", "触碰安全底线拦截")
                elif "stopped_payment_page" in line or "支付方式" in line or "收银台" in line:
                    if status != "error":
                        status = "stopped_payment_page"
                        stop_reason = "到达支付方式/收银台页面，安全停止"
                elif "stopped_login" in line:
                    status = "stopped_login_protection"
                    stop_reason = "检测到登录/风控保护，安全停止"
            except json.JSONDecodeError:
                continue

        if len(timestamps) >= 2:
            try:
                t0 = datetime.fromisoformat(timestamps[0])
                t1 = datetime.fromisoformat(timestamps[-1])
                duration = round((t1 - t0).total_seconds(), 1)
            except Exception:
                pass
    return status, stop_reason, duration


def generate(run_dir: Path, baseline_dir: Path | None = None, query: str = "洗衣液") -> Path:
    """Generate Markdown and JSON reports for a completed run directory."""
    device_json = run_dir / "device_env.json"
    env_data: dict[str, Any] = {}
    if device_json.exists():
        try:
            env_data = json.loads(device_json.read_text(encoding="utf-8"))
        except Exception:
            pass

    dev = env_data.get("device", {})
    host = env_data.get("host", {})

    lines: list[str] = [
        "# 竞品 App 路径巡检报告 (Xiaomi 14 Ultra 真机)",
        "",
        f"- **生成时间**：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- **巡检商品关键词**：`{query}`",
        f"- **运行目录**：`{run_dir.name}`",
        f"- **基线对比**：`{baseline_dir.name if baseline_dir else '无 (首次运行)'}`",
        "",
        "> [!IMPORTANT]",
        "> **安全合规声明**：本巡检严格限定在公开页面浏览与营销信息抓取。到达银行卡/支付方式选择页或收银台时**立即主动停止**。全程**绝不输入**卡号、CVV、短信验证码、支付密码，**绝不点击**提交订单或确认支付按钮，**绝不绕过**登录与风控验证。",
        "",
        "## 一、运行环境与被测设备",
        "",
        "| 项目 | 属性配置 | 说明 |",
        "| :--- | :--- | :--- |",
        f"| **宿主控制机 (Host)** | `{host.get('system', 'Mac/PC')} ({host.get('release', '')})` | 运行 Python / runner.py |",
        f"| **被测真机 (DUT)** | `Xiaomi 14 Ultra` (`{dev.get('model', '24030PN60C')}`) | USB 调试受控手机 |",
        f"| **操作系统** | `Xiaomi HyperOS` (`{dev.get('hyperos_version', 'OS1.0')}`) / Android `{dev.get('android_version', '14')}` | 小米澎湃 OS |",
        f"| **屏幕分辨率/DPI** | `{dev.get('screen_size', '1440x3200')}` (DPI: `{dev.get('screen_density', 560)}`) | 超高分辨率真机屏幕 |",
        f"| **设备序列号 (Serial)** | `{dev.get('connected_serial', 'USB连接')}` | ADB 连接通道 |",
        f"| **感知比对引擎** | `{'Pillow (dHash 感知哈希已启用)' if HAS_PILLOW else 'SHA256 (Pillow未安装)'}` | 视觉差异检测 |",
        "",
        "## 二、巡检概览汇总",
        "",
        "| 目标 App | 巡检状态 | 截图 | UI树 | 发现营销点 | 耗时 | 停止原因 |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :--- |"
    ]

    app_dirs = sorted([p for p in run_dir.iterdir() if p.is_dir()])
    summary_data: list[dict[str, Any]] = []

    for app_dir in app_dirs:
        status, stop_reason, duration = parse_app_status(app_dir)
        png_count = len(list(app_dir.glob("*.png")))
        xml_count = len(list(app_dir.glob("*.xml")))

        # Marketing count
        mkt_count = 0
        for xml_file in app_dir.glob("*.xml"):
            mkt_count += len(extract_marketing_details(xml_file))

        status_badge = f"**{status}**"
        if "stopped_payment_page" in status:
            status_badge = "🟢 **已安全停止(支付页)**"
        elif "reached" in status:
            status_badge = "🟢 **已安全到达(结算页)**"
        elif "error" in status:
            status_badge = "🔴 **异常中断**"
        elif "stopped_login" in status:
            status_badge = "🟡 **登录/风控停止**"

        lines.append(f"| **{app_dir.name}** | {status_badge} | {png_count} 张 | {xml_count} 份 | {mkt_count} 项 | {duration}s | {stop_reason} |")
        summary_data.append({
            "app": app_dir.name,
            "status": status,
            "stop_reason": stop_reason,
            "duration": duration,
            "screenshots": png_count,
            "xml_files": xml_count,
            "marketing_points": mkt_count
        })

    lines.append("")
    lines.append("## 三、各 App 详细巡检结果与营销提取")
    lines.append("")

    for app_dir in app_dirs:
        lines.append(f"### 📱 {app_dir.name}")
        lines.append("")
        status, stop_reason, _ = parse_app_status(app_dir)
        lines.append(f"- **最终状态**：`{status}`")
        lines.append(f"- **安全退出原因**：{stop_reason}")
        lines.append("")

        # Step artifacts table
        png_files = sorted(app_dir.glob("*.png"))
        if png_files:
            lines.append("#### 步骤截图与 UI 树文件")
            lines.append("")
            lines.append("| 序号 | 步骤标签 | 截图文件 | UI Hierarchy XML | 视觉 SHA256 |")
            lines.append("| :---: | :--- | :--- | :--- | :--- |")
            for idx, png in enumerate(png_files, 1):
                xml_companion = app_dir / f"{png.stem}.xml"
                sha = hashlib.sha256(png.read_bytes()).hexdigest()[:12]
                xml_link = f"[{xml_companion.name}](file://{xml_companion.resolve()})" if xml_companion.exists() else "无"
                png_link = f"[{png.name}](file://{png.resolve()})"
                lines.append(f"| {idx} | `{png.stem}` | {png_link} | {xml_link} | `{sha}` |")
            lines.append("")

        # Marketing highlights categorized
        app_marketing: dict[str, list[dict[str, Any]]] = {k: [] for k in MARKETING_TAXONOMY.keys()}
        seen_contents: set[str] = set()

        for xml_file in sorted(app_dir.glob("*.xml")):
            details = extract_marketing_details(xml_file)
            for item in details:
                text_key = f"{item['category_key']}_{item['content']}"
                if text_key not in seen_contents:
                    seen_contents.add(text_key)
                    app_marketing[item["category_key"]].append({
                        "step": xml_file.stem,
                        "content": item["content"],
                        "keyword": item["keyword"],
                        "amounts": item["amounts"]
                    })

        total_mkt = sum(len(v) for v in app_marketing.values())
        lines.append(f"#### 🏷️ 营销与价格优惠信息清单 (共提取 {total_mkt} 项)")
        lines.append("")
        if total_mkt == 0:
            lines.append("*(未在当前流程页面中检测到显著营销文案或已被弹窗遮挡)*")
        else:
            for cat_key, cat_meta in MARKETING_TAXONOMY.items():
                items = app_marketing.get(cat_key, [])
                if items:
                    lines.append(f"**【{cat_meta['title']}】** ({len(items)} 项)")
                    for it in items:
                        amt_str = f" [金额: {', '.join(it['amounts'])}]" if it['amounts'] else ""
                        lines.append(f"- 步骤 `{it['step']}`: **{it['content']}** (触发词: `{it['keyword']}`){amt_str}")
                    lines.append("")

        # Baseline comparison per app
        if baseline_dir:
            base_app_dir = baseline_dir / app_dir.name
            comp = compare_with_baseline(app_dir, base_app_dir)
            lines.append(f"#### 🔄 Baseline 差异对比 (对比基线: `{baseline_dir.name}`)")
            lines.append("")
            if not comp["has_baseline"]:
                lines.append("*(基线目录中无对应 App 历史数据，跳过比对)*")
            else:
                # Visual diffs
                if comp["visual_diffs"]:
                    lines.append("**视觉画面变化步骤**：")
                    for vd in comp["visual_diffs"]:
                        sim_str = f" (dHash 相似度: {vd['similarity']}%)" if vd['similarity'] is not None else ""
                        lines.append(f"- 步骤 `{vd['step']}`: 画面已变更{sim_str} [当前SHA: `{vd['curr_sha']}` vs 基线SHA: `{vd['old_sha']}`]")
                    lines.append("")
                else:
                    lines.append("- **视觉画面**：与基线完全一致，未发现视觉变动。")

                # Text diffs
                if comp["text_diffs"]:
                    lines.append("**UI 文本变动明细**：")
                    for td in comp["text_diffs"]:
                        add_text = f"新增: `{', '.join(td['added'][:6])}`" if td['added'] else ""
                        rem_text = f"消失: `{', '.join(td['removed'][:6])}`" if td['removed'] else ""
                        lines.append(f"- 步骤 `{td['step']}`: {add_text} {'| ' if add_text and rem_text else ''}{rem_text}")
                    lines.append("")
                else:
                    lines.append("- **UI 文本**：所有步骤文本与基线保持一致。")

                # Marketing diffs
                if comp["marketing_diffs"]:
                    lines.append("**营销活动与优惠变动**：")
                    for md in comp["marketing_diffs"]:
                        new_p = f"新增优惠: `{', '.join(md['new_promos'])}`" if md['new_promos'] else ""
                        lost_p = f"失效优惠: `{', '.join(md['lost_promos'])}`" if md['lost_promos'] else ""
                        lines.append(f"- 步骤 `{md['step']}`: {new_p} {'| ' if new_p and lost_p else ''}{lost_p}")
                    lines.append("")

        lines.append("---")
        lines.append("")

    # Footer
    lines.append("## 四、巡检执行建议与排查指南")
    lines.append("")
    lines.append("1. **A/B 实验与文案漂移**：若某 App 在某步骤报 `找不到购买按钮` 或 `找不到搜索框`，请查看对应步骤的 `.xml` 文件，在 `config/apps.yaml` 中将新出现的按钮文案添加至候选列表。")
    lines.append("2. **小米 HyperOS 权限**：若遇到点击无效，请进入手机【设置】->【更多设置】->【开发者选项】，确保【USB 调试（安全设置）】处于开启状态。")
    lines.append("3. **定期巡检与基线维护**：巡检通过后，可将当前目录命名为 `baseline`。下一次巡检加上 `--baseline baseline` 即可自动追踪各 App 营销策略与页面布局的迭代演进。")
    lines.append("")

    report_md = run_dir / "report.md"
    report_md.write_text("\n".join(lines), encoding="utf-8")

    # Also output report.json for CI/CD integration
    report_json = run_dir / "report.json"
    report_json.write_text(json.dumps({
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "baseline_dir": str(baseline_dir) if baseline_dir else None,
        "query": query,
        "environment": env_data,
        "summary": summary_data
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return report_md


def main() -> int:
    parser = argparse.ArgumentParser(description="独立重新生成巡检 Markdown/JSON 报告")
    parser.add_argument("run", type=Path, help="runner.py 生成的时间戳运行目录 (outputs/<timestamp>)")
    parser.add_argument("--baseline", type=Path, default=None, help="基线运行目录 (可选)")
    parser.add_argument("--query", default="洗衣液", help="巡检关键词")
    args = parser.parse_args()

    if not args.run.exists() or not args.run.is_dir():
        print(f"错误：运行目录不存在: {args.run}")
        return 1

    report_path = generate(args.run, args.baseline, query=args.query)
    print(f"成功重新生成报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
