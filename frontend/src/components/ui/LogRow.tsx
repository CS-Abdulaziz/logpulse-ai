import { cn } from '@/lib/utils'
import type { LogLevel } from '@/lib/types'

interface LogRowProps {
  timestamp: string
  level: LogLevel
  message: string
  className?: string
}

const levelConfig: Record<LogLevel, { bg: string; border: string; color: string }> = {
  CRIT: { bg: 'bg-[rgba(239,68,68,0.08)]', border: 'border-l-2 border-[#EF4444]', color: 'text-[#EF4444]' },
  ERR:  { bg: 'bg-[rgba(245,158,11,0.05)]', border: '',                              color: 'text-[#F59E0B]' },
  WARN: { bg: 'bg-[rgba(245,158,11,0.04)]', border: '',                              color: 'text-[#F59E0B]/60' },
  INFO: { bg: '',                            border: '',                              color: 'text-white/30' },
}

export function LogRow({ timestamp, level, message, className }: LogRowProps) {
  const { bg, border, color } = levelConfig[level]
  return (
    <div className={cn('flex gap-2 px-3 py-1 font-mono text-xs leading-5', bg, border, className)}>
      <span className="text-white/30 shrink-0 w-[62px]">{timestamp}</span>
      <span className={cn('shrink-0 w-[36px] font-semibold', color)}>[{level}]</span>
      <span className="text-white/75 break-all">{message}</span>
    </div>
  )
}
