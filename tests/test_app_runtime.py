import unittest
from unittest.mock import AsyncMock

from app_runtime import AppServerRuntime, RuntimeActiveTurn


class AppRuntimeControlCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_turn_uses_thread_id_and_turn_id(self):
        runtime = AppServerRuntime(lambda: None)
        client = AsyncMock()
        runtime._ensure_client_async = AsyncMock(return_value=client)
        active_turn = RuntimeActiveTurn(
            session_id="019d5868-71ba-7101-9143-81867f3db5bf",
            turn_id="turn-123",
            started_at=0,
        )

        await runtime._interrupt_turn_async(active_turn)

        client.request.assert_awaited_once_with(
            "turn/interrupt",
            {
                "threadId": "019d5868-71ba-7101-9143-81867f3db5bf",
                "turnId": "turn-123",
            },
        )

    async def test_steer_turn_uses_thread_id_and_expected_turn_id(self):
        runtime = AppServerRuntime(lambda: None)
        client = AsyncMock()
        runtime._ensure_client_async = AsyncMock(return_value=client)
        active_turn = RuntimeActiveTurn(
            session_id="019d5868-71ba-7101-9143-81867f3db5bf",
            turn_id="turn-456",
            started_at=0,
        )

        await runtime._steer_turn_async(active_turn, "focus on tests")

        client.steer_turn.assert_awaited_once_with(
            thread_id="019d5868-71ba-7101-9143-81867f3db5bf",
            expected_turn_id="turn-456",
            input_items=[{"type": "text", "text": "focus on tests"}],
        )


if __name__ == "__main__":
    unittest.main()
