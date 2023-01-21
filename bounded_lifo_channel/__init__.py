"""
A queue for sending values between asynchronous tasks.

This module provides a bounded channel.
It has a limit on the number of messages that the channel can store,
and if this limit is reached, trying to send another message will wait
until a message is received from the channel.

The channel constructor function provides separate send and receive handles, `Sender` and `Receiver`
If there is no message to read, the current task will be notified when a new value is sent.
`Sender` allows sending values into the channel.
If the channel is at capacity, the send is rejected
and the task will be notified when additional capacity is available.
In other words, the channel provides backpressure.

This channel is also suitable for the single-producer single-consumer use-case.
(Unless you only need to send one message, in which case you should use the oneshot channel.)
"""


from asyncio import FIRST_COMPLETED, Event, LifoQueue, create_task, wait
from dataclasses import dataclass
from typing import AsyncIterator, Generic, TypeVar

from option_and_result import (
    Err,
    MatchesNone,
    MatchesSome,
    NONE,
    Ok,
    Option,
    Result,
    Some,
)

T = TypeVar("T")


@dataclass
class TrySendErrorFull(Generic[T]):
    """
    The data could not be sent on the channel because the channel is currently full
    and sending would require blocking.
    """

    value: T

    def __str__(self):
        return "no available capacity"


@dataclass
class TrySendErrorClosed(Generic[T]):
    "The receive half of the channel was explicitly closed or has been dropped."

    value: T

    def __str__(self):
        return "channel closed"


@dataclass
class SendError(Generic[T]):
    "Error returned by the `Sender`"

    value: T

    def __str__(self):
        return "channel closed"


@dataclass
class Sender(Generic[T]):
    "Sends values to the associated `Receiver`"

    _queue: LifoQueue[T]
    _closed: Event
    _disconnected: Event

    async def send(self, value: T) -> Result[None, SendError[T]]:
        """
        Sends a value, waiting until there is capacity.

        A successful send occurs when it is determined that the other end of the channel
        has not hung up already.
        An unsuccessful send would be one where the corresponding receiver has already been closed.
        Note that a return value of `Err` means that the data will never be received,
        but a return value of `Ok` does not mean that the data will be received.
        It is possible for the corresponding receiver to hang up
        immediately after this function returns `Ok`.

        # Errors

        If the receive half of the channel is closed, either due to `close` being called
        or the `Receiver` handle dropping, the function returns an error.
        The error includes the value passed to send.
        """

        closed_event = create_task(self._closed.wait())
        put_task = create_task(self._queue.put(value))
        done, _pending = await wait(
            [put_task, closed_event], return_when=FIRST_COMPLETED
        )

        if closed_event in done:
            put_task.cancel()
            if self._queue.empty():
                self._disconnected.set()
            return Err(SendError(value))

        assert put_task in done
        closed_event.cancel()
        return Ok(None)

    async def closed(self):
        """
        Completes when the receiver has dropped.

        This allows the producers to get notified when interest in the produced values is canceled
        and immediately stop doing work.
        """
        await self._closed.wait()

    def try_send(
        self, value: T
    ) -> Result[None, TrySendErrorFull[T] | TrySendErrorClosed[T]]:
        """
        Attempts to immediately send a message on this `Sender`

        This method differs from `send` by returning immediately if the channel's buffer is full
        or no receiver is waiting to acquire some data.
        Compared with `send`, this function has two failure cases
        instead of one (one for disconnection, one for a full buffer).
        """

        if self._queue.full():
            # Not actually a type error but Pylance (in VS Code) thinks it is
            return Err(TrySendErrorFull(value))  # type: ignore

        if self._closed.is_set():
            # Not actually a type error but Pylance (in VS Code) thinks it is
            return Err(TrySendErrorClosed(value))  # type: ignore

        # This cannot raise an exception given the guards above
        self._queue.put_nowait(value)

        return Ok(None)

    def is_closed(self):
        """
        Checks if the channel has been closed.
        This happens when the `Receiver` is dropped, or when the `Receiver.close` method is called.
        """
        return self._closed.is_set()

    def max_capacity(self):
        """
        Returns the maximum buffer capacity of the channel.

        The maximum capacity is the buffer capacity initially specified when calling `channel`.
        This is distinct from capacity, which returns the *current* available buffer capacity:
        as messages are sent and received, the value returned by `capacity` will go up or down,
        whereas the value returned by `max_capacity` will remain constant.
        """

        return self._queue.maxsize

    def capacity(self):
        """
        Returns the current capacity of the channel.

        The capacity goes down when sending a value by calling `send`.
        The capacity goes up when values are received by the `Receiver`.
        This is distinct from `max_capacity`, which always returns buffer capacity
        initially specified when calling `channel`
        """

        return self._queue.maxsize - self._queue.qsize()

    def __del__(self):
        self._closed.set()
        if self._queue.empty():
            self._disconnected.set()


@dataclass
class TryRecvErrorEmpty:
    """
    This channel is currently empty, but the `Sender`(s) have not yet
    disconnected, so data may yet become available.
    """

    def __str__(self):
        return "receiving on an empty channel"


@dataclass
class TryRecvErrorDisconnected:
    """
    The channel's sending half has become disconnected,
    and there will never be any more data received on it.
    """

    def __str__(self):
        return "receiving on a closed channel"


@dataclass
class Receiver(Generic[T]):
    "Receives values from the associated `Sender`"

    _queue: LifoQueue[T]
    _closed: Event
    _disconnected: Event

    async def recv(self) -> Option[T]:
        """
        Receives the next value for this receiver.

        This method returns `NONE()` if the channel has been closed
        and there are no remaining messages in the channel's buffer.
        This indicates that no further values can ever be received from this `Receiver`.
        The channel is closed when all senders have been dropped, or when `close` is called.

        If there are no messages in the channel's buffer, but the channel has not yet been closed,
        this method will sleep until a message is sent or the channel is closed.
        """

        disconnected_event = create_task(self._disconnected.wait())
        get_task = create_task(self._queue.get())
        done, _pending = await wait(
            [get_task, disconnected_event], return_when=FIRST_COMPLETED
        )

        if disconnected_event in done:
            get_task.cancel()
            return NONE()

        assert get_task in done
        disconnected_event.cancel()
        if self._closed.is_set() and self._queue.empty():
            self._disconnected.set()
        return Some(get_task.result())

    def try_recv(self) -> Result[T, TryRecvErrorEmpty | TryRecvErrorDisconnected]:
        """
        Tries to receive the next value for this receiver.

        This method returns the `Empty` error if the channel is currently empty,
        but there are still outstanding senders.

        This method returns the `Disconnected` error if the channel is currently empty,
        and there are no outstanding senders.
        """

        if self._disconnected.is_set():
            # Not actually a type error but Pylance (in VS Code) thinks it is
            return Err(TryRecvErrorDisconnected)  # type: ignore

        if self._queue.empty():
            # Not actually a type error but Pylance (in VS Code) thinks it is
            return Err(TryRecvErrorEmpty)  # type: ignore

        # This cannot raise an exception given the guards above
        return Ok(self._queue.get_nowait())

    async def __aiter__(self) -> AsyncIterator[T]:
        while True:
            received = await self.recv()
            match received.to_matchable():
                case MatchesSome(value):
                    yield value
                case MatchesNone():
                    return

    def close(self):
        """
        Closes the receiving half of a channel without dropping it.

        This prevents any further messages from being sent on the channel
        while still enabling the receiver to drain messages that are buffered.

        To guarantee that no messages are dropped, after calling `close()`,
        `recv()` must be called until `NONE()` is returned.
        """

        self._closed.set()
        if self._queue.empty():
            self._disconnected.set()

    def __del__(self):
        self.close()


def bounded_lifo_channel(buffer: int) -> tuple[Sender[T], Receiver[T]]:
    """
    Creates a bounded channel for communicating between asynchronous tasks with backpressure.

    The channel will buffer up to the provided number of messages.
    Once the buffer is full, attempts to send new messages will wait
    until a message is received from the channel.
    The provided buffer capacity must be at least 1.

    All data sent on `Sender` will become available on `Receiver` in the same order as it was sent.

    References to the `Sender` can be implicitly copied to `send` to the same channel
    from multiple code locations. Likewise for the `Receiver` to receive from multiple locations.

    If the `Receiver` is disconnected while trying to `send`,
    the `send` method will return a `SendError`.

    Similarly, if `Sender` is disconnected while trying to `recv`,
    the `recv` method will return `NONE()`.
    """

    assert buffer > 0, "bounded channels need a positive integer buffer size"

    queue: LifoQueue[T] = LifoQueue(buffer)

    closed = Event()
    disconnected = Event()

    sender = Sender(queue, closed, disconnected)
    receiver = Receiver(queue, closed, disconnected)

    return (sender, receiver)
