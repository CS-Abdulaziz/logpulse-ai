import type { NextRequest } from 'next/server'
import http from 'node:http'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'
export const fetchCache = 'force-no-store'
export const revalidate = 0

const SSE_HEADERS: HeadersInit = {
  'Content-Type':      'text/event-stream',
  'Cache-Control':     'no-cache, no-transform, private',
  'Connection':        'keep-alive',
  'X-Accel-Buffering': 'no',
}

export async function GET(req: NextRequest) {
  const encoder = new TextEncoder()
  const search = req.nextUrl.search || ''

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

      const upstreamReq = http.get(
        {
          hostname: 'localhost',
          port:     8000,
          path:     `/api/telemetry/stream${search}`,
          headers: {
            Accept:          'text/event-stream',
            'Cache-Control': 'no-cache',
            Connection:      'keep-alive',
          },
        },
        (res) => {
          if (res.statusCode !== 200) {
            safeEnqueue(encoder.encode(
              `event: telemetry_error\ndata: ${JSON.stringify({ message: `upstream HTTP ${res.statusCode}` })}\n\n`,
            ))
            res.resume()
            safeClose()
            return
          }

          res.on('data', (chunk: Buffer) => {
            safeEnqueue(chunk)
          })
          res.on('end', safeClose)
          res.on('error', safeClose)
        },
      )

      upstreamReq.on('error', () => {
        safeEnqueue(encoder.encode(
          `event: telemetry_error\ndata: ${JSON.stringify({ message: 'backend unreachable on port 8000' })}\n\n`,
        ))
        safeClose()
      })

      const onAbort = () => {
        try { upstreamReq.destroy() } catch { /* noop */ }
        safeClose()
      }
      if (req.signal.aborted) {
        onAbort()
      } else {
        req.signal.addEventListener('abort', onAbort, { once: true })
      }
    },
    cancel() {
      // req.signal aborts on response cancellation and destroys upstream.
    },
  })

  return new Response(stream, {
    status:  200,
    headers: SSE_HEADERS,
  })
}
