import { CheckCircle, Zap } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { PipelineStep } from '@/lib/types'

interface PipelineStepListProps {
  steps: PipelineStep[]
  className?: string
}

export function PipelineStepList({ steps, className }: PipelineStepListProps) {
  return (
    <ol className={cn('flex flex-col gap-0', className)}>
      {steps.map((step, i) => (
        <li key={step.id} className="flex items-center gap-3 py-2 relative">
          {/* Connector line */}
          {i < steps.length - 1 && (
            <span className="absolute left-[11px] top-[calc(50%+10px)] w-px h-[calc(100%-4px)] bg-[#222222]" />
          )}

          {/* Step icon */}
          <span className="relative z-10 w-6 h-6 flex items-center justify-center shrink-0">
            {step.status === 'done' && (
              <CheckCircle size={20} className="text-[#10B981]" strokeWidth={1.5} />
            )}
            {step.status === 'active' && (
              <Zap size={18} className="text-[#8B5CF6] animate-pulse" strokeWidth={1.5} />
            )}
            {(step.status === 'pending' || step.status === 'error') && (
              <span className="w-4 h-4 rounded-full border border-[#333333] bg-[#111]" />
            )}
          </span>

          {/* Label */}
          <span
            className={cn(
              'text-sm font-sans',
              step.status === 'done'    && 'text-white/70',
              step.status === 'active'  && 'text-white font-semibold',
              step.status === 'pending' && 'text-white/30',
              step.status === 'error'   && 'text-[#EF4444]',
            )}
          >
            {step.label}
          </span>
        </li>
      ))}
    </ol>
  )
}
