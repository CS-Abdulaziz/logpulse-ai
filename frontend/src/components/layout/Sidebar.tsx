'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import {
  LayoutDashboard,
  AlertTriangle,
  BookOpen,
  FileText,
  Activity,
  FileQuestion,
  HeartPulse,
} from 'lucide-react'
import { cn } from '@/lib/utils'

const navItems = [
  { href: '/',           label: 'Dashboard',  icon: LayoutDashboard },
  { href: '/incidents',  label: 'Incidents',  icon: AlertTriangle   },
  { href: '/playbooks',  label: 'Playbooks',  icon: BookOpen        },
  { href: '/reports',    label: 'Reports',    icon: FileText        },
]

const bottomItems = [
  { href: '/docs',    label: 'Docs',    icon: FileQuestion },
  { href: '/support', label: 'Support', icon: HeartPulse   },
]

export function Sidebar() {
  const pathname = usePathname()

  const isActive = (href: string) =>
    href === '/' ? pathname === '/' : pathname.startsWith(href)

  return (
    <aside className="fixed left-0 top-12 bottom-0 w-60 flex flex-col border-r border-[#222222] bg-black z-30">
      {/* Cluster identity */}
      <div className="px-4 py-4 border-b border-[#222222]">
        <div className="flex items-center gap-2">
          <Activity size={14} className="text-[#10B981]" />
          <span className="text-white text-sm font-semibold font-sans">LogPulse AI</span>
        </div>
        <p className="text-[11px] font-mono text-white/40 mt-0.5 pl-5">Cluster-01-Operational</p>
      </div>

      {/* Primary nav */}
      <nav className="flex-1 py-2">
        {navItems.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className={cn(
              'flex items-center gap-3 px-4 h-9 text-sm transition-colors relative',
              isActive(href)
                ? 'text-white before:absolute before:left-0 before:top-0 before:bottom-0 before:w-0.5 before:bg-[#8B5CF6]'
                : 'text-white/50 hover:text-white/80'
            )}
          >
            <Icon size={15} strokeWidth={1.5} />
            <span className="font-sans">{label}</span>
          </Link>
        ))}
      </nav>

      {/* Bottom links */}
      <div className="border-t border-[#222222] py-2">
        {bottomItems.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className="flex items-center gap-3 px-4 h-9 text-sm text-white/40 hover:text-white/70 transition-colors"
          >
            <Icon size={15} strokeWidth={1.5} />
            <span className="font-sans">{label}</span>
          </Link>
        ))}
      </div>
    </aside>
  )
}
