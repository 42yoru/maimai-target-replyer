"""WebUI 配置页的中文展示结构测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import TestCase

import importlib.util
import sys

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = PLUGIN_DIR.name


def _load_plugin_module() -> Any:
    """按 Runner 的加载方式导入插件模块。"""

    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        str(PLUGIN_DIR / "plugin.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法为插件创建 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


PLUGIN_MODULE = _load_plugin_module()


class ConfigUiSchemaTest(TestCase):
    """展示层只改组结构，不改真实配置路径。"""

    def setUp(self) -> None:
        self.schema = PLUGIN_MODULE.TargetReplyerPlugin.build_config_schema()

    def test_tabs(self) -> None:
        layout = self.schema["layout"]
        self.assertEqual(layout["type"], "tabs")
        titles = [tab["title"] for tab in layout["tabs"]]
        self.assertEqual(titles, ["基础", "预判与节奏", "高级"])
        advanced = next(tab for tab in layout["tabs"] if tab["id"] == "advanced")
        self.assertEqual(advanced["sections"], ["prompt", "plugin_info"])

    def test_sections_point_to_real_config_names(self) -> None:
        sections = self.schema["sections"]
        self.assertEqual(sections["target"]["name"], "target")
        self.assertEqual(sections["scope"]["name"], "scope")
        self.assertEqual(sections["decide"]["name"], "decide")
        self.assertEqual(sections["prompt"]["name"], "prompt")
        self.assertEqual(sections["reply"]["name"], "reply")
        self.assertEqual(sections["plugin_info"]["name"], "plugin")

    def test_field_labels_are_chinese(self) -> None:
        sections = self.schema["sections"]
        self.assertEqual(sections["plugin_info"]["fields"]["enabled"]["label"], "启用插件")
        self.assertEqual(sections["target"]["fields"]["user"]["label"], "目标账号")
        self.assertEqual(sections["decide"]["fields"]["max_tokens"]["label"], "预判输出上限")
        self.assertEqual(sections["prompt"]["fields"]["template"]["label"], "预判提示词模板")
        for section in sections.values():
            for name, field in section["fields"].items():
                self.assertNotEqual(field["label"], name, f"{section['name']}.{name} 还没有中文标签")

    def test_hints_exist(self) -> None:
        sections = self.schema["sections"]
        for section in sections.values():
            for field in section["fields"].values():
                self.assertTrue(field["hint"], f"{section['name']}.{field['name']} 缺少提示")

    def test_config_version_readonly(self) -> None:
        version_field = self.schema["sections"]["plugin_info"]["fields"]["config_version"]
        self.assertTrue(version_field["disabled"])

    def test_enum_fields_are_selects(self) -> None:
        sections = self.schema["sections"]
        self.assertEqual(sections["target"]["fields"]["presence_policy"]["ui_type"], "select")
        self.assertEqual(sections["decide"]["fields"]["mode"]["ui_type"], "select")

    def test_prompt_is_textarea(self) -> None:
        prompt_field = self.schema["sections"]["prompt"]["fields"]["template"]
        self.assertEqual(prompt_field["ui_type"], "textarea")
        self.assertIn("$recent", prompt_field["hint"])
        self.assertIn("$bot_name", prompt_field["hint"])
