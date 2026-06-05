'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { Settings, Bell, Activity } from 'lucide-react'
import { cn } from '@/lib/utils'

const tabs = [
  { href: '/',         label: 'Dashboard'          },
  { href: '/playbooks', label: 'Playbooks Library' },
  { href: '/archive',  label: 'Live Storage Archive' },
]

export function TopNav() {
  const pathname = usePathname()

  const isActive = (href: string) =>
    href === '/' ? pathname === '/' : pathname.startsWith(href)

  return (
    <header className="fixed top-0 left-0 right-0 h-12 flex items-center border-b border-[#222222] bg-black z-40">
      {/* Logo — aligns with sidebar width */}
      <div className="w-60 flex-shrink-0 flex items-center gap-2 px-4">
        <Activity size={16} className="text-[#8B5CF6]" />
        <span className="text-white text-sm font-semibold font-sans tracking-wide">LogPulse AI</span>
      </div>

      {/* Page tabs */}
      <nav className="flex items-end h-full gap-0 flex-1">
        {tabs.map(({ href, label }) => (
          <Link
            key={href}
            href={href}
            className={cn(
              'h-full flex items-center px-5 text-sm font-sans border-b-2 transition-colors',
              isActive(href)
                ? 'text-white border-white'
                : 'text-white/50 border-transparent hover:text-white/80'
            )}
          >
            {label}
          </Link>
        ))}
      </nav>

      {/* Right actions */}
      <div className="flex items-center gap-1 px-4">
        <button className="w-8 h-8 flex items-center justify-center text-white/50 hover:text-white transition-colors">
          <Settings size={16} strokeWidth={1.5} />
        </button>
        <button className="w-8 h-8 flex items-center justify-center text-white/50 hover:text-white transition-colors">
          <Bell size={16} strokeWidth={1.5} />
        </button>
      </div>
    </header>
  )
}
