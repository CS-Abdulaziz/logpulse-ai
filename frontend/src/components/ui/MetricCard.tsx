import { cn } from '@/lib/utils'
import type { LucideIcon } from 'lucide-react'

interface MetricCardProps {
  label: string
  value: string
  icon: LucideIcon
  active?: boolean
  statusBadge?: string
  className?: string
}

export function MetricCard({ label, value, icon: Icon, active, statusBadge, className }: MetricCardProps) {
  return (
    <div
      className={cn(
        'flex flex-col justify-between p-4 bg-[#121212] border h-[88px] transition-colors',
        active ? 'border-[#8B5CF6]' : 'border-[#222222]',
        className
      )}
    >
      <div className="flex items-center justify-between">
        <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/50">
          {label}
        </span>
        <Icon size={14} strokeWidth={1.5} className={active ? 'text-[#8B5CF6]' : 'text-white/40'} />
      </div>
      <div className="flex items-end gap-2">
        <span className="font-mono text-2xl font-semibold text-white">{value}</span>
        {statusBadge && (
          <span className="mb-0.5 px-1.5 py-0.5 bg-[rgba(16,185,129,0.15)] border border-[#10B981] text-[#10B981] font-mono text-[10px] font-semibold uppercase tracking-[0.05em]">
            {statusBadge}
          </span>
        )}
      </div>
    </div>
  )
}
