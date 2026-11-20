from anyio import create_task_group
from starlette.requests import ClientDisconnect
from starlette.responses import FileResponse
from starlette.types import Message, Receive, Scope, Send


class PlaybackFileResponse(FileResponse):
    """Serve byte ranges and stop reading when the browser abandons a request."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        disconnected = False

        async def watch_disconnect() -> None:
            nonlocal disconnected
            while (await receive())["type"] != "http.disconnect":
                pass
            disconnected = True

        async def send_to_client(message: Message) -> None:
            if disconnected:
                raise ClientDisconnect()
            try:
                await send(message)
            except OSError as error:
                raise ClientDisconnect() from error

        async with create_task_group() as tasks:
            tasks.start_soon(watch_disconnect)
            try:
                await super().__call__(scope, receive, send_to_client)
            except ClientDisconnect:
                # Let FileResponse unwind its file context before cancelling the watcher.
                pass
            finally:
                tasks.cancel_scope.cancel()
