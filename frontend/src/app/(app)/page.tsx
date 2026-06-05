'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { AlertTriangle, Zap, Activity, TrendingUp, RefreshCw, Upload, Radio } from 'lucide-react'
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend,
  PieChart, Pie, Cell,
} from 'recharts'
import { MetricCard } from '@/components/ui/MetricCard'
import { StatusDot } from '@/components/ui/StatusDot'
import { getStats, submitIncident } from '@/lib/api'
import type { StatsData } from '@/lib/api'
import { incidentTrends } from '@/lib/mock-data'

const DONUT_COLORS = ['#EF4444', '#F59E0B', '#8B5CF6', '#3B82F6', '#10B981']

export default function DashboardPage() {
  const router = useRouter()
  const [log, setLog] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [stats, setStats] = useState<StatsData | null>(null)
  const [simPulse, setSimPulse] = useState(false)

  useEffect(() => {
    getStats().then(setStats).catch(console.error)
  }, [])

  async function handleRunPipeline() {
    if (!log.trim() || submitting) return
    setSubmitting(true)
    try {
      const incident = await submitIncident(log)
      router.push(`/incidents/${incident.id}`)
    } catch (err) {
      console.error('Failed to submit incident:', err)
      setSubmitting(false)
    }
  }

  function handleSimulate() {
    setSimPulse(true)
    setTimeout(() => setSimPulse(false), 2000)
  }

  const totalIncidents  = stats?.total_incidents ?? '—'
  const avgResolution   = stats ? `${(stats.avg_resolution_ms / 1000).toFixed(1)}s` : '—'
  const aiAccuracy      = stats ? `${stats.ai_accuracy}%` : '—'
  const uptimePercent   = stats ? `${stats.uptime_percent}%` : '—'

  const donutData = stats && Object.keys(stats.by_category).length > 0
    ? Object.entries(stats.by_category).map(([name, value], i) => ({
        name, value, color: DONUT_COLORS[i % DONUT_COLORS.length],
      }))
    : [
        { name: 'OOM Kills',      value: 120, color: '#EF4444' },
        { name: 'Query Timeouts', value: 85,  color: '#F59E0B' },
        { name: 'Auth Fails',     value: 42,  color: '#8B5CF6' },
      ]

  const donutTotal   = donutData.reduce((s, d) => s + d.value, 0)
  const topPlaybooks = stats?.top_playbooks ?? []

  return (
    <div className="p-6 flex flex-col gap-5">

      {/* ── Stat bar ─────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-4 gap-4">
        <MetricCard label="Total Incidents"       value={String(totalIncidents)} icon={AlertTriangle} active />
        <MetricCard label="Avg Resolution Time"   value={avgResolution}           icon={Zap}          />
        <MetricCard label="AI Synthesis Accuracy" value={aiAccuracy}              icon={Activity}     />
        <MetricCard label="Cluster Uptime"        value={uptimePercent}           icon={TrendingUp} statusBadge="HEALTHY" />
      </div>

      {/* ── Middle row ───────────────────────────────────────────────────── */}
      <div className="grid grid-cols-3 gap-4">

        {/* Incident Trends Chart */}
        <div className="col-span-2 bg-[#121212] border border-[#222222] p-4">
          <h2 className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/50 mb-4">
            Incident Trends (Last 24H)
          </h2>
          <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={incidentTrends} margin={{ top: 0, right: 0, left: -20, bottom: 0 }}>
              <defs>
                <linearGradient id="mem" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#EF4444" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#EF4444" stopOpacity={0}   />
                </linearGradient>
                <linearGradient id="db" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#F59E0B" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#F59E0B" stopOpacity={0}   />
                </linearGradient>
                <linearGradient id="sec" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#8B5CF6" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#8B5CF6" stopOpacity={0}   />
                </linearGradient>
                <linearGradient id="nod" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#3B82F6" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#3B82F6" stopOpacity={0}   />
                </linearGradient>
              </defs>
              <XAxis dataKey="time" tick={{ fill: 'rgba(255,255,255,0.25)', fontSize: 10, fontFamily: 'var(--font-jetbrains-mono)' }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fill: 'rgba(255,255,255,0.25)', fontSize: 10, fontFamily: 'var(--font-jetbrains-mono)' }} axisLine={false} tickLine={false} />
              <Tooltip
                contentStyle={{ background: '#121212', border: '1px solid #333', borderRadius: 0 }}
                labelStyle={{ color: 'rgba(255,255,255,0.6)', fontFamily: 'var(--font-jetbrains-mono)', fontSize: 11 }}
                itemStyle={{ fontFamily: 'var(--font-jetbrains-mono)', fontSize: 11 }}
              />
              <Legend wrapperStyle={{ fontSize: 11, fontFamily: 'var(--font-jetbrains-mono)', paddingTop: 8 }} iconType="circle" iconSize={6} />
              <Area type="monotone" dataKey="memory"   name="Memory"   stroke="#EF4444" fill="url(#mem)" strokeWidth={1.5} />
              <Area type="monotone" dataKey="db"       name="DB"       stroke="#F59E0B" fill="url(#db)"  strokeWidth={1.5} />
              <Area type="monotone" dataKey="security" name="Security" stroke="#8B5CF6" fill="url(#sec)" strokeWidth={1.5} />
              <Area type="monotone" dataKey="node"     name="Node"     stroke="#3B82F6" fill="url(#nod)" strokeWidth={1.5} />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        {/* Anomaly Distribution */}
        <div className="bg-[#121212] border border-[#222222] p-4 flex flex-col gap-4">
          <h2 className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/50">
            Anomaly Distribution
          </h2>

          <div className="flex items-center justify-center relative">
            <ResponsiveContainer width={140} height={140}>
              <PieChart>
                <Pie data={donutData} cx="50%" cy="50%" innerRadius={45} outerRadius={65} dataKey="value" strokeWidth={0}>
                  {donutData.map((entry, i) => (
                    <Cell key={i} fill={entry.color} opacity={0.85} />
                  ))}
                </Pie>
              </PieChart>
            </ResponsiveContainer>
            <span className="absolute font-mono text-2xl font-semibold text-white pointer-events-none">
              {donutTotal}
            </span>
          </div>

          <div className="flex flex-col gap-2">
            {donutData.map((d) => (
              <div key={d.name} className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="w-2 h-2 rounded-full" style={{ background: d.color }} />
                  <span className="font-mono text-xs text-white/60">{d.name}</span>
                </div>
                <span className="font-mono text-xs font-semibold text-white">{d.value}</span>
              </div>
            ))}
          </div>

          <div className="border-t border-[#222222] pt-3 flex flex-col gap-2">
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/40">
              Top Performing Playbooks
            </p>
            {topPlaybooks.length > 0 ? topPlaybooks.map((pb) => (
              <div key={pb.name} className="flex items-center justify-between">
                <span className="font-mono text-[10px] text-white/50 truncate max-w-[140px]">{pb.name}</span>
                {pb.count != null && (
                  <span className="font-mono text-[10px] font-semibold text-[#10B981]">{pb.count}×</span>
                )}
              </div>
            )) : (
              <span className="font-mono text-[10px] text-white/30">No playbooks triggered yet.</span>
            )}
            <button
              onClick={handleSimulate}
              className={`mt-1 w-full h-7 border font-mono text-[10px] transition-colors flex items-center justify-center gap-1.5 ${
                simPulse
                  ? 'border-[#10B981] text-[#10B981]'
                  : 'border-[#333333] text-white/40 hover:text-white/70 hover:border-[#444]'
              }`}
            >
              <RefreshCw size={11} className={simPulse ? 'animate-spin' : ''} />
              {simPulse ? 'Simulating...' : '[Simulate Live Cluster Data]'}
            </button>
          </div>
        </div>
      </div>

      {/* ── Ingestion vectors ─────────────────────────────────────────────── */}
      <div>
        <h2 className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/50 mb-3">
          Inbound Ingestion Vector Selector
        </h2>
        <div className="grid grid-cols-3 gap-4">

          {/* Raw Log Analysis */}
          <div className="bg-[#121212] border border-[#222222] p-4 flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <span className="font-sans text-sm font-semibold text-white">Raw Log Analysis</span>
              <span className="px-1.5 py-0.5 bg-[rgba(16,185,129,0.15)] border border-[#10B981] font-mono text-[9px] font-semibold text-[#10B981] uppercase tracking-wider">
                FAST
              </span>
            </div>
            <textarea
              value={log}
              onChange={(e) => setLog(e.target.value)}
              placeholder="Paste raw JSON or text logs here..."
              className="h-16 bg-black border border-[#1a1a1a] px-3 py-2 font-mono text-xs text-white/70 placeholder-white/20 resize-none outline-none focus:border-[#8B5CF6] transition-colors"
            />
            <button
              onClick={handleRunPipeline}
              disabled={submitting || !log.trim()}
              className="h-9 bg-[#8B5CF6] hover:bg-[#7c3aed] disabled:opacity-40 disabled:cursor-not-allowed text-white font-sans text-xs font-semibold uppercase tracking-wide flex items-center justify-center gap-2 transition-colors"
            >
              {submitting ? 'Submitting...' : <>RUN DIAGNOSTIC PIPELINE <ArrowRight size={13} /></>}
            </button>
          </div>

          {/* Live Stream */}
          <div className="bg-[#121212] border border-[#222222] p-4 flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <span className="font-sans text-sm font-semibold text-white">Live Stream Monitor</span>
              <span className="px-1.5 py-0.5 bg-[rgba(16,185,129,0.15)] border border-[#10B981] font-mono text-[9px] font-semibold text-[#10B981] uppercase tracking-wider flex items-center gap-1">
                <StatusDot status="live" size={6} pulse />
                LIVE
              </span>
            </div>
            <div className="flex-1 flex items-center justify-center py-8">
              <div className="flex items-center gap-2">
                <StatusDot status="live" size={8} pulse />
                <span className="font-mono text-sm text-white/50">Awaiting Connection...</span>
              </div>
            </div>
            <a
              href="/stream"
              className="h-9 border border-[#333333] hover:border-[#8B5CF6] text-white/50 hover:text-white font-sans text-xs font-semibold uppercase tracking-wide flex items-center justify-center gap-2 transition-colors"
            >
              <Radio size={13} />
              Open Stream
            </a>
          </div>

          {/* Batch Upload */}
          <div className="bg-[#121212] border border-[#222222] p-4 flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <span className="font-sans text-sm font-semibold text-white">Batch File Upload</span>
              <span className="px-1.5 py-0.5 bg-[rgba(139,92,246,0.15)] border border-[#8B5CF6] font-mono text-[9px] font-semibold text-[#8B5CF6] uppercase tracking-wider">
                BATCH
              </span>
            </div>
            <a
              href="/archive"
              className="flex-1 flex flex-col items-center justify-center gap-2 border border-dashed border-[#222222] hover:border-[#444444] transition-colors py-6 cursor-pointer"
            >
              <Upload size={28} strokeWidth={1} className="text-white/25" />
              <span className="font-mono text-xs text-white/30">Drop .gz, .tar, or .log files here</span>
            </a>
          </div>
        </div>
      </div>
    </div>
  )
}

function ArrowRight({ size, ...props }: { size: number; [k: string]: unknown }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...props as React.SVGProps<SVGSVGElement>}>
      <path d="M5 12h14M12 5l7 7-7 7" />
    </svg>
  )
}
