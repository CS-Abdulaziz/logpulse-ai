'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { AlertTriangle, Download, Loader2 } from 'lucide-react'
import { DropZone } from '@/components/ui/DropZone'
import { SeverityBadge } from '@/components/ui/SeverityBadge'
import { StatusDot } from '@/components/ui/StatusDot'
import { listIncidents, submitIncident, getReportUrl } from '@/lib/api'
import type { Incident } from '@/lib/api'
import type { Severity } from '@/lib/types'

function deriveSeverity(inc: Incident): Severity {
  if (inc.risk) {
    const r = inc.risk as { risk_level?: string }
    if (r.risk_level === 'critical' || r.risk_level === 'high') return 'CRITICAL'
    if (r.risk_level === 'medium') return 'WARN'
    return 'INFO'
  }
  if (inc.status === 'failed') return 'CRITICAL'
  if (inc.status === 'awaiting_approval') return 'WARN'
  return 'INFO'
}

function deriveService(inc: Incident): string {
  if (inc.classification) {
    const c = inc.classification as { service?: string; category?: string }
    return c.service ?? c.category ?? 'unknown-service'
  }
  return 'unknown-service'
}

const STATUS_LABEL: Record<string, string> = {
  queued:            'Queued',
  running:           'Analyzing',
  awaiting_approval: 'Patch Proposed',
  executing:         'Executing',
  completed:         'Completed',
  failed:            'Failed',
  rejected:          'Rejected',
}

const ACTIVE_STATUSES = new Set(['running', 'awaiting_approval', 'executing'])

export default function ArchivePage() {
  const router = useRouter()
  const [processing,    setProcessing]    = useState(false)
  const [progress,      setProgress]      = useState(0)
  const [progressLabel, setProgressLabel] = useState('')
  const [incidents,     setIncidents]     = useState<Incident[]>([])
  const [loading,       setLoading]       = useState(true)

  useEffect(() => {
    listIncidents()
      .then(setIncidents)
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  async function handleDrop(files: File[]) {
    if (!files.length) return
    setProcessing(true)
    setProgress(0)

    let firstId: string | null = null
    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i]
        setProgressLabel(`Processing ${file.name} (${i + 1} of ${files.length})...`)
        const text     = await file.text()
        const incident = await submitIncident(text)
        if (!firstId) firstId = incident.id
        setProgress(Math.round(((i + 1) / files.length) * 100))
      }
    } catch (err) {
      console.error('Upload failed:', err)
    } finally {
      const updated = await listIncidents().catch(() => incidents)
      setIncidents(updated)
      setProcessing(false)
      if (firstId) router.push(`/incidents/${firstId}`)
    }
  }

  function handleDownload() {
    const incWithReport = incidents.find((i) => i.report_path)
    const target        = incWithReport ?? incidents[0]
    if (target) window.open(getReportUrl(target.id), '_blank')
  }

  const criticalCount = incidents.filter((i) => deriveSeverity(i) === 'CRITICAL').length

  return (
    <div className="p-6 flex flex-col gap-5">
      <div>
        <h1 className="font-sans text-lg font-semibold text-white">Forensic Post-Mortem (Batch Vector)</h1>
        <p className="font-sans text-sm text-white/45 mt-1">
          Upload log bundles for automated root-cause analysis and mitigation tracking.
        </p>
      </div>

      {/* Drop zone */}
      <DropZone
        accept={['.log', '.json', '.gz']}
        maxSizeGB={5}
        onDrop={handleDrop}
        className="bg-[#121212]"
      />

      {/* Progress bar */}
      {processing && (
        <div className="bg-[#121212] border border-[#222222] p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="font-mono text-xs text-white/60">{progressLabel}</span>
            <span className="font-mono text-xs font-semibold text-[#8B5CF6]">{progress}%</span>
          </div>
          <div className="h-1.5 bg-[#0a0a0a] w-full">
            <div
              className="h-full bg-[#8B5CF6] transition-all duration-500"
              style={{ width: `${progress}%` }}
            />
          </div>
        </div>
      )}

      {/* Results table */}
      <div className="bg-[#121212] border border-[#222222]">
        <div className="grid grid-cols-[180px_120px_1fr_1fr] border-b border-[#222222] px-4 py-2.5">
          {['Timestamp', 'Severity Tag', 'Target Microservice', 'AI Mitigation Status'].map((h) => (
            <span key={h} className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/40">
              {h}
            </span>
          ))}
        </div>

        {loading ? (
          <div className="flex items-center gap-2 px-4 py-6 text-white/40">
            <Loader2 size={14} className="animate-spin" />
            <span className="font-mono text-xs">Loading incidents...</span>
          </div>
        ) : incidents.length === 0 ? (
          <div className="font-mono text-xs text-white/30 py-8 text-center">
            No incidents yet. Upload a log file above.
          </div>
        ) : (
          incidents.map((inc) => {
            const isActive = ACTIVE_STATUSES.has(inc.status)
            return (
              <div
                key={inc.id}
                onClick={() => router.push(`/incidents/${inc.id}`)}
                className="grid grid-cols-[180px_120px_1fr_1fr] items-center px-4 py-3 border-b border-[#1a1a1a] last:border-0 hover:bg-[#161616] transition-colors cursor-pointer"
              >
                <span className="font-mono text-xs text-white/60">{formatTimestamp(inc.created_at)}</span>
                <SeverityBadge level={deriveSeverity(inc)} />
                <span className="font-mono text-xs text-white/70">{deriveService(inc)}</span>
                <div className="flex items-center gap-2">
                  {isActive && <StatusDot status="critical" pulse size={7} />}
                  <span className={`font-mono text-xs ${isActive ? 'text-[#F59E0B]' : 'text-white/50'}`}>
                    {STATUS_LABEL[inc.status] ?? inc.status}
                  </span>
                </div>
              </div>
            )
          })
        )}
      </div>

      {/* Footer summary */}
      <div className="bg-[#121212] border border-[#222222] p-5 flex items-center justify-between">
        <div>
          <p className="font-sans text-2xl font-bold text-white">
            {incidents.length} incident{incidents.length !== 1 ? 's' : ''} evaluated.
          </p>
          {criticalCount > 0 && (
            <div className="flex items-center gap-2 mt-1.5">
              <AlertTriangle size={13} className="text-[#EF4444]" strokeWidth={1.5} />
              <span className="font-sans text-sm text-[#EF4444]">
                {criticalCount} Critical Fault{criticalCount !== 1 ? 's' : ''} Isolated.
              </span>
            </div>
          )}
        </div>
        <button
          onClick={handleDownload}
          disabled={incidents.length === 0}
          className="flex items-center gap-2 px-5 h-10 bg-[#8B5CF6] hover:bg-[#7c3aed] disabled:opacity-40 disabled:cursor-not-allowed text-white font-sans text-sm font-semibold transition-colors"
        >
          <Download size={15} strokeWidth={1.5} />
          Download Full Forensic Post-Mortem Bundle
        </button>
      </div>
    </div>
  )
}

function formatTimestamp(iso: string) {
  return iso.replace('T', ' ').replace('Z', '').slice(0, 19)
}
