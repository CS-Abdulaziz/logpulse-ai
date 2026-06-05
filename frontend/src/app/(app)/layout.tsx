import { TopNav } from '@/components/layout/TopNav'
import { Sidebar } from '@/components/layout/Sidebar'

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <TopNav />
      <Sidebar />
      <main className="ml-60 mt-12 min-h-[calc(100vh-48px)] bg-black">
        {children}
      </main>
    </>
  )
}
