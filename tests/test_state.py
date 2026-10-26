"""状态存储测试：去重、冷却、小时额度、持久化。"""

from __future__ import annotations

from pathlib import Path
from unittest import TestCase

import importlib.util
import json
import sys
import tempfile

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = PLUGIN_DIR.name


def _load_state_module() -> object:
    """按 Runner 的加载方式导入插件的 state 模块。"""

    if MODULE_NAME in sys.modules:
        return sys.modules[f"{MODULE_NAME}.state"]
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
    return sys.modules[f"{MODULE_NAME}.state"]


STATE = _load_state_module()


class StateStoreTest(TestCase):
    """状态存储行为。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = STATE.StateStore(Path(self._tmp.name))

    def test_processed_dedupe(self) -> None:
        self.assertFalse(self.store.is_processed("m1", 1000.0))
        self.store.mark_processed("m1", 1000.0)
        self.assertTrue(self.store.is_processed("m1", 1001.0))

    def test_cooldown(self) -> None:
        self.store.record_trigger("s1", 1000.0)
        self.assertEqual(self.store.cooldown_remaining("s1", 1010.0, 180), 170.0)
        self.assertEqual(self.store.cooldown_remaining("s1", 1300.0, 180), 0.0)
        self.assertEqual(self.store.cooldown_remaining("other", 1010.0, 180), 0.0)

    def test_hourly_count_prunes_old(self) -> None:
        self.store.record_trigger("s1", 1000.0)
        self.store.record_trigger("s1", 5000.0)
        self.assertEqual(self.store.hourly_count(5000.0), 1)
        self.assertEqual(self.store.hourly_count(9000.0), 0)

    def test_save_and_load(self) -> None:
        self.store.mark_processed("m1", 1000.0)
        self.store.record_trigger("s1", 1000.0)
        self.store.save()

        payload = json.loads((Path(self._tmp.name) / "state.json").read_text(encoding="utf-8"))
        self.assertIn("m1", payload["processed"])

        reloaded = STATE.StateStore(Path(self._tmp.name))
        reloaded.load()
        self.assertTrue(reloaded.is_processed("m1", 1000.0))
        self.assertEqual(reloaded.cooldown_remaining("s1", 1010.0, 180), 170.0)

    def test_corrupt_state_raises(self) -> None:
        (Path(self._tmp.name) / "state.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(STATE.StateError):
            self.store.load()

    def test_prune_processed_ttl(self) -> None:
        self.store.mark_processed("old", 1000.0)
        self.store.prune(1000.0 + 24 * 3600 + 1)
        self.assertFalse(self.store.is_processed("old", 1000.0 + 24 * 3600 + 1))
