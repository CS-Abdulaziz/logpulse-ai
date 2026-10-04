import { cn } from '@/lib/utils'
import type { AgentType } from '@/lib/types'

interface AgentBadgeProps {
  agent: AgentType
  className?: string
}

const config: Record<AgentType, { bg: string; border: string; text: string }> = {
  INGEST:   { bg: 'bg-[rgba(139,92,246,0.15)]', border: 'border-[#8B5CF6]', text: 'text-[#8B5CF6]' },
  CLASSIFY: { bg: 'bg-[rgba(245,158,11,0.15)]', border: 'border-[#F59E0B]', text: 'text-[#F59E0B]' },
  RAG:      { bg: 'bg-[rgba(59,130,246,0.15)]', border: 'border-[#3B82F6]', text: 'text-[#3B82F6]' },
  DIAGNOSE: { bg: 'bg-[rgba(168,85,247,0.15)]', border: 'border-[#A855F7]', text: 'text-[#A855F7]' },
  RISK:     { bg: 'bg-[rgba(239,68,68,0.15)]',  border: 'border-[#EF4444]', text: 'text-[#EF4444]' },
  SYNTH:    { bg: 'bg-[rgba(16,185,129,0.15)]', border: 'border-[#10B981]', text: 'text-[#10B981]' },
}

export function AgentBadge({ agent, className }: AgentBadgeProps) {
  const { bg, border, text } = config[agent]
  return (
    <span
      className={cn(
        'inline-flex items-center px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.05em] border shrink-0',
        bg, border, text, className
      )}
    >
      [{agent}]
    </span>
  )
}
