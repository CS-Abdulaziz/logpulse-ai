import { cn } from '@/lib/utils'

type Status = 'healthy' | 'warning' | 'critical' | 'live' | 'offline'

interface StatusDotProps {
  status: Status
  pulse?: boolean
  size?: number
  className?: string
}

const colorMap: Record<Status, string> = {
  healthy:  'bg-[#10B981]',
  warning:  'bg-[#F59E0B]',
  critical: 'bg-[#EF4444]',
  live:     'bg-[#10B981]',
  offline:  'bg-white/30',
}

const dotClass: Record<Status, string> = {
  healthy:  'status-dot status-dot-healthy',
  warning:  'status-dot status-dot-warning',
  critical: 'status-dot status-dot-critical',
  live:     'status-dot status-dot-live',
  offline:  'status-dot status-dot-offline',
}

export function StatusDot({ status, pulse = false, size = 8, className }: StatusDotProps) {
  const usePulse = pulse || status === 'critical' || status === 'live'

  return (
    <span
      className={cn(
        'inline-block rounded-full flex-shrink-0 relative',
        usePulse ? dotClass[status] : colorMap[status],
        className
      )}
      style={{ width: size, height: size }}
    />
  )
}
