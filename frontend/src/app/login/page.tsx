'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { User, Lock, ArrowRight } from 'lucide-react'
import { StatusDot } from '@/components/ui/StatusDot'

export default function LoginPage() {
  const router = useRouter()
  const [operatorId, setOperatorId] = useState('admin@cluster-01')
  const [accessKey, setAccessKey] = useState('••••••••••••')
  const [maintain, setMaintain] = useState(false)
  const [loading, setLoading] = useState(false)

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    // UI-only auth — redirect immediately
    setTimeout(() => router.push('/'), 600)
  }

  return (
    <div className="min-h-screen bg-black flex items-center justify-center px-4">
      <div className="w-[400px] bg-[#121212] border border-[#222222]">
        {/* Card header */}
        <div className="px-7 pt-7 pb-5 border-b border-[#222222]">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-lg font-semibold font-sans text-white">LogPulse AI Login</h1>
              <p className="font-mono text-[11px] text-white/40 mt-0.5">
                System Authentication Portal v2.4.1
              </p>
            </div>
            <StatusDot status="healthy" pulse size={10} />
          </div>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="px-7 py-6 flex flex-col gap-5">
          {/* Operator ID */}
          <div className="flex flex-col gap-1.5">
            <label className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/50">
              Operator ID
            </label>
            <div className="relative">
              <User size={13} strokeWidth={1.5} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
              <input
                type="text"
                value={operatorId}
                onChange={(e) => setOperatorId(e.target.value)}
                className="w-full h-10 bg-black border border-[#222222] pl-9 pr-3 font-mono text-sm text-white placeholder-white/30 outline-none focus:border-[#8B5CF6] transition-colors"
              />
            </div>
          </div>

          {/* Access Key */}
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
              <label className="font-mono text-[10px] font-semibold uppercase tracking-[0.05em] text-white/50">
                Access Key
              </label>
              <button type="button" className="font-sans text-[11px] text-[#8B5CF6] hover:text-[#a78bfa] transition-colors">
                Reset Key
              </button>
            </div>
            <div className="relative">
              <Lock size={13} strokeWidth={1.5} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
              <input
                type="password"
                value={accessKey}
                onChange={(e) => setAccessKey(e.target.value)}
                className="w-full h-10 bg-black border border-[#222222] pl-9 pr-3 font-mono text-sm text-white placeholder-white/30 outline-none focus:border-[#8B5CF6] transition-colors"
              />
            </div>
          </div>

          {/* Maintain session checkbox */}
          <label className="flex items-center gap-3 cursor-pointer group">
            <div className="relative">
              <input
                type="checkbox"
                checked={maintain}
                onChange={(e) => setMaintain(e.target.checked)}
                className="sr-only peer"
              />
              <div className="w-4 h-4 border border-[#333333] bg-black peer-checked:bg-[#8B5CF6] peer-checked:border-[#8B5CF6] transition-colors flex items-center justify-center">
                {maintain && (
                  <svg width="10" height="8" viewBox="0 0 10 8" fill="none">
                    <path d="M1 4L3.5 6.5L9 1" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                )}
              </div>
            </div>
            <span className="font-sans text-sm text-white/60 group-hover:text-white/80 transition-colors">
              Maintain session
            </span>
          </label>

          {/* Submit */}
          <button
            type="submit"
            disabled={loading}
            className="w-full h-11 bg-[#10B981] hover:bg-[#0d9668] disabled:opacity-60 text-black font-sans text-sm font-semibold uppercase tracking-wider flex items-center justify-center gap-2 transition-colors"
          >
            {loading ? 'Authenticating...' : (
              <>
                Authenticate Session
                <ArrowRight size={15} strokeWidth={2} />
              </>
            )}
          </button>
        </form>

        {/* Footer */}
        <div className="px-7 pb-6 text-center">
          <p className="font-mono text-[11px] text-white/25 leading-5">
            Unauthorized access is strictly prohibited.<br />
            All connection attempts are logged.
          </p>
        </div>
      </div>
    </div>
  )
}
