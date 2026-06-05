'use client'

import { useEffect, useRef, useState } from 'react'
import { Filter, ChevronRight } from 'lucide-react'
import { useRouter } from 'next/navigation'
import { LogRow } from '@/components/ui/LogRow'
import { StatusDot } from '@/components/ui/StatusDot'
import { useTelemetryStream } from '@/hooks/useTelemetryStream'
import { submitIncident } from '@/lib/api'
import type { HeatmapCell } from '@/hooks/useTelemetryStream'

function formatAgo(min: number) {
  if (min < 60) return `-${min}m`
  return `-${Math.floor(min / 60)}h`
}

export default function StreamPage() {
  const router = useRouter()
  const feedRef = useRef<HTMLDivElement>(null)
  const [submitting, setSubmitting] = useState(false)

  const {
    logs,
    detection,
    heatmap,
    signatures,
    errorConcentration,
    connected,
    complete,
    error,
  } = useTelemetryStream('oom_critical')

  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: 'smooth' })
  }, [logs])

  async function handleInspectWorkflow() {
    if (!detection?.trigger_log || submitting) return
    setSubmitting(true)
    try {
      const incident = await submitIncident(detection.trigger_log, detection.scenario || 'oom_critical')
      router.push(`/incidents/${incident.id}`)
    } catch (err) {
      console.error('Failed to submit telemetry incident:', err)
      setSubmitting(false)
    }
  }

  const status = detection?.status ?? (connected ? 'live' : 'offline')
  const statusLabel = detection
    ? detection.status === 'critical' ? 'Critical State' : 'Warning State'
    : connected ? 'Awaiting Signal' : 'Offline'
  const confidence = detection?.confidence ?? 0

  return (
    <div className="h-[calc(100vh-48px)] flex overflow-hidden">

      <div className="flex flex-col border-r border-[#222222]" style={{ width: '28%' }}>
        <div className="flex items-center justify-between px-3 py-2.5 border-b border-[#222222] shrink-0">
          <div className="flex items-center gap-2">
            <StatusDot status={connected ? 'live' : 'offline'} pulse={connected} size={8} />
            <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-white/80">
              Live Stream: cluster-01
            </span>
          </div>
          <button className="text-white/30 hover:text-white/70 transition-colors">
            <Filter size={13} />
          </button>
        </div>
        <div ref={feedRef} className="flex-1 overflow-y-auto py-1">
          {logs.length > 0 ? logs.map((entry) => (
            <LogRow
              key={entry.id}
              timestamp={entry.timestamp}
              level={entry.level}
              message={entry.message}
            />
          )) : (
            <div className="px-3 py-3 font-mono text-xs text-white/30">
              {error ?? 'Waiting for backend telemetry...'}
            </div>
          )}
        </div>
      </div>

      <div className="flex flex-col flex-1 border-r border-[#222222]">
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-[#222222] shrink-0">
          <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-white/60">
            Detection Theater
          </span>
          <div className="flex items-center gap-2">
            <StatusDot status={status} pulse={status !== 'offline'} size={8} />
            <span className={`font-mono text-[10px] font-semibold uppercase tracking-[0.08em] ${
              status === 'critical' ? 'text-[#EF4444]' : status === 'warning' ? 'text-[#F59E0B]' : 'text-[#10B981]'
            }`}>
              {statusLabel}
            </span>
          </div>
        </div>

        <div className="flex-1 relative overflow-hidden bg-[#0a0a0a] p-2">
          <HeatmapGrid cells={heatmap} />
        </div>

        <div className="px-4 py-2 border-t border-[#222222] shrink-0">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <span className="w-1.5 h-1.5 bg-[#F59E0B] rounded-full animate-pulse" />
              <span className="font-mono text-[10px] text-white/50">
                {connected ? 'Backend Telemetry Pipeline Active' : 'Connecting to Backend Telemetry'}
              </span>
            </div>
            <span className="font-mono text-[10px] text-white/30">
              {complete ? 'Scenario stream complete' : detection ? 'Live detection received' : 'Awaiting qualifying log'}
            </span>
          </div>

          <div className="bg-[#121212] border border-[#222222] p-4">
            <h3 className="font-sans text-base font-semibold text-white mb-2">
              {detection?.title ?? 'Awaiting Detection'}
            </h3>
            <p className="font-sans text-xs text-white/55 leading-5 mb-3">
              {detection?.summary ?? 'The backend telemetry stream is connected. Logs will appear as the ingestion harness emits cluster events that pass through the Python pipeline filters.'}
            </p>
            <div className="flex items-center gap-6 mb-4">
              <div>
                <p className="font-mono text-[10px] text-white/30 uppercase tracking-[0.05em]">Confidence</p>
                <p className="font-mono text-sm font-semibold text-[#10B981]">
                  {detection ? `${confidence.toFixed(1)}%` : '--'}
                </p>
              </div>
              <div>
                <p className="font-mono text-[10px] text-white/30 uppercase tracking-[0.05em]">Impact Area</p>
                <p className="font-mono text-sm font-semibold text-[#EF4444]">
                  {detection?.impact_area ?? '--'}
                </p>
              </div>
            </div>
            <button
              onClick={handleInspectWorkflow}
              disabled={!detection?.trigger_log || submitting}
              className="w-full h-9 bg-[#8B5CF6] hover:bg-[#7c3aed] disabled:opacity-40 disabled:cursor-not-allowed text-white font-sans text-xs font-semibold uppercase tracking-wide flex items-center justify-center gap-2 transition-colors"
            >
              {submitting ? 'Starting Workflow...' : 'Inspect Root-Cause Workflow'}
              <ChevronRight size={14} />
            </button>
          </div>
        </div>
      </div>

      <div className="flex flex-col" style={{ width: '22%' }}>
        <div className="px-4 py-2.5 border-b border-[#222222] shrink-0">
          <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-white/40">
            AI Engine Telemetry
          </span>
        </div>

        <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-5">
          <div className="flex flex-col items-center gap-2">
            <p className="font-mono text-[10px] text-white/40 uppercase tracking-[0.05em] self-start">
              Inference Confidence Level
            </p>
            <div className="relative w-24 h-24">
              <GaugeCircle value={Math.round(confidence)} />
            </div>
            <div className="flex items-center gap-1.5">
              <StatusDot status={error ? 'warning' : 'healthy'} size={6} />
              <span className={`font-mono text-[10px] ${error ? 'text-[#F59E0B]' : 'text-[#10B981]'}`}>
                {error ?? 'Model Drift Nominal'}
              </span>
            </div>
          </div>

          <div>
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/40 mb-3">
              Error Concentration (1h)
            </p>
            <div className="flex flex-col gap-2.5">
              {errorConcentration.length > 0 ? errorConcentration.map((ec) => (
                <div key={ec.service}>
                  <div className="flex items-center justify-between mb-1">
                    <span className="font-mono text-[10px] text-white/60">{ec.service}</span>
                    <span className="font-mono text-[10px] font-semibold text-white">{ec.percent}%</span>
                  </div>
                  <div className="h-1 bg-[#1a1a1a]">
                    <div className="h-full bg-[#8B5CF6] transition-all" style={{ width: `${ec.percent}%` }} />
                  </div>
                </div>
              )) : (
                <span className="font-mono text-xs text-white/30">Awaiting non-info telemetry.</span>
              )}
            </div>
          </div>

          <div>
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/40 mb-3">
              Recent Signatures
            </p>
            <div className="flex flex-col gap-0">
              {signatures.length > 0 ? signatures.map((sig, i) => (
                <div
                  key={sig.id}
                  className={`py-2.5 flex flex-col gap-0.5 ${i < signatures.length - 1 ? 'border-b border-[#1a1a1a]' : ''}`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[10px] text-[#8B5CF6]">{sig.id}</span>
                    <span className="font-mono text-[10px] text-white/30">{formatAgo(sig.minutesAgo)}</span>
                  </div>
                  <span className="font-mono text-xs text-white/70">{sig.label}</span>
                </div>
              )) : (
                <span className="font-mono text-xs text-white/30">No accepted signatures yet.</span>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function HeatmapGrid({ cells }: { cells: HeatmapCell[] }) {
  const cols = 20
  const rows = 14
  const displayCells = cells.length > 0 ? cells : buildEmptyCells(cols, rows)

  return (
    <div
      className="w-full h-full grid"
      style={{
        gridTemplateColumns: `repeat(${cols}, 1fr)`,
        gridTemplateRows:    `repeat(${rows}, 1fr)`,
        gap: '2px',
      }}
    >
      {displayCells.map((cell) => (
        <div
          key={cell.id}
          className="transition-colors"
          style={{ background: heatColor(cell) }}
        />
      ))}
    </div>
  )
}

function buildEmptyCells(cols: number, rows: number): HeatmapCell[] {
  return Array.from({ length: rows * cols }, (_, idx) => {
    const row = Math.floor(idx / cols)
    const col = idx % cols
    return {
      id: `empty-${row}-${col}`,
      row,
      col,
      intensity: 0.02,
      state: 'normal',
    }
  })
}

function heatColor(cell: HeatmapCell) {
  const alpha = Math.max(0.02, Math.min(cell.intensity, 0.85))
  if (cell.state === 'critical') return `rgba(239,68,68,${alpha})`
  if (cell.state === 'warning') return `rgba(245,158,11,${alpha})`
  return `rgba(255,255,255,${alpha})`
}

function GaugeCircle({ value }: { value: number }) {
  const r = 38
  const circ = 2 * Math.PI * r
  const safeValue = Math.max(0, Math.min(value, 100))
  const dash = (safeValue / 100) * circ
  return (
    <svg viewBox="0 0 96 96" className="w-full h-full -rotate-90">
      <circle cx="48" cy="48" r={r} fill="none" stroke="#1a1a1a" strokeWidth="6" />
      <circle
        cx="48" cy="48" r={r} fill="none"
        stroke="#8B5CF6" strokeWidth="6"
        strokeDasharray={`${dash} ${circ - dash}`}
        strokeLinecap="butt"
      />
      <text
        x="48" y="53"
        textAnchor="middle"
        style={{
          fill: 'white',
          fontSize: 18,
          fontWeight: 600,
          fontFamily: 'var(--font-jetbrains-mono)',
          transform: 'rotate(90deg)',
          transformOrigin: '48px 48px',
        }}
      >
        {safeValue || '--'}
      </text>
    </svg>
  )
}
