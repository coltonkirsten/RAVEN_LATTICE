# browser node — mesh round-trip smoke

Sequence proven 2026-05-16:

1. Core restarted with the manifest carrying the new `browser` node +
   8 edges.
2. `browser_node.py` registered as `browser` (host-process mode; see
   README for Docker mode and `/tmp/browser_node/worker_status.md`
   for the deferred-Docker note).
3. Raven side issued a signed invocation to `browser.query`:
   ```
   {"task": "What is the population of Sacramento on Wikipedia?
             Return only the number.",
    "conversation_id": "smoke2"}
   ```
   → HTTP 202 from Core.
4. Browser node spawned Claude CLI with Playwright MCP + the local
   `lattice_mesh` MCP server. CLI navigated Wikipedia, snapshotted,
   extracted `524,943`, then called
   `mcp__lattice_mesh__mesh_raven_message` with the payload.
5. Core routed the new signed invocation to `raven.message`. Ephemeral
   raven listener saw:
   ```
   DELIVER to='raven.message' from='browser'
           payload={"message": "524,943"}
   ```

Wall-clock for steps 3–5: ~17s.

Browser-node log for the same invocation:
```
[browser] query from=raven conv=smoke2 task='What is the population…'
[browser] reply len=7 chars
          mesh_ok=['mcp__lattice_mesh__mesh_raven_message']
          expected='mcp__lattice_mesh__mesh_raven_message'
          replied_via_cli=True
```

`replied_via_cli=True` confirms the spawned CLI shipped the reply
itself — the node-side `mesh_invoke` fallback was not needed. Same
shape EDITH uses for its replies.
