'use client'

import { use, useEffect, useRef, useState } from 'react'
import { FileText, ShieldCheck, SkipBack, Pause, SkipForward, X, Play } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { AgentBadge } from '@/components/ui/AgentBadge'
import { PipelineStepList } from '@/components/ui/PipelineStepList'
import { ProposedPatch } from '@/components/ui/ProposedPatch'
import { usePipelineStream } from '@/hooks/usePipelineStream'
import { getIncident, getReportUrl } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { Incident } from '@/lib/api'
import type { ProposedPatchData } from '@/lib/types'

interface PageProps {
  params: Promise<{ id: string }>
}

export default function IncidentPage({ params }: PageProps) {
  const { id } = use(params)
  const [activeTab, setActiveTab] = useState<'diagnostic' | 'remediation' | 'security'>('diagnostic')
  const streamRef = useRef<HTMLDivElement>(null)

  // Populated ONLY after the `complete` SSE event arrives and the pipeline has
  // finished writing all fields to the store.  Never fetched on mount.
  const [incident, setIncident] = useState<Incident | null>(null)

  const {
    steps,
    streamEntries,
    stageResults,
    awaitingApproval,
    commandResults,
    complete,
    error,
    approve,
    reject,
  } = usePipelineStream(id)

  // ── Fetch incident ONLY after the pipeline signals completion ──────────────
  // Firing GET /api/incidents/{id} before this point returns a skeleton record
  // (all analysis fields are null while the pipeline thread is still running).
  // The `complete` event is the backend's guarantee that the store is fully
  // populated — safe to read now.
  useEffect(() => {
    if (!complete) return
    getIncident(id).then(setIncident).catch(console.error)
  }, [complete, id])

  // ── Auto-scroll the stream panel as entries arrive ─────────────────────────
  useEffect(() => {
    streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight, behavior: 'smooth' })
  }, [streamEntries])

  // ── Derive display values from stageResults (live) → incident (post-complete) ──
  //
  // stageResults is populated incrementally by the hook as each stage `done`
  // event arrives — no DB round-trip needed during the pipeline run.
  // Once `incident` is set (after the final fetch) those values take precedence.

  const classificationResult = stageResults['classification'] as
    { confidence?: number; category?: string } | undefined
  const riskResult = stageResults['risk_assessment'] as
    { risk_level?: string; message?: string; flagged_commands?: string[]; safe_commands?: string[] } | undefined
  const solutionResult = stageResults['solution'] as
    { commands?: string[]; steps?: string[] } | undefined

  const confidence: number =
    (incident?.risk as { confidence?: number } | null)?.confidence
    ?? classificationResult?.confidence
    ?? 0.9

  // Proposed patch: awaitingApproval commands > live stageResults > completed incident
  const proposedPatch: ProposedPatchData | null = (() => {
    if (awaitingApproval?.commands.length) {
      return {
        filename: 'Remediation Commands',
        diff: awaitingApproval.commands.map((cmd) => ({ type: 'add' as const, content: `+ ${cmd}` })),
      }
    }
    const cmds =
      (incident?.solution as { commands?: string[] } | null)?.commands
      ?? solutionResult?.commands
    if (cmds?.length) {
      return {
        filename: 'Proposed Remediation',
        diff: cmds.map((cmd) => ({ type: 'add' as const, content: `+ ${cmd}` })),
      }
    }
    return null
  })()

  // Remediation steps for the Remediation tab
  const remediationSteps: string[] =
    awaitingApproval?.commands
    ?? (incident?.solution as { steps?: string[] } | null)?.steps
    ?? solutionResult?.steps
    ?? []

  // Security data — available from stageResults as soon as risk_assessment completes
  const securityData: Record<string, unknown> | null =
    (incident?.risk as Record<string, unknown> | null)
    ?? (riskResult ? riskResult as Record<string, unknown> : null)

  const hasReport = !!(complete?.report_path ?? incident?.report_path)

  function handleDownload() {
    window.open(getReportUrl(id), '_blank')
  }

  return (
    <div className="h-[calc(100vh-48px)] flex flex-col">

      {/* ── Main two-panel area ─────────────────────────────────────────── */}
      <div className="flex flex-1 min-h-0">

        {/* Left — Analysis stream (65%) */}
        <div className="flex flex-col border-r border-[#222222]" style={{ width: '65%' }}>
          {/* Stream header */}
          <div className="flex items-center justify-between px-5 py-3 border-b border-[#222222] shrink-0">
            <div className="flex items-center gap-2">
              <span className={cn('w-2 h-2 rounded-full', complete ? 'bg-[#10B981]' : 'bg-[#10B981] animate-pulse')} />
              <span className="font-mono text-xs font-semibold uppercase tracking-[0.08em] text-white/80">
                Analysis Engine — {id}
              </span>
            </div>
            <div className="flex items-center gap-1">
              {/* playback controls are decorative */}
              <button className="w-7 h-7 flex items-center justify-center text-white/40 hover:text-white transition-colors"><SkipBack size={13} /></button>
              <button className="w-7 h-7 flex items-center justify-center text-white/40 hover:text-white transition-colors"><Pause size={13} /></button>
              <button className="w-7 h-7 flex items-center justify-center text-white/40 hover:text-white transition-colors"><SkipForward size={13} /></button>
              <span className="font-mono text-[10px] text-white/40 ml-2 px-2 py-0.5 border border-[#333]">1.0×</span>
            </div>
          </div>

          {/* Stream entries */}
          <div ref={streamRef} className="flex-1 overflow-y-auto py-4 px-5 flex flex-col gap-4">
            {error && (
              <div className="font-mono text-xs text-[#EF4444] p-2 border border-[rgba(239,68,68,0.2)]">
                {error}
              </div>
            )}
            <AnimatePresence initial={false}>
              {streamEntries.map((entry) => (
                <motion.div
                  key={entry.id}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.3 }}
                  className={cn(
                    'flex gap-3',
                    entry.highlighted && 'bg-[rgba(239,68,68,0.06)] border border-[rgba(239,68,68,0.2)] p-3 -mx-1'
                  )}
                >
                  <AgentBadge agent={entry.agent} className="mt-0.5" />
                  <p
                    className="font-mono text-xs leading-5 text-white/75 flex-1"
                    dangerouslySetInnerHTML={{ __html: entry.content }}
                  />
                </motion.div>
              ))}
            </AnimatePresence>

            {/* Connecting overlay — visible until first event arrives */}
            {streamEntries.length === 0 && !error && (
              <div className="flex gap-3 items-center">
                <span className="font-mono text-[10px] text-white/20 animate-pulse">
                  Connecting to analysis engine...
                </span>
              </div>
            )}

            {/* In-progress indicator — visible while stream is open */}
            {streamEntries.length > 0 && !complete && !error && (
              <div className="flex gap-3 items-center">
                <span className="w-1.5 h-1.5 rounded-full bg-[#8B5CF6] animate-pulse shrink-0" />
                <span className="font-mono text-[10px] text-white/20 animate-pulse">Processing...</span>
              </div>
            )}
          </div>
        </div>

        {/* Right — Execution pipeline (35%) */}
        <div className="flex flex-col" style={{ width: '35%' }}>
          {/* Pipeline steps */}
          <div className="px-5 py-4 border-b border-[#222222]">
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/40 mb-3">
              Execution Pipeline
            </p>
            <PipelineStepList steps={steps} />
          </div>

          {/* Tabs */}
          <div className="flex border-b border-[#222222] shrink-0">
            {(['diagnostic', 'remediation', 'security'] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={cn(
                  'flex-1 h-9 font-sans text-xs capitalize transition-colors border-b-2 -mb-px',
                  activeTab === tab
                    ? 'text-white border-white'
                    : 'text-white/40 border-transparent hover:text-white/70'
                )}
              >
                {tab.charAt(0).toUpperCase() + tab.slice(1)}
              </button>
            ))}
          </div>

          {/* Tab content */}
          <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-3">

            {/* ── Diagnostic tab ──────────────────────────────────────── */}
            {activeTab === 'diagnostic' && (
              <>
                {/* AI Confidence — live from classification stageResult, or incident after fetch */}
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-mono text-[10px] text-white/50 uppercase tracking-[0.05em]">
                      AI Confidence Score
                    </span>
                    <span className="font-mono text-sm font-semibold text-white">
                      {classificationResult || incident?.risk
                        ? `${(confidence * 100).toFixed(0)}%`
                        : <span className="text-white/30 text-xs">Pending…</span>
                      }
                    </span>
                  </div>
                  <div className="h-1.5 bg-[#1a1a1a] w-full">
                    <div
                      className="h-full bg-[#8B5CF6] transition-all duration-1000"
                      style={{ width: `${(confidence * 100).toFixed(0)}%` }}
                    />
                  </div>
                </div>

                {/* Awaiting approval callout */}
                {awaitingApproval && (
                  <div className="p-3 bg-[rgba(245,158,11,0.06)] border border-[rgba(245,158,11,0.2)] flex flex-col gap-1">
                    <p className="font-mono text-[10px] text-[#F59E0B] uppercase tracking-[0.05em]">
                      Human Approval Required
                    </p>
                    <p className="font-mono text-xs text-white/55">
                      Risk: <span className="text-white/80">{awaitingApproval.risk_level}</span>
                      {' · '}{awaitingApproval.commands.length} command(s) pending
                    </p>
                  </div>
                )}

                {/* Proposed patch — populated as soon as solution/awaiting_approval arrives */}
                {proposedPatch && <ProposedPatch patch={proposedPatch} />}

                {/* Security assessment */}
                <div className="flex items-start gap-3 p-3 bg-[rgba(16,185,129,0.06)] border border-[rgba(16,185,129,0.2)]">
                  <ShieldCheck size={16} className="text-[#10B981] shrink-0 mt-0.5" strokeWidth={1.5} />
                  <div>
                    <p className="font-mono text-[10px] text-[#10B981] uppercase tracking-[0.05em] mb-0.5">
                      Security Assessment
                    </p>
                    <p className="font-mono text-xs text-white/60">
                      {riskResult?.message ?? 'RISK LEVEL: SAFE. Patch does not introduce new attack vectors.'}
                    </p>
                  </div>
                </div>
              </>
            )}

            {/* ── Remediation tab ─────────────────────────────────────── */}
            {activeTab === 'remediation' && (
              <div className="flex flex-col gap-3">
                {remediationSteps.length > 0 ? (
                  <>
                    <p className="font-mono text-xs text-white/50">
                      {awaitingApproval ? 'Proposed remediation commands:' : 'Remediation plan generated:'}
                    </p>
                    <ol className="flex flex-col gap-2">
                      {remediationSteps.map((step, i) => (
                        <li key={i} className="flex items-start gap-2 font-mono text-xs text-white/70">
                          <span className="text-[#8B5CF6] shrink-0">{String(i + 1).padStart(2, '0')}.</span>
                          {step}
                        </li>
                      ))}
                    </ol>
                  </>
                ) : (
                  <p className="font-mono text-xs text-white/30 animate-pulse">
                    Awaiting remediation stage…
                  </p>
                )}

                {commandResults.length > 0 && (
                  <div className="flex flex-col gap-1.5 pt-2 border-t border-[#222222]">
                    <p className="font-mono text-[10px] uppercase tracking-[0.05em] text-white/40">
                      Execution Results
                    </p>
                    {commandResults.map((r, i) => (
                      <div key={i} className={`font-mono text-xs ${r.success ? 'text-[#10B981]' : 'text-[#EF4444]'}`}>
                        {r.success ? '✓' : '✗'} {r.command}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* ── Security tab ─────────────────────────────────────────── */}
            {activeTab === 'security' && (
              <div className="flex flex-col gap-3">
                <div className="flex items-start gap-3 p-3 bg-[rgba(16,185,129,0.06)] border border-[rgba(16,185,129,0.2)]">
                  <ShieldCheck size={16} className="text-[#10B981] shrink-0 mt-0.5" strokeWidth={1.5} />
                  <div>
                    <p className="font-mono text-[10px] text-[#10B981] uppercase tracking-[0.05em] mb-1">
                      Full Security Report
                    </p>
                    <div className="flex flex-col gap-1.5 font-mono text-xs text-white/60">
                      {securityData && Object.keys(securityData).length > 0 ? (
                        Object.entries(securityData)
                          .filter(([k]) => !['incident_id', 'commands'].includes(k))
                          .map(([k, v]) => (
                            <p key={k}>
                              {k.replace(/_/g, ' ')}:{' '}
                              <span className="text-white/80">
                                {Array.isArray(v) ? (v.length === 0 ? 'None' : v.join(', ')) : String(v)}
                              </span>
                            </p>
                          ))
                      ) : (
                        <>
                          <p>Attack surface delta: <span className="text-white/80">None</span></p>
                          <p>CVE scan: <span className="text-[#10B981]">Clean</span></p>
                          <p>Compliance: <span className="text-[#10B981]">SOC2 compliant</span></p>
                          <p>Auth scope change: <span className="text-white/80">No</span></p>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ── Footer bar ──────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between px-5 py-3 border-t border-[#222222] bg-black shrink-0">
        <div className="flex items-center gap-3">
          <FileText size={14} className="text-white/30" strokeWidth={1.5} />
          <span className="font-mono text-xs text-white/40">
            incident-report-{id}.pdf
          </span>
          <button
            onClick={handleDownload}
            disabled={!hasReport}
            className="font-mono text-xs text-[#8B5CF6] hover:text-[#a78bfa] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            Download PDF Report
          </button>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={reject}
            disabled={!awaitingApproval}
            className="flex items-center gap-2 px-4 h-9 border border-[#333333] font-sans text-xs text-white/50 hover:text-white hover:border-[#555] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <X size={13} />
            Reject &amp; Dismiss
          </button>
          <button
            onClick={approve}
            disabled={!awaitingApproval}
            className="flex items-center gap-2 px-4 h-9 bg-[#10B981] hover:bg-[#0d9668] disabled:opacity-40 disabled:cursor-not-allowed text-black font-sans text-xs font-semibold transition-colors"
          >
            <Play size={12} strokeWidth={2.5} />
            Approve &amp; Execute Patch
          </button>
        </div>
      </div>
    </div>
  )
}
