# Streaming Protocol

AgentNet does not stream provider tokens by itself. To support incremental UI rendering, use normal AgentNet messages with `kind="stream"` and one of these payload types:

- `stream_start`
- `stream_delta`
- `stream_end`
- `stream_error`

This keeps streaming inside the normal thread/message model while giving UIs a stable protocol to render partial output.

## Message Shape

All stream messages are standard AgentNet messages:

- same `thread_id`
- same logical reply target
- `kind="stream"`
- payload carries the stream event type

Recommended correlation fields:

- `thread_id`: required
- `stream_id`: required inside payload
- `trace_id`: reuse across the logical streamed response
- `parent_message_id`: set to the triggering user/request message when possible

## Payload Schemas

### `stream_start`

```json
{
  "type": "stream_start",
  "stream_id": "stream_01abc",
  "role": "assistant",
  "content_type": "text/plain",
  "started_at": "2026-03-22T12:00:00Z",
  "metadata": {}
}
```

### `stream_delta`

```json
{
  "type": "stream_delta",
  "stream_id": "stream_01abc",
  "delta": "Hello",
  "seq": 1,
  "role": "assistant",
  "content_type": "text/plain"
}
```

### `stream_end`

```json
{
  "type": "stream_end",
  "stream_id": "stream_01abc",
  "seq": 99,
  "text": "Hello world",
  "role": "assistant",
  "content_type": "text/plain",
  "finished_at": "2026-03-22T12:00:04Z",
  "metadata": {}
}
```

### `stream_error`

```json
{
  "type": "stream_error",
  "stream_id": "stream_01abc",
  "error": "provider timeout",
  "seq": 42,
  "finished_at": "2026-03-22T12:00:02Z",
  "metadata": {}
}
```

## UI Handling

The UI should maintain a stream buffer keyed by:

- `thread_id`
- `stream_id`

Handling rules:

1. On `stream_start`
- create a new empty buffer
- mark stream state as `in_progress`

2. On `stream_delta`
- append `delta`
- track `seq`
- if chunks arrive out of order, sort or ignore duplicates by `seq`

3. On `stream_end`
- mark stream complete
- if `text` is present, treat it as the authoritative final assembled text
- finalize the rendered message

4. On `stream_error`
- mark stream failed
- surface the error in UI

## Persistence Guidance

Recommended behavior:

- persist all stream events if you want full replay/debugging
- or persist events plus one final assembled message for simpler read views

AgentNet transport supports both, but the UI should treat `stream_end.text` as the final canonical output when present.

## Python SDK Helpers

Available on `AgentSDK` and `ThreadSession`:

- `send_stream_start(...)`
- `send_stream_delta(...)`
- `send_stream_end(...)`
- `send_stream_error(...)`

Parsers:

- `is_stream_event(...)`
- `parse_stream_start(...)`
- `parse_stream_delta(...)`
- `parse_stream_end(...)`
- `parse_stream_error(...)`

Example:

```python
stream_id = "stream_01abc"
thread = sdk.thread("chat_1", parent_message_id=incoming.message_id)

await thread.send_stream_start("@ui_agent", stream_id)
await thread.send_stream_delta("@ui_agent", stream_id, "Hel", seq=1)
await thread.send_stream_delta("@ui_agent", stream_id, "lo", seq=2)
await thread.send_stream_end("@ui_agent", stream_id, seq=3, text="Hello")
```

## Why This Design

This keeps streaming:

- thread-aware
- inspectable in registry/message history
- UI-friendly
- independent of any one LLM provider

AgentNet remains the network. The stream protocol is just a stable convention on top of it.
