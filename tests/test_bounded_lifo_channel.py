from asyncio import create_task, gather, sleep
from itertools import count
from time import time

import pytest

from option_and_result import MatchesErr, MatchesOk
from bounded_lifo_channel import Receiver, Sender, bounded_lifo_channel


@pytest.mark.asyncio
async def test_fill_buffer_then_retrieve_all():
    (sender, receiver) = bounded_lifo_channel(8)

    expected_produced_values = [1, 2, 3, 4, 5, 6, 7, 8]

    actual_produced_values = []

    for i in count(1):
        res = sender.try_send(i)

        if res.is_err():
            assert i > 8
            break
        else:
            assert i <= 8
            actual_produced_values.append(i)

    assert actual_produced_values == expected_produced_values

    expected_received_values = [8, 7, 6, 5, 4, 3, 2, 1]

    actual_received_values = []

    i = 0
    while True:
        i += 1

        res = receiver.try_recv()

        if res.is_err():
            assert i > 8
            break
        else:
            assert i <= 8
            value = res.unwrap()
            actual_received_values.append(value)

    assert actual_received_values == expected_received_values


async def producer_finishes_first(sender: Sender[int]):
    value = 3
    while True:
        value += 7

        if value >= 100:
            break

        res = await sender.send(value)

        match res.to_matchable():
            case MatchesErr(send_error):
                assert (
                    False
                ), f"channel was closed before trying to send {send_error.value}"


async def consumer_lags_behind_then_grabs_them_all(receiver: Receiver[int]):
    await sleep(1.0)

    started = time()
    actual_values = []
    async for value in receiver:
        actual_values.append(value)

    ended = time()
    elapsed = ended - started

    expected_values = [94, 87, 80, 73, 66, 59, 52, 45, 38, 31, 24, 17, 10]

    assert actual_values == expected_values
    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_consumer_lags_behind_then_grabs_them_all():
    (sender, receiver) = bounded_lifo_channel(32)

    producer_task = create_task(producer_finishes_first(sender))
    consumer_task = create_task(consumer_lags_behind_then_grabs_them_all(receiver))

    # Remove extra references so RAII can work correctly
    del sender
    del receiver

    await gather(producer_task, consumer_task)


CONSUMER_FINISHES_FIRST_EXPECTED_VALUES = [
    99,
    98,
    97,
    96,
    95,
    94,
    93,
    92,
    91,
    90,
    89,
    88,
    87,
    86,
    85,
    84,
    83,
    82,
    81,
    80,
]
CONSUMER_FINISHES_FIRST_BUFFER = 16


async def producer_is_slower(sender: Sender[int]):
    started = time()
    actual_values = []
    sleep_time = 0.05

    decrement = 1
    value = 100
    while True:
        value -= decrement

        await sleep(sleep_time)

        res = await sender.send(value)

        match res.to_matchable():
            case MatchesErr(send_error):
                assert (
                    send_error.value
                    == CONSUMER_FINISHES_FIRST_EXPECTED_VALUES[-1] - decrement
                )
                break
            case MatchesOk(None):
                actual_values.append(value)
    actual_length = len(actual_values)
    ended = time()
    elapsed = ended - started

    expected_length = len(CONSUMER_FINISHES_FIRST_EXPECTED_VALUES)

    assert elapsed > sleep_time * actual_length
    assert elapsed > sleep_time * expected_length

    assert actual_values == CONSUMER_FINISHES_FIRST_EXPECTED_VALUES


async def consumer_finishes_first(receiver: Receiver[int]):
    expected_length = len(CONSUMER_FINISHES_FIRST_EXPECTED_VALUES)

    started = time()
    actual_values = []
    sleep_time = 0.02
    i = 0
    async for value in receiver:
        actual_values.append(value)
        i += 1
        if i == expected_length:
            break
        await sleep(sleep_time)
    # Ensure references are dropped for proper RAII behavior
    del receiver
    # Probably optional given the rest of this function is synchronous anyway:
    actual_length = len(actual_values)
    ended = time()

    elapsed = ended - started

    assert elapsed > sleep_time * actual_length
    assert elapsed > sleep_time * expected_length

    assert actual_values == CONSUMER_FINISHES_FIRST_EXPECTED_VALUES


@pytest.mark.asyncio
async def test_consumer_finishes_first():
    (sender, receiver) = bounded_lifo_channel(CONSUMER_FINISHES_FIRST_BUFFER)

    producer_task = create_task(producer_is_slower(sender))
    consumer_task = create_task(consumer_finishes_first(receiver))

    # Remove extra references so RAII can work correctly
    del sender
    del receiver

    await gather(producer_task, consumer_task)
