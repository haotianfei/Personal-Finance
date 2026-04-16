'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { cn } from '@/lib/utils'
import {
  LayoutDashboard, FileText, BarChart3, Upload, ChevronLeft, ChevronRight,
  Settings, FolderTree, Wallet, Download, Database, ChevronDown, Bell, PieChart
} from 'lucide-react'
import { useState } from 'react'

const NAV_ITEMS = [
  { href: '/', label: '仪表盘', icon: LayoutDashboard },
  { href: '/analysis', label: '数据分析', icon: BarChart3 },
  { href: '/allocation', label: '资产配置', icon: PieChart },
  { href: '/alerts', label: '资产预警', icon: Bell },
]

const DATA_MANAGEMENT_ITEMS = [
  { href: '/records', label: '资产记录', icon: FileText },
  { href: '/import', label: '数据导入', icon: Upload },
  { href: '/export', label: '数据导出', icon: Download },
]

const DB_MAINTENANCE_ITEMS = [
  { href: '/fund-types', label: '资产类型管理', icon: FolderTree },
  { href: '/accounts', label: '账户管理', icon: Wallet },
  { href: '/liquidity-ratings', label: '流动性评级管理', icon: Settings },
]

export default function Sidebar() {
  const pathname = usePathname()
  const [collapsed, setCollapsed] = useState(false)
  const [dataMenuExpanded, setDataMenuExpanded] = useState(true)

  // 检查是否在数据管理菜单下
  const isDataManagementActive = DATA_MANAGEMENT_ITEMS.some(item => 
    pathname.startsWith(item.href)
  )
  
  // 检查是否在数据库字段维护菜单下
  const isDbMaintenanceActive = DB_MAINTENANCE_ITEMS.some(item => 
    pathname.startsWith(item.href)
  )

  return (
    <aside
      className={cn(
        'h-screen bg-brand-950 text-white flex flex-col z-30 transition-all duration-200 shrink-0',
        collapsed ? 'w-16' : 'w-56'
      )}
    >
      {/* Logo */}
      <div className="h-16 flex items-center px-4 border-b border-white/10">
        {!collapsed && (
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-brand-500 flex items-center justify-center text-sm font-bold">
              资
            </div>
            <span className="text-base font-semibold tracking-tight">个人资产管理</span>
          </div>
        )}
        {collapsed && (
          <div className="w-8 h-8 rounded-lg bg-brand-500 flex items-center justify-center text-sm font-bold mx-auto">
            资
          </div>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-4 px-2 space-y-1 overflow-y-auto">
        {NAV_ITEMS.map((item) => {
          const active = item.href === '/'
            ? pathname === '/'
            : pathname.startsWith(item.href)
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-150',
                active
                  ? 'bg-brand-600/40 text-white'
                  : 'text-slate-400 hover:text-white hover:bg-white/5'
              )}
            >
              <item.icon size={20} className="shrink-0" />
              {!collapsed && <span>{item.label}</span>}
            </Link>
          )
        })}

        {/* Data Management Section */}
        {!collapsed ? (
          <div className="pt-2 space-y-1">
            <button
              onClick={() => setDataMenuExpanded(!dataMenuExpanded)}
              className={cn(
                'w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-150',
                isDataManagementActive || isDbMaintenanceActive
                  ? 'bg-brand-600/40 text-white'
                  : 'text-slate-400 hover:text-white hover:bg-white/5'
              )}
            >
              <div className="flex items-center gap-3">
                <Database size={20} className="shrink-0" />
                <span>数据管理</span>
              </div>
              <ChevronDown
                size={16}
                className={cn(
                  'transition-transform duration-200',
                  dataMenuExpanded ? 'rotate-180' : ''
                )}
              />
            </button>
            {dataMenuExpanded && (
              <div className="mt-1 ml-4 pl-4 border-l border-white/10 space-y-1">
                {/* 数据操作 */}
                <div className="text-xs text-slate-500 px-3 py-1">数据操作</div>
                {DATA_MANAGEMENT_ITEMS.map((item) => {
                  const active = pathname.startsWith(item.href)
                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      className={cn(
                        'flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150',
                        active
                          ? 'bg-brand-600/40 text-white'
                          : 'text-slate-400 hover:text-white hover:bg-white/5'
                      )}
                    >
                      <item.icon size={18} className="shrink-0" />
                      <span>{item.label}</span>
                    </Link>
                  )
                })}
                
                {/* 数据库字段维护 */}
                <div className="text-xs text-slate-500 px-3 py-1 mt-2">数据库字段维护</div>
                {DB_MAINTENANCE_ITEMS.map((item) => {
                  const active = pathname.startsWith(item.href)
                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      className={cn(
                        'flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150',
                        active
                          ? 'bg-brand-600/40 text-white'
                          : 'text-slate-400 hover:text-white hover:bg-white/5'
                      )}
                    >
                      <item.icon size={18} className="shrink-0" />
                      <span>{item.label}</span>
                    </Link>
                  )
                })}
              </div>
            )}
          </div>
        ) : (
          <Link
            href="/records"
            className={cn(
              'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-150',
              isDataManagementActive || isDbMaintenanceActive
                ? 'bg-brand-600/40 text-white'
                : 'text-slate-400 hover:text-white hover:bg-white/5'
            )}
          >
            <Database size={20} className="shrink-0" />
          </Link>
        )}
      </nav>

      {/* Collapse button */}
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="h-12 flex items-center justify-center border-t border-white/10 text-slate-400 hover:text-white transition-colors"
      >
        {collapsed ? <ChevronRight size={18} /> : <ChevronLeft size={18} />}
      </button>
    </aside>
  )
}
