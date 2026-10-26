"""Network-free regressions for the installed LAN counter allocation."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "splashme"


class CounterConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.25
        self.waiters = []
        self.sent = []
        self.posts = []

        async def sleep(delay):
            future = asyncio.get_running_loop().create_future()
            self.waiters.append((self.now + delay, future))
            await future

        def wrap(key, counter, payload):
            self.sent.append((counter, self.now, payload))
            return b"test-envelope"

        counter = next(n for n in ast.parse((ROOT / "pv2.py").read_text(encoding="utf-8")).body
                       if isinstance(n, ast.ClassDef) and n.name == "Counter")
        method = next(n for n in ast.walk(ast.parse((ROOT / "lan.py").read_text(encoding="utf-8")))
                      if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_pv2")
        namespace = {"Any": object, "LanError": RuntimeError,
                     "time": SimpleNamespace(time=lambda: self.now),
                     "asyncio": SimpleNamespace(sleep=sleep),
                     "pv2": SimpleNamespace(wrap=wrap)}
        exec(compile(ast.Module(body=[counter, method], type_ignores=[]),
                     "<production counter methods>", "exec"), namespace)
        self.call = namespace["_async_pv2"]
        self.client = SimpleNamespace(_counter=namespace["Counter"](),
                                      _require_key=lambda: b"test-key")
        self.client._counter._last = 100

        async def post(command, body):
            self.posts.append(body)
            return {"ok": True}

        self.client._async_post = post

    async def advance(self, now):
        self.now = now
        for deadline, future in self.waiters:
            if deadline <= now and not future.done():
                future.set_result(None)
        await asyncio.sleep(0)

    async def test_concurrent_waiters_never_sign_ahead_of_clock(self):
        tasks = [asyncio.create_task(self.call(self.client, "q", "test", b""))
                 for _ in range(2)]
        self.addAsyncCleanup(asyncio.gather, *tasks, return_exceptions=True)
        await asyncio.sleep(0)
        await self.advance(101.0)
        first_second = list(self.sent)
        await self.advance(102.0)
        await asyncio.gather(*tasks)
        self.assertEqual([counter for counter, _, _ in first_second], [101])
        self.assertEqual([counter for counter, _, _ in self.sent], [101, 102])
        self.assertTrue(all(counter <= clock for counter, clock, _ in self.sent))

    async def test_cancelled_wait_does_not_consume_counter(self):
        task = asyncio.create_task(self.call(self.client, "a", "test", b"payload"))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client._counter._last, 100)
        self.assertEqual(self.posts, [])

    async def test_unsigned_state_does_not_wait_or_advance_counter(self):
        await self.call(self.client, "state")
        self.assertEqual(self.waiters, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.posts, [{"op": "state", "name": "", "env": ""}])


if __name__ == "__main__":
    unittest.main()

