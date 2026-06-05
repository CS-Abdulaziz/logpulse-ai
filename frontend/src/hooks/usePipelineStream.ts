'use client'

import { useCallback, useEffect, useState } from 'react'
import { approveIncident, rejectIncident, USE_MOCK } from '@/lib/api'
import type { PipelineStep, StreamEntry, AgentType } from '@/lib/types'

// ── SSE event payload shapes (mirrors api/routers/incidents.py) ───────────────

export interface StagePayload {
  name:         string
  status:       'running' | 'done' | 'error'
  result?:      Record<string, unknown>
  duration_ms?: number
  error?:       string
}

export interface AwaitingApprovalPayload {
  commands:    string[]
  risk_level:  string
  incident_id: string
}

export interface CommandResultPayload {
  command:         string
  success:         boolean
  exit_code:       number
  stdout:          string
  state_mutations: string[]
  error?:          string
}

export interface CompletePayload {
  incident_id: string
  duration_ms: number
  report_path: string | null
  status:      string
}

// ── Stage name → display label (must match api/adapters/pipeline.py _stage() names) ──

const STAGE_LABELS: Record<string, string> = {
  ingestion:       'Ingestion',
  log_parsing:     'Log Parsing & Structuring',
  classification:  'Classification',
  rag:             'Semantic Search (RAG)',
  diagnosis:       'Root Cause Synthesis',
  solution:        'Remediation Drafting',
  risk_assessment: 'Security Assessment',
  execution:       'Execution',
  complete:        'Done',
}

const STAGE_AGENT: Record<string, AgentType> = {
  ingestion:       'INGEST',
  log_parsing:     'INGEST',
  classification:  'CLASSIFY',
  rag:             'RAG',
  diagnosis:       'DIAGNOSE',
  risk_assessment: 'RISK',
  solution:        'SYNTH',
  execution:       'SYNTH',
  complete:        'SYNTH',
}

// ── Mock data for USE_MOCK simulation ─────────────────────────────────────────

const MOCK_STAGE_CONTENT: Record<string, string> = {
  ingestion:       'Parsed incoming log payload. Extracting structured fields and metadata.',
  log_parsing:     'Tokenized log entries. Identified <span class="text-[#8B5CF6]">timestamp</span>, <span class="text-[#8B5CF6]">level</span>, <span class="text-[#8B5CF6]">message</span> fields.',
  classification:  'Signature match identified. Confidence score: <span class="text-[#10B981]">0.94</span>.',
  rag:             'Querying historical playbook DB for similar exception contexts. Found 3 matching patterns.',
  diagnosis:       'Root Cause Hypothesis: Identified cascading failure pattern correlated with recent resource exhaustion event.',
  solution:        'Generating remediation payload and command variants for human review...',
  risk_assessment: '<span class="text-[#EF4444]">SEVERITY: HIGH.</span> Cascading failures detected affecting downstream traffic. Requires immediate mitigation.',
}

const MOCK_STAGE_RESULTS: Record<string, Record<string, unknown>> = {
  classification:  { category: 'Memory', confidence: 0.94, severity: 'Critical', source: 'Qwen', cache_hit: false },
  rag:             { confidence: 0.88, playbook_found: true },
  diagnosis:       { root_cause: 'Auth service failing to acquire read lock on Redis cluster due to network partition', confidence: 0.94, source: 'diagnostic_agent' },
  risk_assessment: { risk_level: 'high', flagged_commands: [], safe_commands: ['kubectl rollout restart', 'kubectl scale'], message: 'RISK LEVEL: HIGH. Cascading failures detected.' },
  solution:        { commands: ['kubectl rollout restart deployment/worker', 'kubectl scale deployment/worker --replicas=3'], steps: ['Apply config-map patch', 'Restart auth-service pods', 'Monitor Redis connection pool'], explanation: 'Apply immediate restart to recover auth service.' },
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatStageContent(payload: StagePayload): string {
  const { name, status, result, error } = payload
  if (status === 'error') return `Error in ${STAGE_LABELS[name] ?? name}: ${error ?? 'unknown error'}`
  if (!result)            return `${STAGE_LABELS[name] ?? name} completed.`

  const r = result as Record<string, unknown>
  const parts: string[] = []

  if (typeof r.root_cause === 'string') {
    parts.push(r.root_cause)
  } else if (typeof r.summary === 'string') {
    parts.push(r.summary)
  } else if (typeof r.category === 'string') {
    const conf = typeof r.confidence === 'number'
      ? ` · Confidence: <span class="text-[#10B981]">${(r.confidence * 100).toFixed(0)}%</span>`
      : ''
    parts.push(`Category: <span class="text-[#8B5CF6]">${r.category}</span>${conf}`)
  }
  if (typeof r.risk_level === 'string') {
    const col = (r.risk_level === 'critical' || r.risk_level === 'high') ? '#EF4444' : '#F59E0B'
    parts.push(`Risk level: <span style="color:${col}">${r.risk_level.toUpperCase()}</span>`)
  }

  return parts.length > 0 ? parts.join(' · ') : `${STAGE_LABELS[name] ?? name} completed.`
}

function buildSteps(overrides: Record<string, 'done' | 'active' | 'pending' | 'error'>): PipelineStep[] {
  const order = [
    'ingestion', 'log_parsing', 'classification', 'rag',
    'diagnosis', 'solution', 'risk_assessment', 'execution', 'complete',
  ]
  return order.map((id) => ({
    id,
    label:  STAGE_LABELS[id] ?? id,
    status: overrides[id] ?? 'pending',
  }))
}

// ── Public interface ──────────────────────────────────────────────────────────

export interface PipelineStreamState {
  steps:            PipelineStep[]
  streamEntries:    StreamEntry[]
  /** Result payloads keyed by stage name, populated as each stage completes. */
  stageResults:     Record<string, Record<string, unknown>>
  awaitingApproval: AwaitingApprovalPayload | null
  commandResults:   CommandResultPayload[]
  complete:         CompletePayload | null
  error:            string | null
  approve:          () => Promise<void>
  reject:           () => Promise<void>
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function usePipelineStream(incidentId: string | null): PipelineStreamState {
  const [stepOverrides,    setStepOverrides]    = useState<Record<string, 'done' | 'active' | 'pending' | 'error'>>({})
  const [streamEntries,    setStreamEntries]    = useState<StreamEntry[]>([])
  const [stageResults,     setStageResults]     = useState<Record<string, Record<string, unknown>>>({})
  const [awaitingApproval, setAwaitingApproval] = useState<AwaitingApprovalPayload | null>(null)
  const [commandResults,   setCommandResults]   = useState<CommandResultPayload[]>([])
  const [complete,         setComplete]         = useState<CompletePayload | null>(null)
  const [error,            setError]            = useState<string | null>(null)

  const updateStep = useCallback((name: string, status: 'done' | 'active' | 'pending' | 'error') => {
    setStepOverrides((prev) => ({ ...prev, [name]: status }))
  }, [])

  const addEntry = useCallback((entry: StreamEntry) => {
    setStreamEntries((prev) => [...prev, entry])
  }, [])

  useEffect(() => {
    if (!incidentId) return

    // ── Mock simulation ────────────────────────────────────────────────────
    if (USE_MOCK) {
      const stages = ['ingestion', 'log_parsing', 'classification', 'rag', 'diagnosis', 'solution', 'risk_assessment']
      let i = 0

      const tick = () => {
        if (i >= stages.length) {
          setAwaitingApproval({
            commands:    ['kubectl rollout restart deployment/worker', 'kubectl scale deployment/worker --replicas=3'],
            risk_level:  'safe',
            incident_id: incidentId,
          })
          addEntry({
            id:          `mock-approval-${Date.now()}`,
            agent:       'SYNTH',
            content:     'Awaiting human approval for 2 command(s). Risk level: <span class="text-[#F59E0B]">SAFE</span>',
            timestamp:   new Date().toTimeString().slice(0, 8),
            highlighted: true,
          })
          updateStep('risk_assessment', 'done')
          return
        }
        const stage = stages[i]
        updateStep(stage, 'active')
        setTimeout(() => {
          updateStep(stage, 'done')
          addEntry({
            id:          `mock-${stage}-${i}`,
            agent:       STAGE_AGENT[stage] ?? 'SYNTH',
            content:     MOCK_STAGE_CONTENT[stage] ?? `${STAGE_LABELS[stage] ?? stage} completed.`,
            timestamp:   new Date().toTimeString().slice(0, 8),
            highlighted: stage === 'risk_assessment',
          })
          // Populate stageResults so the page can derive live data without a DB fetch
          const mockResult = MOCK_STAGE_RESULTS[stage]
          if (mockResult) {
            setStageResults((prev) => ({ ...prev, [stage]: mockResult }))
          }
          i++
          setTimeout(tick, 600)
        }, 900)
      }

      const t = setTimeout(tick, 400)
      return () => clearTimeout(t)
    }

    // ── Live: EventSource + named addEventListener ─────────────────────────
    //
    // The request hits src/app/api/incidents/[id]/stream/route.ts (Route Handler),
    // which bypasses the buffering rewrite proxy by passing the FastAPI response
    // body directly to the browser.  EventSource handles SSE framing and
    // keep-alive reconnects natively; each named listener maps 1-to-1 to the
    // event type the backend emits.

    const ts  = () => new Date().toTimeString().slice(0, 8)
    const url = `/api/incidents/${incidentId}/stream`
    const source = new EventSource(url)

    // Diagnostic: log connection-state transitions so runtime issues are
    // visible in the browser console (CONNECTING=0, OPEN=1, CLOSED=2).
    source.addEventListener('open', () => {
      // eslint-disable-next-line no-console
      console.info(`[SSE] connection open → ${url}`)
    })

    // Fallback listener: catches any event arriving WITHOUT an `event:` header
    // (i.e. a default "message" event).  The backend always emits named events,
    // so this should never fire — but if it does, it tells us the SSE framing
    // is wrong upstream, which is far more actionable than a silent UI freeze.
    source.addEventListener('message', (e: MessageEvent) => {
      // eslint-disable-next-line no-console
      console.warn('[SSE] received unnamed message event:', e.data)
      addEntry({
        id:        `unnamed-${Date.now()}`,
        agent:     'SYNTH',
        content:   `<span class="text-[#F59E0B]">[unnamed event]</span> ${String(e.data).slice(0, 200)}`,
        timestamp: ts(),
      })
    })

    // ── event: stage ──────────────────────────────────────────────────────
    // { name, status: "running"|"done"|"error", result?, duration_ms?, error? }
    source.addEventListener('stage', (e: MessageEvent) => {
      try {
        const p = JSON.parse(e.data as string) as StagePayload
        const agent: AgentType = STAGE_AGENT[p.name] ?? 'SYNTH'

        if (p.status === 'running') {
          updateStep(p.name, 'active')
          addEntry({
            id:        `${p.name}-run-${Date.now()}`,
            agent,
            content:   `Running ${STAGE_LABELS[p.name] ?? p.name}...`,
            timestamp: ts(),
          })
        } else if (p.status === 'done') {
          updateStep(p.name, 'done')
          // Store the result payload keyed by stage name so the page can derive
          // live UI state (confidence, steps, risk data) without a DB round-trip.
          if (p.result) {
            setStageResults((prev) => ({ ...prev, [p.name]: p.result as Record<string, unknown> }))
          }
          addEntry({
            id:          `${p.name}-done-${Date.now()}`,
            agent,
            content:     formatStageContent(p),
            timestamp:   ts(),
            highlighted: p.name === 'risk_assessment',
          })
        } else if (p.status === 'error') {
          updateStep(p.name, 'error')
          addEntry({
            id:        `${p.name}-err-${Date.now()}`,
            agent,
            content:   `<span class="text-[#EF4444]">Error in ${STAGE_LABELS[p.name] ?? p.name}: ${p.error ?? 'unknown'}</span>`,
            timestamp: ts(),
          })
        }
      } catch { /* ignore malformed event data */ }
    })

    // ── event: awaiting_approval ──────────────────────────────────────────
    // { commands: string[], risk_level: string, incident_id: string }
    source.addEventListener('awaiting_approval', (e: MessageEvent) => {
      try {
        const p = JSON.parse(e.data as string) as AwaitingApprovalPayload
        setAwaitingApproval(p)
        addEntry({
          id:          `approval-${Date.now()}`,
          agent:       'SYNTH',
          content:     `Awaiting human approval for ${p.commands.length} command(s). Risk level: <span class="text-[#F59E0B]">${p.risk_level.toUpperCase()}</span>`,
          timestamp:   ts(),
          highlighted: true,
        })
      } catch { /* */ }
    })

    // ── event: command_result ─────────────────────────────────────────────
    // { command, success, exit_code, stdout, state_mutations, error? }
    source.addEventListener('command_result', (e: MessageEvent) => {
      try {
        const p = JSON.parse(e.data as string) as CommandResultPayload
        setCommandResults((prev) => [...prev, p])
        addEntry({
          id:        `cmd-${Date.now()}`,
          agent:     'SYNTH',
          content:   p.success
            ? `<span class="text-[#10B981]">✓</span> Executed: <span class="text-[#8B5CF6]">${p.command}</span>`
            : `<span class="text-[#EF4444]">✗</span> Failed: <span class="text-[#8B5CF6]">${p.command}</span>`,
          timestamp: ts(),
        })
      } catch { /* */ }
    })

    // ── event: complete ───────────────────────────────────────────────────
    // { incident_id, duration_ms, report_path, status }
    // This is the signal for the page to trigger the final GET /api/incidents/{id}
    // fetch to hydrate the full record (risk, solution, diagnosis, etc.).
    source.addEventListener('complete', (e: MessageEvent) => {
      try {
        const p = JSON.parse(e.data as string) as CompletePayload
        setComplete(p)
        updateStep('complete', 'done')
        addEntry({
          id:        `complete-${Date.now()}`,
          agent:     'SYNTH',
          content:   `Pipeline complete in <span class="text-[#10B981]">${(p.duration_ms / 1000).toFixed(1)}s</span>. Status: <span class="text-[#10B981]">${p.status}</span>`,
          timestamp: ts(),
        })
        source.close()
      } catch { /* */ }
    })

    // Surface connection-state failures.  CLOSED=2 means the browser gave up
    // permanently (eg. the proxy never responded with 200 + text/event-stream).
    // CONNECTING=0 means an auto-reconnect is in flight — surface it so the
    // user sees a status instead of a silent freeze.
    source.onerror = () => {
      // eslint-disable-next-line no-console
      console.error(`[SSE] error — readyState=${source.readyState} (0=CONNECTING, 2=CLOSED)`)
      if (source.readyState === EventSource.CLOSED) {
        setError('SSE connection closed by the server. Check that the FastAPI backend is running on port 8000 and that the Next.js dev server has been restarted to register the route handler at src/app/api/incidents/[id]/stream/route.ts.')
      } else if (source.readyState === EventSource.CONNECTING) {
        setError('SSE connection lost — attempting to reconnect…')
      }
    }

    return () => source.close()
  }, [incidentId, updateStep, addEntry])

  const approve = useCallback(async () => {
    if (!incidentId) return
    if (!USE_MOCK) await approveIncident(incidentId)
    setAwaitingApproval(null)
    updateStep('execution', 'active')
  }, [incidentId, updateStep])

  const reject = useCallback(async () => {
    if (!incidentId) return
    if (!USE_MOCK) await rejectIncident(incidentId)
    setAwaitingApproval(null)
    updateStep('execution', 'done')
  }, [incidentId, updateStep])

  return {
    steps:            buildSteps(stepOverrides),
    streamEntries,
    stageResults,
    awaitingApproval,
    commandResults,
    complete,
    error,
    approve,
    reject,
  }
}
