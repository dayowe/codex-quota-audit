"""Bounded streaming helpers shared by workflow extractors."""
import json


class Record(bytes):
    """One log line, with lazy decode/lowercase shared between consumers."""
    def lower(self):
        if not hasattr(self, "_lower"):
            self._lower = super().lower()
        return self._lower


def loads(raw):
    if not isinstance(raw, Record):
        return json.loads(raw)
    if not hasattr(raw, "_decoded"):
        raw._decoded = json.loads(raw)
    return raw._decoded


def response_timing_hint(raw):
    low = raw.lower()
    return (any(value in raw for value in (
        b'"task_started"', b'"task_complete"', b'"turn_context"',
        b'"token_usage_record"', b'"item_completed"', b'"function_call"',
        b'"custom_tool_call"', b'"tool_call"', b'"model"', b'"effort"', b'"reasoning_effort"'))
        or b'call_output' in low or b'call_result' in low)


def finish(consumer):
    try:
        consumer.send(None)
    except StopIteration as done:
        return done.value
    raise RuntimeError("Extractor did not finish")


def read_consumer(path, consumer, empty):
    try:
        fh = open(path, "rb")
    except OSError:
        consumer.close()
        return empty
    try:
        next(consumer)
        with fh:
            for raw in fh:
                consumer.send(Record(raw))
        return finish(consumer)
    finally:
        consumer.close()
