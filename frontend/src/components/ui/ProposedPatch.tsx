import { Copy } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ProposedPatchData } from '@/lib/types'

interface ProposedPatchProps {
  patch: ProposedPatchData
  className?: string
}

export function ProposedPatch({ patch, className }: ProposedPatchProps) {
  return (
    <div className={cn('border border-[#222222] bg-[#0a0a0a]', className)}>
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-[#222222]">
        <span className="font-mono text-[11px] text-white/60">
          Proposed Patch: <span className="text-white/90">{patch.filename}</span>
        </span>
        <button className="text-white/30 hover:text-white/70 transition-colors">
          <Copy size={13} />
        </button>
      </div>
      {/* Diff lines */}
      <div className="p-3 font-mono text-xs leading-6">
        {patch.diff.map((line, i) => (
          <div
            key={i}
            className={cn(
              line.type === 'add'     && 'text-[#10B981]',
              line.type === 'remove'  && 'text-[#EF4444]',
              line.type === 'context' && 'text-white/40',
            )}
          >
            {line.content}
          </div>
        ))}
      </div>
    </div>
  )
}
