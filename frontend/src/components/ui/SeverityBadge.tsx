import { cn } from '@/lib/utils'
import type { Severity } from '@/lib/types'

interface SeverityBadgeProps {
  level: Severity
  className?: string
}

const config: Record<Severity, { bg: string; border: string; text: string; label: string }> = {
  CRITICAL: {
    bg:     'bg-[rgba(239,68,68,0.15)]',
    border: 'border-[#EF4444]',
    text:   'text-[#EF4444]',
    label:  'CRITICAL',
  },
  WARN: {
    bg:     'bg-[rgba(245,158,11,0.15)]',
    border: 'border-[#F59E0B]',
    text:   'text-[#F59E0B]',
    label:  'WARN',
  },
  INFO: {
    bg:     'bg-[rgba(59,130,246,0.15)]',
    border: 'border-[#3B82F6]',
    text:   'text-[#3B82F6]',
    label:  'INFO',
  },
  SUCCESS: {
    bg:     'bg-[rgba(16,185,129,0.15)]',
    border: 'border-[#10B981]',
    text:   'text-[#10B981]',
    label:  'SUCCESS',
  },
}

export function SeverityBadge({ level, className }: SeverityBadgeProps) {
  const { bg, border, text, label } = config[level]
  return (
    <span
      className={cn(
        'inline-flex items-center px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.05em] border',
        bg, border, text, className
      )}
    >
      {label}
    </span>
  )
}
