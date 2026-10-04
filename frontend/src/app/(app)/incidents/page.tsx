'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { AlertTriangle, ChevronRight, Loader2 } from 'lucide-react'
import { SeverityBadge } from '@/components/ui/SeverityBadge'
import { listIncidents } from '@/lib/api'
import type { Incident } from '@/lib/api'
import type { Severity } from '@/lib/types'

function deriveTitle(inc: Incident): string {
  if (inc.diagnosis) {
    const d = inc.diagnosis as { root_cause?: string; summary?: string }
    if (d.root_cause) return d.root_cause.slice(0, 60)
    if (d.summary)    return d.summary.slice(0, 60)
  }
  if (inc.classification) {
    const c = inc.classification as { category?: string; subcategory?: string }
    if (c.category) return `${c.category}${c.subcategory ? ` — ${c.subcategory}` : ''} incident`
  }
  return inc.input_log?.slice(0, 60) ?? `Incident ${inc.id}`
}

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

export default function IncidentsPage() {
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [loading,   setLoading]   = useState(true)

  useEffect(() => {
    listIncidents()
      .then(setIncidents)
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="p-6">
      <h1 className="font-sans text-lg font-semibold text-white mb-5">Incidents</h1>

      {loading ? (
        <div className="flex items-center gap-2 text-white/40 py-8">
          <Loader2 size={16} className="animate-spin" />
          <span className="font-mono text-xs">Loading incidents...</span>
        </div>
      ) : incidents.length === 0 ? (
        <div className="font-mono text-xs text-white/30 py-12 text-center border border-[#222222]">
          No incidents found. Submit a log from the dashboard to get started.
        </div>
      ) : (
        <div className="border border-[#222222]">
          <div className="grid grid-cols-[1fr_100px_180px_160px_40px] gap-0 border-b border-[#222222] px-4 py-2">
            {['Incident', 'Severity', 'Service', 'Status', ''].map((h) => (
              <span key={h} className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/40">
                {h}
              </span>
            ))}
          </div>
          {incidents.map((inc) => (
            <Link
              key={inc.id}
              href={`/incidents/${inc.id}`}
              className="grid grid-cols-[1fr_100px_180px_160px_40px] gap-0 items-center px-4 py-3 border-b border-[#1a1a1a] hover:bg-[#121212] transition-colors group"
            >
              <div className="flex items-center gap-2">
                <AlertTriangle size={13} strokeWidth={1.5} className="text-white/30 shrink-0" />
                <span className="font-sans text-sm text-white/80 truncate">{deriveTitle(inc)}</span>
              </div>
              <SeverityBadge level={deriveSeverity(inc)} />
              <span className="font-mono text-xs text-white/50 truncate">{deriveService(inc)}</span>
              <span className="font-mono text-xs text-white/50">{STATUS_LABEL[inc.status] ?? inc.status}</span>
              <ChevronRight size={14} className="text-white/20 group-hover:text-white/60 transition-colors" />
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
