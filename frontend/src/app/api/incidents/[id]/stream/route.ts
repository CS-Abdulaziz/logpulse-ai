import type { NextRequest } from 'next/server'
import http from 'node:http'

// ── Runtime hints ─────────────────────────────────────────────────────────────
// Force Node.js runtime (Edge has no `node:http`), disable all caching, and
// mark fully dynamic so Next never tries to pre-render, ISR, or memoise.
export const runtime    = 'nodejs'
export const dynamic    = 'force-dynamic'
export const fetchCache = 'force-no-store'
export const revalidate = 0

// ── SSE response headers ──────────────────────────────────────────────────────
// `no-transform` blocks proxy-level gzip which can buffer streams.
// `private` keeps the response out of shared caches.
// `X-Accel-Buffering: no` tells Nginx / sympathetic reverse proxies to flush
// each chunk instead of waiting until the buffer is "efficient".
const SSE_HEADERS: HeadersInit = {
  'Content-Type':      'text/event-stream',
  'Cache-Control':     'no-cache, no-transform, private',
  'Connection':        'keep-alive',
  'X-Accel-Buffering': 'no',
}

/**
 * GET /api/incidents/[id]/stream — raw-socket SSE pass-through to FastAPI.
 *
 * Why this file uses `node:http` instead of global `fetch`:
 *   Node 18+ ships `fetch` powered by undici, which maintains its own
 *   internal receive buffer.  In flowing mode it can hold several KB of
 *   stream body before resolving the next `read()` on the WHATWG
 *   ReadableStream returned by `Response.body.getReader()`.  For SSE
 *   chunks of 100–500 bytes that arrive once per LangGraph stage, this
 *   shows up in the browser as "connection opens, then no events for 60 s,
 *   then everything arrives at once when the pipeline closes the stream".
 *
 *   The raw `node:http` module sits one layer below undici and exposes the
 *   raw socket events.  `res.on('data', chunk => ...)` is fired the moment
 *   each TCP segment arrives — no batching, no buffering, no fetch wrapper.
 *   Each chunk is enqueued straight into the ReadableStream that Next.js
 *   uses for the response, which the browser's EventSource consumes
 *   chunk-by-chunk via HTTP/1.1 chunked transfer encoding.
 *
 * Flow:
 *   browser EventSource
 *     → this Route Handler (filesystem priority over next.config rewrites)
 *     → http.get() raw socket to FastAPI on :8000
 *     → res.on('data') fires per TCP segment
 *     → controller.enqueue() → Next.js response writer → browser
 */
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params

  const encoder = new TextEncoder()

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      let closed = false
      const safeClose = () => {
        if (closed) return
        closed = true
        try { controller.close() } catch { /* already closed */ }
      }
      const safeEnqueue = (chunk: Uint8Array) => {
        if (closed) return
        try { controller.enqueue(chunk) } catch { closed = true }
      }

      // ── 1. Open the raw HTTP connection to FastAPI ─────────────────────
      const upstreamReq = http.get(
        {
          hostname: 'localhost',
          port:     8000,
          path:     `/api/incidents/${id}/stream`,
          headers: {
            Accept:          'text/event-stream',
            'Cache-Control': 'no-cache',
            Connection:      'keep-alive',
          },
        },
        (res) => {
          // ── 2. Validate upstream response ────────────────────────────
          if (res.statusCode !== 200) {
            safeEnqueue(encoder.encode(
              `event: error\ndata: upstream HTTP ${res.statusCode}\n\n`,
            ))
            res.resume()         // drain to allow socket close
            safeClose()
            return
          }

          // ── 3. Flush each TCP chunk synchronously ───────────────────
          // res is in flowing mode as soon as a 'data' listener is added,
          // so chunks arrive as fast as the kernel delivers them.  Node's
          // `Buffer` is a Uint8Array subclass — pass it through directly.
          res.on('data', (chunk: Buffer) => {
            safeEnqueue(chunk)
          })

          res.on('end', () => {
            safeClose()
          })

          res.on('error', () => {
            safeClose()
          })
        },
      )

      // ── 4. Connection-level error (eg. ECONNREFUSED) ───────────────────
      upstreamReq.on('error', () => {
        safeEnqueue(encoder.encode(
          'event: error\ndata: backend unreachable on port 8000\n\n',
        ))
        safeClose()
      })

      // ── 5. Browser disconnect propagation ──────────────────────────────
      // When the EventSource closes (page nav, tab close, .close() call),
      // req.signal aborts.  Destroy the upstream socket so FastAPI's
      // request handler sees the disconnect and stops polling for events.
      const onAbort = () => {
        try { upstreamReq.destroy() } catch { /* */ }
        safeClose()
      }
      if (req.signal.aborted) {
        onAbort()
      } else {
        req.signal.addEventListener('abort', onAbort, { once: true })
      }
    },

    // Called when the consumer (Next.js response writer) cancels the stream.
    // Same cleanup: kill the upstream socket.
    cancel() {
      // No-op — the abort listener above also fires in this path because
      // req.signal aborts when the response is cancelled.
    },
  })

  return new Response(stream, {
    status:  200,
    headers: SSE_HEADERS,
  })
}
