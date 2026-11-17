"""Run with: python3 -m unittest discover -s tests -p 'test_mailbox_v1.py'"""
import sqlite3
import unittest
import mailbox_v1 as m


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.db = "file:mailbox-test?mode=memory&cache=shared"
        self.keep = sqlite3.connect(self.db, uri=True)
        self.keep.row_factory = sqlite3.Row
        self.keep.execute("PRAGMA foreign_keys=ON")
        self.keep.execute("CREATE TABLE users(public_key TEXT PRIMARY KEY,display_name TEXT)")
        self.keep.execute("""CREATE TABLE mail(id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_key TEXT,recipient_key TEXT,message TEXT,created_at TEXT,read_at TEXT)""")
        self.keep.executemany("INSERT INTO users VALUES (?,?)",
                              [("a","Alice"),("b","Bob"),("c","Carol")])
        self.keep.execute("INSERT INTO mail(sender_key,recipient_key,message,created_at) VALUES ('a','b','Hello','2026-01-01')")
        m.ensure_schema(self.keep)
        self.keep.commit()

    def tearDown(self):
        self.keep.close()

    def connect(self):
        c = sqlite3.connect(self.db,uri=True)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def run_cmd(self, key, command):
        user = {"display_name":{"a":"Alice","b":"Bob","c":"Carol"}[key]}
        return m.handle(command,key,user,self.connect)

    def test_mail_counts_and_listing(self):
        self.assertIn("1 unread, 1 total",self.run_cmd("b","MAIL"))
        self.assertIn("1 N Alice",self.run_cmd("b","LIST"))
        self.assertIn("1 N Alice",self.run_cmd("b","LN"))
        self.assertIn("1",self.run_cmd("a","SENT"))

    def test_read_and_reply(self):
        self.assertIn("Hello",self.run_cmd("b","READ"))
        self.assertIn("0 unread",self.run_cmd("b","MAIL"))
        self.assertIn("Reply saved",self.run_cmd("b","REPLY Thanks"))
        self.assertIn("Thanks",self.run_cmd("a","READ"))
        self.assertIn("Hello",self.run_cmd("b","R 1"))

    def test_cross_user_access_denied(self):
        self.assertIn("not found",self.run_cmd("c","READ 1"))
        self.assertIn("not found",self.run_cmd("c","DELETE 1"))
        self.assertIn("not found",self.run_cmd("c","REPLY 1 Intrusion"))
        self.assertIn("1 unread",self.run_cmd("b","MAIL"))

    def test_soft_delete_and_sent_visibility(self):
        self.assertIn("removed",self.run_cmd("b","K 1"))
        self.assertIn("0 total",self.run_cmd("b","MAIL"))
        self.assertIn("1 total",self.run_cmd("a","SENT"))
        self.assertIn("not found",self.run_cmd("b","READ 1"))
        self.assertEqual(1,self.keep.execute("SELECT COUNT(*) FROM mail WHERE id=1").fetchone()[0])

    def test_km_requires_confirmation(self):
        self.run_cmd("b","READ 1")
        self.assertIn("KM CONFIRM",self.run_cmd("b","KM"))
        self.assertIn("1 total",self.run_cmd("b","MAIL"))
        self.assertIn("Removed 1",self.run_cmd("b","KM CONFIRM"))
        self.assertIn("0 total",self.run_cmd("b","MAIL"))

    def test_pagination_and_invalid_inputs(self):
        self.assertIn("Usage",self.run_cmd("b","LIST -1"))
        self.assertIn("Usage",self.run_cmd("b","READ nope"))
        self.assertIn("Usage",self.run_cmd("b","K"))
        self.assertIn("MAIL counts",self.run_cmd("b","HELP MAIL"))

if __name__ == "__main__":
    unittest.main()
