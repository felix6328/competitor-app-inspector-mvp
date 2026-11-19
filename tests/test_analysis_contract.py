"""Offline checks for the deep-report contract and the worked example."""

from __future__ import annotations

import unittest
from pathlib import Path

import analysis_contract


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "docs" / "examples" / "深度报告范例-合成演示.md"
TEMPLATE = ROOT / "docs" / "analysis" / "深度报告模板.md"


class ContractTests(unittest.TestCase):
    def test_example_passes_and_template_does_not_count_as_finished(self) -> None:
        example_errors = analysis_contract.check_path(EXAMPLE, "deep")
        self.assertEqual(example_errors, [])
        template_as_deep = analysis_contract.check_path(TEMPLATE, "deep")
        self.assertTrue(template_as_deep)
        template_errors = analysis_contract.check_path(TEMPLATE, "template")
        self.assertEqual(template_errors, [])

    def test_example_keeps_synthesis_separate_from_named_apps(self) -> None:
        text = EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("合成演示", text)
        self.assertIn("非实测", text)
        self.assertIn("澄市", text)
        self.assertIn("邻时", text)
        self.assertIn("合成人物", text)
        self.assertIn("【缺失】", text)
        for banned in ("淘宝当前", "京东实测", "抖音实测", "美团实测", "实测转化率"):
            self.assertNotIn(banned, text)

    def test_measured_claim_without_evidence_id_is_rejected(self) -> None:
        document = """<!-- evidence_mode: measured -->
# 标题
证据模式：实测
## 结论
京东实测转化率提升 20%，GMV +12，n = 8000
## 分析对象与可比性
## 证据索引
置信度：高
## 重点问题
### 问题一
证据引用 观察 设计机制 用户成本 业务收益与代价 适用边界 建议 验证方案
### 问题二
证据引用 观察 设计机制 用户成本 业务收益与代价 适用边界 建议 验证方案
### 问题三
证据引用 观察 设计机制 用户成本 业务收益与代价 适用边界 建议 验证方案
## 对比与维度取舍
```mermaid
flowchart TD
    A --> B
```
## 建议、取舍与实验
P0 P1 P2
不做
## 自动巡检如何标记
拒绝 登录 坐标 基线 证据丢失 弹窗 实验分组
## 附录：方法、缺失与声明
【事实】【推断】【建议】【待验证】【缺失】【合成】
"""
        errors = analysis_contract.validate_markdown(document, kind="deep")
        joined = "\n".join(errors)
        self.assertIn("京东实测", joined)
        self.assertIn("转化率", joined)
        self.assertIn("GMV", joined)
        self.assertIn("样本量", joined)
        self.assertIn("EVD-", joined)

    def test_finding_missing_a_chain_step_is_rejected(self) -> None:
        document = EXAMPLE.read_text(encoding="utf-8").replace("**适用边界**", "**边界说明**", 1)
        errors = analysis_contract.validate_markdown(document, kind="deep")
        self.assertTrue(any("适用边界" in item for item in errors))

    def test_readme_points_at_the_new_entry(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/examples/深度报告范例-合成演示.md", readme)
        self.assertIn("docs/analysis/深度报告模板.md", readme)
        self.assertIn("合成演示", readme)


if __name__ == "__main__":
    unittest.main()
