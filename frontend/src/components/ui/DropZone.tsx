'use client'

import { useRef, useState } from 'react'
import { FolderOpen } from 'lucide-react'
import { cn } from '@/lib/utils'

interface DropZoneProps {
  accept?: string[]
  maxSizeGB?: number
  onDrop?: (files: File[]) => void
  className?: string
}

export function DropZone({ accept = ['.log', '.json', '.gz'], maxSizeGB = 5, onDrop, className }: DropZoneProps) {
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const files = Array.from(e.dataTransfer.files)
    onDrop?.(files)
  }

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? [])
    onDrop?.(files)
    e.target.value = ''
  }

  return (
    <div
      onClick={() => inputRef.current?.click()}
      onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      className={cn(
        'flex flex-col items-center justify-center gap-3 py-12 border border-dashed cursor-pointer transition-colors',
        dragging ? 'border-[#8B5CF6] bg-[rgba(139,92,246,0.05)]' : 'border-[#222222] hover:border-[#444444]',
        className
      )}
    >
      <FolderOpen size={40} strokeWidth={1} className="text-white/30" />
      <div className="text-center">
        <p className="text-sm text-white/70 font-sans">Drag and drop diagnostic bundle here</p>
        <p className="text-xs font-mono text-white/30 mt-1">
          Supported formats: {accept.join(', ')} (Max {maxSizeGB}GB)
        </p>
      </div>
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        multiple
        accept={accept.join(',')}
        onChange={handleInputChange}
      />
    </div>
  )
}
