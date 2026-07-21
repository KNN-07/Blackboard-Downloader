import unittest
from unittest.mock import patch

from blackboard_gui.dependencies import audit_dependencies, install_command


class DependencyAuditTest(unittest.TestCase):
    @patch("blackboard_gui.dependencies.importlib.util.find_spec")
    def test_audit_lists_only_missing_dependencies(self, find_spec):
        find_spec.side_effect = lambda name: None if name == "selenium" else object()
        missing = audit_dependencies()
        self.assertEqual([dependency.package_name for dependency in missing], ["selenium"])

    def test_install_command_uses_current_interpreter_and_requirements(self):
        command = install_command()
        self.assertIn("-m pip install -r", command)
        self.assertIn("requirements.txt", command)


if __name__ == "__main__":
    unittest.main()
