import { useEffect, useLayoutEffect } from 'react'
import {
  LayoutDashboard, FileText, BarChart3, Upload, Settings,
  Download, Database, Bell, PieChart, Camera, Receipt, Target, Flag, Lightbulb, History, Images,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

/**
 * 导航外壳的唯一数据源（IA v1.0）。
 *
 * 桌面：工作区 5 个顶级 + 系统「设置」；记录/规划/数据为可展开分区。
 * 移动：底部 4 Tab（总览/记录/数据/分析）+「更多」；更多直接列规划与设置。
 * 设置中心是独立分区，不进入工作区分组；设置子页通过 TITLE_ROUTES 提供叶子标题。
 */
export type NavArea = 'workspace' | 'settings'

export interface NavGroup {
  id: string
  label: string
  icon: LucideIcon
  area: NavArea
  order: number
}

export interface NavMobileHint {
  mode: 'tab' | 'drawer'
  order?: number
  shortLabel?: string
}

export interface NavItem {
  id: string
  label: string
  icon: LucideIcon
  /** 叶子页面；分区节点可省略。 */
  href?: string
  group: string
  mobile: NavMobileHint
  children?: NavItem[]
  /** 分区同时也是入口页（如「数据」/data）时，精确命中可对父链接设 aria-current。 */
  branchIsPage?: boolean
}

/** 「更多」抽屉的显式分区：IA 要求直接列规划/设置，不出现父级第三层。 */
export interface NavDrawerSection {
  id: string
  label: string
  itemIds: string[]
}

export interface NavConfig {
  groups: NavGroup[]
  items: NavItem[]
  drawerSections: NavDrawerSection[]
}

export const APP_BRAND_TILE = '资'
export const APP_BRAND_NAME = '个人资产管理'
export const APP_REPO_URL = 'https://github.com/haotianfei/Personal-Finance'

export const NAV_CONFIG: NavConfig = {
  groups: [
    { id: 'workspace', label: '工作区', icon: LayoutDashboard, area: 'workspace', order: 10 },
    { id: 'system', label: '系统', icon: Settings, area: 'settings', order: 90 },
  ],
  items: [
    {
      id: 'overview', label: '总览', icon: LayoutDashboard, href: '/', group: 'workspace',
      mobile: { mode: 'tab', order: 1 },
    },
    {
      id: 'records', label: '记录', icon: FileText, href: '/records', group: 'workspace',
      mobile: { mode: 'tab', order: 2 },
      children: [
        { id: 'assets', label: '资产记录', icon: FileText, href: '/records', group: 'workspace', mobile: { mode: 'drawer' } },
        { id: 'expenses', label: '支出记录', icon: Receipt, href: '/expenses', group: 'workspace', mobile: { mode: 'drawer' } },
      ],
    },
    {
      id: 'analysis', label: '分析', icon: BarChart3, href: '/analysis', group: 'workspace',
      mobile: { mode: 'tab', order: 4 },
    },
    {
      id: 'planning', label: '规划', icon: Target, href: '/allocation', group: 'workspace',
      mobile: { mode: 'drawer' },
      children: [
        { id: 'allocation', label: '目标配置', icon: PieChart, href: '/allocation', group: 'workspace', mobile: { mode: 'drawer' } },
        { id: 'goals', label: '目标与达成度', icon: Flag, href: '/planning/goals', group: 'workspace', mobile: { mode: 'drawer' } },
        { id: 'advice', label: '建议', icon: Lightbulb, href: '/planning/advice', group: 'workspace', mobile: { mode: 'drawer' } },
        { id: 'reviews', label: '复盘', icon: History, href: '/planning/reviews', group: 'workspace', mobile: { mode: 'drawer' } },
        { id: 'alerts', label: '预警', icon: Bell, href: '/alerts', group: 'workspace', mobile: { mode: 'drawer' } },
      ],
    },
    {
      id: 'data', label: '数据', icon: Database, href: '/data', group: 'workspace',
      mobile: { mode: 'tab', order: 3 }, branchIsPage: true,
      children: [
        { id: 'screenshot', label: '截图录入', icon: Camera, href: '/screenshot', group: 'workspace', mobile: { mode: 'drawer' } },
        { id: 'evidence', label: '截图凭证', icon: Images, href: '/data/evidence', group: 'workspace', mobile: { mode: 'drawer' } },
        { id: 'import', label: 'CSV 导入', icon: Upload, href: '/import', group: 'workspace', mobile: { mode: 'drawer' } },
        { id: 'export', label: '数据导出', icon: Download, href: '/export', group: 'workspace', mobile: { mode: 'drawer' } },
      ],
    },
    {
      id: 'settings', label: '设置', icon: Settings, href: '/settings', group: 'system',
      mobile: { mode: 'drawer' },
    },
  ],
  drawerSections: [
    { id: 'planning', label: '规划', itemIds: ['allocation', 'goals', 'advice', 'reviews', 'alerts'] },
    { id: 'system', label: '设置', itemIds: ['settings'] },
  ],
}

/**
 * 设置叶子路由的标题：不进全局导航，但顶栏/面包屑必须显示叶子名而不是「设置」。
 * 全部设置页在这里收口；新增设置页时同步加一行。
 */
export const TITLE_ROUTES: Array<{ href: string; label: string }> = [
  { href: '/settings', label: '设置' },
  { href: '/settings/dictionaries', label: '字典与属性' },
  { href: '/settings/dictionaries/asset-classes', label: '资产类别' },
  { href: '/settings/dictionaries/institutions', label: '机构与渠道' },
  { href: '/settings/dictionaries/liquidity-levels', label: '流动性档位' },
  { href: '/settings/dictionaries/members', label: '成员' },
  { href: '/settings/dictionaries/tags', label: '标签' },
  { href: '/settings/dictionaries/expense-categories', label: '支出分类' },
  { href: '/settings/automation/alerts', label: '预警规则' },
  { href: '/settings/automation/alerts/import-export', label: '规则导入导出' },
  { href: '/settings/backup-restore', label: '备份与恢复' },
  { href: '/settings/audit', label: '操作日志' },
  { href: '/settings/source-groups', label: '来源组' },
  { href: '/settings/llm-providers', label: 'LLM 供应商' },
  { href: '/settings/about', label: '关于与诊断' },
  { href: '/planning/goals', label: '目标与达成度' },
  { href: '/planning/advice', label: '建议' },
  { href: '/planning/reviews', label: '复盘' },
  { href: '/data', label: '数据' },
]

// --- 派生视图：壳层只消费这些，不自行拼菜单 ---

function sortByOrder<T extends { order: number }>(items: T[]): T[] {
  return [...items].sort((a, b) => a.order - b.order)
}

export function groupsForArea(area: NavArea): NavGroup[] {
  return sortByOrder(NAV_CONFIG.groups.filter((group) => group.area === area))
}

export function itemsForGroup(groupId: string): NavItem[] {
  return NAV_CONFIG.items.filter((item) => item.group === groupId)
}

export function flattenItems(items: NavItem[] = NAV_CONFIG.items): NavItem[] {
  return items.flatMap((item) => [item, ...(item.children ? flattenItems(item.children) : [])])
}

export function itemsForArea(area: NavArea): NavItem[] {
  return groupsForArea(area).flatMap((group) => itemsForGroup(group.id))
}

export function findNavItem(id: string): NavItem | undefined {
  return flattenItems().find((item) => item.id === id)
}

export function isItemActive(pathname: string, item: NavItem): boolean {
  if (item.href && isActivePath(pathname, item.href)) return true
  return item.children?.some((child) => isItemActive(pathname, child)) ?? false
}

export function itemHref(item: NavItem): string | undefined {
  if (item.href) return item.href
  for (const child of item.children ?? []) {
    const href = itemHref(child)
    if (href) return href
  }
  return undefined
}

export const WORKSPACE_GROUPS = groupsForArea('workspace')
export const SETTINGS_GROUPS = groupsForArea('settings')
export const ALL_NAV_ITEMS: NavItem[] = flattenItems()
export const MOBILE_TABS: NavItem[] = NAV_CONFIG.items
  .filter((item) => item.mobile.mode === 'tab')
  .sort((a, b) => (a.mobile.order ?? 99) - (b.mobile.order ?? 99))
  .slice(0, 5)

export const DRAWER_SECTIONS: Array<NavDrawerSection & { items: NavItem[] }> =
  NAV_CONFIG.drawerSections.map((section) => ({
    ...section,
    items: section.itemIds
      .map((id) => findNavItem(id))
      .filter((item): item is NavItem => Boolean(item)),
  }))

export function isActivePath(pathname: string, href: string): boolean {
  return href === '/' ? pathname === '/' : pathname.startsWith(href)
}

export function isGroupActive(pathname: string, groupId: string): boolean {
  return itemsForGroup(groupId).some((item) => isItemActive(pathname, item))
}

/** 顶栏标题：导航项优先，其次设置叶子标题；最长 href 前缀。 */
export function resolveNavTitle(pathname: string): string {
  const candidates = [
    ...ALL_NAV_ITEMS.filter((item): item is NavItem & { href: string } => !!item.href),
    ...TITLE_ROUTES,
  ]
  const exact = candidates.filter((item) => item.href === pathname).at(-1)
  if (exact) return exact.label
  const prefixed = candidates
    .filter((item) => item.href !== '/' && pathname.startsWith(`${item.href}/`))
    .sort((a, b) => b.href.length - a.href.length)[0]
  return prefixed?.label ?? APP_BRAND_NAME
}

// --- 折叠偏好持久化 ---
export const SIDEBAR_COLLAPSED_KEY = 'sidebar_collapsed'

export function readPref(key: string): boolean | null {
  try {
    const saved = localStorage.getItem(key)
    return saved === null ? null : saved === '1'
  } catch {
    return null
  }
}

export function writePref(key: string, value: boolean) {
  try {
    localStorage.setItem(key, value ? '1' : '0')
  } catch {
    // Ignore write failures
  }
}

export const useIsomorphicLayoutEffect =
  typeof window !== 'undefined' ? useLayoutEffect : useEffect