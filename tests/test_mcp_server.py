import json
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "mcp", "dobby_mcp_server.py")


def rpc(requests, timeout=60):
    """Send JSON-RPC lines to a fresh server process; return responses by id."""
    lines = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run([sys.executable, SERVER, "--repo", REPO],
                          input=lines, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)
    out = {}
    for line in proc.stdout.splitlines():
        if line.strip():
            msg = json.loads(line)
            out[msg.get("id")] = msg
    return out


def call(name, arguments, msg_id=10):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


INIT = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"}]


def result_json(resp):
    return json.loads(resp["result"]["content"][0]["text"])


class TestMCPServer(unittest.TestCase):
    def test_initialize_and_toollist_minimal(self):
        out = rpc(INIT + [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        self.assertEqual(out[1]["result"]["serverInfo"]["name"], "dobby-gateway")
        tools = out[2]["result"]["tools"]
        self.assertEqual(len(tools), 4, "progressive disclosure: only 4 meta-tools")
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"search_capabilities", "get_capability",
                                 "invoke_capability", "get_context_pack"})

    def test_search_then_get_then_invoke_builtin(self):
        out = rpc(INIT + [
            call("search_capabilities", {"query": "bootstrap scan setup"}, 2),
            call("get_capability", {"id": "run_tests"}, 3),
            call("invoke_capability",
                 {"id": "kg_query",
                  "args": {"query": "start a multi-step task ledger"}}, 4),
        ])
        search = result_json(out[2])
        self.assertTrue(any(c["id"] == "bootstrap_scan"
                            for c in search["capabilities"]))
        sig = result_json(out[3])
        self.assertIn("command_template", sig)
        pack = result_json(out[4])
        ids = [i["id"] for i in pack["items"]]
        self.assertIn("skill:ledgered-task", ids)

    def test_context_pack_routes(self):
        out = rpc(INIT + [call(
            "get_context_pack",
            {"task": "Generate the config export and then convert the schema"},
            2)])
        plan = result_json(out[2])
        self.assertGreaterEqual(plan["level"], 5)
        self.assertIn("P-CONTRACT", plan["policies"])

    def test_exec_capability_enveloped_and_audited(self):
        out = rpc(INIT + [call("invoke_capability", {"id": "env_probe"}, 2)])
        res = result_json(out[2])
        self.assertEqual(res["exit_code"], 0)
        self.assertTrue(res["output"]["untrusted"],
                        "exec output must be marked untrusted (injection defense)")
        audit = os.path.join(REPO, ".dobby", "state", "audit.jsonl")
        self.assertTrue(os.path.exists(audit))

    def test_unlisted_capability_refused(self):
        out = rpc(INIT + [call("invoke_capability",
                               {"id": "shell", "args": {"cmd": "rm -rf /"}}, 2)])
        res = result_json(out[2])
        self.assertIn("not an allowlisted capability", res["error"])

    def test_unknown_tool_is_rpc_error_not_crash(self):
        out = rpc(INIT + [call("format_disk", {}, 2),
                          {"jsonrpc": "2.0", "id": 3, "method": "ping"}])
        self.assertIn("error", out[2])
        self.assertEqual(out[3]["result"], {}, "server must survive bad calls")

    def test_output_size_cap(self):
        # kg_query with huge k still comes back under the cap
        out = rpc(INIT + [call("invoke_capability",
                               {"id": "kg_query",
                                "args": {"query": "policy skill tool doc",
                                         "k": 50}}, 2)])
        text = out[2]["result"]["content"][0]["text"]
        self.assertLessEqual(len(text), 21000)


if __name__ == "__main__":
    unittest.main()
