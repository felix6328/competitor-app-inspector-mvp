#!/usr/bin/env python3
"""Minimum offline checks for a deep-analysis markdown document.

The checker looks at structure and a small set of forbidden claim patterns.
It cannot decide whether a sentence is wise. It can reject a document that
forgets its evidence mode, drops the finding chain, or states a metric as a
measured result.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


H2_TITLES = (
    "结论",
    "分析对象与可比性",
    "证据索引",
    "重点问题",
    "对比与维度取舍",
    "建议、取舍与实验",
    "自动巡检如何标记",
    "附录：方法、缺失与声明",
)

CHAIN_LABELS = (
    "证据引用",
    "观察",
    "设计机制",
    "用户成本",
    "业务收益与代价",
    "适用边界",
    "建议",
    "验证方案",
)

CLAIM_MARKS = ("【事实】", "【推断】", "【建议】", "【待验证】", "【缺失】", "【合成】")

FORBIDDEN_PATTERNS = (
    (r"实测转化率", "出现了「实测转化率」。仓库里没有转化率采集。"),
    (r"淘宝当前", "出现了「淘宝当前」。这会被读成对真实界面的事实陈述。"),
    (r"京东实测", "出现了「京东实测」。"),
    (r"抖音实测", "出现了「抖音实测」。"),
    (r"美团实测", "出现了「美团实测」。"),
    (r"转化率提升\s*\d+(?:\.\d+)?\s*%", "给出了无来源的转化率涨幅。"),
    (r"GMV\s*[+＋]\s*\d+", "给出了无来源的 GMV 增量。"),
    (r"n\s*=\s*\d{4,}", "给出了四位及以上的样本量。样本量必须来自真实基线，不能写死在范例里。"),
    (r"24030PN60C", "写入了默认机型编码。没有 device_env 记录时不应出现该编码。"),
)

INSPECTION_TERMS = ("拒绝", "登录", "坐标", "基线", "证据丢失", "弹窗", "实验分组")


def _h2_titles(text: str) -> list[str]:
    titles: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            titles.append(match.group(1).strip())
    return titles


def _section(text: str, title: str) -> str:
    pattern = rf"^##\s+{re.escape(title)}\s*$"
    match = re.search(pattern, text, flags=re.M)
    if not match:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"^##\s+", rest, flags=re.M)
    if nxt:
        return rest[:nxt.start()]
    return rest


def _findings(section_text: str) -> list[str]:
    parts = re.split(r"^###\s+", section_text, flags=re.M)
    return parts[1:]


def _mode(text: str) -> str | None:
    match = re.search(r"<!--\s*evidence_mode:\s*(synthetic|measured|template)\s*-->", text)
    if not match:
        return None
    return match.group(1)


def validate_markdown(text: str, kind: str = "deep") -> list[str]:
    """Return human-readable errors. An empty list means the minimum check passed."""
    if kind not in {"deep", "template"}:
        return [f"未知校验类型: {kind}"]

    errors: list[str] = []
    mode = _mode(text)
    if mode is None:
        errors.append("缺少 evidence_mode 注释，允许值为 synthetic、measured、template。")
    titles = _h2_titles(text)
    for title in H2_TITLES:
        if title not in titles:
            errors.append(f"缺少章节：## {title}")

    if "```mermaid" not in text:
        errors.append("缺少 Mermaid 图。旅程或状态至少要有一张。")

    findings = _findings(_section(text, "重点问题"))
    if kind == "deep" and len(findings) < 3:
        errors.append("重点问题少于 3 条。")
    if kind == "template" and len(findings) < 1:
        errors.append("模板至少保留一条带完整链条的重点问题骨架。")
    for index, finding in enumerate(findings, start=1):
        missing = [label for label in CHAIN_LABELS if label not in finding]
        if missing:
            errors.append(f"第 {index} 条重点问题缺少链条字段：{'、'.join(missing)}")

    inspection = _section(text, "自动巡检如何标记")
    for term in INSPECTION_TERMS:
        if term not in inspection:
            errors.append(f"「自动巡检如何标记」缺少：{term}")

    if "P0" not in text or "P1" not in text or "P2" not in text:
        errors.append("建议排序需要同时给出 P0、P1、P2。")
    if "不做" not in text:
        errors.append("缺少明确不做项。")

    if kind == "template":
        if mode not in {None, "template"}:
            errors.append("模板的 evidence_mode 必须是 template。")
        if "{{" not in text:
            errors.append("模板缺少占位符。空模板不应被写成已经完成的结论。")
        if "不得伪造" not in text and "不能伪造" not in text:
            errors.append("模板需要写明不得伪造缺失数据。")
        return errors

    if "{{" in text:
        errors.append("正式报告还留着模板占位符。")
    if mode == "template":
        errors.append("模板不能当作正式深度报告通过校验。")
        return errors

    for mark in CLAIM_MARKS:
        if mark not in text:
            errors.append(f"缺少主张标记 {mark}")

    for pattern, message in FORBIDDEN_PATTERNS:
        if re.search(pattern, text):
            errors.append(message)
    if "![" in text:
        errors.append("深度报告含有图片语法。没有可追溯截图文件时不放图，页面拆解用文字线框。")

    if mode == "synthetic":
        for phrase in ("合成演示", "非实测"):
            if phrase not in text:
                errors.append(f"合成报告必须写明「{phrase}」。")
        if "【缺失】" not in text:
            errors.append("合成报告必须保留缺失项，不能把缺口写满。")
    elif mode == "measured":
        if "证据模式：实测" not in text:
            errors.append("实测报告必须写明「证据模式：实测」。")
        if "置信度" not in text:
            errors.append("实测报告的证据索引必须有置信度。")
        if not re.search(r"EVD-\d+", text):
            errors.append("实测报告至少要有一条 EVD- 编号的证据。")
        if "合成演示" in text and "本报告不是合成演示" not in text:
            errors.append("实测报告若提到合成演示，需要明确本报告不是合成演示。")
    elif mode is not None:
        errors.append(f"正式报告不能使用 evidence_mode={mode}。")

    return errors


def check_path(path: Path, kind: str) -> list[str]:
    if not path.is_file():
        return [f"文件不存在：{path}"]
    return validate_markdown(path.read_text(encoding="utf-8"), kind=kind)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验深度分析 Markdown 是否满足最小输出契约")
    parser.add_argument("command", choices=("check",))
    parser.add_argument("path", type=Path)
    parser.add_argument("--kind", choices=("deep", "template"), default="deep")
    args = parser.parse_args(argv)
    errors = check_path(args.path, args.kind)
    if errors:
        print(f"未通过：{args.path}")
        for item in errors:
            print(f"- {item}")
        return 1
    print(f"通过：{args.path} （{args.kind}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
