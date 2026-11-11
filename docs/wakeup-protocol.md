# Wakeup plugin protocol

Wakeup plugins are local processes started by the Loong **client**. They use
newline-delimited JSON-RPC 2.0 on standard input/output; stdout is reserved for
protocol frames and stderr is diagnostic-only. This is not MCP.

## Lifecycle

1. The client starts the configured `command` and `args` with the configured
   workspace as its current directory.
2. The client sends `initialize`:

   ```json
   {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"version":"1","max_frame_size":1048576,"image_paths":true}}
   ```

3. The plugin returns a JSON-RPC success response with the same ID. The
   handshake has a five-second deadline.
4. The plugin sends `wakeup` requests. The client returns `accepted` after the
   daemon admits the turn to its in-memory mailbox.

Frames are limited to 1 MiB. Invalid frames or parameters produce JSON-RPC
errors; plugins must not write log text to stdout.

## `wakeup`

```json
{"jsonrpc":"2.0","id":2,"method":"wakeup","params":{"event_id":"evt-123","session_key":"customer-42","prompt":"Check the deployment status.","context":{"reply_to":"message-9"}}}
```

- `event_id` identifies the originating event.
- `prompt` is required and treated as untrusted external data.
- `session_key` is required when the configured `session_scope` is `key`.
- `context` is optional JSON data, included in the agent turn as untrusted
  context.

## Optional images

Clients advertising `image_paths: true` in `initialize.params` accept an optional
`images` array in wakeup parameters. Older clients ignore unknown fields; plugins
must check this capability rather than assuming that an image was delivered.

```json
{"jsonrpc":"2.0","id":3,"method":"wakeup","params":{"event_id":"photo-1","session_key":"chat-42","prompt":"Describe this image.","images":[{"path":"state/attachments/event-1/photo.png","mime_type":"image/png"}]}}
```

Each image has a required `path` and optional `mime_type`. Paths are absolute or
relative to the declared workspace. The client resolves symlinks, rejects paths
outside the workspace, and checks regular files, signatures and MIME types.
Up to four images are accepted with an aggregate limit of 20 MiB; supported
formats are JPEG, PNG, GIF and WebP. The prompt remains required. The daemon
receives explicit resolved image references through its existing turn API;
no daemon configuration discovery or base64 data in stdio frames is needed.
The daemon/provider must have access to those files for the life of the turn
(and later history reuse); the feature assumes a shared local filesystem.
Files are not snapshotted against later local changes. Model vision support is
required and format restrictions can vary by provider.

Other attachments can be downloaded by the plugin and described in `context`
with workspace-local paths, names and sizes. They are not automatically read,
extracted or executed. All attachment contents and metadata are untrusted.
Plugins own download/storage bounds and cleanup. Do not delete image files while
referencing sessions still need them; ACK is not completion.

## Acknowledgment

A successful reply is:

```json
{"jsonrpc":"2.0","id":2,"result":{"status":"accepted"}}
```

`accepted` is not execution completion or durable delivery. A failed request
may have an ambiguous result if the daemon received it before a client timeout;
there is no automatic retry or exactly-once guarantee.
