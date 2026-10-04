'use client'

import { useEffect, useState } from 'react'
import { Search, Clock, X, Play, Edit2, Loader2 } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { BarChart, Bar, XAxis, ResponsiveContainer, Cell } from 'recharts'
import { listPlaybooks } from '@/lib/api'
import type { PlaybookItem } from '@/lib/api'
import type { PlaybookCategory } from '@/lib/types'
import { cn } from '@/lib/utils'

const ALL_FILTERS: (PlaybookCategory | 'ALL')[] = ['ALL', 'MEMORY', 'DATABASE', 'SECURITY', 'NETWORK']

const categoryColors: Record<string, { bg: string; border: string; text: string }> = {
  MEMORY:   { bg: 'bg-[rgba(139,92,246,0.15)]', border: 'border-[#8B5CF6]', text: 'text-[#8B5CF6]' },
  DATABASE: { bg: 'bg-[rgba(245,158,11,0.15)]', border: 'border-[#F59E0B]', text: 'text-[#F59E0B]' },
  SECURITY: { bg: 'bg-[rgba(239,68,68,0.15)]',  border: 'border-[#EF4444]', text: 'text-[#EF4444]' },
  NETWORK:  { bg: 'bg-[rgba(59,130,246,0.15)]', border: 'border-[#3B82F6]', text: 'text-[#3B82F6]' },
}

const defaultCategoryColor = { bg: 'bg-[rgba(255,255,255,0.08)]', border: 'border-[#444]', text: 'text-white/60' }

function normalizeCategory(cat: string): string {
  return cat.toUpperCase()
}

export default function PlaybooksPage() {
  const [playbooks, setPlaybooks] = useState<PlaybookItem[]>([])
  const [loading,   setLoading]   = useState(true)
  const [search,    setSearch]    = useState('')
  const [filter,    setFilter]    = useState<PlaybookCategory | 'ALL'>('ALL')
  const [selected,  setSelected]  = useState<PlaybookItem | null>(null)

  // Debounced API fetch whenever search or filter changes
  useEffect(() => {
    setLoading(true)
    const t = setTimeout(() => {
      const params: Parameters<typeof listPlaybooks>[0] = {}
      if (search)         params.search   = search
      if (filter !== 'ALL') params.category = filter
      listPlaybooks(params)
        .then(setPlaybooks)
        .catch(console.error)
        .finally(() => setLoading(false))
    }, 300)
    return () => clearTimeout(t)
  }, [search, filter])

  return (
    <div className="flex h-[calc(100vh-48px)] overflow-hidden">

      {/* ── List zone ─────────────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
        {/* Search + filters */}
        <div className="flex items-center gap-3 px-5 py-4 border-b border-[#222222] sticky top-0 bg-black z-10">
          <div className="relative flex-1 max-w-lg">
            <Search size={13} strokeWidth={1.5} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search playbooks, commands, syntax..."
              className="w-full h-9 bg-black border border-[#222222] pl-9 pr-3 font-mono text-xs text-white placeholder-white/25 outline-none focus:border-[#8B5CF6] transition-colors"
            />
          </div>
          <div className="flex items-center gap-2">
            {ALL_FILTERS.map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={cn(
                  'px-3 h-7 font-mono text-[10px] font-semibold uppercase tracking-wider border transition-colors',
                  filter === f
                    ? 'bg-[#8B5CF6] border-[#8B5CF6] text-white'
                    : 'bg-black border-[#222222] text-white/40 hover:text-white/70 hover:border-[#333]'
                )}
              >
                {f}
              </button>
            ))}
          </div>
        </div>

        {/* Card grid */}
        <div className="p-5 grid grid-cols-3 gap-4 auto-rows-min">
          {loading ? (
            <div className="col-span-3 flex items-center gap-2 py-12 text-white/40 justify-center">
              <Loader2 size={16} className="animate-spin" />
              <span className="font-mono text-xs">Loading playbooks...</span>
            </div>
          ) : playbooks.length === 0 ? (
            <div className="col-span-3 font-mono text-xs text-white/30 py-12 text-center border border-[#222222]">
              No playbooks found.
            </div>
          ) : (
            playbooks.map((pb) => (
              <PlaybookCard
                key={pb.id}
                playbook={pb}
                active={selected?.id === pb.id}
                onClick={() => setSelected(selected?.id === pb.id ? null : pb)}
              />
            ))
          )}
        </div>
      </div>

      {/* ── Detail drawer ─────────────────────────────────────────────────── */}
      <AnimatePresence>
        {selected && (
          <motion.div
            key="drawer"
            initial={{ x: 400, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: 400, opacity: 0 }}
            transition={{ type: 'spring', stiffness: 300, damping: 30 }}
            className="w-[400px] shrink-0 flex flex-col border-l border-[#222222] bg-black overflow-hidden"
          >
            {/* Drawer header */}
            <div className="flex items-start justify-between px-5 py-4 border-b border-[#222222] shrink-0">
              <h2 className="font-sans text-base font-semibold text-white pr-4 leading-6">
                {selected.title}
              </h2>
              <button
                onClick={() => setSelected(null)}
                className="text-white/30 hover:text-white transition-colors mt-0.5"
              >
                <X size={16} />
              </button>
            </div>

            {/* Drawer body */}
            <div className="flex-1 overflow-y-auto p-5 flex flex-col gap-5">

              {/* Meta */}
              <div className="flex items-center gap-3">
                {selected.severity_typical && (
                  <span className="font-mono text-[10px] text-white/50">
                    Typical severity: <span className="text-white/80">{selected.severity_typical}</span>
                  </span>
                )}
                {selected.source && (
                  <span className="font-mono text-[10px] text-white/30 truncate">
                    Source: {selected.source}
                  </span>
                )}
              </div>

              {/* Resolution steps */}
              <div>
                <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/40 mb-2">
                  Resolution Steps
                </p>
                <ol className="flex flex-col gap-1.5">
                  {selected.resolution_steps.map((step, i) => (
                    <li key={i} className="flex items-start gap-2 font-sans text-xs text-white/65 leading-5">
                      <span className="text-[#8B5CF6] font-mono shrink-0">{String(i + 1).padStart(2, '0')}.</span>
                      {step}
                    </li>
                  ))}
                </ol>
              </div>

              {/* Code fix / script source */}
              {selected.code_fix && (
                <div>
                  <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/40 mb-2">
                    Code Fix
                  </p>
                  <div className="bg-[#0a0a0a] border border-[#222222] overflow-auto max-h-[260px]">
                    <table className="w-full border-collapse">
                      <tbody>
                        {selected.code_fix.split('\n').map((line, i) => (
                          <tr key={i} className="hover:bg-[#111]">
                            <td className="px-3 py-px select-none font-mono text-[11px] text-white/25 text-right w-8 border-r border-[#1a1a1a]">
                              {String(i + 1).padStart(2, '0')}
                            </td>
                            <td className="px-3 py-px">
                              <code className="font-mono text-[11px] leading-5 whitespace-pre">
                                <SyntaxLine line={line} />
                              </code>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>

            {/* Drawer footer */}
            <div className="flex items-center justify-between px-5 py-3 border-t border-[#222222] shrink-0">
              <button className="flex items-center gap-2 px-4 h-8 border border-[#333333] font-sans text-xs text-white/50 hover:text-white hover:border-[#555] transition-colors">
                <Edit2 size={12} />
                Edit Playbook
              </button>
              <button className="flex items-center gap-2 px-4 h-8 bg-[#10B981] hover:bg-[#0d9668] text-black font-sans text-xs font-semibold transition-colors">
                <Play size={12} strokeWidth={2.5} />
                Run Now
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ─── PlaybookCard ──────────────────────────────────────────────────────────────

function PlaybookCard({
  playbook,
  active,
  onClick,
}: {
  playbook: PlaybookItem
  active:   boolean
  onClick:  () => void
}) {
  const cat    = normalizeCategory(playbook.category)
  const colors = categoryColors[cat] ?? defaultCategoryColor

  return (
    <button
      onClick={onClick}
      className={cn(
        'text-left bg-[#121212] border p-4 flex flex-col gap-3 transition-colors hover:border-[#8B5CF6]/60',
        active ? 'border-[#8B5CF6]' : 'border-[#222222]'
      )}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <span className="font-sans text-sm font-semibold text-white leading-5">{playbook.title}</span>
        <span className={cn('shrink-0 px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-wider border', colors.bg, colors.border, colors.text)}>
          {cat}
        </span>
      </div>

      {/* Step count */}
      <div className="flex items-center gap-1.5">
        <Clock size={11} strokeWidth={1.5} className="text-white/30" />
        <span className="font-mono text-[11px] text-white/40">
          {playbook.resolution_steps.length} step{playbook.resolution_steps.length !== 1 ? 's' : ''}
        </span>
      </div>

      {/* Script preview (first 3 resolution steps) */}
      <div className="bg-[#0a0a0a] border border-[#1a1a1a] px-3 py-2">
        {playbook.resolution_steps.slice(0, 3).map((line, i) => (
          <div key={i} className="flex gap-2 py-px">
            <span className="font-mono text-[10px] text-white/20 shrink-0">{i + 1}</span>
            <span className="font-mono text-[10px] text-white/65 truncate">{line}</span>
          </div>
        ))}
      </div>
    </button>
  )
}

// ─── Minimal bash syntax highlighter ──────────────────────────────────────────

function SyntaxLine({ line }: { line: string }) {
  if (line.trimStart().startsWith('#')) {
    return <span className="text-[#10B981]">{line}</span>
  }
  if (line.startsWith('#!/')) {
    return <span className="text-white/40">{line}</span>
  }
  const varMatch = line.match(/^(\w+=)(.*)$/)
  if (varMatch) {
    return (
      <>
        <span className="text-[#8B5CF6]">{varMatch[1]}</span>
        <span className="text-[#F59E0B]">{varMatch[2]}</span>
      </>
    )
  }
  if (line.trimStart().startsWith('echo')) {
    return <span className="text-white/80">{line}</span>
  }
  return <span className="text-white/75">{line}</span>
}
