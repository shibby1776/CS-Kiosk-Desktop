import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DiagnosticsShortcutTests(unittest.TestCase):
    def test_visible_menu_and_priority_bindtag_are_packaged(self):
        source = (ROOT / "sorter/ui/app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("_install_diagnostics_shortcut_tag", methods)
        self.assertIn('label="Enable Diagnostics"', source)
        self.assertIn('accelerator="Ctrl+D"', source)
        self.assertIn('"<Control-KeyPress-d>"', source)
        self.assertIn('"<Control-KeyPress-D>"', source)
        self.assertIn('"<Map>", self._install_diagnostics_shortcut_tag', source)
        self.assertIn('widget.bindtags((tag, *tags))', source)


if __name__ == "__main__":
    unittest.main()
