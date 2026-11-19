"""Static privacy guards for NAC-BBS. No radio, database, or BBS import needed.

Run: python3 -m unittest discover -s tests -p 'test_privacy_*.py' -v
"""
import ast
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "bbs.py"


class PrivacyGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(SOURCE.read_text(encoding="utf-8"))

    def test_reception_history_never_accepts_message_body(self):
        functions = [
            node for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "store_incoming_message"
        ]
        self.assertEqual(len(functions), 1)
        function = functions[0]
        self.assertEqual([arg.arg for arg in function.args.args], ["sender_key", "snr"])
        inserts = [
            node for node in ast.walk(function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and "INSERT INTO messages" in node.value
        ]
        self.assertEqual(len(inserts), 1)
        self.assertIn("message", inserts[0].value.lower())
        self.assertTrue(any(
            isinstance(node, ast.Constant) and node.value == "[REDACTED]"
            for node in ast.walk(function)
        ))

    def test_reception_calls_do_not_pass_message_body(self):
        calls = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "store_incoming_message"
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0].args), 2)
        self.assertEqual(
            [node.id for node in calls[0].args if isinstance(node, ast.Name)],
            ["public_key", "snr"],
        )

    def test_message_handler_prints_no_content_or_error_payload(self):
        handlers = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "handle_contact_message"
        ]
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        forbidden = {"text", "reply", "msg", "send_result"}
        for node in ast.walk(handler):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "print":
                continue
            for arg in node.args:
                referenced = {
                    n.id for n in ast.walk(arg) if isinstance(n, ast.Name)
                }
                self.assertFalse(
                    referenced & forbidden,
                    f"Sensitive variable used in print at line {node.lineno}: "
                    f"{referenced & forbidden}",
                )


if __name__ == "__main__":
    unittest.main()
