import sys
import time
import unittest

import server

PY = sys.executable


class RunCappedTest(unittest.TestCase):
    def test_normal_run_returns_output(self):
        rc, out, err, timed_out, truncated = server.run_capped(
            [PY, "-c", "import sys; print('hi'); sys.stderr.write('warn')"], 10, 1024, lambda: None)
        self.assertEqual((rc, out.strip(), err, timed_out, truncated), (0, "hi", "warn", False, False))

    def test_endless_output_is_capped_and_killed(self):
        start = time.monotonic()
        rc, out, _, timed_out, truncated = server.run_capped(
            [PY, "-c", "while True: print('A' * 1000)"], 10, 4096, lambda: None)
        self.assertTrue(truncated)
        self.assertFalse(timed_out)
        self.assertEqual(len(out), 4096)
        self.assertLess(time.monotonic() - start, 5)

    def test_timeout_kills_process(self):
        killed = []
        rc, _, _, timed_out, truncated = server.run_capped(
            [PY, "-c", "import time; time.sleep(30)"], 0.5, 1024, lambda: killed.append(1))
        self.assertTrue(timed_out)
        self.assertFalse(truncated)
        self.assertEqual(killed, [1])


class CompileEndpointTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_body_too_large_is_413_json(self):
        r = self.client.post("/compile", json={"language": "py", "code": "#" + "x" * 70_000})
        self.assertEqual(r.status_code, 413)
        self.assertIn("too large", r.get_json()["error"])

    def test_non_object_or_non_string_fields_are_400(self):
        for body in ([1, 2], {"language": 5, "code": "x"}, {"language": "py", "code": None}):
            r = self.client.post("/compile", json=body)
            self.assertEqual(r.status_code, 400, body)

    def test_busy_server_returns_503(self):
        held = [server._run_slots.acquire(blocking=False) for _ in range(server.MAX_CONCURRENT_RUNS)]
        old_wait, server.QUEUE_WAIT_SECONDS = server.QUEUE_WAIT_SECONDS, 0.1
        try:
            r = self.client.post("/compile", json={"language": "py", "code": "print(1)"})
            self.assertEqual(r.status_code, 503)
        finally:
            server.QUEUE_WAIT_SECONDS = old_wait
            for ok in held:
                if ok:
                    server._run_slots.release()


if __name__ == "__main__":
    unittest.main()
