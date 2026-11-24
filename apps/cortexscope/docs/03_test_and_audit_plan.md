## Test & Audit Matrix
- `test_async_ndjson_chunk_timing`: Simulate network latency with mocked stream chunks to verify async writer handles delays without blocking the event loop.
- `test_wraparound_thermal_counter`: Ensure Linux RAPL energy counters correctly handle integer overflow when exceeding 32-bit limits during high-load simulations.
- `test_xss_payload_escape`: Validate that FastAPI request bodies containing `<script>` tags are sanitized before being proxied to downstream services.
- `test_proxy_abort_cleanup`: Confirm that abruptly terminating an active NDJSON stream flushes pending buffers and releases file descriptors immediately.
- `test_thermal_threshold_trigger`: Verify the system triggers thermal throttling logic when RAPL data indicates CPU temperature exceeds safe operating limits.
- `test_sqlite_connection_pool`: Ensure SQLite connections are properly closed after integration tests

